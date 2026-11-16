"""arcgis_geocode adapter — Nominatim primary, ArcGIS fallback,
Google Geocoding final fallback.

Hybrid: legacy_services Nominatim path for the free 99% case +
ArcGIS Geocoding (not-stored mode, $0.50/1K) as the cheaper-
than-Google fallback for raw-land / intersection addresses
Nominatim can't resolve + Google Geocoding ($5/1K) as the LAST
resort when both miss (Admin 2026-08-01 — he pays for the
shared platform key, same pattern as ArcGIS).

Why this shape:
  - legacy_services proves Nominatim handles ~95% of street
    addresses cleanly and costs nothing.
  - legacy_services uses Google as fallback ($5/1K). User asked
    for ArcGIS instead — ArcGIS not-stored geocoding is
    $0.50/1K with 20K free/month, ~10x cheaper than Google.
  - We use ArcGIS in `forStorage=false` mode. The user's key
    returned 403 on `forStorage=true` (stored mode requires a
    higher subscription tier most keys don't have). Not-stored
    mode works on any ArcGIS Location Platform key with
    geocoding scope.
  - Google is the final tier ONLY: it runs when the free +
    cheap tiers fail or reject every candidate, and its
    candidates pass the SAME road/county validation — a paid
    wrong-road match is still a wrong-road match. No key /
    disabled / Google error → the existing geocode_failed
    path, unchanged.

The source_id stays `arcgis_geocode` for registry continuity.
The `source` field on the AdapterResult records which path
actually RESOLVED ("nominatim", "arcgis", or "google") — that is
a display/audit fact, NOT the billing fact.

Billing is metered per REQUEST at the HTTP boundary (spec §8.6):
each tier this module attempts registers its own provider row on
the run's collector, so a tier that was called and missed is
still charged. A run where Nominatim and ArcGIS miss and Google
returns ZERO_RESULTS records nominatim + arcgis + google rows and
resolves nothing — the earlier shape, which priced only the
resolving tier, hid a paid Google request behind an `arcgis` row
(review finding 4, 2026-08-01). A tier that is never called
(disabled gate, missing key, an earlier tier already resolved)
records nothing.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime

import requests

from app.config.settings import get_settings
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.arcgis_geocode")

SOURCE_ID = "arcgis_geocode"

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_ARCGIS_GEOCODE = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/"
    "World/GeocodeServer/findAddressCandidates"
)
_GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT_S = 20

# Texas bounding box for Nominatim viewbox biasing (soft, not bounded):
# left(lng),top(lat),right(lng),bottom(lat).
_TX_VIEWBOX = "-106.65,36.50,-93.51,25.84"

# The same Texas box in Google `bounds` syntax — south,west|north,east.
# Like the Nominatim viewbox this is a soft BIAS, not a filter: Google's
# bounds parameter influences ranking without excluding matches, and the
# road-number + county checks do the hard filtering.
_TX_BOUNDS_GOOGLE = "25.84,-106.65|36.50,-93.51"

# How many candidates to request so we can pick a road-valid one instead
# of blindly taking [0]. legacy_services took limit=1 and shipped whatever OSM
# ranked first — that is exactly how "FM 971" resolved to "FM 1971" in
# Panola County (350km away) on 2026-07-21.
_CANDIDATE_LIMIT = 5

# Texas highway / farm-road designations whose NUMBER must match exactly.
# The 2026-07-21 Panola failure was a one-digit fuzzy match: the geocoder
# returned "FM 1971" for a request of "FM 971". When the requested address
# names such a road, the chosen candidate must be on the SAME numbered road.
_ROAD_RE = re.compile(
    r"\b(?:F\.?M\.?|R\.?M\.?|C\.?R\.?|S\.?H\.?|U\.?S\.?|I\.?H\.?|"
    r"S\.?L\.?|B\.?S\.?|B\.?U\.?|P\.?R\.?|"
    r"FARM[\s-]*TO[\s-]*MARKET|RANCH[\s-]*TO[\s-]*MARKET|"
    r"COUNTY\s+ROAD|STATE\s+HIGHWAY|US\s+HIGHWAY|HIGHWAY|HWY|"
    r"LOOP|SPUR|INTERSTATE)"
    # optional filler between the designation and the number, so
    # "Farm to Market Road 971" / "US Highway 79" also parse:
    r"(?:\s+(?:ROAD|RD|HIGHWAY|HWY|NO|NUMBER|NBR))?\.?"
    r"[\s#-]*(\d{1,5})\b",
    re.IGNORECASE,
)


def _requested_road_numbers(address: str) -> set[str]:
    """Numbers that belong to a named highway/farm-road DESIGNATION in the
    text (e.g. "1281 FM 971 & SH 130" → {"971", "130"}). Because the number
    must follow a road-type token (FM/RM/CR/SH/US/…), a bare house number or
    ZIP is never captured — so "971, FM 1971, Panola" yields {"1971"}, not
    {"971"}. Empty when there is no road designation (plain street address).
    The full contiguous number is captured, so 971 and 1971 are distinct."""
    return {m.group(1) for m in _ROAD_RE.finditer(address or "")}


def _candidate_on_requested_roads(
    candidate_text: str, want_numbers: set[str],
) -> bool:
    """True iff every requested road number appears as an actual road
    DESIGNATION number in the candidate's returned address text — not merely
    as some digit token. This is the fix for the FM 971 → FM 1971 failure:
    the wrong-road candidate "971, FM 1971, Panola County" (a 971 house
    number in front of the FM 1971 road) must NOT pass a request for FM 971.
    No requested roads → always True (plain-address passthrough)."""
    if not want_numbers:
        return True
    candidate_numbers = _requested_road_numbers(candidate_text)
    return want_numbers <= candidate_numbers


def _pick_candidate(
    candidates: list[tuple[float, float, str | None, str]],
    want_numbers: set[str],
    want_county: str | None,
) -> tuple[float, float, str | None] | None:
    """Choose from provider candidates, each (lat, lng, county, text).

    Precedence:
      1. on the requested road(s) AND in the requested county,
      2. on the requested road(s),
      3. if the request named a road but NO candidate is on it → reject
         (return None). Shipping a wrong-road match is the bug we are
         fixing; falling through to the next provider / failing loudly is
         correct.
      4. no road named → first candidate (old behavior).
    """
    on_road = [
        c for c in candidates
        if _candidate_on_requested_roads(c[3], want_numbers)
    ]
    if want_numbers and not on_road:
        return None
    pool = on_road or candidates
    if want_county:
        for lat, lng, county, _text in pool:
            if county and county == want_county:
                return (lat, lng, county)
    if pool:
        lat, lng, county, _text = pool[0]
        return (lat, lng, county)
    return None


# ── Nominatim primary (verbatim from legacy_services) ──────────────


def _try_nominatim(
    sess: requests.Session, address: str,
    *, county: str | None = None,
) -> tuple[float, float, str | None] | None:
    """Port of legacy_services public_intel._geocode Nominatim path,
    hardened 2026-07-21. Tries the full address first, then
    progressively simpler variants for raw-land intersection
    addresses (`X and Y`, `X & Y`, `X at Y`).

    Hardening vs the original (which took `limit=1` and shipped
    OSM's first hit): each query now requests several candidates
    with a Texas viewbox, and picks one that is actually on the
    requested numbered road (and, when known, in the requested
    county) via `_pick_candidate`. A one-digit fuzzy match
    (FM 971 → FM 1971, Panola County) is rejected instead of
    silently anchoring the whole analysis 350km away.
    """
    want_county = _normalize_county(county)
    queries = [address]
    for sep in (" and ", " & ", " at "):
        if sep in address.lower():
            parts = address.lower().split(sep.lower())
            suffix = ""
            for tok in (", TX", ", Texas", "Texas"):
                idx = address.lower().rfind(tok.lower())
                if idx >= 0:
                    suffix = address[idx:]
                    break
            for p in parts:
                p = p.strip().rstrip(",").strip()
                if p and len(p) > 3:
                    queries.append(f"{p}{suffix}")
            break

    for q in queries:
        want_numbers = _requested_road_numbers(q)
        try:
            resp = sess.get(
                _NOMINATIM,
                params={
                    "q": (
                        f"{q}, Texas"
                        if "texas" not in q.lower()
                        else q
                    ),
                    "format": "json",
                    "limit": _CANDIDATE_LIMIT,
                    "addressdetails": 1,
                    "countrycodes": "us",
                    # Soft bias toward Texas (not bounded=1: the road-
                    # number + county checks do the hard filtering, and
                    # bounded=1 can drop valid edge-of-state matches).
                    "viewbox": _TX_VIEWBOX,
                },
                timeout=_TIMEOUT_S,
                # Nominatim TOS requires a UA.
                headers={
                    "User-Agent": (
                        "PropDesk/1.0 "
                        "(propdesk_control_plane site-check)"
                    ),
                },
            )
            resp.raise_for_status()
            results = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("nominatim variant %r failed: %s", q, exc)
            continue
        time.sleep(1)  # Nominatim 1 req/sec rate limit per TOS.
        candidates: list[tuple[float, float, str | None, str]] = []
        for result in results or []:
            try:
                lat = float(result["lat"])
                lng = float(result["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            address_parts = result.get("address") or {}
            cand_county = _normalize_county(address_parts.get("county"))
            text = str(result.get("display_name") or "")
            candidates.append((lat, lng, cand_county, text))
        picked = _pick_candidate(candidates, want_numbers, want_county)
        if picked is not None:
            return picked
        if candidates and want_numbers:
            log.info(
                "geocode: nominatim returned %d candidate(s) for %r but "
                "none on requested road(s) %s — rejecting fuzzy match",
                len(candidates), q, sorted(want_numbers),
            )
    return None


# ── ArcGIS fallback (not-stored mode) ───────────────────────────


def _try_arcgis(
    sess: requests.Session, address: str,
    *, county: str | None = None,
) -> tuple[tuple[float, float, str | None] | None, str | None]:
    """ArcGIS Geocoding in `forStorage=false` (not-stored)
    mode. Cheaper than Google ($0.50/1K vs $5/1K) and works on
    any ArcGIS Location Platform key with geocoding scope.

    Returns (coords | None, error_msg | None). When the key
    has a structural problem (403 / scope / token), returns
    (None, error_msg) so the caller can surface a real error
    rather than masking it as "no candidates."

    Hardened 2026-07-21: requests several candidates and selects
    one that is on the requested numbered road (and, when known,
    in the requested county), highest ArcGIS Score first — rather
    than blindly taking candidates[0].
    """
    from app.core import secret_store

    arcgis_key = secret_store.get_secret("arcgis")
    if not arcgis_key:
        return None, "no ArcGIS key configured"
    api_key = arcgis_key.get_secret_value()
    if not api_key:
        return None, "ArcGIS key empty"

    try:
        resp = sess.get(
            _ARCGIS_GEOCODE,
            params={
                "SingleLine": address,
                "f": "json",
                "outFields": "Match_addr,Score,Subregion,City,RegionAbbr,Postal",
                "maxLocations": _CANDIDATE_LIMIT,
                "countryCode": "USA",  # constrain to the US
                "forStorage": "false",  # not-stored — see module docstring
                "token": api_key,
            },
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return None, f"arcgis request failed: {exc}"

    err = data.get("error")
    if err:
        # 4xx-style structural failure (token / scope / etc.).
        # Surface it loudly — this is NOT "address not found,"
        # it's "we shouldn't be calling ArcGIS at all."
        msg = (
            f"arcgis token error code={err.get('code')} "
            f"message={err.get('message')!r}"
        )
        log.warning(msg)
        return None, msg

    raw = data.get("candidates") or []
    if not raw:
        return None, None  # genuine "no candidates" — not an error

    want_county = _normalize_county(county)
    want_numbers = _requested_road_numbers(address)
    # Highest ArcGIS Score first so _pick_candidate's order tie-break
    # prefers the best match within the road/county-valid pool.
    raw_sorted = sorted(
        raw, key=lambda c: (c.get("attributes") or {}).get("Score") or 0,
        reverse=True,
    )
    candidates: list[tuple[float, float, str | None, str]] = []
    for cand in raw_sorted:
        location = cand.get("location") or {}
        lat = location.get("y")
        lng = location.get("x")
        if lat is None or lng is None:
            continue
        attrs = cand.get("attributes") or {}
        text = str(attrs.get("Match_addr") or cand.get("address") or "")
        candidates.append((
            float(lat), float(lng),
            _normalize_county(attrs.get("Subregion")), text,
        ))
    picked = _pick_candidate(candidates, want_numbers, want_county)
    if picked is None and candidates and want_numbers:
        log.info(
            "geocode: arcgis returned %d candidate(s) for %r but none on "
            "requested road(s) %s — rejecting fuzzy match",
            len(candidates), address, sorted(want_numbers),
        )
    return picked, None


# ── Google Geocoding final fallback (Admin 2026-08-01) ─────────


def _try_google(
    sess: requests.Session, address: str,
    *, county: str | None = None,
) -> tuple[float, float, str | None] | None:
    """Google Geocoding API as the LAST-resort tier, after Nominatim
    and ArcGIS both fail or reject every candidate. $5/1K (rate lives
    in settings.google_geocode_cost_micro_per_call).

    Metering (§8.6): the ledger charges the REQUEST, not the
    resolution. The `sess.get` below registers one google/geocode
    call whatever comes back — OK, ZERO_RESULTS, REQUEST_DENIED or a
    transport error — because Google bills all of them. The three
    early returns above it happen BEFORE any request, so a disabled
    gate or a missing key correctly records zero calls.

    Same validation contract as the other tiers: candidates carry
    their route (address_components type "route") and county
    ("administrative_area_level_2"), and `_pick_candidate` applies the
    identical on-road+county → on-road → reject-if-road-named-but-
    absent precedence. Texas `bounds` bias is soft, like the
    Nominatim viewbox.

    Returns coords or None. No key / gate off / request error /
    non-OK status all return None — the caller's existing
    geocode_failed path is unchanged (a paid tier being unavailable
    is never a NEW kind of failure).
    """
    from app.core import secret_store

    settings = get_settings()
    if not settings.google_geocode_enabled:
        log.debug("google geocode tier disabled by settings gate")
        return None
    google_key = secret_store.get_secret("google_maps")
    if not google_key:
        log.debug("google geocode tier skipped: no google_maps key configured")
        return None
    api_key = google_key.get_secret_value()
    if not api_key:
        log.debug("google geocode tier skipped: empty google_maps key")
        return None

    try:
        resp = sess.get(
            _GOOGLE_GEOCODE,
            params={
                "address": address,
                "key": api_key,
                "region": "us",
                "bounds": _TX_BOUNDS_GOOGLE,  # soft bias, not a filter
            },
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("google geocode request failed: %s", exc)
        return None

    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        # REQUEST_DENIED / OVER_QUERY_LIMIT / INVALID_REQUEST / … —
        # log loudly (key or quota problem worth an operator's eye)
        # but fall to the existing geocode_failed path.
        log.warning(
            "google geocode returned status=%s error_message=%r",
            status, data.get("error_message"),
        )
        return None

    want_county = _normalize_county(county)
    want_numbers = _requested_road_numbers(address)
    candidates: list[tuple[float, float, str | None, str]] = []
    for result in (data.get("results") or [])[:_CANDIDATE_LIMIT]:
        location = (result.get("geometry") or {}).get("location") or {}
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is None or lng is None:
            continue
        cand_county: str | None = None
        route_names: list[str] = []
        for comp in result.get("address_components") or []:
            types = comp.get("types") or []
            if "administrative_area_level_2" in types:
                cand_county = _normalize_county(comp.get("long_name"))
            elif "route" in types:
                # Both spellings so the _ROAD_RE designation check sees
                # whichever form carries the number ("FM 971" vs
                # "Farm to Market Road 971").
                for name in (comp.get("long_name"), comp.get("short_name")):
                    if name:
                        route_names.append(str(name))
        text = " ; ".join(
            [str(result.get("formatted_address") or ""), *route_names],
        )
        candidates.append((float(lat), float(lng), cand_county, text))

    picked = _pick_candidate(candidates, want_numbers, want_county)
    if picked is None and candidates and want_numbers:
        log.info(
            "geocode: google returned %d candidate(s) for %r but none on "
            "requested road(s) %s — rejecting fuzzy match",
            len(candidates), address, sorted(want_numbers),
        )
    return picked


# ── Adapter entry ───────────────────────────────────────────────


_INTERSECTION_MARKERS = (" & ", " and ", " at ")


def _is_intersection_query(address: str) -> bool:
    """Address looks like an intersection query — Nominatim is
    unreliable here (returns whichever 'CR 130' it indexes
    first; for the Hutto/CR-130-and-Chandler case 2026-05-05
    it picked the Celina/Collin one and silently shipped the
    wrong county). For these we go to ArcGIS first because
    its world geocoder handles intersections + city hints
    correctly."""
    return any(m in address.lower() for m in _INTERSECTION_MARKERS)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    """Geocode `identity.address`. Provider order is content-
    aware:

      - Intersection-shaped addresses (`X & Y` / `X and Y` /
        `X at Y`) → ArcGIS first, Nominatim fallback. Nominatim
        cannot tell two TX cities' "CR 130 & Chandler" apart
        and silently picks the wrong one (Celina vs Hutto
        regression caught 2026-05-05).
      - Street-number addresses → Nominatim first (free,
        accurate ~95% of the time), ArcGIS fallback.
      - Google Geocoding is the FINAL tier on BOTH paths
        (Admin 2026-08-01): it runs only after the free +
        cheap tiers miss, its candidates pass the same
        road/county validation, and when it is unavailable
        (no key / gate off / provider error) the existing
        geocode_failed outcome is unchanged.

    On structural ArcGIS failure (403 token, scope, etc.),
    returns AdapterResult with errors populated — the
    orchestrator surfaces this as a `geocode_failed` run
    error. Per user direction 2026-05-04: do not silently
    fall through provider config errors. (Google, whose
    candidates are validated identically, still gets its
    last-resort attempt first; the structural error is
    surfaced only when Google can't resolve either.)
    """
    sess = http_session(session)

    if _is_intersection_query(identity.address):
        # 1. ArcGIS first for intersections
        arc_coords, arc_err = _try_arcgis(sess, identity.address, county=identity.county)
        if arc_coords is not None:
            lat, lng, county = arc_coords
            return _success(
                lat, lng, county, "arcgis", identity.address,
            )
        if arc_err:
            # Token/scope error — don't silently fall to
            # Nominatim and ship the wrong CR 130. The Google
            # last resort (validated identically) still runs;
            # if it misses too, surface the structural error.
            g_coords = _try_google(sess, identity.address, county=identity.county)
            if g_coords is not None:
                lat, lng, county = g_coords
                return _success(
                    lat, lng, county, "google", identity.address,
                )
            return _failed(arc_err)
        # 2. Nominatim fallback (low-quality on intersections,
        # but better than nothing)
        log.info(
            "geocode: arcgis empty on intersection %r; "
            "trying nominatim",
            identity.address,
        )
        nom_coords = _try_nominatim(sess, identity.address, county=identity.county)
        if nom_coords is not None:
            lat, lng, county = nom_coords
            return _success(
                lat, lng, county, "nominatim", identity.address,
            )
        # 3. Google last resort
        log.info(
            "geocode: nominatim empty on intersection %r; "
            "trying google",
            identity.address,
        )
        g_coords = _try_google(sess, identity.address, county=identity.county)
        if g_coords is not None:
            lat, lng, county = g_coords
            return _success(
                lat, lng, county, "google", identity.address,
            )
        return _failed(
            f"Could not geocode {identity.address!r} "
            "(ArcGIS + Nominatim both returned no candidates "
            "for this intersection — try a more specific "
            "address with a city or ZIP)."
        )

    # Street-number address path (Nominatim first, free)
    coords = _try_nominatim(sess, identity.address, county=identity.county)
    if coords is not None:
        lat, lng, county = coords
        return _success(
            lat, lng, county, "nominatim", identity.address,
        )

    log.info(
        "geocode: nominatim failed for %r; trying arcgis",
        identity.address,
    )
    arc_coords, arc_err = _try_arcgis(sess, identity.address, county=identity.county)
    if arc_coords is not None:
        lat, lng, county = arc_coords
        return _success(
            lat, lng, county, "arcgis", identity.address,
        )

    log.info(
        "geocode: arcgis failed for %r; trying google",
        identity.address,
    )
    g_coords = _try_google(sess, identity.address, county=identity.county)
    if g_coords is not None:
        lat, lng, county = g_coords
        return _success(
            lat, lng, county, "google", identity.address,
        )
    if arc_err:
        return _failed(arc_err)
    return _failed(
        f"Could not geocode {identity.address!r} "
        "(Nominatim + ArcGIS both returned no candidates — try "
        "a more specific address)."
    )


def _success(
    lat: float, lng: float, county: str | None,
    source: str, address: str,
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "lat": lat,
            "lng": lng,
            "source": source,
            "address_used": address,
            "county": county,
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
    )


def _failed(
    reason: str,
    *,
    access: SourceAccessStatus = (
        SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
    ),
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=access,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )


def _normalize_county(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    cleaned = cleaned.removesuffix(" county")
    return cleaned or None
