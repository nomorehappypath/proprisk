"""A county that exists only as a file (no special code) behaves safely:
no city hit means "not confirmed" unless the file says otherwise, and its
subdivision screen works without a clerk section and never claims a plat was
recorded."""

from app.services.site_check.adapters import jurisdiction_resolver, plats_intel
from app.services.site_check.identity import PropertyIdentity

_CITY = "https://example.test/city/query"
_ETJ = "https://example.test/etj/query"
_SUBDIV = "https://example.test/subdivisions/FeatureServer/3"


class _Response:
    def __init__(self, body):
        self._body = body
        self.headers = {"content-type": "application/json"}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Session:
    def __init__(self, bodies=None):
        self.bodies = bodies or {}

    def get(self, url, params=None, timeout=None, headers=None):
        return _Response(self.bodies.get(url, {"features": []}))


def _jurisdiction_cfg(**extra):
    return {
        "source": "Test County GIS",
        "source_url": "https://example.test/",
        "city_limits": [{"url": _CITY, "name_field": "CITY"}],
        "etj": [{"url": _ETJ, "name_field": "CITY"}],
        **extra,
    }


def _identity():
    return PropertyIdentity(address="1 Main St", lat=29.7, lng=-98.1, county="testcounty")


def test_no_hit_is_not_confirmed_by_default(monkeypatch):
    monkeypatch.setitem(jurisdiction_resolver._COUNTY_JURISDICTION_GIS, "testcounty", _jurisdiction_cfg())
    verdict = jurisdiction_resolver.resolve_jurisdiction(_identity(), session=_Session())
    assert verdict["jurisdiction_kind"] != "unincorporated_county"


def test_no_hit_is_unincorporated_only_when_the_file_says_so(monkeypatch):
    monkeypatch.setitem(
        jurisdiction_resolver._COUNTY_JURISDICTION_GIS, "testcounty",
        _jurisdiction_cfg(unincorporated_on_no_hit=True),
    )
    verdict = jurisdiction_resolver.resolve_jurisdiction(_identity(), session=_Session())
    assert verdict["jurisdiction_kind"] == "unincorporated_county"


def _screen(monkeypatch):
    monkeypatch.setitem(plats_intel._COUNTY_SUBDIVISION_SCREENS, "testcounty", {
        "source_label": "Test County subdivisions",
        "layer_url": _SUBDIV,
        "query_url": f"{_SUBDIV}/query",
        "name_field": "SUB_NAME",
        "code_field": "SUB_ID",
    })


def _parcel_config():
    ring = [[-98.11, 29.69], [-98.09, 29.69], [-98.09, 29.71], [-98.11, 29.71], [-98.11, 29.69]]
    return {"upstream_results": {"tcad_parcel": {"normalized_facts": {
        "parcel_polygon": {"type": "Polygon", "coordinates": [ring]},
    }}}}


def _run(session, config):
    return plats_intel._fetch_county_subdivision_screen(
        session, _identity(), county="testcounty", config=config,
        discovery={}, active_context=None,
    )


def test_subdivision_nearby_is_never_a_recorded_plat(monkeypatch):
    _screen(monkeypatch)
    session = _Session({f"{_SUBDIV}/query": {"features": [
        {"attributes": {"SUB_NAME": "Oak Hills", "SUB_ID": "OH-1"}},
    ]}})
    result = _run(session, _parcel_config())
    facts = result.normalized_facts
    assert facts["all_plats"][0]["name"] == "Oak Hills"
    assert facts["all_plats"][0]["subdivision_code"] == "OH-1"
    assert facts["recorded_count"] == 0 and facts["approved_count"] == 0
    assert facts["status"] == "testcounty_subdivision_gis_match_not_recorded"
    assert facts["portal_url"] is None  # no clerk section: no clerk steps


def test_no_parcel_polygon_and_no_clerk_does_not_crash(monkeypatch):
    _screen(monkeypatch)
    result = _run(_Session(), {})
    assert result.normalized_facts["testcounty_subdivision_gis_status"] == "no_parcel_polygon"
    assert result.coverage_status.value == "not_supported_in_jurisdiction"
