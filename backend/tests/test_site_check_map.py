"""Public Data map projection tests — GET /api/site-check/runs/{id}/map.

Postgres only (house conftest). Covers tenancy both directions, the
"staged results required" refusal, the missing-coordinates refusal, and
the payload shape on a REAL seeded run (the Chandler / Hutto snapshot
captured from the dev pipeline, stored gzipped under
tests/fixtures/site_check_map/).
"""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models import SiteCheckRun
from app.services.site_check import service as sc_service
from app.services.site_check.map_context import _housing_project_points

FIXTURE = Path(__file__).parent / "fixtures" / "site_check_map" / "chandler_run.json.gz"


def _chandler() -> dict:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def chandler() -> dict:
    return _chandler()


@pytest.fixture
def tenant_a(local_tenant_user):
    # Every HTTP request resolves to the one fixed local office — this
    # file's primary flow goes through the API, so its rows must belong
    # to that same office.
    return local_tenant_user[0]


@pytest.fixture
def tenant_b(make_tenant):
    # A genuinely separate office, for the tenant-isolation checks below,
    # which assert separation at the service layer — PropRisk has only one
    # HTTP identity, so a second office can't be reached over HTTP.
    return make_tenant("Map Beta")


@pytest.fixture
def user_a(local_tenant_user):
    return local_tenant_user[1]


@pytest.fixture
def user_b(make_user, tenant_b):
    return make_user(tenant_b, "map-beta@example.com")


@pytest.fixture
def hdrs_a(login, user_a):
    return login("map-alpha@example.com")


def _mk_run(db, tenant, user, *, status="completed", adapter_results=None,
            address="100 Congress Ave, Austin TX", lat=30.2649, lng=-97.7431,
            county="travis"):
    run = SiteCheckRun(
        tenant_id=tenant.id,
        requested_by=user.id,
        address=address,
        normalized_address=sc_service.normalize_address(address),
        county=county,
        lat=lat,
        lng=lng,
        status=status,
        adapter_results=adapter_results if adapter_results is not None else {},
        sources_queried=[],
        sources_failed=[],
        derived={},
        finished_at=(
            datetime.now(UTC) if status not in ("queued", "running") else None
        ),
    )
    db.add(run)
    db.commit()
    return run


def _seed_chandler(db, tenant, user, chandler, **kw):
    return _mk_run(
        db, tenant, user,
        address=chandler["address"],
        county=chandler["county"],
        lat=chandler["lat"],
        lng=chandler["lng"],
        adapter_results=chandler["adapter_results"],
        **kw,
    )


# ── tenancy ────────────────────────────────────────────────────────────


def test_map_is_tenant_scoped_a_cannot_read_b(db, tenant_a, tenant_b, user_b,
                                              chandler):
    """PropRisk has only one signed-in office over HTTP, so tenant isolation
    for the map route (which 404s via the same `_get_run`/`sc_service.get_run`
    tenant scoping as every other route) is checked at the service layer
    instead of with a second signed-in user."""
    run = _seed_chandler(db, tenant_b, user_b, chandler)
    assert sc_service.get_run(db, tenant_id=tenant_a.id, run_id=run.id) is None


def test_map_is_tenant_scoped_b_cannot_read_a(db, tenant_a, user_a, tenant_b,
                                              chandler):
    run = _seed_chandler(db, tenant_a, user_a, chandler)
    assert sc_service.get_run(db, tenant_id=tenant_b.id, run_id=run.id) is None


def test_map_unknown_run_is_404(client, hdrs_a):
    resp = client.get("/api/site-check/runs/does-not-exist/map", headers=hdrs_a)
    assert resp.status_code == 404


# ── refusals ───────────────────────────────────────────────────────────


def test_map_refuses_active_run(client, db, tenant_a, user_a, hdrs_a):
    """A live lookup (created through the API so it owns a real queued job
    and survives §7.6 read-path reconciliation) refuses the map."""
    created = client.post(
        "/api/site-check", json={"address": "701 Brazos St, Austin TX"},
        headers=hdrs_a,
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    resp = client.get(f"/api/site-check/runs/{run_id}/map", headers=hdrs_a)
    assert resp.status_code == 409
    assert "still running" in resp.json()["error"]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_map_refuses_terminal_run_with_no_staged_results(
    client, db, tenant_a, user_a, hdrs_a, status,
):
    run = _mk_run(db, tenant_a, user_a, status=status, adapter_results={})
    resp = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)
    assert resp.status_code == 409
    assert "nothing to map" in resp.json()["error"]


