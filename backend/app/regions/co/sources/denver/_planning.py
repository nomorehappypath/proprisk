"""Shared bits for Denver's planning source modules (zoning, plan, cases, historic).

Not a source itself (the leading underscore keeps `pick` from loading it).
All Denver Open Data layers below live in one hosted ArcGIS service folder.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.services.site_check.adapters._base import http_session
from app.services.site_check.adapters._resilient_http import (
    arcgis_json_error,
    resilient_json,
)
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)

log = logging.getLogger(__name__)

BASE = "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services"
PROVIDER_NAME = "City and County of Denver"
PROVIDER_SHORT = "Denver"
ZONING_CODE_URL = (
    "https://denvergov.org/Government/Agencies-Departments-Offices/"
    "Agencies-Departments-Offices-Directory/Community-Planning-and-Development/"
    "Denver-Zoning-Code"
)


def layer(name: str, layer_id: int) -> str:
    return f"{BASE}/{name}/FeatureServer/{layer_id}"


def ms_date(value: Any) -> str | None:
    """An ArcGIS epoch-millisecond date as YYYY-MM-DD (None when unusable)."""
    try:
        return datetime.fromtimestamp(float(value) / 1000, UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _layer_meta(url: str, session) -> dict:
    return resilient_json(
        http_session(session), "GET", url, params={"f": "json"}, json_error=arcgis_json_error,
    )


def data_as_of(url: str, session=None) -> str:
    """When the city last edited the layer's data; "unknown" if it cannot be read.
    Never the fetch time."""
    try:
        edited = ((_layer_meta(url, session).get("editingInfo") or {}).get("dataLastEditDate"))
    except Exception as exc:  # the date is a label; the data is still good
        log.warning("Denver layer metadata unavailable: %s", exc)
        return "unknown"
    return ms_date(edited) or "unknown"


def text(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def source_facts(module_url: str, as_of: str) -> dict[str, Any]:
    return {
        "provider_name": PROVIDER_NAME,
        "provider_short": PROVIDER_SHORT,
        "source_url": module_url,
        "data_as_of": as_of,
    }


def no_coordinates(source_id: str, facts: dict[str, Any]) -> AdapterResult:
    return AdapterResult(
        source_id=source_id,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={**facts, "status": "unknown", "advisory": "Coordinates are required."},
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required"],
    )


def failed(source_id: str, facts: dict[str, Any], what: str, exc: Exception,
           upload_types: list[str] | None = None) -> AdapterResult:
    """A provider error: said plainly, never turned into 'nothing found'."""
    message = f"{what} unavailable: {str(exc)[:200]}"
    return AdapterResult(
        source_id=source_id,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            **facts, "status": "unknown", "advisory": message, "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[message],
        manual_upload_request=(
            ManualUploadRequest(document_types=upload_types, reason=message)
            if upload_types else None
        ),
    )
