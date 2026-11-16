"""census_acs adapter — US Census ACS 5-Year Estimates,
aggregated into 1/3/5-mile rings.

Algorithm, variable list and aggregation rules preserved verbatim from the
reference implementation. API key resolution reads the `census` entry from
the local secret store (app/core/secret_store.py) at call time.

Block group centroids are PRE-COMPUTED and shipped in
bg_centroids_48.json (TX only). NO runtime TIGER download.

Census API key: optional, free. Without it the adapter runs at a lower
rate limit.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

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

SOURCE_ID = "census_acs"

ACS_YEAR = 2023
TIGER_YEAR = 2023
ACS_BASE_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
_CENTROIDS_PATH = (
    Path(__file__).resolve().parent.parent / "bg_centroids"
    / "bg_centroids_48.json"
)
RINGS_MILES = (1, 3, 5)
MIN_POP_THRESHOLD = 50
ATTRIBUTION = "Source: U.S. Census Bureau"
USER_AGENT = "PropDesk/1.0 (propdesk_control_plane site-check)"
REQUEST_TIMEOUT = 60
_EARTH_RADIUS_MI = 3958.8
STATE_FIPS = "48"

ACS_VARS = [
    "B01003_001E", "B19013_001E", "B25077_001E",
    "B25003_002E", "B25003_003E", "B01002_001E",
    "B01001_020E", "B01001_021E", "B01001_022E",
    "B01001_023E", "B01001_024E", "B01001_025E",
    "B01001_044E", "B01001_045E", "B01001_046E",
    "B01001_047E", "B01001_048E", "B01001_049E",
    "B01001_011E", "B01001_012E", "B01001_013E",
    "B01001_014E",
    "B01001_035E", "B01001_036E", "B01001_037E",
    "B01001_038E",
    "B25010_001E", "B23025_002E",
    "B15003_022E", "B15003_023E", "B15003_024E",
    "B15003_025E", "B15003_001E",
    "B03003_003E", "B03003_001E",
    "B08303_001E",
    "B08303_002E", "B08303_003E", "B08303_004E",
    "B08303_005E", "B08303_006E", "B08303_007E",
    "B08303_008E", "B08303_009E", "B08303_010E",
    "B08303_011E", "B08303_012E", "B08303_013E",
]

COMMUTE_BIN_MIDPOINTS = {
    "B08303_002E": 2.5, "B08303_003E": 7,
    "B08303_004E": 12, "B08303_005E": 17,
    "B08303_006E": 22, "B08303_007E": 27,
    "B08303_008E": 32, "B08303_009E": 37,
    "B08303_010E": 42, "B08303_011E": 52,
    "B08303_012E": 75, "B08303_013E": 100,
}

_65_PLUS_VARS = [
    "B01001_020E", "B01001_021E", "B01001_022E",
    "B01001_023E", "B01001_024E", "B01001_025E",
    "B01001_044E", "B01001_045E", "B01001_046E",
    "B01001_047E", "B01001_048E", "B01001_049E",
]
_25_44_VARS = [
    "B01001_011E", "B01001_012E", "B01001_013E", "B01001_014E",
    "B01001_035E", "B01001_036E", "B01001_037E", "B01001_038E",
]
_COLLEGE_VARS = [
    "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E",
]


def _api_key() -> str:
    """Read at call time from the local secret store (the `census` entry),
    so a key saved in Settings reaches the worker with no restart. No key
    configured → the adapter's existing no-key degradation path reports
    honestly."""
    from app.core import secret_store

    key = secret_store.get_secret("census")
    return key.get_secret_value() if key else ""


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_MI * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_centroids_cache: dict[str, tuple[float, float]] | None = None


def _load_centroids() -> dict[str, tuple[float, float]]:
    global _centroids_cache
    if _centroids_cache is not None:
        return _centroids_cache
    if not _CENTROIDS_PATH.exists():
        raise FileNotFoundError(
            f"bg_centroids_48.json not found at {_CENTROIDS_PATH}"
        )
    data = json.loads(_CENTROIDS_PATH.read_text(encoding="utf-8"))
    _centroids_cache = {k: (v[0], v[1]) for k, v in data.items()}
    return _centroids_cache


def _block_groups_in_ring(
    center_lat, center_lon, radius_mi, centroids,
) -> list[str]:
    return [
        geoid for geoid, (lat, lon) in centroids.items()
        if _haversine_mi(center_lat, center_lon, lat, lon) <= radius_mi
    ]


def _fetch_acs_for_bgs(
    bg_geoids: list[str],
    variables: list[str],
    sess: requests.Session,
) -> dict[str, dict[str, Any]]:
    """One API call per county; filter in Python."""
    api_key = _api_key()
    if not api_key:
        raise OSError(
            "census: no API key configured for Census figures"
        )

    target_set = set(bg_geoids)
    counties: set[str] = set()
    for geoid in bg_geoids:
        if len(geoid) >= 5:
            counties.add(geoid[2:5])

    var_str = ",".join(variables)
    result: dict[str, dict[str, Any]] = {}

    for county in sorted(counties):
        url = (
            f"{ACS_BASE_URL}?get={var_str}"
            f"&for=block+group:*"
            f"&in=state:{STATE_FIPS}"
            f"+county:{county}"
            f"&key={api_key}"
        )
        try:
            resp = sess.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, list) or len(data) < 2:
                continue
            headers = data[0]
            for row in data[1:]:
                row_dict = dict(zip(headers, row))
                geoid = (
                    f"{STATE_FIPS}{county}"
                    f"{row_dict.get('tract', '')}"
                    f"{row_dict.get('block group', '')}"
                )
                if geoid not in target_set:
                    continue
                parsed: dict[str, Any] = {}
                for v in variables:
                    raw = row_dict.get(v)
                    try:
                        parsed[v] = (
                            float(raw) if raw is not None else None
                        )
                    except (ValueError, TypeError):
                        parsed[v] = None
                result[geoid] = parsed
            time.sleep(0.3)
        except Exception:  # noqa: S112 — ported: per-batch isolation, missing geoids handled downstream
            continue
    return result


def _safe_int(v) -> int:
    try:
        return int(float(v)) if v is not None else 0
    except (ValueError, TypeError):
        return 0


def _aggregate_ring(bg_data, bg_geoids) -> dict[str, Any]:
    if not bg_geoids:
        return _empty_ring(no_block_groups=True)

    total_pop = 0
    weighted_income = weighted_home = weighted_age = 0.0
    total_owner = total_renter = 0
    total_65 = total_25_44 = 0
    total_hh_size_num = 0.0
    total_hh_size_den = 0
    total_workers = total_college = total_25_plus = 0
    total_hispanic = total_hisp_den = 0
    commute_num = 0.0
    commute_den = 0
    bg_count = 0

    for geoid in bg_geoids:
        d = bg_data.get(geoid, {})
        if not d:
            continue
        bg_count += 1
        pop = _safe_int(d.get("B01003_001E"))
        total_pop += pop
        inc = d.get("B19013_001E")
        if inc and inc > 0 and pop > 0:
            weighted_income += float(inc) * pop
        hv = d.get("B25077_001E")
        if hv and hv > 0 and pop > 0:
            weighted_home += float(hv) * pop
        age = d.get("B01002_001E")
        if age and age > 0 and pop > 0:
            weighted_age += float(age) * pop
        total_owner += _safe_int(d.get("B25003_002E"))
        total_renter += _safe_int(d.get("B25003_003E"))
        for v in _65_PLUS_VARS:
            total_65 += _safe_int(d.get(v))
        for v in _25_44_VARS:
            total_25_44 += _safe_int(d.get(v))
        hs = d.get("B25010_001E")
        if hs and hs > 0:
            total_hh_size_num += float(hs)
            total_hh_size_den += 1
        total_workers += _safe_int(d.get("B23025_002E"))
        for v in _COLLEGE_VARS:
            total_college += _safe_int(d.get(v))
        total_25_plus += _safe_int(d.get("B15003_001E"))
        total_hispanic += _safe_int(d.get("B03003_003E"))
        total_hisp_den += _safe_int(d.get("B03003_001E"))
        for bin_var, midpoint in COMMUTE_BIN_MIDPOINTS.items():
            cnt = _safe_int(d.get(bin_var))
            commute_num += cnt * midpoint
            commute_den += cnt

    if total_pop < MIN_POP_THRESHOLD:
        out = _empty_ring(ring_too_small=True)
        out["block_groups_included"] = bg_count
        out["population"] = total_pop
        out["aggregation_note"] = (
            "Population below 50 — estimates suppressed for reliability."
        )
        return out

    return {
        "block_groups_included": bg_count,
        "population": total_pop,
        "median_household_income": (
            round(weighted_income / total_pop) if total_pop > 0 else None
        ),
        "median_home_value": (
            round(weighted_home / total_pop) if total_pop > 0 else None
        ),
        "owner_occupied_units": total_owner,
        "renter_occupied_units": total_renter,
        "owner_renter_ratio": (
            round(total_owner / total_renter, 2)
            if total_renter > 0 else None
        ),
        "median_age": (
            round(weighted_age / total_pop, 1) if total_pop > 0 else None
        ),
        "population_65_plus": total_65,
        "population_25_44": total_25_44,
        "median_household_size": (
            round(total_hh_size_num / total_hh_size_den, 1)
            if total_hh_size_den > 0 else None
        ),
        "estimated_commute_minutes": (
            round(commute_num / commute_den)
            if commute_den > 0 else None
        ),
        "estimated_daytime_workers": total_workers,
        "college_educated_25_plus_pct": (
            round(total_college / total_25_plus * 100, 1)
            if total_25_plus > 0 else None
        ),
        "hispanic_pct": (
            round(total_hispanic / total_hisp_den * 100, 1)
            if total_hisp_den > 0 else None
        ),
        "ring_too_small": False,
        "no_block_groups": False,
        "aggregation_note": (
            "Population-weighted means used for median income, "
            "home value, and age. True medians require PUMS "
            "microdata — these are approximations. Texas block "
            "groups only."
        ),
    }


def _empty_ring(*, no_block_groups=False, ring_too_small=False) -> dict:
    return {
        "block_groups_included": 0,
        "population": None,
        "median_household_income": None,
        "median_home_value": None,
        "owner_occupied_units": None,
        "renter_occupied_units": None,
        "owner_renter_ratio": None,
        "median_age": None,
        "population_65_plus": None,
        "population_25_44": None,
        "median_household_size": None,
        "estimated_commute_minutes": None,
        "estimated_daytime_workers": None,
        "college_educated_25_plus_pct": None,
        "hispanic_pct": None,
        "ring_too_small": ring_too_small,
        "no_block_groups": no_block_groups,
        "aggregation_note": "",
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    """Fetch ACS 5-year demographics in 1/3/5 mi rings around
    identity.lat/lng. Texas block groups only."""
    if identity.lat is None or identity.lng is None:
        return _failed_result("lat/lng required for census_acs")

    api_key = _api_key()
    if not api_key:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.NO_USABLE_PUBLIC_SOURCE_FOUND,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.COVERED,
            normalized_facts={},
            errors=["census: no API key configured for Census figures"],
            fetched_at=datetime.now(UTC),
        )

    sess = http_session(session)
    sess.headers.setdefault("User-Agent", USER_AGENT)

    try:
        centroids = _load_centroids()
    except Exception as exc:
        return _failed_result(f"centroids load failed: {exc}")

    bgs_5mi = _block_groups_in_ring(
        identity.lat, identity.lng, 5, centroids,
    )
    bgs_3mi = [
        g for g in bgs_5mi
        if _haversine_mi(
            identity.lat, identity.lng,
            centroids[g][0], centroids[g][1],
        ) <= 3
    ]
    bgs_1mi = [
        g for g in bgs_3mi
        if _haversine_mi(
            identity.lat, identity.lng,
            centroids[g][0], centroids[g][1],
        ) <= 1
    ]

    bg_data: dict[str, dict] = {}
    errors: list[str] = []
    if bgs_5mi:
        try:
            bg_data = _fetch_acs_for_bgs(bgs_5mi, ACS_VARS, sess)
        except Exception as exc:
            errors.append(f"acs api: {exc}")

    rings = {
        "1mi": _aggregate_ring(bg_data, bgs_1mi),
        "3mi": _aggregate_ring(bg_data, bgs_3mi),
        "5mi": _aggregate_ring(bg_data, bgs_5mi),
    }

    has_population = any(
        rings[r].get("population") for r in ("1mi", "3mi", "5mi")
    )
    if not has_population and not errors:
        errors.append("no block groups within 5 mi (likely outside TX)")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT
            if has_population
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.COVERED if has_population
            else CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION
        ),
        normalized_facts={
            "acs_year": ACS_YEAR,
            "tiger_year": TIGER_YEAR,
            "center_lat": identity.lat,
            "center_lon": identity.lng,
            "rings_miles": list(RINGS_MILES),
            "rings": rings,
            "data_source": "US Census Bureau ACS 5-Year",
            "attribution": ATTRIBUTION,
        },
        errors=errors or None,
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _failed_result(reason: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