def test_map_refuses_run_without_coordinates(client, db, tenant_a, user_a,
                                             hdrs_a):
    run = _mk_run(
        db, tenant_a, user_a, lat=None, lng=None,
        adapter_results={"osm_overpass": {"normalized_facts": {"places": []}}},
    )
    resp = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)
    assert resp.status_code == 409
    assert "no map location" in resp.json()["error"]


# ── payload shape on a real seeded run ─────────────────────────────────


@pytest.fixture
def chandler_payload(client, db, tenant_a, user_a, hdrs_a, chandler):
    run = _seed_chandler(db, tenant_a, user_a, chandler)
    resp = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    body["_run_id"] = run.id
    return body


def test_map_envelope_and_anchor(chandler_payload, chandler):
    body = chandler_payload
    assert body["run_id"] == body["_run_id"]
    assert body["address"] == chandler["address"]
    assert body["subject_anchor"] == "parcel"
    assert body["parcel_geojson"]["type"] in ("Polygon", "MultiPolygon")
    center = body["center"]
    assert 30.5 < center["lat"] < 30.7
    assert -97.7 < center["lng"] < -97.4
    bounds = body["bounds"]
    assert bounds["west"] < center["lng"] < bounds["east"]
    assert bounds["south"] < center["lat"] < bounds["north"]


def test_map_basemaps_are_the_allowlisted_public_tiles(chandler_payload):
    basemaps = chandler_payload["basemaps"]
    assert [b["id"] for b in basemaps] == ["aerial_roads", "aerial", "terrain"]
    for basemap in basemaps:
        assert basemap["tile_url"].startswith("https://")
        assert basemap["attribution"]
        assert basemap["provider_label"]
        assert basemap["detail_advisory"]
    assert basemaps[0]["road_vector_url"]
    assert basemaps[0]["road_glyphs_url"]


def test_map_layers_carry_legend_details_and_opacity(chandler_payload):
    layers = chandler_payload["layers"]
    ids = [layer["id"] for layer in layers]
    # Every layer id the the reference app projection emits, in its emitted order.
    assert ids == [
        "parcel", "neighbors", "projects", "utilities", "sewer_ccn",
        "water_districts", "water_ccn", "traffic", "access",
        "population_density", "regulatory", "environmental",
        "buildable_envelope", "flood", "drainage", "wetlands", "zoning",
        "elevation",
    ]
    assert len(ids) == len(set(ids))
    for layer in layers:
        assert layer["kind"] in ("geojson", "raster")
        assert isinstance(layer["available"], bool)
        assert layer["status"]
        assert layer["attribution"]
        assert 0.0 <= layer["opacity"] <= 1.0
        for entry in layer["legend"]:
            assert set(entry) >= {"label"}
        for entry in layer["details"]:
            assert set(entry) >= {"label", "value"}
        if layer["kind"] == "raster" and layer["available"]:
            assert layer["tile_url"].startswith("https://")

    by_id = {layer["id"]: layer for layer in layers}
    # Parcel geometry drives the subject layer; flood/wetlands/elevation are
    # the public raster screens.
    assert by_id["parcel"]["available"] is True
    assert by_id["parcel"]["geojson"]["type"] == "FeatureCollection"
    for raster_id in ("flood", "wetlands", "elevation"):
        assert by_id[raster_id]["kind"] == "raster"
        assert by_id[raster_id]["tile_url"]


def test_map_categories_and_places_are_ranked_by_distance(chandler_payload):
    places = chandler_payload["places"]
    assert places, "the Chandler snapshot stores nearby places"
    distances = [place["distance_m"] for place in places]
    assert distances == sorted(distances)
    for place in places:
        assert place["name"]
        assert place["distance_basis"] in ("parcel_boundary", "address_pin")
        assert place["distance_m"] >= 0
        assert -180 <= place["lng"] <= 180
        assert -90 <= place["lat"] <= 90
        assert set(place["nearest_subject"]) == {"lat", "lng"}
        assert place["confidence"]

    categories = chandler_payload["categories"]
    assert categories
    counted = {category["id"]: category["count"] for category in categories}
    observed: dict[str, int] = {}
    for place in places:
        observed[place["category"]] = observed.get(place["category"], 0) + 1
    assert counted == observed
    assert sum(counted.values()) == len(places)
    for category in categories:
        assert category["label"]
        assert category["icon"]
        assert category["group"]
    # Default-selected categories sort first (the mobile sheet opens on them).
    selected = [bool(c["default_selected"]) for c in categories]
    assert selected == sorted(selected, reverse=True)
    assert any(selected)


