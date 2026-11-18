"""environmental_screening adapter — TCEQ LPST + EPA ECHO.

Ported verbatim from legacy_services/core/public_intel_adapters/
environmental.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Two-source contamination scan within ~500m (1/3 mi, standard
Phase I ESA distance):
  - TCEQ LPST = Texas leaking petroleum storage tanks
  - EPA ECHO  = federally-regulated facility records

Confidence CAPPED at SCREENED_INDICATOR even on a clean scan
per spec — public records are screening, NEVER a substitute
for a Phase I ESA. Manual upload prompt always populated.

Both sources use ArcGIS / EPA REST APIs. ECHO uses a TWO-CALL
flow (get_facilities → get_qid); the legacy one-call form
silently returned empty across the entire 16-property matrix
on 2026-05-02 — kept verbatim from legacy_services so the fix
that landed there carries forward.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from app.config.settings import get_settings
from app.regions import states
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.environmental_screening")

SOURCE_ID = "environmental_screening"

_TCEQ_PST_QUERY = (
    "https://services2.arcgis.com/LYMgRMwHfrWWEg3s/arcgis/rest/"
    "services/TCEQ_Leaking_Petroleum_Storage_Tank/"
    "FeatureServer/0/query"
)
_EPA_ECHO_QUERY = (
    "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
)
_EPA_ECHO_QID_QUERY = (
    "https://echodata.epa.gov/echo/echo_rest_services.get_qid"
)

# TCEQ's authoritative regulatory GIS (gisweb) exposes the contamination /
# cleanup datasets the original screen (petroleum tanks + EPA ECHO only)
# missed. Each entry is queried near the site and reported as its own
# category so a broker sees WHICH kind of environmental record is nearby.
# Verified live 2026-07-21 (record counts statewide): Superfund 168,
# IHW corrective action 5,713, brownfield 213, dry-cleaner 388, landfill 193,
# municipal setting designations 543.
_TCEQ_GISWEB = "https://gisweb.tceq.texas.gov/arcgis/rest/services"
_TCEQ_SCREEN_LAYERS: tuple[dict[str, str], ...] = (
    {
        "category": "superfund",
        "label": "TCEQ Superfund site",
        "url": f"{_TCEQ_GISWEB}/REM/SuperfundViewer_PRD/MapServer/0/query",
    },
    {
        "category": "corrective_action",
        "label": "Industrial & hazardous-waste corrective-action cleanup",
        "url": f"{_TCEQ_GISWEB}/Public/IHWCA/MapServer/0/query",
    },
    {
        "category": "brownfield",
        "label": "Brownfield site",
        "url": f"{_TCEQ_GISWEB}/Public/Brownfield/MapServer/0/query",
    },
    {
        "category": "dry_cleaner",
        "label": "Dry-cleaner remediation site",
        "url": f"{_TCEQ_GISWEB}/Public/DryCleaner/MapServer/0/query",
    },
    {
        "category": "landfill",
        "label": "Municipal solid-waste landfill",
        "url": f"{_TCEQ_GISWEB}/Public/Landfills/MapServer/0/query",
    },
    {
        "category": "groundwater_msd",
        "label": "Municipal Setting Designation (restricted groundwater)",
        "url": f"{_TCEQ_GISWEB}/Public/MSD_Points/MapServer/0/query",
    },
)

# TCEQ regulatory-history datasets on the Texas Open Data Portal (Socrata).
# These are ADDRESS-based (not radius): "was this address ever cited?".
# Verified live 2026-07-22. The TCEQ *complaints* dataset (tdzs-qqjn) has no
# address/location fields at all, so it cannot be screened programmatically —
# it is covered by the manual-lookup links below instead.
_TCEQ_OPEN_DATA_BASE = "https://data.texas.gov/resource"
_TCEQ_REGISTRY_DATASETS: tuple[dict[str, str], ...] = (
    {
        "key": "violations",
        "label": "TCEQ notice of violation",
        "dataset": "mwzi-gyw7",
        "loc_field": "phys_loc",
        "county_field": "county",
    },
    {
        "key": "enforcement",
        "label": "TCEQ enforcement action",
        "dataset": "rua3-iswk",
        "loc_field": "phys_loc_desc_txt",
        "county_field": "loc_cnty_name",
    },
)

# One-click manual lookups for the record systems that have no public API
# (document rooms + complaint files + deed records). Shown on the card so the
# broker can check the address personally.
_MANUAL_RECORDS_LINKS: tuple[dict[str, str], ...] = (
    {
        "label": "TCEQ Central File Room (documents on file for an address)",
        "url": "https://records.tceq.texas.gov/cs/idcplg?IdcService=TCEQ_SEARCH",
    },
    {
        "label": "TCEQ Central Registry (regulated entities at an address)",
        "url": "https://www15.tceq.texas.gov/crpub/",
    },
    {
        "label": "EPA Cleanups in My Community map",
        "url": "https://www.epa.gov/cleanups/cleanups-my-community",
    },
)

_TIMEOUT = 25
_DEFAULT_RADIUS_M = 500
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
# 5xx + network blips are worth retrying; 429 is NOT. EPA ECHO throttles at
# 300 req/hour and 1,500/day per IP — retrying a 429 immediately just spends
# more of that quota and deepens the throttle, so we fail fast on 429 and let
# the caller record the rate-limit cooldown instead.
_RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}


def _rate_limit_cooldown_seconds() -> int:
    """Provider HTTP 429 cooldown, env-overridable via settings (default 5m)."""
    return get_settings().public_intel_rate_limit_cooldown_seconds

_ENV_UPLOAD_TYPES = [
    "Phase I ESA",
    "Phase II report",
    "closure letters",
    "consultant memo",
]


def _fetch_outside_texas(
    identity: PropertyIdentity, sess: requests.Session, radius_m: int,
) -> AdapterResult:
    """Outside Texas: only EPA ECHO (nationwide) runs. The TCEQ tank,
    cleanup and regulatory-history checks are Texas's, so they are marked
    not connected — never reported as zero records or as an outage."""
    echo_records, echo_error = _query_epa_echo(
        sess, identity.lat, identity.lng, radius_m,
    )
    state_name = states.name_of(identity.state)
    from app.regions import sources as region_sources

    own = region_sources.pick(
        "state_environmental_records", identity.state, identity.county,
    )
    if own is not None:
        return _with_state_records(
            identity, sess, radius_m, own, echo_records, echo_error,
        )
    not_connected = f"State environmental records: Not connected for {state_name} yet."
    if echo_error:
        echo_line = "The federal facility check (EPA ECHO) is temporarily unavailable."
    elif echo_records:
        echo_line = (
            f"{len(echo_records)} federally regulated facilit"
            f"{'y' if len(echo_records) == 1 else 'ies'} within {radius_m:,} m (EPA ECHO)."
        )
    else:
        echo_line = f"No federally regulated facilities within {radius_m:,} m (EPA ECHO)."
    advisory = f"{echo_line} {not_connected}"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE if echo_error
            else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=(
            DataConfidenceStatus.UNKNOWN if echo_error
            else DataConfidenceStatus.SCREENED_INDICATOR
        ),
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "partial",
            "state": identity.state,
            "state_records": "not_connected",
            "echo_records": echo_records,
            "search_radius_m": radius_m,
            "source_statuses": {"epa_echo": _source_status(echo_error)},
            "advisory": advisory,
        },
        errors=[f"EPA ECHO: {echo_error}"] if echo_error else [],
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
    )


def _with_state_records(
    identity: PropertyIdentity,
    sess: requests.Session,
    radius_m: int,
    module: Any,
    echo_records: list[dict[str, Any]],
    echo_error: str | None,
) -> AdapterResult:
    """EPA ECHO plus a state's own environmental-records source (spec 2.6).
    The state part keeps the keys its card reads; the part's coverage,
    agency and version ride in `parts.state_records`."""
    from app.regions import sources as region_sources

    try:
        state_result = module.fetch(identity, session=sess)
        state_facts = dict(state_result.normalized_facts or {})
        state_errors = list(state_result.errors or [])
        state_coverage = region_sources.settle_coverage(
            "state_environmental_records", str(state_result.coverage_status.value),
            state_facts, state_errors,
        )
    except Exception as exc:  # a state source failing never fails the card
        state_facts, state_errors, state_coverage = {}, [str(exc)], "unknown"
    provider_short = str(getattr(module, "PROVIDER_SHORT", "") or "")
    part = {
        "coverage": state_coverage,
        "provider_name": getattr(module, "PROVIDER_NAME", ""),
        "provider_short": provider_short,
        "source_url": state_facts.get("source_url"),
        "data_as_of": state_facts.get("data_as_of"),
        "source_module": module.__name__,
        "source_version": str(getattr(module, "VERSION", 1)),
        "not_connected": state_facts.get("not_connected") or [],
    }
    if echo_error:
        echo_line = "The federal facility check (EPA ECHO) is temporarily unavailable."
    elif echo_records:
        echo_line = (
            f"{len(echo_records)} federally regulated facilit"
            f"{'y' if len(echo_records) == 1 else 'ies'} within {radius_m:,} m (EPA ECHO)."
        )
    else:
        echo_line = f"No federally regulated facilities within {radius_m:,} m (EPA ECHO)."
    state_line = str(state_facts.get("advisory") or "")
    if state_coverage not in ("covered", "partial"):
        state_line = (
            f"State environmental records ({provider_short}) are temporarily "
            "unavailable."
        )
    advisory = f"{echo_line} {state_line}".strip()
    any_records = bool(echo_records or state_facts.get("any_records"))
    errors = ([f"EPA ECHO: {echo_error}"] if echo_error else []) + [
        f"{provider_short}: {e}" for e in state_errors
    ]
    facts = {
        **{k: v for k, v in state_facts.items() if k not in (
            "provider_name", "provider_short", "source_url", "data_as_of",
            "advisory", "status",
        )},
        "status": "mixed" if any_records else ("partial" if errors else "clear"),
        "state": identity.state,
        "state_records": "connected" if state_coverage in ("covered", "partial") else "unavailable",
        "echo_records": echo_records,
        "search_radius_m": radius_m,
        "source_statuses": {
            **(state_facts.get("source_statuses") or {}),
            "epa_echo": _source_status(echo_error),
        },
        "parts": {**(state_facts.get("parts") or {}), "state_records": part},
        "embedded_sources": {
            "state_environmental_records": {
                "module": part["source_module"], "version": part["source_version"],
            },
        },
        "advisory": advisory,
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE if errors
            else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.PARTIAL if errors else CoverageStatus.COVERED,
        normalized_facts=facts,
        errors=errors or None,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
    )


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    radius_m: int = _DEFAULT_RADIUS_M,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)
    if identity.state != "TX":
        return _fetch_outside_texas(identity, sess, radius_m)

    pst_records, pst_error = _query_tceq_pst(
        sess, identity.lat, identity.lng, radius_m,
    )
    echo_records, echo_error = _query_epa_echo(
        sess, identity.lat, identity.lng, radius_m,
    )

    # Broader TCEQ contamination / cleanup layers. Each is independent: a
    # failing layer records its error and the screen continues, never aborts
    # (analysis-never-stops), so one flaky layer can't blank the whole card.
    broad_radius = get_settings().environmental_screening_broad_radius_m
    tceq_screen: dict[str, dict[str, Any]] = {}
    catalog_errors: list[str] = []
    for layer in _TCEQ_SCREEN_LAYERS:
        records, err = _query_tceq_layer(
            sess, layer["url"], identity.lat, identity.lng, broad_radius,
        )
        tceq_screen[layer["category"]] = {
            "label": layer["label"],
            "count": len(records),
            "records": records,
            "error": err,
        }
        if err:
            catalog_errors.append(f"TCEQ {layer['label']}: {err}")

    # Address-based TCEQ regulatory history: "was THIS address ever cited?"
    # (violations + enforcement; the complaints dataset has no address fields
    # and is covered by the manual-lookup links instead).
    registry: dict[str, dict[str, Any]] = {}
    registry_errors: list[str] = []
    house_number, street = _address_parts(identity.address or "")
    if street:
        reg_limit = get_settings().environmental_registry_max_records
        for spec in _TCEQ_REGISTRY_DATASETS:
            records, reg_err = _query_tceq_registry(
                sess, spec, street=street,
                county=identity.county, limit=reg_limit,
            )
            for record in records:
                record["match"] = _classify_registry_match(
                    record, house_number,
                )
            registry[spec["key"]] = {
                "label": spec["label"],
                "count": len(records),
                "address_match_count": sum(
                    1 for r in records if r["match"] == "address"
                ),
                "records": records,
                "error": reg_err,
            }
            if reg_err:
                registry_errors.append(f"TCEQ {spec['label']}: {reg_err}")

    errors: list[str] = []
    if pst_error:
        errors.append(f"TCEQ PST: {pst_error}")
    if echo_error:
        errors.append(f"EPA ECHO: {echo_error}")
    errors.extend(catalog_errors)
    errors.extend(registry_errors)

    fetched_at = datetime.now(UTC)

    catalog_hits = sum(v["count"] for v in tceq_screen.values())
    # Only exact-address registry hits count toward "records found" — a
    # same-road citation can be miles away and is shown as context only.
    registry_address_hits = sum(
        v["address_match_count"] for v in registry.values()
    )
    any_records = bool(
        pst_records or echo_records or catalog_hits or registry_address_hits
    )
    all_errored = (
        bool(pst_error) and bool(echo_error)
        and all(v["error"] for v in tceq_screen.values())
    )
    any_errored = bool(
        pst_error or echo_error or catalog_errors or registry_errors
    )
    if all_errored:
        confidence = DataConfidenceStatus.UNKNOWN
        status = "unknown"
        coverage = CoverageStatus.PARTIAL
        source_access = SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
    elif any_errored:
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        status = "mixed" if any_records else "partial"
        coverage = CoverageStatus.PARTIAL
        source_access = SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
    else:
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        status = "mixed" if any_records else "clear"
        coverage = CoverageStatus.COVERED
        source_access = SourceAccessStatus.PROGRAMMATIC_PUBLIC

    source_statuses = {
        "tceq_pst": _source_status(pst_error),
        "epa_echo": _source_status(echo_error),
    }
    for cat, v in tceq_screen.items():
        source_statuses[f"tceq_{cat}"] = _source_status(v["error"])
    for key, v in registry.items():
        source_statuses[f"tceq_registry_{key}"] = _source_status(v["error"])

    source_cooldowns = _source_cooldowns(
        fetched_at=fetched_at,
        pst_error=pst_error,
        echo_error=echo_error,
    )
    advisory = _build_advisory(
        pst_records,
        echo_records,
        radius_m,
        pst_error=pst_error,
        echo_error=echo_error,
        tceq_screen=tceq_screen,
        broad_radius_m=broad_radius,
        registry=registry,
    )

    unavailable = [
        label for label, error in (
            ("TCEQ PST", pst_error),
            ("EPA ECHO", echo_error),
        )
        if error
    ]
    unavailable.extend(v["label"] for v in tceq_screen.values() if v["error"])
    unavailable.extend(v["label"] for v in registry.values() if v["error"])

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=source_access,
        data_confidence_status=confidence,
        coverage_status=coverage,
        normalized_facts={
            "status": status,
            "pst_records": pst_records,
            "echo_records": echo_records,
            "tceq_screen": tceq_screen,
            "tceq_address_registry": registry,
            "registry_street_queried": street,
            "manual_records_links": [dict(x) for x in _MANUAL_RECORDS_LINKS],
            "search_radius_m": radius_m,
            "broad_search_radius_m": broad_radius,
            "source_statuses": source_statuses,
            "source_cooldowns": source_cooldowns,
            "retry_disabled_until": _latest_cooldown_until(
                source_cooldowns,
            ),
            "retry_after_seconds": (
                _rate_limit_cooldown_seconds() if source_cooldowns else None
            ),
            "unavailable_sources": unavailable,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=fetched_at,
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ENV_UPLOAD_TYPES),
            reason=(
                "Public TCEQ + EPA records are screening only. "
                "A Phase I ESA is required for formal "
                "environmental due diligence; upload one to "
                "satisfy this gap."
            ),
        ),
    )


def _query_tceq_pst(
    session: requests.Session, lat: float, lng: float, radius_m: int,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": radius_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "OBJECTID,SITE_NAME,PHYS_ADDR,CITY,COUNTY,"
            "REM_PROG,RN,PST_ID,ZIP_CODE"
        ),
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = _get_with_retry(session, _TCEQ_PST_QUERY, params=params)
        ct = (getattr(resp, "headers", {}) or {}).get(
            "content-type",
            "",
        ).lower()
        if "json" not in ct:
            return [], (
                f"TCEQ returned non-JSON content-type ({ct[:40]}); "
                "endpoint may have moved again — re-run the AGOL "
                "search 'TCEQ Leaking Petroleum Storage Tank'"
            )
        data = resp.json()
        features = data.get("features", []) or []
        return [_normalize_pst_record(f) for f in features], None
    except Exception as exc:
        log.warning("TCEQ PST query failed: %s", exc)
        return [], _friendly_provider_error(exc)


def _query_epa_echo(
    session: requests.Session, lat: float, lng: float, radius_m: int,
) -> tuple[list[dict[str, Any]], str | None]:
    radius_mi = max(0.1, radius_m / 1609.34)
    step1_params = {
        "output": "JSON",
        "p_lat": lat,
        "p_long": lng,
        "p_radius": radius_mi,
    }
    try:
        resp = _get_with_retry(session, _EPA_ECHO_QUERY, params=step1_params)
        results = (resp.json().get("Results") or {})
        qid = results.get("QueryID")
        qrows = int(results.get("QueryRows") or 0)
        if not qid or qrows == 0:
            return [], None
        step2_params = {
            "output": "JSON",
            "qid": qid,
            "responseset": "1",
            "pageno": "1",
        }
        resp2 = _get_with_retry(
            session,
            _EPA_ECHO_QID_QUERY,
            params=step2_params,
        )
        results2 = (resp2.json().get("Results") or {})
        facilities = results2.get("Facilities") or []
        return [_normalize_echo_record(f) for f in facilities], None
    except Exception as exc:
        log.warning("EPA ECHO query failed: %s", exc)
        return [], _friendly_provider_error(exc)


def _query_tceq_layer(
    session: requests.Session,
    url: str,
    lat: float,
    lng: float,
    radius_m: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Generic near-point query against a TCEQ gisweb MapServer layer.
    Reuses the same point+distance ArcGIS query the PST screen uses.
    Returns (normalized_records, error)."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": radius_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = _get_with_retry(session, url, params=params)
        ct = (getattr(resp, "headers", {}) or {}).get(
            "content-type", "",
        ).lower()
        if "json" not in ct:
            return [], (
                f"TCEQ returned non-JSON content-type ({ct[:40]}); the "
                "layer endpoint may have moved."
            )
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return [], f"TCEQ layer error: {str(data.get('error'))[:120]}"
        features = data.get("features", []) or []
        return [_normalize_tceq_site_record(f) for f in features], None
    except Exception as exc:
        log.warning("TCEQ layer query failed (%s): %s", url, exc)
        return [], _friendly_provider_error(exc)


# ── TCEQ address-based regulatory history (Socrata) ──────────────

# Texas road designations — used to isolate the road name from the address so
# "1281 FM 971 Georgetown Texas" screens on "FM 971" (city/state stripped).
_REGISTRY_ROAD_RE = re.compile(
    r"\b(?:FM|RM|CR|SH|US|IH|HWY|HIGHWAY|LOOP|SPUR|INTERSTATE)"
    r"[\s#-]*\d{1,5}\b",
    re.IGNORECASE,
)


def _address_parts(address: str) -> tuple[str | None, str | None]:
    """(house_number, street_fragment) for registry matching.

    Uses the first comma segment; pulls the leading house number; prefers a
    numbered road designation (FM 971) as the street key because it is
    city-proof. Falls back to the remaining text minus state words. Either
    part may be None when the address doesn't parse — the registry screen is
    skipped rather than guessed."""
    first = (address or "").split(",")[0].strip()
    if not first:
        return None, None
    tokens = first.split()
    house = tokens[0] if tokens and tokens[0].isdigit() else None
    road = _REGISTRY_ROAD_RE.search(first)
    if road:
        return house, re.sub(r"[\s#-]+", " ", road.group(0)).upper().strip()
    rest = tokens[1:] if house else tokens
    rest = [t for t in rest if t.lower() not in ("texas", "tx")]
    street = " ".join(rest).upper().strip()
    return house, (street or None)


def _soql_quote(value: str) -> str:
    """Escape a value for inclusion in a SoQL single-quoted string."""
    return value.replace("'", "''")


def _query_tceq_registry(
    session: requests.Session,
    spec: dict[str, str],
    *,
    street: str,
    county: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Query one TCEQ open-data dataset for records whose physical location
    mentions the subject street, scoped to the county when known. Returns
    (normalized_records, error)."""
    clauses = [
        f"upper({spec['loc_field']}) like '%{_soql_quote(street.upper())}%'",
    ]
    if county:
        clauses.append(
            f"upper({spec['county_field']}) like "
            f"'%{_soql_quote(county.upper())}%'"
        )
    params = {
        "$where": " and ".join(clauses),
        "$limit": limit,
    }
    url = f"{_TCEQ_OPEN_DATA_BASE}/{spec['dataset']}.json"
    try:
        resp = _get_with_retry(session, url, params=params)
        data = resp.json()
        if not isinstance(data, list):
            return [], f"unexpected registry response shape ({type(data).__name__})"
        return [_normalize_registry_record(r) for r in data], None
    except Exception as exc:
        log.warning("TCEQ registry query failed (%s): %s", spec["key"], exc)
        return [], _friendly_provider_error(exc)


