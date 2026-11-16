"""City-portal entitlement-reader contract tests.

Four layers, all on the house Postgres conftest:
  1. THE GUARD — a case whose cited page does not name it is DROPPED. This is
     the whole point of the module: a model asked "what is being built near
     here?" will happily invent a plausible subdivision, so nothing renders
     without a citation that was re-fetched and matched server-side;
  2. the §9.3 read-through cache — a second lookup makes NO provider call, and
     "get fresh data" bypasses it;
  3. the degradation ladder (no portal / no key / budget cap / rejected key /
     provider error / invalid JSON / everything dropped), each of which must
     leave the deterministic county plat records untouched;
  4. what the card actually says with and without reader results.
"""

import json
import socket
from types import SimpleNamespace

import httpx
import openai
import pytest
import requests
from sqlalchemy import select

from app.config.settings import settings
from app.core import secret_store
from app.models import AiUsage, SiteCheckSourceCache
from app.services import gateway
from app.services.site_check import entitlement_reader as er
from app.services.site_check.adapters import entitlement_pipeline as ep

MODEL = settings.entitlement_read_model


@pytest.fixture(autouse=True)
def _tenant_key():
    # The gateway resolves the OpenAI key from the one shared secret store
    # (no per-tenant key any more) — seed a dummy value so these tests
    # exercise the "has a key" path by default.
    secret_store.set_secret("openai", "sk-test-tenant-key")
    yield

PORTAL_URL = "https://www.huttotx.gov/154/Planning-Development"
ANNEX_URL = "https://www.huttotx.gov/212/Annexations"

# What the city's annexation page actually serves (shape copied from the live
# page: a table of name / ordinance / date / status).
ANNEX_HTML = """
<html><head><style>.x{color:red}</style></head><body>
<h1>Annexations</h1>
<table>
  <tr><td>Chandler Parkade</td><td>O-2021-045 (PDF)</td>
      <td>August 5, 2021</td><td>Annexed</td></tr>
  <tr><td>Kerlin Place &amp; Resub, Lot 2</td><td>O-2019-011</td>
      <td>March 7, 2019</td><td>Annexed</td></tr>
</table>
</body></html>
"""


def make_settings(**overrides):
    base = {
        "entitlement_read_enabled": True,
        "entitlement_read_model": MODEL,
        "entitlement_read_max_searches": 10,
        "entitlement_read_radius_m": 3200,
        "entitlement_read_max_cases": 12,
        "entitlement_read_validation_timeout_s": 5,
        "entitlement_read_validation_max_attempts": 2,
        "entitlement_read_cache_ttl_days": 7,
        "openai_model_prices": settings.openai_model_prices,
        "site_check_freshness_days": 180,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def entitlement_results(**fact_overrides):
    facts = {
        "jurisdiction": "HUTTO",
        "source_status": "not_wired_for_jurisdiction",
        "case_portal_name": "Hutto Planning & Development",
        "case_portal_url": PORTAL_URL,
        "search_radius_m": 800,
        "subdivision_radius_m": 3200,
        "subdivision_cases": [
            {"name": "Firefly Pointe Ph 2", "status": "FPA", "lot_count": 258,
             "distance_m": 901.0, "direction": "south"},
        ],
        "subdivision_case_count": 41,
    }
    facts.update(fact_overrides)
    return {"entitlement_pipeline": {"normalized_facts": facts}}


# ── Fake HTTP (the verification fetches) ───────────────────────────────────


class FakeRaw:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, _amt, decode_content=True):
        return self._body


class FakeResponse:
    def __init__(self, status=200, content_type="text/html", body=b""):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.raw = FakeRaw(body)
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Serves a fixed body per URL; anything unknown 404s (an invented URL)."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {ANNEX_URL: ANNEX_HTML})
        self.urls: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.calls.append((url, kwargs))
        page = self.pages.get(url)
        if page is None:
            return FakeResponse(status=404)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, bytes):
            return FakeResponse(content_type="application/pdf", body=page)
        return FakeResponse(body=page.encode())


