"""plats_intel adapter — active subdivision plats + public plat discovery.

Ported verbatim from legacy_services/core/public_intel_adapters/
plats_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Hays has active-subdivision context plus parcel-level subdivision screening;
Travis, Williamson, Bastrop, and Caldwell have parcel-level subdivision
screens. Covered county portals get explicit manual source + instructions.
The adapter also has a public-discovery pass that can surface probable
recorded-plat document numbers from unauthenticated public web/PDF evidence;
those candidates are not authoritative clerk copies and do not satisfy
evidence requirements by themselves.
"""

from __future__ import annotations

import html
import io
import json
import logging
import math
import re
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import requests
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.services.site_check import county_clerk_portals
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

log = logging.getLogger("propdesk.site_check.plats_intel")

SOURCE_ID = "plats_intel"

_HAYS_ACTIVE_SUBDIVISIONS_QUERY = (
    "https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/rest/"
    "services/Active_Subdivisions/FeatureServer/0/query"
)
_HAYS_SUBDIVISIONS_LAYER = (
    "https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/rest/"
    "services/Subdivisions/FeatureServer/4"
)
_HAYS_SUBDIVISIONS_QUERY = f"{_HAYS_SUBDIVISIONS_LAYER}/query"
_HAYS_SUBDIVISIONS_SOURCE = "Hays County GIS Hub (subdivisions)"
_TRAVIS_SUBDIVISIONS_LAYER = (
    "https://gis.traviscountytx.gov/server1/rest/services/"
    "Boundaries_and_Jurisdictions/Travis_County_Subdivisions/"
    "MapServer/0"
)
_TRAVIS_SUBDIVISIONS_QUERY = f"{_TRAVIS_SUBDIVISIONS_LAYER}/query"
_TRAVIS_SUBDIVISIONS_SOURCE = "Travis County GIS (Travis County Subdivisions)"
_WILLIAMSON_SUBDIVISIONS_LAYER = (
    "https://gis.wilco.org/arcgis/rest/services/public/"
    "county_subdivisions/MapServer/0"
)
_WILLIAMSON_SUBDIVISIONS_QUERY = f"{_WILLIAMSON_SUBDIVISIONS_LAYER}/query"
_WILLIAMSON_SUBDIVISIONS_SOURCE = "Williamson County GIS (county_subdivisions)"
_BASTROP_SUBDIVISIONS_LAYER = (
    "https://maps.co.bastrop.tx.us/server/rest/services/Viewers/"
    "PublicGISViewerService/MapServer/13"
)
_BASTROP_SUBDIVISIONS_QUERY = f"{_BASTROP_SUBDIVISIONS_LAYER}/query"
_BASTROP_SUBDIVISIONS_SOURCE = (
    "Bastrop County Public GIS Viewer (subdivisions)"
)
_CALDWELL_SUBDIVISIONS_LAYER = (
    "https://services.arcgis.com/rVxY74DxxIDrDbc0/arcgis/rest/services/"
    "Caldwell_CAD_Parcel_Map/FeatureServer/13"
)
_CALDWELL_SUBDIVISIONS_QUERY = f"{_CALDWELL_SUBDIVISIONS_LAYER}/query"
_CALDWELL_SUBDIVISIONS_SOURCE = (
    "Caldwell CAD Parcel Map (subdivisions)"
)

# Per-county clerk plat-search portals now come from the canonical
# `county_clerk_portals` module via `clerk_lookup_steps(county, "plat")` — one
# source of truth (see docs/specs/recorded_deed_resolution.md §4). The returned
# record exposes the same `name` / `url` / `instructions` keys the plat
# call-sites read, with the plat-specific URL override (e.g. Bastrop kofile)
# and plat-oriented instructions.

_TIMEOUT = 25
_DISCOVERY_TIMEOUT = 15
_MAX_SOURCE_BYTES = 8_000_000
_MAX_DISCOVERY_QUERIES = 12
_MAX_SEARCH_RESULTS = 12
_DEFAULT_BUFFER_MI = 2.0

_PLATS_UPLOAD_TYPES = [
    "recorded plat",
    "preliminary plat",
    "subdivision agenda packet",
    "city subdivision review correspondence",
]

_IN_REVIEW_STATUSES = {
    "TECHNICAL REVIEW", "PRELIMINARY", "PENDING", "UNDER REVIEW",
}

_RECORD_NUMBER_RE = re.compile(
    r"\b(?:doc(?:ument)?|instrument|inst)\s*"
    r"(?:no\.?|number|#)?\s*[:#]?\s*(?P<number>20\d{7,10})\b"
    r"|\bdoc\s*#\s*(?P<doc_hash>20\d{7,10})\b",
    re.IGNORECASE,
)
_LEGAL_PLAT_RE = re.compile(
    r"""
    (?:
        ^\s*(?:S\d+\s*-\s*)?
    )?
    (?P<subdivision>[A-Z0-9 .&'/\-]+?)
    (?:\s*\((?P<modifier>AMD|AMENDED|REPLAT)\))?
    \s*,\s*
    BLOCK\s+(?P<block>[^,.;]+?)
    \s*,\s*
    LOT\s+(?P<lot>[^,.;]+?)
    (?:\s*[,.;]|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PDF_MIME_TYPES = ("application/pdf", "application/octet-stream")
_BING_RESULT_RE = re.compile(
    r"<li\b[^>]*class=\"[^\"]*\bb_algo\b[^\"]*\"[^>]*>.*?"
    r"<h2[^>]*>\s*<a[^>]+href=\"(?P<href>[^\"]+)\"[^>]*>"
    r"(?P<title>.*?)</a>.*?</li>",
    re.IGNORECASE | re.DOTALL,
)
_BING_SNIPPET_RE = re.compile(
    r"<p\b[^>]*>(?P<snippet>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""


class SearchProvider(Protocol):
    def search(
        self,
        queries: list[str],
        *,
        max_results: int = _MAX_SEARCH_RESULTS,
    ) -> list[SearchResult]:
        ...


@dataclass
class PlatClue:
    subdivision_name: str | None = None
    block: str | None = None
    lot: str | None = None
    legal_description: str | None = None
    parcel_id: str | None = None
    address: str | None = None

    def to_public(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "subdivision_name": self.subdivision_name,
                "block": self.block,
                "lot": self.lot,
                "legal_description": self.legal_description,
                "parcel_id": self.parcel_id,
                "address": self.address,
            }.items()
            if isinstance(value, str) and value.strip()
        }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    buffer_mi: float = _DEFAULT_BUFFER_MI,
) -> AdapterResult:
    """Run the plat lookup, then attach `recorded_deed` for UNPLATTED parcels
    (per docs/specs/recorded_deed_resolution.md). Thin wrapper so the intricate
    per-county plat paths in `_fetch_impl` stay untouched."""
    result = _fetch_impl(
        identity, session=session, config=config, buffer_mi=buffer_mi,
    )
    try:
        deed = _resolve_recorded_deed(identity, config, result.normalized_facts)
        if deed is not None:
            result.normalized_facts["recorded_deed"] = deed
    except Exception:
        log.warning("recorded_deed resolution failed", exc_info=True)
    return result


def _fetch_impl(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    buffer_mi: float = _DEFAULT_BUFFER_MI,
) -> AdapterResult:
    county = (identity.county or "").lower().strip()
    sess = http_session(session)
    discovery = _discover_public_recorded_plats(
        sess,
        identity,
        county=county,
        config=config,
    )

    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result(discovery=discovery)

    if county == "hays":
        return _fetch_hays(
            sess,
            identity,
            buffer_mi,
            config=config,
            discovery=discovery,
        )
    if county == "travis":
        return _fetch_travis(sess, identity, config=config, discovery=discovery)
    if county == "williamson":
        return _fetch_williamson(
            sess, identity, config=config, discovery=discovery,
        )
    if county in {"bastrop", "caldwell"}:
        return _fetch_county_subdivision_screen(
            sess,
            identity,
            county=county,
            config=config,
            discovery=discovery,
            active_context=None,
        )
    if county_clerk_portals.clerk_lookup_steps(county, "plat") is not None:
        return _portal_result(county, discovery=discovery)
    return _no_coverage_result(county, discovery=discovery)


# ── Recorded-deed resolution for unplatted parcels ────────────────────
# Per docs/specs/recorded_deed_resolution.md. CAD parcel fields that carry a
# recorded-deed instrument/document number; a DATE alone never reaches Tier 1.
_DEED_INSTRUMENT_FIELDS = (
    "deed_number", "deed_instrument", "deed_instrument_number",
    "recorded_deed_number", "instrument_number", "document_number",
)
_DEED_DATE_FIELDS = ("deed_date", "recording_date", "recorded_date")
_CAD_SOURCE_LABELS = {"tcad_parcel": "TCAD", "wcad_parcel": "WCAD"}
# A parcel is treated as UNPLATTED only on a POSITIVE determination that
# plats_intel screened it against a recorded-subdivision source and found no
# intersecting recorded subdivision plat. This is an ALLOWLIST, not a
# "anything-without-candidates" check: indeterminate / error / pointer states
# (unknown, fetch_failed, insufficient_identity, manual_verification_required,
# no-coordinate) must NEVER yield a deed pointer — claiming "Unplatted —
# abstract-survey tract" when we did not determine it is wrong (review
# 2026-06-01). Counties emit this status only after an equivalent
# parcel-level subdivision GIS screen determines no plat/subdivision match
# (spec §8 — Tier 1/2 is per-county, not universal-on-any-state).
# County subdivision screens must be parcel-specific; nearby/background plat
# context is not enough to unlock the recorded-deed pointer.
_UNPLATTED_STATUSES = frozenset({
    "no_recorded_subdivision_gis_match",
})


def _is_unplatted(facts: dict[str, Any]) -> bool:
    """True ONLY on a positive screened "no recorded subdivision plat"
    determination (`_UNPLATTED_STATUSES`). Never True for indeterminate,
    error, or manual-pointer states, and never when discovery still surfaced a
    probable plat number / candidate (then the parcel is probably platted)."""
    if str(facts.get("status") or "") not in _UNPLATTED_STATUSES:
        return False
    if facts.get("probable_plat_number"):
        return False
    return not facts.get("record_candidates")


def _format_deed_date(value: Any) -> str | None:
    """Normalize a deed/recording date to ISO YYYY-MM-DD. Accepts epoch ms
    (TCAD, e.g. '1641794400000'), or returns the trimmed string otherwise."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit() and len(value) >= 12
    ):
        try:
            return datetime.fromtimestamp(
                int(value) / 1000.0, tz=UTC,
            ).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return str(value).strip() or None


def _legal_description_text(facts: dict[str, Any]) -> str | None:
    identity_block = facts.get("plat_identity")
    if isinstance(identity_block, dict):
        legal = identity_block.get("legal_description")
        if isinstance(legal, str) and legal.strip():
            return legal.strip()
    clue_block = facts.get("plat_clues")
    if isinstance(clue_block, dict):
        legal = clue_block.get("legal_description")
        if isinstance(legal, str) and legal.strip():
            return legal.strip()
    return None