def _normalize_registry_record(row: dict[str, Any]) -> dict[str, Any]:
    def _first(*keys: str) -> str:
        for k in keys:
            v = row.get(k)
            if v not in (None, "", " "):
                return str(v)
        return ""

    return {
        "entity_name": _first("rn_name", "reg_ent_name") or "Unknown entity",
        "location": _first("phys_loc", "phys_loc_desc_txt"),
        "city": _first("phys_city", "city_name", "near_city_name"),
        "county": _first("county", "loc_cnty_name"),
        "tceq_rn": _first("rn_number", "rn"),
        "date": _first("invest_approved_dt", "noe_dt", "violation_status_date"),
        "violation_count": _first("viol_cnt"),
        "citations": _first("c_viol_citations", "a_viol_citations"),
        "raw": row,
    }


def _classify_registry_match(
    record: dict[str, Any], house_number: str | None,
) -> str:
    """'address' when the subject house number appears in the record's
    location (digit-bounded), else 'same_road'."""
    if not house_number:
        return "same_road"
    loc = record.get("location") or ""
    if re.search(rf"(?<!\d){re.escape(house_number)}(?!\d)", loc):
        return "address"
    return "same_road"


def _normalize_tceq_site_record(feature: dict[str, Any]) -> dict[str, Any]:
    """Common shape across the TCEQ remediation/waste layers, which share the
    SITE_NAME / PHYS_ADDR / CITY / COUNTY / RN field convention."""
    attrs = feature.get("attributes") or {}

    def _first(*keys: str) -> str:
        for k in keys:
            v = attrs.get(k)
            if v not in (None, "", " "):
                return str(v)
        return ""

    return {
        "facility_name": _first(
            "SITE_NAME", "FACILITY_NAME", "FAC_NAME", "NAME", "RE_NAME",
        ) or "Unknown site",
        "address": _first("PHYS_ADDR", "ADDRESS", "ADDR_DESC"),
        "city": _first("CITY"),
        "county": _first("COUNTY"),
        "tceq_rn": _first("RN"),
        "program": _first("REM_PROG", "PROGRAM", "STATUS", "SITE_STATUS"),
        "detail_url": _first("WEB_URL", "URL"),
        "raw": attrs,
    }