# ── Fake OpenAI (Responses API) ────────────────────────────────────────────


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        text, input_tokens, output_tokens = outcome
        return SimpleNamespace(
            output_text=text,
            output=[SimpleNamespace(type="web_search_call"),
                    SimpleNamespace(type="message")],
            usage=SimpleNamespace(input_tokens=input_tokens,
                                  output_tokens=output_tokens),
        )


def patch_client(monkeypatch, outcomes):
    fake = FakeResponses(outcomes)
    monkeypatch.setattr(
        er, "_make_client",
        lambda api_key: SimpleNamespace(responses=fake),
    )
    return fake


def case(**overrides):
    base = {
        "name": "Chandler Parkade",
        "case_type": "annexation",
        "status": "Annexed",
        "date": "August 5, 2021",
        "applicant_or_developer": "Lennar Homes",
        "location_text": "North side of Chandler Road",
        "source_url": ANNEX_URL,
        "evidence_quote": "Chandler Parkade | O-2021-045 (PDF)",
    }
    base.update(overrides)
    return base


def model_json(cases, note="Hutto publishes an annexation table only."):
    return json.dumps({"cases": cases, "coverage_note": note})


# ── Shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def tenant_user(db, make_tenant, make_user):
    tenant = make_tenant("Entitlement Co", budget_cents=100_000)
    user = make_user(tenant, "entitlement@example.com")
    return tenant, user


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """This module is replay-only. A missing fake must fail immediately,
    never fall through to a city host or OpenAI."""
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, bytes):
            host = host.decode("utf-8", "replace")
        if isinstance(host, str) and (
            host in ("localhost", "::1") or host.startswith(("127.", "/"))
        ):
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(
            f"live network attempted in entitlement-reader tests: {address!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded)


def make_run(tenant, user, **overrides):
    fields = {
        "id": "run-er-1", "tenant_id": tenant.id, "requested_by": user.id,
        "address": "County Road 130 & Chandler Road, Hutto TX 78634",
        "county": "williamson", "parcel_id": "R482369",
        "lat": 30.590318, "lng": -97.552200,
        "mode": "check", "status": "completed", "derived": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def usage_rows(db):
    db.expire_all()
    return db.execute(select(AiUsage).order_by(AiUsage.id)).scalars().all()


def assert_no_pending_leak(db):
    assert all(row.status != "pending" for row in usage_rows(db))


def http_error(status, kind):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return kind("simulated", response=httpx.Response(status, request=request),
                body=None)


def build(db, run, results, monkeypatch, *, http=None, settings_obj=None):
    return er.build_entitlement_reads(
        db, run, results, settings_obj or make_settings(),
        http=http if http is not None else FakeSession(),
    )


# ── 1. THE GUARD ───────────────────────────────────────────────────────────


def test_a_fabricated_case_whose_url_lacks_the_name_is_dropped(
    db, tenant_user, monkeypatch,
):
    """The acceptance bar. The model returns one real case and one invented
    one that cites a real page which never names it. Only the real one may
    render, and the invention must be COUNTED, not silently swallowed."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([
        case(),
        case(name="Sunset Meadows Ranch Phase 4",
             applicant_or_developer="D.R. Horton",
             evidence_quote="Sunset Meadows Ranch Phase 4 — approved"),
    ]), 60_000, 2_500)])

    results = entitlement_results()
    out = build(db, make_run(tenant, user), results, monkeypatch)

    meta = out["entitlement_read_meta"]
    assert meta["status"] == "ok"
    assert meta["verified_count"] == 1
    assert meta["dropped_unverified"] == 1

    read = results["entitlement_pipeline"]["normalized_facts"]["city_case_reads"]
    assert [c["name"] for c in read["cases"]] == ["Chandler Parkade"]
    assert read["cases"][0]["verification"] == "name_matched"
    assert read["cases"][0]["applicant_or_developer"] == "Lennar Homes"
    assert read["dropped_unverified"] == 1
    assert read["returned_count"] == 2


def test_a_case_citing_an_unreachable_url_is_dropped():
    """An unreachable citation is not a citation."""
    http = FakeSession()
    ok, reason = er.verify_case(
        http, case(source_url="https://www.huttotx.gov/999/Invented"),
        timeout_s=5,
    )
    assert (ok, reason) == (False, "source_unreachable")


def test_a_case_with_no_source_url_is_dropped():
    ok, reason = er.verify_case(FakeSession(), case(source_url=""), timeout_s=5)
    assert (ok, reason) == (False, "no_source_url")
    ok, reason = er.verify_case(
        FakeSession(), case(source_url="not-a-url"), timeout_s=5)
    assert (ok, reason) == (False, "no_source_url")


def test_verification_is_punctuation_insensitive_but_not_word_soup():
    """'Kerlin Place & Resub, Lot 2' on the page matches the same name
    punctuated differently — but a name whose words merely appear scattered
    does NOT."""
    http = FakeSession()
    # "&amp;" and the comma normalise away, so the differently punctuated
    # county spelling matches the city's spelling outright.
    ok, reason = er.verify_case(
        http, case(name="Kerlin Place (Resub Lot 2)", source_url=ANNEX_URL),
        timeout_s=5)
    assert (ok, reason) == (True, "name_matched")

    # Only a trailing qualifier the city omits falls back to the head — and
    # the head must still be substantial and appear verbatim.
    ok, reason = er.verify_case(
        http, case(name="Chandler Parkade, Lot 8 Replat", source_url=ANNEX_URL),
        timeout_s=5)
    assert (ok, reason) == (True, "name_matched_partial")

    # Words of real names, rearranged, must NOT satisfy the guard.
    ok, reason = er.verify_case(
        http, case(name="Place Chandler Kerlin Estates", source_url=ANNEX_URL),
        timeout_s=5)
    assert (ok, reason) == (False, "name_not_on_page")

    # Too short a head to stand alone.
    ok, reason = er.verify_case(
        http, case(name="Oak, Ridge Farms", source_url=ANNEX_URL), timeout_s=5)
    assert (ok, reason) == (False, "name_not_on_page")


def test_a_pdf_source_is_extracted_and_matched(monkeypatch):
    monkeypatch.setattr(
        er, "_pdf_to_text",
        lambda raw: "Planning & Zoning agenda: Firefly Pointe Ph 3 preliminary "
                    "plat, applicant Lennar Homes of Texas.",
    )
    http = FakeSession({"https://hutto.example/agenda.pdf": b"%PDF-1.7 binary"})
    ok, reason = er.verify_case(
        http, case(name="Firefly Pointe Ph 3",
                   source_url="https://hutto.example/agenda.pdf"),
        timeout_s=5)
    assert (ok, reason) == (True, "name_matched")


def test_html_is_stripped_before_matching():
    """A name split across markup must still match; script/style text must
    not be able to satisfy the guard on its own."""
    page = "<p>Firefly <b>Pointe</b> Ph 2</p><script>var x='Ghost Ranch'</script>"
    http = FakeSession({"https://hutto.example/x": page})
    ok, _ = er.verify_case(
        http, case(name="Firefly Pointe Ph 2",
                   source_url="https://hutto.example/x"), timeout_s=5)
    assert ok is True
    ok, reason = er.verify_case(
        http, case(name="Ghost Ranch", source_url="https://hutto.example/x"),
        timeout_s=5)
    assert (ok, reason) == (False, "name_not_on_page")


def test_one_fetch_per_distinct_url(db, tenant_user, monkeypatch):
    """Three cases off one city page must not re-download it three times."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([
        case(), case(name="Kerlin Place"), case(name="Chandler Parkade"),
    ]), 60_000, 2_000)])
    http = FakeSession()
    build(db, make_run(tenant, user), entitlement_results(), monkeypatch,
          http=http)
    assert http.urls == [ANNEX_URL]


