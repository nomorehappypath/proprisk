"""<State or county>'s own source for the <slot> card (spec 2.6).

Copy this file to regions/<st>/sources/<slot>.py (statewide) or
regions/<st>/sources/<county>/<slot>.py (one county). The file name is the
card's slot name. It must return the facts that card reads (see
app/services/site_check/card_fields.py), plus provider_name, source_url and
data_as_of. Run scripts/check_region.sh <st> [<county>] to prove it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions.arcgis import query_near
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "<slot>"            # the card's slot name, same as this file's name
PROVIDER_NAME = "<Agency's full name>"
PROVIDER_SHORT = "<Short name>"  # shown in the card title, e.g. "CDOT"
VERSION = 1                      # raise it whenever what this file returns changes
LAYER = "https://<host>/arcgis/rest/services/<name>/FeatureServer/0"


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={"advisory": "Coordinates are required."},
            fetched_at=datetime.now(UTC),
        )
    rows = query_near(LAYER, identity.lat, identity.lng, 1609, session=session)
    facts = {
        # the facts the card reads, built from rows
        "provider_name": PROVIDER_NAME,
        "provider_short": PROVIDER_SHORT,
        "source_url": LAYER,
        "data_as_of": "unknown",   # a date, a year, a range, or "unknown"
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=facts,
        fetched_at=datetime.now(UTC),
    )
