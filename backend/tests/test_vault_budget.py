"""PT1–PT4 — key vault, resolution/fallback, metering, atomic budgets + concurrency."""

import threading

import pytest
from sqlalchemy import select

from app.core import secret_store
from app.db import SessionLocal
from app.models import AiUsage, SiteCheckRun, Tenant
from app.services import gateway
from app.services.site_check import usage as sc_usage

SECRET_KEY = "sk-tenant-own-key-abcd1234"


@pytest.fixture(autouse=True)
def _tenant_key():
    # The gateway now resolves the OpenAI key from the one shared secret
    # store (no per-tenant key any more) — seed a dummy value so `reserve`'s
    # resolve_key() call succeeds, matching the old conftest make_tenant seed.
    secret_store.set_secret("openai", "sk-test-tenant-key")
    yield


@pytest.fixture
def tenant_with_user(db, make_tenant, make_user, login):
    tenant = make_tenant("Vault Co", budget_cents=12)
    user = make_user(tenant, "vault@example.com")
    return tenant, user, login("vault@example.com")


def test_key_resolution_tenant_only(tenant_with_user, db):
    """Brick 10: the owner's Settings key (secret store) or nothing — no .env fallback."""
    tenant, _, _ = tenant_with_user
    secret_store.set_secret("openai", SECRET_KEY)
    key, source = gateway.resolve_key(tenant)
    assert source == "tenant" and key == SECRET_KEY
    secret_store.delete_secret("openai")
    with pytest.raises(gateway.NoTenantKey):
        gateway.resolve_key(tenant)


def test_reserve_settle_metering(db, tenant_with_user):
    tenant, user, _ = tenant_with_user
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id, app_slug="scanner",
                            model="gpt-4o")
    assert usage.status == "pending" and usage.reserved_cents == 5
    gateway.settle(db, usage, prompt_tokens=1400, completion_tokens=70)
    row = db.execute(select(AiUsage)).scalars().one()
    assert row.status == "settled"
    assert row.cost_cents == 1  # ceil(1400*0.25/1000 + 70*1.0/1000) = ceil(0.42)
    assert row.key_source == "tenant" and row.app_slug == "scanner"


def test_budget_blocks_before_any_call(db, make_tenant, make_user):
    tenant = make_tenant("Broke Co", budget_cents=4)  # below the 5c reserve
    user = make_user(tenant, "broke@example.com")
    with pytest.raises(gateway.BudgetExceeded):
        gateway.reserve(db, tenant_id=tenant.id, user_id=user.id, app_slug="scanner",
                            model="gpt-4o")
    assert db.execute(select(AiUsage)).scalars().first() is None


def test_cancel_releases_reservation(db, tenant_with_user):
    tenant, user, _ = tenant_with_user
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id, app_slug="scanner",
                            model="gpt-4o")
    gateway.cancel(db, usage)
    assert db.execute(select(AiUsage)).scalars().first() is None
    assert gateway.month_spend_cents(db, tenant.id) == 0


def test_concurrent_reservations_never_overspend(make_tenant, make_user):
    """PT4 concurrency: cap 12, reserve 5 → exactly 2 of N racing calls may fit."""
    tenant = make_tenant("Race Co", budget_cents=12)
    user = make_user(tenant, "race@example.com")
    results: list[bool] = []
    lock = threading.Lock()

    def attempt():
        session = SessionLocal()
        try:
            gateway.reserve(session, tenant_id=tenant.id, user_id=user.id, app_slug="scanner",
                            model="gpt-4o")
            ok = True
        except gateway.BudgetExceeded:
            ok = False
        finally:
            session.close()
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 2, results
    session = SessionLocal()
    try:
        total = gateway.month_spend_cents(session, tenant.id)
        cap = gateway.budget_cap_cents(session.get(Tenant, tenant.id))
        assert total <= cap
    finally:
        session.close()


def test_usage_summary_shape(db, tenant_with_user):
    from app.api.settings import compute_spending

    tenant, user, _ = tenant_with_user
    usage = gateway.reserve(db, tenant_id=tenant.id, user_id=user.id, app_slug="scanner",
                            model="gpt-4o")
    gateway.settle(db, usage, 1400, 70)
    body = compute_spending(db, tenant)
    assert body["spend_cents"] == 1
    assert body["budget_cents"] == 12
    # The Public data section is always present — zeros before any lookup.
    assert body["public_data"] == {
        "lookups": 0, "source_calls": 0,
        "estimated_paid_cost_cents": 0, "by_source": [],
    }


# ── §8.6 Spending "Public data" section + admin provider-usage CLI ─────────


def _public_data_run(db, tenant, user, address="500 E Anderson Ln, Austin"):
    run = SiteCheckRun(
        tenant_id=tenant.id, requested_by=user.id, address=address,
        normalized_address=address.lower(), status="completed",
    )
    db.add(run)
    db.flush()
    return run


def test_usage_summary_public_data_tenant_scoped_microdollar_exact(
        db, tenant_with_user, make_tenant, make_user):
    from app.api.settings import compute_spending

    tenant, user, _ = tenant_with_user
    other = make_tenant("Other Desk Co")
    other_user = make_user(other, "otherdesk@example.com")
    run = _public_data_run(db, tenant, user)
    other_run = _public_data_run(db, other, other_user)
    # 3 paid geocodes at $0.004 each → 12,000 µ$ = 1.2 cents, which must
    # round UP to 2 cents in the rendered field (rounding happens only at
    # render time — the service layer still aggregates exact microdollars).
    for _ in range(3):
        sc_usage.record_call(db, run=run, provider="arcgis",
                             service="geocode_stored")
    sc_usage.record_call(db, run=run, provider="nominatim",
                         service="geocode_stored")
    sc_usage.record_call(db, run=other_run, provider="arcgis",
                         service="geocode_stored")
    db.commit()

    pd = compute_spending(db, tenant)["public_data"]
    assert pd["lookups"] == 1  # the other tenant's run is invisible here
    assert pd["source_calls"] == 4
    assert pd["estimated_paid_cost_cents"] == 2  # ceil(12,000 µ$ / 10,000)
    assert pd["by_source"] == [
        {"source": "arcgis", "calls": 3, "estimated_cost_cents": 2},
        {"source": "nominatim", "calls": 1, "estimated_cost_cents": 0},
    ]

    other_pd = compute_spending(db, other)["public_data"]
    assert other_pd["lookups"] == 1  # the first tenant's run is invisible here
    assert other_pd["source_calls"] == 1
