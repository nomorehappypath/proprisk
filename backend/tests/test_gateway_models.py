"""Site Check brick 12 — gateway per-model pricing + microdollar arithmetic
(spec site_check.md §11). Postgres, house harness."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.config.settings import ModelPrice, settings
from app.core import secret_store
from app.models import AiUsage
from app.services import gateway

TERRA_20K = ModelPrice(
    input_usd_per_1k=Decimal("0.002"),
    output_usd_per_1k=Decimal("0.012"),
    max_output_tokens=20_000,
)


@pytest.fixture(autouse=True)
def _tenant_key():
    # gateway.reserve() resolves the OpenAI key from the one shared secret
    # store now (no per-tenant key any more) — seed a dummy value so these
    # reserve()/settle() calls succeed.
    secret_store.set_secret("openai", "sk-test-tenant-key")
    yield


@pytest.fixture
def office(make_tenant, make_user):
    tenant = make_tenant("Model Co", budget_cents=10_000)
    user = make_user(tenant, "models@example.com")
    return tenant, user


# ── Integer-safe arithmetic (§11.2) ────────────────────────────────────────

def test_ceildiv_rounds_up_and_guards_non_int_operands():
    assert gateway.ceildiv(100_001, 10_000) == 11   # cent boundary: never 10
    assert gateway.ceildiv(100_000, 10_000) == 10
    assert gateway.ceildiv(1, 1_000) == 1
    assert gateway.ceildiv(0, 1_000) == 0
    # The -(-a // b) identity is unsafe for Decimal — the guard must raise.
    with pytest.raises(TypeError):
        gateway.ceildiv(Decimal(1), 1_000)
    with pytest.raises(TypeError):
        gateway.ceildiv(1_000, Decimal(10))


def test_terra_numerical_example_49_cents(monkeypatch, office, db):
    """§11.2 REQUIRED (b): Terra 0.002/0.012 USD per 1k, estimated_input
    100,000 → padded 125,000 → 250,000 µ$ input; max_output 20,000 →
    240,000 µ$ output; reserved 490,000 µ$ = exactly 49 cents."""
    monkeypatch.setattr(settings, "openai_model_prices",
                        {**settings.openai_model_prices, "test-terra": TERRA_20K})
    assert gateway.reservation_cents(model="test-terra",
                                     estimated_input_tokens=100_000) == 49
    # Same figure through the real reserve() path, recorded on the row.
    tenant, user = office
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                            app_slug="site-check", model="test-terra",
                            estimated_input_tokens=100_000)
    assert usage.reserved_cents == 49 and usage.model == "test-terra"
    assert usage.status == "pending"


def test_fractional_intermediate_rounds_up(monkeypatch):
    """§11.2 REQUIRED (b2): 2,500 µ$/1k × 1,233 tokens = 3,082,500 / 1,000 =
    3,082.5 → MUST yield 3,083 µ$ (truncation's 3,082 fails)."""
    monkeypatch.setattr(settings, "openai_model_prices", {
        "frac-model": ModelPrice(input_usd_per_1k=Decimal("0.0025"),
                                 output_usd_per_1k=Decimal("0.01"),
                                 max_output_tokens=1),
    })
    assert gateway.tokens_cost_micro_usd(
        model="frac-model", input_tokens=1_233, output_tokens=0
    ) == 3_083


def test_cent_boundary_settlement(monkeypatch, office, db):
    """§11.2 REQUIRED (b3): a settlement totalling 100,001 µ$ MUST cost 11
    cents (truncation's 10 fails)."""
    monkeypatch.setattr(settings, "openai_model_prices", {
        **settings.openai_model_prices,
        # 0.100001 USD/1k = 100,001 µ$/1k → 1,000 input tokens = 100,001 µ$.
        "boundary-model": ModelPrice(input_usd_per_1k=Decimal("0.100001"),
                                     output_usd_per_1k=Decimal("0.000001"),
                                     max_output_tokens=1),
    })
    tenant, user = office
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                            app_slug="site-check", model="boundary-model")
    gateway.settle(db, usage, prompt_tokens=1_000, completion_tokens=0)
    row = db.execute(select(AiUsage)).scalars().one()
    assert row.cost_cents == 11 and row.status == "settled"