def _get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any],
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code in _TRANSIENT_HTTP_STATUSES:
                raise requests.HTTPError(
                    f"temporary HTTP {resp.status_code}",
                    response=resp,
                )
            resp.raise_for_status()
            return resp
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
        ) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # Never retry a 429 — that only burns more of the provider's
            # hourly/daily quota. Retry genuine transient failures (5xx +
            # network/timeout) once or twice with backoff.
            retryable = status in _RETRYABLE_HTTP_STATUSES or isinstance(
                exc, (requests.ConnectionError, requests.Timeout),
            )
            if attempt < 2 and retryable:
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise
    raise last_exc or RuntimeError("provider request failed")


def _friendly_provider_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 429:
            return (
                "temporary provider rate limit (HTTP 429); retry the "
                "Public Data refresh later"
            )
        if status in _TRANSIENT_HTTP_STATUSES:
            return (
                f"temporary provider outage (HTTP {status}); retry the "
                "Public Data refresh later"
            )
    if isinstance(exc, requests.Timeout):
        return (
            "temporary provider timeout; retry the Public Data refresh later"
        )
    return str(exc)[:200]


def _source_status(error: str | None) -> str:
    if not error:
        return "queried"
    if "rate limit" in error:
        return "temporary_rate_limited"
    if "timeout" in error:
        return "temporary_timeout"
    return "temporary_unavailable"


