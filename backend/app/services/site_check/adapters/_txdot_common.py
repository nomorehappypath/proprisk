"""Shared TxDOT helpers — ArcGIS spatial query + name formatting
+ epoch formatter. Lifted from legacy_services/core/public_intel.py
helpers used by every TxDOT fetcher."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import requests

from app.config.settings import get_settings
from app.services.site_check.adapters._resilient_http import (
    arcgis_json_error,
    resilient_json,
)

# URLs verbatim from legacy_services/core/public_intel.py
# (do not edit without re-running smoke_public_intel.py — wrong
# URLs return Token Required / Invalid URL even though the
# response is HTTP 200, which is exactly the silent-failure
# pattern the smoke matrix exists to catch).
_TXDOT_AADT = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/ArcGIS/rest/services/"
    "TxDOT_AADT/FeatureServer/0/query"
)
_TXDOT_PROJECTS = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_Projects/FeatureServer/0/query"
)
_TXDOT_ROADWAY = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_Roadway_Inventory/FeatureServer/0/query"
)
_TXDOT_BRIDGES = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_Bridges/FeatureServer/0/query"
)

_MILES_TO_METERS = 1609.344
_ARCGIS_QUERY_TIMEOUT = 45
_TIMEOUT_S = 20


def arcgis_spatial_query(
    sess: requests.Session,
    url: str,
    lat: float,
    lng: float,
    radius_meters: int = 2000,
    out_fields: str = "*",
    return_geometry: bool = False,
    max_records: int = 50,
) -> list[dict]:
    """ArcGIS FeatureServer spatial query via envelope.

    Resilience policy (2026-07-14, replaces the legacy_services 45s + one
    instant retry): the heavy TxDOT spatial scans (TxDOT_AADT has 717K
    polylines) intermittently exceed a single attempt while the service
    is loaded, and an instant retry re-fires into the same overloaded
    window — which is why user refreshes failed in bursts. Now:
    settings-driven attempts with exponential backoff + jitter, and
    retries also cover connection errors, HTTP 5xx/429, and transient
    in-JSON ArcGIS 5xx/429. Geometry queries ask the server for
    generalized, precision-capped coordinates — cheaper to produce,
    smaller to ship, invisible at map zooms."""
    d = radius_meters / 111_320.0
    envelope = json.dumps({
        "xmin": lng - d, "ymin": lat - d,
        "xmax": lng + d, "ymax": lat + d,
        "spatialReference": {"wkid": 4326},
    })
    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": 4326 if return_geometry else "",
        "resultRecordCount": max_records,
        "f": "json",
    }
    if return_geometry:
        apply_geometry_generalization(params)
    data = arcgis_resilient_get(sess, url, params)
    time.sleep(0.5)
    features = data.get("features", [])
    if return_geometry:
        return features
    return [f["attributes"] for f in features]


def apply_geometry_generalization(params: dict) -> None:
    """Ask the server for generalized, precision-capped coordinates —
    cheaper for Esri to produce, smaller to ship, invisible at map
    zooms (defaults: 6 decimals ≈ 0.11 m, offset ≈ 1.1 m)."""
    settings = get_settings()
    params["geometryPrecision"] = settings.txdot_arcgis_geometry_precision
    offset = settings.txdot_arcgis_max_allowable_offset_deg
    if offset > 0:
        params["maxAllowableOffset"] = offset


def arcgis_resilient_get(
    sess: requests.Session,
    url: str,
    params: dict,
) -> dict:
    """Resilient GET against an ArcGIS endpoint: settings-driven attempt
    count, exponential backoff + jitter between attempts, retrying
    timeouts, connection errors, HTTP 5xx/429, and transient in-JSON
    ArcGIS 5xx/429 errors. Non-transient errors (bad query, token
    required) raise immediately. Delegates to the shared resilient HTTP
    engine with the TxDOT-tuned settings (90s per-attempt: the heavy
    AADT scan legitimately takes 50-90s in its slow mode)."""
    settings = get_settings()
    return resilient_json(
        sess,
        "GET",
        url,
        params=params,
        timeout=settings.txdot_arcgis_timeout_seconds,
        attempts=settings.txdot_arcgis_attempts,
        backoff_base=settings.txdot_arcgis_backoff_base_seconds,
        json_error=arcgis_json_error,
    )


def format_hwy_name(raw: str, prefix: str = "") -> str:
    """Convert TxDOT highway codes to readable names.
    'IH0035' → 'IH 35'; 'US0183' → 'US 183'; 'BI0035J' → 'BI 35J'.

    Two-letter route prefixes covered:
      IH/US/SH/SL/SS/FM/RM/PR  state-system mainlines
      BI/BU/BS/BF              business routes (Business I-35, etc.)
      LP                       district loops
    legacy_services format_hwy_name only covered the first 7;
    everything else fell through to the raw code (e.g. BI0035J
    rendered as 'BI0035J' in the dashboard). The list here is
    the union of every prefix observed in the Roadway Inventory
    layer in the smoke matrix runs.
    """
    raw = (raw or "").strip()
    if not raw:
        return prefix or "Unknown"
    for pfx in (
        "IH", "US", "SH", "SL", "SS", "FM", "RM", "PR",
        "BI", "BU", "BS", "BF", "LP",
    ):
        if raw.upper().startswith(pfx):
            rest = raw[len(pfx):]
            # Trailing alpha (segment letter, e.g. "0035J") is
            # significant — preserve it. Strip leading zeros from
            # the numeric portion only.
            i = 0
            while i < len(rest) and rest[i].isdigit():
                i += 1
            num_part = rest[:i].lstrip("0") or "0"
            tail = rest[i:]
            # TxDOT sometimes appends internal district/segment
            # suffixes to public routes (e.g. "FM0455-KG").
            # That suffix is useful to TxDOT, not to an owner
            # reading the project page. Keep true business-route
            # alpha tails like BI0035J; drop dash suffixes.
            if tail.startswith("-"):
                tail = ""
            return f"{pfx} {num_part}{tail}"
    if raw.isdigit():
        if prefix and prefix != "CS":
            return f"{prefix} {raw.lstrip('0') or raw}"
        return f"TxDOT local segment {raw}"
    return raw


def format_epoch(val: Any) -> str:
    """ArcGIS epoch ms → 'YYYY-MM' string."""
    if not val:
        return "N/A"
    if isinstance(val, str):
        return val
    try:
        dt = datetime.fromtimestamp(val / 1000, tz=UTC)
        return dt.strftime("%Y-%m")
    except Exception:
        return str(val)


__all__ = [
    "_MILES_TO_METERS",
    "_TIMEOUT_S",
    "_TXDOT_AADT",
    "_TXDOT_BRIDGES",
    "_TXDOT_PROJECTS",
    "_TXDOT_ROADWAY",
    "arcgis_spatial_query",
    "format_epoch",
    "format_hwy_name",
]
