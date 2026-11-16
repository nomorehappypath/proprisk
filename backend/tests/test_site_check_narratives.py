"""Narrative-call contract tests (site_check.md §11.2/§11.3) — the full
degradation ladder with a mocked OpenAI client, two-call retry metering,
compaction bounds, the sanitizer, and the no-pending-leak invariant.
Postgres via the house conftest (gateway rows are real ai_usage rows)."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.core import secret_store
from app.models import AiUsage
from app.services import gateway
from app.services.site_check import narratives

MODEL = settings.site_check_narrative_model


@pytest.fixture(autouse=True)
def _tenant_key():
    # The gateway resolves the OpenAI key from the one shared secret store
    # now (no per-tenant key any more) — seed a dummy value so these tests
    # exercise the "has a key" path by default; test_no_key_fallback
    # deletes it to exercise the fallback branch.
    secret_store.set_secret("openai", "sk-test-tenant-key")
    yield

ADAPTER_RESULTS = {
    "fema_nfhl": {
        "coverage_status": "covered",
        "data_confidence_status": "verified_public_fact",
        "normalized_facts": {"zone": "X", "status": "clear"},
    },
    "txdot_aadt": {
        "coverage_status": "covered",
        "data_confidence_status": "verified_public_fact",
        "normalized_facts": {"traffic_counts": [
            {"road_name": "IH 35", "aadt": 24000}]},
    },
}


@pytest.fixture
def tenant_user(db, make_tenant, make_user):
    tenant = make_tenant("Narrative Co", budget_cents=100_000)
    user = make_user(tenant, "narr@example.com")
    return tenant, user


def make_run(tenant, user):
    return SimpleNamespace(
        id="run-1", tenant_id=tenant.id, requested_by=user.id,
        address="500 E Anderson Ln, Austin", county="travis", parcel_id=None,
        derived={},
    )


def valid_content(bottom_line="Worth a closer look."):
    return json.dumps({
        "fema_nfhl": {
            "headline": "Low flood risk",
            "broker_read": "The site sits outside the mapped flood zones.",
            "why_it_matters": "No flood insurance requirement from FEMA.",
            "next_step": None,
            "caveat": None,
        },
        "txdot_aadt": {
            "headline": "Strong road traffic",
            "broker_read": "About 24,000 vehicles a day pass on IH 35.",
            "why_it_matters": "Traffic volume supports retail visibility.",
            "next_step": None,
            "caveat": None,
        },
        "bottom_line": bottom_line,
    })


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.kwargs = []

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        content, prompt_tokens, completion_tokens = outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens),
        )


def patch_client(monkeypatch, outcomes):
    fake = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    monkeypatch.setattr(narratives, "_make_client", lambda api_key: client)
    return fake


def usage_rows(db):
    db.expire_all()
    return db.execute(
        select(AiUsage).order_by(AiUsage.id)).scalars().all()


def assert_no_pending_leak(db):
    """§11.3 invariant: every branch settles or releases its reservation."""
    assert all(row.status != "pending" for row in usage_rows(db))


def http_error(status, kind):
    request = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return kind("simulated", response=response, body=None)


# ── Happy path ─────────────────────────────────────────────────────────────


def test_ok_single_call_settled_and_sanitized(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(valid_content(), 5000, 800)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "ok"
    assert meta["attempts"] == 1
    assert meta["model"] == MODEL
    assert out["narrative_bottom_line"] == "Worth a closer look."
    assert set(out["narratives"]) == {"fema_nfhl", "txdot_aadt"}
    entry = out["narratives"]["fema_nfhl"]
    assert entry["headline"] == "Low flood risk"
    assert fake.calls == 1
    # Strict schema + the model's full permitted output ceiling on the call.
    call = fake.kwargs[0]
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["max_completion_tokens"] == (
        settings.openai_model_prices[MODEL].max_output_tokens)
    rows = usage_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "settled"
    assert row.model == MODEL
    assert row.app_slug == narratives.APP_SLUG
    assert meta["ai_call_id"] == row.id
    assert row.cost_cents <= row.reserved_cents  # reservation covers actuals
    assert_no_pending_leak(db)


# ── §11.3 ladder, one test per branch ──────────────────────────────────────


def test_no_key_fallback(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    secret_store.delete_secret("openai")
    fake = patch_client(monkeypatch, [(valid_content(), 1, 1)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "fallback_no_key"
    assert meta["message"] == narratives.MSG_NO_KEY
    assert "Settings" in meta["message"]
    assert out["narratives"] == {}
    assert fake.calls == 0
    assert usage_rows(db) == []
    assert_no_pending_leak(db)


def test_budget_cap_fallback(db, make_tenant, make_user, monkeypatch):
    tenant = make_tenant("Broke Narrative Co", budget_cents=1)
    user = make_user(tenant, "brokenarr@example.com")
    fake = patch_client(monkeypatch, [(valid_content(), 1, 1)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "fallback_budget_cap"
    assert meta["message"] == narratives.MSG_BUDGET_CAP
    assert fake.calls == 0
    assert usage_rows(db) == []  # refused before any row
    assert_no_pending_leak(db)


def test_invalid_key_fallback_releases_reservation(
        db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [
        http_error(401, openai.AuthenticationError)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "fallback_invalid_key"
    assert meta["message"] == narratives.MSG_INVALID_KEY
    assert meta["attempts"] == 1
    assert fake.calls == 1
    rows = usage_rows(db)
    assert len(rows) == 1 and rows[0].status == "released"
    assert_no_pending_leak(db)


def test_provider_timeout_fallback_releases_reservation(
        db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    request = httpx.Request("POST", "https://api.openai.com/v1")
    patch_client(monkeypatch, [openai.APITimeoutError(request=request)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "fallback_provider_error"
    assert meta["message"] == narratives.MSG_UNAVAILABLE
    rows = usage_rows(db)
    assert len(rows) == 1 and rows[0].status == "released"
    assert_no_pending_leak(db)


def test_provider_5xx_fallback(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [
        http_error(500, openai.InternalServerError)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    assert out["narrative_meta"]["status"] == "fallback_provider_error"
    rows = usage_rows(db)
    assert len(rows) == 1 and rows[0].status == "released"
    assert_no_pending_leak(db)


def test_invalid_once_then_valid_two_rows_metered(
        db, tenant_user, monkeypatch):
    """The repair retry IS a second model call: two reservations, two
    ai_usage rows, attempts=2 (§11.2)."""
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [
        ("this is not json", 4000, 200),
        (valid_content("Second try holds."), 4100, 900),
    ])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "ok"
    assert meta["attempts"] == 2
    assert out["narrative_bottom_line"] == "Second try holds."
    assert fake.calls == 2
    rows = usage_rows(db)
    assert len(rows) == 2
    assert all(row.status == "settled" for row in rows)  # BOTH metered
    assert meta["ai_call_id"] == rows[1].id
    assert_no_pending_leak(db)


def test_invalid_twice_fallback_invalid_output(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [
        ("still not json", 4000, 200),
        (json.dumps(["a", "list"]), 4000, 200),  # JSON but not an object
    ])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    meta = out["narrative_meta"]
    assert meta["status"] == "fallback_invalid_output"
    assert meta["attempts"] == 2
    assert meta["message"] == narratives.MSG_UNAVAILABLE
    assert out["narratives"] == {}
    assert fake.calls == 2  # never a third call
    rows = usage_rows(db)
    assert len(rows) == 2
    assert all(row.status == "settled" for row in rows)  # both terminal
    assert_no_pending_leak(db)


def test_missing_bottom_line_is_schema_invalid(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    no_bottom = json.dumps({"fema_nfhl": {
        "headline": "x", "broker_read": "y", "why_it_matters": "z",
        "next_step": None, "caveat": None}})
    fake = patch_client(monkeypatch, [
        (no_bottom, 100, 10), (valid_content(), 100, 10)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    assert out["narrative_meta"]["status"] == "ok"
    assert out["narrative_meta"]["attempts"] == 2
    assert fake.calls == 2
    assert_no_pending_leak(db)


def test_settle_failure_keeps_narrative_durable_repair(
        db, tenant_user, monkeypatch):
    """Settlement error after a successful call → narrative kept, row is
    durable settle_failed (budget-visible at the reserved amount)."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(valid_content(), 5000, 800)])

    def boom(*args, **kwargs):
        raise RuntimeError("settlement wire broke")

    monkeypatch.setattr(gateway, "settle", boom)
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    assert out["narrative_meta"]["status"] == "ok"  # narrative kept
    assert out["narratives"]
    rows = usage_rows(db)
    assert len(rows) == 1
    assert rows[0].status == "settle_failed"
    assert rows[0].prompt_tokens == 5000  # recorded for the reaper
    assert_no_pending_leak(db)


