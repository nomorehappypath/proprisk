"""§8.6 provider metering: the ledger counts ACTUAL upstream requests.

Review finding 4 (2026-08-01) failed the previous shape because usage
rows were written once per logical adapter OUTCOME. With Nominatim and
ArcGIS missing and Google returning ZERO_RESULTS, one paid Google
request had gone out but the ledger wrote `arcgis/geocode_stored
calls=1 cost=4000 µ$` and no google row at all.

Everything here drives the REAL adapters over an offline session that
carries the same `provider_call_collector` hook as the hardened runtime
session, so the assertions are about the exact request sequence a
provider would have billed:

  - every Google attempt — success, ZERO_RESULTS, REQUEST_DENIED —
    records exactly one google/geocode call, and a disabled gate or a
    missing key records none;
  - a resolving tier does not erase the tiers that were called and
    missed;
  - a multi-attempt HTTP request records the billed retry;
  - the OSM → ArcGIS Places internal fallback records BOTH providers;
  - money is integer microdollars end to end (sub-cent aggregation is
    exact) and the monthly free tier is allocated atomically, so two
    concurrent runs cannot both spend the last free unit;
  - cache/reuse paths make no request and write no row, and rows
    survive run deletion (ON DELETE SET NULL).
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from threading import Barrier, Thread

import pytest
import requests
from sqlalchemy import select

from app.config.settings import get_settings
from app.core import secret_store
from app.db import SessionLocal
from app.models import SiteCheckProviderUsage, SiteCheckRun
from app.services.site_check import service as sc_service
from app.services.site_check import usage
from app.services.site_check.adapters import arcgis_geocode as ag
from app.services.site_check.adapters import arcgis_places as ap
from app.services.site_check.adapters import osm_overpass as osm
from app.services.site_check.adapters._base import http_session
from app.services.site_check.adapters._resilient_http import resilient_json
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.usage import (
    ProviderCallCollector,
    note_session_request,
)

ADDRESS = "1100 Congress Ave, Austin, TX 78701"


# ── Offline instrumented session ─────────────────────────────────────


def _response(payload, status: int = 200, url: str = "") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload).encode("utf-8")
    resp._content_consumed = True
    resp.headers["Content-Type"] = "application/json"
    resp.url = url
    resp.reason = ""
    return resp


class _MeteredStubSession(requests.Session):
    """Canned responses keyed by base URL, metered exactly like the
    production session: the collector is notified in a `finally`, so a
    request that raises still counts — providers bill those too.

    A route value is a list of (status, payload) or exception
    instances, served in order; the last entry repeats.
    """

    def __init__(self, routes: dict, *, collector=None):
        super().__init__()
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.provider_call_collector = collector
        self.no_transport_retry = self
        self.calls: list[str] = []

    def request(self, method, url, **kwargs):  # type: ignore[override]
        self.calls.append(url)
        try:
            queue = self.routes.get(url)
            if not queue:
                raise AssertionError(
                    f"unexpected offline request: {method} {url}"
                )
            step = queue[0] if len(queue) == 1 else queue.popleft()
            if isinstance(step, Exception):
                raise step
            status, payload = step
            resp = _response(payload, status=status, url=url)
            return resp
        finally:
            note_session_request(self, url)


def _collector_map(collector: ProviderCallCollector) -> dict:
    return {
        (e.provider, e.service): (e.calls, e.billable_units, e.retried_calls)
        for e in collector.totals()
    }


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tenant(make_tenant):
    return make_tenant("Metering Office")


@pytest.fixture
def user(make_user, tenant):
    return make_user(tenant, "metering@example.com")


@pytest.fixture
def keys(monkeypatch):
    """Both platform keys present + the Google gate open, with every
    politeness sleep neutralized (these tests never touch a socket)."""
    settings = get_settings()
    secret_store.set_secret("arcgis", "k-arcgis")
    secret_store.set_secret("google_maps", "k-google")
    monkeypatch.setattr(settings, "google_geocode_enabled", True)
    monkeypatch.setattr(settings, "google_geocode_cost_micro_per_call", 5000)
    monkeypatch.setattr(settings, "public_intel_http_backoff_base_seconds", 0.0)
    monkeypatch.setattr(ag.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(osm.time, "sleep", lambda *_a, **_k: None)
    return settings


def _run(db, tenant, user) -> SiteCheckRun:
    run = SiteCheckRun(
        tenant_id=tenant.id,
        requested_by=user.id,
        address=ADDRESS,
        normalized_address=sc_service.normalize_address(ADDRESS),
        county="travis",
        lat=30.2747,
        lng=-97.7403,
        status="completed",
        mode="check",
        adapter_results={},
        sources_queried=[],
        sources_failed=[],
        derived={},
        finished_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    return run


def _rows(db, run_id) -> dict:
    return {
        (r.provider, r.service): r
        for r in db.scalars(
            select(SiteCheckProviderUsage).where(
                SiteCheckProviderUsage.run_id == run_id,
            )
        ).all()
    }


_GOOGLE_OK = {
    "status": "OK",
    "results": [{
        "formatted_address": "1100 Congress Ave, Austin, TX 78701, USA",
        "geometry": {"location": {"lat": 30.2747, "lng": -97.7403}},
        "address_components": [
            {"long_name": "Congress Avenue", "short_name": "Congress Ave",
             "types": ["route"]},
            {"long_name": "Travis County", "short_name": "Travis County",
             "types": ["administrative_area_level_2", "political"]},
        ],
    }],
}


def _ladder_routes(google_payload):
    """Nominatim and ArcGIS both miss; Google answers `google_payload`."""
    return {
        ag._NOMINATIM: [(200, [])],
        ag._ARCGIS_GEOCODE: [(200, {"candidates": []})],
        ag._GOOGLE_GEOCODE: [(200, google_payload)],
    }


# ── Google: metered per REQUEST, not per resolution ──────────────────


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("zero_results", {"status": "ZERO_RESULTS", "results": []}),
        ("error", {"status": "REQUEST_DENIED",
                   "error_message": "The provided API key is invalid."}),
        ("success", _GOOGLE_OK),
    ],
)
def test_every_google_attempt_records_exactly_one_google_call(
    keys, label, payload,
):
    """The reviewer's reproduction, all three ways it can end. One
    Google HTTP request happened, so the ledger shows exactly one
    google/geocode call — priced at the settings rate — whether the
    answer was a location, ZERO_RESULTS, or a rejected key."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession(_ladder_routes(payload), collector=collector)

    result = ag.fetch(
        PropertyIdentity(address=ADDRESS, county="travis"), session=sess,
    )

    recorded = _collector_map(collector)
    assert recorded[("google", "geocode")] == (1, 1, 0)
    # …and the tiers that were called and MISSED are still on the
    # ledger — a resolving tier never erases them (the old shape
    # recorded only the resolver).
    assert recorded[("nominatim", "geocode_stored")][0] == 1
    assert recorded[("arcgis", "geocode_stored")][0] == 1
    if label == "success":
        assert result.normalized_facts["source"] == "google"
    else:
        assert result.errors


