"""§13.2 per-source adapter contract tests (docs/specs/site_check.md
§13.2, §18): every registry source is driven through its REAL
`fetch()` over a captured raw provider payload
(`tests/fixtures/site_check/<source_id>/fixture.json`, recorded by
`tests/record_site_check_fixtures.py` with the Austin identity) and the
full AdapterResult contract is asserted — statuses, normalized_facts,
result_schema_version.

The whole module runs FULLY OFFLINE: a socket guard rejects any
non-loopback connection, so a replay miss can never silently fall
through to the live provider.

Also here (same §13.2 bullet list): `_source_waves` exact-set
membership, authoritative-anchor reconciliation, and per-token retry
classification units.
"""

import socket
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import settings as settings_obj
from app.core import secret_store
from app.models import SiteCheckRun
from app.services.site_check import registry
from app.services.site_check import service as sc_service
from app.services.site_check.adapters._base import stub_inactive_result
from app.services.site_check.contract import AdapterResult
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.orchestrator import (
    _reconcile_anchor_with_parcel,
    _source_waves,
)

from .site_check_fixture_replay import (
    comparable_dump,
    fixture_path,
    freeze_adapter_clocks,
    load_fixture,
    replay_fetch,
)

ACTIVE_SOURCES = sorted(registry.active_source_ids())
INACTIVE_SOURCES = sorted(
    set(registry.all_source_ids()) - set(registry.active_source_ids())
)


