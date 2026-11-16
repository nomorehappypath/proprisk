"""hydro_intel adapter — USGS NHD flowlines.

Ported verbatim from legacy_services/core/public_intel_adapters/
hydro_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Single point+distance query against the USGS National Map
NHD MapServer layer 6 (Flowline). Returns named streams +
rivers + canals within ~500m of the parcel, sorted named-
first then by length.

Confidence stays SCREENED_INDICATOR — NHD is broad-scale
mapping. The binding ordinary high water mark + WOTUS
jurisdiction comes from a USACE JD + field delineation.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
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
    ManualUploadRequest,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.hydro_intel")

SOURCE_ID = "hydro_intel"

_NHD_FLOWLINE_QUERY = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/"
    "MapServer/6/query"
)

_TIMEOUT = 25
_DEFAULT_BUFFER_M = 500
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

_FTYPE_LABELS: dict[int, str] = {
    460: "Stream/River (perennial or intermittent)",
    558: "Artificial Path",
    336: "Canal/Ditch",
    566: "Coastline",
    334: "Connector",
    420: "Underground Conduit",
    428: "Pipeline",
}

_HYDRO_UPLOAD_TYPES = [
    "USACE Jurisdictional Determination",
    "wetland delineation",
    "stream-buffer waiver",
    "civil drainage study",
]


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    buffer_m: int = _DEFAULT_BUFFER_M,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)

    features, error = _query_nhd(
        sess, identity.lat, identity.lng, buffer_m,
    )
    if error:
        return _fetch_failed_result(error)

    flowlines = _summarize_flowlines(features)
    named_count = sum(1 for f in flowlines if f.get("name"))
    nearest_named = next(
        (f for f in flowlines if f.get("name")), None,
    )

    advisory = _build_advisory(
        flowlines, named_count, nearest_named, buffer_m,
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": (
                "clear" if flowlines else "no_flowlines_in_buffer"
            ),
            "search_radius_m": buffer_m,
            "flowlines_count": len(flowlines),
            "named_flowlines_count": named_count,
            "nearest_named_flowline": nearest_named,
            "flowlines": flowlines[:10],
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HYDRO_UPLOAD_TYPES),
            reason=(
                "NHD flowlines are screening — the binding "
                "ordinary high water mark + WOTUS "
                "jurisdiction comes from a USACE JD + field "
                "delineation. City setback waivers / "
                "stream-buffer modifications require a civil "
                "drainage study + planning department "
                "approval."
            ),
        ),
    )


def _query_nhd(
    session: requests.Session,
    lat: float, lng: float, buffer_m: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Retry transient HTTP/timeout errors.

    The USGS NHD MapServer has intermittent 429/5xx/timeout
    days. This source is useful screening, but a missed response
    must be shown as provider instability, not as zero waterways.
    """
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gnis_name,lengthkm,reachcode,ftype,fcode",
        "returnGeometry": "false",
        "f": "json",
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = session.get(
                _NHD_FLOWLINE_QUERY,
                params=params, timeout=_TIMEOUT,
            )
            if resp.status_code in _TRANSIENT_HTTP_STATUSES:
                raise requests.HTTPError(
                    f"NHD transient {resp.status_code}",
                    response=resp,
                )
            resp.raise_for_status()
            ct = (getattr(resp, "headers", {}) or {}).get(
                "content-type",
                "",
            ).lower()
            if "json" not in ct:
                return [], f"non-JSON content-type ({ct[:40]})"
            return resp.json().get("features", []) or [], None
        except (
            requests.exceptions.HTTPError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (2 ** attempt))
                continue
            log.warning("NHD query failed after retry: %s", exc)
            return [], _friendly_nhd_error(exc)
    return [], _friendly_nhd_error(last_exc) if last_exc else "NHD query failed"


def _friendly_nhd_error(exc: Exception | None) -> str:
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
    if exc:
        return str(exc)[:200]
    return "temporary provider failure; retry the Public Data refresh later"


def _summarize_flowlines(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = (attrs.get("gnis_name") or "").strip() or None
        ftype = attrs.get("ftype")
        ftype_label = _FTYPE_LABELS.get(
            int(ftype) if ftype is not None else 0,
            f"FType {ftype}" if ftype else "Unknown type",
        )
        length_km = attrs.get("lengthkm") or 0
        out.append({
            "name": name,
            "ftype": ftype,
            "ftype_label": ftype_label,
            "reach_code": attrs.get("reachcode") or "",
            # The NHD segment's own length — NOT the distance to the parcel.
            # Named unambiguously so narrative AI can't read it as proximity.
            "segment_length_km": round(float(length_km), 3),
        })
    out.sort(
        key=lambda f: (
            0 if f["name"] else 1,
            -f["segment_length_km"],
        ),
    )
    return out


def _build_advisory(
    flowlines: list[dict[str, Any]],
    named_count: int,
    nearest_named: dict[str, Any] | None,
    buffer_m: int,
) -> str:
    if not flowlines:
        return (
            f"No NHD flowlines within {buffer_m} m of the "
            "parcel. Stormwater + WOTUS context still "
            "requires civil engineering review."
        )
    parts = [
        (f"{len(flowlines)} NHD flowline(s) within {buffer_m} m "
        f"({named_count} named).")
    ]
    if nearest_named:
        # Markdown-bold the waterway name to match legacy_services
        # advisory verbatim (the memo renderer interprets the
        # markdown).
        parts.append(
            f"Nearest named waterway: **{nearest_named['name']}** "
            f"({nearest_named['ftype_label']}, "
            f"{nearest_named['segment_length_km']} km in this segment)."
        )
        parts.append(
            "Named perennial streams typically trigger city "
            "setbacks (water-quality buffer / Critical Water "
            "Quality Zone) and may fall under USACE Section "
            "404 jurisdiction for any fill activity."
        )
    parts.append(
        "Mapping is broad-scale screening — USACE JD + field "
        "delineation is the binding answer."
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
            "flowlines_count": 0,
            "named_flowlines_count": 0,
            "nearest_named_flowline": None,
            "flowlines": [],
            "advisory": (
                "Coordinates required for NHD flowline lookup."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for NHD query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HYDRO_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Pull the USGS "
                "topo or a survey with hydrography."
            ),
        ),
    )


def _fetch_failed_result(error_msg: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "temporary_unavailable",
            "flowlines_count": 0,
            "named_flowlines_count": 0,
            "nearest_named_flowline": None,
            "flowlines": [],
            "source_status": (
                "temporary_rate_limited"
                if "rate limit" in error_msg
                else "temporary_timeout"
                if "timeout" in error_msg
                else "temporary_unavailable"
            ),
            "advisory": (
                "USGS NHD did not respond during this refresh. "
                "Do not treat this as zero mapped waterways; retry "
                "Public Data or verify hydrography from survey, "
                "civil drainage plans, USGS topo, or USACE JD."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"NHD query failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HYDRO_UPLOAD_TYPES),
            reason=(
                "NHD service did not respond. Pull a USACE "
                "JD or wetland delineation directly."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