def test_map_context_features_carry_geometry_and_details(chandler_payload):
    features = chandler_payload["context_features"]
    assert features, "the Chandler snapshot stores map context features"
    ids = [feature["id"] for feature in features]
    assert len(ids) == len(set(ids))
    kinds = {feature["kind"] for feature in features}
    # The the reference app context-feature families this address exercises.
    assert {"access_roadway", "neighbor_parcel", "traffic_count"} <= kinds
    for feature in features:
        assert feature["title"]
        assert feature["source"]
        assert feature["confidence"]
        assert feature["geometry"]["type"] in (
            "Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon",
        )
        for entry in feature["details"]:
            assert set(entry) >= {"label", "value"}


def test_nearest_sewer_ccn_is_labeled_as_context_not_service(
    client, db, tenant_a, user_a, hdrs_a,
):
    parcel = {
        "type": "Polygon",
        "coordinates": [[
            [-97.484, 30.351], [-97.482, 30.351],
            [-97.482, 30.353], [-97.484, 30.353],
            [-97.484, 30.351],
        ]],
    }
    nearest_ccn = {
        "type": "Polygon",
        "coordinates": [[
            [-97.47, 30.34], [-97.46, 30.34],
            [-97.46, 30.35], [-97.47, 30.35],
            [-97.47, 30.34],
        ]],
    }
    run = _mk_run(
        db, tenant_a, user_a,
        address="13105 FM 1100 Rd, Manor TX 78653",
        adapter_results={
            "tcad_parcel": {"normalized_facts": {"parcel_polygon": parcel}},
            "jurisdiction_resolver": {"normalized_facts": {
                "city": "Manor",
                "jurisdiction_kind": "full_purpose_city",
            }},
            "twdb_ccn": {"normalized_facts": {
                "service_territories": [{
                    "service": "wastewater",
                    "authority": "nearest_ccn",
                    "provider": "CITY OF ELGIN",
                    "source": "PUCT CCN Sewer Service Areas",
                    "geometry": nearest_ccn,
                }],
            }},
        },
    )

    resp = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)

    assert resp.status_code == 200
    layer = next(item for item in resp.json()["layers"] if item["id"] == "sewer_ccn")
    assert layer["label"] == "Nearest certificated sewer area (context only)"
    assert layer["legend"][0]["label"] == "Nearest sewer CCN - context only"
    assert "CITY OF ELGIN" in layer["advisory"]
    assert "does not serve this parcel" in layer["advisory"]
    assert {row["label"]: row["value"] for row in layer["details"]}[
        "First city sewer contact"
    ] == "City of Manor"


def test_county_housing_projects_join_the_apartments_category():
    run = type("Run", (), {"adapter_results": {
        "entitlement_pipeline": {"normalized_facts": {
            "county_gis_source": "Williamson County GIS",
            "subdivision_cases": [{
                "name": "Firefly Pointe Ph 2",
                "map_lat": 30.585,
                "map_lng": -97.55,
                "lot_count": 258,
                "status": "FPA",
            }],
        }},
    }})()

    points = _housing_project_points(run)

    assert len(points) == 1
    assert points[0]["category"] == "apartments"
    assert points[0]["name"] == "Firefly Pointe Ph 2"
    assert points[0]["address"] == "FPA · 258 platted lots"


def test_map_is_read_only_and_repeatable(client, db, tenant_a, user_a, hdrs_a,
                                         chandler):
    """No provider calls, no writes: two reads of the same stored run return
    byte-identical payloads and leave the run untouched."""
    run = _seed_chandler(db, tenant_a, user_a, chandler)
    before = dict(run.adapter_results or {})
    first = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)
    second = client.get(f"/api/site-check/runs/{run.id}/map", headers=hdrs_a)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    db.refresh(run)
    assert run.adapter_results == before
    assert run.status == "completed"
