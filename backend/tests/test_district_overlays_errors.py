"""district_overlays reports failed layers honestly: an ArcGIS error body sent
with HTTP 200 is an error, and any failed layer makes the result partial."""

from app.services.site_check.adapters import district_overlays
from app.services.site_check.identity import PropertyIdentity


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Session:
    """Answers each URL with a fixed body; raises for URLs mapped to None."""

    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, url, params=None, timeout=None):
        body = self.bodies.get(url, {"features": []})
        if body is None:
            raise ConnectionError("unreachable")
        return _Response(body)


def _fetch(session):
    identity = PropertyIdentity(
        address="804 Pecan St, Bastrop, TX 78602", lat=30.11, lng=-97.32, county="bastrop",
    )
    return district_overlays.fetch(identity, session=session)


def _county_urls():
    return [urls[0] for _, urls, _ in district_overlays._LAYERS_BY_COUNTY["bastrop"]]


def test_all_layers_answering_is_covered():
    result = _fetch(_Session({}))
    assert result.coverage_status.value == "covered"
    assert result.normalized_facts["layer_errors"] == []


def test_arcgis_error_body_counts_as_an_error():
    first = _county_urls()[0]
    result = _fetch(_Session({first: {"error": {"code": 400, "message": "Invalid query"}}}))
    assert result.coverage_status.value == "partial"
    errors = result.normalized_facts["layer_errors"]
    assert len(errors) == 1 and "Invalid query" in errors[0]["error"]


def test_every_layer_failing_is_not_covered():
    bodies = {url: None for url in _county_urls()}
    bodies.update({urls[0]: None for _, urls, _ in district_overlays._TEXAS_STATEWIDE_LAYERS})
    result = _fetch(_Session(bodies))
    assert result.coverage_status.value == "partial"
    assert result.errors
    assert len(result.normalized_facts["layer_errors"]) == len(district_overlays._LAYERS_BY_COUNTY["bastrop"]) + 2
