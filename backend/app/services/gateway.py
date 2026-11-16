"""AI gateway: per-tenant key resolution, atomic budget reservations, metering.

Spec contract (platform_core §3 + site_check.md §11): budget decisions
serialize on the tenant row (SELECT ... FOR UPDATE); a pending reservation is
inserted before any OpenAI request; the row settles to actual cost afterwards.

Money contract (site_check.md §11.1–§11.2):
- The LEDGER stays in integer CENTS (reserved_cents / cost_cents / the
  monthly budget). All price arithmetic is exact ALL-INTEGER MICRODOLLARS
  (1 USD = 1,000,000 µ$; 1 cent = 10,000 µ$) and converts to cents only at
  the ledger boundary, always by CEILING — a rounding step can never
  under-charge against the cap.
- Rates come from `openai_model_prices` (USD per 1,000 tokens, Decimal,
  ≤ 6 dp validated at startup → `rate × 1_000_000` is an EXACT integer).
- Effective monthly spend per ai_usage status: pending & settle_failed count
  reserved_cents; settled & settled_estimated count cost_cents; released
  counts 0. Cap rule (inherited brick 10): month_spend + reserve <= cap ⇒
  allowed (equality accepted).
- Settlement failure after a successful model call writes durable
  `settle_failed` (budget-visible at the RESERVED amount); the stale-
  reservation reaper repairs it — settled from recorded token counts when
  present, else finalized at the reserved amount as `settled_estimated`.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.settings import ModelPrice, settings
from app.core import secret_store
from app.models import AiUsage, Tenant

log = logging.getLogger("propdesk.gateway")

MICRO_PER_CENT = 10_000
# Reservation input padding (spec §11.2): padded = ceil(estimated × 1.25),
# computed as exact integers (ceil(5x/4)). Part of the reservation contract,
# not a tunable.
_PAD_NUMERATOR, _PAD_DENOMINATOR = 5, 4


class BudgetExceeded(Exception):
    """The tenant's monthly AI budget is used up."""


class NoTenantKey(Exception):
    """The tenant has no usable AI key — and there is no fallback (brick 10)."""


# ── Exact-integer money arithmetic (§11.2) ─────────────────────────────────