def test_validation_timeout_and_attempt_ceiling_are_hard():
    """The validation policy bypasses the shared session's timeout floor and
    transport retries. Exactly the configured attempts occur, each carrying
    the configured connect/read timeout tuple."""
    class AlwaysSlow:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise requests.ReadTimeout("city portal stalled")

    retry_free = AlwaysSlow()
    shared = SimpleNamespace(no_transport_retry=retry_free)
    ok, reason = er.verify_case(
        shared,
        case(),
        timeout_s=3,
        max_attempts=2,
    )

    assert (ok, reason) == (False, "source_unreachable")
    assert len(retry_free.calls) == 2
    assert all(call[1]["timeout"] == (3, 3) for call in retry_free.calls)


def test_subdivision_geometry_keeps_a_map_centroid_for_project_markers():
    ring = [[-97.56, 30.58], [-97.55, 30.58], [-97.55, 30.59],
            [-97.56, 30.59], [-97.56, 30.58]]
    assert ep._polygon_centroid([ring]) == pytest.approx((30.585, -97.555))


def test_every_case_dropped_renders_nothing_and_says_so(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([
        case(name="Invented Ranch Estates"),
        case(name="Another Invention Sub"),
    ]), 60_000, 2_000)])
    results = entitlement_results()
    out = build(db, make_run(tenant, user), results, monkeypatch)

    meta = out["entitlement_read_meta"]
    assert meta["status"] == "all_dropped"
    assert meta["dropped_unverified"] == 2
    assert meta["message"] == er.MSG_ALL_DROPPED
    facts = results["entitlement_pipeline"]["normalized_facts"]
    assert facts["city_case_reads"]["cases"] == []
    # The deterministic records are untouched.
    assert facts["subdivision_case_count"] == 41