# ── Offline guard (§13.2 "No live network in the suite") ─────────────


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """Refuse any non-loopback socket connect for this module. Postgres
    (conftest) stays reachable on localhost / unix sockets."""
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, bytes):
            host = host.decode("utf-8", "replace")
        if isinstance(host, str) and (
            host in ("localhost", "::1")
            or host.startswith(("127.", "/"))
        ):
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(
            f"live network attempted in the offline adapter suite: {address!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def _replay_env(monkeypatch):
    """Deterministic replay settings: dummy provider keys so key-gated
    adapters take their recorded path on any machine (fixture matching
    redacts key values), zero backoff so recorded failure exchanges
    replay without real sleeps."""
    secret_store.set_secret("arcgis", "test-arcgis-key")
    secret_store.set_secret("census", "test-census-key")
    monkeypatch.setattr(
        settings_obj, "public_intel_http_backoff_base_seconds", 0.0,
    )
    monkeypatch.setattr(
        settings_obj, "txdot_arcgis_backoff_base_seconds", 0.0,
    )
    return monkeypatch


# ── The §13.2 regression floor: one contract test per ACTIVE source ──


@pytest.mark.parametrize("source_id", ACTIVE_SOURCES)
def test_adapter_contract_over_captured_payload(source_id, _replay_env):
    path = fixture_path(source_id)
    assert path.exists(), (
        f"missing §13.2 fixture for {source_id!r} — record it with "
        "`uv run python tests/record_site_check_fixtures.py "
        f"{source_id}`"
    )
    fixture = load_fixture(source_id)
    for name, value in (fixture.get("settings_overrides") or {}).items():
        _replay_env.setattr(settings_obj, name, value)

    restore = freeze_adapter_clocks(
        datetime.fromisoformat(fixture["recorded_at"])
    )
    try:
        result = replay_fetch(source_id, fixture)
    finally:
        restore()

    # Contract: a real AdapterResult for THIS source with valid enums
    # (enforced by pydantic on construction).
    assert isinstance(result, AdapterResult)
    assert result.source_id == source_id

    # Full-dump regression floor: statuses + normalized_facts +
    # schema/version fields must reproduce the recorded expectation
    # byte-for-byte (minus recorded volatile paths).
    dump = result.model_dump(mode="json")
    assert comparable_dump(dump, fixture) == comparable_dump(
        fixture["expected"], fixture
    )

    # Key statuses asserted explicitly so a fixture regression reads
    # clearly in the failure output.
    expected = fixture["expected"]
    assert dump["source_access_status"] == expected["source_access_status"]
    assert dump["data_confidence_status"] == expected["data_confidence_status"]
    assert dump["coverage_status"] == expected["coverage_status"]
    assert dump["normalized_facts"].keys() == (
        expected["normalized_facts"].keys()
    )

    # The registry-owned reusable-contract version at capture time must
    # match the registry NOW — bumping result_schema_version without
    # re-recording the fixture is a contract change and must fail here.
    assert (
        registry.result_schema_version(source_id)
        == fixture["registry_result_schema_version"]
    )


@pytest.mark.parametrize("source_id", INACTIVE_SOURCES)
def test_inactive_source_stub_contract(source_id):
    """Wired-inactive sources (§4/§6.3): the registry short-circuits
    dispatch BEFORE the adapter — the run records the `not_yet_wired`
    stub payload downstream consumers key on, with the registry-owned
    schema version stamped."""
    from app.services.site_check.orchestrator import _adapter_result_payload

    reg = registry.get(source_id)
    assert reg.active_in_v0 is False
    assert source_id not in registry.active_source_ids()
    assert source_id in registry.all_source_ids()  # never silently dropped

    stub = stub_inactive_result(source_id)
    assert stub.source_id == source_id
    assert stub.coverage_status.value == "not_yet_wired"
    assert stub.data_confidence_status.value == "unknown"
    assert stub.normalized_facts == {}
    assert stub.errors is None
    assert stub.advisories and source_id in stub.advisories[0]

    # The exact payload the orchestrator stages for the run.
    payload = _adapter_result_payload(stub)
    assert payload["coverage_status"] == "not_yet_wired"
    assert payload["result_schema_version"] == (
        registry.result_schema_version(source_id)
    )


# ── §13.2 wave membership (`_source_waves` exact sets) ───────────────


def test_source_waves_exact_sets_over_active_roster():
    active = registry.active_source_ids()
    wave1, post_parcel, wave2, wave3 = _source_waves(active)

    assert set(post_parcel) == {"austin_zoning", "fema_nfhl", "usgs_elevation"}
    assert set(wave2) == {
        "drainage_screen", "txdot_extended", "future_land_use",
        "district_overlays", "entitlement_pipeline", "plats_intel",
        "zoning_standards",
    }
    assert set(wave3) == {"tax_intel", "site_constraints"}
    assert set(wave1) == (
        set(active)
        - {"arcgis_geocode", "jurisdiction_resolver"}
        - set(post_parcel) - set(wave2) - set(wave3)
    )
    # Partition: every active non-geocode source is in EXACTLY one wave;
    # the geocoder + jurisdiction resolver are excluded from the fan-out.
    all_waved = wave1 + post_parcel + wave2 + wave3
    assert len(all_waved) == len(set(all_waved)) == len(active) - 2
    assert "arcgis_geocode" not in all_waved
    assert "jurisdiction_resolver" not in all_waved


def test_source_waves_preserve_registry_dispatch_order():
    active = registry.active_source_ids()
    wave1, post_parcel, wave2, wave3 = _source_waves(active)
    for wave in (wave1, post_parcel, wave2, wave3):
        assert wave == sorted(wave, key=active.index)


# ── §13.2 anchor reconciliation ──────────────────────────────────────

# A ~1km square polygon near Manor, far from the (wrong) geocode anchor.
_PARCEL_POLYGON = {
    "type": "Polygon",
    "coordinates": [[
        [-97.52, 30.34], [-97.51, 30.34],
        [-97.51, 30.35], [-97.52, 30.35],
        [-97.52, 30.34],
    ]],
}


def _anchor_run(lat, lng, parcel_id="R12345"):
    return SiteCheckRun(
        tenant_id="t", requested_by="u",
        address="123 FM 971, Georgetown, TX",
        normalized_address="123 fm 971, georgetown, tx",
        county="travis", lat=lat, lng=lng, status="running",
        parcel_id=parcel_id,
        adapter_results={"arcgis_geocode": {"advisories": []}},
    )


def _identity_for(run):
    return PropertyIdentity(
        address=run.address, lat=run.lat, lng=run.lng,
        county=run.county, parcel_id=run.parcel_id,
    )


@pytest.fixture
def _parcel_geometry(monkeypatch):
    from app.services.site_check import orchestrator

    monkeypatch.setattr(
        orchestrator, "_resolve_parcel_geometry",
        lambda identity, sess: {"parcel_polygon": _PARCEL_POLYGON},
    )


def test_anchor_outside_polygon_beyond_threshold_reanchors(
    _parcel_geometry,
):
    # Panola-county-style mismatch: ~350 km from the parcel.
    run = _anchor_run(lat=32.15, lng=-94.34)
    identity = _reconcile_anchor_with_parcel(
        run, _identity_for(run), sess=None,
    )
    assert (run.lat, run.lng) == (identity.lat, identity.lng)
    # Vertex mean of the outer ring (the module's representative point).
    assert run.lat == pytest.approx(30.344, abs=1e-3)
    assert run.lng == pytest.approx(-97.516, abs=1e-3)
    assert run.geocode_source == "parcel_centroid"
    correction = run.adapter_results["arcgis_geocode"]["anchor_correction"]
    assert correction["parcel_id"] == "R12345"
    assert correction["distance_km"] > settings_obj.public_intel_anchor_parcel_max_km


def test_anchor_inside_polygon_never_reanchors(_parcel_geometry):
    run = _anchor_run(lat=30.345, lng=-97.515)  # inside the parcel
    identity = _identity_for(run)
    out = _reconcile_anchor_with_parcel(run, identity, sess=None)
    assert out is identity  # untouched
    assert (run.lat, run.lng) == (30.345, -97.515)
    assert run.geocode_source is None
    assert "anchor_correction" not in run.adapter_results["arcgis_geocode"]


def test_anchor_outside_polygon_within_threshold_kept(_parcel_geometry):
    # Just outside the boundary but well inside the distance tolerance:
    # a road-edge geocode must NOT be rewritten.
    run = _anchor_run(lat=30.3395, lng=-97.515)
    identity = _identity_for(run)
    out = _reconcile_anchor_with_parcel(run, identity, sess=None)
    assert out is identity
    assert run.geocode_source is None


def test_anchor_without_parcel_id_is_untouched(_parcel_geometry):
    run = _anchor_run(lat=32.15, lng=-94.34, parcel_id=None)
    identity = _identity_for(run)
    assert _reconcile_anchor_with_parcel(run, identity, sess=None) is identity
    assert run.geocode_source is None


# ── §13.2 retry classification units (per token / status class) ──────


def _classification_run(result: dict, *, failed_error: str | None = None):
    failed = []
    if failed_error is not None:
        failed.append({
            "source": "fema_nfhl", "error": failed_error,
            "failed_at": datetime.now(UTC).isoformat(),
        })
    return SiteCheckRun(
        tenant_id="t", requested_by="u", address="a",
        normalized_address="a", county="travis",
        status="completed",
        adapter_results={"fema_nfhl": result},
        sources_failed=failed,
        derived={},
    )


def _fragile_result(**overrides):
    result = {
        "source_id": "fema_nfhl",
        "source_access_status": "PROGRAMMATIC_PUBLIC_BUT_FRAGILE",
        "data_confidence_status": "unknown",
        "coverage_status": "partial",
        "normalized_facts": {},
        "errors": ["provider exploded"],
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("coverage", sorted(sc_service._NON_RETRYABLE_COVERAGE))
def test_non_retryable_coverage_classes(coverage):
    run = _classification_run(_fragile_result(coverage_status=coverage))
    assert sc_service._retryable_failure_for_source(run, "fema_nfhl") is None


@pytest.mark.parametrize("access", sorted(sc_service._NON_RETRYABLE_ACCESS))
def test_non_retryable_access_classes(access):
    run = _classification_run(_fragile_result(source_access_status=access))
    assert sc_service._retryable_failure_for_source(run, "fema_nfhl") is None


@pytest.mark.parametrize("token", sorted(sc_service._NON_RETRYABLE_ERROR_TOKENS))
def test_non_retryable_error_tokens(token):
    run = _classification_run(
        _fragile_result(errors=[f"upstream said: {token} happened"])
    )
    assert sc_service._retryable_failure_for_source(run, "fema_nfhl") is None


@pytest.mark.parametrize("token", sorted(sc_service._TRANSIENT_ERROR_TOKENS))
def test_transient_tokens_are_retryable_on_verified_results(token):
    """A PROGRAMMATIC_PUBLIC result is retryable ONLY via a transient
    signal — every declared token must open the gate."""
    run = _classification_run(_fragile_result(
        source_access_status="PROGRAMMATIC_PUBLIC",
        errors=[f"the fetch failed: {token}"],
    ))
    row = sc_service._retryable_failure_for_source(run, "fema_nfhl")
    assert row is not None and row["source_id"] == "fema_nfhl"


def test_non_transient_error_on_solid_access_is_not_retryable():
    run = _classification_run(_fragile_result(
        source_access_status="PROGRAMMATIC_PUBLIC",
        errors=["the parser found an unexpected shape"],
    ))
    assert sc_service._retryable_failure_for_source(run, "fema_nfhl") is None


def test_fragile_access_is_retryable_without_a_transient_token():
    run = _classification_run(
        _fragile_result(errors=["something odd but the site is flaky"])
    )
    row = sc_service._retryable_failure_for_source(run, "fema_nfhl")
    assert row is not None


def test_geocode_and_inactive_sources_never_classify_retryable():
    run = _classification_run(_fragile_result())
    assert sc_service._retryable_failure_for_source(run, "arcgis_geocode") is None
    run.adapter_results["txdot_county_vmt"] = _fragile_result(
        source_id="txdot_county_vmt",
    )
    assert (
        sc_service._retryable_failure_for_source(run, "txdot_county_vmt")
        is None
    )


# ── §13.2 cooldown math (rate-limit fallback window) ─────────────────


def test_rate_limit_cooldown_window_derives_from_settings(monkeypatch):
    monkeypatch.setattr(
        settings_obj, "public_intel_rate_limit_cooldown_seconds", 300,
    )
    failed_at = datetime.now(UTC) - timedelta(seconds=60)
    row = sc_service._retry_cooldown_for_result(
        _fragile_result(),
        error_text="HTTP 429 rate limit",
        last_failed_at=failed_at.isoformat(),
    )
    assert row["retry_status"] == "cooldown"
    # 300s window minus the 60s already elapsed (clock-read slack).
    assert 230 <= row["retry_after_seconds"] <= 240
    assert "cooling down" in row["retry_message"]


def test_rate_limit_cooldown_expired_window_is_empty(monkeypatch):
    monkeypatch.setattr(
        settings_obj, "public_intel_rate_limit_cooldown_seconds", 300,
    )
    failed_at = datetime.now(UTC) - timedelta(seconds=301)
    assert sc_service._retry_cooldown_for_result(
        _fragile_result(),
        error_text="HTTP 429 rate limit",
        last_failed_at=failed_at.isoformat(),
    ) == {}


def test_source_cooldown_from_facts_wins_over_fallback():
    until = datetime.now(UTC) + timedelta(seconds=900)
    result = _fragile_result(normalized_facts={
        "source_cooldowns": {
            "epa_echo": {
                "retry_disabled_until": until.isoformat(),
                "message": "This source asked us to pause. Try later.",
            },
        },
    })
    row = sc_service._retry_cooldown_for_result(
        result, error_text="whatever", last_failed_at=None,
    )
    assert row["retry_disabled_until"] == until.isoformat()
    assert row["retry_message"] == "This source asked us to pause. Try later."