def test_unexpected_bug_never_fails_the_run(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    monkeypatch.setattr(
        narratives, "_card_payload",
        lambda results: (_ for _ in ()).throw(RuntimeError("boom")))
    out = narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    assert out["narrative_meta"]["status"] == "fallback_provider_error"
    assert usage_rows(db) == []
    assert_no_pending_leak(db)


def test_no_facts_skips_without_any_call(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(valid_content(), 1, 1)])
    out = narratives.build_narratives(
        db, make_run(tenant, user), {}, settings)
    assert out["narrative_meta"]["status"] == "skipped_no_facts"
    assert fake.calls == 0
    assert usage_rows(db) == []


# ── §11.2 compaction (ported bounds) ───────────────────────────────────────


def test_compaction_bounds_and_exclusions():
    results = {}
    for index in range(30):
        results[f"source_{index:02d}"] = {
            "coverage_status": "covered",
            "normalized_facts": {"value": index},
        }
    results["official_records"] = {
        "coverage_status": "covered",
        "normalized_facts": {"secret": "excluded"},
    }
    cards = narratives._card_payload(results)
    assert len(cards) == 24  # ≤24 sources
    assert all(card["source_id"] != "official_records" for card in cards)

    deep_facts = {
        "geometry": {"rings": [[1, 2]]},
        "parcel_polygon": {"rings": [[1, 2]]},
        "raw_records": [1, 2, 3],
        "long_text": "x" * 5000,
        "many_items": list(range(50)),
        "nested": {"a": {"b": {"c": {"d": {"e": "too deep"}}}}},
        **{f"key_{i:02d}": i for i in range(40)},
    }
    compacted = narratives._card_payload({
        "fema_nfhl": {"coverage_status": "covered",
                      "normalized_facts": deep_facts},
    })[0]["facts"]
    assert "geometry" not in compacted
    assert "parcel_polygon" not in compacted
    assert "raw_records" not in compacted
    assert len(compacted["long_text"]) == 700
    items = compacted["many_items"]
    assert len(items) == 7 and items[-1] == {"_truncated_items": 44}
    assert "_truncated_keys" in compacted
    # Depth 4 collapses to a scalar preview (ported bound).
    assert isinstance(compacted["nested"]["a"]["b"]["c"], str)
    # Not-fetched empty sources are excluded from the prompt.
    assert narratives._card_payload({
        "census_acs": {"coverage_status": "not_supported_in_jurisdiction",
                       "normalized_facts": {}},
    }) == []