def test_dropped_cases_are_logged_for_diagnosis(db, tenant_user, monkeypatch, caplog):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json(
        [case(name="Invented Ranch Estates")]), 60_000, 2_000)])
    with caplog.at_level("WARNING", logger="propdesk.site_check.entitlement_reader"):
        build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    assert any(
        "DROPPED unverified case" in record.message
        and "Invented Ranch Estates" in str(record.args)
        for record in caplog.records
    )


# ── 2. Cache reuse (§9.3 read-through) ─────────────────────────────────────


def test_a_verified_read_is_cached_and_reused_without_a_provider_call(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(model_json([case()]), 60_000, 2_000)])
    first = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    db.commit()
    assert first["entitlement_read_meta"]["cache"] == "miss"
    assert fake.calls == 1

    row = db.execute(
        select(SiteCheckSourceCache).where(
            SiteCheckSourceCache.source_id == er.SOURCE_ID)
    ).scalars().one()
    # SHORT ttl — an active case moves on a two-to-four-week cadence.
    assert (row.expires_at - row.fetched_at).days == 7
    # The per-run ai_call_id must never be frozen into the shared row.
    assert "_meta" not in row.payload

    results = entitlement_results()
    second = build(db, make_run(tenant, user, id="run-er-2"), results,
                   monkeypatch)
    assert second["entitlement_read_meta"]["cache"] == "hit"
    assert fake.calls == 1  # no second provider call
    read = results["entitlement_pipeline"]["normalized_facts"]["city_case_reads"]
    assert [c["name"] for c in read["cases"]] == ["Chandler Parkade"]
    assert len(usage_rows(db)) == 1


def test_fresh_mode_bypasses_the_cache(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(model_json([case()]), 60_000, 2_000)] * 2)
    build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    db.commit()

    out = build(db, make_run(tenant, user, id="run-er-3", mode="fresh"),
                entitlement_results(), monkeypatch)
    assert out["entitlement_read_meta"]["cache"] == "miss"
    assert fake.calls == 2