def ceildiv(a: int, b: int) -> int:
    """Ceiling division on INTEGERS ONLY: -(-a // b). Guarded because the
    identity is UNSAFE for Decimal operands (Decimal floor-division truncates
    toward zero — ceildiv(Decimal("1"), 1000) would yield 0, not 1)."""
    if type(a) is not int or type(b) is not int:
        raise TypeError("ceildiv operands must be plain ints — convert exactly first")
    return -(-a // b)


def _rate_micro_per_1k(rate: Decimal) -> int:
    """USD-per-1k Decimal → exact integer microdollars per 1k. The ≤6dp
    startup validation guarantees exactness; this guard is defensive."""
    micro = rate * 1_000_000
    if micro != micro.to_integral_value():
        raise ValueError(f"rate {rate} is not an exact number of microdollars per 1k")
    return int(micro)


def model_price(model: str) -> ModelPrice:
    entry = settings.openai_model_prices.get(model)
    if entry is None:
        raise KeyError(f"no pricing entry configured for model {model!r}")
    return entry


def tokens_cost_micro_usd(*, model: str, input_tokens: int, output_tokens: int) -> int:
    """The one per-token formula (§11.2) — used for reservations AND
    settlements: ceildiv(tokens × rate_micro_per_1k, 1000) per side."""
    entry = model_price(model)
    input_micro = ceildiv(
        int(input_tokens) * _rate_micro_per_1k(entry.input_usd_per_1k), 1_000
    )
    output_micro = ceildiv(
        int(output_tokens) * _rate_micro_per_1k(entry.output_usd_per_1k), 1_000
    )
    return input_micro + output_micro


def reservation_cents(*, model: str, estimated_input_tokens: int) -> int:
    """§11.2 reservation: pad the input estimate by 1.25 (exact integer
    ceiling), price padded input + the model's full max_output_tokens in
    microdollars, then ceiling-convert to ledger cents."""
    entry = model_price(model)
    padded_input = ceildiv(int(estimated_input_tokens) * _PAD_NUMERATOR, _PAD_DENOMINATOR)
    micro = tokens_cost_micro_usd(
        model=model, input_tokens=padded_input, output_tokens=entry.max_output_tokens
    )
    return ceildiv(micro, MICRO_PER_CENT)


# ── Key + budget resolution ────────────────────────────────────────────────

def resolve_key(tenant: Tenant) -> tuple[str, str]:
    """Returns (api_key, "tenant") or raises NoTenantKey. The owner's key
    from the Settings screen (stored in the local secret store) is the only
    source — nothing is read from the environment."""
    key = secret_store.get_secret("openai")
    if key is not None and key.get_secret_value():
        return key.get_secret_value(), "tenant"
    raise NoTenantKey


def budget_cap_cents(tenant: Tenant) -> int:
    if tenant.monthly_budget_cents is not None:
        return tenant.monthly_budget_cents
    return settings.monthly_budget_cents_default


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend_cents(db: Session, tenant_id: str, now: datetime | None = None) -> int:
    """Effective monthly spend per status (§11.1): pending & settle_failed →
    reserved_cents; settled & settled_estimated → cost_cents; released → 0."""
    now = now or datetime.now(UTC)
    start = _month_start(now)
    settled = db.execute(
        select(func.coalesce(func.sum(AiUsage.cost_cents), 0)).where(
            AiUsage.tenant_id == tenant_id,
            AiUsage.status.in_(("settled", "settled_estimated")),
            AiUsage.created_at >= start,
        )
    ).scalar_one()
    reserved = db.execute(
        select(func.coalesce(func.sum(AiUsage.reserved_cents), 0)).where(
            AiUsage.tenant_id == tenant_id,
            AiUsage.status.in_(("pending", "settle_failed")),
            AiUsage.created_at >= start,
        )
    ).scalar_one()
    return int(settled) + int(reserved)


# ── Reaper (backstop; runs under the tenant lock inside reserve) ───────────

def _reap_stale_rows(db: Session, tenant_id: str, now: datetime) -> None:
    """Repairs leaked rows so none stays non-terminal past one reaper cycle:
    - stale `pending` (crashed before/mid call) → `released` (bills nothing);
    - `settle_failed` → settled from the recorded token counts when present,
      else finalized at the reserved amount as `settled_estimated`."""
    stale_cutoff = now - timedelta(minutes=settings.gateway_pending_timeout_minutes)
    for row in db.execute(
        select(AiUsage).where(
            AiUsage.tenant_id == tenant_id,
            AiUsage.status == "pending",
            AiUsage.created_at < stale_cutoff,
        )
    ).scalars():
        row.status = "released"
        row.settled_at = now
    for row in db.execute(
        select(AiUsage).where(
            AiUsage.tenant_id == tenant_id, AiUsage.status == "settle_failed"
        )
    ).scalars():
        if row.prompt_tokens is not None and row.completion_tokens is not None:
            micro = tokens_cost_micro_usd(
                model=row.model,
                input_tokens=row.prompt_tokens,
                output_tokens=row.completion_tokens,
            )
            row.cost_cents = ceildiv(micro, MICRO_PER_CENT)
            row.status = "settled"
        else:
            row.cost_cents = row.reserved_cents
            row.status = "settled_estimated"
        row.settled_at = now
    db.flush()


# ── Reserve / settle / release ─────────────────────────────────────────────

def reserve(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    app_slug: str,
    model: str,
    estimated_input_tokens: int | None = None,
) -> AiUsage:
    """Atomically reserve budget for one AI call with the named model. Raises
    BudgetExceeded when it doesn't fit (month_spend + reserve <= cap ⇒
    allowed; equality accepted). Commits the reservation so it is visible to
    concurrent callers.

    With `estimated_input_tokens` the reservation is the §11.2 per-model
    ceiling; without it (the scanner path) it stays the flat
    `ai_reserve_cents_per_call` ceiling — scanner behavior unchanged."""
    model_price(model)  # unknown model fails loudly before any billing
    now = datetime.now(UTC)
    tenant = db.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    ).scalar_one()

    # Reap leaked rows (crashed calls, failed settlements) inside the same lock.
    _reap_stale_rows(db, tenant_id, now)

    if estimated_input_tokens is None:
        reserve_cents = settings.ai_reserve_cents_per_call
    else:
        reserve_cents = reservation_cents(
            model=model, estimated_input_tokens=estimated_input_tokens
        )
    _, key_source = resolve_key(tenant)
    if month_spend_cents(db, tenant_id, now) + reserve_cents > budget_cap_cents(tenant):
        db.commit()  # release the lock (reaping is kept)
        raise BudgetExceeded()

    usage = AiUsage(
        tenant_id=tenant_id,
        user_id=user_id,
        app_slug=app_slug,
        model=model,
        status="pending",
        reserved_cents=reserve_cents,
        key_source=key_source,
    )
    db.add(usage)
    db.commit()  # reservation visible; tenant row lock released
    return usage


def settle(db: Session, usage: AiUsage, prompt_tokens: int, completion_tokens: int) -> None:
    """Price the ACTUAL token counts with the same §11.2 formula (in
    microdollars, ceiling to cents at the ledger boundary). The difference to
    the reservation is released implicitly; if actuals exceed the reservation
    the cost stays at actuals and the shortfall is logged."""
    micro = tokens_cost_micro_usd(
        model=usage.model, input_tokens=prompt_tokens, output_tokens=completion_tokens
    )
    cost_cents = ceildiv(micro, MICRO_PER_CENT)
    if cost_cents > usage.reserved_cents:
        log.warning(
            "ai_usage %s: settled cost %s¢ exceeds reservation %s¢ (model %s)",
            usage.id, cost_cents, usage.reserved_cents, usage.model,
        )
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.cost_cents = cost_cents
    usage.status = "settled"
    usage.settled_at = datetime.now(UTC)
    db.commit()


def mark_settle_failed(
    db: Session,
    usage: AiUsage,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """Settlement errored AFTER a successful model call: write the durable
    settle_failed state (budget-visible at the RESERVED amount — the cap can't
    be leaked past). Any known provider token counts are recorded so the
    reaper can settle exactly; without them it finalizes at the reserved
    amount as settled_estimated."""
    db.rollback()  # discard whatever the failed settlement left half-done
    row = db.get(AiUsage, usage.id)
    if row is None or row.status not in ("pending", "settle_failed"):
        return
    row.prompt_tokens = prompt_tokens
    row.completion_tokens = completion_tokens
    row.status = "settle_failed"
    db.commit()


def release(db: Session, usage: AiUsage) -> None:
    """AI call failed after the reservation — terminal `released` status:
    bills nothing, keeps the metering history row."""
    usage.status = "released"
    usage.settled_at = datetime.now(UTC)
    db.commit()


def cancel(db: Session, usage: AiUsage) -> None:
    """AI call failed — the reservation is deleted, nothing is billed
    (brick-10 scanner path; site_check paths use release())."""
    db.delete(usage)
    db.commit()
