"""Canonical per-county clerk records registry — one source of truth.

Reconciles the two per-county registries that previously drifted:
  - official_records._CLERK_RECORD_PORTALS  (deed / recording-oriented)
  - plats_intel._MANUAL_PORTALS             (plat-oriented)

into one record per county with **document-type-aware** lookup steps, plus
the per-county record capabilities (CAD sources, search modes). Per
docs/specs/recorded_deed_resolution.md §4 (reviewer review B).

Consumers: `official_records`, `plats_intel`, `plat_reader`, and the
recorded-deed resolver — all read here so per-county URLs + instructions
never drift again.

Key reconciliation note: a county's plat-search portal can differ from its
general/deed portal. Where it does (bastrop), the record carries a
`plat_url` override; otherwise both document types share `url`.
"""

from __future__ import annotations

from typing import Any

from app.regions import loader as regions


def _key(county: str | None) -> str:
    return (county or "").lower().strip().removesuffix(" county").strip()


# Per-county canonical record:
#   name, url (deed/general portal), optional plat_url override,
#   deed_instructions, plat_instructions, and fallback_* where known.
_COUNTY_CLERK_PORTALS: dict[str, dict[str, Any]] = regions.county_sections(
    "TX", "clerk",
)


# Per-county record capabilities — migrated verbatim from
# official_records.COUNTY_RECORD_CAPABILITIES. `cad_sources` tells the deed
# resolver which CAD parcel adapter(s) serve a county.
_COUNTY_RECORD_CAPABILITIES: dict[str, dict[str, Any]] = {
    "travis": {
        "official_records_status": "configured",
        "clerk_system": "Travis County Clerk Web Search / Aumentum",
        "official_record_search_modes": [
            "document_number", "party_grantor_grantee",
            "legal_description", "recording_date",
        ],
        "cad_sources": ["tcad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "manual_or_session_based",
    },
    "williamson": {
        "official_records_status": "configured",
        "clerk_system": "Tyler Self-Service",
        "official_record_search_modes": [
            "instrument_number", "document_type",
            "party_grantor_grantee", "recording_date", "book_page",
        ],
        "cad_sources": ["wcad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "manual_or_session_based",
    },
    "hays": {
        "official_records_status": "configured",
        "clerk_system": "Hays County Clerk Self-Service",
        "official_record_search_modes": [
            "document_number", "instrument_number",
            "party_name", "recording_date",
        ],
        "cad_sources": ["hayscad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "copy_checkout_required",
        "official_image_retrieval": {
            "deed": {
                "status": "copy_checkout_required",
                "fixture_proof_date": "2026-06-02",
                "fixture": {
                    "instrument_number": "26019170",
                    "document_id": "DOC663S169",
                    "document_type": "DEED",
                    "recording_date": "2026-05-26",
                    "grantor": "PERRY HOMES LLC",
                    "grantee": "ARCOS CAROLYN",
                    "page_count": 4,
                    "detail_url": (
                        "https://erss.co.hays.tx.us/web/document/"
                        "DOC663S169?search=DOCSEARCH149S1"
                    ),
                },
                "anonymous_search_proven": True,
                "viewer_image_endpoint": None,
                "checkout_evidence": {
                    "copy_product": "COPIES",
                    "copy_price": "$4.00",
                    "checkout_url": "https://erss.co.hays.tx.us/web/cart",
                    "required_fields": [
                        "Name", "Email Address", "Phone Number",
                        "Billing Address",
                    ],
                    "payment_methods_seen": ["Credit Card", "In Office"],
                },
                "broker_action": "purchase_or_order_copy",
                "auto_import_allowed": False,
            },
        },
    },
    "bastrop": {
        "official_records_status": "configured",
        "clerk_system": "Bastrop County Clerk Web Access",
        "official_record_search_modes": [
            "instrument_number", "party_grantor_grantee",
            "recording_date", "document_type",
        ],
        "cad_sources": ["bastropcad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "session_retrievable",
        "official_image_retrieval": {
            "deed": {
                "status": "session_retrievable",
                "fixture_proof_date": "2026-06-02",
                "fixture": {
                    "instrument_number": "00102058",
                    "book_page": "102-58",
                    "document_type": "DEED",
                    "date_filed": "1972-05-16",
                    "grantor": "NAUMANN CHARLIE",
                    "grantee": "LEUSCHNER EDMOND",
                    "page_count": 2,
                    "global_id": "OPR-714310",
                    "image_id": "1654654",
                },
                "anonymous_search_proven": True,
                "viewer_image_endpoint": (
                    "https://cc.co.bastrop.tx.us/Controls/"
                    "GetImage.aspx?Fpg=0&IMAGE_ID=1654654"
                ),
                "image_byte_proof": {
                    "status": 200,
                    "content_type": "image/tiff",
                    "length_bytes": 52560,
                    "first16_hex": (
                        "49 49 2a 00 08 00 00 00 13 00 fe 00 "
                        "04 00 01 00"
                    ),
                },
                "certified_copy": "cart_required",
                "auto_import_allowed": True,
            },
        },
    },
    "caldwell": {
        "official_records_status": "configured",
        "clerk_system": "Texas County Government Records",
        "official_record_search_modes": [
            "recording_date", "party_name", "book_page",
            "document_number_if_available",
        ],
        "cad_sources": ["caldwellcad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "manual_or_session_based",
    },
    "burnet": {
        "official_records_status": "configured",
        "clerk_system": "Tyler Self-Service",
        "official_record_search_modes": [
            "instrument_number", "document_type", "party_name",
            "recording_date", "book_page",
        ],
        "cad_sources": ["burnetcad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "manual_or_session_based",
    },
    "blanco": {
        "official_records_status": "configured",
        "clerk_system": "Neumo / publicsearch Official Records Search",
        "official_record_search_modes": [
            "quick_search", "index_only", "index_and_full_text_ocr",
            "party_name", "document_number_if_available",
        ],
        "cad_sources": ["blancocad_parcel"],
        "supports_probable_plat_resolution": True,
        "official_pdf_retrieval": "session_retrievable",
        "official_image_retrieval": {
            "deed": {
                "status": "session_retrievable",
                "fixture_proof_date": "2026-06-02",
                "fixture": {
                    "document_number": "2017-170648",
                    "internal_document_id": "94120155",
                    "document_type": "GIFT DEED",
                    "recorded_date": "2017-03-06",
                    "grantor": "DOLLAHITE JAMES W., AKA",
                    "grantee": "DOLLAHITE DARIN V.",
                    "page_count": 8,
                    "detail_url": "https://blanco.tx.publicsearch.us/doc/94120155",
                },
                "anonymous_search_proven": True,
                "viewer_image_endpoint": (
                    "https://blanco.tx.publicsearch.us/files/documents/"
                    "94120155/images/{image_base}_{page}.png"
                ),
                "image_byte_proof": {
                    "pages_fetched": 8,
                    "content_type": "image/png",
                    "first8_hex": "89 50 4e 47 0d 0a 1a 0a",
                    "page_lengths_bytes": [
                        60976, 33458, 76436, 84387,
                        85724, 81747, 79445, 79049,
                    ],
                },
                "certified_copy": "cart_required",
                "auto_import_allowed": True,
            },
        },
    },
}


def clerk_portal_for_county(county: str | None) -> dict[str, Any] | None:
    """The county's general/deed clerk portal record (name, url, fallback_*,
    deed_instructions, plat_instructions). None when the county isn't
    configured. Returns a copy. Back-compat shape for official_records:
    callers reading `instructions` get the deed instructions."""
    portal = _COUNTY_CLERK_PORTALS.get(_key(county))
    if not portal:
        return None
    out = dict(portal)
    # Back-compat: official_records callers historically read `instructions`.
    out.setdefault("instructions", out.get("deed_instructions"))
    return out


def clerk_lookup_steps(
    county: str | None, document_type: str = "deed",
) -> dict[str, Any] | None:
    """Document-type-aware lookup record for a county. `document_type` is
    "deed" or "plat". Returns {name, url, instructions, fallback_*} with the
    right URL (plat_url override when present for plats) and the right
    instructions. None when the county isn't configured."""
    portal = _COUNTY_CLERK_PORTALS.get(_key(county))
    if not portal:
        return None
    is_plat = str(document_type).lower() == "plat"
    url = portal.get("plat_url") if is_plat else portal.get("url")
    instructions = (
        portal.get("plat_instructions") if is_plat
        else portal.get("deed_instructions")
    )
    return {
        "name": portal.get("name"),
        "url": url or portal.get("url"),
        "instructions": instructions,
        "fallback_url": portal.get("fallback_url"),
        "fallback_label": portal.get("fallback_label"),
        "fallback_note": portal.get("fallback_note"),
        "document_type": "plat" if is_plat else "deed",
    }


def record_capabilities_for_county(county: str | None) -> dict[str, Any] | None:
    """Per-county record capabilities (CAD sources, search modes). Returns a
    copy, or None when the county isn't configured."""
    capabilities = _COUNTY_RECORD_CAPABILITIES.get(_key(county))
    return dict(capabilities) if capabilities else None


def cad_source_ids_for_county(county: str | None) -> list[str]:
    """The CAD parcel adapter source_ids that serve a county (e.g.
    ['tcad_parcel'] for Travis). Empty list when unknown. Used by the deed
    resolver to know which upstream adapter results to scan."""
    caps = _COUNTY_RECORD_CAPABILITIES.get(_key(county))
    sources = (caps or {}).get("cad_sources")
    return [s for s in sources if isinstance(s, str)] if isinstance(sources, list) else []


# Public re-export so `official_records.COUNTY_RECORD_CAPABILITIES` (a
# back-compat module attribute in `__all__`) can point at this one source.
COUNTY_RECORD_CAPABILITIES = _COUNTY_RECORD_CAPABILITIES


__all__ = [
    "COUNTY_RECORD_CAPABILITIES",
    "cad_source_ids_for_county",
    "clerk_lookup_steps",
    "clerk_portal_for_county",
    "record_capabilities_for_county",
]