def test_google_attempt_costs_the_settings_rate_even_on_zero_results(
    db, tenant, user, keys,
):
    """Persisted proof of the failing reproduction: the paid Google
    request is a google row at 5,000 µ$, and the arcgis row prices only
    the ArcGIS request that was actually made."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession(
        _ladder_routes({"status": "ZERO_RESULTS", "results": []}),
        collector=collector,
    )
    ag.fetch(PropertyIdentity(address=ADDRESS, county="travis"), session=sess)

    run = _run(db, tenant, user)
    usage.flush_collector(db, run=run, collector=collector)
    db.commit()

    rows = _rows(db, run.id)
    assert set(rows) == {
        ("nominatim", "geocode_stored"),
        ("arcgis", "geocode_stored"),
        ("google", "geocode"),
    }
    assert rows[("google", "geocode")].estimated_cost_microdollars == 5_000
    assert rows[("arcgis", "geocode_stored")].estimated_cost_microdollars == 4_000
    assert rows[("nominatim", "geocode_stored")].estimated_cost_microdollars == 0
    # 4,000 + 5,000 µ$ — aggregated as integers, rendered only here.
    assert usage.run_total_cost_usd(db, run_id=run.id) == pytest.approx(0.009)


@pytest.mark.parametrize("gate", ["no_key", "disabled"])
def test_unattempted_google_tier_records_zero_calls(keys, monkeypatch, gate):
    """A disabled gate or a missing key sends nothing, so it is charged
    nothing — the ledger must not invent a call for a tier that never
    left the process."""
    if gate == "no_key":
        secret_store.delete_secret("google_maps")
    else:
        monkeypatch.setattr(keys, "google_geocode_enabled", False)
    collector = ProviderCallCollector()
    sess = _MeteredStubSession(
        _ladder_routes({"status": "ZERO_RESULTS"}), collector=collector,
    )

    ag.fetch(PropertyIdentity(address=ADDRESS, county="travis"), session=sess)

    assert ("google", "geocode") not in _collector_map(collector)
    assert ag._GOOGLE_GEOCODE not in sess.calls


def test_nominatim_success_records_no_arcgis_or_google_call(keys):
    """§8.6 geocoder-path contract, unchanged by per-request metering:
    the free tier resolved, so the paid tiers were never called and
    have no row."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession({
        ag._NOMINATIM: [(200, [{
            "lat": "30.2747", "lon": "-97.7403",
            "display_name": "1100 Congress Ave, Austin, Travis County",
            "address": {"county": "Travis County"},
        }])],
    }, collector=collector)

    result = ag.fetch(
        PropertyIdentity(address=ADDRESS, county="travis"), session=sess,
    )

    assert result.normalized_facts["source"] == "nominatim"
    assert _collector_map(collector) == {
        ("nominatim", "geocode_stored"): (1, 1, 0),
    }