def _source_cooldowns(
    *,
    fetched_at: datetime,
    pst_error: str | None,
    echo_error: str | None,
) -> dict[str, dict[str, Any]]:
    cooldowns: dict[str, dict[str, Any]] = {}
    for key, label, error in (
        ("tceq_pst", "TCEQ PST", pst_error),
        ("epa_echo", "EPA ECHO", echo_error),
    ):
        if not error or "rate limit" not in error.lower():
            continue
        retry_disabled_until = (
            fetched_at + timedelta(seconds=_rate_limit_cooldown_seconds())
        )
        cooldowns[key] = {
            "source_label": label,
            "reason": "provider_rate_limited",
            "retry_after_seconds": _rate_limit_cooldown_seconds(),
            "retry_disabled_until": retry_disabled_until.isoformat(),
            "message": (
                f"{label} asked us to slow down. This source can be "
                "refreshed again in a few minutes."
            ),
        }
    return cooldowns


def _latest_cooldown_until(
    cooldowns: dict[str, dict[str, Any]],
) -> str | None:
    values = [
        str(item.get("retry_disabled_until") or "").strip()
        for item in cooldowns.values()
        if isinstance(item, dict)
        and str(item.get("retry_disabled_until") or "").strip()
    ]
    return max(values) if values else None


