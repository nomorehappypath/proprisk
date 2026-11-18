"""Spec 2.6, Codex build review round 1: a state's or county's own source
never turns a missing answer into "none" or "0", reuse follows the source
that would answer now, and the aerial cache key fits its column."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from app.regions import sources
from app.services.site_check import cards, map_context, orchestrator
from app.services.site_check.adapters import txdot_extended
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)
from app.services.site_check.historical_aerials import _Config, _scope_key
from app.services.site_check.identity import PropertyIdentity

DENVER = PropertyIdentity(
    address="201 W Colfax Ave, Denver, CO 80202", lat=39.74074, lng=-104.99009,
    county="denver", state="CO",
)


def _result(slot, facts, coverage=CoverageStatus.COVERED, errors=None):
    return AdapterResult(
        source_id=slot,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=coverage,
        normalized_facts=facts,
        errors=errors,
        fetched_at=datetime.now(UTC),
    )


def _payload(result):
    return orchestrator._adapter_result_payload(result, refresh_status="fetched")


def _run(results, **extra):
    return SimpleNamespace(
        id="r", status="completed", state="CO", county="denver", sources_failed=[],
        derived={}, adapter_results=results, finished_at=None,
        lat=39.74074, lng=-104.99009, parcel_id=None, address=DENVER.address,
        **extra,
    )


def _card(results, key):
    for group in cards.build_cards(_run(results), None).get("groups", []):
        for card in group["cards"]:
            if card["card_key"] == key:
                return card
    raise AssertionError(f"no {key} card")


def _text(card):
    return json.dumps(card).lower()


def _own(slot, facts, **kw):
    module = sources.pick(slot, "CO", "denver")
    assert module is not None, slot
    facts = {
        "provider_name": module.PROVIDER_NAME, "provider_short": module.PROVIDER_SHORT,
        "source_module": module.__name__, "source_version": str(module.VERSION),
        "state": "CO", **facts,
    }
    return _payload(_result(slot, facts, **kw))


def test_a_failed_own_source_says_nothing_was_checked():
    for slot, key in (
        ("transit_intel", "transit_intel"),
        ("txdot_bridges", "txdot_bridges"),
        ("schools_intel", "schools_intel"),
        ("historic_intel", "historic_intel"),
    ):
        payload = _own(
            slot, {"status": "unknown"},
            coverage=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            errors=["provider timed out"],
        )
        card = _card({slot: payload}, key)
        text = _text(card)
        assert card["status"] == "issue", slot
        assert "did not answer this time" in text, slot
        assert "not a \\\"none found\\\"" in text, slot
        assert card["facts"] == [] and card["tables"] == [], slot
        assert "(" in card["title"], slot  # named after its own agency


def test_transit_part_not_answered_is_not_none():
    payload = _own("transit_intel", {
        "status": "partial", "rail_stations_count": 22,
        "nearest_rail_station": {"name": "16th St / California Station", "distance_mi": 0.31},
        "parts": {"bus_stops": {"coverage": "not_connected", "note": "bus layer down"}},
    }, coverage=CoverageStatus.PARTIAL, errors=["bus layer down"])
    card = _card({"transit_intel": payload}, "transit_intel")
    rows = {row["label"]: row["value"] for row in card["facts"]}
    assert rows["Bus stops nearby"] == "Not checked this time"
    assert rows["Rail stations nearby"] == "22"
    # A partial answer gets the card's general "not a full answer" headline;
    # nothing on it may read as "no bus stops".
    assert "no public transit" not in _text(card)
    assert "none" not in json.dumps(rows).lower()


def test_utilities_headline_does_not_deny_sewer_when_not_connected():
    payload = _own("twdb_ccn", {
        "water_provider": "FORT COLLINS", "water_providers": ["FORT COLLINS"],
        "service_territories": [{"service": "water", "provider": "FORT COLLINS",
                                 "authority": "service_area", "contains_site": True}],
        "parts": {"sewer": {"coverage": "not_connected",
                            "note": "no sewer source outside Metro Water Recovery's area"}},
    })
    jr = _payload(_result("jurisdiction_resolver", {
        "jurisdiction_kind": "full_purpose_city", "city": "Fort Collins"}))
    card = _card({"twdb_ccn": payload, "jurisdiction_resolver": jr}, "twdb_ccn")
    text = _text(card)
    assert "certificated" not in text
    assert "sewer provider is not connected here" in card["takeaway"]


def test_summary_keeps_missing_road_inputs_unknown(monkeypatch):
    traffic = _result("txdot_aadt", {
        "provider_short": "FHWA HPMS", "source_module": "x", "traffic_counts": [
            {"road_name": "US 40", "aadt": 31000, "year": 2023, "distance_mi": 0.05}],
    })
    upstream = {
        "txdot_aadt": _payload(traffic),
        "txdot_roadway": _payload(_result("txdot_roadway", {"roadways": []})),
        "txdot_projects": {"coverage_status": "not_supported_in_jurisdiction",
                           "normalized_facts": {}},
        "txdot_bridges": {"coverage_status": "not_supported_in_jurisdiction",
                          "normalized_facts": {}},
    }
    result = txdot_extended.fetch(DENVER, config={"upstream_results": upstream})
    facts = result.normalized_facts
    assert facts["project_pipeline"]["count_within_radius"] is None
    assert facts["bridge_constraints"]["count_within_radius"] is None
    text = json.dumps(facts).lower()
    assert "no colorado bridge records" not in text
    assert "no mapped colorado projects" not in text
    assert "not checked: bridges, road projects" in facts["broker_read"]
    card = _card({"txdot_extended": _payload(result)}, "txdot_extended")
    assert "0 within" not in _text(card)


def test_reuse_refetches_a_pre_c2_colorado_environmental_result():
    old = {"state": "CO", "state_records": "not_connected", "echo_records": []}
    assert not sources.result_matches_current_source(
        "environmental_screening", old, state="CO", county="denver", built_in_covers=True)
    module = sources.pick("state_environmental_records", "CO", "denver")
    current = {**old, "embedded_sources": {"state_environmental_records": {
        "module": module.__name__, "version": str(module.VERSION)}}}
    assert sources.result_matches_current_source(
        "environmental_screening", current, state="CO", county="denver", built_in_covers=True)
    # Texas keeps reusing its own results.
    assert sources.result_matches_current_source(
        "environmental_screening", {"state": "TX"}, state="TX", county="travis",
        built_in_covers=True)


def test_aerial_cache_key_fits_its_column():
    cfg = _Config(object())
    cfg.eras = {
        str(year): f"https://drcog-data.sanborn.com/arcgis/rest/services/DRCOG_{year}/"
        "Orthos_3in_6in_12in_long_service_name/ImageServer"
        for year in range(1990, 2027)
    }
    key = _scope_key(_run({}), cfg)
    assert len(key) <= 255
    other = dict(cfg.eras, **{"2022": "https://example.gov/x/ImageServer"})
    cfg2 = _Config(object())
    cfg2.eras = other
    assert _scope_key(_run({}), cfg2) != key  # a changed service is a new key


def test_denver_sewer_mains_are_drawn_on_the_map():
    payload = _own("twdb_ccn", {
        "service_territories": [],
        "sewer_mains": [{
            "provider": "Denver Wastewater Management", "diameter_in": 10,
            "distance_m": 54,
            "geometry": {"type": "LineString",
                         "coordinates": [[-104.9901, 39.7406], [-104.9895, 39.7406]]},
        }],
    })
    features = map_context._physical_utility_features(_run({"twdb_ccn": payload}))
    mains = [f for f in features if f.kind == "utility_main_wastewater"]
    assert len(mains) == 1
    assert mains[0].source == "Denver Wastewater Management"
