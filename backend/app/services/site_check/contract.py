"""Public-data adapter contract.

Lifts legacy_services/core/public_intel_contract.py verbatim and
extends with the `CoverageStatus` axis per
docs/specs/public_data.md §6.2 (the new axis Site Analysis
reads to decide whether to apply its `marginal` floor mechanic).

Two enums + one result model + one mapping helper. Per spec
Rule #7 — additional fields land as adapters need them, not
preemptively.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

# ── Source access status (lifted from legacy_services) ──────────────


class SourceAccessStatus(str, Enum):
    """How the source can be reached programmatically.

    Distinct axis from `DataConfidenceStatus` — never collapse
    these two. A source can be `PROGRAMMATIC_PUBLIC` (we can hit
    its API today) AND `screened_indicator` (the answer is
    indicative, not definitive). Both axes are needed for honest
    feasibility reporting.
    """

    PROGRAMMATIC_PUBLIC = "PROGRAMMATIC_PUBLIC"
    PROGRAMMATIC_PUBLIC_BUT_FRAGILE = "PROGRAMMATIC_PUBLIC_BUT_FRAGILE"
    JURISDICTION_NOT_COVERED = "JURISDICTION_NOT_COVERED"
    HTML_OR_PDF_ONLY = "HTML_OR_PDF_ONLY"
    FORBIDDEN_BY_SOURCE = "FORBIDDEN_BY_SOURCE"
    NO_USABLE_PUBLIC_SOURCE_FOUND = "NO_USABLE_PUBLIC_SOURCE_FOUND"


# ── Data confidence status (lifted from legacy_services) ────────────


class DataConfidenceStatus(str, Enum):
    """How confident we are in the result the source returned.

    `contradicted_by_uploaded_evidence` is load-bearing — it's
    what DomainGuard sets when uploaded project documents
    disagree with the public answer.

    `manual_verification_required` is the bridge to the
    upload-evidence pipeline.
    """

    VERIFIED_PUBLIC_FACT = "verified_public_fact"
    SCREENED_INDICATOR = "screened_indicator"
    CONTRADICTED_BY_UPLOADED_EVIDENCE = "contradicted_by_uploaded_evidence"
    UNKNOWN = "unknown"
    MANUAL_VERIFICATION_REQUIRED = "manual_verification_required"


# ── Coverage status (NEW per public_data.md §6.2) ────────────────


class CoverageStatus(str, Enum):
    """Distinct from `data_confidence_status` — answers
    "did the adapter even try, and could it?" rather than
    "how good is the answer?"

    Site Analysis (and any future downstream consumer) reads
    this to decide whether to apply the floor mechanic
    (cap use-fit at 'marginal' until evidence binds when
    coverage is not_yet_wired or manual_verification_required).
    """

    COVERED = "covered"
    PARTIAL = "partial"
    NOT_SUPPORTED_IN_JURISDICTION = "not_supported_in_jurisdiction"
    NOT_YET_WIRED = "not_yet_wired"
    MANUAL_VERIFICATION_REQUIRED = "manual_verification_required"


# ── Manual upload request (lifted from legacy_services) ─────────────


class ManualUploadRequest(BaseModel):
    """Surfaced on the adapter result when the source itself
    can't satisfy the gap and the user needs to upload evidence.

    The future upload-evidence binding system reads this to know
    which gap a freshly-uploaded document is supposed to satisfy.
    """

    document_types: list[str] = Field(
        description=(
            "Normalized doc types the upload-evidence pipeline "
            "knows about (e.g. 'will-serve letter', 'Phase I "
            "ESA', 'survey'). At least one entry."
        ),
    )
    reason: str = Field(
        description=(
            "Short product-facing prompt for why this upload is "
            "needed."
        ),
    )


# ── Adapter result ───────────────────────────────────────────────


class AdapterResult(BaseModel):
    """Every public-data adapter returns one of these.

    Lifts the legacy_services contract verbatim and adds the
    `coverage_status` axis required by Site Analysis (per
    public_data.md §6.2).
    """

    # ── V1 required ──────────────────────────────────────────
    source_id: str = Field(
        description=(
            "Stable identifier for this source, e.g. 'fema_nfhl', "
            "'austin_zoning', 'arcgis_places'. Dispatch key in "
            "the registry; join key when downstream features "
            "cross-reference findings."
        ),
    )
    source_access_status: SourceAccessStatus
    data_confidence_status: DataConfidenceStatus
    coverage_status: CoverageStatus
    normalized_facts: dict = Field(
        description=(
            "Source-specific normalized payload. Today's flood "
            "fetcher returns its v1 shape verbatim during port; "
            "as consumers move to read structured fields off the "
            "AdapterResult itself, the per-source dicts shrink."
        ),
    )

    # ── V1 optional ──────────────────────────────────────────
    result_schema_version: int | None = Field(default=None, ge=0)
    registry_version: str | None = Field(default=None)
    identifier_used: str | None = Field(default=None)
    fetched_at: datetime | None = Field(default=None)
    ttl_seconds: int | None = Field(default=None)
    raw_payload_ref: str | None = Field(default=None)
    domain_guard_signals: list[str] | None = Field(default=None)
    advisories: list[str] | None = Field(default=None)
    manual_upload_request: ManualUploadRequest | None = Field(default=None)
    errors: list[str] | None = Field(default=None)


# ── Locked mapping from current shipped taxonomy ────────────────


def map_legacy_status(
    legacy_status: str | None,
    *,
    screening_only: bool = False,
) -> DataConfidenceStatus:
    """Translate the legacy_services legacy `clear / mixed /
    unknown / contradicted` taxonomy to the new
    `DataConfidenceStatus` axis. Lifted from legacy_services
    contract — locked mapping.

    `screening_only` marks sources like wetlands NWI that are
    advisory by design even when they return a clear answer.
    """
    if legacy_status == "clear":
        if screening_only:
            return DataConfidenceStatus.SCREENED_INDICATOR
        return DataConfidenceStatus.VERIFIED_PUBLIC_FACT
    if legacy_status == "mixed":
        return DataConfidenceStatus.SCREENED_INDICATOR
    if legacy_status == "contradicted":
        return DataConfidenceStatus.CONTRADICTED_BY_UPLOADED_EVIDENCE
    return DataConfidenceStatus.UNKNOWN


__all__ = [
    "AdapterResult",
    "CoverageStatus",
    "DataConfidenceStatus",
    "ManualUploadRequest",
    "SourceAccessStatus",
    "map_legacy_status",
]
