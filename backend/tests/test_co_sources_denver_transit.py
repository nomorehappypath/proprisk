"""Denver transit_intel (RTD) - replayed from live responses, no network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.regions import arcgis
from app.regions.co.sources.denver import transit_intel as mod
from app.services.site_check import cards, orchestrator
from app.services.site_check.adapters._base import PropertyIdentity

FIX = Path(__file__).parent / "fixtures" / "region_sources" / "co" / "transit_intel"
COLFAX = (39.74074, -104.99009)
RESI = (39.7290, -104.9545)


def _load(name):
    return json.loads((FIX / name).read_text())["features"]


@pytest.fixture
def replay(monkeypatch):
    def install(point, *, fail=()):
        tag = "colfax" if point == COLFAX else "resi"

        def fake(url, params, session, max_records):
            kind = "bus" if "Bus" in url else "rail"
            if kind in fail:
                raise RuntimeError("provider down")
            if kind == "bus":
                assert params["where"] == "STATUS='active'"
            return _load(f"{tag}_{kind}.json")[:max_records]

        monkeypatch.setattr(arcgis, "_query", fake)
    return install


def _ident(point):
    return PropertyIdentity(address="x", lat=point[0], lng=point[1], county="denver", state="CO")


def _card(result):
    payload = orchestrator._adapter_result_payload(result, refresh_status="fetched")
    run = SimpleNamespace(
        id="r", status="completed", state="CO", sources_failed=[], derived={},
        adapter_results={"transit_intel": payload}, finished_at=None, lat=None, lng=None,
    )
    return next(
        c for g in cards.build_cards(run, None)["groups"]
        for c in g["cards"] if c["card_key"] == "transit_intel"
    )


def test_colfax_values_and_every_card_key(replay):
    replay(COLFAX)
    r = mod.fetch(_ident(COLFAX))
    f = r.normalized_facts
    assert r.coverage_status.value == "covered"
    assert f["bus_stops_count"] == 234
    assert f["rail_stations_count"] == 22
    assert f["nearest_bus_stop"]["name"] == f["bus_stops"][0]["name"]
    assert f["nearest_bus_stop"]["distance_mi"] <= f["bus_stops"][1]["distance_mi"]
    assert f["nearest_rail_station"]["name"] == "16th St / California Station"
    assert f["nearest_rail_station"]["distance_mi"] == pytest.approx(0.4, abs=0.15)
    assert f["nearest_rail_station"]["lines"] == "D-F-H-L"
    assert len(f["bus_stops"]) == 10 and len(f["rail_stations"]) == 5
    assert (f["bus_buffer_mi"], f["rail_buffer_mi"]) == (1.0, 3.0)
    assert f["status"] == "clear"
    for key in ("status", "bus_stops_count", "rail_stations_count", "nearest_bus_stop",
                "nearest_rail_station", "bus_stops", "rail_stations", "bus_buffer_mi",
                "rail_buffer_mi", "advisory", "contradiction_flags", "provider_name",
                "provider_short", "source_url", "data_as_of"):
        assert key in f
    for row in f["bus_stops"] + f["rail_stations"]:
        assert {"stop_id", "name", "distance_mi", "lat", "lng"} <= set(row)
        assert 39.5 < row["lat"] < 40 and -105.2 < row["lng"] < -104.8
    assert f["provider_short"] == "RTD"
    assert f["data_as_of"] == "2021–2022"
    assert "CMTA" not in f["advisory"] and "Capital" not in f["advisory"]


def test_card_shows_rtd_and_no_texas_agency(replay):
    replay(COLFAX)
    card = _card(mod.fetch(_ident(COLFAX)))
    assert card["title"] == "Transit (RTD)"
    assert "234 bus stops and 22 rail stations" in card["takeaway"]
    text = json.dumps(card)
    assert "16th St / California Station" in text
    for texas in ("CapMetro", "CMTA", "Capital Metro", "Austin", "TxDOT"):
        assert texas not in text


def test_residential_point_has_stops_and_rail(replay):
    replay(RESI)
    f = mod.fetch(_ident(RESI)).normalized_facts
    assert f["bus_stops_count"] == 99
    assert f["rail_stations_count"] == 14
    assert f["nearest_bus_stop"]["distance_mi"] < 1.0


def test_provider_failure_is_not_a_false_zero(replay):
    replay(COLFAX, fail=("bus",))
    r = mod.fetch(_ident(COLFAX))
    f = r.normalized_facts
    assert r.coverage_status.value == "partial"
    assert "bus_stops_count" not in f and f["parts"]["bus_stops"]["coverage"] == "not_connected"
    assert f["rail_stations_count"] == 22
    assert any("bus stops" in e for e in r.errors)

    replay(COLFAX, fail=("bus", "rail"))
    r = mod.fetch(_ident(COLFAX))
    assert r.coverage_status.value == "manual_verification_required"
    assert r.normalized_facts["status"] == "unknown"
    assert "bus_stops_count" not in r.normalized_facts


def test_no_coordinates_does_not_query(monkeypatch):
    monkeypatch.setattr(arcgis, "_query", lambda *a: pytest.fail("network"))
    r = mod.fetch(PropertyIdentity(address="x", county="denver", state="CO"))
    assert r.coverage_status.value == "manual_verification_required"
    assert r.errors


def test_empty_layers_report_no_transit(monkeypatch):
    monkeypatch.setattr(arcgis, "_query", lambda *a: [])
    f = mod.fetch(_ident(COLFAX)).normalized_facts
    assert f["status"] == "no_transit_in_buffer"
    assert f["bus_stops_count"] == 0 and f["rail_stations_count"] == 0
