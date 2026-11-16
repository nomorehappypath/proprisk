"""tax_intel adapter — TX Comptroller HB 1378 debt-report links.

Ported verbatim from legacy_services/core/public_intel_adapters/
tax_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Reads `district_names` from the orchestrator's `config` arg
(the orchestrator pulls them out of district_overlays.normalized_facts
after that adapter runs). For each district, fuzzy-matches
against the HB 1378 Socrata index and surfaces the per-
entity debt-report URL.

Confidence always SCREENED_INDICATOR — name matching is
fuzzy, and actual debt numbers live INSIDE the linked PDFs
(not in any queryable column).
"""

from __future__ import annotations

import logging
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

log = logging.getLogger("propdesk.site_check.tax_intel")

SOURCE_ID = "tax_intel"

_HB1378_LATEST = "https://data.texas.gov/resource/u3nh-2phm.json"
_HB1378_FY = "2023"
_COMPTROLLER_DEBT_PORTAL = (
    "https://comptroller.texas.gov/transparency/local/hb1378/"
)

_TIMEOUT = 25
_MAX_PER_DISTRICT = 3

_TAX_UPLOAD_TYPES = [
    "local government debt report (HB 1378 PDF)",
    "MUD bond report",
    "TIRZ debt schedule",
    "ISD debt schedule",
    "tax bill (parcel-specific)",
]


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config: dict | None = None,
) -> AdapterResult:
    """`config` shape (passed by the orchestrator after
    district_overlays runs):
      {"district_names": ["Travis County MUD #17", "Round Rock ISD"]}
    Empty / missing → returns "no_districts_to_query"."""
    cfg = config or {}
    district_names = cfg.get("district_names") or []
    if not isinstance(district_names, list):
        district_names = []

    if not district_names:
        return _no_districts_result()

    sess = http_session(session)

    matched_per_district: list[dict[str, Any]] = []
    errors: list[str] = []

    for name in district_names:
        if not name or not isinstance(name, str):
            continue
        candidates, error = _match_entity(sess, name)
        if error:
            errors.append(f"{name}: {error}")
            continue
        matched_per_district.append({
            "district_name": name,
            "matches": candidates[:_MAX_PER_DISTRICT],
        })

    advisory = _build_advisory(matched_per_district, errors)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": (
                "clear" if matched_per_district else "no_matches"
            ),
            "fiscal_year": _HB1378_FY,
            "districts_queried": len(district_names),
            "districts_matched": sum(
                1 for d in matched_per_district
                if d.get("matches")
            ),
            "results": matched_per_district,
            "comptroller_portal_url": _COMPTROLLER_DEBT_PORTAL,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=ManualUploadRequest(
            document_types=list(_TAX_UPLOAD_TYPES),
            reason=(
                "TX Comptroller HB 1378 publishes the index "
                "of debt-reporting entities + a link to each "
                "entity's debt-report PDF. Download each report "
                "linked above and upload the relevant ones."
            ),
        ),
    )


def _match_entity(
    session: requests.Session, name: str,
) -> tuple[list[dict[str, Any]], str | None]:
    norm_name = _normalize(name)
    if not norm_name:
        return [], None
    try:
        search_term = _build_search_term(name)
        params = {"$q": search_term, "$limit": 25}
        resp = session.get(
            _HB1378_LATEST, params=params, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            return [], "unexpected response shape"
        out: list[dict[str, Any]] = []
        for r in rows:
            ent_name = r.get("entity_name") or ""
            score = _name_score(name, ent_name)
            if score <= 0:
                continue
            url_raw = r.get("debt_report_url")
            if isinstance(url_raw, dict):
                url = url_raw.get("url") or ""
            else:
                url = url_raw or ""
            out.append({
                "entity_name": ent_name,
                "entity_type": r.get("entity_type") or "",
                "city": r.get("city") or "",
                "address": r.get("address_1") or "",
                "fiscal_year": r.get("fiscal_year") or "",
                "debt_report_url": url,
                "match_score": score,
            })
        out.sort(key=lambda x: -x["match_score"])
        return out, None
    except Exception as exc:
        log.warning("HB 1378 lookup for %s failed: %s", name, exc)
        return [], str(exc)[:200]


def _normalize(s: str) -> str:
    if not s:
        return ""
    out = s.lower().strip()
    for c in (",", ".", "  ", "(", ")", "&", "/"):
        out = out.replace(c, " ")
    return " ".join(out.split())


def _build_search_term(name: str) -> str:
    norm = _normalize(name)
    noise = {"county", "the", "of", "city", "town", "no", "number"}
    tokens = [t for t in norm.split() if t not in noise]
    return " ".join(tokens) or norm


def _name_score(query: str, candidate: str) -> int:
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 100
    if q in c:
        return 80
    if c in q:
        return 60
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if not q_tokens or not c_tokens:
        return 0
    overlap = len(q_tokens & c_tokens)
    if overlap >= 2:
        return 40 + 5 * overlap
    return 0


def _build_advisory(
    results: list[dict[str, Any]], errors: list[str],
) -> str:
    if not results:
        return (
            "No districts to query — district_overlays "
            "didn't return MUD/WCID/PID/TIRZ/school names "
            "for this parcel."
        )
    matched_count = sum(1 for d in results if d.get("matches"))
    parts = [
        (f"{matched_count} of {len(results)} districts matched "
        f"to HB 1378 entries (FY {_HB1378_FY}). Each match "
        "links to the entity's debt-report PDF; the actual "
        "outstanding-debt numbers are inside the PDF, not "
        "in the index.")
    ]
    if matched_count < len(results):
        parts.append(
            "Unmatched districts may be sub-units, recently "
            "renamed, or below the HB 1378 reporting "
            "threshold — search the Comptroller portal "
            "directly: " + _COMPTROLLER_DEBT_PORTAL
        )
    if errors:
        parts.append(
            f"{len(errors)} district lookup(s) errored — "
            "retry or check the portal."
        )
    return " ".join(parts)


def _no_districts_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "no_districts_to_query",
            "fiscal_year": _HB1378_FY,
            "districts_queried": 0,
            "districts_matched": 0,
            "results": [],
            "comptroller_portal_url": _COMPTROLLER_DEBT_PORTAL,
            "advisory": (
                "No special taxing districts (MUD, WCID, "
                "PID, TIRZ, ISD) reported by district_"
                "overlays for this parcel. If a district "
                "is suspected, search the Comptroller "
                "portal directly."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_TAX_UPLOAD_TYPES),
            reason=(
                "No district names available to query. "
                "Search the Comptroller portal at "
                f"{_COMPTROLLER_DEBT_PORTAL} and upload "
                "the entity's debt report if a district "
                "is found."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
