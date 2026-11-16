"""Google Geocoding final-fallback tier (Admin 2026-08-01).

Ladder contract: Nominatim (free, primary) → ArcGIS (fallback) →
Google (last resort, shared platform key). All offline — the Google
success case replays the sanitized body recorded from ONE live call on
2026-08-01 ("1100 Congress Ave, Austin, TX 78701" → 30.274703,
-97.740345, Travis County), stored at
tests/fixtures/site_check/arcgis_geocode/google_geocode_1100_congress.json.

Covered here:
  - fixture replay: `_try_google` parses the real response shape
    (geometry.location + address_components route/county extraction);
  - ladder: nominatim success → google never called; nominatim+arcgis
    miss → google resolves (source="google"); all three miss →
    geocode_failed; ArcGIS structural error still lets google try,
    and the structural error surfaces only when google misses too;
  - the 2026-07-21 road-validation hardening applies to google
    candidates (FM 971 request must not accept an FM 1971 candidate);
  - no key / disabled gate → tier skipped with zero HTTP calls.

Provider metering is asserted separately, in
tests/test_site_check_provider_metering.py: §8.6 charges every actual
Google REQUEST (success, ZERO_RESULTS and error alike) rather than a
resolution, so the tiers that were called and missed keep their own
rows, and a disabled gate or missing key records nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from app.config.settings import get_settings
from app.core import secret_store
from app.services.site_check import usage
from app.services.site_check.adapters import arcgis_geocode as ag
from app.services.site_check.adapters._base import PropertyIdentity

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "site_check" / "arcgis_geocode"
    / "google_geocode_1100_congress.json"
)

_CAPITOL_ADDRESS = "1100 Congress Ave, Austin, TX 78701"


# ── Offline HTTP stubs ────────────────────────────────────────────────


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubSession(requests.Session):
    """Serves canned JSON bodies per URL; any un-routed request is a
    test failure (these tests are strictly offline)."""

    def __init__(self, routes: dict):
        super().__init__()
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url in self.routes:
            return _StubResponse(self.routes[url])
        raise AssertionError(f"unexpected HTTP GET in offline test: {url}")


@pytest.fixture
def google_key(monkeypatch):
    """Enable the google tier with a fake key (never the real one)."""
    settings = get_settings()
    secret_store.set_secret("google_maps", "test-key-not-real")
    monkeypatch.setattr(settings, "google_geocode_enabled", True)
    return settings


def _google_body(*, route_long, route_short, county, lat, lng,
                 formatted):
    """Minimal Google Geocoding response in the documented shape."""
    return {
        "status": "OK",
        "results": [{
            "formatted_address": formatted,
            "geometry": {"location": {"lat": lat, "lng": lng}},
            "address_components": [
                {"long_name": route_long, "short_name": route_short,
                 "types": ["route"]},
                {"long_name": county, "short_name": county,
                 "types": ["administrative_area_level_2", "political"]},
                {"long_name": "Texas", "short_name": "TX",
                 "types": ["administrative_area_level_1", "political"]},
            ],
        }],
    }


# ── _try_google: fixture replay + tier gates ─────────────────────────


def test_google_replay_fixture_resolves_capitol(google_key):
    """The sanitized live-recorded body parses to the Capitol anchor —
    lat/lng from geometry.location, county from the
    administrative_area_level_2 component (normalized)."""
    sess = _StubSession({ag._GOOGLE_GEOCODE: json.loads(_FIXTURE.read_text())})
    coords = ag._try_google(sess, _CAPITOL_ADDRESS, county="travis")
    assert coords is not None
    lat, lng, county = coords
    assert lat == pytest.approx(30.2747, abs=0.01)
    assert lng == pytest.approx(-97.7403, abs=0.01)
    assert county == "travis"
    assert sess.calls == [ag._GOOGLE_GEOCODE]


def test_google_tier_skipped_cleanly_without_key(monkeypatch):
    secret_store.delete_secret("google_maps")
    sess = _StubSession({})  # any HTTP call would raise
    assert ag._try_google(sess, _CAPITOL_ADDRESS) is None
    assert sess.calls == []


def test_google_tier_skipped_cleanly_when_disabled(google_key, monkeypatch):
    monkeypatch.setattr(google_key, "google_geocode_enabled", False)
    sess = _StubSession({})
    assert ag._try_google(sess, _CAPITOL_ADDRESS) is None
    assert sess.calls == []


def test_google_non_ok_status_returns_none(google_key):
    sess = _StubSession({ag._GOOGLE_GEOCODE: {
        "status": "REQUEST_DENIED",
        "error_message": "The provided API key is invalid.",
    }})
    assert ag._try_google(sess, _CAPITOL_ADDRESS) is None


def test_google_zero_results_returns_none(google_key):
    sess = _StubSession({ag._GOOGLE_GEOCODE: {
        "status": "ZERO_RESULTS", "results": [],
    }})
    assert ag._try_google(sess, _CAPITOL_ADDRESS) is None


# ── Road-number validation on google candidates (2026-07-21 rule) ────


def test_google_road_validation_rejects_wrong_road(google_key):
    """A request for FM 971 must NOT accept an FM 1971 candidate —
    the exact Panola-County fuzzy-match class the 2026-07-21
    hardening rejects on the other tiers."""
    sess = _StubSession({ag._GOOGLE_GEOCODE: _google_body(
        route_long="Farm to Market Road 1971", route_short="FM 1971",
        county="Panola County", lat=32.1543, lng=-94.3391,
        formatted="971 FM 1971, Gary, TX 75643, USA",
    )})
    assert ag._try_google(
        sess, "1281 FM 971, Georgetown, TX", county="williamson",
    ) is None


def test_google_road_validation_accepts_matching_road(google_key):
    sess = _StubSession({ag._GOOGLE_GEOCODE: _google_body(
        route_long="Farm to Market Road 971", route_short="FM 971",
        county="Williamson County", lat=30.6698, lng=-97.6612,
        formatted="1281 FM 971, Georgetown, TX 78626, USA",
    )})
    coords = ag._try_google(
        sess, "1281 FM 971, Georgetown, TX", county="williamson",
    )
    assert coords is not None
    assert coords[2] == "williamson"


# ── The ladder through fetch() ───────────────────────────────────────


_IDENTITY = PropertyIdentity(address=_CAPITOL_ADDRESS, county="travis")
_CAPITOL_COORDS = (30.274703, -97.740345, "travis")


def _wire(monkeypatch, *, nominatim, arcgis, google):
    """Stub the three tier functions; each records whether it ran."""
    ran = {"nominatim": False, "arcgis": False, "google": False}

    def fake_nominatim(sess, address, *, county=None):
        ran["nominatim"] = True
        return nominatim

    def fake_arcgis(sess, address, *, county=None):
        ran["arcgis"] = True
        return arcgis

    def fake_google(sess, address, *, county=None):
        ran["google"] = True
        return google

    monkeypatch.setattr(ag, "_try_nominatim", fake_nominatim)
    monkeypatch.setattr(ag, "_try_arcgis", fake_arcgis)
    monkeypatch.setattr(ag, "_try_google", fake_google)
    return ran


def test_ladder_nominatim_success_never_calls_google(monkeypatch):
    ran = _wire(
        monkeypatch,
        nominatim=_CAPITOL_COORDS, arcgis=(None, None), google=None,
    )
    result = ag.fetch(_IDENTITY, session=_StubSession({}))
    assert result.normalized_facts["source"] == "nominatim"
    assert ran == {"nominatim": True, "arcgis": False, "google": False}


def test_ladder_nominatim_and_arcgis_miss_google_resolves(monkeypatch):
    ran = _wire(
        monkeypatch,
        nominatim=None, arcgis=(None, None), google=_CAPITOL_COORDS,
    )
    result = ag.fetch(_IDENTITY, session=_StubSession({}))
    assert not result.errors
    facts = result.normalized_facts
    assert facts["source"] == "google"
    assert facts["lat"] == pytest.approx(30.274703)
    assert facts["lng"] == pytest.approx(-97.740345)
    assert facts["county"] == "travis"
    assert ran == {"nominatim": True, "arcgis": True, "google": True}


def test_ladder_all_three_miss_is_geocode_failed(monkeypatch):
    ran = _wire(
        monkeypatch, nominatim=None, arcgis=(None, None), google=None,
    )
    result = ag.fetch(_IDENTITY, session=_StubSession({}))
    assert result.errors  # → orchestrator's geocode_failed path
    assert result.normalized_facts == {}
    assert ran == {"nominatim": True, "arcgis": True, "google": True}


def test_ladder_arcgis_structural_error_google_still_tries(monkeypatch):
    """A token/scope error on ArcGIS no longer ends the ladder — the
    validated google last resort runs first."""
    _wire(
        monkeypatch,
        nominatim=None,
        arcgis=(None, "arcgis token error code=403 message='bad scope'"),
        google=_CAPITOL_COORDS,
    )
    result = ag.fetch(_IDENTITY, session=_StubSession({}))
    assert not result.errors
    assert result.normalized_facts["source"] == "google"


def test_ladder_arcgis_structural_error_surfaces_when_google_misses(
    monkeypatch,
):
    err = "arcgis token error code=403 message='bad scope'"
    _wire(monkeypatch, nominatim=None, arcgis=(None, err), google=None)
    result = ag.fetch(_IDENTITY, session=_StubSession({}))
    assert result.errors == [err]  # loud config-error surfacing kept


def test_ladder_intersection_path_google_is_last(monkeypatch):
    """Intersection addresses go ArcGIS → Nominatim → Google."""
    ran = _wire(
        monkeypatch,
        nominatim=None, arcgis=(None, None), google=_CAPITOL_COORDS,
    )
    identity = PropertyIdentity(
        address="CR 130 and Chandler Rd, Hutto, TX", county="williamson",
    )
    result = ag.fetch(identity, session=_StubSession({}))
    assert result.normalized_facts["source"] == "google"
    assert ran == {"nominatim": True, "arcgis": True, "google": True}


# ── Settings-driven usage rate ───────────────────────────────────────


def test_google_geocode_rate_is_settings_driven(monkeypatch):
    """No hardcoded price: the google/geocode rate card derives from
    google_geocode_cost_micro_per_call, with NO free-tier discount
    (shared platform key — this DB can't see the key's true monthly
    position). The settings value is integer microdollars and is used
    verbatim — money never passes through a float."""
    monkeypatch.setattr(
        get_settings(), "google_geocode_cost_micro_per_call", 7000,
    )
    rate = usage._rate_for("google", "geocode")
    assert rate is not None
    assert rate.free_units_per_month == 0
    assert rate.unit_cost_micro == 7000
    # Static entries are untouched.
    assert usage._rate_for("arcgis", "geocode_stored").unit_cost_micro == 4000
    assert usage._rate_for("nope", "nope") is None