def _owner_from_cad(cad: dict[str, Any] | None) -> str | None:
    if not isinstance(cad, dict):
        return None
    for field in ("owner_name", "owner", "owner_name_1", "ownername"):
        val = cad.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_recorded_deed(
    identity: PropertyIdentity,
    config: Any,
    facts: dict[str, Any],
) -> dict[str, Any] | None:
    """Recorded-deed reference for an UNPLATTED parcel — Tier 1 (instrument #
    found in a CAD parcel adapter) or Tier 2 (per-county clerk pointer). None
    when the parcel is platted or the county has no configured clerk portal.
    Per docs/specs/recorded_deed_resolution.md §4."""
    if not isinstance(facts, dict) or not _is_unplatted(facts):
        return None
    county = (identity.county or "").lower().strip().removesuffix(" county").strip()
    portal = county_clerk_portals.clerk_lookup_steps(county, "deed")
    if portal is None:
        return None

    base = {
        "confidence": "screened_indicator",
        "verification_status": "clerk_image_required",
        "county": county,
        "clerk_portal_name": portal.get("name"),
        "clerk_portal_url": portal.get("url"),
        "clerk_fallback_url": portal.get("fallback_url"),
    }

    # Tier 1 — scan ONLY CAD parcel adapters for an instrument/document number.
    upstream = config.get("upstream_results") if isinstance(config, dict) else None
    cad_ids = county_clerk_portals.cad_source_ids_for_county(county) or [
        "tcad_parcel", "wcad_parcel",
    ]
    cad_facts_for_owner: dict[str, Any] | None = None
    if isinstance(upstream, dict):
        for sid in cad_ids:
            raw = upstream.get(sid)
            cad = raw.get("normalized_facts") if isinstance(raw, dict) else None
            if not isinstance(cad, dict):
                continue
            cad_facts_for_owner = cad_facts_for_owner or cad
            number_field = next(
                (f for f in _DEED_INSTRUMENT_FIELDS
                 if isinstance(cad.get(f), (str, int)) and str(cad.get(f)).strip()),
                None,
            )
            if number_field is None:
                continue
            date_field = next(
                (f for f in _DEED_DATE_FIELDS if cad.get(f) not in (None, "")),
                None,
            )
            instrument = str(cad[number_field]).strip()
            return {
                **base,
                "tier": "instrument_resolved",
                "instrument_number": instrument,
                "recorded_date": (
                    _format_deed_date(cad.get(date_field)) if date_field else None
                ),
                "source_id": sid,
                "source_label": _CAD_SOURCE_LABELS.get(sid, sid),
                "source_fields": [number_field] + ([date_field] if date_field else []),
                "source_field": number_field,
                "lookup_steps": (
                    f"Open {portal.get('name')} and search Document / Instrument # "
                    f"{instrument} to view or order the recorded deed."
                ),
            }

    # Tier 2 — clerk pointer (no instrument # in our data).
    return {
        **base,
        "tier": "clerk_lookup",
        "instrument_number": None,
        "recorded_date": None,
        "source_id": None,
        "source_field": None,
        "lookup_steps": portal.get("instructions"),
        "lookup_inputs": {
            "owner": _owner_from_cad(cad_facts_for_owner),
            "legal_description": _legal_description_text(facts),
            "parcel_id": identity.parcel_id,
        },
    }


def _fetch_hays(
    session: requests.Session,
    identity: PropertyIdentity,
    buffer_mi: float,
    *,
    config: Any,
    discovery: dict[str, Any],
) -> AdapterResult:
    features, error = _query_layer(
        session, _HAYS_ACTIVE_SUBDIVISIONS_QUERY,
        identity.lat, identity.lng, buffer_mi,
    )
    plats = [] if error else _summarize_plats(
        features, identity.lat, identity.lng,
    )
    in_review = [
        p for p in plats
        if (p.get("status") or "").upper() in _IN_REVIEW_STATUSES
    ]
    approved = [
        p for p in plats
        if "APPROVED" in (p.get("status") or "").upper()
    ]
    advisory = (
        f"Hays County active subdivision feed failed: {error}"
        if error else
        _build_advisory_hays(plats, in_review, approved, buffer_mi)
    )
    active_context = {
        "search_radius_mi": buffer_mi,
        "plats_count": len(plats),
        "in_review_count": len(in_review),
        "approved_count": len(approved),
        "in_review": in_review[:10],
        "approved": approved[:10],
        "all_plats": plats[:20],
        "hays_active_subdivision_status": (
            "fetch_failed"
            if error else
            "active_plats_in_buffer"
            if plats else
            "no_active_plats_in_buffer"
        ),
        **(
            {"hays_active_subdivision_error": error}
            if error else {}
        ),
        "hays_active_subdivision_matches": plats[:20],
        "hays_active_subdivision_source": (
            "Hays County GIS Hub (Active_Subdivisions)"
        ),
        "hays_active_subdivision_url": _HAYS_ACTIVE_SUBDIVISIONS_QUERY,
        "hays_active_subdivision_advisory": advisory,
    }

    screened = _fetch_county_subdivision_screen(
        session,
        identity,
        county="hays",
        config=config,
        discovery=discovery,
        active_context=active_context,
    )
    facts = screened.normalized_facts
    if facts.get("hays_subdivision_gis_status") not in (
        None,
        "no_parcel_polygon",
        "fetch_failed",
    ):
        return screened

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            if error else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=(
            DataConfidenceStatus.UNKNOWN
            if error else DataConfidenceStatus.SCREENED_INDICATOR
        ),
        coverage_status=CoverageStatus.PARTIAL if error else CoverageStatus.COVERED,
        normalized_facts={
            "status": "fetch_failed" if error else (
                "clear" if plats else "no_plats_in_buffer"
            ),
            "county": "hays",
            "search_radius_mi": buffer_mi,
            "plats_count": len(plats),
            "in_review_count": len(in_review),
            "approved_count": len(approved),
            "in_review": in_review[:10],
            "approved": approved[:10],
            "all_plats": plats[:20],
            **active_context,
            "hays_subdivision_gis_status": facts.get("hays_subdivision_gis_status"),
            "hays_subdivision_gis_source": _HAYS_SUBDIVISIONS_SOURCE,
            "hays_subdivision_gis_url": _HAYS_SUBDIVISIONS_LAYER,
            **discovery,
            "manual_next_step": _build_manual_next_step("hays", discovery),
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=([error] if error else None),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                "hays",
                discovery,
                default=(
                    "Plat status changes weekly. Pull the most recent "
                    "Hays P&Z agenda packet for any in-review plat that "
                    "affects the deal area. If a recorded-plat candidate "
                    "is shown, retrieve the official clerk PDF before "
                    "treating it as evidence."
                ),
            ),
        ),
    )


_COUNTY_SUBDIVISION_SCREENS = {
    "hays": {
        "layer_url": _HAYS_SUBDIVISIONS_LAYER,
        "query_url": _HAYS_SUBDIVISIONS_QUERY,
        "source_label": _HAYS_SUBDIVISIONS_SOURCE,
    },
    "bastrop": {
        "layer_url": _BASTROP_SUBDIVISIONS_LAYER,
        "query_url": _BASTROP_SUBDIVISIONS_QUERY,
        "source_label": _BASTROP_SUBDIVISIONS_SOURCE,
    },
    "caldwell": {
        "layer_url": _CALDWELL_SUBDIVISIONS_LAYER,
        "query_url": _CALDWELL_SUBDIVISIONS_QUERY,
        "source_label": _CALDWELL_SUBDIVISIONS_SOURCE,
    },
}


def _fetch_county_subdivision_screen(
    session: requests.Session,
    identity: PropertyIdentity,
    *,
    county: str,
    config: Any,
    discovery: dict[str, Any],
    active_context: dict[str, Any] | None,
) -> AdapterResult:
    screen = _COUNTY_SUBDIVISION_SCREENS[county]
    portal = county_clerk_portals.clerk_lookup_steps(county, "plat")
    cfg = config if isinstance(config, dict) else {}
    parcel_geometry = _arcgis_polygon_from_upstream_parcel(cfg)
    status_key = f"{county}_subdivision_gis_status"
    source_key = f"{county}_subdivision_gis_source"
    url_key = f"{county}_subdivision_gis_url"
    query_key = f"{county}_subdivision_gis_query"
    matches_key = f"{county}_subdivision_gis_matches"
    source_label = screen["source_label"]
    layer_url = screen["layer_url"]

    if parcel_geometry is None:
        enriched = {
            **(active_context or {}),
            **discovery,
            status_key: "no_parcel_polygon",
            source_key: source_label,
            url_key: layer_url,
        }
        return _portal_result(county, discovery=enriched)

    features, error = _query_subdivision_geometry(
        session,
        screen["query_url"],
        parcel_geometry,
    )
    if error:
        enriched = {
            **(active_context or {}),
            **discovery,
            status_key: "fetch_failed",
            f"{county}_subdivision_gis_error": error,
            source_key: source_label,
            url_key: layer_url,
        }
        return _portal_result(county, discovery=enriched)

    records = _summarize_county_subdivision_records(county, features)
    recorded_records = [
        record for record in records
        if _is_recorded_county_subdivision_record(county, record)
    ]
    gis_candidates = _county_subdivision_candidates_from_records(
        county,
        recorded_records,
    )
    existing_candidates = [
        item for item in (discovery.get("record_candidates") or [])
        if isinstance(item, dict)
    ]
    record_candidates = _dedupe_candidates([*gis_candidates, *existing_candidates])
    candidate_numbers = [
        str(candidate.get("document_number"))
        for candidate in record_candidates
        if isinstance(candidate, dict) and candidate.get("document_number")
    ]
    official_attempts = list(discovery.get("official_retrieval_attempts") or [])
    if gis_candidates:
        official_attempts = [
            *_attempt_official_retrievals(
                session,
                county=county,
                candidates=gis_candidates,
                config=cfg,
            ),
            *official_attempts,
        ]

    if gis_candidates:
        status = "recorded_plat_number_resolved"
    elif existing_candidates:
        status = "probable_recorded_plat_candidates_found"
    elif recorded_records:
        status = f"{county}_subdivision_gis_match_no_document"
    elif records:
        status = f"{county}_subdivision_gis_match_not_recorded"
    else:
        status = "no_recorded_subdivision_gis_match"

    enriched = {
        **(active_context or {}),
        **discovery,
        "public_discovery_status": discovery.get("public_discovery_status"),
        "record_candidates": record_candidates[:5],
        "probable_plat_number": candidate_numbers[0] if candidate_numbers else None,
        "probable_plat_numbers": candidate_numbers[:5],
        "official_retrieval_attempts": official_attempts[:5],
        status_key: (
            "recorded_plat_found"
            if gis_candidates else
            "platted_subdivision_match"
            if recorded_records else
            "subdivision_match_not_recorded"
            if records else
            "no_intersecting_recorded_subdivision"
        ),
        source_key: source_label,
        url_key: layer_url,
        query_key: "parcel_polygon",
        matches_key: records[:10],
    }
    if records:
        enriched["plat_identity"] = _county_subdivision_plat_identity(
            records[0],
            discovery.get("plat_clues") or {},
            source_label=source_label,
        )
    advisory = _build_advisory_county_subdivision_screen(
        county=county,
        source_label=source_label,
        status=status,
        records=records,
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": status,
            "county": county,
            "plats_count": len(records),
            "recorded_count": len(gis_candidates),
            "in_review_count": (active_context or {}).get("in_review_count", 0),
            "approved_count": len(recorded_records),
            "in_review": (active_context or {}).get("in_review", []),
            "approved": recorded_records[:10],
            "all_plats": records[:20],
            "portal_name": portal["name"],
            "portal_url": portal["url"],
            "manual_instructions": portal["instructions"],
            **enriched,
            "manual_next_step": _build_manual_next_step(
                county,
                enriched,
                portal=portal,
            ),
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="parcel_polygon",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                county,
                enriched,
                default=(
                    f"PropDesk queried {source_label} using the parcel "
                    "polygon. Retrieve any official recorded image/copy from "
                    f"{portal['name']} ({portal['url']}) before treating it "
                    "as evidence."
                ),
            ),
        ),
    )