# ── Retries: the provider served every attempt ───────────────────────


def test_multi_attempt_request_records_the_billed_retry(keys):
    """A 503 then a success is TWO requests the provider served. Both
    are on the ledger and the second is flagged as a billed retry."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession({
        ag._GOOGLE_GEOCODE: [
            (503, {"status": "UNKNOWN_ERROR"}),
            (200, _GOOGLE_OK),
        ],
    }, collector=collector)

    payload = resilient_json(
        sess, "GET", ag._GOOGLE_GEOCODE, params={"address": ADDRESS},
        attempts=3, backoff_base=0.0,
    )

    assert payload["status"] == "OK"
    assert _collector_map(collector) == {("google", "geocode"): (2, 2, 1)}


def test_transport_layer_replays_are_billed(keys):
    """urllib3 can replay a request below Session.request. Those are
    real requests the provider received, recovered from the retry
    history on the raw response."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession({}, collector=collector)

    class _Raw:
        retries = type("R", (), {"history": (object(), object())})()

    note_session_request(
        sess, ag._GOOGLE_GEOCODE, type("Resp", (), {"raw": _Raw()})(),
    )

    assert _collector_map(collector) == {("google", "geocode"): (3, 3, 2)}


def test_nested_logical_requests_do_not_double_count(keys):
    """An adapter retry loop wrapping the resilient engine must not
    turn one original request into a retry of itself."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession(
        {ag._GOOGLE_GEOCODE: [(200, _GOOGLE_OK)]}, collector=collector,
    )
    with usage.logical_request():
        resilient_json(
            sess, "GET", ag._GOOGLE_GEOCODE, attempts=2, backoff_base=0.0,
        )
    assert _collector_map(collector) == {("google", "geocode"): (1, 1, 0)}


# ── Internal fallback: BOTH upstreams are real ───────────────────────


def test_osm_to_arcgis_places_fallback_records_both_providers(keys):
    """Overpass times out three times and the adapter falls back to
    ArcGIS Places. The run billed two different upstreams; the ledger
    shows both, including the two billed Overpass retries."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession({
        osm.OVERPASS_URL: [requests.Timeout("overpass timed out")],
        ap._PLACES_NEAR_POINT: [(200, {"results": [
            {"placeId": "p1", "name": "Cafe", "categories": [],
             "location": {"x": -97.74, "y": 30.27}, "distance": 120.0},
        ]})],
    }, collector=collector)

    result = osm.fetch(
        PropertyIdentity(address=ADDRESS, lat=30.2747, lng=-97.7403),
        session=sess,
    )

    assert result.normalized_facts["fallback_used"] is True
    recorded = _collector_map(collector)
    assert recorded[("osm", "fetch")] == (3, 3, 2)
    assert recorded[("arcgis", "places_nearby")] == (1, 1, 0)


