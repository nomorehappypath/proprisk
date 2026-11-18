"""Denver planning sources (spec 2.6): zoning, zoning standards, future land use,
development activity, historic. Replays real Denver Open Data responses recorded
on 2026-11-18 from tests/fixtures/region_sources/co/<slot>/<point>.json; no network.

Re-record with RECORD_FIXTURES=1 (calls the live layers)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.regions import arcgis, sources
from app.regions.co.sources.denver import _planning as planning
from app.regions.co.sources.denver import (
    entitlement_pipeline,
    zoning_standards,
)
from app.services.site_check import cards, orchestrator
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.zoning_standards import engine
from app.services.site_check.zoning_standards.schema import CityZoningStandards

FIXTURES = Path(__file__).parent / "fixtures" / "region_sources" / "co"
RECORD = os.environ.get("RECORD_FIXTURES") == "1"
POINTS = {
    "colfax": (39.74074, -104.99009),     # 201 W Colfax Ave parcel point
    "residential": (39.7290, -104.9545),  # Congress Park, U-SU-C
    "larimer": (39.7478, -104.9988),      # Larimer Square
    "rino": (39.7700, -104.9750),         # I-B
    "street": (39.7401, -104.9915),       # in the Colfax right-of-way: no future place mapped
}
FIXED_NOW = datetime(2026, 11, 18, tzinfo=UTC)
TEXAS_WORDS = ("TxDOT", "TxGIO", "Texas", "Travis", "Austin", "THC")


def _key(url, params):
    return f"{url}|{params.get('geometry')}|{params.get('distance')}"


@pytest.fixture
def replay(monkeypatch):
    """run(slot, point, config) -> AdapterResult, answered from the recorded fixture."""

    def run(slot, point, config=None, module=None):
        module = module or sources.pick(slot, "CO", "Denver")
        path = FIXTURES / slot / f"{point}.json"
        store = json.loads(path.read_text()) if path.exists() and not RECORD else {"calls": {}, "meta": {}}
        real_query, real_meta = arcgis._query, planning._layer_meta

        def fake_query(url, params, session, max_records):
            key = _key(url, params)
            if RECORD:
                store["calls"][key] = real_query(url, params, session, max_records)
            return store["calls"][key]

        def fake_meta(url, session):
            if RECORD:
                payload = real_meta(url, session)
                store["meta"][url] = {"editingInfo": payload.get("editingInfo")}
            return store["meta"][url]

        monkeypatch.setattr(arcgis, "_query", fake_query)
        monkeypatch.setattr(planning, "_layer_meta", fake_meta)
        monkeypatch.setattr(entitlement_pipeline, "_today", lambda: FIXED_NOW)
        lat, lng = POINTS[point]
        identity = PropertyIdentity(address="x", lat=lat, lng=lng, county="denver", state="CO")
        try:
            return module.fetch(identity, session=None, config=config)
        finally:
            if RECORD:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(store, indent=1, sort_keys=True))

    return run


def _card(slot, result):
    payload = orchestrator._adapter_result_payload(result, refresh_status="fetched")
    run = SimpleNamespace(
        id="r", status="completed", state="CO", sources_failed=[], derived={},
        adapter_results={slot: payload}, finished_at=None, lat=None, lng=None,
    )
    card = next(c for g in cards.build_cards(run, None)["groups"] for c in g["cards"] if c["card_key"] == slot)
    blob = json.dumps(card)
    assert not [w for w in TEXAS_WORDS if w in blob], blob
    return card


def _facts(result):
    return result.normalized_facts


def _standard(facts):
    for key in ("provider_name", "provider_short", "source_url", "data_as_of"):
        assert facts[key], key
    assert facts["data_as_of"] == "unknown" or len(facts["data_as_of"]) == 10


# ── the picker finds all five ───────────────────────────────────────────────


@pytest.mark.parametrize("slot", ["austin_zoning", "zoning_standards", "future_land_use",
                                  "entitlement_pipeline", "historic_intel"])
def test_denver_has_a_module_for_every_slot(slot):
    module = sources.pick(slot, "CO", "Denver")
    assert module is not None and module.SOURCE_ID == slot
    assert module.PROVIDER_NAME == "City and County of Denver"


# ── zoning ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("point", "district", "overlay"), [
    ("colfax", "D-C", "UO-1"),
    ("residential", "U-SU-C", None),
    ("larimer", "D-C", "UO-1"),
    ("rino", "I-B", "UO-2"),
])
def test_zoning_district_in_both_fields(replay, point, district, overlay):
    result = replay("austin_zoning", point)
    f = _facts(result)
    _standard(f)
    assert f["zoning_type"] == f["base_zone"] == district
    assert f["status"] == "clear" and f["zones_found"] == [district]
    assert f["jurisdiction"] == "Denver" and f["jurisdiction_kind"] == "city"
    assert f["data_as_of"] == "2026-11-12"
    assert f["query_anchor"] == "point"
    assert (overlay in f["overlay_districts"]) if overlay else f["overlay_districts"] == []
    assert result.coverage_status.value == "covered"


def test_zoning_card_shows_district_and_no_texas_agency(replay):
    card = _card("austin_zoning", replay("austin_zoning", "residential"))
    assert "U-SU-C" in card["takeaway"] and "Denver" in card["takeaway"]
    labels = {row["label"]: row["value"] for row in card["facts"]}
    assert labels["Zoning district"] == "U-SU-C"
    assert "Accessory dwelling units: Yes" in labels["What this means"]


def test_zoning_orchestrator_hands_district_to_standards():
    result = SimpleNamespace(normalized_facts={"zoning_type": "U-SU-C", "base_zone": "U-SU-C",
                                               "jurisdiction": "Denver"})
    config = orchestrator._build_adapter_config("zoning_standards", {"austin_zoning": {
        "normalized_facts": result.normalized_facts}})
    assert config["district"] == "U-SU-C"


# ── zoning standards (curated denver.yaml) ──────────────────────────────────


def test_denver_yaml_validates_and_every_value_cites_its_section():
    import yaml

    raw = yaml.safe_load((engine._CURATED_DIR / "denver.yaml").read_text())
    city = CityZoningStandards.model_validate(raw)
    assert {"U-SU-C", "D-C", "U-SU-A", "U-SU-B", "E-SU-D", "U-MX-3", "C-MX-5", "I-MX-3"} <= set(city.districts)
    for key, rules in city.districts.items():
        assert rules.dimensional_standards, key
        for std in rules.dimensional_standards.values():
            assert std.code_ref.startswith("DZC "), (key, std)


def test_u_su_c_numbers_match_the_zoning_code(replay):
    result = replay("zoning_standards", "residential", {"district": "U-SU-C"})
    f = _facts(result)
    _standard(f)
    d = f["dimensional_standards"]
    assert f["status"] == "clear" and f["district"] == "U-SU-C"
    assert (d["min_lot_size"]["value"], d["min_lot_width"]["value"]) == (5500, 50)
    assert d["max_height"]["value"] == 30 and d["max_stories"]["value"] == 2.5
    assert d["side_setback"]["value"] == 5 and d["rear_setback"]["value"] == 20
    assert d["max_building_coverage"]["value"] == 45
    assert d["front_setback"]["value"].startswith("Calculated per DZC 13.1.5.9")
    assert "5.3.3.3.A" in d["max_height"]["code_ref"]


def test_downtown_core_numbers(replay):
    f = _facts(replay("zoning_standards", "colfax", {"district": "D-C"}))
    assert f["dimensional_standards"]["max_far"]["value"] == 10.0
    assert "200 ft" in f["dimensional_standards"]["max_height"]["notes"]


def test_district_codes_ending_in_h_are_not_stripped(replay):
    f = _facts(replay("zoning_standards", "residential", {"district": "U-SU-H"}))
    assert f["district"] == "U-SU-H" and f["dimensional_standards"]["min_lot_width"]["value"] == 75


def test_uncurated_district_says_so_not_made_up(replay):
    f = _facts(replay("zoning_standards", "residential", {"district": "PUD"}))
    assert f["status"] == "district_not_curated" and "dimensional_standards" not in f
    assert f["provider_name"] == "City and County of Denver"


def test_no_district_is_said(replay):
    assert _facts(replay("zoning_standards", "residential", {}))["status"] == "no_district_provided"


def test_version_carries_the_file_hash():
    import hashlib

    digest = hashlib.sha256((engine._CURATED_DIR / "denver.yaml").read_bytes()).hexdigest()[:8]
    assert zoning_standards.VERSION == f"1-{digest}"


def test_standards_card_cites_the_code(replay):
    card = _card("zoning_standards", replay("zoning_standards", "residential", {"district": "U-SU-C"}))
    assert "U-SU-C" in card["takeaway"] and "5.2.2" in card["takeaway"]
    blob = json.dumps(card)
    assert "5.3.3.3.A" in blob and "5500" in blob.replace(",", "")


# ── future land use ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("point", "label", "growth"), [
    ("colfax", "Civic (Special District context)", "All other areas of the city"),
    ("residential", "Low (Urban context)", "All other areas of the city"),
    ("larimer", "Regional Center (Downtown context)", "Regional centers"),
])
def test_blueprint_future_place(replay, point, label, growth):
    f = _facts(replay("future_land_use", point))
    _standard(f)
    assert f["status"] == "clear" and f["jurisdiction"] == "denver"
    assert f["designations"][0]["designation"] == label
    assert f["designations"][0]["overlays"] == [f"Growth strategy: {growth}"]
    assert f["plan_name"] and f["plan_url"]


def test_future_land_use_card(replay):
    card = _card("future_land_use", replay("future_land_use", "residential"))
    assert "Low (Urban context)" in card["takeaway"] and "Denver" in card["takeaway"]


def test_no_designation_is_not_a_designation(replay):
    result = replay("future_land_use", "street")
    f = _facts(result)
    assert f["status"] == "unknown" and f["designations"] == []
    assert result.coverage_status.value == "partial"
    card = _card("future_land_use", result)
    assert "does not show a designation" in card["takeaway"]


# ── development activity ────────────────────────────────────────────────────


def test_entitlement_near_colfax(replay):
    result = replay("entitlement_pipeline", "colfax")
    f = _facts(result)
    _standard(f)
    assert f["search_radius_m"] == 800 and f["source_status"] == "queried"
    assert f["zoning_case_count"] == len(f["zoning_cases"]) >= 1
    assert f["site_plan_case_count"] >= 1
    assert f["city_entitlement_record_count"] >= 1
    first = f["city_entitlement_records"][0]
    assert first["permit_type"] in {"New Building", "Addition", "Wreck / demolition"}
    assert set(f["parts"]) == {"subdivision_plats", "current_rezoning_applications"}
    assert all(v["coverage"] == "not_connected" for v in f["parts"].values())


def test_permit_class_is_normalised():
    cases = {"NEW BUILDING": "New Building", "New Building": "New Building", "1 - NEW BUILDING": "New Building",
             "6-Wreck": "Wreck / demolition", "6 - WRECK": "Wreck / demolition", "ADDITION": "Addition"}
    for raw, want in cases.items():
        assert entitlement_pipeline._permit_class(raw) == want


def test_pending_rezoning_carries_application_date_and_staleness_note():
    ms = 1700000000000  # 2023-11-14
    rows = [{"APP_NUM": "23i-00099", "APP_DATE": ms, "STATUS": "Pending", "FROM_ZONE_DESC": "From U-SU-C",
             "ZONE_DISTRICT": "U-MX-3", "NBHD": "Capitol Hill", "_distance_m": 120.0}]
    cases = entitlement_pipeline._rezonings(rows, 0)
    assert cases[0]["status"] == "Pending (applied 2023-11-14; the city's status may be out of date)"
    assert cases[0]["case_name"] == "Rezoning U-SU-C to U-MX-3"
    assert "no street address" in cases[0]["address"]


def test_old_passed_rezonings_are_dropped_but_pending_kept():
    old = {"APP_NUM": "a", "APP_DATE": 1_000_000_000_000, "PASSED_DATE": 1_000_000_000_000, "STATUS": "Passed",
           "ZONE_DISTRICT": "X", "_distance_m": 1.0}
    pending = {"APP_NUM": "b", "APP_DATE": 1_000_000_000_000, "STATUS": "Pending", "ZONE_DISTRICT": "Y",
               "_distance_m": 2.0}
    cases = entitlement_pipeline._rezonings([old, pending], 1_500_000_000_000)
    assert [c["case_number"] for c in cases] == ["b"]


def test_entitlement_card_has_tables_and_no_texas_agency(replay):
    card = _card("entitlement_pipeline", replay("entitlement_pipeline", "colfax"))
    titles = [t["title"] for t in card["tables"]]
    assert "Site plans nearby" in titles and "City project records" in titles
    assert "Denver" in json.dumps(card)


def test_entitlement_all_layers_failing_is_an_error_not_nothing_found(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(arcgis, "_query", boom)
    monkeypatch.setattr(planning, "_layer_meta", boom)
    identity = PropertyIdentity(address="x", lat=39.74, lng=-104.99, county="denver", state="CO")
    result = entitlement_pipeline.fetch(identity)
    assert result.errors and result.coverage_status.value == "manual_verification_required"
    assert "zoning_cases" not in result.normalized_facts


# ── historic ────────────────────────────────────────────────────────────────


def test_larimer_square_is_historic(replay):
    result = replay("historic_intel", "larimer")
    f = _facts(result)
    _standard(f)
    assert f["status"] == "in_historic_district" and f["is_in_local_historic_district"] is True
    district = f["districts"][0]
    assert district["name"] == "Larimer Square" and district["case_number"] == "1"
    assert district["ordinance_number"] == "288 (1971)"
    assert f["nearby_landmarks_count"] == 27
    assert f["nearby_landmarks"][0]["name"] == "Sussex Building"
    assert f["nearby_landmarks"][0]["distance_mi"] == pytest.approx(0.02, abs=0.01)
    assert "Lower Downtown" in f["nearby_districts"]
    assert "thc_atlas_url" not in f


def test_residential_point_is_in_east_seventh_avenue(replay):
    f = _facts(replay("historic_intel", "residential"))
    assert f["districts"][0]["name"] == "East Seventh Avenue" and f["nearby_landmarks_count"] == 0


def test_colfax_is_near_landmarks_not_in_a_district(replay):
    f = _facts(replay("historic_intel", "colfax"))
    assert f["status"] == "near_landmark" and f["is_in_local_historic_district"] is False
    assert f["nearby_landmarks_count"] == 6
    assert f["nearby_landmarks"][0]["name"] == "Curry-Chucovich House"
    assert f["nearby_districts"] == ["Civic Center"]


def test_rino_has_nothing_mapped(replay):
    f = _facts(replay("historic_intel", "rino"))
    assert f["status"] == "clear" and f["nearby_landmarks_count"] == 0


def test_historic_card(replay):
    card = _card("historic_intel", replay("historic_intel", "larimer"))
    assert "Larimer Square" in card["takeaway"]
    assert "Texas Historical Commission" not in json.dumps(card)


# ── errors and missing coordinates ──────────────────────────────────────────


@pytest.mark.parametrize("slot", ["austin_zoning", "future_land_use", "historic_intel"])
def test_provider_error_is_an_error_never_a_clear_result(monkeypatch, slot):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(arcgis, "_query", boom)
    identity = PropertyIdentity(address="x", lat=39.74, lng=-104.99, county="denver", state="CO")
    result = sources.pick(slot, "CO", "Denver").fetch(identity)
    assert result.errors and result.normalized_facts["status"] == "unknown"
    assert result.normalized_facts["provider_name"] == "City and County of Denver"


@pytest.mark.parametrize("slot", ["austin_zoning", "future_land_use", "historic_intel", "entitlement_pipeline"])
def test_no_coordinates(slot):
    result = sources.pick(slot, "CO", "Denver").fetch(PropertyIdentity(address="x"))
    assert result.errors and result.coverage_status.value == "manual_verification_required"