def _fetch_travis(
    session: requests.Session,
    identity: PropertyIdentity,
    *,
    config: Any,
    discovery: dict[str, Any],
) -> AdapterResult:
    portal = county_clerk_portals.clerk_lookup_steps("travis", "plat")
    cfg = config if isinstance(config, dict) else {}
    features, error, identifier_used = _query_travis_subdivision_features(
        session,
        identity,
        config=cfg,
    )
    if error:
        enriched = {
            **discovery,
            "travis_subdivision_gis_status": "fetch_failed",
            "travis_subdivision_gis_error": error,
            "travis_subdivision_gis_source": _TRAVIS_SUBDIVISIONS_SOURCE,
            "travis_subdivision_gis_url": _TRAVIS_SUBDIVISIONS_LAYER,
        }
        return _portal_result("travis", discovery=enriched)

    travis_records = _summarize_travis_subdivision_records(features)
    gis_candidates = _travis_candidates_from_records(travis_records)
    existing_candidates = [
        item for item in (discovery.get("record_candidates") or [])
        if isinstance(item, dict)
    ]
    record_candidates = _dedupe_candidates([*gis_candidates, *existing_candidates])
    candidate_numbers = [
        str(candidate.get("document_number"))
        for candidate in record_candidates
        if isinstance(candidate, dict) and candidate.get("document_number")
    ]
    official_attempts = list(discovery.get("official_retrieval_attempts") or [])
    if gis_candidates:
        official_attempts = [
            *_attempt_official_retrievals(
                session,
                county="travis",
                candidates=gis_candidates,
                config=cfg,
            ),
            *official_attempts,
        ]

    if gis_candidates:
        status = "recorded_plat_number_resolved"
    elif existing_candidates:
        status = "probable_recorded_plat_candidates_found"
    elif travis_records:
        status = "travis_subdivision_gis_match_no_document"
    else:
        status = "no_recorded_subdivision_gis_match"

    enriched = {
        **discovery,
        "public_discovery_status": discovery.get("public_discovery_status"),
        "record_candidates": record_candidates[:5],
        "probable_plat_number": candidate_numbers[0] if candidate_numbers else None,
        "probable_plat_numbers": candidate_numbers[:5],
        "official_retrieval_attempts": official_attempts[:5],
        "travis_subdivision_gis_status": (
            "recorded_plat_found"
            if gis_candidates else
            "subdivision_match_no_document"
            if travis_records else
            "no_intersecting_recorded_subdivision"
        ),
        "travis_subdivision_gis_source": _TRAVIS_SUBDIVISIONS_SOURCE,
        "travis_subdivision_gis_url": _TRAVIS_SUBDIVISIONS_LAYER,
        "travis_subdivision_gis_query": identifier_used,
        "travis_subdivision_gis_matches": travis_records[:10],
    }
    if travis_records:
        enriched["plat_identity"] = _travis_plat_identity(
            travis_records[0],
            discovery.get("plat_clues") or {},
        )
    advisory = _build_advisory_travis(
        status=status,
        travis_records=travis_records,
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=(
            CoverageStatus.COVERED
            if gis_candidates or not existing_candidates else CoverageStatus.PARTIAL
        ),
        normalized_facts={
            "status": status,
            "county": "travis",
            "plats_count": len(travis_records),
            "recorded_count": len(gis_candidates),
            "in_review_count": 0,
            "approved_count": len(gis_candidates),
            "in_review": [],
            "approved": travis_records[:10],
            "all_plats": travis_records[:20],
            "portal_name": portal["name"],
            "portal_url": portal["url"],
            "manual_instructions": portal["instructions"],
            **enriched,
            "manual_next_step": _build_manual_next_step(
                "travis",
                enriched,
                portal=portal,
            ),
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used=identifier_used,
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                "travis",
                enriched,
                default=(
                    "PropDesk queried Travis County GIS recorded "
                    "subdivisions. If the survey or title commitment says "
                    "the parcel is platted, retrieve the official recorded "
                    f"plat image from {portal['name']} ({portal['url']})."
                ),
            ),
        ),
    )


def _fetch_williamson(
    session: requests.Session,
    identity: PropertyIdentity,
    *,
    config: Any,
    discovery: dict[str, Any],
) -> AdapterResult:
    portal = county_clerk_portals.clerk_lookup_steps("williamson", "plat")
    cfg = config if isinstance(config, dict) else {}
    parcel_geometry = _arcgis_polygon_from_upstream_parcel(cfg)
    if parcel_geometry is None:
        enriched = {
            **discovery,
            "williamson_subdivision_gis_status": "no_parcel_polygon",
            "williamson_subdivision_gis_source": _WILLIAMSON_SUBDIVISIONS_SOURCE,
            "williamson_subdivision_gis_url": _WILLIAMSON_SUBDIVISIONS_LAYER,
        }
        return _portal_result("williamson", discovery=enriched)

    features, error = _query_subdivision_geometry(
        session,
        _WILLIAMSON_SUBDIVISIONS_QUERY,
        parcel_geometry,
    )
    if error:
        enriched = {
            **discovery,
            "williamson_subdivision_gis_status": "fetch_failed",
            "williamson_subdivision_gis_error": error,
            "williamson_subdivision_gis_source": _WILLIAMSON_SUBDIVISIONS_SOURCE,
            "williamson_subdivision_gis_url": _WILLIAMSON_SUBDIVISIONS_LAYER,
        }
        return _portal_result("williamson", discovery=enriched)

    records = _summarize_williamson_subdivision_records(features)
    gis_candidates = _williamson_candidates_from_records(records)
    existing_candidates = [
        item for item in (discovery.get("record_candidates") or [])
        if isinstance(item, dict)
    ]
    record_candidates = _dedupe_candidates([*gis_candidates, *existing_candidates])
    candidate_numbers = [
        str(candidate.get("document_number"))
        for candidate in record_candidates
        if isinstance(candidate, dict) and candidate.get("document_number")
    ]
    official_attempts = list(discovery.get("official_retrieval_attempts") or [])
    if gis_candidates:
        official_attempts = [
            *_attempt_official_retrievals(
                session,
                county="williamson",
                candidates=gis_candidates,
                config=cfg,
            ),
            *official_attempts,
        ]

    if gis_candidates:
        status = "recorded_plat_number_resolved"
    elif existing_candidates:
        status = "probable_recorded_plat_candidates_found"
    elif records:
        status = "williamson_subdivision_gis_match_no_document"
    else:
        status = "no_recorded_subdivision_gis_match"

    enriched = {
        **discovery,
        "public_discovery_status": discovery.get("public_discovery_status"),
        "record_candidates": record_candidates[:5],
        "probable_plat_number": candidate_numbers[0] if candidate_numbers else None,
        "probable_plat_numbers": candidate_numbers[:5],
        "official_retrieval_attempts": official_attempts[:5],
        "williamson_subdivision_gis_status": (
            "recorded_plat_found"
            if gis_candidates else
            "subdivision_match_no_document"
            if records else
            "no_intersecting_recorded_subdivision"
        ),
        "williamson_subdivision_gis_source": _WILLIAMSON_SUBDIVISIONS_SOURCE,
        "williamson_subdivision_gis_url": _WILLIAMSON_SUBDIVISIONS_LAYER,
        "williamson_subdivision_gis_query": "parcel_polygon",
        "williamson_subdivision_gis_matches": records[:10],
    }
    if records:
        enriched["plat_identity"] = _williamson_plat_identity(
            records[0],
            discovery.get("plat_clues") or {},
        )
    advisory = _build_advisory_williamson(
        status=status,
        records=records,
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": status,
            "county": "williamson",
            "plats_count": len(records),
            "recorded_count": len(gis_candidates),
            "in_review_count": 0,
            "approved_count": len(gis_candidates),
            "in_review": [],
            "approved": records[:10],
            "all_plats": records[:20],
            "portal_name": portal["name"],
            "portal_url": portal["url"],
            "manual_instructions": portal["instructions"],
            **enriched,
            "manual_next_step": _build_manual_next_step(
                "williamson",
                enriched,
                portal=portal,
            ),
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="parcel_polygon",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                "williamson",
                enriched,
                default=(
                    "PropDesk queried Williamson County GIS recorded "
                    "subdivisions using the WCAD parcel polygon. Retrieve any "
                    f"official recorded image from {portal['name']} "
                    f"({portal['url']}) before treating it as evidence."
                ),
            ),
        ),
    )


def _query_travis_subdivision_features(
    session: requests.Session,
    identity: PropertyIdentity,
    *,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None, str]:
    parcel_geometry = _arcgis_polygon_from_upstream_parcel(config)
    if parcel_geometry is not None:
        features, error = _query_travis_subdivision_geometry(
            session,
            parcel_geometry,
        )
        return features, error, "parcel_polygon"
    if identity.lat is None or identity.lng is None:
        return [], "lat/lng required for Travis subdivision GIS lookup", "lat_lng"
    features, error = _query_layer(
        session,
        _TRAVIS_SUBDIVISIONS_QUERY,
        identity.lat,
        identity.lng,
        0.0,
    )
    return features, error, "lat_lng"


def _arcgis_polygon_from_upstream_parcel(
    config: dict[str, Any],
) -> dict[str, Any] | None:
    results = config.get("upstream_results")
    if not isinstance(results, dict):
        return None
    for source_id in ("tcad_parcel", "wcad_parcel"):
        raw = results.get(source_id)
        facts = raw.get("normalized_facts") if isinstance(raw, dict) else None
        polygon = facts.get("parcel_polygon") if isinstance(facts, dict) else None
        if not isinstance(polygon, dict):
            continue
        if polygon.get("type") != "Polygon":
            continue
        coordinates = polygon.get("coordinates")
        if isinstance(coordinates, list) and coordinates:
            return {
                "rings": coordinates,
                "spatialReference": {"wkid": 4326},
            }
    return None