def test_source_attribution_labels_unrouted_hosts(keys):
    """Single-provider adapters hit hosts the router does not know.
    The orchestrator's per-dispatch attribution names them from the
    registry row; the identity-bearing upstreams still win."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession({
        "https://hazards.fema.gov/gis/nfhl/rest/services/query": [(200, {})],
        ag._GOOGLE_GEOCODE: [(200, _GOOGLE_OK)],
    }, collector=collector)

    with usage.attribute_to("fema", "fetch"):
        sess.get("https://hazards.fema.gov/gis/nfhl/rest/services/query")
        sess.get(ag._GOOGLE_GEOCODE)

    assert _collector_map(collector) == {
        ("fema", "fetch"): (1, 1, 0),
        ("google", "geocode"): (1, 1, 0),   # routed — never mislabeled
    }


def test_unattributed_request_falls_back_to_the_host(keys):
    """A request made with no source context is still a request that
    happened — it lands under its host rather than vanishing."""
    collector = ProviderCallCollector()
    sess = _MeteredStubSession(
        {"https://gis.wilco.org/arcgis/rest/services/x": [(200, {})]},
        collector=collector,
    )
    sess.get("https://gis.wilco.org/arcgis/rest/services/x")
    assert _collector_map(collector) == {("gis.wilco.org", "fetch"): (1, 1, 0)}


def test_production_session_meters_success_and_failure(keys):
    """The metering point is the REAL hardened session class, not a
    test double: drive `http_session()` through a stubbed transport
    adapter and both a served request and a raising one land on the
    ledger."""
    from requests.adapters import HTTPAdapter

    collector = ProviderCallCollector()
    sess = http_session(collector=collector)

    class _StubAdapter(HTTPAdapter):
        def __init__(self):
            super().__init__()
            self.remaining = [_response(_GOOGLE_OK, url=ag._GOOGLE_GEOCODE)]

        def send(self, request, **kwargs):
            if self.remaining:
                return self.remaining.pop()
            raise requests.ConnectionError("upstream refused")

    sess.mount("https://", _StubAdapter())
    sess.get(ag._GOOGLE_GEOCODE)
    with pytest.raises(requests.ConnectionError):
        sess.get(ag._GOOGLE_GEOCODE)

    assert _collector_map(collector) == {("google", "geocode"): (2, 2, 0)}


def test_http_session_attaches_the_collector_to_both_siblings():
    """The retry-free sibling the resilient engine sends through must
    meter onto the same collector, or app-level retries go missing."""
    collector = ProviderCallCollector()
    sess = http_session(collector=collector)
    assert sess.provider_call_collector is collector
    assert sess.no_transport_retry.provider_call_collector is collector
    # Adapters re-enter http_session with the orchestrator's session.
    assert http_session(sess) is sess
    assert sess.provider_call_collector is collector


# ── Money: integer microdollars, atomic free-tier allocation ─────────


def test_sub_cent_calls_aggregate_exactly(db, tenant, user):
    """Place Attributes bills 50 µ$ per call — a fifth of a cent. 120
    calls cross the 100/month free ceiling by 20, so the run owes
    exactly 1,000 µ$ with no float in the path."""
    run = _run(db, tenant, user)
    for _ in range(120):
        usage.record_call(
            db, run=run, provider="arcgis", service="places_attributes",
        )
    db.commit()

    row = _rows(db, run.id)[("arcgis", "places_attributes")]
    assert row.calls == 120 and row.billable_units == 120
    assert row.estimated_cost_microdollars == 1_000
    assert usage.run_total_cost_usd(db, run_id=run.id) == pytest.approx(0.001)


def test_free_tier_bills_only_the_overflow_units(db, tenant, user):
    """A single call that straddles the ceiling pays for the overflow
    units only — not the whole call, and not nothing."""
    run = _run(db, tenant, user)
    usage.record_call(
        db, run=run, provider="arcgis", service="places_attributes",
        billable_units=130,
    )
    db.commit()
    row = _rows(db, run.id)[("arcgis", "places_attributes")]
    assert row.estimated_cost_microdollars == 30 * 50


def test_settings_priced_rate_stays_integer_microdollars(monkeypatch):
    """The Google geocode rate is settings-driven and used verbatim —
    no float round-trip, no rounding of money."""
    monkeypatch.setattr(
        get_settings(), "google_geocode_cost_micro_per_call", 7_000,
    )
    rate = usage._rate_for("google", "geocode")
    assert rate.free_units_per_month == 0
    assert rate.unit_cost_micro == 7_000
    assert isinstance(rate.unit_cost_micro, int)
    assert usage._rate_for("arcgis", "places_attributes").unit_cost_micro == 50


def test_concurrent_runs_never_over_allocate_the_last_free_unit(
    db, tenant, user,
):
    """499 of the 500 free ArcGIS Places calls are gone this month and
    two runs ask for one each, at the same instant. Exactly one may be
    free; the read-then-write this replaces let both through."""
    seed_run = _run(db, tenant, user)
    db.add(SiteCheckProviderUsage(
        tenant_id=tenant.id, run_id=seed_run.id,
        provider="arcgis", service="places_nearby",
        calls=499, billable_units=499, retried_calls=0,
        estimated_cost_microdollars=0, created_at=datetime.now(UTC),
    ))
    db.commit()
    run_ids = [_run(db, tenant, user).id for _ in range(2)]
    db.commit()

    barrier = Barrier(2)
    errors: list[Exception] = []

    def allocate(run_id: str) -> None:
        session = SessionLocal()
        try:
            run = session.get(SiteCheckRun, run_id)
            barrier.wait(timeout=20)
            usage.record_call(
                session, run=run, provider="arcgis", service="places_nearby",
            )
            session.commit()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [Thread(target=allocate, args=(rid,)) for rid in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "free-tier allocation deadlocked"
    assert errors == []

    db.expire_all()
    costs = sorted(
        row.estimated_cost_microdollars
        for row in db.scalars(
            select(SiteCheckProviderUsage).where(
                SiteCheckProviderUsage.run_id.in_(run_ids),
            )
        ).all()
    )
    # One free (the 500th), one paid at $8/1K — never two free.
    assert costs == [0, 8_000]


# ── Zero-call paths + accounting durability ──────────────────────────


def test_cache_and_reuse_paths_write_no_usage_row(db, tenant, user):
    """A reused or cache-served source makes no request, so the
    collector has nothing to drain and no row is written."""
    run = _run(db, tenant, user)
    collector = ProviderCallCollector()
    assert usage.flush_collector(db, run=run, collector=collector) == []
    db.commit()
    assert _rows(db, run.id) == {}


def test_flush_drains_so_a_second_flush_never_double_bills(db, tenant, user):
    """The orchestrator flushes after every source; already-persisted
    requests must not be counted again."""
    run = _run(db, tenant, user)
    collector = ProviderCallCollector()
    collector.record("google", "geocode")
    usage.flush_collector(db, run=run, collector=collector)
    assert usage.flush_collector(db, run=run, collector=collector) == []
    db.commit()
    row = _rows(db, run.id)[("google", "geocode")]
    assert row.calls == 1 and row.estimated_cost_microdollars == 5_000


def test_usage_rows_survive_run_deletion(db, tenant, user):
    """Accounting outlives the run it describes (ON DELETE SET NULL) —
    a deleted lookup must not erase what the provider billed."""
    run = _run(db, tenant, user)
    usage.record_call(db, run=run, provider="google", service="geocode")
    db.commit()
    run_id = run.id
    db.delete(db.get(SiteCheckRun, run_id))
    db.commit()
    db.expire_all()

    rows = db.scalars(select(SiteCheckProviderUsage)).all()
    assert len(rows) == 1
    assert rows[0].run_id is None
    assert rows[0].estimated_cost_microdollars == 5_000
