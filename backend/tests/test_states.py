"""Phase C: a lookup knows its state (docs/specs/add_counties_and_states.md).

Covers the state-settling table, the address/map disagreement stop, a lookup
in another state never calling a Texas service, the stopped upgrade of a v1
database, a state folder with no counties passing the check, and the Denver
screens saying "not connected" instead of showing Texas findings.
"""

import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import sqlalchemy as sa

from app.regions import loader, states
from app.services.site_check import cards, map_context, orchestrator, registry
from app.services.site_check.adapters import arcgis_geocode as ag
from app.services.site_check.identity import PropertyIdentity

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ── Reading and settling the state ─────────────────────────────────────────


@pytest.mark.parametrize(("address", "expected"), [
    ("1437 Bannock St, Denver, CO 80202", "CO"),
    ("100 N Seguin Ave, New Braunfels, TX 78130", "TX"),
    ("12 Main St, Franklin, Tennessee", "TN"),
    ("500 Washington St, Austin", None),  # a street name is not a state
])
def test_state_named_in_the_address(address, expected):
    assert states.state_from_text(address) == expected


@pytest.mark.parametrize(("named", "provider", "expected"), [
    ("CO", "CO", ("CO", "address_and_map")),   # names a state, map agrees
    ("TX", "CO", (None, "conflict")),          # names a state, map disagrees
    ("TN", None, ("TN", "address")),           # names a state, map gives none
    (None, "CO", ("CO", "map")),               # names none, map gives one
    (None, None, ("TX", "assumed")),           # names none, map gives none
])
def test_state_settling_table(named, provider, expected):
    assert states.settle(named, provider) == expected


def test_address_and_map_disagreeing_stops_at_the_address_step(monkeypatch):
    def colorado(sess, address, *, county=None, state=None):
        return (39.739, -104.990, "denver", "CO")

    monkeypatch.setattr(ag, "_try_nominatim", colorado)
    result = ag.fetch(PropertyIdentity(address="1437 Bannock St, Denver, TX"))
    assert result.errors
    assert result.normalized_facts["state_conflict"] is True
    assert "disagree on the state" in orchestrator._STATE_CONFLICT_REASON


def test_named_state_wins_over_a_texas_candidate(monkeypatch):
    def nominatim(sess, address, *, county=None, state=None):
        candidates = [
            (30.27, -97.74, "travis", "Main St, Austin", "TX"),
            (35.92, -86.87, "williamson", "Main St, Franklin", "TN"),
        ]
        return ag._pick_candidate(candidates, set(), county, state)

    monkeypatch.setattr(ag, "_try_nominatim", nominatim)
    monkeypatch.setattr(ag, "_county_from_point", lambda *a: None)
    facts = ag.fetch(PropertyIdentity(address="1 Main St, Franklin, TN")).normalized_facts
    assert (facts["state"], facts["county"]) == ("TN", "williamson")


# ── Williamson County, Tennessee never reaches a Texas service ─────────────

# Hosts that only Texas agencies and Texas counties use.
_TEXAS_HOSTS = (
    "texas.gov", "txdot", "tceq", "twdb", "capmetro", "austintexas",
    "traviscounty", "wilco", "hayscounty", "bastrop", "caldwell", "burnet",
    "blanco", "comal", "tnris", "hayscad", "urbaneng",
)


# The Texas parts of shared sources, by constant name.
_TEXAS_PARTS = {
    "district_overlays": ("_TEXAS_STATEWIDE_LAYERS",),
    "jurisdiction_resolver": ("_STATEWIDE_CITY_LIMIT_LAYERS",),
    "tcad_parcel": ("_TX_STATEWIDE_PARCELS_LAYER",),
    "environmental_screening": ("_TCEQ_SCREEN_LAYERS", "_TCEQ_REGISTRY_DATASETS"),
    "site_constraints": ("_EDWARDS_QUERY",),
}