def test_an_empty_read_is_never_cached(db, tenant_user, monkeypatch):
    """Freezing "nothing found" for a week would hide a case filed tomorrow."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([]), 60_000, 300)])
    build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    db.commit()
    assert db.execute(
        select(SiteCheckSourceCache).where(
            SiteCheckSourceCache.source_id == er.SOURCE_ID)
    ).scalars().first() is None


def test_cache_layer_failure_degrades_without_failing_the_run(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user

    def _boom(*args, **kwargs):
        raise RuntimeError("cache table exploded")

    monkeypatch.setattr(er.source_cache, "get_or_fetch", _boom)
    results = entitlement_results()
    out = build(db, make_run(tenant, user), results, monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "fallback_provider_error"
    assert "city_case_reads" not in results["entitlement_pipeline"]["normalized_facts"]


# ── 3. The degradation ladder ──────────────────────────────────────────────


def test_disabled_makes_no_call(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [])
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch,
                settings_obj=make_settings(entitlement_read_enabled=False))
    assert out["entitlement_read_meta"]["status"] == "disabled"
    assert fake.calls == 0


def test_no_portal_on_file_skips_the_call(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [])
    out = build(db, make_run(tenant, user),
                entitlement_results(case_portal_url=""), monkeypatch)
    meta = out["entitlement_read_meta"]
    assert meta["status"] == "skipped_no_portal"
    assert meta["message"] == er.MSG_NO_PORTAL
    assert fake.calls == 0


def test_no_entitlement_card_at_all_is_a_clean_skip(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    out = build(db, make_run(tenant, user), {}, monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "no_entitlement_result"


def test_no_key(db, make_tenant, make_user, monkeypatch):
    tenant = make_tenant("Keyless Co", budget_cents=100_000)
    user = make_user(tenant, "keyless-er@example.com")
    monkeypatch.setattr(
        er.gateway, "resolve_key",
        lambda t: (_ for _ in ()).throw(gateway.NoTenantKey("no key")),
    )
    results = entitlement_results()
    out = build(db, make_run(tenant, user), results, monkeypatch)
    meta = out["entitlement_read_meta"]
    assert meta["status"] == "fallback_no_key"
    assert meta["message"] == er.MSG_NO_KEY
    assert usage_rows(db) == []
    # The county records still render.
    assert results["entitlement_pipeline"]["normalized_facts"][
        "subdivision_case_count"] == 41


def test_budget_cap(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    monkeypatch.setattr(
        er.gateway, "reserve",
        lambda *a, **k: (_ for _ in ()).throw(gateway.BudgetExceeded("cap")),
    )
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    meta = out["entitlement_read_meta"]
    assert meta["status"] == "fallback_budget_cap"
    assert meta["message"] == er.MSG_BUDGET_CAP
    assert_no_pending_leak(db)


def test_rejected_key_releases_the_reservation(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [http_error(401, openai.AuthenticationError)])
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "fallback_invalid_key"
    assert out["entitlement_read_meta"]["message"] == er.MSG_INVALID_KEY
    assert [row.status for row in usage_rows(db)] == ["released"]
    assert_no_pending_leak(db)


def test_provider_error_releases_the_reservation(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [http_error(503, openai.InternalServerError)])
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "fallback_provider_error"
    assert out["entitlement_read_meta"]["message"] == er.MSG_UNAVAILABLE
    assert [row.status for row in usage_rows(db)] == ["released"]


def test_invalid_json_is_still_metered_and_degrades(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [("not json at all", 60_000, 40)])
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "fallback_invalid_output"
    # The model answered — those tokens are real and stay billed.
    rows = usage_rows(db)
    assert [row.status for row in rows] == ["settled"]
    assert (rows[0].prompt_tokens, rows[0].completion_tokens) == (60_000, 40)
    assert_no_pending_leak(db)


def test_no_cases_found_is_an_honest_empty_answer(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([]), 60_000, 200)])
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    meta = out["entitlement_read_meta"]
    assert meta["status"] == "no_cases_found"
    assert meta["message"] == er.MSG_NONE_FOUND
    assert [row.status for row in usage_rows(db)] == ["settled"]


def test_an_unexpected_bug_degrades_and_never_raises(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    monkeypatch.setattr(
        er, "_build",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = build(db, make_run(tenant, user), entitlement_results(), monkeypatch)
    assert out["entitlement_read_meta"]["status"] == "fallback_provider_error"


# ── 4. The metered call itself ─────────────────────────────────────────────


def test_the_call_uses_the_web_search_tool_and_a_strict_schema(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(model_json([case()]), 60_000, 2_000)])
    build(db, make_run(tenant, user), entitlement_results(), monkeypatch)

    kwargs = fake.kwargs[0]
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["max_tool_calls"] == 10
    schema = kwargs["text"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["strict"] is True
    assert schema["schema"]["properties"]["cases"]["items"]["required"] == list(
        er.CASE_FIELDS)
    # The deterministic plat names are handed over as look-up clues.
    assert "Firefly Pointe Ph 2" in kwargs["input"]
    assert "2.0 miles" in kwargs["input"]

    rows = usage_rows(db)
    assert len(rows) == 1
    assert rows[0].status == "settled"
    assert rows[0].model == MODEL
    assert (rows[0].prompt_tokens, rows[0].completion_tokens) == (60_000, 2_000)


def test_model_output_is_sanitized_and_capped(db, tenant_user, monkeypatch):
    """Raw model text never reaches the card unsanitized."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([case(
        name="Chandler Parkade",
        location_text="<script>alert(1)</script>North   of\nChandler Road",
        applicant_or_developer="L" * 900,
    )]), 60_000, 2_000)])
    results = entitlement_results()
    build(db, make_run(tenant, user), results, monkeypatch)
    row = results["entitlement_pipeline"]["normalized_facts"][
        "city_case_reads"]["cases"][0]
    assert "<" not in row["location_text"]
    assert row["location_text"] == "alert(1) North of Chandler Road"
    # Table-cell fields get the tight budget, not the free-text one.
    assert len(row["applicant_or_developer"]) == er._MAX_SHORT_CHARS


