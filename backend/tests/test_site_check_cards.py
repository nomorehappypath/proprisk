"""Card-assembly contract tests (site_check.md §10) — every §10.5 run state,
every §10.4 status row, composite provenance, and the deterministic
formatter floor. Shapes are asserted against the frontend DTO
(propdesk_app web/src/lib/publicData.ts + scripts/fixtures/public_data)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.site_check import cards

NEW_TS = "2026-07-28T15:00:00Z"
OLD_TS = "2026-01-12T15:00:00Z"

SETTINGS = SimpleNamespace(
    site_check_freshness_days=180, site_check_expiring_notice_days=30,
)

# Frontend DTO key sets (publicData.ts) — asserted EXACTLY.
CARD_KEYS = {
    "card_key", "title", "status", "takeaway", "facts", "tables", "links",
    "image", "images", "advisories", "sources", "checked_at", "narrative",
    "retryable",
    # v4 accordion identity — the report screen is a flat ordered list of
    # collapsible sections, and the collapsed row is drawn entirely from
    # these: glyph, place in the order, compact summary, opening line and
    # the confidence badge.
    "icon", "order", "summary", "hero", "confidence",
}
TABLE_KEYS = {"title", "columns", "rows"}
LINK_KEYS = {"label", "url"}
IMAGE_KEYS = {"url", "caption", "attribution"}
# v3 gallery entries carry a `label` (the photo year) on top of an image.
GALLERY_IMAGE_KEYS = IMAGE_KEYS | {"label"}
PROVENANCE_KEYS = {
    "source_id", "source_name", "source_url", "checked_at", "from_cache",
}
PAYLOAD_KEYS = {"groups", "bottom_line", "manual_checks", "anchor_note"}
GROUP_KEYS_IN_ORDER = [
    "land", "flood", "zoning", "roads", "utilities", "environment", "area",
]
NARRATIVE_FIELDS = {
    "headline", "broker_read", "why_it_matters", "next_step", "caveat",
}


def result(source_id, facts=None, *, errors=None, coverage="covered",
           access="PROGRAMMATIC_PUBLIC", fetched_at=NEW_TS, stale=False,
           refresh_status="fetched"):
    payload = {
        "source_id": source_id,
        "coverage_status": coverage,
        "source_access_status": access,
        "data_confidence_status": "verified_public_fact",
        "normalized_facts": facts or {},
        "fetched_at": fetched_at,
        "refresh_status": refresh_status,
    }
    if errors:
        payload["errors"] = errors
    if stale:
        payload["stale"] = True
    return payload


def make_run(status="completed", adapter_results=None, sources_failed=None,
             derived=None, anchor_quality="verified"):
    return SimpleNamespace(
        id="run-1", tenant_id="t1", requested_by="u1",
        address="500 E Anderson Ln, Austin", parcel_id=None, county="travis",
        status=status, error_code=None, failure_reason=None,
        adapter_results=adapter_results if adapter_results is not None else {},
        sources_queried=[], sources_failed=sources_failed or [],
        derived=derived or {}, anchor_kind="geocode",
        anchor_quality=anchor_quality,
    )


def find_card(payload, card_key):
    for group in payload["groups"]:
        for card in group["cards"]:
            if card["card_key"] == card_key:
                return card
    return None


def all_cards(payload):
    return [card for group in payload["groups"] for card in group["cards"]]


# ── Realistic normalized_facts samples (adapter/panel key shapes) ──────────

FEMA_CLEAR = {"status": "clear", "zone": "X", "in_floodplain": False,
              "has_sfha": False}
FEMA_SFHA = {"status": "mixed", "zone": "AE", "in_floodplain": True,
             "has_sfha": True, "base_flood_elevation": 512.0,
             "zones_found": ["AE", "X"]}
PARCEL_FACTS = {"parcel_id": "R123456", "acreage": 11.9,
                "owner_name": "ACME Land LP", "county": "travis",
                "situs": "500 E ANDERSON LN", "market_value": 1450000}
AADT_FACTS = {"traffic_counts": [
    {"road_name": "IH 35", "aadt": 24000, "year": "2024"},
    {"road_name": "E Anderson Ln", "aadt": 9800, "year": "2023"},
]}
HISTORY_FACTS = {"representative_station": {
    "on_road": "IH 35", "latest_aadt": 24500,
    "trend_summary": {"change_5yr_pct": 12.0, "direction": "up"},
}}
ZONING_GR = {"jurisdiction": "Austin", "zoning_type": "GR",
             "jurisdiction_kind": "city"}
WETLANDS_MIXED = {"status": "mixed",
                  "wetland_types_seen": ["Freshwater Pond"],
                  "wetland_features": [{"wetland_type": "Freshwater Pond",
                                        "attribute": "PUBHh", "acres": 0.4}],
                  "envelope_radius_m": 250}
ENV_ZERO = {"status": "clear", "pst_records": [], "echo_records": [],
            "search_radius_m": 500,
            "source_statuses": {"tceq_pst": "queried", "epa_echo": "queried"}}
CENSUS_FACTS = {"rings": {
    "1mi": {"population": 5400, "median_household_income": 68000},
    "3mi": {"population": 41000},
}}
OSM_FACTS = {"category_groups": {"food": {"group_total_5mi": 80},
                                 "retail": {"group_total_5mi": 40}}}
JURIS_FACTS = {"jurisdiction_kind": "city", "city": "Austin",
               "county": "travis",
               "broker_summary": "Inside Austin full-purpose city limits."}


def completed_run():
    """A realistic completed run: mixed successes, one transient failure."""
    return make_run(
        status="completed",
        adapter_results={
            "tcad_parcel": result("tcad_parcel", PARCEL_FACTS),
            "fema_nfhl": result("fema_nfhl", FEMA_CLEAR),
            "wetlands_nwi": result("wetlands_nwi", WETLANDS_MIXED),
            "environmental_screening": result(
                "environmental_screening", ENV_ZERO),
            "jurisdiction_resolver": result(
                "jurisdiction_resolver", JURIS_FACTS),
            "austin_zoning": result("austin_zoning", ZONING_GR),
            "txdot_aadt": result("txdot_aadt", AADT_FACTS),
            "txdot_aadt_history": result(
                "txdot_aadt_history", HISTORY_FACTS,
                fetched_at=OLD_TS, refresh_status="reused_recent"),
            "census_acs": result("census_acs", CENSUS_FACTS),
            "osm_overpass": result("osm_overpass", OSM_FACTS),
            "txdot_projects": result(
                "txdot_projects", {},
                errors=["504 Server Error: Gateway Time-out"],
                fetched_at=None),
        },
        sources_failed=[{"source": "txdot_projects",
                         "error": "504 Server Error: Gateway Time-out",
                         "failed_at": NEW_TS}],
    )


# ── Payload + card shapes vs the frontend DTO ──────────────────────────────


def test_payload_shape_and_group_map():
    payload = cards.build_cards(completed_run(), SETTINGS)
    assert set(payload.keys()) == PAYLOAD_KEYS
    assert [g["key"] for g in payload["groups"]] == GROUP_KEYS_IN_ORDER
    titles = {g["key"]: g["title"] for g in payload["groups"]}
    assert titles["land"] == "The land itself"
    assert titles["flood"] == "Flood & water"
    assert titles["area"] == "Area & people"
    assert set(payload["bottom_line"].keys()) == {"text", "kind", "checked_at"}


def test_card_shape_matches_frontend_dto():
    payload = cards.build_cards(completed_run(), SETTINGS)
    for card in all_cards(payload):
        assert set(card.keys()) == CARD_KEYS, card["card_key"]
        assert card["status"] in (
            "ok", "issue", "not_covered", "manual", "not_checked")
        assert isinstance(card["takeaway"], str) and card["takeaway"]
        assert len(card["sources"]) >= 1
        for src in card["sources"]:
            assert set(src.keys()) == PROVENANCE_KEYS
            assert isinstance(src["from_cache"], bool)
        for row in card["facts"]:
            assert set(row.keys()) <= {"label", "value", "hint", "source_id"}
            assert isinstance(row["value"], str)
        for table in card["tables"]:
            assert set(table.keys()) == TABLE_KEYS
            assert table["title"] and table["columns"] and table["rows"]
            for table_row in table["rows"]:
                assert len(table_row) == len(table["columns"])
                assert all(isinstance(cell, str) for cell in table_row)
        for link in card["links"]:
            assert set(link.keys()) == LINK_KEYS
            assert link["url"].startswith("http")
        if card["image"] is not None:
            assert set(card["image"].keys()) == IMAGE_KEYS
            assert card["image"]["url"].startswith("http")
        assert isinstance(card["images"], list)
        for gallery_image in card["images"]:
            assert set(gallery_image.keys()) == GALLERY_IMAGE_KEYS
            assert gallery_image["url"].startswith("http")
        if card["narrative"] is not None:
            assert set(card["narrative"].keys()) == NARRATIVE_FIELDS
            assert all(isinstance(v, str)
                       for v in card["narrative"].values())


# ── §10.4 status rows ──────────────────────────────────────────────────────


def test_ok_card():
    payload = cards.build_cards(completed_run(), SETTINGS)
    card = find_card(payload, "fema_nfhl")
    assert card["status"] == "ok"
    assert "Zone X" in card["takeaway"]
    assert card["checked_at"] == NEW_TS


def test_issue_card_copy_retry_and_broker_safe_detail():
    payload = cards.build_cards(completed_run(), SETTINGS)
    card = find_card(payload, "txdot_projects")
    assert card["status"] == "issue"
    assert card["takeaway"] == "We couldn't get a full answer from Road projects."
    assert card["retryable"] is True
    joined = " ".join(card["advisories"])
    assert "504" not in joined and "http" not in joined.lower()
    assert "took too long" in joined or "temporary" in joined


def test_not_covered_honest_scope_line_outside_texas():
    run = make_run(adapter_results={
        "census_acs": result(
            "census_acs", {}, coverage="not_supported_in_jurisdiction"),
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR),
    })
    payload = cards.build_cards(run, SETTINGS)
    card = find_card(payload, "census_acs")
    assert card["status"] == "not_covered"
    assert "Texas" in card["takeaway"]
    assert card["facts"] == []
    assert card["retryable"] is False
    lowered = card["takeaway"].lower()
    assert "error" not in lowered and "fail" not in lowered


def test_manual_card_keeps_the_source_own_content():
    """A source that needs a human look still renders its real findings —
    the ported panel sections do not blank out on a manual verdict."""
    run = make_run(adapter_results={
        "plats_intel": result(
            "plats_intel",
            {"advisory": "open the county plat page and search this address.",
             "manual_lookup_url": "https://county.example/plats",
             "county": "travis",
             "portal_name": "Travis County Clerk",
             "portal_url": "https://county.example/plats",
             "manual_next_step": "Search the clerk records by owner name."},
            coverage="manual_verification_required",
            access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "plats_intel")
    assert card["status"] == "manual"
    assert "recorded subdivision plat" in card["takeaway"]
    assert not card["takeaway"].startswith("This one needs a human look")
    assert any("Search the clerk records" in row["value"]
               for row in card["facts"])
    assert card["links"] == [
        {"label": "Travis County Clerk", "url": "https://county.example/plats"}]
    assert card["sources"][0]["source_url"] == "https://county.example/plats"
    assert card["retryable"] is False


def test_manual_card_generic_pointer_for_an_unported_source(monkeypatch):
    """A future source with no ported section falls back to the plain
    pointer line rather than rendering nothing."""
    monkeypatch.setattr(
        cards, "_SELF_WORDED", cards._SELF_WORDED - {"plats_intel"})
    monkeypatch.setitem(cards._BUILDERS, "plats_intel",
                        lambda facts: cards._Content("placeholder"))
    run = make_run(adapter_results={
        "plats_intel": result(
            "plats_intel",
            {"advisory": "open the county plat page and search this address."},
            coverage="manual_verification_required",
            access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "plats_intel")
    assert card["takeaway"] == (
        "This one needs a human look — open the county plat page and search "
        "this address.")


def test_not_yet_wired_stub_card_is_omitted():
    run = make_run(adapter_results={
        "soil_intel": result("soil_intel", {}, coverage="not_yet_wired"),
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR),
    })
    payload = cards.build_cards(run, SETTINGS)
    assert find_card(payload, "soil_intel") is None


HUTTO_UNWIRED_FACTS = {
    "jurisdiction": "HUTTO",
    "source_status": "not_wired_for_jurisdiction",
    "case_portal_name": "Hutto Planning & Development",
    "case_portal_url": "https://hutto.example/planning",
    "search_radius_m": 800,
    "subdivision_radius_m": 3200,
    "subdivision_cases": [
        {"name": "Thomas Park Sub Sec 2", "status": "FPA", "agency": "WC",
         "distance_m": 390.4, "direction": "south-west", "acres": 55.11,
         "final_plat_date_ms": 843195600000},
        {"name": "Firefly Pointe Ph 2", "status": "FPA", "agency": "WC",
         "distance_m": 5.0, "lot_count": 258, "acres": 44.81,
         "final_plat_date_ms": 1730700000000},
    ],
    "subdivision_case_count": 41,
    "subdivision_total_lots": 2103,
    "subdivision_largest": {
        "name": "Flora Ph 2 Sec 1", "lot_count": 435,
        "distance_m": 2881.0, "direction": "south-east"},
    "entitlement_read": {
        "why_it_matters": "What is approved nearby sets the competition.",
        "next_step": "Search the planning page by address."},
}


def test_unwired_entitlement_leads_with_the_records_it_actually_found():
    """The city having no readable case feed is a CAVEAT, never the takeaway:
    when recorded plats came back, the card must lead with them — count,
    nearest, and the largest project — because that IS the answer to 'what is
    being built near me'."""
    run = make_run(adapter_results={
        "entitlement_pipeline": result(
            "entitlement_pipeline", dict(HUTTO_UNWIRED_FACTS),
            coverage="not_yet_wired", access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "entitlement_pipeline")
    assert card is not None
    takeaway = card["takeaway"]
    assert takeaway.startswith("41 recorded subdivision plats within 2.0 mi")
    assert "Thomas Park Sub Sec 2" in takeaway
    assert "Flora Ph 2 Sec 1" in takeaway and "435 lots" in takeaway
    # The old lede must not be the headline any more.
    assert "does not" not in takeaway and "no readable" not in takeaway
    assert any(table["rows"] for table in card["tables"])
    assert card["links"][0]["url"] == "https://hutto.example/planning"

    plats = next(t for t in card["tables"]
                 if t["title"].startswith("Recorded development"))
    assert plats["columns"][:4] == [
        "Subdivision", "How far", "Stage", "Homes/lots"]
    # Distance + direction render; a sub-160 m case is given in feet and gets
    # no compass word it was never handed.
    assert plats["rows"][0][1] == "0.2 mi south-west"
    assert plats["rows"][1][1] == "20 ft away"
    assert plats["rows"][1][3] == "258"
    assert plats["rows"][0][5] == "Williamson County"

    # The missing city feed survives — as a caveat, below.
    assert any("only as web pages" in advisory
               for advisory in card["advisories"])
    assert any("none of them is a plan for this land itself" in advisory
               for advisory in card["advisories"])


def test_unwired_entitlement_with_no_records_says_so_plainly():
    facts = {key: value for key, value in HUTTO_UNWIRED_FACTS.items()
             if not key.startswith("subdivision")}
    run = make_run(adapter_results={
        "entitlement_pipeline": result(
            "entitlement_pipeline", facts,
            coverage="not_yet_wired", access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "entitlement_pipeline")
    assert card["takeaway"].startswith("No recorded plats or posted cases")
    assert "check the planning page below" in card["takeaway"]


def test_city_case_reads_render_as_their_own_table_with_sources():
    """The AI-read city cases merge into the SAME card as a second table,
    each row traceable to the page it was verified against."""
    facts = dict(HUTTO_UNWIRED_FACTS)
    facts["city_case_reads"] = {
        "cases": [{
            "name": "Chandler Parkade",
            "case_type": "annexation",
            "status": "Annexed",
            "date": "August 5, 2021",
            "applicant_or_developer": "Lennar Homes",
            "location_text": "North side of Chandler Road",
            "source_url": "https://hutto.example/212/Annexations",
            "evidence_quote": "Chandler Parkade | O-2021-045",
            "verification": "name_matched",
        }],
        "dropped_unverified": 2,
        "coverage_note": "Hutto publishes an annexation table only.",
    }
    run = make_run(adapter_results={
        "entitlement_pipeline": result(
            "entitlement_pipeline", facts,
            coverage="not_yet_wired", access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "entitlement_pipeline")
    city = next(t for t in card["tables"]
                if t["title"].startswith("Cases published by"))
    assert city["title"] == "Cases published by Hutto Planning & Development"
    assert city["rows"][0][0] == "Chandler Parkade"
    assert city["rows"][0][4] == "Lennar Homes"
    assert city["rows"][0][5] == "North side of Chandler Road"
    assert any(link["url"] == "https://hutto.example/212/Annexations"
               for link in card["links"])
    # The dropped ones are stated, not hidden.
    assert any("2 further possible cases could not be confirmed" in advisory
               for advisory in card["advisories"])
    assert any("re-opened the source" in advisory
               for advisory in card["advisories"])


def test_card_without_reader_results_is_unchanged_by_the_reader():
    """No reader results ⇒ no city table, no verification advisory, and no
    claim about cases that were never found."""
    run = make_run(adapter_results={
        "entitlement_pipeline": result(
            "entitlement_pipeline", dict(HUTTO_UNWIRED_FACTS),
            coverage="not_yet_wired", access="HTML_OR_PDF_ONLY"),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "entitlement_pipeline")
    assert not any(t["title"].startswith("Cases published by")
                   for t in card["tables"])
    assert not any("re-opened the source" in advisory
                   for advisory in card["advisories"])
    assert not any("could not be confirmed" in advisory
                   for advisory in card["advisories"])


def test_not_checked_card_on_cancelled_run():
    run = make_run(status="cancelled", adapter_results={
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR),
    })
    payload = cards.build_cards(run, SETTINGS)
    fetched = find_card(payload, "fema_nfhl")
    assert fetched["status"] == "ok"  # staged waves render normally
    unfetched = find_card(payload, "parcel")
    assert unfetched["status"] == "not_checked"
    assert unfetched["takeaway"] == (
        "Not checked — the lookup was cancelled before this source.")
    assert unfetched["sources"][0]["checked_at"] is None
    assert unfetched["retryable"] is False


# ── Composite cards + provenance (§10.1) ───────────────────────────────────


def test_composite_traffic_reused_history_and_retried_aadt():
    """REQUIRED fixture: AADT history reused (older date) + AADT retried
    (newer date) — both dates present, rollup = the older one."""
    payload = cards.build_cards(completed_run(), SETTINGS)
    card = find_card(payload, "traffic")
    assert card["status"] == "ok"
    by_id = {src["source_id"]: src for src in card["sources"]}
    assert set(by_id) == {"txdot_aadt", "txdot_aadt_history"}
    assert by_id["txdot_aadt"]["checked_at"] == NEW_TS
    assert by_id["txdot_aadt_history"]["checked_at"] == OLD_TS
    assert card["checked_at"] == OLD_TS  # oldest contributor
    attributed = {row.get("source_id") for row in card["facts"]}
    assert "txdot_aadt" in attributed
    assert "txdot_aadt_history" in attributed
    assert "24,000" in card["takeaway"]


def test_parcel_first_resolved_precedence_wcad_wins():
    run = make_run(adapter_results={
        "wcad_parcel": result("wcad_parcel", {
            "parcel_id": "W555", "acreage": 3.2, "county": "williamson",
            "owner_name": "RANCH LLC"}),
        "tcad_parcel": result(
            "tcad_parcel", {}, errors=["503 Service Unavailable"],
            fetched_at=None),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "parcel")
    assert card["status"] == "ok"  # the losing source's noise stays hidden
    assert [src["source_id"] for src in card["sources"]] == ["wcad_parcel"]
    assert any("3.2" in row["value"] for row in card["facts"])


def test_parcel_tcad_general_path():
    run = make_run(adapter_results={
        "tcad_parcel": result("tcad_parcel", PARCEL_FACTS),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "parcel")
    assert card["status"] == "ok"
    assert card["sources"][0]["source_id"] == "tcad_parcel"
    assert "11.9 acres" in card["takeaway"]
    assert "Travis County" in card["takeaway"]


def test_cache_stale_marks_from_cache():
    run = make_run(adapter_results={
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR, stale=True),
    })
    card = find_card(cards.build_cards(run, SETTINGS), "fema_nfhl")
    assert card["sources"][0]["from_cache"] is True


# ── §10.5 / §8.3 run states ────────────────────────────────────────────────


def test_cancelled_zero_staged_full_scaffold():
    run = make_run(status="cancelled", adapter_results={})
    payload = cards.cards_or_none(run, SETTINGS)
    assert payload is not None
    assert [g["key"] for g in payload["groups"]] == GROUP_KEYS_IN_ORDER
    scaffold = all_cards(payload)
    assert scaffold  # every group serves its cards
    assert all(card["status"] == "not_checked" for card in scaffold)
    assert all(card["checked_at"] is None for card in scaffold)
    assert payload["bottom_line"] == {
        "text": "", "kind": "deterministic", "checked_at": None}
    # Every mapped slot is present in the scaffold.
    keys = {card["card_key"] for card in scaffold}
    assert {"parcel", "fema_nfhl", "traffic", "jurisdiction",
            "environmental_screening", "census_acs"} <= keys


def test_failed_partial_keeps_staged_and_marks_rest():
    run = make_run(status="failed", adapter_results={
        "fema_nfhl": result("fema_nfhl", FEMA_SFHA),
    })
    payload = cards.cards_or_none(run, SETTINGS)
    assert payload is not None
    assert find_card(payload, "fema_nfhl")["status"] == "ok"
    rest = find_card(payload, "traffic")
    assert rest["status"] == "not_checked"
    assert rest["takeaway"] == (
        "Not checked — the lookup stopped before this source.")


def test_two_class_rule_nulls():
    assert cards.cards_or_none(
        make_run(status="failed", adapter_results={}), SETTINGS) is None
    assert cards.cards_or_none(
        make_run(status="queued", adapter_results={}), SETTINGS) is None
    assert cards.cards_or_none(
        make_run(status="running", adapter_results={}), SETTINGS) is None
    # Staged results → payload (completed / failed-partial); cancelled always.
    assert cards.cards_or_none(completed_run(), SETTINGS) is not None
    assert cards.cards_or_none(
        make_run(status="cancelled", adapter_results={}), SETTINGS) is not None


# ── Bottom line: deterministic floor + AI overlay + ladder message ─────────


def test_no_ai_deterministic_bottom_line():
    payload = cards.build_cards(completed_run(), SETTINGS)
    line = payload["bottom_line"]
    assert line["kind"] == "deterministic"
    text = line["text"]
    assert "Zone X" in text or "minimal flood risk" in text
    assert "24,000 vehicles a day" in text
    assert "zoned GR" in text
    assert "120 places nearby" in text
    assert "5,400 people within a mile" in text
    assert line["checked_at"] == OLD_TS  # oldest successful source
    assert all(card["narrative"] is None for card in all_cards(payload))


def test_ai_overlay_fills_narrative_and_bottom_line():
    run = completed_run()
    run.derived = {
        "narratives": {
            "fema_nfhl": {
                "headline": "Low flood risk",
                "broker_read": "The site sits outside the mapped flood zones.",
                "why_it_matters": "No flood insurance requirement.",
                "next_step": None,
                "caveat": None,
            },
        },
        "narrative_bottom_line": (
            "Low flood risk and strong traffic — worth a closer look."),
        "narrative_meta": {"status": "ok", "model": "gpt-5.6-terra",
                           "attempts": 1, "ai_call_id": 7},
    }
    payload = cards.build_cards(run, SETTINGS)
    card = find_card(payload, "fema_nfhl")
    assert card["narrative"]["headline"] == "Low flood risk"
    assert card["narrative"]["next_step"] == ""  # nulls become strings (DTO)
    assert payload["bottom_line"]["kind"] == "ai"
    assert payload["bottom_line"]["text"].startswith("Low flood risk")
    assert payload["bottom_line"]["checked_at"] == OLD_TS
    # Deterministic takeaway is still present underneath (the no-AI floor).
    assert card["takeaway"]


def test_fallback_ladder_message_rides_with_deterministic_line():
    run = completed_run()
    run.derived = {
        "narratives": {},
        "narrative_meta": {
            "status": "fallback_no_key", "model": "gpt-5.6-terra",
            "attempts": 0,
            "message": "Add your OpenAI key in Settings to get written summaries.",
        },
    }
    line = cards.build_cards(run, SETTINGS)["bottom_line"]
    assert line["kind"] == "deterministic"
    assert "Add your OpenAI key in Settings" in line["text"]
    assert "vehicles a day" in line["text"]  # deterministic summary kept


# ── Anchor note + manual follow-ups ────────────────────────────────────────


def test_anchor_note_provisional_only():
    provisional = make_run(anchor_quality="provisional", adapter_results={
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR)})
    payload = cards.build_cards(provisional, SETTINGS)
    assert payload["anchor_note"] == cards.ANCHOR_NOTE
    assert "survey or recorded plat" in payload["anchor_note"]
    verified = make_run(anchor_quality="verified", adapter_results={
        "fema_nfhl": result("fema_nfhl", FEMA_CLEAR)})
    assert cards.build_cards(verified, SETTINGS)["anchor_note"] is None


def test_manual_checks_from_derived():
    run = completed_run()
    run.derived = {"manual_lookups_needed": [
        {"item": "Flood zone - manual lookup",
         "why": "FEMA's flood map didn't give a clear answer here.",
         "who_to_contact": "The county flood office - search this address"},
        {"item": "Road projects - fetch failed",
         "why": "The road-projects list did not answer.",
         "who_to_contact": "https://apps3.txdot.gov/project_tracker/"},
    ]}
    payload = cards.build_cards(run, SETTINGS)
    manuals = payload["manual_checks"]
    assert len(manuals) == 2
    assert all(set(m.keys()) == {"label", "reason", "link"} for m in manuals)
    assert manuals[0]["label"] == "Flood zone"
    # A non-URL pointer is never silently dropped: it rides in the reason.
    assert manuals[0]["link"] is None
    assert "Where to look: The county flood office" in manuals[0]["reason"]
    assert manuals[1]["link"].startswith("https://")
    assert "Where to look" not in manuals[1]["reason"]  # URL renders as a link


def test_manual_lookups_copy_is_plain_english():
    """§2/§14: the orchestrator's manual-lookup strings carry no agency
    jargon and always carry a usable pointer (link or in-reason text)."""
    from app.services.site_check.orchestrator import _build_manual_lookups

    items = _build_manual_lookups({
        "fema_nfhl": {"normalized_facts": {"status": "unknown"}},
        "txdot_aadt": {"normalized_facts": {"traffic_counts": []}},
        "txdot_projects": {"normalized_facts": {}, "errors": ["timeout"]},
    })
    assert len(items) == 3
    joined = " ".join(f"{i['item']} {i['why']}" for i in items)
    for token in ("SFHA", "NFHL", "FIRM", "LOMA", "LOMR", "AADT",
                  "timed out", "errored", "Re-run analysis"):
        assert token not in joined, token
    # The FEMA pointer survives into the rendered card as a real link.
    flood = items[0]
    assert flood["who_to_contact"].startswith("https://msc.fema.gov")
    run = completed_run()
    run.derived = {"manual_lookups_needed": items}
    manuals = cards.build_cards(run, SETTINGS)["manual_checks"]
    assert manuals[0]["link"] == flood["who_to_contact"]


# ── §10.3 formatter unit tests (the six headline sources) ──────────────────


def values_of(content):
    return {row["label"]: row["value"] for row in content.facts}


def test_formatter_city_sewer_precedes_nearest_ccn_context():
    content = cards._format_twdb(
        {
            "water_provider": "AQUA WSC",
            "wastewater_nearest": {
                "provider": "CITY OF ELGIN", "within_m": 2000, "ccn_no": "20120",
            },
            "availability_request_text": "To CITY OF ELGIN:\n\nPlease confirm service.",
        },
        {},
        {"city": "Manor", "jurisdiction_kind": "full_purpose_city"},
    )
    values = values_of(content)
    assert "contact City of Manor first" in content.takeaway
    assert values["Sewer provider"] == "City of Manor"
    assert values["Nearest certificated sewer area (context only)"] == (
        "City Of Elgin · about 2.0 km away · certificate 20120"
    )
    assert values["Ready-to-send request to the provider"].startswith(
        "To City of Manor:")


def test_formatter_fema():
    content = cards._format_fema(FEMA_CLEAR)
    assert content.takeaway == (
        "Outside the mapped flood zones (Zone X — minimal risk).")
    assert values_of(content)["FEMA flood zone"] == "Zone X"
    content = cards._format_fema(FEMA_SFHA)
    assert "Inside a mapped flood zone (Zone AE)" in content.takeaway
    values = values_of(content)
    assert values["Base flood elevation"] == "512 ft"
    assert values["Inside a high-risk flood zone"] == "Yes"
    assert values["All zones touching this site"] == "AE, X"
    content = cards._format_fema({"status": "unknown"})
    assert "could not be determined" in content.takeaway
    assert "verify" in content.takeaway.lower()
    assert content.links[0]["url"].startswith("https://msc.fema.gov")


def test_formatter_parcel():
    content = cards._format_parcel(PARCEL_FACTS)
    assert content.takeaway == (
        "11.9 acres in Travis County — parcel R123456 on the county "
        "appraisal roll.")
    values = values_of(content)
    assert values["Size"] == "11.9 acres"
    assert values["Owner on record"] == "ACME Land LP"
    assert values["Parcel ID"] == "R123456"
    assert values["Appraised market value"] == "$1,450,000"
    assert values["Address on record"] == "500 E Anderson Ln"
    assert "did not return a matching parcel" in cards._format_parcel(
        {}).takeaway
    # No exact match, but the county offered neighbours: say so and list them.
    candidates = cards._format_parcel({
        "county": "travis", "status": "nearby_candidates_only",
        "nearby_candidates": [
            {"parcel_id": "200164", "acreage": 2.03, "cad_account": "0210040114",
             "owner_name": "TEXAS PUBLIC FINANCE AUTHORITY"},
        ]})
    assert "closest parcels are listed below" in candidates.takeaway
    assert candidates.tables[0]["rows"][0][0] == "200164"


def test_formatter_austin_zoning():
    content = cards._format_austin_zoning(ZONING_GR)
    assert content.takeaway == "Zoned GR in Austin."
    assert values_of(content)["Zoning district"] == "GR"
    assert "No city zoning applies here" in cards._format_austin_zoning(
        {"jurisdiction": "Travis County",
         "zoning_type": "NO_CITY_ZONING"}).takeaway
    # An ETJ verdict is a real answer, not a blank: say what still applies.
    etj = cards._format_austin_zoning(
        {"jurisdiction": "HUTTO ETJ", "jurisdiction_kind": "etj",
         "zoning_type": "HUTTO_ETJ", "base_zone": "ETJ",
         "description": "Verified HUTTO extraterritorial jurisdiction."})
    assert "extended area" in etj.takeaway
    assert "subdivision and development review" in etj.takeaway
    assert values_of(etj)["What this means"].startswith("Verified HUTTO")
    content = cards._format_austin_zoning(
        {"jurisdiction": "Hutto", "zoning_read_status": "use_action_only",
         "zoning_use_action": "SUP-2021-014"})
    assert "no published zoning district" in content.takeaway
    assert "zoning letter" in content.takeaway
    assert any(row["value"] == "SUP-2021-014" for row in content.facts)
    # Missing everything: never claims "no zoning".
    unknown = cards._format_austin_zoning({"jurisdiction": "Austin"})
    assert "could not be determined" in unknown.takeaway
    assert "unzoned" in unknown.takeaway


def test_formatter_traffic():
    content = cards._format_traffic(AADT_FACTS, HISTORY_FACTS)
    assert content.takeaway.startswith(
        "About 24,000 vehicles a day pass on IH 35.")
    assert "rising" in content.takeaway
    trend = [row for row in content.facts
             if row["label"] == "Five-year change"]
    assert trend and trend[0]["value"] == "Up 12%"
    assert trend[0]["source_id"] == "txdot_aadt_history"
    counts = content.tables[0]
    assert counts["columns"][:2] == ["Road", "Vehicles per day"]
    assert counts["rows"][1][0] == "E Anderson Ln"
    assert "No traffic counts were published" in cards._format_traffic(
        {}, {}).takeaway


def test_formatter_wetlands():
    content = cards._format_wetlands(WETLANDS_MIXED)
    assert "Mapped wetland features" in content.takeaway
    assert "1 type" in content.takeaway
    # The Cowardin code becomes a practical read, not a code dump.
    assert "Impounded freshwater pond" in content.tables[0]["rows"][0][2]
    assert cards._format_wetlands({"status": "clear"}).takeaway == (
        "No mapped wetland features on or near this site.")
    unknown = cards._format_wetlands({"status": "unknown"})
    assert "could not finish" in unknown.takeaway
    assert "not as all clear" in unknown.takeaway


def test_formatter_environmental_honesty():
    # Zero records: a LIMITED screen — never "clean", never "no contamination".
    content = cards._format_environmental(ENV_ZERO)
    assert "limited screen" in content.takeaway
    assert "Phase I" in content.takeaway
    assert "clean" not in content.takeaway.lower()
    assert "no contamination" not in content.takeaway.lower()
    # Records present: counted plainly.
    hits = dict(ENV_ZERO, pst_records=[{"facility_name": "A"}],
                echo_records=[{"facility_name": "B"}, {"facility_name": "C"}])
    content = cards._format_environmental(hits)
    assert content.takeaway.startswith("3 environmental records within 500 m")
    # A source that could not answer is never an all-clear.
    partial = dict(ENV_ZERO, source_statuses={
        "tceq_pst": "queried", "epa_echo": "temporary_rate_limited"})
    content = cards._format_environmental(partial)
    assert "incomplete" in content.takeaway
    assert "do not treat this as an all-clear" in content.takeaway
    values = values_of(content)
    assert "not an all-clear" in values["Federally regulated facilities"]


# ── §9.4 freshness helpers at a NON-DEFAULT value ──────────────────────────


def test_freshness_ladder_derives_from_settings():
    from datetime import UTC, datetime

    settings = SimpleNamespace(
        site_check_freshness_days=10, site_check_expiring_notice_days=3)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    assert cards.freshness_band(
        "2026-07-22T00:00:00Z", settings, now) == "current"       # age 6
    assert cards.freshness_band(
        "2026-07-20T00:00:00Z", settings, now) == "expiring"      # age 8
    assert cards.freshness_band(
        "2026-07-18T00:00:00Z", settings, now) == "expired"       # age 10
    refresh = cards.refresh_after_iso("2026-07-22T00:00:00Z", settings)
    assert refresh.startswith("2026-08-01")  # +10 days, not +180
    assert cards.freshness_band(None, settings, now) is None


def test_oldest_checked_and_headline_takeaway_helpers():
    run = completed_run()
    assert cards.oldest_checked_at_for_run(run) == OLD_TS
    headline = cards.headline_takeaway(run)
    assert headline and "vehicles a day" in headline
    assert cards.headline_takeaway(
        make_run(adapter_results={})) is None


# ── Copy discipline: the NOUN "check" never appears in rendered strings ────


def _rendered_strings(payload):
    for card in all_cards(payload):
        yield card["title"]
        yield card["takeaway"]
        yield from card["advisories"]
        for row in card["facts"]:
            yield row["label"]
            yield row["value"]
            if row.get("hint"):
                yield row["hint"]
        for table in card["tables"]:
            yield table["title"]
            yield from table["columns"]
            for table_row in table["rows"]:
                yield from table_row
        for link in card["links"]:
            yield link["label"]
        if card["image"]:
            yield card["image"]["caption"]
            yield card["image"]["attribution"]
        for src in card["sources"]:
            yield src["source_name"]
    yield payload["bottom_line"]["text"]
    for manual in payload["manual_checks"]:
        yield manual["label"]
        yield manual["reason"]
    if payload["anchor_note"]:
        yield payload["anchor_note"]


def test_no_bare_check_token_in_rendered_strings():
    """'checked'/'checking' (verbs) are allowed; the bare noun token is not
    (spec §2 — a run is a 'lookup', its result a 'report')."""
    for state in ("completed", "cancelled", "failed"):
        run = completed_run()
        run.status = state
        payload = cards.build_cards(run, SETTINGS)
        for text in _rendered_strings(payload):
            assert not re.search(r"\bcheck\b", text, re.IGNORECASE), text
    scaffold = cards.build_cards(
        make_run(status="cancelled", adapter_results={}), SETTINGS)
    for text in _rendered_strings(scaffold):
        assert not re.search(r"\bcheck\b", text, re.IGNORECASE), text


def test_errorless_partial_coverage_is_ok_not_issue():
    """Refined §10.4 (2026-08-01): coverage 'partial' with NO errors is a
    screening-level success (drainage_screen's normal verdict) — the card
    must not claim 'We couldn't get a full answer'."""
    from app.services.site_check import cards

    status = cards._card_status(
        {"coverage_status": "partial", "source_access_status": "PROGRAMMATIC_PUBLIC"},
        [("drainage_screen", {"normalized_facts": {"status": "screened"}})],
    )
    assert status == "ok"

    with_errors = cards._card_status(
        {"coverage_status": "partial", "source_access_status": "PROGRAMMATIC_PUBLIC"},
        [("drainage_screen", {"errors": ["boom"], "normalized_facts": {}})],
    )
    assert with_errors == "issue"


# ── Fixture-driven content parity: every ACTIVE source, real payloads ──────
# The corpus is the recorded per-adapter fixtures (tests/fixtures/site_check/
# <source>/fixture.json → "expected"), i.e. exactly what the adapters return
# for a real address. A card that renders a placeholder, or one or two token
# rows, is the regression this guards.

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "site_check"

# The floor per card, by GROUP (spec §10.2). A card must carry at least this
# many rendered items — fact rows plus table rows plus links plus its image.
GROUP_CONTENT_FLOOR = {
    "land": 4, "flood": 4, "zoning": 2, "roads": 4, "utilities": 3,
    "environment": 4, "area": 5,
}


def fixture_results():
    """Every recorded adapter payload, keyed by source id."""
    out = {}
    for path in sorted(FIXTURE_DIR.glob("*/fixture.json")):
        fixture = json.loads(path.read_text())
        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            continue
        expected.setdefault("fetched_at", NEW_TS)
        out[fixture["source_id"]] = expected
    return out


def fixture_payload():
    return cards.build_cards(
        make_run(adapter_results=fixture_results()), SETTINGS)


def rendered_items(card):
    return (
        len(card["facts"])
        + sum(len(table["rows"]) for table in card["tables"])
        + len(card["links"])
        + (1 if card["image"] else 0)
    )


def test_fixture_corpus_covers_every_active_source():
    """The corpus must exercise the whole registry — otherwise the content
    floors below are silently skipping sources."""
    from app.services.site_check.registry import REGISTRY

    active = {sid for sid, row in REGISTRY.items() if row.active_in_v0}
    recorded = set(fixture_results())
    assert active - recorded == set(), sorted(active - recorded)


def test_no_placeholder_takeaway_survives_on_real_payloads():
    """The v1 regression: 18 of 31 cards read 'What <source> shows for this
    site is below.' with a handful of rows. No template may come back."""
    for card in all_cards(fixture_payload()):
        takeaway = card["takeaway"]
        assert takeaway and takeaway[-1] in ".!", card["card_key"]
        assert len(takeaway) >= 15, (card["card_key"], takeaway)
        assert not re.match(r"^What .* (is|are) below\.$", takeaway), takeaway
        assert "shows for this site is below" not in takeaway, takeaway
        assert "returned nothing notable" not in takeaway, card["card_key"]


@pytest.mark.parametrize("group_key", [g[0] for g in cards._GROUP_MAP])
def test_every_card_in_the_group_carries_real_content(group_key):
    payload = fixture_payload()
    group = next(g for g in payload["groups"] if g["key"] == group_key)
    floor = GROUP_CONTENT_FLOOR[group_key]
    assert group["cards"], group_key
    for card in group["cards"]:
        assert card["status"] in ("ok", "manual", "issue"), card["card_key"]
        assert rendered_items(card) >= floor, (
            card["card_key"], rendered_items(card), card["facts"])
        # Labels are plain English: no raw field names, no adapter jargon.
        for row in card["facts"]:
            assert "_" not in row["label"], (card["card_key"], row["label"])
            assert row["label"][0].isupper() or row["label"][0].isdigit(), (
                row["label"])
        for table in card["tables"]:
            assert len(table["rows"]) >= 1
            for column in table["columns"]:
                assert "_" not in column, (card["card_key"], column)


def test_named_findings_land_on_the_right_card():
    """Spot-check that the takeaway names the actual finding, not a
    category — the difference Admin flagged."""
    payload = fixture_payload()
    expected = {
        "soil_intel": "Urban land",
        "hydro_intel": "Waller Creek",
        "austin_zoning": "Zoned CS",
        "schools_intel": "Austin ISD",
        "electric_power_infrastructure": "kV transmission line",
        "txdot_roadway": "IH 35",
        "census_acs": "people live within",
        "drainage_screen": "low spots",
        "usgs_elevation": "level",
        "public_aerial_imagery": "aerial photo",
    }
    for card_key, needle in expected.items():
        card = find_card(payload, card_key)
        assert card is not None, card_key
        assert needle in card["takeaway"], (card_key, card["takeaway"])


def test_tables_images_and_links_ride_on_the_cards_that_own_them():
    payload = fixture_payload()
    aerial = find_card(payload, "public_aerial_imagery")
    assert aerial["image"]["url"].startswith("http")
    assert aerial["image"]["caption"]
    assert aerial["image"]["attribution"]

    schools = find_card(payload, "schools_intel")
    table = next(t for t in schools["tables"] if "school" in t["title"].lower())
    assert table["columns"] == [
        "School", "District", "Grades", "Distance", "Students"]
    assert len(table["rows"]) >= 5

    census = find_card(payload, "census_acs")
    ring_table = census["tables"][0]
    assert ring_table["columns"][0] == "Measure"
    assert any(row[0] == "Median household income" for row in
               ring_table["rows"])

    soils = find_card(payload, "soil_intel")
    assert any(link["url"].startswith("https://websoilsurvey")
               for link in soils["links"])
    fema = find_card(payload, "fema_nfhl")
    assert any("fema.gov" in link["url"] for link in fema["links"])


def test_no_bare_check_token_on_real_payloads():
    """The copy rule holds over adapter-authored text too (advisories and
    read lines are rewritten, never rendered raw)."""
    for text in _rendered_strings(fixture_payload()):
        assert not re.search(r"\bcheck\b", text, re.IGNORECASE), text
