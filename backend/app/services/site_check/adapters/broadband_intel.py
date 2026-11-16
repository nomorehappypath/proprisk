"""broadband_intel adapter — FCC NBM portal pointer.

Ported verbatim from legacy_services/core/public_intel_adapters/
broadband_intel.py (May 2026).

FCC blocks programmatic access to the National Broadband
Map AND its lat/lng → location_id lookup. Honest path: link
the operator to the search entry page + surface parcel
coords/address so they can paste them in. Always returns
HTML_OR_PDF_ONLY + MANUAL_VERIFICATION_REQUIRED.
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)

SOURCE_ID = "broadband_intel"

_NBM_SEARCH_ENTRY = (
    "https://broadbandmap.fcc.gov/location-summary/fixed"
)
_NBM_BULK_DOWNLOAD = (
    "https://broadbandmap.fcc.gov/data-download/nationwide-data"
)

_BROADBAND_UPLOAD_TYPES = [
    "FCC NBM Location Summary screenshot",
    "FCC NBM Location Summary PDF export",
    "carrier service availability email",
    "fiber-route map (per-carrier)",
]


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    advisory = (
        "FCC blocks programmatic access to the National "
        "Broadband Map AND its lat/lng → location_id "
        "lookup, so the only honest path is: open the FCC "
        "NBM search page, paste the parcel address (or "
        "lat/lng), and the viewer will return the official "
        "Location Summary — every fixed broadband provider "
        "+ advertised speeds the FCC has on file. For "
        "office / life-science / data-center deals where "
        "broadband is a hard underwriting factor, also "
        "email the carriers directly to confirm building "
        "service + actual delivered speeds."
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "manual_verification_required",
            "fcc_nbm_search_url": _NBM_SEARCH_ENTRY,
            "fcc_nbm_bulk_download_url": _NBM_BULK_DOWNLOAD,
            "parcel_lat": identity.lat,
            "parcel_lng": identity.lng,
            "parcel_address": identity.address,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_BROADBAND_UPLOAD_TYPES),
            reason=(
                "Broadband data is available publicly via "
                "FCC NBM but the lat/lng → location_id "
                f"lookup isn't open. Open {_NBM_SEARCH_ENTRY}, "
                "paste the parcel address, then screenshot / "
                "export the Location Summary PDF."
            ),
        ),
    )


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "fcc_nbm_search_url": _NBM_SEARCH_ENTRY,
            "fcc_nbm_bulk_download_url": _NBM_BULK_DOWNLOAD,
            "parcel_lat": None,
            "parcel_lng": None,
            "parcel_address": None,
            "advisory": (
                "Open the FCC NBM search page and enter the "
                "parcel address — the viewer will return the "
                "Location Summary with the provider list and "
                "advertised speeds."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_BROADBAND_UPLOAD_TYPES),
            reason=(
                "Search the FCC NBM viewer at "
                f"{_NBM_SEARCH_ENTRY} by address."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