def test_a_paragraph_status_is_cut_to_a_cell_sized_phrase(
    db, tenant_user, monkeypatch,
):
    """The live Hutto read answered `status` with a two-clause sentence
    carrying 'off-site WWL-B construction' — a table cell is not the place
    for a paragraph of the source document's own jargon."""
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([case(
        status="Recently accepted infrastructure improvements; the December "
               "5, 2024 inspection letter states that off-site WWL-B "
               "construction was still ongoing.",
        location_text="FM 1660 right-of-way; the construction record also "
                      "identifies Twilight Breeze Way, Painted Night Road, "
                      "Little Sparks Loop, Ghost Stories Way, and Bright "
                      "Night Lane.",
    )]), 60_000, 2_000)])
    results = entitlement_results()
    build(db, make_run(tenant, user), results, monkeypatch)
    row = results["entitlement_pipeline"]["normalized_facts"][
        "city_case_reads"]["cases"][0]
    assert row["status"] == "Recently accepted infrastructure improvements"
    assert "WWL-B" not in row["status"]
    assert row["location_text"] == "FM 1660 right-of-way"
    assert len(row["location_text"]) <= er._MAX_CELL_CHARS


def test_a_case_with_no_name_or_url_is_discarded_before_any_fetch(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(model_json([
        case(name=""), case(source_url=""), case(),
    ]), 60_000, 2_000)])
    results = entitlement_results()
    out = build(db, make_run(tenant, user), results, monkeypatch)
    read = results["entitlement_pipeline"]["normalized_facts"]["city_case_reads"]
    # Two unusable rows never became candidates at all.
    assert read["returned_count"] == 1
    assert out["entitlement_read_meta"]["dropped_unverified"] == 0
