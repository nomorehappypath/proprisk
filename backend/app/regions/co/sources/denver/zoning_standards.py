"""Denver's zoning standards for the zoning-standards card (spec 2.6).

Same curated-standards engine as Texas, fed by `curated/denver.yaml` (numbers
copied from the Denver Zoning Code, each with its section). The district comes
from the zoning source's district code. The file's hash is part of VERSION, so
a saved result is reused only while the file is unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.regions.co.sources.denver import _planning as p
from app.services.site_check.adapters import zoning_standards as _std
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)
from app.services.site_check.zoning_standards import engine

SOURCE_ID = "zoning_standards"
PROVIDER_NAME = p.PROVIDER_NAME
PROVIDER_SHORT = p.PROVIDER_SHORT
CITY = "denver"
CODE_AS_OF = "2025-02-25"  # the Denver Zoning Code edition the file was copied from
_FILE = engine._CURATED_DIR / f"{CITY}.yaml"
VERSION = f"1-{hashlib.sha256(_FILE.read_bytes()).hexdigest()[:8]}"


def fetch(identity, session=None, config=None) -> AdapterResult:
    cfg = config or {}
    district = (cfg.get("district") or "").strip().upper() or None
    common = {
        **p.source_facts(p.ZONING_CODE_URL, CODE_AS_OF),
        "code_url": p.ZONING_CODE_URL,
        "manual_lookup_url": p.ZONING_CODE_URL,
    }
    if not district:
        result = _std._no_district_result(CITY)
    else:
        rules = engine.get_district_rules(CITY, district)
        if rules is None:
            result = _std._district_not_curated_result(CITY, district)
        else:
            use = None
            if cfg.get("intended_use"):
                status, entry = engine.is_use_permitted(CITY, district, cfg["intended_use"])
                use = {"use_id": cfg["intended_use"], "status": status or "not_listed",
                       "code_ref": entry.code_ref if entry else None,
                       "notes": entry.notes if entry else None}
            advisory = _std._build_advisory(CITY, district, rules, use)
            result = AdapterResult(
                source_id=SOURCE_ID,
                source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
                data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
                coverage_status=CoverageStatus.COVERED,
                normalized_facts={
                    "status": "clear",
                    "city": CITY,
                    "district": district,
                    "district_full_name": rules.full_name,
                    "district_code_section": rules.code_section,
                    "use_query": use,
                    "dimensional_standards": {
                        k: {"value": v.value, "unit": v.unit, "code_ref": v.code_ref, "notes": v.notes}
                        for k, v in rules.dimensional_standards.items()
                    },
                    "parking": [],
                    "overlay_interactions": [],
                    "advisory": advisory,
                    "contradiction_flags": [],
                },
                identifier_used="lat_lng",
                fetched_at=datetime.now(UTC),
                advisories=[advisory],
                manual_upload_request=ManualUploadRequest(
                    document_types=list(_std._ZONING_UPLOAD_TYPES),
                    reason="Curated rules cite the Denver Zoning Code; a zoning verification "
                           "letter from the city is the binding answer.",
                ),
            )
    result.normalized_facts = {**result.normalized_facts, **common}
    return result