def _normalize_pst_record(feature: dict[str, Any]) -> dict[str, Any]:
    attrs = feature.get("attributes") or {}
    return {
        "facility_name": (
            attrs.get("SITE_NAME") or attrs.get("FACILITY_NAME")
            or attrs.get("FAC_NAME") or attrs.get("NAME")
            or "Unknown facility"
        ),
        "address": (
            attrs.get("PHYS_ADDR") or attrs.get("FACILITY_ADDRESS")
            or attrs.get("ADDRESS") or ""
        ),
        "city": attrs.get("CITY") or "",
        "county": attrs.get("COUNTY") or "",
        "tceq_rn": attrs.get("RN") or "",
        "pst_id": attrs.get("PST_ID") or "",
        "lpst_status": (
            attrs.get("REM_PROG") or attrs.get("LPST_STATUS")
            or attrs.get("STATUS") or ""
        ),
        "raw": attrs,
    }


def _normalize_echo_record(facility: dict[str, Any]) -> dict[str, Any]:
    return {
        "facility_name": facility.get("FacName") or "Unknown facility",
        "address": facility.get("FacStreet") or "",
        "city": facility.get("FacCity") or "",
        "epa_id": facility.get("RegistryID") or "",
        "programs": facility.get("EnvAreaName") or "",
        "raw": facility,
    }