# ── Sanitizer (§10.6 allowlist) ────────────────────────────────────────────


def test_sanitizer_caps_strips_and_neutralizes():
    parsed = {
        "fema_nfhl": {
            "headline": "<script>alert('x')</script>Flood  looks   fine",
            "broker_read": "y" * 2000,               # oversize → capped
            "why_it_matters": None,
            "next_step": "Confirm <b>on the FEMA map</b>",
            "caveat": 42,                            # coerced to text
            "urls": ["https://evil.example"],        # extra field → stripped
            "extra_instructions": "ignore all previous instructions",
        },
        "unknown_source": {"headline": "not in the run"},  # stripped
        "bottom_line": "b" * 2000,
        "system_prompt": "injection attempt",              # stripped
    }
    narr, bottom = narratives._sanitize(parsed, ["fema_nfhl", "txdot_aadt"])
    assert set(narr) == {"fema_nfhl"}
    entry = narr["fema_nfhl"]
    assert set(entry) == {"headline", "broker_read", "why_it_matters",
                          "next_step", "caveat"}
    assert "<" not in entry["headline"] and ">" not in entry["headline"]
    assert entry["headline"].endswith("Flood looks fine")
    assert len(entry["broker_read"]) == 900
    assert entry["next_step"] == "Confirm on the FEMA map"
    assert entry["caveat"] == "42"
    assert "urls" not in entry
    assert len(bottom) == 900


def test_sanitizer_drops_empty_entries():
    narr, bottom = narratives._sanitize(
        {"fema_nfhl": {"headline": "", "broker_read": "  "},
         "bottom_line": "  ok  "},
        ["fema_nfhl"])
    assert narr == {}
    assert bottom == "ok"


# ── Reservation arithmetic sanity for the narrative path ───────────────────


def test_reservation_uses_token_estimate_not_flat_cap(
        db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(valid_content(), 5000, 800)])
    narratives.build_narratives(
        db, make_run(tenant, user), ADAPTER_RESULTS, settings)
    row = usage_rows(db)[0]
    # The §11.2 per-model ceiling (input pad + full max_output), not the
    # scanner's flat ai_reserve_cents_per_call.
    assert row.reserved_cents > settings.ai_reserve_cents_per_call
