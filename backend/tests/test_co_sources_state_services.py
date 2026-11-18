"""Colorado utilities, state environmental records, schools and tax sources
(spec 2.6). Real responses recorded 2026-11-18 are replayed offline; no test
touches the network."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.regions import arcgis
from app.regions.co.sources import (
    schools_intel,
    state_environmental_records,
    tax_intel,
    twdb_ccn,
)
from app.services.site_check import cards, orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "region_sources" / "co"
TEXAS_WORDS = ("TWDB", "TCEQ", "TEA ", "Texas", "HB 1378", "Comptroller", "PUCT", "txschools")


def _fixture(slot, name):
    return json.loads((FIXTURES / slot / f"{name}.json").read_text())


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    """Replays the recorded Urban Institute response; any other call fails."""

    def __init__(self, ccd):
        self.ccd = ccd

    def get(self, url, params=None, timeout=None):
        assert "educationdata.urban.org" in url, f"unexpected network call {url}"
        return _Response(self.ccd[params["state_leaid"]])


@pytest.fixture
def replay(monkeypatch):
    def load(slot, name):
        data = _fixture(slot, name)

        def fake_query(url, params, session, max_records):
            key = url + ("|near" if "distance" in params and "Water_System" in url else "")
            assert key in data["layers"], f"unrecorded layer {key}"
            return data["layers"][key]

        monkeypatch.setattr(arcgis, "_query", fake_query)
        identity = SimpleNamespace(
            lat=data["lat"], lng=data["lng"], county=data["county"], state="CO",
            address="201 W Colfax Ave, Denver, CO", parcel_geometry=None)
        return identity, _Session(data["ccd"])
    return load


def _card(slot, result):
    payload = orchestrator._adapter_result_payload(result, refresh_status="fetched")
    run = SimpleNamespace(
        id="r", status="completed", state="CO", sources_failed=[], derived={},
        adapter_results={slot: payload}, finished_at=None, lat=None, lng=None)
    for group in cards.build_cards(run, None).get("groups", []):
        for card in group["cards"]:
            if card["card_key"] == slot:
                return card
    raise AssertionError(f"no {slot} card")


def _no_texas(card):
    text = json.dumps(card)
    for word in TEXAS_WORDS:
        assert word not in text, word


# -- twdb_ccn -----------------------------------------------------------------

READ_BY_UTILITY_CARD = (
    "water_provider", "wastewater_provider", "water_providers", "wastewater_providers",
    "water_provider_legal_ccn", "water_provider_operating", "wastewater_nearest",
    "sewer_district_candidates", "availability_request_text", "service_territories",
    "providers_disagree", "municipal_wastewater_candidate", "status", "advisory",
)
STANDARD = ("provider_name", "provider_short", "source_url", "data_as_of")


def test_denver_water_serves_201_w_colfax(replay):
    identity, session = replay("twdb_ccn", "colfax")
    facts = twdb_ccn.fetch(identity, session=session).normalized_facts
    for key in READ_BY_UTILITY_CARD + STANDARD:
        assert key in facts, key
    assert facts["water_provider"] == "DENVER WATER BOARD"
    assert facts["water_confirmed_by_denver_water"] is True
    assert facts["water_provider_legal_ccn"] == []
    assert facts["providers_disagree"] is False
    assert facts["wastewater_provider"] == "City & County of Denver (member of Metro Water Recovery)"
    assert facts["parts"] == {}
    assert facts["status"] == "clear"
    assert "no certificate" in facts["advisory"].lower()
    assert facts["data_as_of"] == "2022–2026"


def test_territories_say_contains_site_and_never_claim_a_ccn(replay):
    identity, session = replay("twdb_ccn", "colfax")
    facts = twdb_ccn.fetch(identity, session=session).normalized_facts
    areas = facts["service_territories"]
    assert {(t["service"], t["authority"], t["contains_site"]) for t in areas} == {
        ("water", "service_area", True), ("wastewater", "service_area", True)}
    assert all(t["geometry"]["type"] in {"Polygon", "MultiPolygon"} for t in areas)
    assert all(t["authority"] != "legal_ccn" for t in areas)


def test_denver_sewer_mains_shape(replay):
    identity, session = replay("twdb_ccn", "colfax")
    mains = twdb_ccn.fetch(identity, session=session).normalized_facts["sewer_mains"]
    assert mains and all(m["provider"] == "Denver Wastewater Management" for m in mains)
    first = mains[0]
    assert {"diameter_in", "material", "owner", "distance_m", "geometry", "unit_type"} <= set(first)
    assert first["geometry"]["type"] in {"LineString", "MultiLineString"}
    assert [m["distance_m"] for m in mains] == sorted(m["distance_m"] for m in mains)
    assert all(m["distance_m"] <= 150 for m in mains)


def test_metro_districts_at_union_station(replay):
    identity, session = replay("twdb_ccn", "union_station")
    facts = twdb_ccn.fetch(identity, session=session).normalized_facts
    assert [d["name"] for d in facts["sewer_district_candidates"]] == [
        "DUS Metropolitan District No. 2", "DUS Metropolitan District No. 3"]
    assert {d["kind"] for d in facts["sewer_district_candidates"]} == {"Metropolitan District"}
    assert any(t["authority"] == "dola_special_district" for t in facts["service_territories"])


def test_sewer_is_not_connected_outside_metro_water_recovery(replay):
    identity, session = replay("twdb_ccn", "fort_collins")
    facts = twdb_ccn.fetch(identity, session=session).normalized_facts
    assert facts["parts"]["sewer"]["coverage"] == "not_connected"
    assert facts["wastewater_provider"] is None and facts["wastewater_providers"] == []
    assert "sewer_mains" not in facts  # Denver's layer only answers for Denver sites
    assert facts["water_providers"]  # water still answered


def test_aurora_is_not_denver_water(replay):
    identity, session = replay("twdb_ccn", "aurora")
    facts = twdb_ccn.fetch(identity, session=session).normalized_facts
    assert "AURORA" in facts["water_provider"].upper()
    assert facts["water_confirmed_by_denver_water"] is False
    assert facts["wastewater_provider"].startswith("City of Aurora")


def test_nearby_water_area_is_flagged_as_not_containing_the_site(replay, monkeypatch):
    identity, _ = replay("twdb_ccn", "colfax")
    colfax = _fixture("twdb_ccn", "colfax")["layers"]
    water_feature = colfax[twdb_ccn.EPA_WATER][0]

    def fake_query(url, params, session, max_records):
        if url == twdb_ccn.EPA_WATER:
            return [water_feature] if "distance" in params else []
        return []

    monkeypatch.setattr(arcgis, "_query", fake_query)
    identity.county = "elbert"
    facts = twdb_ccn.fetch(identity).normalized_facts
    nearby = [t for t in facts["service_territories"] if t["authority"] == "nearest_ccn"]
    assert nearby and nearby[0]["contains_site"] is False
    assert facts["water_providers"] == []  # nearby is not "serves the site"
    assert facts["nearby_water_providers"] == ["DENVER WATER BOARD"]


def test_a_failed_layer_stays_unknown_not_empty(monkeypatch):
    def broken(url, params, session, max_records):
        raise RuntimeError("provider down")

    monkeypatch.setattr(arcgis, "_query", broken)
    result = twdb_ccn.fetch(SimpleNamespace(lat=39.7, lng=-104.9, county="denver", address=None))
    assert result.normalized_facts["status"] == "unknown"
    assert result.normalized_facts["parts"]["sewer"]["coverage"] == "not_connected"
    assert result.errors


def test_utilities_card_shows_colorado_and_no_texas_agency(replay):
    identity, session = replay("twdb_ccn", "colfax")
    card = _card("twdb_ccn", twdb_ccn.fetch(identity, session=session))
    assert card["title"] == "Utilities (EPA / MWR / DOLA)"
    text = json.dumps(card)
    assert "Denver Water Board" in text
    _no_texas(card)


# -- state_environmental_records ---------------------------------------------

READ_BY_ENV_CARD = (
    "pst_records", "tceq_screen", "tceq_address_registry", "registry_street_queried",
    "manual_records_links", "search_radius_m", "broad_search_radius_m", "source_statuses",
    "source_cooldowns", "unavailable_sources", "advisory", "status",
)


def test_colfax_counts_only_open_releases_and_names_real_records(replay):
    identity, session = replay("state_environmental_records", "colfax")
    facts = state_environmental_records.fetch(identity, session=session).normalized_facts
    for key in READ_BY_ENV_CARD + STANDARD:
        assert key in facts, key
    assert facts["pst_records"] == []          # 19 releases nearby, every one closed
    assert facts["closed_release_count"] == 19
    screen = facts["tceq_screen"]
    assert screen["superfund"]["count"] == 1
    assert screen["superfund"]["records"][0]["facility_name"] == "Denver Radium/DRS"
    assert screen["superfund"]["records"][0]["program"] == "NPL ACTIVE"
    assert screen["superfund"]["records"][0]["tceq_rn"] == "COD980716955"
    assert screen["corrective_action"]["records"][0]["facility_name"].startswith("Rocky Mtn. Orthodontics")
    assert screen["groundwater_msd"]["count"] == 2
    assert screen["brownfield"]["count"] == 0 and screen["landfill"]["count"] == 0
    assert "dry_cleaner" not in screen
    assert facts["tceq_address_registry"] == {}
    assert facts["parts"]["enforcement"]["coverage"] == "not_connected"
    assert facts["parts"]["dry_cleaner"]["coverage"] == "not_connected"
    assert "No open storage-tank releases" in facts["advisory"]
    assert "dry-cleaner records are not connected" in facts["advisory"]
    assert all(v == "queried" for v in facts["source_statuses"].values())


def test_open_tank_release_in_globeville(replay):
    identity, session = replay("state_environmental_records", "globeville")
    facts = state_environmental_records.fetch(identity, session=session).normalized_facts
    assert len(facts["pst_records"]) == 1
    record = facts["pst_records"][0]
    assert record["facility_name"] == "Globeville Redevelopment"
    assert record["lpst_status"] == "Site Characterization Requested"
    assert record["release_number"] == "REL-017229"
    assert record["pst_id"] and record["address"] == "4995 Washington St"
    assert facts["tceq_screen"]["landfill"]["count"] == 7   # compost and recycling sites
    assert facts["tceq_screen"]["superfund"]["count"] == 2


def test_rcra_handlers_and_pfas_are_not_queried(replay, monkeypatch):
    seen = []
    identity, session = replay("state_environmental_records", "colfax")
    inner = arcgis._query

    def spy(url, params, session, max_records):
        seen.append(url)
        return inner(url, params, session, max_records)

    monkeypatch.setattr(arcgis, "_query", spy)
    state_environmental_records.fetch(identity, session=session)
    assert not any(u.endswith("/RCRA/FeatureServer/0") or "PFAS" in u for u in seen)


def test_environmental_layer_failure_is_not_an_all_clear(monkeypatch):
    def flaky(url, params, session, max_records):
        if "Superfund" in url:
            raise RuntimeError("temporary provider timeout")
        return []

    monkeypatch.setattr(arcgis, "_query", flaky)
    facts = state_environmental_records.fetch(
        SimpleNamespace(lat=39.7, lng=-104.9)).normalized_facts
    assert facts["tceq_screen"]["superfund"]["error"]
    assert facts["source_statuses"]["tceq_superfund"] == "temporary_timeout"
    assert facts["source_statuses"]["tceq_pst"] == "queried"
    assert facts["unavailable_sources"] == ["Superfund / National Priorities List site"]


def test_environmental_card_shows_colorado_records(replay):
    identity, session = replay("state_environmental_records", "globeville")
    result = state_environmental_records.fetch(identity, session=session)
    merged = dict(result.normalized_facts, state="CO", echo_records=[])
    merged["source_statuses"] = dict(merged["source_statuses"], epa_echo="queried")
    content = cards._format_environmental(merged)
    rows = {r["label"]: r["value"] for r in content.facts}
    tanks = rows["Fuel & storage tank records"]
    assert "1 open release" in tanks and "11 closed" in tanks
    assert "superfund" in rows["State cleanup & waste records"].lower()
    text = json.dumps([content.takeaway, content.facts])
    _no_texas({"t": text})


# -- schools_intel -----------------------------------------------------------

READ_BY_SCHOOLS_CARD = (
    "status", "search_radius_mi", "schools_count", "schools", "nearest_by_band",
    "report_card_lookup_url", "advisory",
)


def test_schools_near_a_denver_home(replay):
    identity, session = replay("schools_intel", "residential")
    facts = schools_intel.fetch(identity, session=session).normalized_facts
    for key in READ_BY_SCHOOLS_CARD + STANDARD:
        assert key in facts, key
    assert facts["school_district"]["name"] == "Denver County School District 1"
    assert facts["school_district"]["geoid"] == "0803360"
    school = facts["schools"][0]
    for key in ("name", "district", "grade_range", "band", "address", "city", "phone",
                "enrollment", "school_number", "website", "distance_mi", "lat", "lng"):
        assert key in school, key
    assert school["name"] == "Bromwell Elementary School"
    assert school["district"] == "Denver County 1"
    assert school["grade_range"] == "PK-5" and school["band"] == "elementary"
    assert school["enrollment"] == 329          # NCES CCD 2024-25
    assert school["school_number"] == "0880-1056"
    assert school["distance_mi"] == pytest.approx(0.47, abs=0.02)
    assert facts["nearest_by_band"]["elementary"]["name"] == "Bromwell Elementary School"
    assert facts["nearest_by_band"]["high"]["band"] == "high"
    assert facts["schools_count"] == 59 and len(facts["schools"]) == 10
    assert facts["report_card_lookup_url"].startswith("https://www.cde.state.co.us")
    assert "score" not in json.dumps(facts).lower().replace("not scores", "")
    distances = [s["distance_mi"] for s in facts["schools"]]
    assert distances == sorted(distances)


def test_schools_without_enrollment_service_still_answer(replay, monkeypatch):
    identity, _ = replay("schools_intel", "residential")

    class Down:
        def get(self, *a, **k):
            raise RuntimeError("offline")

    facts = schools_intel.fetch(identity, session=Down()).normalized_facts
    assert facts["schools"][0]["enrollment"] is None
    assert facts["schools"][0]["band"] == "elementary"   # from CDE grade codes
    assert facts["enrollment_year"] is None



def test_schools_card_shows_cde_and_no_texas_agency(replay):
    identity, session = replay("schools_intel", "residential")
    card = _card("schools_intel", schools_intel.fetch(identity, session=session))
    assert card["title"] == "Schools (CDE)"
    assert "Bromwell Elementary School" in json.dumps(card)
    assert "cde.state.co.us" in json.dumps(card)
    _no_texas(card)


# -- tax_intel ---------------------------------------------------------------


def test_denver_levies_at_201_w_colfax(replay):
    identity, session = replay("tax_intel", "colfax")
    facts = tax_intel.fetch(identity, session=session).normalized_facts
    for key in ("levies", "total_mills", "status", "fiscal_year", "advisory") + STANDARD:
        assert key in facts, key
    by_name = {x["name"]: x for x in facts["levies"]}
    school = by_name["Denver County 1 School District"]
    assert (school["total_mills"], school["bond_mills"], school["tax_year"]) == (52.274, 9.339, 2025)
    assert by_name["Denver, City And County of"]["total_mills"] == 26.328
    assert facts["total_mills"] == pytest.approx(78.602)
    assert facts["data_as_of"] == "2025" and facts["fiscal_year"] == "2025"
    for key in ("results", "districts_matched", "comptroller_portal_url"):
        assert key not in facts                  # no Texas debt-report keys
    assert "not the amount of debt" in facts["advisory"]
    assert set(facts["levies"][0]) >= {"name", "total_mills", "bond_mills", "tax_year"}


def test_metro_district_levies_are_listed(replay):
    identity, session = replay("tax_intel", "union_station")
    levies = tax_intel.fetch(identity, session=session).normalized_facts["levies"]
    metro = {x["name"]: x["total_mills"] for x in levies if "Metropolitan" in x["type"]}
    assert metro == {"DUS Metropolitan District No. 2": 8.0, "DUS Metropolitan District No. 3": 5.0}


def test_tax_failure_is_unknown_not_zero(monkeypatch):
    def broken(url, params, session, max_records):
        raise RuntimeError("down")

    monkeypatch.setattr(arcgis, "_query", broken)
    facts = tax_intel.fetch(SimpleNamespace(lat=39.7, lng=-104.9)).normalized_facts
    assert facts["status"] == "unknown" and "total_mills" not in facts


def test_tax_card_names_dola_and_no_texas_agency(replay):
    identity, session = replay("tax_intel", "colfax")
    card = _card("tax_intel", tax_intel.fetch(identity, session=session))
    assert card["title"] == "Tax / Levies (DOLA)"
    _no_texas(card)


def test_every_module_declares_the_contract():
    for module, slot in ((twdb_ccn, "twdb_ccn"), (schools_intel, "schools_intel"),
                         (tax_intel, "tax_intel"),
                         (state_environmental_records, "state_environmental_records")):
        assert module.SOURCE_ID == slot and module.VERSION == 1
        assert module.PROVIDER_NAME and module.PROVIDER_SHORT
        assert callable(module.fetch)