def _urls_in(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith("http") else set()
    if isinstance(value, dict):
        return set().union(*(_urls_in(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return set().union(*(_urls_in(v) for v in value)) if value else set()
    return set()


def _texas_service_paths() -> set[str]:
    """Every Texas service path: the Texas region files, every URL a
    Texas-only source module holds, and the Texas parts of shared sources.
    Shared ArcGIS hosts are matched by full path, never by host."""
    import importlib

    text = "".join(p.read_text() for p in (loader.REGIONS_DIR / "tx").rglob("*.yaml"))
    urls = set(re.findall(r"https?://[^\s'\"]+", text))
    for source_id, reg in registry.REGISTRY.items():
        module = importlib.import_module(f"app.services.site_check.adapters.{source_id}")
        if reg.states == ("TX",):
            urls |= _urls_in([v for k, v in vars(module).items() if k.isupper() or k.startswith("_")])
        for name in _TEXAS_PARTS.get(source_id, ()):
            urls |= _urls_in(getattr(module, name))
    paths = {u.split("?")[0].removesuffix("/query").rstrip("/") for u in urls}
    # Bare hosts or service roots would match everything on a shared host.
    return {p for p in paths if p.count("/") >= 4}


def test_tennessee_lookup_calls_no_texas_service(monkeypatch):
    called: list[str] = []

    def record(self, request, **kwargs):
        called.append(request.url)
        raise requests.ConnectionError("offline in this test")

    monkeypatch.setattr(requests.Session, "send", record)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    identity = PropertyIdentity(
        address="1 Main St, Franklin, TN 37064",
        lat=35.925, lng=-86.869, county="williamson", state="TN",
    )
    for source_id in registry.all_source_ids():
        if source_id == "arcgis_geocode" or not registry.get(source_id).active_in_v0:
            continue
        with contextlib.suppress(Exception):  # failing offline is fine here
            orchestrator._load_adapter_fetch(source_id)(identity, session=None)

    texas_paths = _texas_service_paths()
    texas_calls = [
        url for url in called
        if any(marker in url.split("/")[2] for marker in _TEXAS_HOSTS)
        or any(url.startswith(path) for path in texas_paths)
    ]
    assert called, "the adapters should have tried their nationwide sources"
    assert texas_calls == []


def test_texas_only_source_says_not_connected_elsewhere():
    identity = PropertyIdentity(address="x", lat=39.7, lng=-105.0, county="denver", state="CO")
    result = orchestrator._load_adapter_fetch("txdot_aadt")(identity, session=None)
    assert result.coverage_status.value == "not_supported_in_jurisdiction"
    assert result.normalized_facts["advisory"] == "Not connected for Colorado yet."


def test_county_key_keeps_texas_rules_for_texas_only():
    assert loader.county_key("TX", "Williamson County") == "williamson"
    assert loader.county_key("TN", "Williamson") == "tn:williamson"
    assert PropertyIdentity(address="x", county="denver", state="co").county_key == "co:denver"


# ── The stopped upgrade of a v1 database ───────────────────────────────────


def test_v1_database_upgrades_with_its_rows_kept_as_texas():
    from app.db import engine

    base_url = str(engine.url).rsplit("/", 1)[0]
    name = "proprisk_state_upgrade_check"
    url = f"{base_url}/{name}"
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")

    def alembic(target: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            cwd=BACKEND_DIR, env={**os.environ, "DATABASE_URL": url},
            capture_output=True, text=True, check=False, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        alembic("0001_proprisk_baseline")
        check = sa.create_engine(url)
        try:
            with check.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO site_check_runs (id, tenant_id, requested_by, address, "
                    "normalized_address, county, status, mode, created_at) "
                    "SELECT 'r1', t.id, u.id, '1 Main St', '1 main st', 'travis', "
                    "'completed', 'check', now() FROM tenants t, users u"
                ))
                conn.execute(sa.text(
                    "INSERT INTO site_check_coverage_gaps (id, tenant_id, capability, "
                    "county, reason, created_at) SELECT 'g1', id, 'parcel_adapter', "
                    "'Williamson', 'county_parcel_adapter_missing', now() FROM tenants"
                ))
            alembic("head")
            with check.begin() as conn:
                assert conn.execute(sa.text(
                    "SELECT state FROM site_check_runs WHERE id='r1'")).scalar_one() == "TX"
                # Williamson County, Tennessee can now get its own gap row.
                conn.execute(sa.text(
                    "INSERT INTO site_check_coverage_gaps (id, tenant_id, capability, "
                    "state, county, reason, created_at) SELECT 'g2', id, 'parcel_adapter', "
                    "'TN', 'Williamson', 'county_parcel_adapter_missing', now() FROM tenants"
                ))
                assert conn.execute(sa.text(
                    "SELECT count(*) FROM site_check_coverage_gaps")).scalar_one() == 2
        finally:
            check.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))


# ── The check on a state folder with no counties ───────────────────────────


def test_check_passes_a_state_folder_with_no_counties(tmp_path, monkeypatch, capsys):
    from app.cli import check_region

    (tmp_path / "nm" / "counties").mkdir(parents=True)
    (tmp_path / "nm" / "state.yaml").write_text(
        "code: NM\nname: New Mexico\nfips: \"35\"\n"
        "search_box: [-109.05, 31.33, -103.0, 37.0]\n"
    )
    original = loader.load_regions.__wrapped__
    monkeypatch.setattr(loader, "load_regions", lambda: original(tmp_path))
    monkeypatch.setattr(check_region, "run_settings_tests", lambda report: report.ok("skipped"))
    assert check_region.main(["nm"]) == 0
    assert "PASS" in capsys.readouterr().out


# ── Denver screens: not connected, never a Texas finding ───────────────────


def _env_facts(echo_records, echo_status="queried"):
    return {
        "state": "CO", "state_records": "not_connected",
        "echo_records": echo_records, "search_radius_m": 500,
        "source_statuses": {"epa_echo": echo_status}, "advisory": "advisory",
    }


@pytest.mark.parametrize(("echo_records", "status", "expected"), [
    ([{"facility_name": "Shop", "address": "1 Main"}], "queried", "1 nearby"),
    ([], "queried", "None found nearby"),
    ([], "temporary_unavailable", "Temporarily unavailable — not an all-clear"),
])
def test_denver_environmental_card(echo_records, status, expected):
    facts = _env_facts(echo_records, status)
    content = cards._format_environmental(facts)
    rows = {row["label"]: row["value"] for row in content.facts}
    assert rows["Federally regulated facilities"] == expected
    assert rows["State environmental records"] == "Not connected for Colorado yet"
    assert "Fuel & storage tank records" not in rows
    summary = cards._summary_environmental(facts)
    assert summary.startswith("State records not connected")
    assert "PST" not in summary


def test_denver_jurisdiction_card_states_only_what_was_found():
    from app.services.site_check.adapters import jurisdiction_resolver as jr

    verdict = {"jurisdiction_kind": "full_purpose_city", "city": "DENVER",
               "county": "denver", "state": "CO", "confidence": "authoritative"}
    verdict["broker_summary"] = jr._found_only_summary(verdict)
    content = cards._format_jurisdiction(verdict)
    assert content.takeaway.endswith("Zoning and development authority not checked.")
    labels = {row["label"] for row in content.facts}
    assert "What governs this site" not in labels


def test_denver_map_has_no_texas_layers_or_credits():
    class Run:
        state = "CO"

    texas_layer = map_context.SiteCheckMapLayer(
        id="water_ccn", label="Water", kind="geojson", available=True,
        status="screening", attribution="TWDB / PUCT CCN water service areas",
        details=[{"label": "Water areas shown", "value": "0"}],
    )
    out = map_context._outside_texas_layer(texas_layer, Run())
    assert out.available is False and out.attribution == ""
    assert out.details == [{"label": "Status", "value": "Not connected for Colorado yet"}]


def test_denver_historical_photos_card_says_not_connected():
    card = cards._historical_aerials_card(
        object(), {"historical_aerials_meta": {"status": "not_connected", "state": "CO"}},
    )
    assert card["status"] == "not_covered"
    assert "Not connected for Colorado yet" in card["takeaway"]


# ── Codex review round 1 fixes ─────────────────────────────────────────────


@pytest.mark.parametrize(("address", "expected"), [
    ("1 Main St Franklin TN", "TN"),
    ("1 Main St, Franklin Tennessee", "TN"),
    ("1 Main St, Franklin, TN, USA", "TN"),
    ("1 Main St, Franklin, TN 37064, USA", "TN"),
    ("12 Main St, Santa Fe, New Mexico", "NM"),
    ("1 Virginia Ave", None),
])
def test_state_read_from_ordinary_address_formats(address, expected):
    assert states.state_from_text(address) == expected


def _reuse_result(source_id, facts):
    from datetime import UTC, datetime, timedelta

    return {
        "source_id": source_id, "source_access_status": "PROGRAMMATIC_PUBLIC",
        "data_confidence_status": "verified_public_fact", "coverage_status": "covered",
        "normalized_facts": facts, "errors": [],
        "fetched_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "result_schema_version": registry.result_schema_version(source_id),
    }


def _denver_run(db, tenant, user, *, state, results, minutes_ago):
    from datetime import UTC, datetime, timedelta

    from app.models import SiteCheckRun
    from app.services.site_check import service

    run = SiteCheckRun(
        tenant_id=tenant.id, requested_by=user.id, address="1437 Bannock St, Denver",
        normalized_address=service.normalize_address("1437 Bannock St, Denver"),
        county="denver", state=state, lat=39.739, lng=-104.990, status="completed",
        mode="check", adapter_results=results, sources_queried=[], sources_failed=[],
        derived={}, created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        finished_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    return run


def test_reuse_never_mixes_states_and_keeps_the_county(db, make_tenant, make_user):
    from app.services.site_check import service

    tenant = make_tenant("States Office")
    user = make_user(tenant, "states@example.com")
    geocode = _reuse_result("arcgis_geocode", {
        "lat": 39.739, "lng": -104.990, "county": "denver", "state": "CO",
    })
    # An older run at the same point that was settled as Texas.
    _denver_run(db, tenant, user, state="TX", minutes_ago=30, results={
        "environmental_screening": _reuse_result(
            "environmental_screening", {"advisory": "No TCEQ PST records found"}),
        "txdot_aadt": _reuse_result("txdot_aadt", {"aadt": 1000}),
    })
    _denver_run(db, tenant, user, state="CO", minutes_ago=5, results={
        "arcgis_geocode": geocode,
        "fema_nfhl": _reuse_result("fema_nfhl", {"zone": "X"}),
    })
    seed = service.build_refresh_reuse_seed(
        db, tenant_id=tenant.id, address="1437 Bannock St, Denver",
        county="", parcel_id=None,
    )
    assert seed["cached_state"] == "CO"
    assert seed["cached_county"] == "denver"
    assert "environmental_screening" not in seed["adapter_results"]
    assert "txdot_aadt" not in seed["adapter_results"]


def test_district_card_is_not_connected_when_no_layer_exists():
    from app.services.site_check.adapters import district_overlays

    identity = PropertyIdentity(
        address="x", lat=35.9, lng=-86.9, county="williamson", state="TN",
    )
    result = district_overlays.fetch(identity)
    assert result.coverage_status.value == "not_supported_in_jurisdiction"
    assert "No district maps are connected" in result.normalized_facts["advisory"]


def test_denver_environmental_card_on_epa_failure_keeps_the_state_line():
    from app.services.site_check.adapters import environmental_screening as env

    class Offline:
        def get(self, *a, **k):
            raise requests.ConnectionError("offline")

        def __getattr__(self, name):
            return getattr(requests.Session(), name)

    identity = PropertyIdentity(
        address="1437 Bannock St, Denver, CO", lat=39.739, lng=-104.99,
        county="denver", state="CO",
    )
    result = env._fetch_outside_texas(identity, Offline(), 500)
    payload = orchestrator._adapter_result_payload(result, refresh_status="fetched_failed")

    run = SimpleNamespace(
        id="r", status="completed", state="CO", sources_failed=[], derived={},
        adapter_results={"environmental_screening": payload},
        finished_at=None, lat=None, lng=None,
    )
    card = next(
        c for g in cards.build_cards(run, None).get("groups", [])
        for c in g["cards"] if c["card_key"] == "environmental_screening"
    )
    assert "Not connected for Colorado yet" in card["takeaway"]
    assert card["summary"].startswith("State records not connected")