def _query_travis_subdivision_geometry(
    session: requests.Session,
    geometry: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    return _query_subdivision_geometry(
        session,
        _TRAVIS_SUBDIVISIONS_QUERY,
        geometry,
    )


def _query_subdivision_geometry(
    session: requests.Session,
    query_url: str,
    geometry: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "where": "1=1",
        "geometry": json.dumps(geometry),
        "geometryType": "esriGeometryPolygon",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        resp = session.get(
            query_url,
            params=params,
            timeout=_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-plats"},
        )
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], f"non-JSON content-type ({ct[:40]})"
        data = resp.json()
        if "error" in data:
            return [], str(data["error"].get("message", data["error"]))[:200]
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning("Subdivision GIS query failed: %s", exc)
        return [], str(exc)[:200]


def _summarize_travis_subdivision_records(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _clean_attr(attrs.get("Name"))
        if not name:
            continue
        document_number = _clean_attr(attrs.get("Document"))
        volume_page = _clean_attr(attrs.get("Vol_Page"))
        clerk_link = _clean_attr(attrs.get("Clerk_Link"))
        record = {
            "name": name,
            "document_number": document_number,
            "volume_page": volume_page,
            "record_date_ms": attrs.get("Record_Date"),
            "court_date_ms": attrs.get("Court_Date"),
            "gis_date_ms": attrs.get("GIS_Date"),
            "status": _clean_attr(attrs.get("Status")),
            "jurisdiction": _clean_attr(attrs.get("Jurisdiction")),
            "plat_acres": attrs.get("Plat_Acres"),
            "lots": attrs.get("Lots"),
            "clerk_link": clerk_link,
            "source": _TRAVIS_SUBDIVISIONS_SOURCE,
            "source_url": clerk_link or _TRAVIS_SUBDIVISIONS_LAYER,
        }
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    records.sort(
        key=lambda item: (
            0 if str(item.get("status") or "").lower() == "recorded" else 1,
            -(int(item.get("record_date_ms") or 0)),
        ),
    )
    return records


def _travis_candidates_from_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        document_number = str(record.get("document_number") or "").strip()
        if not document_number:
            continue
        status = str(record.get("status") or "").lower()
        warnings = [] if status in ("", "recorded") else [f"travis_status_{status}"]
        candidates.append({
            "record_type": "recorded_plat",
            "document_number": document_number,
            "candidate_status": "recorded_in_county_gis",
            "source_kind": "county_subdivision_gis",
            "source_id": SOURCE_ID,
            "source_label": _TRAVIS_SUBDIVISIONS_SOURCE,
            "source_url": record.get("source_url"),
            "subdivision_name": record.get("name"),
            "volume_page": record.get("volume_page"),
            "record_date_ms": record.get("record_date_ms"),
            "confidence_score": 90 if status in ("", "recorded") else 75,
            "evidence": [
                ("Travis County GIS recorded-subdivisions layer provides "
                "this plat document number.")
            ],
            "warnings": warnings,
            "requires_official_clerk_upload": True,
            "context": (
                f"{record.get('name')} "
                f"({record.get('status') or 'status not shown'})."
            ),
        })
    return candidates


def _summarize_williamson_subdivision_records(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _clean_attr(attrs.get("SUBDIVISION_NAME"))
        if not name:
            continue
        document_number = _clean_attr(attrs.get("DOCUMENT_NUMBER"))
        cabinet = _clean_attr(attrs.get("CABINET"))
        slide = _clean_attr(attrs.get("SLIDE"))
        cabinet_slide = (
            f"Cabinet {cabinet}, Slide {slide}" if cabinet and slide else None
        )
        record = {
            "name": name,
            "document_number": document_number,
            "cabinet": cabinet,
            "slide": slide,
            "cabinet_slide": cabinet_slide,
            "record_date_ms": attrs.get("PLAT_FILE_DATE"),
            "final_sub_date_ms": attrs.get("FINAL_SUB_DATE"),
            "final_app_date_ms": attrs.get("FINAL_APP_DATE"),
            "status": _clean_attr(attrs.get("SUBDIVISION_STATUS")),
            "category": _clean_attr(attrs.get("SUBDIVISION_CATEGORY")),
            "general_name": _clean_attr(attrs.get("GENERAL_NAME")),
            "etj": _clean_attr(attrs.get("ETJ")),
            "plat_acres": attrs.get("ACRES"),
            "lots": attrs.get("NUMBER_LOTS"),
            "source": _WILLIAMSON_SUBDIVISIONS_SOURCE,
            "source_url": _WILLIAMSON_SUBDIVISIONS_LAYER,
        }
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    records.sort(
        key=lambda item: (
            0 if item.get("document_number") else 1,
            -(int(item.get("record_date_ms") or item.get("final_app_date_ms") or 0)),
        ),
    )
    return records


def _williamson_candidates_from_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        document_number = str(record.get("document_number") or "").strip()
        if not document_number:
            continue
        candidates.append({
            "record_type": "recorded_plat",
            "document_number": document_number,
            "candidate_status": "recorded_in_county_gis",
            "source_kind": "county_subdivision_gis",
            "source_id": SOURCE_ID,
            "source_label": _WILLIAMSON_SUBDIVISIONS_SOURCE,
            "source_url": record.get("source_url"),
            "subdivision_name": record.get("name"),
            "cabinet_slide": record.get("cabinet_slide"),
            "record_date_ms": record.get("record_date_ms"),
            "confidence_score": 90,
            "evidence": [
                ("Williamson County GIS county_subdivisions layer provides "
                "this plat document number for the intersecting parcel polygon.")
            ],
            "warnings": [],
            "requires_official_clerk_upload": True,
            "context": (
                f"{record.get('name')} "
                f"({record.get('status') or 'status not shown'})."
            ),
        })
    return candidates


def _summarize_county_subdivision_records(
    county: str,
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if county == "hays":
        return _summarize_hays_subdivision_records(features)
    if county == "bastrop":
        return _summarize_bastrop_subdivision_records(features)
    if county == "caldwell":
        return _summarize_caldwell_subdivision_records(features)
    return []


def _summarize_hays_subdivision_records(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _clean_attr(attrs.get("SUB_NAME"))
        if not name:
            continue
        record = {
            "name": name,
            "platted": _clean_attr(attrs.get("PLATTED")),
            "lots": attrs.get("LOTS"),
            "plat_acres": attrs.get("ACRES"),
            "year_platted": attrs.get("YEAR_PLATT"),
            "record_date_ms": attrs.get("DATE_RECORDED"),
            "platted_name": _clean_attr(attrs.get("PLATTED_NAME")),
            "work_type": _clean_attr(attrs.get("WORK_TYPE")),
            "status": _clean_attr(attrs.get("STATUS")),
            "case_number": _clean_attr(attrs.get("MGO_PLN")),
            "source": _HAYS_SUBDIVISIONS_SOURCE,
            "source_url": _HAYS_SUBDIVISIONS_LAYER,
        }
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    records.sort(
        key=lambda item: (
            0 if _truthy_text(item.get("platted")) else 1,
            -(int(float(item.get("year_platted") or 0))),
        ),
    )
    return records


def _summarize_bastrop_subdivision_records(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _clean_attr(attrs.get("desc_") or attrs.get("code"))
        if not name:
            continue
        record = {
            "name": name,
            "subdivision_code": _clean_attr(attrs.get("code")),
            "created_date_ms": attrs.get("created_date"),
            "last_edited_date_ms": attrs.get("last_edited_date"),
            "source": _BASTROP_SUBDIVISIONS_SOURCE,
            "source_url": _BASTROP_SUBDIVISIONS_LAYER,
        }
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    records.sort(key=lambda item: str(item.get("name") or ""))
    return records


def _summarize_caldwell_subdivision_records(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _clean_attr(attrs.get("SUBDNAME"))
        if not name:
            continue
        record = {
            "name": name,
            "subdivision_id": attrs.get("SUBD_ID"),
            "recorded": _clean_attr(attrs.get("RECORDED")),
            "recorded_year": attrs.get("YEAR"),
            "source": _CALDWELL_SUBDIVISIONS_SOURCE,
            "source_url": _CALDWELL_SUBDIVISIONS_LAYER,
        }
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    records.sort(
        key=lambda item: (
            0 if _truthy_text(item.get("recorded")) else 1,
            -(int(float(item.get("recorded_year") or 0))),
        ),
    )
    return records


def _is_recorded_county_subdivision_record(
    county: str,
    record: dict[str, Any],
) -> bool:
    if county == "hays":
        return _truthy_text(record.get("platted")) or bool(record.get("record_date_ms"))
    if county == "bastrop":
        # The Bastrop Public GIS Viewer layer is the county subdivision
        # polygon layer and exposes no separate recorded flag/document field.
        return True
    if county == "caldwell":
        return _truthy_text(record.get("recorded"))
    return False


def _county_subdivision_candidates_from_records(
    county: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_label = _COUNTY_SUBDIVISION_SCREENS[county]["source_label"]
    candidates: list[dict[str, Any]] = []
    for record in records:
        document_number = str(record.get("document_number") or "").strip()
        if not document_number:
            continue
        candidates.append({
            "record_type": "recorded_plat",
            "document_number": document_number,
            "candidate_status": "recorded_in_county_gis",
            "source_kind": "county_subdivision_gis",
            "source_id": SOURCE_ID,
            "source_label": source_label,
            "source_url": record.get("source_url"),
            "subdivision_name": record.get("name"),
            "record_date_ms": record.get("record_date_ms"),
            "confidence_score": 90,
            "evidence": [
                (f"{source_label} provides this plat document number for "
                "the intersecting parcel polygon.")
            ],
            "warnings": [],
            "requires_official_clerk_upload": True,
            "context": (
                f"{record.get('name')} "
                f"({record.get('status') or 'status not shown'})."
            ),
        })
    return candidates


def _county_subdivision_plat_identity(
    record: dict[str, Any],
    clue: dict[str, Any],
    *,
    source_label: str,
) -> dict[str, Any]:
    identity = {
        "subdivision_name": record.get("name"),
        "recorded_document_number": record.get("document_number"),
        "record_date_ms": record.get("record_date_ms"),
        "recorded_year": record.get("recorded_year") or record.get("year_platted"),
        "recorded_status": record.get("recorded") or record.get("platted"),
        "source_label": source_label,
        "source_url": record.get("source_url"),
        "legal_description": clue.get("legal_description"),
    }
    return {k: v for k, v in identity.items() if v not in (None, "")}


def _travis_plat_identity(
    record: dict[str, Any],
    clue: dict[str, Any],
) -> dict[str, Any]:
    identity = {
        "subdivision_name": record.get("name"),
        "recorded_document_number": record.get("document_number"),
        "volume_page": record.get("volume_page"),
        "record_date_ms": record.get("record_date_ms"),
        "source_label": _TRAVIS_SUBDIVISIONS_SOURCE,
        "source_url": record.get("source_url"),
        "legal_description": clue.get("legal_description"),
    }
    return {k: v for k, v in identity.items() if v not in (None, "")}


def _williamson_plat_identity(
    record: dict[str, Any],
    clue: dict[str, Any],
) -> dict[str, Any]:
    identity = {
        "subdivision_name": record.get("name"),
        "recorded_document_number": record.get("document_number"),
        "cabinet_slide": record.get("cabinet_slide"),
        "record_date_ms": record.get("record_date_ms"),
        "source_label": _WILLIAMSON_SUBDIVISIONS_SOURCE,
        "source_url": record.get("source_url"),
        "legal_description": clue.get("legal_description"),
    }
    return {k: v for k, v in identity.items() if v not in (None, "")}


def _build_advisory_travis(
    *,
    status: str,
    travis_records: list[dict[str, Any]],
) -> str:
    if status == "recorded_plat_number_resolved" and travis_records:
        first = travis_records[0]
        return (
            "Travis County GIS recorded-subdivisions data resolved "
            f"Document No. {first.get('document_number')} for "
            f"{first.get('name')}. Retrieve the official image/copy from "
            "Travis County Clerk Web Search before treating it as evidence."
        )
    if status == "no_recorded_subdivision_gis_match":
        return (
            "PropDesk queried Travis County GIS recorded subdivisions and "
            "found no intersecting recorded subdivision polygon for this "
            "parcel/point. If the survey or title commitment later shows a "
            "platted lot, search Travis County Clerk Web Search with that "
            "plat name and upload the official image/copy."
        )
    return (
        "Travis County GIS returned subdivision context, but PropDesk still "
        "needs the official Travis County Clerk recorded image/copy for "
        "authority."
    )


def _build_advisory_williamson(
    *,
    status: str,
    records: list[dict[str, Any]],
) -> str:
    if status == "recorded_plat_number_resolved" and records:
        first = records[0]
        number = first.get("document_number")
        extra = first.get("cabinet_slide")
        return (
            "Williamson County GIS county_subdivisions data resolved "
            f"Document No. {number}"
            f"{f' ({extra})' if extra else ''} for {first.get('name')}. "
            "Retrieve the official image/copy from Williamson County Clerk "
            "Official Public Records before treating it as evidence."
        )
    if status == "no_recorded_subdivision_gis_match":
        return (
            "PropDesk queried Williamson County GIS county_subdivisions with "
            "the WCAD parcel polygon and found no intersecting recorded "
            "subdivision polygon for this parcel. If the survey or title "
            "commitment later shows a platted lot, search Williamson County "
            "Clerk records with that plat name and upload the official "
            "image/copy."
        )
    return (
        "Williamson County GIS returned subdivision context for the parcel "
        "polygon, but PropDesk still needs the official Williamson County "
        "Clerk recorded image/copy for authority."
    )


def _build_advisory_county_subdivision_screen(
    *,
    county: str,
    source_label: str,
    status: str,
    records: list[dict[str, Any]],
) -> str:
    county_label = county.title()
    if status == "recorded_plat_number_resolved" and records:
        first = records[0]
        return (
            f"{source_label} resolved Document No. "
            f"{first.get('document_number')} for {first.get('name')}. "
            f"Retrieve the official image/copy from {county_label} County "
            "Clerk records before treating it as evidence."
        )
    if status == "no_recorded_subdivision_gis_match":
        return (
            f"PropDesk queried {source_label} with the parcel polygon and "
            "found no intersecting recorded/platted subdivision polygon for "
            "this parcel. If the survey or title commitment later shows a "
            f"platted lot, search {county_label} County Clerk records with "
            "that plat name and upload the official image/copy."
        )
    if status.endswith("_subdivision_gis_match_not_recorded") and records:
        first = records[0]
        return (
            f"{source_label} returned subdivision context for "
            f"{first.get('name')}, but did not identify an intersecting "
            "recorded/platted subdivision with a clerk document number. "
            "Confirm with the clerk, survey, or title commitment before "
            "treating plat status as resolved."
        )
    if records:
        first = records[0]
        return (
            f"{source_label} found {first.get('name')} intersecting the "
            "parcel polygon, but PropDesk still needs the official "
            f"{county_label} County Clerk recorded image/copy for authority."
        )
    return (
        f"{source_label} returned no subdivision context for the parcel "
        "polygon. Verify against title/survey if later diligence suggests a "
        "platted lot."
    )


def _clean_attr(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy_text(value: Any) -> bool:
    return str(value or "").strip().lower() in {"y", "yes", "true", "1"}


def _discover_public_recorded_plats(
    session: requests.Session,
    identity: PropertyIdentity,
    *,
    county: str,
    config: Any,
) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    if cfg.get("public_plat_discovery_enabled") is False:
        return {
            "public_discovery_status": "disabled",
            "record_candidates": [],
        }

    clue = _plat_clue_from_config(identity, cfg)
    if not _has_discovery_identity(identity, clue):
        return {
            "public_discovery_status": "insufficient_identity",
            "status": "insufficient_identity",
            "record_candidates": [],
            "plat_clues": clue.to_public(),
            "search_queries": [],
            "source_attempts": [],
            "search_errors": [],
            "advisory_public_discovery": (
                "Need an address, parcel ID, subdivision name, or legal "
                "description before public recorded-plat discovery can run."
            ),
        }
    provider = _search_provider_from_config(cfg, session)
    seed_results = _seed_results_from_config(cfg)
    search_errors: list[str] = []
    queries = _build_discovery_queries(identity, county=county, clue=clue)
    search_results: list[SearchResult] = list(seed_results)
    if provider is not None and queries:
        try:
            search_results.extend(
                provider.search(
                    queries[: cfg.get("max_queries", _MAX_DISCOVERY_QUERIES)],
                    max_results=cfg.get("max_results", _MAX_SEARCH_RESULTS),
                ),
            )
        except Exception as exc:
            log.warning("public plat search provider failed: %s", exc)
            search_errors.append(str(exc)[:300])

    search_results = _dedupe_search_results(search_results)
    clue = _merge_clues(
        clue,
        _plat_clue_from_search_results(
            search_results,
            identity=identity,
            base_clue=clue,
        ),
    )
    source_documents = _source_documents_from_config(cfg)
    record_candidates: list[dict[str, Any]] = []
    source_attempts: list[dict[str, Any]] = []
    for result in search_results[: cfg.get("max_results", _MAX_SEARCH_RESULTS)]:
        if not _is_public_http_url(result.url):
            continue
        if not _search_result_relevant(result, identity=identity, clue=clue):
            continue
        doc_text, content_type, error = _fetch_or_load_source_text(
            session,
            result,
            source_documents,
        )
        source_payload = {
            "url": result.url,
            "title": result.title,
            "snippet": result.snippet,
            "content_type": content_type,
        }
        if error:
            source_payload["error"] = error
            source_attempts.append(source_payload)
            continue
        source_payload["matched_text_terms"] = _matched_source_terms(doc_text)
        source_attempts.append(source_payload)
        # Enrich the clue from the fetched document body (not just the
        # search title/snippet) so block/lot/subdivision findings come
        # straight off the recorded-plat text.
        source_clue = _merge_clues(clue, _plat_clue_from_text(doc_text) or PlatClue())
        clue = source_clue
        record_candidates.extend(_record_candidates_from_source(
            result,
            doc_text,
            content_type=content_type,
            county=county,
            identity=identity,
            clue=source_clue,
        ))

    record_candidates = _dedupe_candidates(record_candidates)
    official_retrieval_attempts = _attempt_official_retrievals(
        session,
        county=county,
        candidates=record_candidates,
        config=cfg,
    )
    candidate_numbers = [
        str(candidate.get("document_number"))
        for candidate in record_candidates
        if isinstance(candidate, dict) and candidate.get("document_number")
    ]
    read_target = _select_read_target(record_candidates, source_attempts)
    if record_candidates:
        status = "probable_recorded_plat_candidates_found"
    elif source_attempts:
        status = "public_sources_checked_no_candidate"
    elif provider is not None:
        status = "public_search_checked_no_relevant_results"
    else:
        status = "no_public_search_provider"
    return {
        "public_discovery_status": status,
        "public_search_provider": (
            provider.__class__.__name__ if provider is not None else None
        ),
        "plat_clues": clue.to_public(),
        "record_candidates": record_candidates[:5],
        "probable_plat_number": candidate_numbers[0] if candidate_numbers else None,
        "probable_plat_numbers": candidate_numbers[:5],
        "plat_read_target": read_target,
        "official_retrieval_attempts": official_retrieval_attempts,
        "search_queries": queries,
        "source_attempts": source_attempts[:8],
        "search_errors": search_errors,
        "advisory_public_discovery": (
            "Public discovery candidates are leads only. Retrieve the "
            "official county-clerk recorded plat PDF before treating a "
            "candidate number as authoritative."
        ),
    }


def _search_provider_from_config(
    config: dict[str, Any],
    session: requests.Session,
) -> SearchProvider | None:
    provider = config.get("search_provider")
    if provider is not None and hasattr(provider, "search"):
        return provider
    provider_name = str(
        config.get("public_plat_search_provider") or "disabled",
    ).strip().lower()
    if provider_name == "searxng":
        url = str(config.get("searxng_search_url") or "").strip()
        if url:
            return SearxngSearchProvider(url, session=session)
    if provider_name == "bing_html":
        return BingHtmlSearchProvider(session=session)
    if provider_name == "brave":
        key = str(config.get("brave_search_api_key") or "").strip()
        if key:
            return BraveSearchProvider(key, session=session)
    if provider_name == "bing":
        key = str(config.get("bing_search_api_key") or "").strip()
        if key:
            return BingSearchProvider(key, session=session)
    return None


class BingHtmlSearchProvider:
    """Best-effort no-key web discovery fallback.

    This is not the authoritative source. It only finds public leads
    (for example sales packages or public PDFs) that are later confirmed
    against the county clerk.
    """

    def __init__(self, *, session: requests.Session):
        self.session = session

    def search(
        self,
        queries: list[str],
        *,
        max_results: int = _MAX_SEARCH_RESULTS,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in queries:
            resp = self.session.get(
                "https://www.bing.com/search",
                params={"q": query, "count": min(max_results, 10)},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=_DISCOVERY_TIMEOUT,
            )
            resp.raise_for_status()
            for match in _BING_RESULT_RE.finditer(resp.text):
                block = match.group(0)
                href = _decode_bing_href(html.unescape(match.group("href")))
                if not _is_public_http_url(href):
                    continue
                title = _clean_html_text(match.group("title"))
                snippet_match = _BING_SNIPPET_RE.search(block)
                snippet = (
                    _clean_html_text(snippet_match.group("snippet"))
                    if snippet_match else ""
                )
                results.append(SearchResult(
                    url=href,
                    title=title,
                    snippet=snippet,
                ))
                if len(results) >= max_results:
                    return results[:max_results]
        return results[:max_results]


class SearxngSearchProvider:
    def __init__(self, base_url: str, *, session: requests.Session):
        self.base_url = base_url.rstrip("/")
        self.session = session

    def search(
        self,
        queries: list[str],
        *,
        max_results: int = _MAX_SEARCH_RESULTS,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in queries:
            resp = self.session.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
                timeout=_DISCOVERY_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results") or []:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                results.append(
                    SearchResult(
                        url=url,
                        title=str(item.get("title") or ""),
                        snippet=str(item.get("content") or ""),
                    ),
                )
                if len(results) >= max_results:
                    break
        return results[:max_results]


class BraveSearchProvider:
    def __init__(self, api_key: str, *, session: requests.Session):
        self.api_key = api_key
        self.session = session

    def search(
        self,
        queries: list[str],
        *,
        max_results: int = _MAX_SEARCH_RESULTS,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in queries:
            resp = self.session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": min(max_results, 20)},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key,
                },
                timeout=_DISCOVERY_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in (data.get("web") or {}).get("results") or []:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                results.append(
                    SearchResult(
                        url=url,
                        title=str(item.get("title") or ""),
                        snippet=str(item.get("description") or ""),
                    ),
                )
                if len(results) >= max_results:
                    break
        return results[:max_results]


class BingSearchProvider:
    def __init__(self, api_key: str, *, session: requests.Session):
        self.api_key = api_key
        self.session = session

    def search(
        self,
        queries: list[str],
        *,
        max_results: int = _MAX_SEARCH_RESULTS,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in queries:
            resp = self.session.get(
                "https://api.bing.microsoft.com/v7.0/search",
                params={"q": query, "count": min(max_results, 10)},
                headers={"Ocp-Apim-Subscription-Key": self.api_key},
                timeout=_DISCOVERY_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("webPages", {}).get("value") or []:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                results.append(
                    SearchResult(
                        url=url,
                        title=str(item.get("name") or ""),
                        snippet=str(item.get("snippet") or ""),
                    ),
                )
                if len(results) >= max_results:
                    break
        return results[:max_results]


def _build_discovery_queries(
    identity: PropertyIdentity,
    *,
    county: str,
    clue: PlatClue,
) -> list[str]:
    address = (identity.address or "").strip()
    parcel_id = (identity.parcel_id or "").strip()
    county_label = f"{county.title()} County" if county else ""
    queries: list[str] = []
    street_core = _street_core(address)
    street_road = _road_variant(street_core)
    if street_core:
        queries.extend([
            f'"{street_core}" "Recorded Plat"',
            f'"{street_road}" "Plat: Recorded Plat"',
            f'site:images1.showcase.com/d2 "{street_core}"',
            f'site:images1.showcase.com/d2 "{street_road}"',
            f'"{street_core}" "Doc #"',
            f'"{street_core}" "document.pdf"',
        ])
    if address:
        queries.extend([
            f'"{address}" "recorded plat"',
            f'"{address}" "Plat: Recorded Plat"',
            f'"{address}" "document.pdf"',
            f'"{address}" "Doc #"',
            f'"{address}" "commercial" "document.pdf"',
            f'"{address}" "sale package" "pdf"',
        ])
        address_core = _road_variant(address.split(",")[0])
        if address_core and address_core != address and address_core != street_road:
            queries.extend([
                f'"{address_core}" "Plat: Recorded Plat"',
                f'"{address_core}" "document.pdf"',
            ])
    if parcel_id:
        queries.append(f'"{parcel_id}" "recorded plat"')
    if clue.subdivision_name:
        subdivision = clue.subdivision_name
        queries.extend([
            f'"{subdivision}" "{county_label}" "Doc #"',
            f'"{subdivision}" "{county_label}" "Document No"',
            f'"{subdivision}" "recorded plat"',
        ])
        if clue.block and clue.lot:
            queries.append(
                f'"{subdivision}" "Block {clue.block}" "Lot {clue.lot}" plat',
            )
    return list(dict.fromkeys(q for q in queries if q.strip()))


def _street_core(address: str | None) -> str:
    if not address:
        return ""
    first = address.split(",")[0]
    first = re.sub(
        r"\b(?:rd|road|st|street|ave|avenue|blvd|boulevard|dr|drive|ln|lane)\b\.?",
        "",
        first,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", first).strip()


def _road_variant(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    text = re.sub(r"\bRd\b\.?", "Road", text, flags=re.IGNORECASE)
    return text


def _plat_clue_from_config(
    identity: PropertyIdentity,
    config: dict[str, Any],
) -> PlatClue:
    clue = PlatClue(
        parcel_id=identity.parcel_id,
        address=identity.address,
    )
    raw_clue = config.get("plat_clues")
    if isinstance(raw_clue, dict):
        clue = _merge_clues(clue, _plat_clue_from_mapping(raw_clue))
    results = config.get("upstream_results")
    if isinstance(results, dict):
        for source_id in ("wcad_parcel", "tcad_parcel", "plats_intel"):
            raw = results.get(source_id)
            facts = (
                raw.get("normalized_facts")
                if isinstance(raw, dict) else None
            )
            if isinstance(facts, dict):
                clue = _merge_clues(clue, _plat_clue_from_mapping(facts))
    project_context = config.get("project_context")
    if isinstance(project_context, dict):
        clue = _merge_clues(
            clue,
            _plat_clue_from_text(str(project_context.get("project_note") or "")),
        )
        for doc in project_context.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            text = " ".join(
                str(doc.get(key) or "")
                for key in ("filename", "doc_type", "summary", "text_sample")
            )
            clue = _merge_clues(clue, _plat_clue_from_text(text))
    return clue


def _plat_clue_from_search_results(
    results: list[SearchResult],
    *,
    identity: PropertyIdentity,
    base_clue: PlatClue,
) -> PlatClue:
    clue = PlatClue()
    for result in results:
        if not _search_result_relevant(result, identity=identity, clue=base_clue):
            continue
        text = f"{result.title} {result.snippet}"
        parsed = _parse_legal_description(text)
        if parsed is not None:
            clue = _merge_clues(clue, parsed)
    return clue


def _plat_clue_from_mapping(mapping: dict[str, Any]) -> PlatClue:
    legal = _first_present(
        mapping,
        (
            "legal_description",
            "legal_desc",
            "legal",
            "property_legal",
            "situs_legal_description",
        ),
    )
    clue = (
        _parse_legal_description(str(legal))
        if legal else PlatClue()
    ) or PlatClue()
    if legal and not clue.legal_description:
        clue.legal_description = str(legal).strip()
    subdivision = _first_present(
        mapping,
        (
            "subdivision",
            "subdivision_name",
            "addition",
            "plat_name",
            "map_subdivision",
        ),
    )
    block = _first_present(mapping, ("block", "block_number"))
    lot = _first_present(mapping, ("lot", "lot_number"))
    parcel_id = _first_present(mapping, ("parcel_id", "apn", "pin"))
    address = _first_present(mapping, ("address", "situs_address"))
    if subdivision:
        clue.subdivision_name = _normalize_name(str(subdivision))
    if block:
        clue.block = str(block).strip().upper()
    if lot:
        clue.lot = str(lot).strip().upper()
    if parcel_id:
        clue.parcel_id = str(parcel_id).strip()
    if address:
        clue.address = str(address).strip()
    return clue


def _parse_legal_description(value: str | None) -> PlatClue | None:
    if not value:
        return None
    match = _LEGAL_PLAT_RE.search(value)
    if not match:
        return None
    subdivision = _normalize_name(match.group("subdivision"))
    modifier = match.group("modifier")
    if subdivision and modifier:
        subdivision = f"{subdivision} ({modifier.upper()})"
    return PlatClue(
        subdivision_name=subdivision,
        block=match.group("block").strip().upper(),
        lot=match.group("lot").strip().upper(),
        legal_description=value,
    )


def _plat_clue_from_text(value: str | None) -> PlatClue | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value)
    legal_match = re.search(
        r"(S\d+\s*-\s*[^.;\n]{5,180}?"
        r"BLOCK\s+[^,;.]{1,24}\s*,\s*LOT\s+[^,;.]{1,24}"
        r"(?:\s*,\s*ACRES\s+[0-9.]+)?)",
        text,
        re.IGNORECASE,
    )
    if legal_match:
        return _parse_legal_description(legal_match.group(1))
    labeled = re.search(
        r"legal description\s*[:\-]\s*([^.;\n]{5,220})",
        text,
        re.IGNORECASE,
    )
    if labeled:
        return _parse_legal_description(labeled.group(1))
    return _parse_legal_description(text)


def _merge_clues(base: PlatClue, other: PlatClue | None) -> PlatClue:
    if other is None:
        return base
    return PlatClue(
        subdivision_name=other.subdivision_name or base.subdivision_name,
        block=other.block or base.block,
        lot=other.lot or base.lot,
        legal_description=other.legal_description or base.legal_description,
        parcel_id=other.parcel_id or base.parcel_id,
        address=other.address or base.address,
    )


def _seed_results_from_config(config: dict[str, Any]) -> list[SearchResult]:
    raw = config.get("seed_search_results") or []
    out: list[SearchResult] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append(
            SearchResult(
                url=url,
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
            ),
        )
    return out


def _source_documents_from_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("public_discovery_documents") or {}
    if isinstance(raw, dict):
        return {
            str(url): doc
            for url, doc in raw.items()
            if isinstance(doc, dict)
        }
    if isinstance(raw, list):
        return {
            str(doc.get("url")): doc
            for doc in raw
            if isinstance(doc, dict) and doc.get("url")
        }
    return {}


def _fetch_or_load_source_text(
    session: requests.Session,
    result: SearchResult,
    source_documents: dict[str, dict[str, Any]],
) -> tuple[str, str, str | None]:
    fixture = source_documents.get(result.url)
    if fixture is not None:
        content_type = str(fixture.get("content_type") or "text/plain")
        if "text" in fixture:
            return str(fixture["text"]), content_type, None
        raw = fixture.get("bytes")
        if isinstance(raw, bytes):
            return _extract_source_text(raw, content_type), content_type, None
    try:
        resp = session.get(
            result.url,
            timeout=_DISCOVERY_TIMEOUT,
            headers={
                "Accept": "application/pdf,text/html,*/*",
                "User-Agent": (
                    "PropDesk/1.0 public-plat-discovery "
                    "(support@reference.app)"
                ),
            },
            stream=True,
        )
        if resp.status_code in {401, 402, 403, 429}:
            return "", resp.headers.get("content-type", ""), (
                f"blocked_or_gated_http_{resp.status_code}"
            )
        resp.raise_for_status()
        raw = resp.raw.read(_MAX_SOURCE_BYTES + 1, decode_content=True)
        if len(raw) > _MAX_SOURCE_BYTES:
            return "", resp.headers.get("content-type", ""), "source_too_large"
        content_type = resp.headers.get("content-type", "")
        return _extract_source_text(raw, content_type), content_type, None
    except Exception as exc:
        return "", "", str(exc)[:300]


def _extract_source_text(raw: bytes, content_type: str) -> str:
    lowered = content_type.lower()
    if raw.startswith(b"%PDF-") or any(t in lowered for t in _PDF_MIME_TYPES):
        text = _extract_pdf_text(raw)
        if not _record_numbers(text) and _looks_like_plat_source(text):
            ocr_text = _extract_pdf_ocr_text_if_available(raw)
            if ocr_text:
                text = f"{text}\n{ocr_text}"
        return text
    decoded = raw.decode("utf-8", errors="ignore")
    if "<html" in decoded[:500].lower() or "text/html" in lowered:
        decoded = _clean_html_text(decoded)
    return re.sub(r"\s+", " ", decoded)


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub(" ", value))).strip()


def _decode_bing_href(href: str) -> str:
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    raw = qs.get("u", [None])[0]
    if not raw:
        return href
    if raw.startswith("a1"):
        encoded = raw[2:]
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        try:
            return urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8")
        except Exception:
            return href
    return raw


def _extract_pdf_text(raw: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except (PdfReadError, Exception) as exc:
        log.debug("public plat PDF text extraction failed: %s", exc)
        return ""
    chunks: list[str] = []
    for page in reader.pages[:12]:
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            log.debug("public plat PDF page text extraction failed: %s", exc)
            continue
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _extract_pdf_ocr_text_if_available(raw: bytes) -> str:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
        import pytesseract  # type: ignore[import-not-found]
    except Exception:
        return ""
    try:
        pdf = pdfium.PdfDocument(raw)
    except Exception:
        return ""
    chunks: list[str] = []
    for page_index in range(min(len(pdf), 12)):
        try:
            bitmap = pdf[page_index].render(scale=2).to_pil()
            text = pytesseract.image_to_string(bitmap) or ""
        except Exception as exc:
            log.debug("public plat PDF page OCR failed: %s", exc)
            continue
        if _looks_like_plat_source(text) or _record_numbers(text):
            chunks.append(text)
    return "\n".join(chunks)


def _record_candidates_from_source(
    result: SearchResult,
    text: str,
    *,
    content_type: str,
    county: str,
    identity: PropertyIdentity,
    clue: PlatClue,
) -> list[dict[str, Any]]:
    combined = f"{result.title} {result.snippet} {text}"
    numbers = _record_numbers(combined)
    if not numbers:
        return []
    out: list[dict[str, Any]] = []
    for number in numbers:
        score, evidence, warnings = _score_record_candidate(
            number,
            combined,
            result=result,
            content_type=content_type,
            county=county,
            identity=identity,
            clue=clue,
        )
        if score < 55:
            continue
        out.append({
            "record_type": "recorded_plat",
            "document_number": number,
            "candidate_status": "probable_not_authoritative",
            "source_kind": (
                "public_web_pdf"
                if result.url.lower().split("?")[0].endswith(".pdf")
                or "pdf" in content_type.lower()
                else "public_web_html"
            ),
            "source_id": SOURCE_ID,
            "source_label": result.title or _host_label(result.url),
            "source_url": result.url,
            "subdivision_name": clue.subdivision_name,
            "block": clue.block,
            "lot": clue.lot,
            "confidence_score": score,
            "evidence": evidence,
            "warnings": warnings,
            "requires_official_clerk_upload": True,
            "context": _context_for_number(combined, number),
        })
    return out


def _score_record_candidate(
    number: str,
    text: str,
    *,
    result: SearchResult,
    content_type: str,
    county: str,
    identity: PropertyIdentity,
    clue: PlatClue,
) -> tuple[int, list[str], list[str]]:
    text_norm = _normalize_for_match(text)
    score = 0
    evidence: list[str] = []
    warnings: list[str] = []
    if _county_number_format_ok(county, number):
        score += 20
        evidence.append("Document number format is plausible for the county.")
    else:
        warnings.append("county_number_format_not_confirmed")
    if "recorded plat" in text_norm or "plat recorded" in text_norm:
        score += 20
        evidence.append("Public source says Recorded Plat.")
    elif "plat" in text_norm:
        score += 10
        evidence.append("Public source contains plat language.")
    if identity.address and _address_tokens_match(identity.address, text_norm):
        score += 20
        evidence.append("Public source matches the project address.")
    if identity.parcel_id and identity.parcel_id.lower() in text.lower():
        score += 15
        evidence.append("Public source matches the project parcel ID.")
    if clue.subdivision_name and _name_matches(clue.subdivision_name, text):
        score += 25
        evidence.append("Public source matches the subdivision/plat name.")
    if clue.block and re.search(rf"\bblock\s+{re.escape(clue.block)}\b", text, re.IGNORECASE):
        score += 10
        evidence.append(f"Public source shows Block {clue.block}.")
    if clue.lot and re.search(rf"\blot\s+{re.escape(clue.lot)}\b", text, re.IGNORECASE):
        score += 10
        evidence.append(f"Public source shows Lot {clue.lot}.")
    if "pdf" in content_type.lower() or result.url.lower().split("?")[0].endswith(".pdf"):
        score += 5
        evidence.append("Candidate came from a public PDF.")
    if "access denied" in text_norm or "forbidden" in text_norm:
        warnings.append("source_access_blocked")
        score -= 20
    return max(0, min(score, 100)), evidence, warnings


def _attempt_official_retrievals(
    session: requests.Session,
    *,
    county: str,
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    if config.get("official_record_retrieval_enabled") is False:
        return []
    if not candidates:
        return []
    portal = county_clerk_portals.clerk_lookup_steps(county, "plat")
    if not portal:
        return []
    attempts: list[dict[str, Any]] = []
    for candidate in candidates[:3]:
        number = str(candidate.get("document_number") or "").strip()
        if not number:
            continue
        attempts.append(
            _attempt_official_retrieval(
                session,
                county=county,
                portal=portal,
                document_number=number,
            ),
        )
    return attempts


def _attempt_official_retrieval(
    session: requests.Session,
    *,
    county: str,
    portal: dict[str, str],
    document_number: str,
) -> dict[str, Any]:
    if county == "williamson":
        return _attempt_williamson_retrieval(
            session,
            portal=portal,
            document_number=document_number,
        )
    return _attempt_portal_probe(
        session,
        portal=portal,
        document_number=document_number,
        result=(
            "manual_session_required"
        ),
        detail=(
            "PropDesk opened the official records portal, but this county "
            "does not expose an unauthenticated direct PDF endpoint in the "
            "configured adapter. Use the portal session to search and "
            "download the official image/copy."
        ),
    )


def _attempt_williamson_retrieval(
    session: requests.Session,
    *,
    portal: dict[str, str],
    document_number: str,
) -> dict[str, Any]:
    base_url = "https://williamsoncountytx-web.tylerhost.net"
    portal_url = portal["url"]
    try:
        first = session.get(
            portal_url,
            timeout=_DISCOVERY_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except Exception as exc:
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status="portal_unavailable",
            detail=f"Official portal request failed: {str(exc)[:180]}",
        )

    if first.status_code in {401, 402, 403, 429}:
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status=f"blocked_or_gated_http_{first.status_code}",
            detail=(
                "PropDesk reached the official Williamson County portal, "
                "but the request was blocked or gated before a document "
                "copy could be retrieved."
            ),
            http_status=first.status_code,
            attempted_url=first.url,
        )

    text = first.text or ""
    accepted_disclaimer = False
    if "/user/disclaimer" in first.url or "submitDisclaimerAccept" in text:
        try:
            disclaimer = session.post(
                f"{base_url}/williamsonweb/user/disclaimer",
                timeout=_DISCOVERY_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "ajaxRequest": "true",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            accepted_disclaimer = disclaimer.ok
            second = session.get(
                portal_url,
                timeout=_DISCOVERY_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            text = second.text or ""
            first = second
        except Exception as exc:
            return _official_attempt_payload(
                portal=portal,
                document_number=document_number,
                status="manual_session_required",
                detail=(
                    "PropDesk reached the Williamson County disclaimer but "
                    f"could not complete the portal session: {str(exc)[:160]}"
                ),
                attempted_url=first.url,
                disclaimer_accepted=accepted_disclaimer,
            )

    if first.headers.get("content-type", "").lower().startswith("application/pdf"):
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status="retrieved_pdf",
            detail="PropDesk reached an official PDF response.",
            http_status=first.status_code,
            attempted_url=first.url,
            disclaimer_accepted=accepted_disclaimer,
        )

    if "field_DocNumID" in text and "Instrument Number" in text:
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status="manual_session_required",
            detail=(
                "PropDesk opened the Williamson County Official Public "
                "Records search form after the disclaimer. The Tyler portal "
                "does not expose a stable unauthenticated recorded-PDF URL; "
                "the image/copy must be opened or purchased in the browser "
                "session."
            ),
            http_status=first.status_code,
            attempted_url=first.url,
            disclaimer_accepted=accepted_disclaimer,
        )

    return _official_attempt_payload(
        portal=portal,
        document_number=document_number,
        status="manual_session_required",
        detail=(
            "PropDesk reached the official Williamson County portal but did "
            "not find a direct PDF response or stable public download URL."
        ),
        http_status=first.status_code,
        attempted_url=first.url,
        disclaimer_accepted=accepted_disclaimer,
    )


def _attempt_portal_probe(
    session: requests.Session,
    *,
    portal: dict[str, str],
    document_number: str,
    result: str,
    detail: str,
) -> dict[str, Any]:
    try:
        resp = session.get(
            portal["url"],
            timeout=_DISCOVERY_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        status = result
        if resp.status_code in {401, 402, 403, 429}:
            status = f"blocked_or_gated_http_{resp.status_code}"
        elif resp.headers.get("content-type", "").lower().startswith("application/pdf"):
            status = "retrieved_pdf"
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status=status,
            detail=detail,
            http_status=resp.status_code,
            attempted_url=resp.url,
        )
    except Exception as exc:
        return _official_attempt_payload(
            portal=portal,
            document_number=document_number,
            status="portal_unavailable",
            detail=f"Official portal request failed: {str(exc)[:180]}",
        )


def _official_attempt_payload(
    *,
    portal: dict[str, str],
    document_number: str,
    status: str,
    detail: str,
    http_status: int | None = None,
    attempted_url: str | None = None,
    disclaimer_accepted: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_number": document_number,
        "portal_name": portal["name"],
        "portal_url": portal["url"],
        "attempted_url": attempted_url or portal["url"],
        "status": status,
        "detail": detail,
        "search_mode": "Instrument Number",
        "search_value": document_number,
        "next_step": (
            f"Open {portal['name']}, search Instrument/Document Number = "
            f"{document_number}, confirm the document is a recorded plat, "
            "then download the official image/copy PDF and upload it."
        ),
    }
    if http_status is not None:
        payload["http_status"] = http_status
    if disclaimer_accepted is not None:
        payload["disclaimer_accepted"] = disclaimer_accepted
    return payload


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_number: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        number = str(candidate.get("document_number") or "")
        if not number:
            continue
        existing = by_number.get(number)
        if existing is None or int(candidate.get("confidence_score") or 0) > int(
            existing.get("confidence_score") or 0,
        ):
            by_number[number] = candidate
    return sorted(
        by_number.values(),
        key=lambda item: int(item.get("confidence_score") or 0),
        reverse=True,
    )


def _dedupe_search_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in results:
        url = result.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(result)
    return out


def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")


def _select_read_target(
    candidates: list[dict[str, Any]],
    source_attempts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the best PDF for PropDesk to AI-read.

    Priority 1: the top-confidence candidate that came from a PDF (read it
    to confirm the number + pull plat findings). Priority 2: a plat-like PDF
    we fetched but could NOT text-extract a number from — that is the
    image-only / scanned plat case the AI vision read exists for.
    """
    for candidate in candidates:
        url = str(candidate.get("source_url") or "")
        if not url:
            continue
        if candidate.get("source_kind") == "public_web_pdf" or _is_pdf_url(url):
            return {
                "url": url,
                "reason": "top_candidate_pdf",
                "document_number": candidate.get("document_number"),
            }
    for attempt in source_attempts:
        if attempt.get("error"):
            continue
        url = str(attempt.get("url") or "")
        if not url:
            continue
        content_type = str(attempt.get("content_type") or "").lower()
        if "pdf" not in content_type and not _is_pdf_url(url):
            continue
        terms = attempt.get("matched_text_terms") or []
        # Empty terms on a relevant PDF == no extractable text layer
        # (image-only plat). A "plat"/"recorded plat" term also qualifies.
        if not terms or "plat" in terms or "recorded plat" in terms:
            return {"url": url, "reason": "plat_like_pdf_unextracted"}
    return None


def _plat_terms_from_clue(clue: dict[str, Any]) -> str:
    parts: list[str] = []
    subdivision = clue.get("subdivision_name")
    if subdivision:
        parts.append(str(subdivision))
    if clue.get("block"):
        parts.append(f"Block {clue.get('block')}")
    if clue.get("lot"):
        parts.append(f"Lot {clue.get('lot')}")
    if clue.get("legal_description"):
        parts.append(f"Legal description {clue.get('legal_description')}")
    if parts:
        return ", ".join(parts)
    return "the subdivision / lot / block from your survey or title commitment"


def _build_manual_next_step(
    county: str,
    discovery: dict[str, Any],
    *,
    portal: dict[str, str] | None = None,
) -> str:
    """Single plain-English instruction the broker can act on.

    Always names the destination (official portal when covered, else the
    county clerk) and either the exact number to search or the search terms
    to use when no number was resolved.
    """
    numbers = discovery.get("probable_plat_numbers") or []
    county_label = (county or "your property's").title()
    if portal:
        destination = f"{portal['name']} ({portal['url']})"
    else:
        destination = f"the {county_label} County Clerk Official Public Records"
    if numbers:
        number = numbers[0]
        return (
            f"PropDesk's best recorded-plat lead is Document/Instrument "
            f"No. {number}. Open {destination}, search Instrument/Document "
            f"Number = {number}, confirm it is the recorded plat for this "
            "property, download the official PDF, and upload it here."
        )
    no_subdivision_source = _no_recorded_subdivision_source(discovery)
    if no_subdivision_source is not None:
        terms = _plat_terms_from_clue(discovery.get("plat_clues") or {})
        return (
            f"PropDesk checked {no_subdivision_source} and did not find an "
            "intersecting recorded/platted subdivision polygon for this "
            f"parcel. Search terms available: {terms}. A recorded plat upload "
            "is only needed if a survey, title commitment, or later clerk "
            "search identifies a platted subdivision lot; then open "
            f"{destination}, search document type Plat by that subdivision "
            "name or legal-description terms, and upload the official "
            "recorded image/copy."
        )
    terms = _plat_terms_from_clue(discovery.get("plat_clues") or {})
    return (
        "PropDesk could not resolve a probable recorded-plat number from "
        f"public sources. Open {destination}, search document type Plat by "
        f"{terms}, then download the recorded plat PDF and upload it here."
    )


def _no_recorded_subdivision_source(discovery: dict[str, Any]) -> str | None:
    for key, value in discovery.items():
        if not (
            key.endswith("_subdivision_gis_status")
            and value == "no_intersecting_recorded_subdivision"
        ):
            continue
        source = discovery.get(key.removesuffix("_status") + "_source")
        if isinstance(source, str) and source.strip():
            return source.strip()
        county = key.removesuffix("_subdivision_gis_status").replace("_", " ")
        return f"{county.title()} County subdivision GIS"
    return None


def _record_numbers(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _RECORD_NUMBER_RE.finditer(text or ""):
        number = match.group("number") or match.group("doc_hash")
        if not number or number in seen:
            continue
        seen.add(number)
        out.append(number)
    return out


def _context_for_number(text: str, number: str) -> str:
    idx = text.find(number)
    if idx < 0:
        return ""
    start = max(0, idx - 180)
    end = min(len(text), idx + 180)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _looks_like_plat_source(text: str) -> bool:
    lowered = (text or "").lower()
    return "plat" in lowered and (
        "recorded" in lowered or "subdivision" in lowered or "block" in lowered
    )


def _matched_source_terms(text: str) -> list[str]:
    lowered = (text or "").lower()
    terms = [
        "recorded plat",
        "plat",
        "document no",
        "doc #",
        "instrument no",
        "block",
        "lot",
    ]
    return [term for term in terms if term in lowered]


def _county_number_format_ok(county: str, number: str) -> bool:
    county_key = (county or "").lower().replace(" county", "").strip()
    if county_key == "williamson":
        return bool(re.fullmatch(r"20\d{8}", number))
    return bool(re.fullmatch(r"20\d{7,10}", number))


def _is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.lower()
    return not any(
        blocked in host
        for blocked in (
            "localhost",
            "127.0.0.1",
            "williamsoncountytx-web.tylerhost.net",
        )
    )


def _search_result_relevant(
    result: SearchResult,
    *,
    identity: PropertyIdentity,
    clue: PlatClue,
) -> bool:
    haystack = _normalize_for_match(f"{result.url} {result.title} {result.snippet}")
    if identity.parcel_id and identity.parcel_id.lower() in haystack:
        return True
    if clue.subdivision_name:
        normalized = _normalize_for_match(clue.subdivision_name)
        if normalized and normalized in haystack:
            return True
    if identity.address:
        street_core = _normalize_for_match(_street_core(identity.address))
        street_tokens = [
            token for token in re.split(r"[^a-z0-9]+", street_core)
            if len(token) >= 4 or token.isdigit()
        ]
        numeric = [token for token in street_tokens if token.isdigit()]
        words = [token for token in street_tokens if not token.isdigit()]
        if (
            numeric
            and len([word for word in words if word in haystack]) >= 2
            and all(number in haystack for number in numeric)
        ):
            return True
        if street_core and street_core in haystack:
            return True
    terms: list[str] = []
    if identity.parcel_id:
        terms.append(identity.parcel_id.lower())
    if clue.subdivision_name:
        normalized = _normalize_for_match(clue.subdivision_name)
        terms.extend(part for part in normalized.split() if len(part) >= 4)
        terms.append(normalized)
    return any(term and term in haystack for term in terms)


def _has_discovery_identity(identity: PropertyIdentity, clue: PlatClue) -> bool:
    return any((
        bool((identity.address or "").strip()),
        bool((identity.parcel_id or "").strip()),
        bool((clue.subdivision_name or "").strip()),
        bool((clue.legal_description or "").strip()),
    ))


def _host_label(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return url


def _normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip(" ,.-").upper()
    text = text.replace("(AMD)", "").replace("(AMENDED)", "").strip()
    return text or None


def _normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).lower()


def _name_matches(name: str, text: str) -> bool:
    normalized_name = _normalize_name(name)
    normalized_text = _normalize_name(text)
    if not normalized_name or not normalized_text:
        return False
    return normalized_name in normalized_text or normalized_name.replace("THE ", "") in normalized_text


def _address_tokens_match(address: str, normalized_text: str) -> bool:
    address_norm = _normalize_for_match(_street_core(address))
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", address_norm)
        if len(token) >= 3
    ]
    if not tokens:
        return False
    # Derive the required match tokens entirely from the property's own
    # address — never a per-property literal. The numeric house number is
    # the strongest address-derived signal; fall back to the leading street
    # tokens when the address has no number.
    numeric = [token for token in tokens if token.isdigit()]
    required = numeric if numeric else tokens[:3]
    return all(token in normalized_text for token in required)


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None



def _summarize_plats(
    features: list[dict[str, Any]],
    parcel_lat: float, parcel_lng: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        slat, slng = _centroid_from_geom(geom)
        if slat is None or slng is None:
            continue
        try:
            dist = _haversine_mi(
                parcel_lat, parcel_lng, slat, slng,
            )
        except (TypeError, ValueError):
            continue
        out.append({
            "name": attrs.get("SUB_NAME") or "Unnamed",
            "phase": attrs.get("PHASE") or "",
            "section": attrs.get("SECTION") or "",
            "work_type": attrs.get("WORK_TYPE") or "",
            "status": attrs.get("STATUS") or "",
            "case_number": attrs.get("MGO_PLN") or "",
            "water": attrs.get("WATER") or "",
            "wastewater": attrs.get("WASTEWATER") or "",
            "url": attrs.get("URL") or "",
            "distance_mi": round(dist, 2),
        })
    out.sort(key=lambda p: p["distance_mi"])
    return out


def _centroid_from_geom(
    geom: dict,
) -> tuple[float | None, float | None]:
    rings = geom.get("rings") or []
    if not rings:
        return geom.get("y"), geom.get("x")
    pts = rings[0]
    if not pts:
        return None, None
    xs = [p[0] for p in pts if isinstance(p, list) and len(p) >= 2]
    ys = [p[1] for p in pts if isinstance(p, list) and len(p) >= 2]
    if not xs or not ys:
        return None, None
    return sum(ys) / len(ys), sum(xs) / len(xs)


def _haversine_mi(
    lat1: float, lng1: float, lat2: float, lng2: float,
) -> float:
    R_MI = 3958.7613
    rad1, rad2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rad1) * math.cos(rad2)
        * math.sin(dlng / 2) ** 2
    )
    return 2 * R_MI * math.asin(min(1.0, math.sqrt(a)))


def _build_advisory_hays(
    plats: list[dict[str, Any]],
    in_review: list[dict[str, Any]],
    approved: list[dict[str, Any]],
    buffer_mi: float,
) -> str:
    if not plats:
        return (
            f"No active subdivision plats within "
            f"{buffer_mi:.0f} mi (Hays County GIS)."
        )
    parts = [
        (f"{len(plats)} active plats within {buffer_mi:.0f} mi: "
        f"{len(in_review)} in technical review, "
        f"{len(approved)} approved.")
    ]
    if in_review:
        first = in_review[0]
        parts.append(
            f"Closest in-review: {first['name']} "
            f"({first['distance_mi']} mi, "
            f"{first.get('case_number','no case#')})."
        )
    parts.append(
        "Plat status changes weekly — pull the most recent "
        "Hays P&Z agenda packet for binding context."
    )
    return " ".join(parts)


def _query_layer(
    session: requests.Session, url: str,
    lat: float, lng: float, buffer_mi: float,
) -> tuple[list[dict[str, Any]], str | None]:
    buffer_m = buffer_mi * 1609.34
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        resp = session.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], f"non-JSON content-type ({ct[:40]})"
        return resp.json().get("features", []) or [], None
    except Exception as exc:
        log.warning("Plats layer query failed: %s", exc)
        return [], str(exc)[:200]


def _portal_result(county: str, *, discovery: dict[str, Any]) -> AdapterResult:
    portal = county_clerk_portals.clerk_lookup_steps(county, "plat")
    candidates = discovery.get("record_candidates") or []
    status = (
        "probable_recorded_plat_candidates_found"
        if candidates else "manual_verification_required"
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if candidates else DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED
        ),
        coverage_status=(
            CoverageStatus.PARTIAL
            if candidates else CoverageStatus.MANUAL_VERIFICATION_REQUIRED
        ),
        normalized_facts={
            "status": status,
            "county": county,
            "plats_count": 0, "in_review_count": 0,
            "approved_count": 0,
            "in_review": [], "approved": [], "all_plats": [],
            "portal_name": portal["name"],
            "portal_url": portal["url"],
            "manual_instructions": portal["instructions"],
            **discovery,
            "manual_next_step": _build_manual_next_step(
                county, discovery, portal=portal,
            ),
            "advisory": (
                f"{county.title()} County recorded plats require the "
                f"official clerk image/copy for authority. PropDesk may "
                "surface public web candidates, but the user must retrieve "
                f"the official PDF from {portal['name']}."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                county,
                discovery,
                default=(
                    f"{county.title()} County official records portal: "
                    f"{portal['url']}. Steps: {portal['instructions']}"
                ),
            ),
        ),
    )


def _manual_upload_reason(
    county: str,
    discovery: dict[str, Any],
    *,
    default: str,
) -> str:
    no_subdivision_source = _no_recorded_subdivision_source(discovery)
    if no_subdivision_source is not None:
        return (
            f"PropDesk checked {no_subdivision_source} and found no "
            "intersecting recorded/platted subdivision polygon for this "
            "parcel. Upload a recorded plat only if a survey, title "
            "commitment, or later clerk search identifies the parcel as a "
            "platted subdivision lot."
        )
    candidates = discovery.get("record_candidates") or []
    if candidates:
        numbers = [
            str(item.get("document_number"))
            for item in candidates
            if isinstance(item, dict) and item.get("document_number")
        ]
        first_search = (
            f" First search Instrument/Document Number: {', '.join(numbers)}."
            if numbers else ""
        )
        return (
            f"PropDesk found probable recorded-plat candidate(s) from public "
            f"web evidence for {county.title()} County, but not the official "
            "county-clerk recorded PDF. Retrieve and upload the official "
            f"clerk image/copy before treating the plat as evidence."
            f"{first_search}"
        )
    return default


def _no_coverage_result(
    county: str, *, discovery: dict[str, Any] | None = None,
) -> AdapterResult:
    label = county or "this property's county"
    discovery = discovery or {}
    candidates = discovery.get("record_candidates") or []
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC
            if candidates else SourceAccessStatus.JURISDICTION_NOT_COVERED
        ),
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if candidates else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.PARTIAL
            if candidates else CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION
        ),
        normalized_facts={
            "status": (
                "probable_recorded_plat_candidates_found"
                if candidates else "unknown"
            ),
            "county": county,
            "plats_count": 0, "in_review_count": 0,
            "approved_count": 0,
            "in_review": [], "approved": [], "all_plats": [],
            # Discovery is global — it runs and surfaces a probable plat
            # number for ANY county, even one without a wired clerk portal.
            **discovery,
            "manual_next_step": _build_manual_next_step(county, discovery),
            "advisory": (
                f"No wired subdivision-plats portal for "
                f"{label.title()} County. PropDesk still ran public "
                "recorded-plat discovery; retrieve the official clerk PDF "
                "before treating any candidate as evidence."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=_manual_upload_reason(
                county,
                discovery,
                default=(
                    f"No wired plats portal for {label.title()} "
                    "County. Search the county clerk's Official Public "
                    "Records for the recorded plat + upload it."
                ),
            ),
        ),
    )


def _no_coordinates_result(*, discovery: dict[str, Any] | None = None) -> AdapterResult:
    discovery = discovery or {}
    candidates = discovery.get("record_candidates") or []
    status = (
        "probable_recorded_plat_candidates_found"
        if candidates else discovery.get("status") or "unknown"
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if candidates else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.PARTIAL
            if candidates else CoverageStatus.MANUAL_VERIFICATION_REQUIRED
        ),
        normalized_facts={
            "plats_count": 0, "in_review_count": 0,
            "approved_count": 0,
            "in_review": [], "approved": [], "all_plats": [],
            **discovery,
            "status": status,
            "manual_next_step": _build_manual_next_step("", discovery),
            "advisory": (
                "Coordinates required for plat-proximity lookup. Public "
                "recorded-plat discovery still runs from address / parcel / "
                "legal clues when available."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for plats query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable — pull plat info "
                "manually from the county clerk."
            ),
        ),
    )


def _fetch_failed_result(
    error_msg: str, county: str,
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown",
            "county": county,
            "plats_count": 0, "in_review_count": 0,
            "approved_count": 0,
            "in_review": [], "approved": [], "all_plats": [],
            "advisory": (
                f"{county.title()} plats layer did not "
                "respond. Retry or pull manually."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"plats query failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PLATS_UPLOAD_TYPES),
            reason=(
                "Plats service did not respond. Pull from "
                "the county clerk manually."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
