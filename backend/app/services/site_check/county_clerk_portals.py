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


def _key(county: str | None) -> str:
    return (county or "").lower().strip().removesuffix(" county").strip()


# Per-county canonical record:
#   name, url (deed/general portal), optional plat_url override,
#   deed_instructions, plat_instructions, and fallback_* where known.
_COUNTY_CLERK_PORTALS: dict[str, dict[str, Any]] = {
    "travis": {
        "name": "Travis County Clerk Official Public Records",
        "url": "https://www.tccsearch.org/",
        "deed_instructions": (
            "1) Open Travis County Clerk Web Search and acknowledge "
            "the disclaimer. 2) Use the Recording / Official Public "
            "Records search, not court dockets. 3) Search by document "
            "number when one is provided; otherwise search by party/"
            "grantor/grantee or legal description. 4) Open the matching "
            "recorded deed, download the image/copy if available, and "
            "upload it to the project."
        ),
        "plat_instructions": (
            "Use Travis County Clerk Web Search for recorded plats. "
            "Acknowledge the disclaimer, then search the Recording / "
            "Official Public Records records by Instrument/Document Number "
            "when PropDesk provides one. If no number is resolved, search "
            "document type Plat by the subdivision name or legal-description "
            "terms from TCAD."
        ),
        "fallback_url": (
            "https://countyclerk.traviscountytx.gov/departments/"
            "recording/search-copies-of-records/"
        ),
        "fallback_label": "County clerk recording help",
        "fallback_note": (
            "Official Travis County Clerk page describing recording "
            "search and copy options."
        ),
    },
    "williamson": {
        "name": "Williamson County Clerk Official Public Records",
        "url": (
            "https://williamsoncountytx-web.tylerhost.net/"
            "williamsonweb/search/DOCSEARCH149S1"
        ),
        "deed_instructions": (
            "1) Open Williamson County Clerk Official Public Record Search "
            "and Copies - Web. 2) If the disclaimer appears, click I Accept. "
            "3) Use the Instrument Number field for instrument numbers; do "
            "not use Williamson publicsearch, Commissioners Court minutes, "
            "judicial/court records, agendas, or case search. 4) Enter the "
            "instrument number exactly. 5) Open the matching recorded deed, "
            "download or purchase the PDF image/copy, and upload it."
        ),
        "plat_instructions": (
            "Use Official Public Record Search and Copies - Web. "
            "Search Instrument Number first when PropDesk provides a "
            "probable number. If no number is resolved, use document type "
            "Plat plus subdivision / lot / block terms. Do not use "
            "Commissioners Court minutes, judicial records, agendas, or "
            "case search for the recorded plat copy."
        ),
    },
    "hays": {
        "name": "Hays County Clerk Official Public Records",
        "url": "https://erss.co.hays.tx.us/web/user/disclaimer",
        "deed_instructions": (
            "1) Open the Hays County Clerk Self-Service portal and "
            "accept the disclaimer. 2) Use Property Records / Official "
            "Public Records, not District Clerk court records, jail "
            "look-up, or Commissioners Court archive records. "
            "3) Search by document/instrument number when one is provided; "
            "otherwise search by party name or recording date. 4) Open the "
            "matching recorded deed. 5) Select Purchase, choose COPIES and "
            "the page range, then Add to Cart or Proceed to Checkout. "
            "6) Enter the required customer and billing information, choose "
            "Credit Card or In Office payment, place the order, and upload "
            "the received copy."
        ),
        "plat_instructions": (
            "Open the Hays County Clerk Self-Service portal and accept the "
            "disclaimer. Use Property Records / Official Public Records. "
            "Search a probable instrument/document number first when present; "
            "otherwise search document type Plat plus subdivision / lot / "
            "block or party terms."
        ),
        "fallback_url": (
            "https://www.hayscountytx.gov/county-clerk/records-division"
        ),
        "fallback_label": "County clerk records page",
        "fallback_note": (
            "Official Hays County page linking to the Self-Service "
            "property records portal."
        ),
    },
    "bastrop": {
        "name": "Bastrop County Clerk Official Public Records",
        "url": "https://cc.co.bastrop.tx.us/",
        # Plat searches use the Kofile index portal (differs from the deed
        # path) — reconciled from plats_intel._MANUAL_PORTALS.
        "plat_url": "https://bastroptx.search.kofile.com/48109/Home/Index/1",
        "deed_instructions": (
            "1) Open Bastrop County Clerk Web Access and acknowledge "
            "the disclaimer. 2) Use Records / Official Public Records "
            "for land recordings, not probate, misdemeanor, or "
            "Commissioners Court records. 3) Search by instrument "
            "number when one is provided; otherwise search by party/"
            "grantor/grantee. 4) Open the matching recorded deed, download "
            "the image/copy if available, and upload it."
        ),
        "plat_instructions": (
            "Use the County Clerk Official Public Records search for "
            "recorded plats. Search probable instrument/document number "
            "first when present; otherwise search document type Plat and "
            "the subdivision / lot / block terms."
        ),
        "fallback_url": "https://www.bastropcounty.gov/page/co.county_clerk",
        "fallback_label": "County clerk page",
        "fallback_note": (
            "Official Bastrop County Clerk page linking to Records: "
            "Official Public Records."
        ),
    },
    "caldwell": {
        "name": "Caldwell County Clerk Official Public Records",
        "url": "https://tx.countygovernmentrecords.com/texas/web/login.jsp",
        "deed_instructions": (
            "1) Open Texas County Government Records for Caldwell County. "
            "2) Use County Clerk / Official Public Records / real property "
            "records, not District Clerk or court records. 3) Search by "
            "document number if one is provided; otherwise search by party "
            "name, recording date, or book/page. 4) Open the matching "
            "recorded deed, download/purchase the image/copy if available, "
            "and upload it."
        ),
        "plat_instructions": (
            "Use Texas County Government Records for Caldwell County Clerk "
            "Official Public Records / real property records. Search a "
            "probable document number first when one is resolved; "
            "otherwise search document type Plat plus subdivision / lot / "
            "block, party, date, or book/page terms."
        ),
        "fallback_url": "https://www.co.caldwell.tx.us/page/County.Clerk",
        "fallback_label": "County clerk page",
        "fallback_note": (
            "Official Caldwell County Clerk page; the County Clerk is the "
            "official record keeper for real-property Official Public "
            "Records and links to online records from 1990 to present."
        ),
    },
    "burnet": {
        "name": "Burnet County Clerk Official Public Records",
        "url": "https://burnetcountytx-web.tylerhost.net/web/user/disclaimer",
        "deed_instructions": (
            "1) Open Burnet County Clerk Self-Service and accept the "
            "disclaimer. 2) Use Official Public Records / property records, "
            "not court records. 3) Search by instrument/document number if "
            "one is provided; otherwise search by party name, recording "
            "date, or book/page. 4) Open the matching recorded deed, "
            "download the image/copy if available, and upload it."
        ),
        "plat_instructions": (
            "Use Burnet County Clerk Self-Service / Official Public Records. "
            "Search a probable instrument/document number first when present; "
            "otherwise search document type Plat plus subdivision / lot / "
            "block, party, recording date, or book/page terms."
        ),
        "fallback_url": "https://www.burnetcountytexas.org/page/cclerk.home",
        "fallback_label": "County clerk page",
        "fallback_note": (
            "Official Burnet County Clerk page describing deeds, maps, "
            "plats, and other property records."
        ),
    },
    "blanco": {
        "name": "Blanco County Clerk Official Public Records",
        "url": "https://blanco.tx.publicsearch.us/",
        "deed_instructions": (
            "1) Open Blanco County Official Records Search. 2) Use the "
            "property / official records search, not court records. "
            "3) Search by document number if one is provided; otherwise "
            "search party name, full-text OCR, recording date, or book/"
            "page. 4) Open the matching recorded deed, download the image/"
            "copy if available, and upload it."
        ),
        "plat_instructions": (
            "Use Blanco County Official Records Search for property / "
            "official records. Search a probable document number first when "
            "PropDesk resolves one; otherwise search subdivision / plat "
            "name, document type Plat, party, full-text OCR, recording "
            "date, or book/page terms."
        ),
        "fallback_url": "https://www.blancocountytexas.gov/page/County.Clerk",
        "fallback_label": "County clerk page",
        "fallback_note": (
            "Official Blanco County Clerk page links blanco.tx.publicsearch.us "
            "for property records, foreclosures, and marks/brands."
        ),
    },
}


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