def _build_advisory(
    pst_records: list[dict[str, Any]],
    echo_records: list[dict[str, Any]],
    radius_m: int,
    *,
    pst_error: str | None = None,
    echo_error: str | None = None,
    tceq_screen: dict[str, dict[str, Any]] | None = None,
    broad_radius_m: int | None = None,
    registry: dict[str, dict[str, Any]] | None = None,
) -> str:
    parts: list[str] = []
    if pst_records:
        parts.append(
            f"{len(pst_records)} TCEQ petroleum storage tank "
            f"record(s) within {radius_m}m. Review for proximity "
            "and LPST status."
        )
    elif pst_error:
        parts.append(
            f"TCEQ PST could not be checked today ({pst_error}). "
            "Do not treat this as a clean PST result."
        )
    else:
        parts.append(
            f"No TCEQ PST records found within {radius_m}m "
            "screened radius."
        )
    if echo_records:
        parts.append(
            f"{len(echo_records)} EPA ECHO regulated facility "
            f"record(s) within {radius_m}m. Review for relevance "
            "to the proposed use."
        )
    elif echo_error:
        parts.append(
            f"EPA ECHO could not be checked today ({echo_error}). "
            "Do not treat this as a clean ECHO result."
        )
    else:
        parts.append(
            f"No EPA ECHO regulated facility records within "
            f"{radius_m}m."
        )

    tceq_screen = tceq_screen or {}
    hits = [
        (v["label"], v["count"])
        for v in tceq_screen.values()
        if not v.get("error") and v.get("count")
    ]
    errored = [v["label"] for v in tceq_screen.values() if v.get("error")]
    br = broad_radius_m or radius_m
    if hits:
        summary = "; ".join(f"{count} {label}" for label, count in hits)
        parts.append(
            f"Within {br}m, TCEQ contamination/cleanup layers show: "
            f"{summary}. Review each for relevance to the proposed use."
        )
    elif tceq_screen and not errored:
        parts.append(
            f"No TCEQ Superfund, corrective-action, brownfield, dry-cleaner, "
            f"landfill, or restricted-groundwater records within {br}m."
        )
    if errored:
        parts.append(
            "Some TCEQ layers could not be checked today "
            f"({', '.join(errored)}); do not treat these as clean results."
        )

    registry = registry or {}
    address_hits = sum(
        v.get("address_match_count") or 0 for v in registry.values()
    )
    same_road_hits = sum(
        (v.get("count") or 0) - (v.get("address_match_count") or 0)
        for v in registry.values()
        if not v.get("error")
    )
    registry_errored = [
        v["label"] for v in registry.values() if v.get("error")
    ]
    if address_hits:
        parts.append(
            f"IMPORTANT: {address_hits} TCEQ violation/enforcement record(s) "
            "are on file FOR THIS ADDRESS. Review each before purchase — "
            "this is documented regulatory history, not a proximity signal."
        )
    elif registry and not registry_errored:
        parts.append(
            "No TCEQ violation or enforcement record is on file for this "
            "address."
        )
    if same_road_hits:
        parts.append(
            f"{same_road_hits} record(s) on the same road nearby are shown "
            "as context only."
        )
    if registry_errored:
        parts.append(
            "TCEQ regulatory-history search unavailable today "
            f"({', '.join(registry_errored)}); not a clean result."
        )
    if registry:
        parts.append(
            "TCEQ complaint files, the Central File Room, and county deed "
            "records are not searchable by API — use the provided lookup "
            "links to check the address directly."
        )

    # No hits in any queried layer is NOT a clean-site finding: this screen
    # does not assess historical land use, and small operations (e.g. fuel,
    # chemical, or pyrotechnic/fireworks storage) may leave no record in these
    # public registries even when they caused contamination.
    nothing_found = (
        not pst_records and not echo_records
        and not hits and not pst_error and not echo_error and not errored
    )
    if nothing_found:
        parts.append(
            "No records in these public databases is NOT a clean-site "
            "finding: this screen does not assess historical land use, and "
            "some contaminating uses leave no public regulatory record."
        )
    parts.append(
        "Screening only — Phase I ESA recommended for formal "
        "environmental due diligence."
    )
    return " ".join(parts)


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "pst_records": [], "echo_records": [],
            "search_radius_m": _DEFAULT_RADIUS_M,
            "advisory": (
                "Coordinates required for environmental "
                "screening. Upload a Phase I ESA instead."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for environmental query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ENV_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable for TCEQ PST + EPA "
                "ECHO screening. Upload a Phase I ESA / Phase II."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