# ── Cap boundary (§11.2 (c), inherited brick-10 rule) ──────────────────────

@pytest.mark.parametrize(
    ("budget_cents", "fits"),
    [
        (6, True),   # reservation = remaining − 1 → accepted
        (5, True),   # reservation = exactly remaining → accepted (equality)
        (4, False),  # reservation = remaining + 1 → refused
    ],
)
def test_cap_boundary_month_spend_plus_reserve_lte_cap(db, make_tenant, make_user,
                                                       budget_cents, fits):
    tenant = make_tenant(f"Cap {budget_cents} Co", budget_cents=budget_cents)
    user = make_user(tenant, f"cap{budget_cents}@example.com")
    if fits:
        usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                                app_slug="scanner", model="gpt-4o")
        assert usage.reserved_cents == 5
    else:
        with pytest.raises(gateway.BudgetExceeded):
            gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                            app_slug="scanner", model="gpt-4o")
        assert db.execute(select(AiUsage)).scalars().first() is None


# ── Model-change metering (§11.1) ──────────────────────────────────────────

def test_model_change_meters_each_call_at_its_own_rates(db, office):
    """Change model between two calls → each ai_usage row records its actual
    model string and is priced at that model's rates."""
    tenant, user = office
    first = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                            app_slug="site-check", model="gpt-4o")
    gateway.settle(db, first, prompt_tokens=100_000, completion_tokens=10_000)
    second = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                             app_slug="site-check", model="gpt-5.6-sol")
    gateway.settle(db, second, prompt_tokens=100_000, completion_tokens=10_000)

    rows = {r.model: r for r in db.execute(select(AiUsage)).scalars()}
    # gpt-4o: 100k×2,500/1k + 10k×10,000/1k = 350,000 µ$ = 35 ¢
    assert rows["gpt-4o"].cost_cents == 35
    # gpt-5.6-sol: 100k×5,000/1k + 10k×30,000/1k = 800,000 µ$ = 80 ¢
    assert rows["gpt-5.6-sol"].cost_cents == 80
    assert all(r.status == "settled" for r in rows.values())


def test_unknown_model_is_refused_before_any_billing(db, office):
    tenant, user = office
    with pytest.raises(KeyError):
        gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                        app_slug="site-check", model="model-with-no-price")
    assert db.execute(select(AiUsage)).scalars().first() is None


# ── Reservation-leak: every failure branch ends terminal (§11.3) ───────────

def test_every_branch_ends_in_a_terminal_status(db, office):
    tenant, user = office

    settled = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                              app_slug="site-check", model="gpt-5.6-terra")
    gateway.settle(db, settled, prompt_tokens=10, completion_tokens=10)

    released = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                               app_slug="site-check", model="gpt-5.6-terra")
    gateway.release(db, released)

    cancelled = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                                app_slug="site-check", model="gpt-5.6-terra")
    gateway.cancel(db, cancelled)

    statuses = sorted(s for (s,) in db.execute(select(AiUsage.status)).all())
    assert statuses == ["released", "settled"]  # cancelled row is deleted
    # Released bills nothing; settled bills its cost.
    assert gateway.month_spend_cents(db, tenant.id) == settled.cost_cents


