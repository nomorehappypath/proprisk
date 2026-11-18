"""Colorado's road sources (spec 2.6): traffic counts, traffic history, roads,
road projects and bridges, replayed offline from recorded live responses
(tests/fixtures/region_sources/co/<slot>/<point>.json, recorded 2026-11-18).

No test touches the network: the shared ArcGIS reader is replaced by a replay
of the recorded layer answers.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.regions import arcgis
from app.regions.sources import pick
from app.services.site_check import cards, orchestrator
from app.services.site_check.adapters import txdot_extended
from app.services.site_check.identity import PropertyIdentity

FIXTURES = Path(__file__).parent / "fixtures" / "region_sources" / "co"
POINTS = {
    "colfax": (39.74074, -104.99009),     # 201 W Colfax Ave
    "residential": (39.7290, -104.9545),
    "i70": (39.78004, -104.95),           # I-70 near Monaco St
}
SLOTS = (
    "txdot_aadt", "txdot_aadt_history", "txdot_roadway", "txdot_projects",
    "txdot_bridges",
)
TEXAS_WORDS = ("txdot", "texas", "txgio", "cris")


def identity(point):
    lat, lng = POINTS[point]
    return PropertyIdentity(address="x", lat=lat, lng=lng, county="denver", state="CO")


@pytest.fixture
def replay(monkeypatch):
    """Answer every layer query from the recorded file; fail on anything else."""
    def use(slot, point):
        recorded = json.loads((FIXTURES / slot / f"{point}.json").read_text())

        def fake(url, params, session, max_records):
            assert url in recorded, f"unrecorded layer queried: {url}"
            return recorded[url][:max_records]

        monkeypatch.setattr(arcgis, "_query", fake)
    return use


@pytest.fixture
def run_slot(replay):
    def go(slot, point="colfax"):
        replay(slot, point)
        return pick(slot, "CO", "denver").fetch(identity(point))
    return go


def facts_of(result):
    return result.normalized_facts


def card_for(slot_results: dict, card_key: str) -> dict:
    run = SimpleNamespace(
        id="r", status="completed", state="CO", sources_failed=[], derived={},
        adapter_results={
            slot: orchestrator._adapter_result_payload(res, refresh_status="fetched")
            for slot, res in slot_results.items()
        },
        finished_at=None, lat=None, lng=None,
    )
    built = cards.build_cards(run, None)
    return next(c for g in built["groups"] for c in g["cards"] if c["card_key"] == card_key)


def card_text(card: dict) -> str:
    """Everything the card shows except link labels (those are shared text)."""
    parts = [card.get("takeaway") or ""]
    parts += [f"{f['label']} {f['value']}" for f in card.get("facts", [])]
    for table in card.get("tables", []):
        parts.append(table.get("title") or "")
        parts += [str(c) for row in table.get("rows", []) for c in row]
    parts += card.get("advisories") or []
    return " ".join(parts).lower()


def assert_provenance(result, name_part, short):
    f = facts_of(result)
    assert name_part in f["provider_name"]
    assert f["provider_short"] == short
    assert f["source_url"].startswith("https://")
    assert f["data_as_of"] != "unknown"


# ── every slot is found for Colorado ───────────────────────────────────────


@pytest.mark.parametrize("slot", SLOTS)
def test_picker_finds_the_colorado_module(slot):
    module = pick(slot, "CO", "denver")
    assert module.SOURCE_ID == slot
    assert module.VERSION == 1
    assert module.PROVIDER_NAME and module.PROVIDER_SHORT


@pytest.mark.parametrize("slot", SLOTS)
def test_no_coordinates_is_an_error_not_a_crash(slot):
    result = pick(slot, "CO", "denver").fetch(
        PropertyIdentity(address="x", county="denver", state="CO"))
    assert result.errors and result.normalized_facts == {}


@pytest.mark.parametrize("slot", SLOTS)
def test_provider_error_is_reported_not_raised(slot, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(arcgis, "_query", boom)
    result = pick(slot, "CO", "denver").fetch(identity("colfax"))
    assert result.errors and "provider down" in result.errors[0]
    assert result.normalized_facts == {}


# ── Traffic counts ─────────────────────────────────────────────────────────


def test_counts_at_colfax(run_slot):
    result = run_slot("txdot_aadt")
    f = facts_of(result)
    assert_provenance(result, "Highway Performance Monitoring", "FHWA HPMS")
    counts = f["traffic_counts"]
    by_name = {c["road_name"]: c for c in counts}
    colfax = by_name["U.S. 40"]
    assert colfax["aadt"] == 31000 and colfax["year"] == "2023"
    assert colfax["is_frontage"] is True
    assert colfax["distance_mi"] < 0.1
    assert colfax["map_geometry"]["type"] in {"LineString", "MultiLineString"}
    assert by_name["15TH ST"]["aadt"] == 10000
    # one entry per named road, frontage roads first, then busiest
    assert len(by_name) == len(counts)
    flags = [c["is_frontage"] for c in counts]
    assert flags == sorted(flags, reverse=True)
    assert f["data_as_of"] == "2020–" + "2025"


def test_counts_keys_the_cards_read(run_slot):
    for c in facts_of(run_slot("txdot_aadt"))["traffic_counts"]:
        for key in ("road_name", "aadt", "year", "source", "is_frontage",
                    "map_geometry", "lat", "lng", "distance_mi"):
            assert key in c
        assert isinstance(c["aadt"], int) and c["aadt"] > 0


def test_counts_add_named_local_counts_without_repeating_a_road(run_slot):
    counts = facts_of(run_slot("txdot_aadt", "residential"))["traffic_counts"]
    names = [c["road_name"] for c in counts]
    drcog = [c for c in counts if c["source"] == "DRCOG traffic counts"]
    assert drcog and all(c["map_geometry"] is None for c in drcog)
    # Colorado Blvd is SH 2 in HPMS; DRCOG's "(SH-2)" copy is not listed twice
    assert "SH 2" in names and not any("SH-2" in n for n in names)


def test_counts_partial_when_drcog_is_down(replay, monkeypatch):
    replay("txdot_aadt", "colfax")
    inner = arcgis._query

    def fake(url, params, session, max_records):
        if "drcog" in url:
            raise RuntimeError("drcog down")
        return inner(url, params, session, max_records)

    monkeypatch.setattr(arcgis, "_query", fake)
    result = pick("txdot_aadt", "CO", "denver").fetch(identity("colfax"))
    assert result.coverage_status.value == "partial"
    assert result.normalized_facts["traffic_counts"]
    assert "drcog down" in result.normalized_facts["source_warnings"][0]


# ── Traffic history ────────────────────────────────────────────────────────


def test_history_at_colfax(run_slot):
    result = run_slot("txdot_aadt_history")
    f = facts_of(result)
    assert_provenance(result, "Colorado Department of Transportation", "CDOT")
    assert "local streets" in f["coverage_note"].lower()
    nearest = f["stations"][0]
    assert nearest["on_road"] == "U.S. 40"
    assert nearest["latest_year"] == 2025 and nearest["latest_aadt"] == 31000
    years = {p["year"]: p["aadt"] for p in nearest["history"]}
    assert years[2020] == 27000 and years[1997] == 31783   # recorded series
    assert nearest["trend_summary"]["points"][-1] == {"year": 2025, "aadt": 31000}
    rep = f["representative_station"]
    assert rep["on_road"] == "I-25" and rep["latest_aadt"] == 268000
    assert rep["trend_summary"]["direction"] == "rising"
    assert f["station_count"] == len(f["stations"]) >= 5
    assert len({s["on_road_raw"] for s in f["stations"]}) == len(f["stations"])


def test_history_skips_years_with_no_count(run_slot):
    for station in facts_of(run_slot("txdot_aadt_history"))["stations"]:
        assert all(p["aadt"] > 0 for p in station["history"])


# ── Roads ──────────────────────────────────────────────────────────────────


def test_roads_at_colfax(run_slot):
    result = run_slot("txdot_roadway")
    f = facts_of(result)
    assert_provenance(result, "Highway Performance Monitoring", "FHWA HPMS")
    assert f["data_as_of"] == "2025"
    roads = {x["road_name"]: x for x in f["roadways"]}
    colfax = roads["U.S. 40"]
    assert colfax["functional_class"] == 3
    assert colfax["speed_limit"] == 30
    assert colfax["num_lanes"] == 5
    assert colfax["ownership_label"] == "State highway agency"
    assert colfax["access_control_label"] == "Partial access control"
    assert colfax["row_width"] == "manual_verification_required"
    assert colfax["row_width_ft"] is None
    assert roads["COURT PL"]["ownership_label"].startswith("City")
    assert len(roads) == len(f["roadways"])
    distances = [x["distance_mi"] for x in f["roadways"]]
    assert distances == sorted(distances)


def test_roads_keys_the_cards_read(run_slot):
    f = facts_of(run_slot("txdot_roadway"))
    assert f["engineering_details"]["read"]
    for row in f["roadways"]:
        for key in (
            "road_name", "distance_mi", "functional_class", "speed_limit",
            "num_lanes", "row_width", "row_width_ft", "lane_width_ft",
            "median_type_label", "median_width_ft", "access_control_label",
            "crossover_regulation", "turn_lane_presence",
            "turn_lane_presence_label", "stacking_length_ft",
            "engineering_detail_status", "manual_access_fields",
        ):
            assert key in row


def test_roads_drop_bad_values(run_slot):
    f = facts_of(run_slot("txdot_roadway", "colfax"))
    for row in f["roadways"]:
        assert row["speed_limit"] != 999
        assert row["speed_limit"] == "" or 5 <= row["speed_limit"] <= 85
        # a freeway cannot have "no access control"
        if row["functional_class"] in (1, 2) or "FWY" in row["road_name"]:
            assert row["access_control_code"] != 3
    freeway = next(x for x in f["roadways"] if x["road_name"] == "W 6TH AVENUE FWY")
    assert freeway["speed_limit"] == ""        # recorded as 999
    assert freeway["access_control_code"] in (None, 2)  # the recorded 3 is dropped


def test_roads_use_hpms_median_codes(run_slot):
    f = facts_of(run_slot("txdot_roadway", "colfax"))
    with_median = [x for x in f["roadways"] if x["median_type_code"]]
    assert with_median
    from app.regions.co.sources import txdot_roadway as mod
    assert mod.MEDIAN[1] == "No median" and mod.MEDIAN[3] == "Curbed median"
    for row in with_median:
        assert row["median_type_label"] == mod.MEDIAN[row["median_type_code"]]


# ── Road projects ──────────────────────────────────────────────────────────


def test_projects_at_colfax(run_slot):
    result = run_slot("txdot_projects")
    f = facts_of(result)
    assert_provenance(result, "Colorado Department of Transportation", "CDOT")
    assert f["count_within_radius"] == len(f["items"]) == 14
    items = {i["csj"]: i for i in f["items"]}
    colfax = items["SHE M320-118"]
    assert colfax["description"] == "COLFAX/BROADWAY - LINCOLN - CCD FY18 HSIP PKG5"
    assert colfax["cost_range"] == "Budget Pending"
    assert colfax["estimated_let_date"] == "2027-01-29"
    assert colfax["county"] == "Denver" and colfax["district_number"] == 1
    assert colfax["distance_mi"] == 0.15
    assert abs(colfax["lat"] - 39.74) < 0.01
    ten_year = items["SR13322.002"]
    assert ten_year["phase"] == "Design" and ten_year["program"] == "CDOT Ten-Year Plan"
    assert ten_year["official_lookup_links"][0]["url"].startswith("https://www.codot.gov")


def test_projects_never_invent_a_cost_or_an_award(run_slot):
    f = facts_of(run_slot("txdot_projects"))
    assert any(i["cost_range"] and "$" in i["cost_range"] for i in f["items"])
    for item in f["items"]:
        assert item["estimated_cost_usd"] is None
        assert "award" not in item
    assert "no award" in f["coverage_note"].lower()


def test_projects_list_each_project_once_nearest_first(run_slot):
    items = facts_of(run_slot("txdot_projects"))["items"]
    keys = [(i["program"], i["project_id"]) for i in items]
    assert len(keys) == len(set(keys))
    distances = [i["distance_mi"] for i in items]
    assert distances == sorted(distances)
    for item in items:
        assert item["lat"] is not None and item["lng"] is not None


# ── Bridges ────────────────────────────────────────────────────────────────


def test_bridges_at_colfax(run_slot):
    result = run_slot("txdot_bridges")
    f = facts_of(result)
    assert_provenance(result, "National Bridge Inventory", "NBI / CDOT")
    assert f["count_within_radius"] > len(f["items"]) - 10
    assert f["nearest_distance_mi"] == 0.42
    nearest = f["items"][0]
    assert nearest["bridge_id"] == "D-01-CC-150"
    assert nearest["carries"] == "13TH AVE" and nearest["intersects"] == "CHERRY CREEK"
    assert nearest["year_built"] == 1928
    assert nearest["worst_cond_rtg"] == 6
    assert nearest["condition_source"] == "CDOT"
    assert nearest["inventory_rating_us_tons"] == 35.7


def test_bridge_is_posted_only_from_the_weight_limit_layer(run_slot):
    f = facts_of(run_slot("txdot_bridges", "i70"))
    posted = [b for b in f["items"] if b["posting_status"] == "posted"]
    assert {b["bridge_id"] for b in posted} == {"E-17-FX", "E-17-GC", "E-17-GD", "E-17-HZ"}
    assert all(b["load_restriction"] == "Y" for b in posted)
    assert f["posted_count"] == 4
    # everything else is unknown: never "N", never "not posted"
    others = [b for b in f["items"] if b["posting_status"] != "posted"]
    assert others
    assert all(b["posting_status"] == "unknown" and b["load_restriction"] == "unknown"
               for b in others)
    assert all(b["posted_weight_us_tons"] is None for b in f["items"])
    assert "not 'not posted'" in f["posting_note"]


def test_nbi_posting_evaluation_is_not_a_posting(run_slot):
    # every recorded NBI structure carries a posting evaluation of "5"; none
    # of them is therefore posted
    f = facts_of(run_slot("txdot_bridges", "colfax"))
    near_colfax = [b for b in f["items"] if b["distance_mi"] < 1]
    assert near_colfax
    assert all(b["posting_status"] == "unknown" for b in near_colfax)


def test_bridges_without_the_weight_layer_say_so(replay, monkeypatch):
    replay("txdot_bridges", "colfax")
    inner = arcgis._query

    def fake(url, params, session, max_records):
        if "Bridge_Weight_Limit" in url:
            raise RuntimeError("weight layer down")
        return inner(url, params, session, max_records)

    monkeypatch.setattr(arcgis, "_query", fake)
    result = pick("txdot_bridges", "CO", "denver").fetch(identity("colfax"))
    f = result.normalized_facts
    assert result.coverage_status.value == "partial"
    assert f["posting_data"] == "unavailable"
    assert all(b["load_restriction"] == "unknown" for b in f["items"])


# ── The cards built from these results ────────────────────────────────────


@pytest.fixture
def colfax_results(run_slot):
    return {slot: run_slot(slot) for slot in SLOTS}


def test_traffic_card(colfax_results):
    card = card_for(colfax_results, "traffic")
    text = card_text(card)
    assert "31,000 vehicles" in text and "u.s. 40" in text
    assert "u.s. 40" in text and "2023" in text
    assert "rising" in text and "i-25" in text          # history merged in
    assert not any(w in text for w in TEXAS_WORDS)


def test_roads_card(colfax_results):
    card = card_for(colfax_results, "txdot_roadway")
    text = card_text(card)
    assert "u.s. 40" in text and "30 mph" in text and "partial access control" in text
    assert "not published" in text      # right of way
    assert not any(w in text for w in TEXAS_WORDS)


def test_projects_card_shows_the_published_cost_range_only(colfax_results):
    card = card_for(colfax_results, "txdot_projects")
    text = card_text(card)
    assert "colfax/broadway - lincoln" in text and "budget pending" in text
    assert "$1 to $1,000,000" in text
    assert "not published by cdot" in text      # no award data
    assert not any(w in text for w in TEXAS_WORDS)


def test_bridges_card_names_the_nearest_bridge(colfax_results):
    card = card_for(colfax_results, "txdot_bridges")
    text = card_text(card)
    assert "13th ave" in text and "cherry creek" in text and "6 of 9" in text
    assert not any(w in text for w in TEXAS_WORDS)


def test_cards_say_posting_is_unknown_and_name_no_texas_agency(colfax_results):
    bridges = card_text(card_for(colfax_results, "txdot_bridges"))
    assert "none posted" not in bridges
    traffic = card_for(colfax_results, "traffic")
    assert not any("txdot" in (l.get("label") or "").lower() for l in traffic["links"])


# ── The transportation summary reads these facts ──────────────────────────


def test_extended_reads_the_colorado_facts(colfax_results):
    upstream = {
        slot: orchestrator._adapter_result_payload(res, refresh_status="fetched")
        for slot, res in colfax_results.items()
    }
    result = txdot_extended.fetch(
        identity("colfax"), config={"upstream_results": upstream})
    f = result.normalized_facts
    assert all(v == "reused" for v in f["input_status"].values())
    assert f["traffic_exposure"]["top_aadt"]["aadt"] >= 31000
    assert f["traffic_exposure"]["aadt_band"] in {"high", "very_high"}
    roadway = f["roadway_context"]
    assert roadway["primary_roadway"]["functional_class_label"]
    assert roadway["highest_speed_mph"] == 30 and roadway["largest_lane_count"] >= 5
    pipeline = f["project_pipeline"]
    assert pipeline["count_within_radius"] == 14
    assert pipeline["largest_project"] is not None
    assert any(p["csj"] == "SHE M320-118" for p in pipeline["near_term_projects"])
    bridges = f["bridge_constraints"]
    assert bridges["count_within_radius"] > 0
    assert bridges["nearest_distance_mi"] == 0.42