def test_settle_failed_is_durable_budget_visible_and_repaired(db, office):
    """§11.2 REQUIRED: injected settlement failure → durable settle_failed at
    the RESERVED amount (budget still enforced); the reaper repairs it —
    settled from recorded token counts when present, settled_estimated at the
    reserved amount otherwise."""
    tenant, user = office

    # Reserve both BEFORE failing settlement — a reserve() would run the
    # reaper and repair an existing settle_failed row straight away.
    with_tokens = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                                  app_slug="site-check", model="gpt-4o")
    without_tokens = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                                     app_slug="site-check", model="gpt-5.6-terra")
    gateway.mark_settle_failed(db, with_tokens, prompt_tokens=100_000,
                               completion_tokens=10_000)
    gateway.mark_settle_failed(db, without_tokens)

    db.expire_all()
    rows = {r.id: r for r in db.execute(select(AiUsage)).scalars()}
    assert all(r.status == "settle_failed" for r in rows.values())
    # Budget-visible at the reserved amount — the cap can't be leaked past.
    assert gateway.month_spend_cents(db, tenant.id) == sum(
        r.reserved_cents for r in rows.values()
    )

    # The reaper (backstop inside the next reserve) repairs both rows.
    trigger = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                              app_slug="site-check", model="gpt-5.6-terra")
    db.expire_all()
    repaired_with = db.get(AiUsage, with_tokens.id)
    assert repaired_with.status == "settled"
    assert repaired_with.cost_cents == 35  # priced from the recorded response
    repaired_without = db.get(AiUsage, without_tokens.id)
    assert repaired_without.status == "settled_estimated"
    assert repaired_without.cost_cents == repaired_without.reserved_cents
    gateway.release(db, trigger)

    remaining = [s for (s,) in db.execute(select(AiUsage.status)).all()]
    assert "pending" not in remaining and "settle_failed" not in remaining


def test_stale_pending_row_is_released_by_the_reaper(db, office):
    """No row remains pending beyond one reaper cycle (§11.2 invariant)."""
    tenant, user = office
    leaked = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                             app_slug="site-check", model="gpt-5.6-terra")
    db.execute(text("UPDATE ai_usage SET created_at = now() - interval '1 hour' "
                    "WHERE id = :id"), {"id": leaked.id})
    db.commit()

    trigger = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                              app_slug="site-check", model="gpt-5.6-terra")
    db.expire_all()
    assert db.get(AiUsage, leaked.id).status == "released"
    assert gateway.month_spend_cents(db, tenant.id) == trigger.reserved_cents
    gateway.release(db, trigger)


def test_reservation_at_least_covers_settlement(db, office):
    """§11.2 REQUIRED (a): with a token estimate, the reservation is >= the
    settled cost for actuals within the padded estimate + max output."""
    tenant, user = office
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id,
                            app_slug="site-check", model="gpt-5.6-terra",
                            estimated_input_tokens=8_000)
    reserved = usage.reserved_cents
    gateway.settle(db, usage, prompt_tokens=9_500, completion_tokens=4_000)
    assert usage.cost_cents <= reserved


# ── §11.1 startup pricing validation (Settings instantiation) ──────────────
# "Schema change isn't done until an endpoint is smoked" class: the
# model-pricing cross-validator only fires when Settings instantiates, so
# it gets a real instantiation test per configured model field (§17 GWT 28
# startup half).

@pytest.mark.parametrize("field", [
    "site_check_narrative_model",
    "site_check_vision_model",
    "site_check_document_model",
])
def test_settings_reject_unpriced_configured_model_at_startup(field):
    from pydantic import ValidationError

    from app.config.settings import Settings

    with pytest.raises(ValidationError, match="openai_model_prices is missing"):
        Settings(**{field: "gpt-not-in-the-rate-card"})


def test_settings_accept_newly_priced_model_and_shipped_config():
    from app.config.settings import Settings

    shipped = Settings()  # the shipped configuration must instantiate
    for model in (
        shipped.site_check_narrative_model,
        shipped.site_check_vision_model,
        shipped.site_check_document_model,
    ):
        assert model in shipped.openai_model_prices

    grown = Settings(
        openai_model_prices={
            **shipped.openai_model_prices, "gpt-custom": TERRA_20K,
        },
        site_check_narrative_model="gpt-custom",
    )
    assert grown.site_check_narrative_model == "gpt-custom"


def test_settings_cross_field_guards_fire_on_instantiation():
    from pydantic import ValidationError

    from app.config.settings import Settings

    with pytest.raises(ValidationError, match="site_check_expiring_notice_days"):
        Settings(site_check_expiring_notice_days=10, site_check_freshness_days=10)
    # §12: aerial chip reuse is capped by the freshness rule.
    capped = Settings(
        public_aerial_imagery_ttl_days=400, site_check_freshness_days=180,
    )
    assert capped.public_aerial_imagery_ttl_days == 180
