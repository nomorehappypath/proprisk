"""Deterministic card assembly for Public Data reports (site_check.md §10).

Builds the versioned CardsPayload v2 the frontend renders verbatim — the UI
never composes facts. Everything here is deterministic: per-source content
builders ported field-for-field from the pinned the reference app details panel
(PublicIntelDetailsPanel.tsx — its 27 sections, their InfoGrid rows,
SimpleTables, hero lines and official-source links), reworded into PropDesk
plain English. Every active source has a real takeaway naming its actual
finding; no generic "what this source shows" template survives. The §11
narrative overlay only fills `Card.narrative` and may replace the bottom line
with kind "ai" — nothing else.

Card DTO (v4), rendered exactly as emitted:
    card_key, title, icon, order, summary, hero, confidence,
    status, takeaway, facts[{label,value,hint?,source_id?}],
    tables[{title,columns[],rows[][]}], links[{label,url}],
    image{url,caption,attribution}|null,
    images[{url,label,caption,attribution}], advisories[], sources[],
    checked_at, narrative, retryable

v4 adds the accordion identity the report screen is built from — `icon`,
`order`, `summary`, `hero` and `confidence`. The report is the pinned the reference app
details panel: one flat ordered list of collapsible sections, each collapsed
row showing glyph + title + a COMPACT summary + the confidence badge. All five
are composed here so the client only draws them.

v3 adds `images` — an ORDERED gallery, distinct from the singular `image`
(one hero picture). The historical-aerials card is its first consumer: the
era filmstrip that answers "what was this land before".

Copy rules (spec §2): the user-facing word for a run is a "lookup", its
result a "report"; the NOUN "check" never appears in any string built here
("checked"/"checking" as verbs are fine) — passthrough text from adapters is
run through `_plain` so upstream wording cannot break the rule. No
engineer-speak: no raw field names as labels, no coverage/status jargon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.regions import states as state_names

from .service import (
    _broker_error_detail,
    retryable_failed_sources_for_run,
)

CARDS_SCHEMA_VERSION = 4

# ── §10.2 source → group map ───────────────────────────────────────────────
# Composite slots: "parcel" (first-resolved of wcad_parcel/tcad_parcel),
# "jurisdiction" (derived from jurisdiction_resolver), "traffic"
# (txdot_aadt with txdot_aadt_history merged in).

_GROUP_MAP: list[tuple[str, str, list[str]]] = [
    ("land", "The land itself",
     ["parcel", "usgs_elevation", "soil_intel", "site_constraints",
      "public_aerial_imagery"]),
    ("flood", "Flood & water",
     ["fema_nfhl", "wetlands_nwi", "hydro_intel", "drainage_screen"]),
    ("zoning", "Zoning & permissions",
     ["jurisdiction", "austin_zoning", "zoning_standards", "future_land_use",
      "district_overlays", "entitlement_pipeline", "plats_intel"]),
    ("roads", "Roads & access",
     ["traffic", "txdot_roadway", "txdot_bridges", "txdot_projects",
      "txdot_extended", "transit_intel"]),
    ("utilities", "Utilities & services",
     ["twdb_ccn", "electric_power_infrastructure", "broadband_intel",
      "fire_district", "schools_intel"]),
    ("environment", "Environment & history",
     ["environmental_screening", "historical_aerials", "historic_intel"]),
    ("area", "Area & people",
     ["census_acs", "osm_overpass", "tax_intel"]),
]

# §10.2 section identity — titles ported 1:1 from the pinned the reference app panel
# (PublicIntelDetailsPanel.tsx SectionShell `title`). The report screen IS
# that panel: one flat ordered accordion, so the section name a reader sees
# here is the name they see in the reference app. Slots the reference app folds into a
# neighbouring section (elevation, drainage, the standalone roadway/bridge
# tables) keep our own plain name and sit next to their the reference app kin.
_CARD_TITLES: dict[str, str] = {
    "parcel": "Parcel Info",
    "usgs_elevation": "Elevation",
    "soil_intel": "Soils (SSURGO)",
    "site_constraints": "Mapped Site Constraints",
    "public_aerial_imagery": "Aerial photo",
    "fema_nfhl": "Flood",
    "wetlands_nwi": "Wetlands",
    "hydro_intel": "Hydrography (USGS NHD)",
    "drainage_screen": "Drainage",
    "jurisdiction": "Jurisdiction",
    "austin_zoning": "Zoning / jurisdiction",
    "zoning_standards": "Zoning standards",
    "future_land_use": "Future Land Use",
    "district_overlays": "District / Subdivision Overlays",
    "entitlement_pipeline": "Entitlement Pipeline",
    "plats_intel": "Plats",
    "traffic": "Traffic & Roads",
    "txdot_roadway": "Roads",
    "txdot_bridges": "Bridges",
    "txdot_projects": "TxDOT Projects",
    "txdot_extended": "TxDOT Extended",
    "transit_intel": "Transit (CapMetro)",
    "twdb_ccn": "Utilities (TWDB CCN)",
    "electric_power_infrastructure": "Electric Infrastructure (HIFLD)",
    "broadband_intel": "Broadband (FCC NBM)",
    "fire_district": "Fire / ESD",
    "schools_intel": "Schools (TEA)",
    "environmental_screening": "Environmental Screening",
    "historical_aerials": "Historical Aerials — what was this land before",
    "historic_intel": "Historic Preservation",
    "census_acs": "Trade Area Demographics",
    "osm_overpass": "Nearby Activity",
    "tax_intel": "Tax / Debt (HB 1378)",
}

# Neutral title a state's or county's own source (spec 2.6) is shown under,
# followed by its short agency name: "Schools (CDE)". A Texas result has no
# provider_short, so a Texas card keeps its title above exactly.
_NEUTRAL_TITLES: dict[str, str] = {
    "austin_zoning": "Zoning",
    "traffic": "Traffic & Roads",
    "txdot_roadway": "Roads",
    "txdot_bridges": "Bridges",
    "txdot_projects": "Road Projects",
    "txdot_extended": "Transportation Summary",
    "transit_intel": "Transit",
    "twdb_ccn": "Utilities",
    "schools_intel": "Schools",
    "tax_intel": "Tax / Levies",
    "zoning_standards": "Zoning standards",
    "future_land_use": "Future Land Use",
    "entitlement_pipeline": "Development Activity",
    "historic_intel": "Historic Preservation",
}


def _card_title(slot: str, facts: dict[str, Any] | None = None) -> str:
    short = str((facts or {}).get("provider_short") or "").strip()
    if short and slot in _NEUTRAL_TITLES:
        return f"{_NEUTRAL_TITLES[slot]} ({short})"
    return _CARD_TITLES[slot]


# Section glyph, ported from the same panel's SectionShell `icon`. Slots the
# panel has no section for get the nearest sibling's family glyph.
_CARD_ICONS: dict[str, str] = {
    "parcel": "📋",
    "site_constraints": "SC",
    "fema_nfhl": "🌊",
    "austin_zoning": "🏛",
    "jurisdiction": "🏛",
    "zoning_standards": "📏",
    "traffic": "🛣",
    "txdot_roadway": "🛣",
    "txdot_bridges": "🌉",
    "txdot_projects": "🏗",
    "txdot_extended": "🧭",
    "census_acs": "📊",
    "osm_overpass": "🏪",
    "wetlands_nwi": "🌿",
    "twdb_ccn": "💧",
    "electric_power_infrastructure": "⚡",
    "future_land_use": "🗺",
    "environmental_screening": "⚗",
    "historical_aerials": "🕰",
    "public_aerial_imagery": "🛰",
    "soil_intel": "🪨",
    "usgs_elevation": "⛰",
    "hydro_intel": "🌊",
    "drainage_screen": "💦",
    "district_overlays": "📐",
    "fire_district": "🚒",
    "schools_intel": "🎓",
    "broadband_intel": "📡",
    "transit_intel": "🚌",
    "tax_intel": "💰",
    "historic_intel": "🏛",
    "plats_intel": "📐",
    "entitlement_pipeline": "📋",
}

# Render order — the panel's own top-to-bottom section order (its lines
# 39–91), with our extra slots inserted beside the the reference app section they
# belong to. Parcel Info is first and opens by default; it carries the map.
_SECTION_ORDER: list[str] = [
    "parcel",
    "site_constraints",
    "fema_nfhl",
    "austin_zoning",
    "jurisdiction",
    "zoning_standards",
    "traffic",
    "txdot_roadway",
    "txdot_bridges",
    "txdot_projects",
    "txdot_extended",
    "census_acs",
    "osm_overpass",
    "wetlands_nwi",
    "twdb_ccn",
    "electric_power_infrastructure",
    "future_land_use",
    "environmental_screening",
    "historical_aerials",
    "public_aerial_imagery",
    "soil_intel",
    "usgs_elevation",
    "hydro_intel",
    "drainage_screen",
    "district_overlays",
    "fire_district",
    "schools_intel",
    "broadband_intel",
    "transit_intel",
    "tax_intel",
    "historic_intel",
    "plats_intel",
    "entitlement_pipeline",
]

_SECTION_INDEX: dict[str, int] = {
    slot: index for index, slot in enumerate(_SECTION_ORDER)
}

# Plain-English provenance names — what a broker would call the source.
_SOURCE_NAMES: dict[str, str] = {
    "tcad_parcel": "County appraisal records",
    "wcad_parcel": "Williamson County appraisal records",
    "usgs_elevation": "USGS elevation data",
    "soil_intel": "USDA soil survey",
    "site_constraints": "Utility & constraint maps",
    "public_aerial_imagery": "Public aerial photos",
    "fema_nfhl": "FEMA flood maps",
    "wetlands_nwi": "Federal wetlands maps",
    "hydro_intel": "USGS waterway maps",
    "drainage_screen": "Terrain & drainage data",
    "jurisdiction_resolver": "City & county boundary maps",
    "austin_zoning": "City zoning records",
    "zoning_standards": "City zoning summaries",
    "future_land_use": "City future land use maps",
    "district_overlays": "District map layers",
    "entitlement_pipeline": "City & county development records",
    "plats_intel": "County plat records",
    "txdot_aadt": "Traffic counts",
    "txdot_aadt_history": "Traffic history",
    "txdot_roadway": "Road inventory",
    "txdot_bridges": "Bridge records",
    "txdot_projects": "Road projects",
    "txdot_extended": "Combined road data",
    "transit_intel": "Transit maps",
    "twdb_ccn": "Water & sewer service areas",
    "electric_power_infrastructure": "Power line maps",
    "broadband_intel": "Broadband coverage maps",
    "fire_district": "Fire district maps",
    "schools_intel": "School locations",
    "environmental_screening": "State & federal environmental records",
    "historical_aerials": "Texas historical aerial photos",
    "historic_intel": "Historic site records",
    "census_acs": "Census figures",
    "osm_overpass": "Map of nearby places",
    "tax_intel": "Tax records",
}

# ── §10.4 honest per-source scope lines (not_covered) ──────────────────────
# Written from the §5 coverage matrix; reviewed against the
# no-engineer-speak rule. Say plainly what the source covers — never an
# error, never blank.

_SCOPE_LINES: dict[str, str] = {
    "tcad_parcel": "Parcel details from this source cover Texas counties only.",
    "wcad_parcel": (
        "Parcel details from this source cover Williamson County, Texas only."
    ),
    "census_acs": "Population figures from this source cover Texas only.",
    "txdot_aadt": "Traffic counts from this source cover Texas roads only.",
    "txdot_aadt_history": (
        "Traffic history from this source covers Texas roads only."
    ),
    "txdot_projects": "Road projects from this source cover Texas only.",
    "txdot_roadway": "Road details from this source cover Texas only.",
    "txdot_bridges": "Bridge records from this source cover Texas only.",
    "txdot_extended": "Combined road data from this source covers Texas only.",
    "twdb_ccn": (
        "Water and sewer service areas from this source cover Texas only."
    ),
    "historic_intel": (
        "Historic site records from this source cover Texas only."
    ),
    "schools_intel": "School locations from this source cover Texas only.",
    "tax_intel": "Tax records from this source cover Texas only.",
    "jurisdiction_resolver": (
        "City and county boundary details from this source cover Central "
        "Texas counties only."
    ),
    "austin_zoning": (
        "Zoning records from this source cover Central Texas cities only."
    ),
    "zoning_standards": (
        "Zoning summaries from this source cover ten Central Texas cities "
        "only — this site is outside them."
    ),
    "future_land_use": (
        "Future land use maps from this source cover Central Texas cities "
        "only."
    ),
    "district_overlays": (
        "District map layers from this source cover Central Texas only."
    ),
    "entitlement_pipeline": (
        "Development records from this source cover Central Texas cities "
        "and counties only."
    ),
    "plats_intel": (
        "Plat records from this source cover Central Texas counties only."
    ),
    "fire_district": (
        "Fire district maps from this source cover Central Texas only."
    ),
    "transit_intel": (
        "Transit maps from this source cover the Austin area only."
    ),
    "drainage_screen": (
        "Drainage screening covers the continental United States only."
    ),
    "environmental_screening": (
        "State environmental records from this source cover Texas only."
    ),
}

_DEFAULT_SCOPE_LINE = "This source does not cover this area."

_NOT_CHECKED_CANCELLED = (
    "Not checked — the lookup was cancelled before this source."
)
_NOT_CHECKED_STOPPED = (
    "Not checked — the lookup stopped before this source."
)

ANCHOR_NOTE = (
    "Mapped from the address you typed. For a purchase or permit decision, "
    "confirm against the survey or recorded plat."
)

_MANUAL_PREFIX = "This one needs a human look — "

# Sources whose adapter reports coverage "not_yet_wired" but still returns
# real, renderable public findings (ported from the panel's
# `allowNotYetWired` on the entitlement section). Every other stub stays off
# the report.
_ALLOW_NOT_YET_WIRED = {"entitlement_pipeline"}

# Facts keys that never belong in a rendered fact row (generic fallback only).
_GENERIC_FACT_SKIP_TOKENS = (
    "url", "geometry", "polygon", "raw", "lat", "lng", "lon",
    "id", "status", "advisory", "cooldown", "schema", "version",
)


# ── Small helpers ──────────────────────────────────────────────────────────


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _facts_of(result: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(result.get("normalized_facts"))


def _errors_of(result: dict[str, Any]) -> list[str]:
    errors = result.get("errors")
    if isinstance(errors, list):
        return [str(item) for item in errors if str(item).strip()]
    if isinstance(errors, str) and errors.strip():
        return [errors]
    return []


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: Any) -> str | None:
    """Return the stored timestamp as an ISO string, or None."""
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    if isinstance(value, str):
        return value
    return parsed.isoformat()


def _successful(result: dict[str, Any]) -> bool:
    """Ported success rule (matches the pinned oldest-date selector):
    no errors, non-empty facts, parseable fetched_at."""
    return (
        not _errors_of(result)
        and bool(_facts_of(result))
        and _parse_ts(result.get("fetched_at")) is not None
    )


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fmt_int(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{round(number):,}"


def _fmt_acres(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if abs(number - round(number)) < 0.05:
        return f"{round(number):,}"
    return f"{number:,.1f}"


def _fmt_ft(value: Any, *, decimals: int = 0) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{number:,.{decimals}f} ft"


def _fmt_miles(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if number < 0.1:
        return f"{number:.2f} mi"
    return f"{number:.2f} mi"


def _fmt_pct(value: Any, *, decimals: int = 1) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{number:.{decimals}f}%"


def _fmt_money(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if abs(number) >= 10_000_000:
        return f"${number / 1_000_000:,.1f} million"
    return f"${round(number):,}"


def _fmt_km(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{number:.2f} km"


# Passthrough sanitiser: adapters write their own advisories and read lines.
# They are the most informative copy on the card, so they are rendered — but
# the spec §2 copy rule owns the final string, so the bare noun/verb token
# "check" is normalised to "review" here rather than dropped.
_CHECK_TOKEN = re.compile(r"\bcheck(s)?\b", re.IGNORECASE)


def _plain(value: Any) -> str | None:
    """Trim, collapse whitespace-only values and apply the copy rule."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    if not text:
        return None

    def _swap(match: re.Match[str]) -> str:
        plural = bool(match.group(1))
        word = "reviews" if plural else "review"
        original = match.group(0)
        if original[0].isupper():
            return word.capitalize()
        return word

    return _CHECK_TOKEN.sub(_swap, text)


def _text(value: Any) -> str | None:
    """Plain trimmed text (no copy-rule rewrite) — internal comparisons."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _labelize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


# Tokens that must survive title-casing as-is — district and road acronyms
# read as gibberish when they come back as "Isd" or "Cr 130".
_KEEP_UPPER = {
    "ISD", "ESD", "WCID", "MUD", "PID", "TIRZ", "SUD", "CCN", "PWS", "LLC",
    "LP", "LLP", "US", "TX", "FM", "RM", "CR", "IH",
    "SH", "SL", "BS", "PR", "NE", "NW", "SE", "SW", "N", "S", "E", "W",
    "I", "II", "III", "IV", "HOA", "MSD", "TCEQ", "TXDOT", "EPA", "USGS",
    "NHD", "NWI", "AADT", "ROW", "PDF", "GIS", "AC", "DC", "KV", "U.S",
    "CDOT", "RTD", "DOLA", "CDE", "CDPHE",
}


def _title_case(value: Any) -> str | None:
    text = _plain(value)
    if not text:
        return None
    if not (text.isupper() or text.islower()):
        return text
    out: list[str] = []
    for word in text.split(" "):
        stripped = word.strip(".,#()")
        if stripped.upper() in _KEEP_UPPER or (
            stripped.isalnum() and any(ch.isdigit() for ch in stripped)
            and any(ch.isalpha() for ch in stripped)
        ):
            out.append(word.replace(stripped, stripped.upper()))
        else:
            out.append(word[:1].upper() + word[1:].lower() if word else word)
    return " ".join(out)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count:,} {word}"


def _first_sentence(value: Any, limit: int = 220) -> str | None:
    text = _plain(value)
    if not text:
        return None
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = parts[0].strip() if parts else text
    if len(out) > limit:
        out = f"{out[: limit - 1].rstrip()}…"
    return out or None


def _join(parts: list[str | None], sep: str = " · ") -> str | None:
    kept = [part for part in parts if part]
    return sep.join(kept) if kept else None


def _fact(label: str, value: Any, *, hint: str | None = None,
          source_id: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"label": label, "value": _plain(value) or "-"}
    if hint:
        row["hint"] = _plain(hint) or hint
    if source_id:
        row["source_id"] = source_id
    return row


def _row(rows: list[dict[str, Any]], label: str, value: Any, *,
         hint: str | None = None, source_id: str | None = None) -> None:
    """Append a fact row, skipping empty values (never render a blank row)."""
    text = _plain(value)
    if text:
        rows.append(_fact(label, text, hint=hint, source_id=source_id))


def _cell(value: Any, fallback: str = "-") -> str:
    return _plain(value) or fallback


def _table(title: str, columns: list[str],
           rows: list[list[Any]]) -> dict[str, Any] | None:
    kept = [[_cell(cell) for cell in row] for row in rows if row]
    if not kept:
        return None
    return {"title": title, "columns": columns, "rows": kept}


def _link(label: str, url: Any) -> dict[str, str] | None:
    text = _text(url)
    if not text or not text.lower().startswith("http"):
        return None
    return {"label": label, "url": text}


def _image(url: Any, caption: Any, attribution: Any) -> dict[str, str] | None:
    text = _text(url)
    if not text or not text.lower().startswith("http"):
        return None
    return {
        "url": text,
        "caption": _plain(caption) or "Aerial photo of this site.",
        "attribution": _plain(attribution) or "Public domain imagery",
    }


@dataclass
class _Content:
    """What one source contributes to its card, before status overrides."""

    takeaway: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    image: dict[str, str] | None = None
    advisories: list[str] = field(default_factory=list)

    def add_table(self, table: dict[str, Any] | None) -> None:
        if table:
            self.tables.append(table)

    def add_link(self, link: dict[str, str] | None) -> None:
        if link and all(link["url"] != row["url"] for row in self.links):
            self.links.append(link)


def _source_url_from(facts: dict[str, Any]) -> str | None:
    for key in ("manual_lookup_url", "zoning_source_url", "source_url",
                "portal_url", "official_source_url"):
        value = _text(facts.get(key))
        if value and value.startswith("http"):
            return value
    return None


def _provenance(source_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """One SourceProvenance row — its OWN checked_at (§10.1) and the §9.3
    stale-fallback marker as from_cache."""
    result = result or {}
    checked_at = _iso(result.get("fetched_at")) if _successful(result) else None
    from_cache = bool(result.get("stale")) or (
        str(result.get("refresh_status") or "") == "stale_cache"
    )
    return {
        "source_id": source_id,
        "source_name": _SOURCE_NAMES.get(
            source_id, source_id.replace("_", " ").title()
        ),
        "source_url": _source_url_from(_facts_of(result)),
        "checked_at": checked_at,
        "from_cache": from_cache,
    }


def _rollup_checked_at(sources: list[dict[str, Any]]) -> str | None:
    """§10.1 card-level rollup: the OLDEST checked_at among contributing
    sources with successful facts (null if none)."""
    stamped = [
        (parsed, entry["checked_at"])
        for entry in sources
        if (parsed := _parse_ts(entry.get("checked_at"))) is not None
    ]
    if not stamped:
        return None
    stamped.sort(key=lambda pair: pair[0])
    return stamped[0][1]


def _advisories_of(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    advisory = _plain(_facts_of(result).get("advisory"))
    if advisory:
        out.append(advisory)
    extra = result.get("advisories")
    if isinstance(extra, list):
        for item in extra:
            text = _plain(item)
            if text and text not in out:
                out.append(text)
    return out[:4]


# ── §10.3 deterministic content builders (the no-AI floor) ─────────────────
# One per active source, ported from the matching the reference app panel section.


def _format_parcel(facts: dict[str, Any]) -> _Content:
    """Panel: LocationSection + ParcelInfoBlock."""
    acreage = _fmt_acres(facts.get("acreage") or facts.get("acres"))
    county = _text(facts.get("county"))
    owner = _title_case(facts.get("owner_name") or facts.get("owner"))
    parcel_id = _text(facts.get("parcel_id"))
    situs = _title_case(facts.get("situs") or facts.get("situs_address"))
    cad_account = _text(facts.get("cad_account"))
    market_value = _fmt_money(facts.get("market_value"))
    neighbours = _num(facts.get("neighboring_parcel_count"))
    candidates = _dicts(facts.get("nearby_candidates"))
    county_label = (
        f"{county.split(':')[-1].title()} County" if county else "the county")

    if not parcel_id and candidates:
        takeaway = (
            f"No single parcel matched this address on the {county_label} "
            f"appraisal roll — the {len(candidates)} closest parcels are "
            "listed below, so pick the one your survey names."
        )
    elif acreage and parcel_id:
        takeaway = (
            f"{acreage} acres in {county_label} — parcel {parcel_id} on the "
            "county appraisal roll."
        )
    elif acreage:
        takeaway = (
            f"{acreage} acres in {county_label}, on record with the county "
            "appraisal district."
        )
    elif parcel_id:
        takeaway = (
            f"Parcel {parcel_id} in {county_label}, on record with the county "
            "appraisal district."
        )
    else:
        takeaway = (
            "The county appraisal district did not return a matching parcel "
            "for this address."
        )
    content = _Content(takeaway)
    rows = content.facts
    if acreage:
        _row(rows, "Size", f"{acreage} acres")
    _row(rows, "Parcel ID", parcel_id)
    _row(rows, "Appraisal district account", cad_account)
    if county:
        _row(rows, "County", f"{county.title()} County")
    _row(rows, "Owner on record", owner)
    _row(rows, "Address on record", situs)
    if market_value:
        _row(rows, "Appraised market value", market_value)
    if neighbours:
        _row(rows, "Parcels mapped next door", f"{int(neighbours):,}")
    _row(rows, "What the appraisal district says", facts.get("note"))

    content.add_table(_table(
        "Other parcels near this address",
        ["Parcel ID", "Size", "Owner", "Address on record", "Account",
         "Distance"],
        [[
            _cell(item.get("parcel_id")),
            _cell(_fmt_acres(item.get("acreage")) and
                  f"{_fmt_acres(item.get('acreage'))} acres"),
            _cell(_title_case(item.get("owner_name"))),
            _cell(_title_case(item.get("situs"))),
            _cell(item.get("cad_account")),
            _cell(_fmt_ft(item.get("distance_ft"))),
        ] for item in candidates[:8]],
    ))
    content.add_link(_link(
        "Open the county property search", facts.get("manual_lookup_url")))
    return content


def _slope_phrase(pct: float | None) -> str:
    if pct is None:
        return "slope not measured"
    if pct < 2:
        return "almost flat"
    if pct < 5:
        return "gentle slopes"
    if pct < 10:
        return "noticeable slope"
    return "steep in places"


def _format_elevation(facts: dict[str, Any]) -> _Content:
    """No the reference app panel section — PropDesk renders the full 3DEP screen."""
    relief = _num(facts.get("relief_ft"))
    max_ft = _num(facts.get("max_elevation_ft"))
    min_ft = _num(facts.get("min_elevation_ft"))
    centre = _num(facts.get("center_elevation_ft"))
    grade = _num(facts.get("sampled_max_grade_pct"))
    samples = _dicts(facts.get("samples"))

    if relief is not None and relief < 1:
        takeaway = (
            "The site is level — less than a foot of height difference across "
            "the points measured."
        )
    elif relief is not None:
        takeaway = (
            f"About {relief:,.0f} ft of fall across the site — "
            f"{_slope_phrase(grade)}"
            + (f" (steepest slope measured {grade:.1f}%)." if grade is not None
               else ".")
        )
    elif centre is not None:
        takeaway = (
            f"The site sits about {centre:,.0f} ft above sea level; the "
            "elevation data did not resolve how much it falls across the "
            "parcel."
        )
    else:
        takeaway = (
            "Public elevation data did not return a reading for this site."
        )
    content = _Content(takeaway)
    rows = content.facts
    if relief is not None:
        _row(rows, "Fall across the site", f"{relief:,.1f} ft")
    if max_ft is not None:
        _row(rows, "Highest point", f"{max_ft:,.1f} ft above sea level")
    if min_ft is not None:
        _row(rows, "Lowest point", f"{min_ft:,.1f} ft above sea level")
    if centre is not None:
        _row(rows, "At the centre of the site", f"{centre:,.1f} ft")
    if grade is not None:
        _row(rows, "Steepest slope measured", f"{grade:.1f}%")
    if samples:
        _row(rows, "Points measured", f"{len(samples):,}")

    labels = {
        "center": "Centre of the site",
        "parcel_boundary": "Parcel edge",
        "offset": "Nearby point",
    }
    content.add_table(_table(
        "Elevation readings",
        ["Where", "Height above sea level", "Detail"],
        [[
            labels.get(str(item.get("kind")), _labelize(str(item.get("kind")))),
            _cell(_fmt_ft(item.get("elevation_ft"), decimals=1)),
            _cell(item.get("resolution_m") and
                  f"{_num(item.get('resolution_m')):,.0f} m grid"),
        ] for item in samples[:8]],
    ))
    content.add_link(_link("USGS elevation service", facts.get("source_url")))
    return content


def _format_soil(facts: dict[str, Any]) -> _Content:
    """Panel: SoilSection."""
    units = _dicts(facts.get("map_units"))
    top = units[0] if units else {}
    components = _dicts(top.get("components"))
    lead = components[0] if components else {}
    name = _plain(top.get("name"))
    symbol = _text(top.get("symbol"))
    drainage = _plain(lead.get("drainage_class"))
    runoff = _plain(lead.get("runoff"))
    prime = facts.get("any_prime_farmland") is True
    hydric = facts.get("any_hydric_component") is True
    max_slope = _num(facts.get("max_component_slope_pct"))

    if name:
        traits = _join([
            f"{runoff.lower()} runoff" if runoff else None,
            drainage.lower() if drainage else None,
            "prime farmland" if prime else None,
            "wet (hydric) soils" if hydric else None,
        ], ", ")
        label = f"{name} ({symbol})" if symbol else name
        takeaway = f"{label}" + (f" — {traits}." if traits else ".")
    elif _text(facts.get("status")) == "no_soils_at_point":
        takeaway = (
            "The national soil survey has no mapped soil unit at this point — "
            "common on paved or built-over land."
        )
    else:
        takeaway = "The national soil survey did not return a reading here."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Soil types on the site", _fmt_int(
        facts.get("map_units_count") or (len(units) or None)))
    _row(rows, "Main soil", name)
    _row(rows, "Drainage", drainage)
    _row(rows, "Runoff", runoff)
    _row(rows, "Prime farmland", "Yes — federal farmland review can apply"
         if prime else "No")
    _row(rows, "Wet (hydric) soils", "Yes — expect wetland review"
         if hydric else "None flagged")
    if max_slope is not None:
        _row(rows, "Steepest slope in the soil data", f"{max_slope:.0f}%")

    content.add_table(_table(
        "Soil map units",
        ["Symbol", "Soil", "Farmland", "Main component", "Drainage", "Slope"],
        [[
            _cell(unit.get("symbol")),
            _cell(unit.get("name")),
            _cell(unit.get("farmland_class")),
            _soil_component_label(unit),
            _cell(_soil_first(unit, "drainage_class")),
            _soil_slope(unit),
        ] for unit in units[:6]],
    ))
    content.add_link(_link(
        "Full soil report (Web Soil Survey)", facts.get("web_soil_survey_url")))
    return content


def _soil_first(unit: dict[str, Any], key: str) -> Any:
    components = _dicts(unit.get("components"))
    return components[0].get(key) if components else None


def _soil_component_label(unit: dict[str, Any]) -> str:
    components = _dicts(unit.get("components"))
    if not components:
        return "-"
    lead = components[0]
    name = _plain(lead.get("component_name")) or "-"
    pct = _num(lead.get("comp_pct"))
    return f"{name} ({pct:.0f}%)" if pct is not None else name


def _soil_slope(unit: dict[str, Any]) -> str:
    low = _num(_soil_first(unit, "slope_low"))
    high = _num(_soil_first(unit, "slope_high"))
    if low is None and high is None:
        return "-"
    if low is not None and high is not None:
        return f"{low:g}–{high:g}%"
    return f"{(low if low is not None else high):g}%"


_CONSTRAINT_VISIBLE_STATUSES = {
    "verified", "verified_public_fact", "mapped_screening_evidence",
    "screened_indicator", "covered",
}


def _format_site_constraints(facts: dict[str, Any]) -> _Content:
    """Panel: SiteConstraintsSection."""
    coverage = [
        row for row in _dicts(facts.get("coverage"))
        if _text(row.get("status")) in _CONSTRAINT_VISIBLE_STATUSES
        or _text(row.get("status")) in (
            "not_connected", "not_applicable", "unavailable_this_time")
    ]
    findings = _dicts(facts.get("material_findings"))
    degraded = _dicts(facts.get("degraded_sources"))
    pipelines = _dicts(facts.get("gas_pipelines"))
    utility_lines = _dicts(facts.get("utility_lines"))
    facilities = _dicts(facts.get("utility_facilities"))
    access = _dicts(facts.get("access_features"))
    edwards = _dicts(facts.get("edwards_aquifer_zones"))
    habitat = _dicts(facts.get("critical_habitat"))
    buildable = _as_dict(facts.get("buildable_envelope"))
    access_facts = _as_dict(facts.get("access_facts"))
    frontages = _dicts(access_facts.get("frontages"))
    mapped = int(_num(facts.get("mapped_feature_count")) or (
        len(utility_lines) + len(facilities) + len(pipelines)
        + len(access) + len(edwards) + len(habitat)
    ))
    screened_acres = _num(buildable.get("screened_area_acres"))

    if findings:
        headline = _plain(findings[0].get("headline")) or "A constraint applies"
        takeaway = (
            f"{headline} — {_plural(len(findings), 'constraint')} worth "
            "settling before you commit."
        )
    else:
        parts: list[str | None] = []
        water = next(
            (_title_case(row.get("result")) for row in coverage
             if _text(row.get("key")) == "water_wastewater_authority"
             and _text(row.get("status")) not in (
                 "not_connected", "not_applicable", "unavailable_this_time")),
            None)
        if water:
            parts.append(f"water and sewer authority is {water}")
        if frontages:
            road = _title_case(frontages[0].get("road_name"))
            if road:
                parts.append(f"frontage on {road}")
        if mapped:
            parts.append(f"{mapped:,} mapped features within five miles")
        summary = _join(parts, "; ")
        takeaway = (
            f"Nothing deal-stopping showed up in the mapped screen — "
            f"{summary}."
            if summary else
            "No deal-stopping constraint showed up in the mapped screen."
        )
    content = _Content(takeaway)
    rows = content.facts
    if screened_acres is not None:
        _row(rows, "Area screened as developable",
             f"{screened_acres:,.2f} acres",
             hint="A first-pass screen from public maps — not a survey.")
    setback = _num(buildable.get("uniform_setback_offset_ft"))
    if setback is not None:
        _row(rows, "Setback applied to that area",
             f"{setback:,.0f} ft" if setback > 0
             else "None — no published setback to apply yet")
    _row(rows, "Flood-zone area removed", _fmt_acres(
        buildable.get("sfha_excluded_acres")) and
        f"{_fmt_acres(buildable.get('sfha_excluded_acres'))} acres")
    _row(rows, "Wetland area removed", _fmt_acres(
        buildable.get("nwi_wetlands_excluded_acres")) and
        f"{_fmt_acres(buildable.get('nwi_wetlands_excluded_acres'))} acres")
    if access:
        _row(rows, "Driveways and signals mapped nearby", f"{len(access):,}")
    if edwards:
        _row(rows, "Edwards Aquifer zones nearby",
             _plain(edwards[0].get("title")))
    if habitat:
        _row(rows, "Protected habitat nearby", f"{len(habitat):,} mapped areas")
    if pipelines:
        nearest = min(
            (p for p in pipelines if _num(p.get("distance_ft")) is not None),
            key=lambda p: _num(p.get("distance_ft")) or 0, default=None)
        if nearest:
            _row(rows, "Nearest pipeline", _join([
                _plain(nearest.get("title")),
                _fmt_ft(nearest.get("distance_ft")) and
                f"{_fmt_ft(nearest.get('distance_ft'))} away",
            ], " · "))
    _row(rows, "Utility crossings on the parcel", _fmt_int(
        buildable.get("utility_crossing_count")))

    content.add_table(_table(
        "What the public maps settled",
        ["Item", "What we found", "Source"],
        [[
            _cell(row.get("label"), "Item"),
            _cell(row.get("result"), "No result returned"),
            _cell(row.get("source"), "Public map"),
        ] for row in coverage[:16]],
    ))
    content.add_table(_table(
        "Mapped infrastructure nearby",
        ["Feature", "Kind", "Distance", "Published detail", "Source"],
        [[
            _cell(row.get("title"), "Mapped infrastructure"),
            _labelize(str(
                row.get("utility_kind") or row.get("facility_kind")
                or row.get("pipeline_type") or row.get("kind")
                or "infrastructure")),
            _cell(_constraint_distance(row.get("distance_ft"))),
            _cell(_join([
                _plain(row.get("pipe_size")) and
                f"size {_plain(row.get('pipe_size'))}",
                _plain(row.get("flow_type")),
                _title_case(row.get("operator")),
            ]), "Location only — no size or capacity published"),
            _cell(row.get("source"), "Public map"),
        ] for row in (utility_lines + facilities + pipelines)[:16]],
    ))
    content.add_table(_table(
        "Road frontage",
        ["Road", "Authority", "Lanes", "Distance to parcel edge", "Status"],
        [[
            _cell(_title_case(row.get("road_name"))),
            _cell(_title_case(row.get("authority"))),
            _cell(_fmt_int(row.get("lanes"))),
            _cell(_fmt_ft(row.get("parcel_edge_distance_ft"))),
            _cell(row.get("status")),
        ] for row in frontages[:6]],
    ))
    for link in _dicts(facts.get("source_links")):
        content.add_link(_link(
            _plain(link.get("label")) or "Official source", link.get("url")))
    for finding in findings[:3]:
        text = _join([
            _plain(finding.get("headline")),
            _plain(finding.get("why_it_matters")),
            _plain(finding.get("next_step")) and
            f"Do this next: {_plain(finding.get('next_step'))}",
        ], " ")
        if text:
            content.advisories.append(text)
    if degraded:
        labels = ", ".join(
            _plain(row.get("label")) or "a map source" for row in degraded)
        content.advisories.append(
            f"Some map sources were briefly unavailable this time: {labels}. "
            "Run the lookup again later to fill them in."
        )
    if _plain(access_facts.get("advisory")):
        content.advisories.append(_plain(access_facts.get("advisory")) or "")
    return content


def _constraint_distance(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if number < 5280:
        return f"{number:,.0f} ft"
    return f"{number / 5280:.2f} mi"


def _format_aerial(facts: dict[str, Any]) -> _Content:
    """Panel: ProjectAerialImage (derived.project_imagery chip)."""
    provider = _plain(facts.get("provider")) or _plain(facts.get("source"))
    radius = _num(facts.get("radius_m"))
    image = _image(
        facts.get("image_url"), facts.get("caption"), facts.get("attribution"))
    if image and provider:
        takeaway = (
            f"A recent aerial photo of the site and its surroundings, from "
            f"the {provider}."
        )
    elif image:
        takeaway = "A recent aerial photo of the site and its surroundings."
    else:
        takeaway = (
            "No public aerial photo came back for this site this time — try "
            "the lookup again later."
        )
    content = _Content(takeaway, image=image)
    rows = content.facts
    _row(rows, "Photo source", provider)
    if radius:
        _row(rows, "How much land is shown",
             f"about {radius:,.0f} m around the centre of the site")
    licence = _plain(facts.get("license"))
    if licence:
        _row(rows, "Rights", f"{licence[:1].upper()}{licence[1:]} — free to "
             "share with a client")
    if facts.get("is_field_verified") is False:
        _row(rows, "Field verified", "No — use it for context, not decisions")
    return content


def _format_fema(facts: dict[str, Any]) -> _Content:
    """Panel: FloodSection."""
    zone = _text(facts.get("zone"))
    status = _text(facts.get("status")) or "unknown"
    description = _plain(facts.get("zone_description"))
    details = _dicts(facts.get("zone_engineering_detail"))
    subtype = _plain(details[0].get("subtype")) if details else None
    in_floodplain = (
        facts.get("in_floodplain") is True or facts.get("has_sfha") is True
    )
    zones_found = [str(z) for z in _as_list(facts.get("zones_found"))]
    bfe = _num(facts.get("base_flood_elevation"))
    depth = _num(facts.get("max_flood_depth_ft"))
    sfha_acres = _num(facts.get("sfha_intersection_acres"))

    if status == "unknown" or not zone:
        content = _Content(
            "The flood zone could not be determined — verify it on the FEMA "
            "flood maps before relying on it."
        )
        content.add_link(_link(
            "FEMA Map Service Center", "https://msc.fema.gov/portal/search"))
        return content

    zone_label = _join([f"Zone {zone}", subtype or description], " — ")
    if zone == "X" and not in_floodplain:
        takeaway = "Outside the mapped flood zones (Zone X — minimal risk)."
    elif in_floodplain:
        takeaway = (
            f"Inside a mapped flood zone (Zone {zone}) — flood insurance and "
            "elevation rules likely apply."
        )
    else:
        takeaway = f"Flood zone {zone} on the FEMA map."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "FEMA flood zone", zone_label)
    _row(rows, "What that means", description)
    _row(rows, "Inside a high-risk flood zone", "Yes" if in_floodplain else "No")
    _row(rows, "In the regulatory floodway",
         "Yes" if facts.get("in_regulatory_floodway") is True else "No")
    if bfe is not None:
        _row(rows, "Base flood elevation", f"{bfe:g} ft")
    if depth is not None:
        _row(rows, "Mapped flood depth", f"{depth:g} ft")
    if sfha_acres is not None and sfha_acres > 0:
        _row(rows, "Parcel area in the high-risk zone",
             f"{sfha_acres:,.2f} acres")
    if len(zones_found) > 1:
        _row(rows, "All zones touching this site", ", ".join(zones_found))
    geometry = _text(facts.get("geometry_type"))
    _row(rows, "What was compared", {
        "parcel": "The parcel boundary against the FEMA map",
        "radius_envelope": "A circle around the address against the FEMA map",
    }.get(geometry or "", geometry and _labelize(geometry)))

    if len(details) > 1:
        content.add_table(_table(
            "Flood zones on this site",
            ["Zone", "Detail", "Base flood elevation", "Depth", "Floodway"],
            [[
                _cell(item.get("zone")),
                _cell(item.get("subtype")),
                _cell(_fmt_ft(item.get("bfe"))),
                _cell(_fmt_ft(item.get("depth"))),
                "Yes" if item.get("in_floodway") is True else "No",
            ] for item in details[:6]],
        ))
    content.add_link(_link(
        "FEMA Map Service Center", "https://msc.fema.gov/portal/search"))
    return content


def _wetland_read(feature: dict[str, Any]) -> str:
    """Ported wetlandFeatureRead — the practical read per Cowardin code."""
    code = (_text(feature.get("attribute")) or "").upper()
    kind = (_text(feature.get("wetland_type")) or "").lower()
    if code.startswith("PUB") and "H" in code:
        return ("Impounded freshwater pond — often a managed or stock pond.")
    if code.startswith("R4"):
        return ("Channel that runs only part of the year; confirm whether it "
                "connects downstream.")
    if code.startswith("R5"):
        return ("Channel that may run year-round — locate it against the "
                "parcel first.")
    if code.startswith("PSS") or "shrub" in kind:
        return ("Shrubby freshwater wetland — needs a seasonal look on the "
                "ground if it touches the site.")
    if code.startswith("R"):
        return "Water channel — locate it before treating it as a constraint."
    return "Mapped wetland feature — a screening flag only."


def _format_wetlands(facts: dict[str, Any]) -> _Content:
    """Panel: WetlandsSection."""
    status = _text(facts.get("status")) or "unknown"
    types = [str(t) for t in _as_list(facts.get("wetland_types_seen"))]
    features = _dicts(facts.get("wetland_features"))
    warning = _plain(facts.get("source_warning"))
    radius = _num(facts.get("envelope_radius_m"))
    parcel_specific = facts.get("parcel_specific") is True
    overlap = _num(facts.get("wetlands_intersection_acres"))

    if status == "mixed":
        count = len(types) or len(features)
        where = ("on the parcel itself" if parcel_specific
                 else "within the area searched")
        takeaway = (
            f"Mapped wetland features {where} — "
            f"{_plural(count, 'type')}. Walk the ground before building "
            "near them."
        )
    elif status == "clear":
        takeaway = "No mapped wetland features on or near this site."
    else:
        takeaway = (
            "Wetlands screening could not finish — treat it as unanswered, "
            "not as all clear."
        )
    if warning:
        takeaway += " A backup map source was used for this answer."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Result", {
        "clear": "Nothing mapped",
        "mixed": "Wetland features mapped",
    }.get(status, "Screening did not finish"))
    _row(rows, "What was searched",
         "The parcel boundary" if parcel_specific
         else "A circle around the address")
    if radius is not None:
        _row(rows, "Search distance", f"{radius:,.0f} m from the site")
    if types:
        _row(rows, "Wetland types seen", ", ".join(types))
    _row(rows, "Mapped features", f"{len(features):,}" if features
         else "None found")
    if overlap is not None and overlap > 0:
        _row(rows, "Wetland area on the parcel", f"{overlap:,.2f} acres")

    content.add_table(_table(
        "Wetland features found",
        ["Wetland type", "Map code", "What it means in practice", "Acres"],
        [[
            _cell(item.get("wetland_type")),
            _cell(item.get("attribute")),
            _wetland_read(item),
            _cell(_fmt_acres(item.get("acres"))),
        ] for item in features[:8]],
    ))
    if warning:
        content.advisories.append(warning)
    return content


def _hydro_relevance(flowline: dict[str, Any]) -> str:
    kind = (_text(flowline.get("ftype_label")) or "").lower()
    named = bool(_text(flowline.get("name")))
    if "stream" in kind or "river" in kind:
        return ("Named stream near the site — expect setbacks and drainage "
                "review." if named else
                "Mapped stream near the site — expect drainage review.")
    if any(word in kind for word in ("canal", "ditch", "connector")):
        return "Mapped drainage channel — confirm it against the survey."
    if any(word in kind for word in ("lake", "pond", "reservoir")):
        return "Mapped water body — confirm setbacks and drainage."
    return "Mapped water feature — confirm it before relying on it."


def _format_hydro(facts: dict[str, Any]) -> _Content:
    """Panel: HydroSection."""
    flowlines = _dicts(facts.get("flowlines"))
    total = int(_num(facts.get("flowlines_count")) or len(flowlines))
    named = int(_num(facts.get("named_flowlines_count")) or 0)
    radius = _num(facts.get("search_radius_m"))
    nearest = _as_dict(facts.get("nearest_named_flowline"))
    status = _text(facts.get("status")) or ""
    unavailable = status == "temporary_unavailable" or str(
        facts.get("source_status") or "").startswith("temporary_")

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in flowlines:
        name = _plain(line.get("name"))
        if not name:
            continue
        kind = _plain(line.get("ftype_label")) or "-"
        key = (name.lower(), kind.lower())
        entry = grouped.setdefault(key, {
            "name": name, "kind": kind, "count": 0, "km": 0.0, "sample": line,
        })
        entry["count"] += 1
        entry["km"] += _num(line.get("segment_length_km")) or _num(
            line.get("length_km")) or 0.0
    ranked = sorted(grouped.values(), key=lambda item: -item["km"])

    if unavailable:
        takeaway = (
            "The national waterway map did not respond — that is not a "
            "no-streams answer, so run the lookup again later."
        )
    elif nearest and _plain(nearest.get("name")):
        name = _plain(nearest.get("name"))
        kind = (_plain(nearest.get("ftype_label")) or "waterway").lower()
        distance = f"within {radius:,.0f} m" if radius else "nearby"
        takeaway = (
            f"{name} — a mapped {kind} — runs {distance}. Expect a setback "
            "and drainage review where it crosses."
        )
    elif total:
        takeaway = (
            f"{_plural(total, 'mapped waterway')} within "
            f"{radius:,.0f} m of the site, none of them named."
            if radius else
            f"{_plural(total, 'mapped waterway')} near this site."
        )
    else:
        takeaway = (
            "No mapped stream, creek or drainage channel within the area "
            "searched."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Waterways mapped nearby", f"{total:,}")
    _row(rows, "Named waterways", f"{named:,}")
    if radius is not None:
        _row(rows, "Search distance", f"{radius:,.0f} m from the site")
    if nearest:
        _row(rows, "Nearest named waterway", _join([
            _plain(nearest.get("name")),
            _plain(nearest.get("ftype_label")),
        ], " — "))
        _row(rows, "Length of that segment",
             _fmt_km(nearest.get("segment_length_km")))
    unnamed = [line for line in flowlines if not _plain(line.get("name"))]
    if unnamed:
        _row(rows, "Unnamed channels mapped", f"{len(unnamed):,}")

    content.add_table(_table(
        "Named waterways nearby",
        ["Name", "Kind", "Segments", "Total length", "Why it matters"],
        [[
            _cell(item["name"]),
            _cell(item["kind"]),
            f"{item['count']:,}",
            _cell(_fmt_km(item["km"])),
            _hydro_relevance(item["sample"]),
        ] for item in ranked[:5]],
    ))
    return content


def _format_drainage(facts: dict[str, Any]) -> _Content:
    """No the reference app panel section — PropDesk renders the full terrain screen."""
    depressions = _dicts(facts.get("topographic_depressions"))
    detention = _dicts(facts.get("detention_candidates"))
    flow_paths = _dicts(facts.get("flow_paths"))
    notes = [_plain(note) for note in _as_list(facts.get("notes"))]
    cross_check = _text(facts.get("nhd_cross_check"))
    window = _num(facts.get("analysis_window_m"))
    cell = _num(facts.get("cell_size_m"))
    ddf = _as_dict(facts.get("atlas14_ddf_inches"))
    rain_100 = _num(_as_dict(ddf.get("24-hr")).get("100yr"))

    ranked = sorted(
        depressions, key=lambda item: -(_num(item.get("area_acres")) or 0))
    largest = ranked[0] if ranked else {}
    largest_acres = _num(largest.get("area_acres"))
    deepest = max(
        (_num(item.get("max_depth_ft")) or 0 for item in depressions),
        default=0)

    if depressions:
        takeaway = (
            f"{_plural(len(depressions), 'low spot')} where water can pond "
            f"near this site — the largest about {largest_acres:,.1f} acres"
            + (f", up to {deepest:,.1f} ft deep." if deepest else ".")
        )
    elif flow_paths:
        takeaway = (
            "Water runs off this site without ponding anywhere the terrain "
            "data can see."
        )
    else:
        takeaway = (
            "The terrain screen did not return drainage results for this "
            "site."
        )
    if cross_check == "partial":
        takeaway += (
            " Flow direction disagrees with the mapped stream network in part "
            "of the area, so treat it as unconfirmed."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Low spots that can hold water", f"{len(depressions):,}"
         if depressions else "None found")
    if largest_acres is not None:
        _row(rows, "Largest low spot", f"{largest_acres:,.1f} acres")
    if deepest:
        _row(rows, "Deepest low spot", f"{deepest:,.1f} ft")
    if detention:
        _row(rows, "Possible detention pond locations", f"{len(detention):,}")
    if flow_paths:
        _row(rows, "Runoff paths traced", f"{len(flow_paths):,}")
    if rain_100 is not None:
        _row(rows, "100-year, 24-hour rainfall", f"{rain_100:,.1f} inches",
             hint="The design storm most Texas cities size drainage against.")
    if window is not None:
        _row(rows, "Area studied around the site", f"{window:,.0f} m")
    if cell is not None:
        _row(rows, "Detail of the terrain data", f"{cell:,.0f} m grid")
    if cross_check:
        _row(rows, "Compared against mapped streams", {
            "partial": "Partly — they disagree in places",
            "agree": "Yes — they agree",
        }.get(cross_check, _labelize(cross_check)))

    content.add_table(_table(
        "Largest low spots",
        ["Size", "Greatest depth"],
        [[
            _cell(_fmt_acres(item.get("area_acres")) and
                  f"{_fmt_acres(item.get('area_acres'))} acres"),
            _cell(_fmt_ft(item.get("max_depth_ft"), decimals=1)),
        ] for item in ranked[:5]],
    ))
    content.add_table(_table(
        "Possible detention pond locations",
        ["Size"],
        [[_cell(_fmt_acres(item.get("area_acres")) and
                f"{_fmt_acres(item.get('area_acres'))} acres")]
         for item in detention[:5]],
    ))
    for note in notes:
        if note:
            content.advisories.append(note)
    return content


def _format_jurisdiction(facts: dict[str, Any]) -> _Content:
    """Panel: ZoningSection's verified-jurisdiction block."""
    summary = _plain(facts.get("broker_summary"))
    kind = (_text(facts.get("jurisdiction_kind")) or "").lower()
    city = _text(facts.get("city"))
    if city:
        city = _title_case(city.removeprefix("City of ").strip())
    county = _text(facts.get("county"))

    if summary:
        takeaway = summary
    elif kind in ("city", "full_purpose_city") and city:
        takeaway = f"Inside {city} city limits — city rules apply."
    elif kind == "etj" and city:
        takeaway = (
            f"Outside city limits but inside the {city} extended area — "
            "some city rules can still apply."
        )
    elif kind in ("unincorporated", "unincorporated_county"):
        takeaway = (
            "Outside any city limits — county rules apply, and there is no "
            "city zoning."
        )
    else:
        takeaway = (
            "Which city or county governs this site could not be confirmed "
            "from the boundary maps."
        )
    content = _Content(takeaway)
    rows = content.facts
    if county:
        _row(rows, "County", f"{county.title()} County")
    _row(rows, "City", city)
    texas = (_text(facts.get("state")) or "TX") == "TX"
    _row(rows, "What governs this site", None if not texas else {
        "city": "City rules — the site is inside the city limits",
        "full_purpose_city": "City rules — the site is inside the city limits",
        "limited_purpose": "Limited-purpose city area — some city rules apply",
        "etj": "County rules, plus the city's subdivision and development "
               "review",
        "unincorporated": "County rules only — no city zoning",
        "unincorporated_county": "County rules only — no city zoning",
    }.get(kind))
    _row(rows, "How certain this is", {
        "authoritative": "Confirmed from the county's official boundary map",
    }.get(_text(facts.get("confidence")) or "",
        _title_case(facts.get("confidence"))))
    _row(rows, "Boundary map used", facts.get("source"))
    content.add_link(_link("Open the boundary map", facts.get("source_url")))
    for item in _as_list(facts.get("advisories")):
        text = _plain(item)
        if text:
            content.advisories.append(text)
    return content


def _meaningful_zone(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    lowered = text.lower()
    if lowered in ("unknown", "no_city_zoning", "etj") or lowered.endswith(
            "_etj"):
        return None
    return text


def _format_austin_zoning(facts: dict[str, Any]) -> _Content:
    """Panel: ZoningSection."""
    jurisdiction = _text(facts.get("jurisdiction")) or "this jurisdiction"
    kind = (_text(facts.get("jurisdiction_kind")) or "").lower()
    zone = _text(facts.get("zoning_type") or facts.get("base_zone"))
    base = _meaningful_zone(facts.get("base_zone"))
    named = _meaningful_zone(facts.get("zoning_type"))
    read_status = _text(facts.get("zoning_read_status"))
    use_action = _text(facts.get("zoning_use_action"))
    use_status = _text(facts.get("zoning_use_status"))
    description = _plain(facts.get("description"))
    zones_found = [str(z) for z in _as_list(facts.get("zones_found"))]

    if zone == "NO_CITY_ZONING" or kind in (
            "unincorporated", "unincorporated_county"):
        takeaway = (
            "No city zoning applies here — development rules come from the "
            "county. Confirm subdivision, access, and utility rules "
            "separately."
        )
        conclusion = "No city zoning district applies"
    elif kind == "etj":
        city = _title_case(jurisdiction.replace(" ETJ", "")) or jurisdiction
        takeaway = (
            f"Inside {city}'s extended area, not the city limits — no city "
            "zoning district is assigned, but the city's subdivision and "
            "development review can still apply."
        )
        conclusion = "No city zoning district assigned"
    elif named or base:
        label = _join([base, named if named != base else None], " - ")
        takeaway = f"Zoned {label} in {_title_case(jurisdiction)}."
        conclusion = label or ""
    elif read_status == "use_action_only" or use_action:
        takeaway = (
            f"{jurisdiction} is confirmed, but no published zoning district "
            "was found for this parcel — a zoning letter from the city would "
            "settle it."
        )
        conclusion = "Not verified from published city records"
    else:
        takeaway = (
            f"A zoning district could not be determined for {jurisdiction} "
            "from published records. That does not mean the site is "
            "unzoned."
        )
        conclusion = "Not verified from published city records"
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Jurisdiction", _title_case(jurisdiction))
    _row(rows, "Zoning district", base or named or conclusion)
    if named and named != base:
        _row(rows, "Zoning district name", named)
    _row(rows, "What this means", description)
    _row(rows, "Recorded land-use action", use_action)
    if use_action:
        _row(rows, "Outcome of that action",
             _title_case(use_status) if use_status and use_status != "unknown"
             else "Not published in the record we retrieved")
    if len(zones_found) > 1:
        _row(rows, "Zoning layers touching this site", ", ".join(zones_found))
    _row(rows, "Where this came from", facts.get("source"))
    content.add_link(_link(
        "Open the city zoning page",
        facts.get("zoning_source_url") or facts.get("manual_lookup_url")))
    return content


def _format_zoning_standards(facts: dict[str, Any]) -> _Content:
    """Panel: ZoningStandardsContent."""
    status = _text(facts.get("status")) or "unknown"
    city = _title_case(facts.get("city"))
    district = _text(facts.get("district"))
    full_name = _plain(facts.get("district_full_name"))
    section = _text(facts.get("district_code_section"))
    dimensional = _as_dict(facts.get("dimensional_standards"))
    parking = _dicts(facts.get("parking"))
    overlays = _dicts(facts.get("overlay_interactions"))

    if district and full_name:
        takeaway = _join([
            f"{city} {district} — {full_name}" if city
            else f"{district} — {full_name}",
            f"code §{section}" if section else None,
        ], " · ") or full_name
        takeaway = f"{takeaway}. Setbacks, height and parking are below."
    elif status == "etj_no_zoning_code":
        takeaway = (
            "The site is in a city's extended area, not its city limits — no "
            "city zoning code applies. Subdivision and development review can "
            "still apply."
        )
    elif status in ("no_city_zoning_authority", "no_district_provided"):
        takeaway = (
            "No city zoning code governs this site, so there are no setback "
            "or height standards to summarise."
        )
    elif status == "city_not_curated":
        takeaway = (
            "PropDesk does not carry a written zoning summary for this city "
            "yet — read the district rules on the city's own code page."
        )
    else:
        takeaway = (
            "No written zoning standards were resolved for this site."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "City", city)
    _row(rows, "Zoning district", district)
    _row(rows, "District name", full_name)
    _row(rows, "Code section", section and f"§{section}")

    content.add_table(_table(
        "Setbacks, height and lot rules",
        ["Standard", "Value", "Code section", "Notes"],
        [[
            _labelize(key),
            _join([
                _plain(_as_dict(value).get("value")),
                _plain(_as_dict(value).get("unit")),
            ], " ") or "-",
            _cell(_as_dict(value).get("code_ref")),
            _cell(_as_dict(value).get("notes"), ""),
        ] for key, value in dimensional.items()],
    ))
    content.add_table(_table(
        "Parking required",
        ["Use", "Rate", "Per", "Code section"],
        [[
            _cell(item.get("use_id")),
            _cell(item.get("rate")),
            _cell(item.get("unit")),
            _cell(item.get("code_ref")),
        ] for item in parking[:8]],
    ))
    content.add_table(_table(
        "Overlays that change the rules",
        ["Overlay", "Effect", "Code section"],
        [[
            _cell(item.get("overlay_id")),
            _cell(item.get("effect")),
            _cell(item.get("code_ref")),
        ] for item in overlays[:8]],
    ))
    content.add_link(_link(
        "Open the city code", facts.get("code_url") or facts.get(
            "manual_lookup_url")))
    return content


def _designation_label(item: dict[str, Any]) -> str | None:
    attrs = _as_dict(item.get("raw_attributes"))
    name = _plain(attrs.get("NAME")) or _plain(attrs.get("Name"))
    code = _plain(item.get("designation"))
    if name and code and name.lower() != code.lower():
        return f"{name} ({code.upper()})"
    return name or (code.upper() if code and len(code) <= 4 else code)


def _format_future_land_use(facts: dict[str, Any]) -> _Content:
    """Panel: FutureLandUseSection."""
    status = _text(facts.get("status")) or "unknown"
    jurisdiction = _title_case(facts.get("jurisdiction"))
    designations = _dicts(facts.get("designations"))
    labels = [
        label for label in (_designation_label(d) for d in designations)
        if label
    ]

    if labels:
        takeaway = (
            f"{jurisdiction or 'The city'}'s long-range plan shows this land "
            f"as {', '.join(labels)} — what the city wants here, not what the "
            "parcel is entitled to today."
        )
    elif status == "no_city_flu_authority":
        takeaway = (
            "The site is outside any city's plan area, so there is no "
            "future-land-use designation for it."
        )
    elif jurisdiction:
        takeaway = (
            f"{jurisdiction}'s long-range plan does not show a designation at "
            "this point — the planning department can confirm what the plan "
            "intends here."
        )
    else:
        takeaway = (
            "No future-land-use designation was published for this site."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "City plan", jurisdiction and f"{jurisdiction} comprehensive "
         "plan")
    if not labels:
        _row(rows, "Planned for", "Nothing designated at this point")
        _row(rows, "How to confirm it",
             "Ask the planning department, or read the plan map for the area")
    if labels:
        _row(rows, "Planned for", ", ".join(labels))
    first = designations[0] if designations else {}
    _row(rows, "Neighbourhood plan area",
         first.get("neighborhood_planning_area"))
    overlays = [str(o) for o in _as_list(first.get("overlays"))]
    if overlays:
        _row(rows, "Plan overlays", ", ".join(overlays))
    _row(rows, "What this changes", "Nothing today — it guides future "
         "rezonings and city investment" if labels else None)

    content.add_table(_table(
        "Plan designations on this site",
        ["Designation", "Neighbourhood plan", "Overlays"],
        [[
            _cell(_designation_label(item)),
            _cell(item.get("neighborhood_planning_area")),
            _cell(", ".join(str(o) for o in _as_list(item.get("overlays")))),
        ] for item in designations[:6]],
    ))
    content.add_link(_link(
        _plain(facts.get("plan_name")) or "Open the published plan",
        facts.get("plan_url")))
    _row(rows, "How to confirm it", facts.get("manual_instructions"))
    return content


_DISTRICT_BUCKETS: list[tuple[str, str, str]] = [
    ("school_districts", "School district", "School districts"),
    ("esd_districts", "Emergency services district",
     "Emergency services districts"),
    ("wcid_districts", "Water control district", "Water control districts"),
    ("mud_districts", "Municipal utility district",
     "Municipal utility districts"),
    ("pid_districts", "Public improvement district",
     "Public improvement districts"),
    ("tirz_districts", "Tax increment zone", "Tax increment zones"),
    ("metro_districts", "Metropolitan district", "Metropolitan districts"),
    ("subdivisions", "Recorded subdivision", "Recorded subdivisions"),
]


def _format_district_overlays(facts: dict[str, Any]) -> _Content:
    """Panel: DistrictOverlaysSection."""
    present: list[tuple[str, str, list[dict[str, Any]]]] = []
    for key, singular, plural in _DISTRICT_BUCKETS:
        items = _dicts(facts.get(key))
        if items:
            present.append((singular if len(items) == 1 else plural,
                            singular, items))
    names = [
        _title_case(item.get("name")) or "Unnamed"
        for _, _, items in present for item in items
    ]
    warnings = [_plain(w) for w in _as_list(facts.get("source_warnings"))]
    unavailable = [
        _plain(layer) for layer in _as_list(facts.get("unavailable_layers"))
    ]

    if names:
        listed = ", ".join(names[:3])
        extra = len(names) - 3
        takeaway = (
            f"This site sits inside {listed}"
            + (f" and {extra} more. " if extra > 0 else ". ")
            + "Each of these sets part of the tax bill."
        )
    else:
        takeaway = (
            "No school, utility or special taxing district overlay matched "
            "this site in the county's map layers."
        )
    content = _Content(takeaway)
    rows = content.facts
    for label, _singular, items in present:
        _row(rows, label, ", ".join(
            _title_case(item.get("name")) or "Unnamed" for item in items))

    content.add_table(_table(
        "Districts covering this site",
        ["Kind", "Name", "District number"],
        [[
            singular,
            _cell(_title_case(item.get("name")), "Unnamed"),
            _cell(item.get("id")),
        ] for _label, singular, items in present for item in items],
    ))
    if warnings or unavailable:
        content.advisories.append(
            "Some county map layers were briefly unavailable, so this list "
            "may be short. Run the lookup again later to fill it in."
        )
    return content


def _distance_phrase(item: dict[str, Any]) -> str | None:
    """"0.6 mi south" / "0.6 mi away" / None — a bearing is only ever printed
    when the geometry gave us one."""
    metres = _num(item.get("distance_m"))
    if metres is None or metres < 0:
        return None
    distance = (
        f"{int(round(metres * 3.28084 / 10) * 10)} ft" if metres < 160
        else f"{metres / 1609.34:.1f} mi"
    )
    direction = _text(item.get("direction"))
    return f"{distance} {direction}" if direction else f"{distance} away"


def _radius_phrase(facts: dict[str, Any]) -> str | None:
    """"2.0 mi", or None when no radius was recorded — the caller then says
    "near this site" rather than inventing a distance."""
    metres = _num(facts.get("subdivision_radius_m")) or _num(
        facts.get("search_radius_m"))
    if metres is None or metres <= 0:
        return None
    return f"{metres / 1609.34:.1f} mi"


def _source_page_label(url: str, portal_url: str | None,
                       portal_name: str | None) -> str:
    """A human label for a case's source page — the portal's own name when it
    IS the portal, otherwise the last path segment made readable."""
    if portal_url and portal_name and url.rstrip("/") == portal_url.rstrip("/"):
        return portal_name
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.(pdf|html?|aspx?)$", "", tail, flags=re.IGNORECASE)
    tail = re.sub(r"[-_+]+", " ", tail).strip()
    if not tail or tail.isdigit() or len(tail) < 3:
        return "Source page"
    return tail[:60].title()


def _format_entitlement(facts: dict[str, Any]) -> _Content:
    """Panel: EntitlementPipelineSection.

    LEADS with the development actually found near the site — the recorded
    county plats and any case read off the city's own pages. A city having no
    machine-readable case feed is a LIMIT on that answer, so it is stated as a
    caveat underneath, never as the headline."""
    read = _as_dict(facts.get("entitlement_read"))
    broker_read = _plain(read.get("broker_read"))
    headline = _plain(read.get("headline"))
    jurisdiction = _title_case(facts.get("jurisdiction"))
    city_label = jurisdiction or "this city"
    zoning_cases = _dicts(facts.get("zoning_cases"))
    site_plans = _dicts(facts.get("site_plan_cases"))
    city_records = _dicts(facts.get("city_entitlement_records"))
    subdivisions = _dicts(facts.get("subdivision_cases"))
    subdivision_count = int(
        _num(facts.get("subdivision_case_count")) or len(subdivisions))
    largest = _as_dict(facts.get("subdivision_largest"))
    total_lots = _num(facts.get("subdivision_total_lots"))
    source_status = _text(facts.get("source_status"))
    sweep = _radius_phrase(facts)

    city_read = _as_dict(facts.get("city_case_reads"))
    read_cases = _dicts(city_read.get("cases"))
    dropped = int(_num(city_read.get("dropped_unverified")) or 0)
    portal_url = _text(facts.get("case_portal_url"))
    portal_name = _plain(facts.get("case_portal_name"))

    if zoning_cases or site_plans:
        takeaway = (
            f"{_plural(len(zoning_cases), 'zoning case')} and "
            f"{_plural(len(site_plans), 'site plan')} on file near this site."
        )
    elif subdivisions:
        nearest = subdivisions[0]
        nearest_name = _title_case(nearest.get("name")) or "an unnamed plat"
        place = _distance_phrase(nearest)
        date = _ms_date(nearest.get("final_plat_date_ms"))
        stage = _plat_stage(nearest.get("status"))
        detail = ", ".join(part for part in (
            place, stage if stage != "-" else None, date) if part)
        takeaway = (
            f"{_plural(subdivision_count, 'recorded subdivision plat')} "
            + (f"within {sweep} of this site" if sweep else "near this site")
            + f" — nearest is {nearest_name}"
            + (f" ({detail})" if detail else "") + "."
        )
        if largest.get("lot_count"):
            largest_place = _distance_phrase(largest)
            takeaway += (
                f" The largest is {_title_case(largest.get('name')) or 'one plat'}"
                f" at {_fmt_int(largest.get('lot_count'))} lots"
                + (f", {largest_place}" if largest_place else "") + "."
            )
    elif read_cases:
        takeaway = (
            f"{_plural(len(read_cases), 'development case')} posted on "
            f"{city_label}'s own planning pages near this site."
        )
    elif broker_read:
        takeaway = broker_read
    elif headline:
        takeaway = headline
    else:
        takeaway = (
            f"No recorded plats or posted cases were found near this site. "
            f"{_title_case(city_label) or 'This city'} publishes its live "
            "case list only as web pages, so check the planning page below "
            "before treating this as a clear result."
        )
    content = _Content(takeaway)
    rows = content.facts

    if total_lots:
        _row(rows, "Homes platted nearby",
             f"About {_fmt_int(total_lots)} lots"
             + (f" within {sweep} of this site" if sweep else " near this site"))
    if largest.get("lot_count") and subdivisions:
        largest_place = _distance_phrase(largest)
        _row(rows, "Largest project nearby",
             f"{_title_case(largest.get('name')) or 'Unnamed plat'} — "
             f"{_fmt_int(largest.get('lot_count'))} lots"
             + (f", {largest_place}" if largest_place else ""))
    if read_cases:
        _row(rows, f"Posted by {portal_name or city_label}",
             f"{_plural(len(read_cases), 'case')} confirmed against the "
             "city's own pages")
    _row(rows, "Why it matters", read.get("why_it_matters"))
    _row(rows, "Worth doing next", read.get("next_step"))
    _row(rows, "Jurisdiction", jurisdiction)
    if sweep:
        _row(rows, "Area searched", f"{sweep} around the site")
    _row(rows, "County records searched", facts.get("county_gis_source"))

    content.add_table(_table(
        "Zoning cases nearby",
        ["Case", "Name", "Address", "Status"],
        [[
            _cell(item.get("case_number")),
            _cell(item.get("case_name")),
            _cell(item.get("address")),
            _cell(item.get("status")),
        ] for item in zoning_cases[:6]],
    ))
    content.add_table(_table(
        "Site plans nearby",
        ["Case", "Kind", "Address", "Status"],
        [[
            _cell(item.get("case_number")),
            _cell(item.get("case_type")),
            _cell(item.get("address")),
            _cell(item.get("status")),
        ] for item in site_plans[:6]],
    ))
    content.add_table(_table(
        "City project records",
        ["Project", "Name", "Status", "Year", "What it signals"],
        [[
            _cell(item.get("project_number")),
            _cell(item.get("project_name")),
            _cell(item.get("project_status")),
            _cell(_year(item.get("project_year"))),
            _cell(item.get("primary_signal")),
        ] for item in city_records[:8]],
    ))
    # LEAD table: what has actually been approved to be built around this land.
    content.add_table(_table(
        f"Recorded development within {sweep}" if sweep
        else "Recorded development nearby",
        ["Subdivision", "How far", "Stage", "Homes/lots", "Acres",
         "Approved by", "Plat date"],
        [[
            _cell(_title_case(item.get("name")), "Unnamed"),
            _cell(_distance_phrase(item)),
            _plat_stage(item.get("status")),
            _cell(_fmt_int(item.get("lot_count"))),
            _cell(_fmt_num(item.get("acres"))),
            _cell(_agency_label(item.get("agency"))),
            _cell(_ms_date(item.get("final_plat_date_ms"))),
        ] for item in subdivisions],
    ))
    # The quote column is the proof: the reader can see the exact words
    # PropDesk matched on the city's page, next to the link to that page.
    content.add_table(_table(
        f"Cases published by {portal_name or city_label}",
        ["Case", "Kind", "Status", "Date", "Applicant or builder",
         "Where it is", "Quoted from the page"],
        [[
            _cell(item.get("name")),
            _cell(_labelize(_text(item.get("case_type")) or "")),
            _cell(item.get("status")),
            _cell(item.get("date")),
            _cell(item.get("applicant_or_developer")),
            _cell(item.get("location_text")),
            _cell(_quote(item.get("evidence_quote"))),
        ] for item in read_cases],
    ))

    content.add_link(_link(
        portal_name or "Open the planning portal", portal_url))
    for pointer in _dicts(facts.get("manual_source_pointers")):
        content.add_link(_link(
            _plain(pointer.get("label")) or "Planning source",
            pointer.get("url")))
    for item in read_cases:
        url = _text(item.get("source_url"))
        if url:
            content.add_link(_link(
                _source_page_label(url, portal_url, portal_name), url))

    # ── Caveats: everything that limits the answer above ─────────────────
    if subdivisions:
        content.advisories.append(
            "These plats are records of land approved for building near this "
            "site — none of them is a plan for this land itself. Distances "
            "are measured from this site to the nearest edge of each one."
        )
    if source_status == "not_wired_for_jurisdiction" and not read_cases:
        content.advisories.append(
            f"{_title_case(city_label) or 'This city'} publishes its live "
            "case list only as web pages, so a rezoning or site plan filed in "
            "the last few weeks may not appear here. Open the planning page "
            "above to check."
        )
    if read_cases:
        content.advisories.append(
            "Each case above was checked against the page it came from — "
            "PropDesk re-opened the source and confirmed the case is named "
            "there."
        )
    if dropped:
        content.advisories.append(
            f"{_plural(dropped, 'further possible case')} could not be "
            "confirmed on the page it was attributed to, so it is not shown."
        )
    _row(rows, "How to search it", facts.get("manual_instructions"))
    return content


# The county plat layers record the approving body as a two-letter code; a
# bare "RR" on a card is not an answer to "who approved this".
_AGENCY_LABELS = {
    "WC": "Williamson County",
    "TC": "Travis County",
    "HU": "City of Hutto",
    "RR": "City of Round Rock",
    "GT": "City of Georgetown",
    "TAY": "City of Taylor",
    "CP": "City of Cedar Park",
    "LE": "City of Leander",
    "PF": "City of Pflugerville",
    "HL": "City of Hutto",
    "FL": "City of Florence",
    "GB": "City of Granger",
    "LB": "City of Liberty Hill",
    "TH": "City of Thrall",
    "WE": "City of Weir",
}


def _agency_label(value: Any) -> str | None:
    """A code we do not recognise is dropped rather than printed raw — a
    two-letter mystery is worse than an empty cell."""
    text = _text(value)
    if not text:
        return None
    known = _AGENCY_LABELS.get(text.upper())
    if known:
        return known
    return text if len(text) > 4 else None


def _fmt_num(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{number:,.1f}"


def _quote(value: Any) -> str | None:
    text = _plain(value)
    return f"“{text}”" if text else None


_PLAT_STAGES = {
    "FPA": "Final plat approved",
    "FPR": "Final plat recorded",
    "PPA": "Preliminary plat approved",
    "IR": "In review",
}


def _plat_stage(value: Any) -> str:
    text = _text(value)
    if not text:
        return "-"
    return _PLAT_STAGES.get(text.upper(), _labelize(text))


def _ms_date(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=UTC).strftime("%b %Y")
    except (OverflowError, OSError, ValueError):
        return None


def _format_plats(facts: dict[str, Any]) -> _Content:
    """Panel: PlatsSection."""
    deed = _as_dict(facts.get("recorded_deed"))
    candidates = _dicts(facts.get("record_candidates"))
    in_review = _dicts(facts.get("in_review"))
    approved = _dicts(facts.get("approved"))
    attempts = _dicts(facts.get("official_retrieval_attempts"))
    identity = _as_dict(facts.get("plat_identity"))
    clues = _as_dict(facts.get("plat_clues"))
    probable = _text(facts.get("probable_plat_number"))
    gis_record = _as_dict(facts.get("recorded_plat_gis"))
    county = _text(facts.get("county"))
    county_label = (
        f"{county.split(':')[-1].title()} County" if county else "the county")
    legal = _plain(clues.get("legal_description")) or _plain(
        _as_dict(deed.get("lookup_inputs")).get("legal_description"))

    if deed and not candidates:
        takeaway = (
            "This reads as original survey land rather than a lot in a "
            "recorded subdivision"
            + (f" — {legal}." if legal else ".")
            + " The deed is the record to pull next."
        )
    elif probable and gis_record:
        takeaway = (
            f"Recorded plat {probable}, confirmed from the county's own "
            "records — pull the official image to finish the file."
        )
    elif probable:
        takeaway = (
            f"Plat {probable} looks like the recorded document for this "
            "parcel, but it is a lead — confirm it against the county clerk's "
            "copy."
        )
    elif in_review or approved:
        takeaway = (
            f"{_plural(len(in_review), 'plat')} in review and "
            f"{_plural(len(approved), 'plat')} approved near this site."
        )
    else:
        takeaway = (
            f"No recorded subdivision plat covers this parcel in "
            f"{county_label}'s map records — search the clerk's records by "
            "owner or legal description."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Recorded plats found", f"{len(candidates):,}" if candidates
         else "None")
    _row(rows, "Plat document number", probable)
    _row(rows, "Subdivision", identity.get("subdivision_name"))
    _row(rows, "Block and lot", _join([
        _plain(identity.get("block")) and f"Block {_plain(identity.get('block'))}",
        _plain(identity.get("lot")) and f"Lot {_plain(identity.get('lot'))}",
    ]))
    _row(rows, "Legal description", legal or identity.get("legal_description"))
    _row(rows, "Parcel searched", clues.get("parcel_id"))
    if deed:
        _row(rows, "Deed number", deed.get("instrument_number"))
        _row(rows, "Recorded", deed.get("recorded_date"))
        _row(rows, "Where the deed lives", deed.get("clerk_portal_name"))
        _row(rows, "How to pull it", deed.get("lookup_steps"))
    _row(rows, "What the county's map layer says", {
        "no_intersecting_recorded_subdivision":
            "No recorded subdivision polygon covers this parcel",
    }.get(_text(facts.get("williamson_subdivision_gis_status")) or "",
        _labelize(_text(facts.get("williamson_subdivision_gis_status")) or "")
        or None))
    _row(rows, "Worth doing next", facts.get("manual_next_step"))

    content.add_table(_table(
        "Plat leads found in public sources",
        ["Document", "Source", "Match", "Standing"],
        [[
            _cell(item.get("document_number")),
            _cell(item.get("source_label") or item.get("source_kind"),
                  "Public source"),
            _cell(_join([
                _plain(item.get("subdivision_name")),
                _plain(item.get("block")) and f"Block {item.get('block')}",
                _plain(item.get("lot")) and f"Lot {item.get('lot')}",
            ])),
            _labelize(_text(item.get("candidate_status")) or "probable lead"),
        ] for item in candidates[:5]],
    ))
    content.add_table(_table(
        "Plats in review nearby",
        ["Subdivision", "Case", "Work", "Distance"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(item.get("case_number")),
            _cell(item.get("work_type")),
            _cell(_fmt_miles(item.get("distance_mi"))),
        ] for item in in_review[:6]],
    ))
    content.add_table(_table(
        "Plats approved nearby",
        ["Subdivision", "Status", "Distance"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(item.get("status")),
            _cell(_fmt_miles(item.get("distance_mi"))),
        ] for item in approved[:6]],
    ))
    content.add_table(_table(
        "What PropDesk tried at the county portal",
        ["Document", "Attempt", "Result"],
        [[
            _cell(item.get("document_number")),
            _cell(_join([
                _plain(item.get("portal_name")),
                _plain(item.get("search_mode")) and
                f"{item.get('search_mode')} = {item.get('search_value')}",
            ]), "Official records portal"),
            _labelize(_text(item.get("status")) or "manual session required"),
        ] for item in attempts[:4]],
    ))
    content.add_link(_link(
        _plain(facts.get("portal_name")) or "Open the county records portal",
        facts.get("portal_url")))
    content.add_link(_link(
        _plain(deed.get("clerk_portal_name")) or "Open the county clerk "
        "records",
        deed.get("clerk_portal_url") or deed.get("clerk_fallback_url")))
    _row(rows, "How to search the portal", facts.get("manual_instructions"))
    return content


def _format_traffic(aadt_facts: dict[str, Any],
                    history_facts: dict[str, Any]) -> _Content:
    """Panel: TrafficSection (counts + historical station)."""
    counts = [
        item for item in _dicts(aadt_facts.get("traffic_counts"))
        if _displayable_road(_text(item.get("road_name")))
    ]
    top = counts[0] if counts else {}
    top_aadt = _fmt_int(top.get("aadt"))
    road = _title_case(top.get("road_name"))
    distance = _num(top.get("distance_mi"))
    station = _as_dict(history_facts.get("representative_station"))
    trend = _as_dict(station.get("trend_summary"))
    change = _num(trend.get("change_5yr_pct")) or _num(
        trend.get("change_3yr_pct"))
    change_label = "Five-year change" if _num(
        trend.get("change_5yr_pct")) is not None else "Three-year change"
    points = _dicts(trend.get("points"))

    if top_aadt:
        where = f" on {road}" if road else " on the nearest counted road"
        takeaway = f"About {top_aadt} vehicles a day pass{where}"
        takeaway += (f", {distance:.2f} mi from the site." if distance
                     is not None else ".")
    else:
        takeaway = (
            "No traffic counts were published near this site — busy frontage "
            "roads usually still have counts by route."
        )
    if change is not None and change > 0 and top_aadt:
        takeaway += " The count is rising."
    content = _Content(takeaway)
    rows = content.facts
    if top_aadt:
        _row(rows, "Vehicles per day", top_aadt, source_id="txdot_aadt")
        _row(rows, "Busiest road nearby", road, source_id="txdot_aadt")
        _row(rows, "Counted in", _display_year(top.get("year")),
             source_id="txdot_aadt")
        if distance is not None:
            _row(rows, "How far that count is", f"{distance:.2f} mi",
                 source_id="txdot_aadt")
        _row(rows, "Count locations nearby", f"{len(counts):,}",
             source_id="txdot_aadt")
    if station:
        _row(rows, "Long-running count station", _join([
            _title_case(station.get("on_road")) if _text(
                station.get("on_road")) not in (None, "-") else None,
            _fmt_miles(station.get("distance_mi")) and
            f"{_fmt_miles(station.get('distance_mi'))} away",
        ]), source_id="txdot_aadt_history")
        _row(rows, "Its latest count", _join([
            _fmt_int(station.get("latest_aadt")) and
            f"{_fmt_int(station.get('latest_aadt'))} vehicles a day",
            _year(station.get("latest_year")),
        ], " in "), source_id="txdot_aadt_history")
    if change is not None:
        direction = "Up" if change > 0 else "Down" if change < 0 else "Flat"
        _row(rows, change_label, f"{direction} {abs(change):g}%",
             source_id="txdot_aadt_history")
    _row(rows, "Traffic direction", _title_case(trend.get("direction"))
         if _text(trend.get("direction")) not in (None, "unknown") else None,
         source_id="txdot_aadt_history")
    _row(rows, "Count stations nearby",
         _fmt_int(history_facts.get("station_count")),
         source_id="txdot_aadt_history")

    content.add_table(_table(
        "Busiest roads nearby",
        ["Road", "Vehicles per day", "Counted", "Distance"],
        [[
            _cell(_title_case(item.get("road_name"))),
            _cell(_fmt_int(item.get("aadt"))),
            _cell(_display_year(item.get("year"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
        ] for item in counts[:5]],
    ))
    content.add_table(_table(
        "Traffic over time at the nearest long-running station",
        ["Year", "Vehicles per day"],
        [[
            _cell(_year(point.get("year"))),
            _cell(_fmt_int(point.get("aadt"))),
        ] for point in points[:12]],
    ))
    content.add_link(_link(
        f"{history_facts.get('provider_short') or 'TxDOT'} traffic count data",
        history_facts.get("source_url")))
    return content


def _displayable_road(value: str | None) -> bool:
    road = (value or "").strip()
    if not road or road == "Unknown":
        return False
    if road.startswith("TxDOT local segment "):
        return False
    return not re.fullmatch(r"\d+[A-Z]{2}\d+", road)


def _display_year(value: Any) -> str | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else None


_FUNCTIONAL_CLASS = {
    1: "Interstate", 2: "Other freeway", 3: "Main arterial road",
    4: "Smaller arterial road", 5: "Major collector road",
    6: "Minor collector road", 7: "Local road",
}


def _road_row_and_lane(row: dict[str, Any]) -> str:
    parts = _join([
        _fmt_ft(row.get("row_width_ft")) and
        f"right of way {_fmt_ft(row.get('row_width_ft'))}",
        _fmt_ft(row.get("lane_width_ft")) and
        f"lane {_fmt_ft(row.get('lane_width_ft'))}",
    ])
    return parts or "Not published — confirm with the road authority"


def _road_median(row: dict[str, Any]) -> str:
    median = _plain(row.get("median_type_label"))
    width = _fmt_ft(row.get("median_width_ft"))
    access = _plain(row.get("access_control_label"))
    parts = _join([
        f"{median} ({width})" if median and width else median,
        access,
    ])
    if parts:
        return parts
    fallback = _text(row.get("crossover_regulation"))
    if fallback and fallback != "manual_verification_required":
        return _labelize(fallback)
    return "Not published — confirm with the road authority"


def _road_turn_lane(row: dict[str, Any]) -> str:
    label = _plain(row.get("turn_lane_presence_label")) or _text(
        row.get("turn_lane_presence"))
    if not label or label == "manual_verification_required":
        return "Not published — confirm with the road authority"
    return _labelize(label) if "_" in label else label


def _format_roadway(facts: dict[str, Any]) -> _Content:
    """Panel: TrafficSection's roadway inventory table."""
    roads = [
        item for item in _dicts(facts.get("roadways"))
        if _displayable_road(_text(item.get("road_name")))
    ]
    primary = roads[0] if roads else {}
    engineering = _as_dict(facts.get("engineering_details"))
    name = _title_case(primary.get("road_name"))
    lanes = _num(primary.get("num_lanes"))
    speed = _num(primary.get("speed_limit"))
    klass = _FUNCTIONAL_CLASS.get(int(_num(primary.get("functional_class")) or 0))

    if name:
        detail = _join([
            f"{int(lanes)} lanes" if lanes else None,
            klass.lower() if klass else None,
            f"{int(speed)} mph" if speed else None,
        ], ", ")
        takeaway = (
            f"{name} runs along the site"
            + (f" — {detail}." if detail else ".")
        )
        if _text(primary.get("row_width")) == "manual_verification_required":
            takeaway += (
                " The state's inventory has no right-of-way or turn-lane "
                "detail for it, so confirm those with the road authority."
            )
    else:
        takeaway = (
            "The state road inventory did not return a named road at this "
            "site."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Road at the site", name)
    _row(rows, "Road class", klass)
    if lanes:
        _row(rows, "Lanes", f"{int(lanes)}")
    if speed:
        _row(rows, "Speed limit", f"{int(speed)} mph")
    _row(rows, "Right of way and lane width", _road_row_and_lane(primary))
    _row(rows, "Median and access", _road_median(primary))
    _row(rows, "Turn lanes", _road_turn_lane(primary))
    _row(rows, "Roads screened nearby", f"{len(roads):,}" if roads else None)

    content.add_table(_table(
        "Roads near this site",
        ["Road", "Distance", "Class", "Lanes", "Speed",
         "Right of way / lane", "Median / access", "Turn lanes"],
        [[
            _cell(_title_case(item.get("road_name"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(_FUNCTIONAL_CLASS.get(
                int(_num(item.get("functional_class")) or 0))),
            _cell(_fmt_int(item.get("num_lanes"))),
            _cell(_fmt_int(item.get("speed_limit")) and
                  f"{_fmt_int(item.get('speed_limit'))} mph"),
            _road_row_and_lane(item),
            _road_median(item),
            _road_turn_lane(item),
        ] for item in roads[:6]],
    ))
    read = _plain(engineering.get("read"))
    if read:
        content.advisories.append(read)
    return content


def _format_bridges(facts: dict[str, Any]) -> _Content:
    """Panel: TrafficSection's nearby-bridges table."""
    items = _dicts(facts.get("items"))
    ranked = sorted(items, key=lambda item: _num(item.get("distance_mi")) or 99)
    nearest = ranked[0] if ranked else {}
    total = int(_num(facts.get("count_within_radius")) or len(items))
    nearest_mi = _num(facts.get("nearest_distance_mi")) or _num(
        nearest.get("distance_mi"))
    posted = [item for item in items if _text(item.get("load_restriction")) == "Y"]
    # A state that records only posted bridges (spec 2.6): the others are
    # "posting unknown", never "none posted".
    unknown_posting = any(
        _text(item.get("posting_status")) == "unknown" for item in items)
    agency = _text(facts.get("provider_short")) or "the state"

    if items:
        carries = _title_case(nearest.get("carries"))
        crosses = _title_case(nearest.get("intersects"))
        where = _join([
            f"on {carries}" if carries else None,
            f"over {crosses}" if crosses else None,
        ], " ")
        label = "bridge or culvert" if total == 1 else "bridges and culverts"
        takeaway = (
            f"{total:,} {label} within the area searched; the nearest is "
            f"{nearest_mi:.2f} mi away" + (f" {where}." if where else ".")
        )
        takeaway += (
            f" {len(posted):,} of them "
            + ("is" if len(posted) == 1 else "are")
            + " posted with a weight limit."
            if posted else
            f" Whether any is weight-posted is not published by {agency}."
            if unknown_posting else
            " None of them is posted with a weight limit."
        )
    else:
        takeaway = "No bridge or culvert records were returned near this site."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Bridges and culverts nearby", f"{total:,}")
    if nearest_mi is not None:
        _row(rows, "Nearest one", f"{nearest_mi:.2f} mi")
    _row(rows, "Nearest bridge carries", _title_case(nearest.get("carries")))
    _row(rows, "It crosses", _title_case(nearest.get("intersects")))
    _row(rows, "Weight limits posted", (
        f"{len(posted):,}" + (f"; the rest: posting not published by {agency}"
                              if unknown_posting else "")) if posted
         else f"Not published by {agency}" if unknown_posting
         else "None posted")
    _row(rows, "Load rating on the nearest",
         _num(nearest.get("inventory_rating_us_tons")) and
         f"{_num(nearest.get('inventory_rating_us_tons')):,.1f} tons")

    content.add_table(_table(
        "Bridges and culverts nearby",
        ["Carries", "Crosses", "Distance", "Built", "Condition",
         "Weight limit"],
        [[
            _cell(_title_case(item.get("carries") or item.get("name"))),
            _cell(_title_case(item.get("intersects"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(item.get("year_built")),
            _cell(_fmt_int(item.get("worst_cond_rtg")) and
                  f"{_fmt_int(item.get('worst_cond_rtg'))} of 9"),
            "Posted" if _text(item.get("load_restriction")) == "Y"
            else "Posting unknown"
            if _text(item.get("posting_status")) == "unknown"
            else "None posted",
        ] for item in ranked[:8]],
    ))
    return content


def _format_projects_ranged(
    facts: dict[str, Any], items: list[dict[str, Any]],
    ranked: list[dict[str, Any]], total: int, agency: str,
) -> _Content:
    nearest = ranked[0] if ranked else {}
    takeaway = (
        f"{_plural(total, 'state road project')} mapped nearby; the nearest is "
        f"{_plain(nearest.get('description')) or 'unnamed'}"
        + (f" ({_fmt_miles(nearest.get('distance_mi'))} away)"
           if _fmt_miles(nearest.get("distance_mi")) else "") + "."
    )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Road projects nearby", f"{total:,}")
    has_phase = any(_text(item.get("phase")) for item in items)
    _row(rows, "Already under construction", (
        f"{sum(1 for i in items if 'underway' in (_text(i.get('phase')) or '').lower()):,}"
        if has_phase else f"Not published by {agency}"))
    _row(rows, "Nearest project", _plain(nearest.get("description")))
    _row(rows, "Its published cost range", _plain(nearest.get("cost_range")),
         hint=f"{agency} publishes cost as a range, not an estimate.")
    _row(rows, "Contract awards", f"Not published by {agency}")
    content.add_table(_table(
        "Road projects nearby",
        ["Project", "Stage", "Work", "Between", "Cost range", "Distance",
         "Project number"],
        [[
            _cell(item.get("description")),
            _cell(_join([
                _plain(item.get("phase")),
                _plain(item.get("project_stage")),
                _plain(item.get("project_status")),
            ])),
            _cell(item.get("work_type")),
            _cell(_join([
                _plain(item.get("limits_from")),
                _plain(item.get("limits_to")),
            ], " to ")),
            _cell(item.get("cost_range")),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(item.get("csj") or item.get("project_number")),
        ] for item in ranked],
    ))
    content.add_link(_link(f"{agency} project data", facts.get("source_url")))
    return content


def _format_projects(facts: dict[str, Any]) -> _Content:
    """Panel: ProjectsSection."""
    items = _dicts(facts.get("items")) or _dicts(facts.get("projects"))
    total = int(_num(facts.get("count_within_radius")) or len(items))
    ranked = sorted(items, key=lambda item: _num(item.get("distance_mi")) or 99)
    biggest = max(
        items, key=lambda item: _num(item.get("estimated_cost_usd")) or 0,
        default={})
    nearest = ranked[0] if ranked else {}
    underway = [
        item for item in items
        if "underway" in (_text(item.get("phase")) or "").lower()
    ]

    agency = _text(facts.get("provider_short"))
    if agency and items and not any(
            _num(item.get("estimated_cost_usd")) for item in items):
        # A state that publishes cost only as a range (spec 2.6): never rank
        # by an invented number; describe the nearest project instead.
        return _format_projects_ranged(facts, items, ranked, total, agency)
    if items:
        name = _plain(biggest.get("description"))
        cost = _fmt_money(biggest.get("estimated_cost_usd"))
        letting = _let_date(biggest.get("estimated_let_date"))
        takeaway = f"{_plural(total, 'state road project')} mapped nearby"
        if name:
            takeaway += (
                f"; the biggest is {name}"
                + (f" at about {cost}" if cost else "")
                + (f", bidding {letting}" if letting else "")
            )
        takeaway += "."
    else:
        takeaway = "No state road projects are mapped near this site."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Road projects nearby", f"{total:,}")
    _row(rows, "Already under construction", f"{len(underway):,}"
         if underway else "None")
    _row(rows, "Nearest project", _join([
        _plain(nearest.get("description")),
        _fmt_miles(nearest.get("distance_mi")) and
        f"{_fmt_miles(nearest.get('distance_mi'))} away",
    ], " · "))
    _row(rows, "Biggest project", _plain(biggest.get("description")))
    _row(rows, "Its estimated cost", _fmt_money(
        biggest.get("estimated_cost_usd")))
    _row(rows, "When it goes out to bid", _let_date(
        biggest.get("estimated_let_date")))

    # The pinned panel opens every project in its own disclosure with a
    # twelve-row detail grid; the same twelve fields ride here as columns so
    # nothing is lost by flattening them into one table.
    content.add_table(_table(
        "Road projects nearby",
        ["Project", "Stage", "Work", "Between", "Length", "Bid date",
         "Estimated cost", "Awarded", "Contractor", "Progress", "Distance",
         "Project number"],
        [[
            _cell(item.get("description")),
            _cell(_join([
                _plain(item.get("phase")),
                _plain(item.get("project_stage")),
                _plain(item.get("project_status")),
            ])),
            _cell(item.get("work_type")),
            _cell(_join([
                _plain(item.get("limits_from")),
                _plain(item.get("limits_to")),
            ], " to ")),
            _cell(_fmt_miles(item.get("project_length_mi"))),
            _cell(_let_date(item.get("estimated_let_date")
                            or item.get("scheduled_let_date"))),
            _cell(_fmt_money(item.get("estimated_cost_usd"))),
            _cell(_fmt_money(_as_dict(item.get("award")).get(
                "award_amount_usd"))),
            _cell(_title_case(_as_dict(item.get("award")).get("contractor"))),
            _cell(_fmt_pct(_as_dict(item.get("award")).get(
                "percent_complete"), decimals=0)),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(item.get("csj")),
        ] for item in ranked],
    ))
    for item in ranked[:4]:
        for link in _dicts(item.get("official_lookup_links")) + _dicts(
                item.get("documents")):
            content.add_link(_link(
                _plain(link.get("label")) or "Official project record",
                link.get("url")))
    return content


def _let_date(value: Any) -> str | None:
    """TxDOT publishes bid dates as 2027-01 / 2027-01-06."""
    text = _text(value)
    if not text:
        return None
    match = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", text)
    if not match:
        return text
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    index = int(match.group(2)) - 1
    if not 0 <= index < 12:
        return text
    month = months[index]
    day = match.group(3)
    if day:
        return f"{month} {int(day)}, {match.group(1)}"
    return f"{month} {match.group(1)}"


def _format_txdot_extended(facts: dict[str, Any]) -> _Content:
    """Panel: ExtendedTxdotSection."""
    traffic = _as_dict(facts.get("traffic_exposure"))
    roadway = _as_dict(facts.get("roadway_context"))
    projects = _as_dict(facts.get("project_pipeline"))
    bridges = _as_dict(facts.get("bridge_constraints"))
    crash = _as_dict(facts.get("crash_data"))
    top = _as_dict(traffic.get("top_aadt"))
    primary = _as_dict(roadway.get("primary_roadway"))
    engineering = _as_dict(roadway.get("engineering_details"))
    primary_engineering = _as_dict(engineering.get("primary"))
    near_term = _dicts(projects.get("near_term_projects"))
    bridge_items = _dicts(bridges.get("items"))
    radius = _num(facts.get("radius_miles"))
    verification = [
        _labelize(str(item))
        for item in _as_list(engineering.get("fields_requiring_verification"))
    ]

    takeaway = _plain(facts.get("broker_read")) or _join([
        _fmt_int(top.get("aadt")) and
        f"{_fmt_int(top.get('aadt'))} vehicles a day on "
        f"{_title_case(top.get('road_name')) or 'the busiest road'}",
        _fmt_int(projects.get("count_within_radius")) and
        f"{_fmt_int(projects.get('count_within_radius'))} road projects",
        _fmt_int(bridges.get("count_within_radius")) and
        f"{_fmt_int(bridges.get('count_within_radius'))} bridge records",
    ], "; ") or "The combined road screen returned no findings for this site."
    if not takeaway.endswith("."):
        takeaway = f"{takeaway}."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Busiest road nearby", _join([
        _fmt_int(top.get("aadt")) and
        f"{_fmt_int(top.get('aadt'))} vehicles a day",
        _title_case(top.get("road_name")),
    ], " on "))
    _row(rows, "How busy that is", {
        "low": "Low for the region",
        "moderate": "Middle of the road for the region",
        "high": "Heavily travelled for the region",
    }.get(_text(traffic.get("aadt_band")) or "",
        _title_case(traffic.get("aadt_band"))))
    _row(rows, "Road at the site", _join([
        _title_case(primary.get("road_name")),
        _plain(primary.get("functional_class_label")),
        _fmt_int(roadway.get("highest_speed_mph")) and
        f"{_fmt_int(roadway.get('highest_speed_mph'))} mph",
    ]))
    _row(rows, "Right of way and lane width",
         _road_row_and_lane(primary_engineering))
    _row(rows, "Median and access", _road_median(primary_engineering))
    _row(rows, "Turn lanes", _road_turn_lane(primary_engineering))
    if verification:
        _row(rows, "Still to confirm with the road authority",
             ", ".join(verification))
    _row(rows, "Road projects nearby", _fmt_int(
        projects.get("count_within_radius")) and
        f"{_fmt_int(projects.get('count_within_radius'))} within "
        f"{radius:,.0f} mi" if radius else _fmt_int(
            projects.get("count_within_radius")))
    _row(rows, "Bridge limits", {
        "no_posted_bridge_constraint_in_screened_set":
            "None posted in the bridges nearby",
        "posting_unknown_in_screened_set":
            "Weight posting not published for the bridges nearby",
    }.get(_text(bridges.get("constraint_level")) or "",
        _labelize(_text(bridges.get("constraint_level")) or "") or None))
    _row(rows, "Crash records",
         "Not included — the state crash extract needs credentials, so this "
         "is not a no-crash result"
         if _text(crash.get("integration_status")) == "credentials_not_configured"
         else _plain(crash.get("read"))
         if _text(crash.get("integration_status")) == "not_connected"
         else _labelize(_text(crash.get("integration_status")) or "") or None)

    content.add_table(_table(
        "Road projects coming up",
        ["Project", "Stage", "Bid date", "Estimated cost"],
        [[
            _cell(item.get("description")),
            _cell(item.get("phase")),
            _cell(item.get("estimated_let_date")),
            _cell(_fmt_money(item.get("estimated_cost_usd"))),
        ] for item in near_term[:5]],
    ))
    content.add_table(_table(
        "Bridges screened for limits",
        ["Carries", "Crosses", "Distance", "Weight limit and condition"],
        [[
            _cell(_title_case(item.get("carries") or item.get("name"))),
            _cell(_title_case(item.get("intersects"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _join([
                "Posted" if _text(item.get("load_restriction")) == "Y"
                else "Weight posting unknown"
                if _text(item.get("posting_status")) == "unknown"
                else "No weight limit posted",
                _fmt_int(item.get("worst_cond_rtg")) and
                f"condition {_fmt_int(item.get('worst_cond_rtg'))} of 9",
            ]) or "-",
        ] for item in bridge_items[:5]],
    ))
    content.add_link(_link(
        "State crash records portal", crash.get("official_access_url")))
    content.add_link(_link(
        "How to pull the crash extract", crash.get("extract_guide_url")))
    for text in (_plain(engineering.get("read")), _plain(crash.get("read")),
                 _plain(facts.get("next_step"))):
        if text and text not in content.advisories:
            content.advisories.append(text)
    return content


def _format_transit(facts: dict[str, Any]) -> _Content:
    """Panel: TransitSection."""
    bus_count = int(_num(facts.get("bus_stops_count")) or 0)
    rail_count = int(_num(facts.get("rail_stations_count")) or 0)
    nearest_bus = _as_dict(facts.get("nearest_bus_stop"))
    nearest_rail = _as_dict(facts.get("nearest_rail_station"))
    bus_buffer = _num(facts.get("bus_buffer_mi"))
    rail_buffer = _num(facts.get("rail_buffer_mi"))

    if bus_count or rail_count:
        takeaway = _join([
            _plural(bus_count, "bus stop") if bus_count else None,
            _plural(rail_count, "rail station") if rail_count else None,
        ], " and ")
        takeaway = f"{takeaway} within walking-and-driving reach of this site."
    else:
        takeaway = (
            f"No bus stop within {bus_buffer:g} mi and no rail station within "
            f"{rail_buffer:g} mi — this site is outside the transit network."
            if bus_buffer and rail_buffer else
            "No public transit stops were found near this site."
        )
    parts = _as_dict(facts.get("parts"))

    def unchecked(part: str) -> bool:
        # A part a state's source could not answer (spec 2.6) is unknown.
        return _text(_as_dict(parts.get(part)).get("coverage")) in (
            "not_connected", "not_applicable")

    if unchecked("bus_stops") or unchecked("rail_stations"):
        takeaway = _join([
            "Bus stops were not checked this time" if unchecked("bus_stops")
            else _plural(bus_count, "bus stop") + " nearby",
            "rail stations were not checked this time" if unchecked("rail_stations")
            else _plural(rail_count, "rail station") + " nearby",
        ], "; ") + "."
    content = _Content(takeaway)
    rows = content.facts
    if unchecked("bus_stops"):
        _row(rows, "Bus stops nearby", "Not checked this time")
    if unchecked("rail_stations"):
        _row(rows, "Rail stations nearby", "Not checked this time")
    if unchecked("bus_stops") or unchecked("rail_stations"):
        for key, label, near in (
            ("bus_stops", "Bus stops nearby", nearest_bus),
            ("rail_stations", "Rail stations nearby", nearest_rail),
        ):
            if not unchecked(key):
                count = bus_count if key == "bus_stops" else rail_count
                _row(rows, label, f"{count:,}")
                _row(rows, f"Nearest {'bus stop' if key == 'bus_stops' else 'rail station'}",
                     _join([_title_case(near.get("name")),
                            _fmt_miles(near.get("distance_mi"))]))
        return content
    _row(rows, "Bus stops nearby", f"{bus_count:,}" if bus_count else "None")
    _row(rows, "Nearest bus stop", _join([
        _title_case(nearest_bus.get("name")),
        _fmt_miles(nearest_bus.get("distance_mi")),
    ]) or (f"None within {bus_buffer:g} mi" if bus_buffer else None))
    _row(rows, "Rail stations nearby", f"{rail_count:,}" if rail_count
         else "None")
    _row(rows, "Nearest rail station", _join([
        _title_case(nearest_rail.get("name")),
        _fmt_miles(nearest_rail.get("distance_mi")),
    ]) or (f"None within {rail_buffer:g} mi" if rail_buffer else None))
    return content


def _district_label(name: Any, kind: Any) -> str | None:
    """"Jonah Water SUD" — never "Jonah Water SUD SUD"."""
    label = _title_case(name)
    suffix = _text(kind)
    if not label:
        return suffix
    if suffix and suffix.upper() not in label.upper():
        return f"{label} ({suffix.upper()})"
    return label


_SERVICEABILITY_ROWS = [
    ["Who is allowed to serve the site",
     "Mapped service territory only",
     "Written confirmation from the provider"],
    ["Water and sewer mains at the site",
     "Not established — no pipe size, pressure or capacity published",
     "The provider's current utility maps"],
    ["How far the tie-in is, and whether it can run downhill",
     "Not established",
     "A will-serve letter plus a route study"],
    ["Three-phase electric service",
     "Not established",
     "Written confirmation from the electric provider"],
    ["Easements across the land",
     "Not established",
     "A title survey and the recorded documents"],
]


def _format_twdb(facts: dict[str, Any], district_facts: dict[str, Any],
                 jurisdiction_facts: dict[str, Any]) -> _Content:
    """Panel: UtilitiesSection (with the district/city sewer fallbacks)."""
    water = _title_case(facts.get("water_provider"))
    sewer = _title_case(facts.get("wastewater_provider"))
    water_all = [_title_case(v) for v in _as_list(facts.get("water_providers"))]
    sewer_all = [
        _title_case(v) for v in _as_list(facts.get("wastewater_providers"))
    ]
    legal = [_title_case(v) for v in _as_list(
        facts.get("water_provider_legal_ccn"))]
    operating = [_title_case(v) for v in _as_list(
        facts.get("water_provider_operating"))]
    nearest = _as_dict(facts.get("wastewater_nearest"))
    nearest_provider = _title_case(nearest.get("provider"))
    nearest_km = _num(nearest.get("within_m"))
    candidates = [
        _district_label(item.get("name"), item.get("kind"))
        for item in _dicts(facts.get("sewer_district_candidates"))
    ]
    if not candidates and district_facts:
        candidates = [
            _title_case(item.get("name")) or "Unnamed district"
            for key in ("mud_districts", "wcid_districts")
            for item in _dicts(district_facts.get(key))
        ]
    kind = (_text(jurisdiction_facts.get("jurisdiction_kind")) or "").lower()
    city = _title_case(jurisdiction_facts.get("city"))
    inside_city = kind in ("city", "full_purpose_city")
    municipal_candidate = _title_case(
        facts.get("municipal_wastewater_candidate"))
    if not municipal_candidate and inside_city and city:
        municipal_candidate = f"City of {city}"

    parts: list[str] = []
    sewer_part = _as_dict(_as_dict(facts.get("parts")).get("sewer"))
    sewer_unknown = not sewer and _text(sewer_part.get("coverage")) in (
        "not_connected", "not_applicable")
    if water:
        parts.append(f"Water comes from {water}")
    if sewer:
        parts.append(f"sewer from {sewer}")
    elif sewer_unknown:
        # The state's sewer source does not cover this site (spec 2.6): say
        # so, never "no sewer area covers it".
        parts.append(
            "the sewer provider is not connected here"
            + (f" ({_text(sewer_part.get('note'))})" if _text(sewer_part.get("note")) else "")
        )
    elif candidates:
        parts.append(
            f"no certificated sewer area covers the site, but it sits inside "
            f"{candidates[0]}, which commonly serves sewer inside its boundary"
        )
    elif inside_city and city:
        parts.append(
            f"no certificated sewer area covers the site, but it is inside "
            f"{city} city limits — contact {municipal_candidate or f'City of {city}'} first"
        )
    elif nearest_provider:
        parts.append(
            f"no sewer service area covers the site; the nearest is "
            f"{nearest_provider}, about {(nearest_km or 0) / 1000:.1f} km away"
        )
    takeaway = (
        f"{'; '.join(parts)}." if parts else
        "No water or sewer service area was matched to this site."
    )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Water provider", ", ".join(water_all) if water_all else water)
    if not sewer_all and sewer_unknown:
        # The sewer source does not cover this site (spec 2.6): unknown,
        # never "no area covers it".
        _row(rows, "Sewer provider",
             f"Not connected here: {_text(sewer_part.get('note')) or 'no sewer source'}")
    else:
        _row(rows, "Sewer provider", ", ".join(sewer_all) if sewer_all
             else (sewer or municipal_candidate or "No certificated area covers this site"))
    _row(rows, "Holds the water certificate", ", ".join(legal))
    _row(rows, "Actually runs the water system", ", ".join(operating))
    if nearest_provider:
        _row(rows, "Nearest certificated sewer area (context only)", _join([
            nearest_provider,
            f"about {(nearest_km or 0) / 1000:.1f} km away" if nearest_km
            else None,
            f"certificate {_text(nearest.get('ccn_no'))}" if _text(
                nearest.get("ccn_no")) else None,
        ]))
    if candidates:
        _row(rows, "Districts that could serve sewer", ", ".join(
            c for c in candidates if c))
    if facts.get("providers_disagree") is True:
        _row(rows, "Watch out",
             "The certificate holder and the operating provider differ — "
             "likely a recent transfer; confirm before asking for service")
    availability_request = facts.get("availability_request_text")
    if municipal_candidate and not sewer and isinstance(availability_request, str):
        _, separator, remainder = availability_request.partition("\n")
        availability_request = (
            f"To {municipal_candidate}:{separator}{remainder}"
        )
    _row(rows, "Ready-to-send request to the provider",
         availability_request,
         hint="Copy this and email it to the provider.")

    content.add_table(_table(
        "What is settled and what is not",
        ["Question", "What the public data shows", "What settles it"],
        _SERVICEABILITY_ROWS,
    ))
    content.add_table(_table(
        "Mapped service areas",
        ["Provider", "Service", "Basis"],
        [[
            _cell(_title_case(item.get("provider"))),
            _cell(_title_case(item.get("service"))),
            _cell(
                ("Contains the site" if item.get("contains_site") else "Nearby only")
                if "contains_site" in item
                else _labelize(_text(item.get("authority")) or "")),
        ] for item in _dicts(facts.get("service_territories"))[:8]],
    ))
    mains = _dicts(facts.get("sewer_mains"))
    if mains:
        content.add_table(_table(
            f"Sanitary sewer mains near the site ({_title_case(mains[0].get('provider'))})",
            ["Distance", "Diameter", "Material", "Owner"],
            [[
                _cell(f"{_num(m.get('distance_m')):,.0f} m"
                      if _num(m.get("distance_m")) is not None else None),
                _cell(f"{_num(m.get('diameter_in')):g} in"
                      if _num(m.get("diameter_in")) else None),
                _cell(m.get("material")),
                _cell(m.get("owner")),
            ] for m in mains[:8]],
        ))
    return content


def _format_electric(facts: dict[str, Any]) -> _Content:
    """Panel: ElectricInfrastructureSection."""
    lines = _dicts(facts.get("nearest_transmission_lines"))
    substations = _dicts(facts.get("nearest_substations"))
    radius = _num(facts.get("radius_mi"))
    max_kv = _num(facts.get("max_voltage_nearby_kv"))
    nearest_mi = min(
        (value for value in (
            _num(item.get("distance_mi"))
            for item in (lines + substations)) if value is not None),
        default=None)
    cannot = [_labelize(str(item)) for item in _as_list(
        facts.get("cannot_confirm"))]

    if lines and nearest_mi is not None:
        top = lines[0]
        kv = _fmt_int(top.get("voltage_kv"))
        owner = _title_case(top.get("owner"))
        takeaway = (
            f"A {kv} kV transmission line runs about {nearest_mi:.2f} mi from "
            f"the site" + (f", owned by {owner}." if owner else ".")
        )
    elif substations:
        takeaway = (
            f"{_plural(len(substations), 'substation')} mapped within "
            f"{radius:,.0f} mi of the site." if radius else
            f"{_plural(len(substations), 'substation')} mapped nearby."
        )
    else:
        takeaway = (
            "No transmission line or substation is mapped within the area "
            "searched."
        )
    content = _Content(takeaway)
    rows = content.facts
    if max_kv is not None:
        _row(rows, "Highest voltage nearby", f"{max_kv:,.0f} kV")
    _row(rows, "Nearest 138 kV line", _fmt_miles(facts.get("nearest_138kv_mi")))
    _row(rows, "Nearest 345 kV line", _fmt_miles(facts.get("nearest_345kv_mi")))
    _row(rows, "Transmission lines mapped", f"{len(lines):,}" if lines
         else "None")
    _row(rows, "Substations mapped", f"{len(substations):,}" if substations
         else "None found in the area searched")
    if radius is not None:
        _row(rows, "Area searched", f"{radius:,.0f} mi around the site")
    if cannot:
        _row(rows, "What these maps cannot answer", ", ".join(cannot),
             hint="Only the electric provider can confirm these.")

    content.add_table(_table(
        "Transmission lines nearby",
        ["Voltage", "Distance", "Owner", "Status"],
        [[
            _cell(_fmt_int(item.get("voltage_kv")) and
                  f"{_fmt_int(item.get('voltage_kv'))} kV"),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(_title_case(item.get("owner"))),
            _cell(_title_case(item.get("status"))),
        ] for item in lines[:8]],
    ))
    content.add_table(_table(
        "Substations nearby",
        ["Substation", "Highest voltage", "Distance", "Status"],
        [[
            _cell(_title_case(item.get("name")), "Unnamed"),
            _cell(_fmt_int(item.get("max_voltage_kv")) and
                  f"{_fmt_int(item.get('max_voltage_kv'))} kV"),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(_title_case(item.get("status"))),
        ] for item in substations[:8]],
    ))
    return content


def _format_broadband(facts: dict[str, Any]) -> _Content:
    """Panel: BroadbandSection."""
    address = _plain(facts.get("parcel_address"))
    lat = _num(facts.get("parcel_lat"))
    lng = _num(facts.get("parcel_lng"))
    takeaway = (
        "The federal broadband map cannot be read automatically — open it and "
        "paste this address to see which providers and speeds are reported "
        "here."
    )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "What to do",
         "Open the national broadband map, paste the address, and read the "
         "provider list for the site")
    _row(rows, "Address to paste", address)
    if lat is not None and lng is not None:
        _row(rows, "Or these coordinates", f"{lat:.6f}, {lng:.6f}")
    _row(rows, "Why it is not automatic",
         "The federal map blocks automated lookups, so an honest answer needs "
         "a person")
    content.add_link(_link(
        "Open the national broadband map", facts.get("fcc_nbm_search_url")))
    content.add_link(_link(
        "Download the full broadband data",
        facts.get("fcc_nbm_bulk_download_url")))
    return content


def _format_fire(facts: dict[str, Any],
                 district_facts: dict[str, Any]) -> _Content:
    """Panel: FireDistrictSection (with the ESD-overlay fallback)."""
    stations = _dicts(facts.get("stations"))
    nearest = _as_dict(facts.get("nearest_station"))
    service_area = _as_dict(facts.get("service_area"))
    if not service_area and district_facts:
        esds = _dicts(district_facts.get("esd_districts"))
        if esds:
            service_area = {
                "name": esds[0].get("name") or esds[0].get("id"),
                "code": esds[0].get("id"),
            }
    radius = _num(facts.get("search_radius_mi"))
    distance = _num(nearest.get("distance_mi"))
    name = _title_case(nearest.get("name"))
    area_name = _title_case(service_area.get("name"))

    if name and distance is not None:
        takeaway = (
            f"The nearest fire station is {name}, about {distance:.1f} mi away "
            "in a straight line"
            + (f"; {area_name} covers this site." if area_name else ".")
        )
    elif area_name:
        takeaway = f"{area_name} covers this site for fire response."
    else:
        takeaway = (
            "No fire station or fire district was matched to this site."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Fire district covering this site", area_name)
    _row(rows, "District number", service_area.get("code"))
    _row(rows, "Nearest station", name)
    _row(rows, "Department", _title_case(nearest.get("department")))
    if distance is not None:
        _row(rows, "Distance", f"{distance:.2f} mi in a straight line",
             hint="Straight-line distance, not the driving route.")
    _row(rows, "Station address", _title_case(nearest.get("address")))
    _row(rows, "Stations found nearby", f"{len(stations):,}" if stations
         else None)
    if radius is not None:
        _row(rows, "Area searched", f"{radius:g} mi around the site")
    if _text(facts.get("fire_flow_status")) == "manual_verification_required":
        _row(rows, "Fire-flow capacity",
             "Not published anywhere public — ask the department or the water "
             "utility for a flow test")

    content.add_table(_table(
        "Fire stations nearby",
        ["Station", "Department", "Address", "Distance"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(_title_case(item.get("department"))),
            _cell(_title_case(item.get("address"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
        ] for item in stations[:8]],
    ))
    content.add_link(_link(
        "Insurance rating lookup", facts.get("iso_rating_lookup_url")))
    return content


def _school_line(entry: dict[str, Any]) -> str | None:
    name = _title_case(entry.get("name"))
    if not name:
        return None
    return _join([
        name,
        _title_case(entry.get("district")),
        _fmt_miles(entry.get("distance_mi")),
    ])


def _format_schools(facts: dict[str, Any]) -> _Content:
    """Panel: SchoolsSection."""
    schools = _dicts(facts.get("schools"))
    bands = _as_dict(facts.get("nearest_by_band"))
    total = int(_num(facts.get("schools_count")) or len(schools))
    radius = _num(facts.get("search_radius_mi"))
    districts = [
        _title_case(item.get("district")) for item in schools
        if _text(item.get("district"))
    ]
    main_district = max(set(districts), key=districts.count) if districts else None
    elementary = _as_dict(bands.get("elementary"))
    elementary_mi = _num(elementary.get("distance_mi"))

    if total:
        takeaway = (
            f"{_plural(total, 'school')} within "
            + (f"{radius:g} mi" if radius else "the area searched")
            + (f" — mostly {main_district}" if main_district else "")
            + (f"; nearest elementary {elementary_mi:.1f} mi away."
               if elementary_mi is not None else ".")
        )
    else:
        takeaway = (
            f"No schools within {radius:g} mi of this site." if radius
            else "No schools were found near this site."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "School district", main_district)
    _row(rows, "Nearest elementary", _school_line(elementary))
    _row(rows, "Nearest middle school", _school_line(_as_dict(
        bands.get("middle"))))
    _row(rows, "Nearest high school", _school_line(_as_dict(bands.get("high"))))
    _row(rows, "Schools nearby", f"{total:,}")
    if radius is not None:
        _row(rows, "Area searched", f"{radius:g} mi around the site")

    content.add_table(_table(
        "Nearest schools",
        ["School", "District", "Grades", "Distance", "Students"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(_title_case(item.get("district"))),
            _cell(item.get("grade_range")),
            _cell(_fmt_miles(item.get("distance_mi"))),
            _cell(_fmt_int(item.get("enrollment"))),
        ] for item in sorted(
            schools, key=lambda s: _num(s.get("distance_mi")) or 99)[:8]],
    ))
    content.add_link(_link(
        f"School ratings ({_text(facts.get('provider_short')) or 'txschools.gov'})",
        facts.get("report_card_lookup_url")))
    return content


def _format_environmental_federal_only(facts: dict[str, Any]) -> _Content:
    """Outside Texas only EPA ECHO ran; the state records are not connected
    and are never shown as zero, none found, or an outage."""
    echos = _dicts(facts.get("echo_records"))
    statuses = _as_dict(facts.get("source_statuses"))
    echo_unavailable = _text(statuses.get("epa_echo")) not in (None, "queried")
    not_connected = (
        f"Not connected for {state_names.name_of(_text(facts.get('state')) or '')} yet"
    )
    content = _Content(_plain(facts.get("advisory")) or "")
    content.facts.append(_fact(
        "Federally regulated facilities",
        "Temporarily unavailable — not an all-clear" if echo_unavailable
        else "None found nearby" if not echos
        else f"{len(echos)} nearby",
    ))
    content.facts.append(_fact("State environmental records", not_connected))
    radius = _num(facts.get("search_radius_m"))
    if radius is not None:
        _row(content.facts, "Area searched", f"{radius:,.0f} m around the site")
    content.add_table(_table(
        "Federally regulated facilities nearby",
        ["Facility", "Address", "Programmes"],
        [[
            _cell(_title_case(item.get("facility_name"))),
            _cell(_title_case(item.get("address"))),
            _cell(item.get("programs")),
        ] for item in echos[:6]],
    ))
    return content


def _format_environmental(facts: dict[str, Any]) -> _Content:
    """Panel: EnvironmentalSection. No-records is a LIMITED screen — never
    'clean'."""
    if _text(facts.get("state_records")) == "not_connected":
        return _format_environmental_federal_only(facts)
    psts = _dicts(facts.get("pst_records"))
    echos = _dicts(facts.get("echo_records"))
    statuses = _as_dict(facts.get("source_statuses"))
    pst_unavailable = _text(statuses.get("tceq_pst")) not in (None, "queried")
    echo_unavailable = _text(statuses.get("epa_echo")) not in (None, "queried")
    radius = _num(facts.get("search_radius_m"))
    broad_radius = _num(facts.get("broad_search_radius_m"))
    radius_label = f"{radius:,.0f} m" if radius else "the screened area"

    screen = _as_dict(facts.get("tceq_screen"))
    categories = [_as_dict(value) for value in screen.values()]
    hits = [cat for cat in categories if (_num(cat.get("count")) or 0) > 0]
    errored = [cat for cat in categories if _text(cat.get("error"))]
    catalog_count = int(sum(_num(cat.get("count")) or 0 for cat in hits))
    registry = _as_dict(facts.get("tceq_address_registry"))
    registry_cats = [_as_dict(value) for value in registry.values()]
    registry_address_hits = int(sum(
        _num(cat.get("address_match_count")) or 0 for cat in registry_cats))
    registry_total = int(sum(
        _num(cat.get("count")) or 0 for cat in registry_cats))
    registry_errored = [
        cat for cat in registry_cats if _text(cat.get("error"))
    ]

    total = len(psts) + len(echos) + catalog_count + registry_address_hits
    source_issue = (
        pst_unavailable or echo_unavailable or bool(errored)
        or bool(registry_errored)
    )
    if source_issue:
        takeaway = (
            "Part of the environmental screening is temporarily incomplete — "
            "do not treat this as an all-clear. Try it again later."
        )
    elif total == 0:
        takeaway = (
            "The screened public databases returned no records near this "
            "site. That is a limited screen of a few public sources, not "
            "proof of a problem-free site — a Phase I environmental report "
            "is still the real answer."
        )
    else:
        detail = ", ".join(
            f"{int(_num(cat.get('count')) or 0)} "
            f"{(_plain(cat.get('label')) or 'record').lower()}"
            for cat in hits[:3]
        )
        takeaway = (
            f"{_plural(total, 'environmental record')} within "
            f"{_fmt_int(broad_radius) + ' m' if broad_radius else radius_label}"
            + (f" — {detail}" if detail else "")
            + ". Review each one before a purchase decision."
        )
    content = _Content(takeaway)
    rows = content.facts
    closed_releases = facts.get("closed_release_count")
    rows.append(_fact(
        "Fuel & storage tank records",
        "Temporarily unavailable — not an all-clear" if pst_unavailable
        else (
            # A state that lists open and closed releases (spec 2.6).
            (f"{len(psts)} open release(s) nearby" if psts
             else "No open releases nearby")
            + f"; {int(_num(closed_releases) or 0)} closed on file"
        ) if closed_releases is not None
        else "None found nearby" if not psts
        else f"{len(psts)} nearby",
    ))
    for part_key, part in _as_dict(facts.get("parts")).items():
        part = _as_dict(part)
        if part_key != "state_records" and _text(part.get("coverage")) in (
                "not_connected", "not_applicable"):
            _row(rows, _labelize(part_key).capitalize() + " records",
                 _text(part.get("note")) or "Not connected yet")
    rows.append(_fact(
        "Federally regulated facilities",
        "Temporarily unavailable — not an all-clear" if echo_unavailable
        else "None found nearby" if not echos
        else f"{len(echos)} nearby",
    ))
    if categories:
        _row(rows, "State cleanup & waste records", _join([
            ", ".join(
                f"{int(_num(cat.get('count')) or 0)} "
                f"{(_plain(cat.get('label')) or 'record').lower()}"
                for cat in hits) if hits
            else ("None found nearby" if not errored else None),
            f"{len(errored)} layers temporarily unavailable — not an all-clear"
            if errored else None,
        ], ". "))
    if registry_cats:
        _row(rows, "Records on file for this address",
             f"{registry_address_hits} on file — review before purchase"
             if registry_address_hits
             else (f"None for this address; {registry_total} on the same road"
                   if registry_total else "None on file for this address"))
    if radius is not None:
        _row(rows, "Area searched", f"{radius:,.0f} m around the site")
    if broad_radius is not None:
        _row(rows, "Wider area searched for cleanup sites",
             f"{broad_radius:,.0f} m")
    _row(rows, "Address searched", facts.get("registry_street_queried"))

    content.add_table(_table(
        "State cleanup & waste records nearby",
        ["Site", "Kind", "Address", "Programme"],
        [[
            _cell(_title_case(record.get("facility_name")), "Unnamed site"),
            _cell(cat.get("label")),
            _cell(_join([
                _title_case(record.get("address")),
                _title_case(record.get("city")),
            ], ", ")),
            _cell(record.get("program")),
        ] for cat in hits for record in _dicts(cat.get("records"))[:4]],
    ))
    content.add_table(_table(
        "Fuel & storage tank records nearby",
        ["Facility", "Address", "City", "Cleanup status"],
        [[
            _cell(_title_case(item.get("facility_name"))),
            _cell(_title_case(item.get("address"))),
            _cell(_title_case(item.get("city"))),
            _cell(item.get("lpst_status")),
        ] for item in psts[:6]],
    ))
    content.add_table(_table(
        "Federally regulated facilities nearby",
        ["Facility", "Address", "Programmes"],
        [[
            _cell(_title_case(item.get("facility_name"))),
            _cell(_title_case(item.get("address"))),
            _cell(item.get("programs")),
        ] for item in echos[:6]],
    ))
    content.add_table(_table(
        "Violations & enforcement on record",
        ["Match", "Entity", "Kind", "Where", "Date"],
        [[
            "This address" if _text(record.get("match")) == "address"
            else "Same road",
            _cell(_title_case(record.get("entity_name"))),
            _cell(cat.get("label")),
            _cell(_join([
                _title_case(record.get("location")),
                _title_case(record.get("city")),
            ], ", ")),
            _cell(record.get("date")),
        ] for cat in registry_cats for record in _dicts(cat.get("records"))[:5]],
    ))
    for link in _dicts(facts.get("manual_records_links")):
        content.add_link(_link(
            _plain(link.get("label")) or "Records lookup", link.get("url")))
    return content


def _format_historic(facts: dict[str, Any]) -> _Content:
    """Panel: HistoricSection."""
    in_district = facts.get("is_in_local_historic_district") is True
    districts = _dicts(facts.get("districts"))
    landmarks = _dicts(facts.get("nearby_landmarks"))
    landmark_count = int(_num(facts.get("nearby_landmarks_count"))
                         or len(landmarks))

    if in_district and districts:
        names = ", ".join(
            _title_case(item.get("name")) or "an unnamed district"
            for item in districts)
        takeaway = (
            f"This site sits inside {names} — exterior changes go through "
            "design review."
        )
    elif landmark_count:
        takeaway = (
            f"{_plural(landmark_count, 'historic landmark')} within a quarter "
            "mile of this site, but the site itself is not in a historic "
            "district."
        )
    else:
        takeaway = (
            "No historic district or landmark is mapped on this site. Local "
            "districts are not published as a map layer in every county, so "
            "search the state Atlas by address to be sure."
        )
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Inside a historic district", "Yes" if in_district else "No")
    _row(rows, "Landmarks within a quarter mile",
         f"{landmark_count:,}" if landmark_count else "None mapped")
    _row(rows, "How to be sure",
         "Search the state historic Atlas by address, then ask the city's "
         "planning department about local districts")

    content.add_table(_table(
        "Historic districts covering this site",
        ["District", "Case", "Ordinance", "Design standards"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(item.get("case_number")),
            _cell(item.get("ordinance_number")),
            "Published" if _text(item.get("design_standards_url"))
            else "Not published",
        ] for item in districts[:6]],
    ))
    content.add_table(_table(
        "Landmarks nearby",
        ["Landmark", "Address", "Distance"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(_title_case(item.get("address"))),
            _cell(_fmt_miles(item.get("distance_mi"))),
        ] for item in landmarks[:6]],
    ))
    content.add_link(_link(
        "Texas Historical Commission Atlas", facts.get("thc_atlas_url")))
    content.add_link(_link(
        _text(facts.get("state_atlas_label")) or "State historic records",
        facts.get("state_atlas_url")))
    return content


_CENSUS_METRICS: list[tuple[str, str, str]] = [
    ("People", "population", "int"),
    ("Median household income", "median_household_income", "money"),
    ("Median home value", "median_home_value", "money"),
    ("Homes owned vs rented", "owner_renter_ratio", "ratio"),
    ("Median age", "median_age", "plain"),
    ("People 65 and over", "population_65_plus", "int"),
    ("People aged 25 to 44", "population_25_44", "int"),
    ("People per household", "median_household_size", "plain"),
    ("Typical commute", "estimated_commute_minutes", "minutes"),
    ("Workers in the area by day", "estimated_daytime_workers", "int"),
    ("College educated (25+)", "college_educated_25_plus_pct", "pct"),
    ("Hispanic share", "hispanic_pct", "pct"),
]


def _census_value(ring: dict[str, Any], key: str, kind: str) -> str:
    value = ring.get(key)
    if kind == "int":
        return _fmt_int(value) or "-"
    if kind == "money":
        return _fmt_money(value) or "-"
    if kind == "pct":
        return _fmt_pct(value) or "-"
    if kind == "minutes":
        number = _num(value)
        return f"{number:.0f} min" if number is not None else "-"
    if kind == "ratio":
        number = _num(value)
        return f"{number:.1f} owned per renter" if number is not None else "-"
    number = _num(value)
    return f"{number:g}" if number is not None else "-"


def _format_census(facts: dict[str, Any]) -> _Content:
    """Panel: DemographicsSection — the FULL ACS fact set, per ring."""
    rings = _as_dict(facts.get("rings"))
    ring_keys = [key for key in ("1mi", "3mi", "5mi") if key in rings]
    labelled = {"1mi": "1 mile", "3mi": "3 miles", "5mi": "5 miles"}
    acs_year = _fmt_int(facts.get("acs_year"))

    lead_key = next(
        (key for key in ring_keys
         if _num(_as_dict(rings.get(key)).get("population"))), None)
    if lead_key:
        lead = _as_dict(rings.get(lead_key))
        population = _fmt_int(lead.get("population"))
        income = _fmt_money(lead.get("median_household_income"))
        takeaway = (
            f"About {population} people live within {labelled[lead_key]} of "
            "this site"
            + (f"; the typical household earns {income} a year." if income
               else ".")
        )
        if lead_key != "1mi" and "1mi" in ring_keys:
            takeaway += (
                " Nothing is counted within a mile — the census areas nearby "
                "are too large to split."
            )
    else:
        takeaway = (
            "No census population figures came back for the area around this "
            "site."
        )
    content = _Content(takeaway)
    rows = content.facts
    for key in ring_keys:
        ring = _as_dict(rings.get(key))
        _row(rows, f"People within {labelled[key]}",
             _fmt_int(ring.get("population")) or (
                 "No census areas fall inside this ring"
                 if ring.get("no_block_groups") is True else None))
    if lead_key:
        lead = _as_dict(rings.get(lead_key))
        _row(rows, "Median household income",
             _fmt_money(lead.get("median_household_income")))
        _row(rows, "Median home value",
             _fmt_money(lead.get("median_home_value")))
        _row(rows, "Median age", _census_value(lead, "median_age", "plain"))
    if acs_year:
        _row(rows, "Figures from", f"Census survey year {acs_year}")

    content.add_table(_table(
        "The area in numbers",
        ["Measure"] + [labelled[key] for key in ring_keys],
        [
            [label] + [
                _census_value(_as_dict(rings.get(key)), field_key, kind)
                for key in ring_keys
            ]
            for label, field_key, kind in _CENSUS_METRICS
        ],
    ))
    note = _plain(_as_dict(
        rings.get(lead_key or "5mi")).get("aggregation_note"))
    if note:
        content.advisories.append(note)
    return content


def _poi_total(facts: dict[str, Any]) -> int:
    groups = _as_dict(facts.get("category_groups"))
    total = 0
    for group in groups.values():
        count = _num(_as_dict(group).get("group_total_5mi"))
        if count:
            total += int(count)
    return total


def _format_osm(facts: dict[str, Any]) -> _Content:
    """Panel: PoiSection — every group, every category."""
    groups = _as_dict(facts.get("category_groups"))
    ranked: list[dict[str, Any]] = []
    for name, value in groups.items():
        entry = _as_dict(value)
        categories = _as_dict(entry.get("categories"))
        total = int(_num(entry.get("group_total_5mi")) or 0)
        if total <= 0:
            continue
        ranked.append({
            "name": _labelize(name),
            "total": total,
            "one": sum(
                int(_num(_as_dict(cat).get("count_1mi")) or 0)
                for cat in categories.values()),
            "three": sum(
                int(_num(_as_dict(cat).get("count_3mi")) or 0)
                for cat in categories.values()),
            "categories": categories,
        })
    ranked.sort(key=lambda item: -item["total"])
    total = _poi_total(facts)
    raw = _fmt_int(facts.get("raw_element_count"))

    if ranked:
        leaders = ", ".join(item["name"].lower() for item in ranked[:3])
        takeaway = (
            f"About {total:,} places are mapped within five miles — mostly "
            f"{leaders}."
        )
    else:
        takeaway = "Few or no mapped places near this site."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Places within 5 miles", f"{total:,}")
    if ranked:
        _row(rows, "Biggest category",
             f"{ranked[0]['name']} — {ranked[0]['total']:,} places")
        _row(rows, "Places within 1 mile",
             f"{sum(item['one'] for item in ranked):,}")
        _row(rows, "Places within 3 miles",
             f"{sum(item['three'] for item in ranked):,}")
    if raw:
        _row(rows, "Map features looked at", raw)

    content.add_table(_table(
        "Places by category",
        ["Category", "Within 1 mile", "Within 3 miles", "Within 5 miles"],
        [[
            item["name"],
            f"{item['one']:,}",
            f"{item['three']:,}",
            f"{item['total']:,}",
        ] for item in ranked],
    ))
    # The pinned panel expands EVERY group and lists EVERY category inside
    # it — including the zero rows — so the same full set rides here.
    detail: list[tuple[int, list[Any]]] = []
    for group in ranked:
        for name, value in _as_dict(group["categories"]).items():
            entry = _as_dict(value)
            count = int(_num(entry.get("count_5mi")) or 0)
            detail.append((count, [
                _labelize(name),
                group["name"],
                f"{int(_num(entry.get('count_1mi')) or 0):,}",
                f"{int(_num(entry.get('count_3mi')) or 0):,}",
                f"{count:,}",
                _cell(_title_case(entry.get("nearest_name"))),
                _cell(_fmt_miles(entry.get("nearest_distance_mi"))),
            ]))
    detail.sort(key=lambda pair: -pair[0])
    content.add_table(_table(
        "Every kind of place, and the nearest one",
        ["Kind", "Category", "Within 1 mile", "Within 3 miles",
         "Within 5 miles", "Nearest", "Distance"],
        [row for _count_value, row in detail],
    ))
    return content


def _format_tax_levies(facts: dict[str, Any]) -> _Content:
    """A state that publishes property-tax levies per taxing unit (spec 2.6,
    Colorado's DOLA map). A bond levy is a tax rate that pays bonds — not the
    amount of debt outstanding."""
    levies = _dicts(facts.get("levies"))
    total = _num(facts.get("total_mills"))
    year = _text(facts.get("tax_year")) or next(
        (_text(item.get("tax_year")) for item in levies if item.get("tax_year")), None)
    agency = _text(facts.get("provider_short")) or "the state"
    if levies:
        takeaway = (
            f"{len(levies)} taxing unit(s) levy property tax here"
            + (f", {total:,.3f} mills in total" if total is not None else "")
            + (f" for tax year {year}" if year else "") + "."
        )
    else:
        takeaway = f"No taxing units were returned by {agency} for this point."
    content = _Content(takeaway)
    rows = content.facts
    if total is not None:
        _row(rows, "Total levy", f"{total:,.3f} mills",
             hint="One mill is $1 of tax per $1,000 of assessed value.")
    bond_total = sum(_num(item.get("bond_mills")) or 0 for item in levies)
    if levies:
        _row(rows, "Of which bond levies", f"{bond_total:,.3f} mills",
             hint="A bond levy pays for voter-approved bonds; it is a tax "
                  "rate, not the amount of debt outstanding.")
    if year:
        _row(rows, "Tax year", year)
    content.add_table(_table(
        "Property tax levies",
        ["Taxing unit", "Total levy (mills)", "Bond levy (mills)"],
        [[
            _cell(_title_case(item.get("name"))),
            _cell(f"{_num(item.get('total_mills')):,.3f}"
                  if _num(item.get("total_mills")) is not None else None),
            _cell(f"{_num(item.get('bond_mills')):,.3f}"
                  if _num(item.get("bond_mills")) is not None else None),
        ] for item in levies[:15]],
    ))
    content.add_link(_link(
        f"{agency} property tax data", facts.get("source_url")))
    return content


def _format_tax(facts: dict[str, Any]) -> _Content:
    """Panel: TaxSection."""
    if "levies" in facts:
        return _format_tax_levies(facts)
    results = _dicts(facts.get("results"))
    queried = int(_num(facts.get("districts_queried")) or len(results))
    matched = int(_num(facts.get("districts_matched")) or 0)
    year = _text(facts.get("fiscal_year"))

    if matched:
        takeaway = (
            f"{matched} of {queried} local districts filed a debt report for "
            f"{year or 'the latest year'} — the amounts are inside the linked "
            "reports."
        )
    elif queried:
        takeaway = (
            f"None of the {queried} districts covering this site matched an "
            f"entry in the state debt report for {year or 'the latest year'} — "
            "they may file under a parent entity or a different name."
        )
    else:
        takeaway = "No local taxing districts were found to look up."
    content = _Content(takeaway)
    rows = content.facts
    _row(rows, "Districts looked up", f"{queried:,}")
    _row(rows, "Districts with a filed debt report", f"{matched:,}")
    if year:
        _row(rows, "Reporting year", year)
    _row(rows, "Where the numbers live",
         "Inside each district's debt-report PDF on the state portal")

    content.add_table(_table(
        "Districts and their debt filings",
        ["District", "Filed as", "City", "Debt report"],
        [[
            _cell(_title_case(item.get("district_name"))),
            _cell(_title_case(_as_dict(match).get("entity_name"))
                  if (match := (_dicts(item.get("matches")) or [None])[0])
                  else "No matching filing found"),
            _cell(_title_case(_as_dict(match).get("city")) if match else None),
            "Published" if match and _text(
                _as_dict(match).get("debt_report_url")) else "-",
        ] for item in results[:10]],
    ))
    for item in results:
        for match in _dicts(item.get("matches"))[:2]:
            content.add_link(_link(
                f"{_title_case(item.get('district_name')) or 'District'} debt "
                "report",
                match.get("debt_report_url")))
    content.add_link(_link(
        "State debt-report portal", facts.get("comptroller_portal_url")))
    return content


def _format_generic(source_id: str, source_name: str,
                    facts: dict[str, Any]) -> _Content:
    """Last-resort builder for a source with no dedicated port. It still
    names the finding — the old 'what this source shows' template is gone."""
    rows: list[dict[str, Any]] = []
    for key, value in facts.items():
        if len(rows) >= 6:
            break
        lowered = str(key).lower()
        if any(token in lowered for token in _GENERIC_FACT_SKIP_TOKENS):
            continue
        if isinstance(value, bool):
            rows.append(_fact(_labelize(str(key)), "Yes" if value else "No"))
        elif isinstance(value, (int, float)):
            rows.append(_fact(_labelize(str(key)), f"{value:,}"))
        elif isinstance(value, str) and value.strip() and len(value) <= 200:
            rows.append(_fact(_labelize(str(key)), value.strip()))
    headline = _first_sentence(facts.get("advisory")) or _first_sentence(
        facts.get("note"))
    if headline:
        takeaway = headline
    elif rows:
        first = rows[0]
        takeaway = f"{source_name}: {first['label'].lower()} {first['value']}."
    else:
        takeaway = f"{source_name} returned nothing notable for this site."
    del source_id
    return _Content(takeaway, facts=rows)


def _format_source(slot: str, source_id: str, facts: dict[str, Any],
                   extras: dict[str, dict[str, Any]]) -> _Content:
    """Dispatch to the ported per-source builder."""
    if slot == "parcel":
        return _format_parcel(facts)
    if slot == "traffic":
        return _format_traffic(facts, extras.get("txdot_aadt_history") or {})
    if slot == "jurisdiction":
        return _format_jurisdiction(facts)
    builder = _BUILDERS.get(source_id)
    if builder is not None:
        return builder(facts)
    if source_id == "twdb_ccn":
        return _format_twdb(
            facts, extras.get("district_overlays") or {},
            extras.get("jurisdiction_resolver") or {})
    if source_id == "fire_district":
        return _format_fire(facts, extras.get("district_overlays") or {})
    name = _SOURCE_NAMES.get(source_id, source_id.replace("_", " ").title())
    return _format_generic(source_id, name, facts)


_BUILDERS: dict[str, Any] = {
    "usgs_elevation": _format_elevation,
    "soil_intel": _format_soil,
    "site_constraints": _format_site_constraints,
    "public_aerial_imagery": _format_aerial,
    "fema_nfhl": _format_fema,
    "wetlands_nwi": _format_wetlands,
    "hydro_intel": _format_hydro,
    "drainage_screen": _format_drainage,
    "austin_zoning": _format_austin_zoning,
    "zoning_standards": _format_zoning_standards,
    "future_land_use": _format_future_land_use,
    "district_overlays": _format_district_overlays,
    "entitlement_pipeline": _format_entitlement,
    "plats_intel": _format_plats,
    "txdot_roadway": _format_roadway,
    "txdot_bridges": _format_bridges,
    "txdot_projects": _format_projects,
    "txdot_extended": _format_txdot_extended,
    "transit_intel": _format_transit,
    "electric_power_infrastructure": _format_electric,
    "broadband_intel": _format_broadband,
    "schools_intel": _format_schools,
    "environmental_screening": _format_environmental,
    "historic_intel": _format_historic,
    "census_acs": _format_census,
    "osm_overpass": _format_osm,
    "tax_intel": _format_tax,
}

# Sources whose builder writes its own manual/limited wording — the generic
# "needs a human look" prefix would throw their content away. Keyed by
# SOURCE id (the composite slots list their contributing sources).
_SELF_WORDED = set(_BUILDERS) | {
    "wcad_parcel", "tcad_parcel", "txdot_aadt", "txdot_aadt_history",
    "jurisdiction_resolver", "twdb_ccn", "fire_district",
}


# ── Collapsed-row summary (the panel's SectionShell `subtitle`) ────────────
# A COMPACT one-liner — never a sentence. It is what the reader scans with
# every section shut: "FEMA Zone X · AREA OF MINIMAL FLOOD HAZARD",
# "19,765 AADT · Chandler Rd", "28 nearby". Ported expression-for-expression
# from the pinned panel; the fallback trims the card's own takeaway so a
# section we did not port a subtitle for still reads as a summary, not a
# paragraph.

_SUMMARY_MAX = 76


def _compact(value: str | None) -> str | None:
    """Trim a takeaway down to a scannable summary line."""
    text = (value or "").strip()
    if not text:
        return None
    # Keep only the opening clause — but never end mid-bracket, which reads
    # as a rendering bug rather than a summary.
    head = text.split(" — ")[0].strip()
    if len(head) >= 24 and head.count("(") == head.count(")"):
        text = head
    if len(text) > _SUMMARY_MAX:
        head = text[:_SUMMARY_MAX]
        cut = head.rfind(" ")
        text = (head[:cut] if cut > _SUMMARY_MAX * 0.6 else head).rstrip(" ,;·")
        return f"{text}…"
    return text.rstrip(".")


def _coordinates(run: Any) -> str | None:
    lat, lng = _num(getattr(run, "lat", None)), _num(getattr(run, "lng", None))
    if lat is None or lng is None:
        return None
    return f"{lat:.5f}, {lng:.5f}"


def _parcel_hero(run: Any) -> str | None:
    """The panel's LocationSection hero, composed server-side (§10.1: the UI
    renders, it never writes). Names the address, the county and the exact
    point every downstream screen was run against."""
    address = _plain(getattr(run, "address", None))
    county = _title_case(getattr(run, "county", None))
    coordinates = _coordinates(run)
    if not address:
        return None
    where = f"The parcel is located at {address}"
    if county:
        where += f" in {county} County"
    where += "."
    if coordinates:
        where += f" Coordinates are {coordinates}."
    return where


def _summary_parcel(facts: dict[str, Any], run: Any) -> str | None:
    address = _plain(getattr(run, "address", None))
    if address:
        return f"for {address}"
    return _join([_title_case(facts.get("county")),
                  _fmt_acres(facts.get("acreage") or facts.get("acres"))])


def _summary_site_constraints(facts: dict[str, Any]) -> str | None:
    visible = [
        row for row in _dicts(facts.get("coverage"))
        if _text(row.get("status")) in _CONSTRAINT_VISIBLE_STATUSES
    ]
    mapped = sum(len(_dicts(facts.get(key))) for key in (
        "utility_lines", "utility_facilities", "gas_pipelines",
        "access_features", "edwards_aquifer_zones", "critical_habitat",
    ))
    total = len(visible) + mapped
    return f"{total:,} published result{'' if total == 1 else 's'}"


def _summary_flood(facts: dict[str, Any]) -> str | None:
    zone = _text(facts.get("zone"))
    if not zone:
        return None
    details = _dicts(facts.get("zone_engineering_detail"))
    subtype = (_plain(details[0].get("subtype")) if details else None) \
        or _plain(facts.get("zone_description"))
    return f"FEMA Zone {zone}" + (f" · {subtype}" if subtype else "")


def _jurisdiction_label(facts: dict[str, Any],
                        jurisdiction: dict[str, Any]) -> str:
    kind = (_text(jurisdiction.get("jurisdiction_kind"))
            or _text(facts.get("jurisdiction_kind")) or "unknown").lower()
    city = _text(jurisdiction.get("city") or facts.get("jurisdiction"))
    if kind in ("city", "full_purpose_city"):
        return f"Inside {city} city limits" if city else "Inside city limits"
    if kind == "limited_purpose":
        return (f"{city} limited-purpose jurisdiction" if city
                else "Limited-purpose city jurisdiction")
    if kind == "etj":
        return (f"{city} ETJ - outside city limits" if city
                else "ETJ - outside city limits")
    if kind == "unincorporated_county":
        return "Outside incorporated city limits"
    return _text(facts.get("jurisdiction")) or "Jurisdiction not verified"


def _summary_zoning(facts: dict[str, Any],
                    jurisdiction: dict[str, Any]) -> str | None:
    kind = (_text(jurisdiction.get("jurisdiction_kind"))
            or _text(facts.get("jurisdiction_kind")) or "").lower()
    base = _meaningful_zone(facts.get("base_zone"))
    ztype = _meaningful_zone(facts.get("zoning_type"))
    if kind == "etj":
        zoning = "No city zoning district assigned"
    elif kind in ("unincorporated", "unincorporated_county"):
        zoning = "No city zoning district applies"
    elif base and ztype and base != ztype:
        zoning = f"{base} - {ztype}"
    else:
        zoning = base or ztype or "Base district not verified"
    return _join([_jurisdiction_label(facts, jurisdiction), zoning])


def _summary_traffic(facts: dict[str, Any]) -> str | None:
    counts = _dicts(facts.get("traffic_counts"))
    if counts:
        top = counts[0]
        aadt = _fmt_int(top.get("aadt"))
        road = _title_case(top.get("road_name"))
        if aadt:
            return f"{aadt} AADT" + (f" · {road}" if road else "")
    return None


def _summary_projects(facts: dict[str, Any]) -> str | None:
    items = _dicts(facts.get("items")) or _dicts(facts.get("projects"))
    total = int(_num(facts.get("count_within_radius")) or len(items))
    return f"{total:,} nearby"


def _year(value: Any) -> str | None:
    """A calendar year is never thousands-separated."""
    number = _num(value)
    return str(int(number)) if number is not None else None


def _summary_census(facts: dict[str, Any]) -> str | None:
    acs = _year(facts.get("acs_year"))
    if not acs:
        return "ACS"
    tiger = _year(facts.get("tiger_year")) or acs
    return f"ACS {acs} 5-Year (TIGER {tiger})"


def _summary_osm(facts: dict[str, Any]) -> str | None:
    total = _poi_total(facts)
    raw = _fmt_int(facts.get("raw_element_count"))
    if raw:
        return f"{raw} raw POIs · {total:,} categorised"
    return f"{total:,} POIs"


def _summary_wetlands(facts: dict[str, Any]) -> str | None:
    status = _text(facts.get("status")) or "unknown"
    if _plain(facts.get("source_warning")):
        return "Fallback screening"
    if status == "clear":
        return "No mapped indicators"
    if status == "mixed":
        count = (len(_as_list(facts.get("wetland_types_seen")))
                 or len(_dicts(facts.get("wetland_features"))))
        return f"{count} indicator type{'' if count == 1 else 's'}"
    return "Screening incomplete"


def _summary_electric(facts: dict[str, Any]) -> str | None:
    nearest = min(
        (value for value in (
            _num(item.get("distance_mi"))
            for item in (_dicts(facts.get("nearest_transmission_lines"))
                         + _dicts(facts.get("nearest_substations")))
        ) if value is not None),
        default=None)
    return (f"Nearest mapped feature {nearest:.2f} mi"
            if nearest is not None else "No mapped feature in search radius")


def _summary_txdot_extended(facts: dict[str, Any]) -> str | None:
    exposure = _as_dict(facts.get("traffic_exposure"))
    pipeline = _as_dict(facts.get("project_pipeline"))
    aadt = _fmt_int(_as_dict(exposure.get("top_aadt")).get("aadt"))
    count = int(_num(pipeline.get("count_within_radius"))
                or len(_dicts(pipeline.get("items"))))
    crash = _as_dict(facts.get("crash_data"))
    crash_status = _text(crash.get("integration_status")) or "unknown"
    return _join([
        f"{aadt} AADT" if aadt else None,
        f"{count} projects",
        "CRIS gated" if crash_status == "credentials_not_configured"
        else _labelize(crash_status),
    ])


def _summary_twdb(facts: dict[str, Any]) -> str | None:
    water = _text(facts.get("water_provider")) or _text(
        _as_dict(facts.get("water_provider_operating")).get("name"))
    sewer = _text(facts.get("wastewater_provider"))
    if water or sewer:
        return " / ".join(part for part in (water, sewer) if part)
    return "No CCN coverage"


def _summary_soil(facts: dict[str, Any]) -> str | None:
    units = _dicts(facts.get("map_units"))
    if not units:
        return ("No SSURGO unit at point"
                if _text(facts.get("status")) == "no_soils_at_point"
                else "Screening incomplete")
    flags: list[str] = []
    if facts.get("any_hydric_component") is True:
        flags.append("Hydric")
    if facts.get("any_prime_farmland") is True:
        flags.append("Prime farmland")
    slope = _num(facts.get("max_component_slope_pct"))
    if slope is not None and slope >= 15:
        flags.append(f"Slope {slope:.0f}%+")
    count = len(units)
    return (f"{count} map unit{'' if count == 1 else 's'}"
            + (" · " + ", ".join(flags) if flags else ""))


def _summary_hydro(facts: dict[str, Any]) -> str | None:
    if (_text(facts.get("status")) == "temporary_unavailable"
            or (_text(facts.get("source_status")) or "").startswith(
                "temporary_")):
        return "USGS NHD unavailable"
    total = int(_num(facts.get("flowlines_count"))
                or len(_dicts(facts.get("flowlines"))))
    radius = _num(facts.get("search_radius_m"))
    radius_label = f"{radius:,.0f}" if radius else "screened"
    if total == 0:
        return f"No flowlines within {radius_label}m"
    named = int(_num(facts.get("named_flowlines_count")) or 0)
    return (f"{total} flowline{'' if total == 1 else 's'} · {named} named "
            f"· {radius_label}m")


# The panel's own buckets, in its own order — the reader compares this line
# across reports, so the order is part of the answer.
_OVERLAY_BUCKETS: list[tuple[str, str]] = [
    ("subdivisions", "Subdivision"),
    ("mud_districts", "MUD"),
    ("wcid_districts", "WCID"),
    ("esd_districts", "ESD"),
    ("pid_districts", "PID"),
    ("tirz_districts", "TIRZ"),
    ("school_districts", "School district"),
    ("metro_districts", "Metro district"),
]


def _summary_overlays(facts: dict[str, Any]) -> str | None:
    rows = [
        f"{label}:{len(_dicts(facts.get(key)))}"
        for key, label in _OVERLAY_BUCKETS
        if _dicts(facts.get(key))
    ]
    if rows:
        return " · ".join(rows)
    return ("No overlay matches"
            if _text(facts.get("status")) == "no_overlays" else "Not covered")


def _summary_fire(facts: dict[str, Any]) -> str | None:
    nearest = _as_dict(facts.get("nearest_station"))
    if nearest:
        miles = _fmt_miles(nearest.get("distance_mi"))
        return f"Nearest station · {miles}" if miles else "Nearest station mapped"
    area = _as_dict(facts.get("service_area"))
    name = _title_case(area.get("name"))
    return f"Serving area · {name}" if name else "Fetch issue"


def _summary_schools(facts: dict[str, Any]) -> str | None:
    total = int(_num(facts.get("schools_count"))
                or len(_dicts(facts.get("schools"))))
    radius = _num(facts.get("search_radius_mi"))
    radius_label = f"{radius:g}" if radius else "?"
    if total == 0:
        return f"No schools within {radius_label} mi"
    return (f"{total} school{'' if total == 1 else 's'} within "
            f"{radius_label} mi")


def _summary_transit(facts: dict[str, Any]) -> str | None:
    if facts.get("provider_short"):
        # A state's or county's own transit source (spec 2.6): a part that
        # did not answer is unknown, never zero.
        def count(key: str, part: str, label: str) -> str:
            if facts.get(key) is None or (
                (facts.get("parts") or {}).get(part, {}).get("coverage")
                in ("not_connected", "not_applicable")
            ):
                return f"{label}: not checked"
            return f"{int(_num(facts.get(key)) or 0)} {label}"
        return (f"{count('bus_stops_count', 'bus_stops', 'bus stops')} · "
                f"{count('rail_stations_count', 'rail_stations', 'rail stations')}")
    bus = int(_num(facts.get("bus_stops_count")) or 0)
    rail = int(_num(facts.get("rail_stations_count")) or 0)
    if bus + rail == 0:
        return "Outside CMTA walkshed"
    return f"{bus} bus stops · {rail} rail stations"


def _summary_historic(facts: dict[str, Any]) -> str | None:
    if facts.get("is_in_local_historic_district") is True:
        count = len(_dicts(facts.get("districts")))
        return f"Inside {count} historic district{'' if count == 1 else 's'}"
    landmarks = int(_num(facts.get("nearby_landmarks_count"))
                    or len(_dicts(facts.get("nearby_landmarks"))))
    if landmarks > 0:
        return (f"{landmarks} landmark{'' if landmarks == 1 else 's'} "
                "within 0.25 mi")
    return "No historic overlays"


def _summary_plats(facts: dict[str, Any]) -> str | None:
    if _as_dict(facts.get("recorded_deed")):
        return "Unplatted — abstract-survey tract"
    number = _text(facts.get("probable_plat_number"))
    if number:
        return f"Probable recorded plat: {number}"
    if _dicts(facts.get("record_candidates")):
        return "Recorded plat candidates found"
    in_review = len(_dicts(facts.get("in_review")))
    approved = len(_dicts(facts.get("approved")))
    if in_review or approved:
        return f"{in_review} in review · {approved} approved"
    return "No recorded-plat candidate found"


def _summary_entitlement(facts: dict[str, Any], coverage: str) -> str | None:
    """The collapsed-row subtitle. A jurisdiction with no readable case feed
    is only summarised as a manual check when nothing was ACTUALLY found —
    otherwise the row would say "manual city case check" over a card that
    lists 41 plats and 2,000 lots."""
    del coverage  # what was found decides the subtitle, not how it was found
    jurisdiction = _title_case(facts.get("jurisdiction")) or "City"
    records = _dicts(facts.get("city_entitlement_records"))
    if records:
        return f"{jurisdiction} · {len(records)} city cases"
    subdivisions = _dicts(facts.get("subdivision_cases"))
    if subdivisions:
        count = int(_num(facts.get("subdivision_case_count")) or len(subdivisions))
        lots = _num(facts.get("subdivision_total_lots"))
        sweep = _radius_phrase(facts)
        summary = f"{count} plats" + (f" within {sweep}" if sweep else " nearby")
        if lots:
            summary += f" · ~{_fmt_int(lots)} lots"
        read_cases = _dicts(_as_dict(facts.get("city_case_reads")).get("cases"))
        if read_cases:
            summary += f" · {len(read_cases)} city cases"
        return summary
    read_cases = _dicts(_as_dict(facts.get("city_case_reads")).get("cases"))
    if read_cases:
        return f"{jurisdiction} · {len(read_cases)} city cases"
    zoning = len(_dicts(facts.get("zoning_cases")))
    site_plans = len(_dicts(facts.get("site_plan_cases")))
    if zoning or site_plans:
        return f"{zoning} zoning · {site_plans} site plan"
    return f"{jurisdiction} · manual city case check"


def _summary_tax(facts: dict[str, Any]) -> str | None:
    if "levies" in facts:
        total = _num(facts.get("total_mills"))
        count = len(_dicts(facts.get("levies")))
        return (f"{count} taxing units"
                + (f" · {total:,.3f} mills" if total is not None else ""))
    matched = int(_num(facts.get("districts_matched")) or 0)
    queried = int(_num(facts.get("districts_queried")) or 0)
    year = _text(facts.get("fiscal_year"))
    return (f"{matched}/{queried} districts matched"
            + (f" · FY {year}" if year else ""))


def _summary_future_land_use(facts: dict[str, Any]) -> str | None:
    jurisdiction = _text(facts.get("jurisdiction"))
    if jurisdiction:
        return f"{_labelize(jurisdiction)} comp plan"
    status = _text(facts.get("status")) or "unknown"
    if status == "no_city_flu_authority":
        return "Unincorporated — no city plan"
    if status == "manual_verification_required":
        return "Manual lookup"
    return "Not covered"


def _summary_environmental(facts: dict[str, Any]) -> str | None:
    statuses = _as_dict(facts.get("source_statuses"))
    psts = _dicts(facts.get("pst_records"))
    echos = _dicts(facts.get("echo_records"))
    # The adapter writes tceq_pst / epa_echo (it never wrote pst / echo).
    pst_out = _text(statuses.get("tceq_pst")) not in (None, "queried")
    echo_out = _text(statuses.get("epa_echo")) not in (None, "queried")
    radius = _num(facts.get("search_radius_m"))
    state_part = (
        "State records not connected"
        if _text(facts.get("state_records")) == "not_connected"
        else "PST unavailable" if pst_out else f"{len(psts)} PST"
    )
    return " · ".join([
        state_part,
        "ECHO unavailable" if echo_out
        else f"{len(echos)} EPA record{'' if len(echos) == 1 else 's'}",
        f"{radius:,.0f}m" if radius else "screened",
    ])


def _summary_elevation(facts: dict[str, Any]) -> str | None:
    relief = _fmt_ft(facts.get("relief_ft"))
    grade = _num(facts.get("sampled_max_grade_pct"))
    return _join([f"{relief} of fall" if relief else None,
                  f"max {grade:.1f}% grade" if grade is not None else None])


def _summary_drainage(facts: dict[str, Any]) -> str | None:
    lows = len(_dicts(facts.get("topographic_depressions")))
    paths = len(_dicts(facts.get("flow_paths")))
    return f"{lows} low spots · {paths} flow paths"


def _summary_roadway(facts: dict[str, Any]) -> str | None:
    roads = _dicts(facts.get("roadways"))
    if not roads:
        return "No mapped roadway record"
    name = _title_case(roads[0].get("road_name"))
    return _join([f"{len(roads)} roadway{'' if len(roads) == 1 else 's'}",
                  name])


def _summary_bridges(facts: dict[str, Any]) -> str | None:
    total = int(_num(facts.get("count_within_radius"))
                or len(_dicts(facts.get("items"))))
    nearest = _fmt_miles(facts.get("nearest_distance_mi"))
    if total == 0:
        return "No bridge records nearby"
    return _join([f"{total} bridges & culverts",
                  f"nearest {nearest}" if nearest else None])


def _summary_aerials(images: list[dict[str, Any]]) -> str:
    years = len(images)
    return f"{years} year{'' if years == 1 else 's'} of imagery" if years \
        else "temporarily unavailable"


def _summary_for(slot: str, primary_id: str, facts: dict[str, Any],
                 extras: dict[str, dict[str, Any]], run: Any,
                 coverage: str = "") -> str | None:
    """The panel subtitle for this section, or None to fall back."""
    del primary_id
    if slot == "parcel":
        return _summary_parcel(facts, run)
    if slot == "site_constraints":
        return _summary_site_constraints(facts)
    if slot == "fema_nfhl":
        return _summary_flood(facts)
    if slot == "austin_zoning":
        return _summary_zoning(facts, extras.get("jurisdiction_resolver", {}))
    if slot == "jurisdiction":
        return _jurisdiction_label(facts, facts)
    if slot == "traffic":
        return _summary_traffic(facts)
    if slot == "txdot_projects":
        return _summary_projects(facts)
    if slot == "census_acs":
        return _summary_census(facts)
    if slot == "osm_overpass":
        return _summary_osm(facts)
    if slot == "wetlands_nwi":
        return _summary_wetlands(facts)
    if slot == "electric_power_infrastructure":
        return _summary_electric(facts)
    if slot == "txdot_extended":
        return _summary_txdot_extended(facts)
    if slot == "twdb_ccn":
        return _summary_twdb(facts)
    if slot == "soil_intel":
        return _summary_soil(facts)
    if slot == "hydro_intel":
        return _summary_hydro(facts)
    if slot == "district_overlays":
        return _summary_overlays(facts)
    if slot == "fire_district":
        return _summary_fire(facts)
    if slot == "schools_intel":
        return _summary_schools(facts)
    if slot == "transit_intel":
        return _summary_transit(facts)
    if slot == "historic_intel":
        return _summary_historic(facts)
    if slot == "plats_intel":
        return _summary_plats(facts)
    if slot == "entitlement_pipeline":
        return _summary_entitlement(facts, coverage)
    if slot == "tax_intel":
        return _summary_tax(facts)
    if slot == "future_land_use":
        return _summary_future_land_use(facts)
    if slot == "environmental_screening":
        return _summary_environmental(facts)
    if slot == "usgs_elevation":
        return _summary_elevation(facts)
    if slot == "drainage_screen":
        return _summary_drainage(facts)
    if slot == "txdot_roadway":
        return _summary_roadway(facts)
    if slot == "txdot_bridges":
        return _summary_bridges(facts)
    if slot == "broadband_intel":
        return "Manual verification"
    if slot == "public_aerial_imagery":
        return "Recent public aerial"
    return None


# ── Confidence badge (the panel's ConfidenceBadge, ported verbatim) ────────
# Its exact vocabulary rides on the card so the client never invents one:
#   verified public fact | screened indicator | manual verification required
#   | partial | warning | unknown


# Two sections carry NO badge in the panel, and the absence is deliberate:
#   · Parcel Info's shell is bound to the geocode adapter, which has no
#     confidence axis of its own — the section is the map and the address.
#   · Entitlement Pipeline passes showBadge={!notWired}: a jurisdiction with
#     no case feed has nothing to be confident about yet.
_NO_BADGE_SLOTS = {"parcel"}


def _confidence_label(
    slot: str, primary: dict[str, Any],
    contributors: list[tuple[str, dict[str, Any]]],
) -> str | None:
    coverage = str(primary.get("coverage_status") or "")
    if slot in _NO_BADGE_SLOTS:
        return None
    if slot == "entitlement_pipeline" and coverage == "not_yet_wired":
        return None
    if any(_errors_of(result) for _, result in contributors):
        return "warning"
    if coverage == "partial":
        return "partial"
    raw = str(primary.get("data_confidence_status") or "unknown")
    return raw.replace("_", " ")


# ── Card assembly ──────────────────────────────────────────────────────────


def _slot_source_ids(slot: str) -> list[str]:
    if slot == "parcel":
        return ["wcad_parcel", "tcad_parcel"]
    if slot == "jurisdiction":
        return ["jurisdiction_resolver"]
    if slot == "traffic":
        return ["txdot_aadt", "txdot_aadt_history"]
    return [slot]


def _parcel_resolved(result: dict[str, Any]) -> bool:
    if str(result.get("coverage_status") or "") != "covered":
        return False
    facts = _facts_of(result)
    return bool(facts.get("parcel_id") or facts.get("parcel_polygon"))


def _empty_card(slot: str, status: str, takeaway: str,
                sources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "card_key": slot,
        "title": _CARD_TITLES[slot],
        "icon": _CARD_ICONS.get(slot, "•"),
        "order": _SECTION_INDEX.get(slot, len(_SECTION_ORDER)),
        "summary": _compact(takeaway),
        "hero": None,
        "confidence": "unknown",
        "status": status,
        "takeaway": takeaway,
        "facts": [],
        "tables": [],
        "links": [],
        "image": None,
        "images": [],
        "advisories": [],
        "sources": sources,
        "checked_at": None,
        "narrative": None,
        "retryable": False,
    }


def _not_checked_card(slot: str, run_status: str) -> dict[str, Any]:
    placeholder = _slot_source_ids(slot)[-1] if slot == "parcel" else (
        _slot_source_ids(slot)[0]
    )
    takeaway = (
        _NOT_CHECKED_CANCELLED if run_status == "cancelled"
        else _NOT_CHECKED_STOPPED
    )
    return _empty_card(slot, "not_checked", takeaway,
                       [_provenance(placeholder, None)])


def _card_status(
    primary: dict[str, Any], contributors: list[tuple[str, dict[str, Any]]],
) -> str:
    coverage = str(primary.get("coverage_status") or "")
    access = str(primary.get("source_access_status") or "")
    if coverage == "not_supported_in_jurisdiction":
        return "not_covered"
    if coverage == "manual_verification_required" or access == "HTML_OR_PDF_ONLY":
        return "manual"
    if any(_errors_of(result) for _, result in contributors):
        return "issue"
    # Coverage "partial" WITHOUT errors is a source's normal
    # screening-level success (e.g. terrain & drainage always reports
    # partial — it is a first-pass screen, not a survey). The pinned
    # the reference app panel renders this as a badge on normal content, never
    # as a failure; "We couldn't get a full answer" would be untrue.
    # Documented refinement of the spec's coarse §10.4 partial→issue
    # row (flagged for review 2026-08-01); the source's own advisory
    # still rides the card's advisories list.
    return "ok"


def _narrative_payload(entry: Any) -> dict[str, str] | None:
    """Emit the §10.6 five-field narrative with the exact frontend types
    (all strings — null fields become empty strings)."""
    if not isinstance(entry, dict):
        return None
    fields = ("headline", "broker_read", "why_it_matters", "next_step", "caveat")
    out = {field_name: (_plain(entry.get(field_name)) or "")
           for field_name in fields}
    if not (out["headline"] or out["broker_read"]):
        return None
    return out


def _build_card(
    slot: str,
    run: Any,
    results: dict[str, Any],
    narratives: dict[str, Any],
    retry_ids: set[str],
) -> dict[str, Any] | None:
    run_status = str(getattr(run, "status", "") or "")
    candidate_ids = _slot_source_ids(slot)
    present: list[tuple[str, dict[str, Any]]] = [
        (sid, results[sid]) for sid in candidate_ids
        if isinstance(results.get(sid), dict)
    ]
    # Stubs never render a card (ported rule) — drop them from contributors,
    # except the sources the panel renders anyway when they carry real
    # findings (allowNotYetWired).
    present = [
        (sid, result) for sid, result in present
        if str(result.get("coverage_status") or "") != "not_yet_wired"
        or (sid in _ALLOW_NOT_YET_WIRED and _facts_of(result))
    ]

    if slot == "parcel" and present:
        # First-resolved precedence (ported): WCAD's verified parcel wins;
        # a transient loser is internal noise and stays off the card.
        resolved = [pair for pair in present if _parcel_resolved(pair[1])]
        if resolved:
            present = resolved[:1]
        else:
            present = present[-1:]  # the general county path (tcad)

    if not present:
        if run_status in ("cancelled", "failed"):
            return _not_checked_card(slot, run_status)
        return None  # completed runs: nothing staged for this slot

    primary_id, primary = present[0]
    status = _card_status(primary, present)
    facts_dict = _facts_of(primary)
    if facts_dict.get("source_module") and str(
            primary.get("coverage_status") or "") not in ("covered", "partial"):
        # A state's or county's own source that did not answer (spec 2.6):
        # nothing on this card was checked, so no formatter may turn the
        # missing answers into "none" or "0".
        agency = _text(facts_dict.get("provider_short")) or "The source"
        card = _empty_card(
            slot, "issue",
            f"{agency} did not answer this time, so nothing on this card was "
            "checked. This is not a \"none found\" result; try again later.",
            [_provenance(sid, result) for sid, result in present],
        )
        card["title"] = _card_title(slot, facts_dict)
        card["retryable"] = any(sid in retry_ids for sid, _ in present)
        return card
    extras = {
        sid: _facts_of(_as_dict(results.get(sid)))
        for sid in ("txdot_aadt_history", "district_overlays",
                    "jurisdiction_resolver")
    }
    content = _format_source(slot, primary_id, facts_dict, extras)

    advisories: list[str] = list(content.advisories)
    for _sid, result in present:
        for advisory in _advisories_of(result):
            if advisory not in advisories:
                advisories.append(advisory)

    sources = [_provenance(sid, result) for sid, result in present]
    checked_at = _rollup_checked_at(sources)
    retryable = any(sid in retry_ids for sid, _ in present)

    takeaway = content.takeaway
    facts = content.facts
    tables = content.tables
    links = content.links
    image = content.image

    if status == "not_covered":
        # Honest scope line — but the adapter's own plain reason (e.g. "in
        # an ETJ, no city zoning code applies") is more useful when present.
        takeaway = (
            _first_sentence(facts_dict.get("advisory"))
            or _SCOPE_LINES.get(primary_id, _DEFAULT_SCOPE_LINE)
        )
        facts, tables, image = [], [], None
    elif status == "manual" and primary_id not in _SELF_WORDED:
        pointer = (
            _plain(facts_dict.get("advisory"))
            or "open the official page and search this address."
        )
        takeaway = f"{_MANUAL_PREFIX}{pointer}"
    elif status == "issue" and _text(facts_dict.get("state_records")) == "not_connected":
        # Federal-only environmental screen (outside Texas): keep its own
        # line, which says both that EPA did not answer and that the state's
        # records are not connected.
        pass
    elif status == "issue":
        failing = next(
            (sid for sid, result in present if _errors_of(result)), primary_id
        )
        failing_name = _SOURCE_NAMES.get(
            failing, failing.replace("_", " ").title()
        )
        takeaway = f"We couldn't get a full answer from {failing_name}."
        error_text = "; ".join(_errors_of(dict(present)[failing]))
        if error_text:
            detail = _broker_error_detail(failing, error_text)
            if detail not in advisories:
                advisories.append(detail)

    narrative = None
    if status in ("ok", "issue", "manual"):
        narrative = _narrative_payload(narratives.get(primary_id))
        if narrative is None and slot == "traffic":
            narrative = _narrative_payload(narratives.get("txdot_aadt"))

    # The collapsed accordion row: glyph, name, compact summary, badge. When
    # the card is not a normal answer the summary says THAT — a scope line or
    # "Needs a human look" is the honest one-liner, not a stale hero fact.
    federal_only = _text(facts_dict.get("state_records")) == "not_connected"
    if status in ("not_covered", "issue") and not federal_only:
        summary = _compact(takeaway)
    else:
        summary = (_summary_for(slot, primary_id, facts_dict, extras, run,
                                str(primary.get("coverage_status") or ""))
                   or _compact(takeaway))

    # Parcel Info carries the map, so it also carries the point the map is
    # centred on — the coordinate row was the one datum the card never showed.
    hero = None
    if slot == "parcel" and status == "ok":
        hero = _parcel_hero(run)
        coordinates = _coordinates(run)
        if coordinates and not any(
            row["label"] == "Coordinates" for row in facts
        ):
            facts = [*facts, _fact("Coordinates", coordinates,
                                   hint="Where every screen was run.")]

    return {
        "card_key": slot,
        "title": _card_title(slot, facts_dict),
        "icon": _CARD_ICONS.get(slot, "•"),
        "order": _SECTION_INDEX.get(slot, len(_SECTION_ORDER)),
        "summary": summary,
        "hero": hero,
        "confidence": _confidence_label(slot, primary, present),
        "status": status,
        "takeaway": takeaway,
        "facts": facts,
        "tables": tables,
        "links": links,
        "image": image,
        # v3 gallery — only the derived historical-aerials card fills it
        # today; every adapter-backed card emits the key empty so the DTO is
        # uniform and the client never has to branch on presence.
        "images": [],
        "advisories": advisories[:6],
        "sources": sources,
        "checked_at": checked_at,
        "narrative": narrative,
        "retryable": retryable,
    }


# ── "What this land was before" (§19) — the one derived-backed card ────────
# Everything else on the report is projected from `adapter_results`; the era
# aerial timeline and its prior-use read live in `derived.historical_aerials`
# because they are produced AFTER the adapter waves. The copy below is the
# card's own — the model's words are rendered as observations, never as facts.

_HA_HONESTY = (
    "These are observations from old photographs — a screening aid, not a "
    "verified history of the site. A Phase I environmental report is the "
    "binding record of what this land was used for."
)
_HA_NO_PHOTOS = (
    "We could not load older photos of this land this time. That says "
    "nothing about its history — the photo archive may be temporarily "
    "unavailable."
)
_HA_ATTRIBUTION = (
    "Historical aerial photos: Texas Geographic Information Office (TxGIO)"
)

# Closed set (matches historical_aerials.RISK_CATEGORIES) → the plain words
# a reader understands. Free model text never becomes a label.
_HA_RISK_LABELS: dict[str, str] = {
    "fuel_or_chemical": "Possible fuel or chemical storage",
    "industrial_or_yard": "Possible industrial or storage yard",
    "pit_or_quarry": "Possible pit or quarry",
    "landfill_or_dumping": "Possible dumping or landfill",
    "agricultural": "Heavy farming use",
    "other": "Worth a closer look",
}

# Worst first. The model returns signals in whatever order it wrote them, so
# a run that spots BOTH long-term farming and an industrial-looking yard led
# with the farming — the least useful thing on the list (caught on the live
# Chandler run, 2026-08-02). Severity decides what the reader sees first.
_HA_RISK_ORDER: dict[str, int] = {
    "fuel_or_chemical": 0,
    "landfill_or_dumping": 1,
    "pit_or_quarry": 2,
    "industrial_or_yard": 3,
    "agricultural": 4,
    "other": 5,
}


def _ha_by_severity(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable sort, worst first — ties keep the model's own order."""
    return sorted(
        signals,
        key=lambda row: _HA_RISK_ORDER.get(
            str(row.get("risk_category") or ""), len(_HA_RISK_ORDER),
        ),
    )


def _ha_era_images(eras: list[dict[str, Any]],
                   attribution: str | None = None) -> list[dict[str, str]]:
    """The filmstrip, oldest first — one entry per era that actually
    resolved. An unavailable year contributes no picture (and its absence is
    explained in the advisory, never implied to mean empty land)."""
    images: list[dict[str, str]] = []
    for era in eras:
        entry = _as_dict(era)
        url = _text(entry.get("image_url"))
        year = _text(entry.get("year"))
        if (
            not url
            or not url.lower().startswith("http")
            or str(entry.get("status") or "") != "resolved"
        ):
            continue
        images.append({
            "url": url,
            "label": year or "",
            "caption": (
                (f"Aerial photo from {year}" if year else "Aerial photo")
                + (" (interim release)." if entry.get("interim") else ".")
            ),
            "attribution": attribution or _HA_ATTRIBUTION,
        })
    return images


def _ha_seen_phrase(first: Any, last: Any) -> str:
    """'visible 1996–2010' / 'visible from 1996' / '' — the years a signal
    was on the ground, in reader English."""
    first_year = _text(first)
    last_year = _text(last)
    if first_year and last_year and first_year != last_year:
        return f"{first_year}–{last_year}"
    if first_year:
        return first_year
    if last_year:
        return last_year
    return ""


def _ha_takeaway(reading: dict[str, Any], signals: list[dict[str, Any]]) -> str:
    """One line for the collapsed card. When the photos show something an
    environmental review would want, the WORST of those leads and is named —
    the summary never buries a flagged prior use, and the reader sees what
    kind of concern it is before opening the card."""
    summary = _plain(reading.get("summary"))
    if reading.get("environmental_review_relevant") and signals:
        top = signals[0]
        label = _HA_RISK_LABELS.get(str(top.get("risk_category") or ""))
        detail = _first_sentence(top.get("signal"), limit=140)
        if label and detail:
            when = _ha_seen_phrase(top.get("first_seen"), top.get("last_seen"))
            detail = detail.rstrip(".")
            return _plain(
                f"Worth confirming before you buy: {label[0].lower()}{label[1:]}"
                f" — {detail[0].lower()}{detail[1:]}"
                + (f" ({when})." if when else ".")
            ) or ""
    return _first_sentence(summary, limit=260) or (
        "Older photos of this land are below."
    )


def _historical_aerials_card(run: Any, derived: dict[str, Any]) -> dict[str, Any] | None:
    """The §19 card. Returns None when the run carries no historical-aerials
    block at all (an older saved report, or the feature switched off) — an
    absent card is honest; an empty one would look like a finding."""
    meta = _as_dict(derived.get("historical_aerials_meta"))
    payload = _as_dict(derived.get("historical_aerials"))
    if not meta and not payload:
        return None
    if str(meta.get("status") or "") == "disabled":
        return None
    if str(meta.get("status") or "") == "not_connected":
        return _empty_card(
            "historical_aerials", "not_covered",
            f"Not connected for {state_names.name_of(str(meta.get('state') or ''))} yet.",
            [{"source_id": "historical_aerials",
              "source_name": _SOURCE_NAMES["historical_aerials"],
              "source_url": None, "checked_at": None, "from_cache": False}],
        )

    eras = [_as_dict(era) for era in _as_list(payload.get("eras"))]
    images = _ha_era_images(eras, _text(payload.get("attribution")))
    checked_at = _iso(getattr(run, "finished_at", None))
    in_texas = (getattr(run, "state", None) or "TX").upper() == "TX"
    sources = [{
        "source_id": "historical_aerials",
        "source_name": (
            _SOURCE_NAMES["historical_aerials"] if in_texas
            else f"{state_names.name_of((run.state or '').upper())} historical aerial photos"
        ),
        "source_url": None,
        "checked_at": checked_at if images else None,
        "from_cache": str(meta.get("cache") or "") in ("hit", "stale"),
    }]

    if not images:
        # No pictures at all — the honest failure state. NOT "not_covered":
        # the archive covers Texas, it just did not answer this time.
        card = _empty_card(
            "historical_aerials", "issue",
            _plain(meta.get("message")) or _HA_NO_PHOTOS, sources,
        )
        card["advisories"] = [_HA_HONESTY]
        return card

    reading = _as_dict(payload.get("reading"))
    signals = _ha_by_severity(
        [_as_dict(row) for row in _as_list(reading.get("prior_use_signals"))]
    )
    observations = [
        _as_dict(row) for row in _as_list(reading.get("era_observations"))
    ]
    changes = [_as_dict(row) for row in _as_list(reading.get("change_events"))]
    years = [_text(era.get("year")) for era in eras if _text(era.get("year"))]
    resolved_years = [
        _text(era.get("year")) for era in eras
        if str(era.get("status") or "") == "resolved" and _text(era.get("year"))
    ]

    facts: list[dict[str, Any]] = []
    if reading:
        _row(facts, "What this land was before", reading.get("summary"))
    _row(
        facts, "Photos we found",
        (
            f"{len(images)} of {len(years)} years"
            if len(images) != len(years) else f"{len(images)} years"
        ),
        hint=(
            f"{resolved_years[0]} to {resolved_years[-1]}"
            if len(resolved_years) > 1 else None
        ),
    )

    tables: list[dict[str, Any]] = []
    signal_table = _table(
        "Worth confirming before you buy",
        ["What the photos show", "When", "Why it matters"],
        [
            [
                _join([
                    _HA_RISK_LABELS.get(
                        str(row.get("risk_category") or ""), None,
                    ),
                    _plain(row.get("signal")),
                ], sep=" — "),
                _ha_seen_phrase(row.get("first_seen"), row.get("last_seen")),
                _plain(row.get("why_it_matters")),
            ]
            for row in signals
        ],
    )
    if signal_table:
        tables.append(signal_table)
    year_table = _table(
        "Year by year",
        ["Year", "What you can see", "What it looks like it was used for",
         "How sure"],
        [
            [
                _text(row.get("year")),
                _plain(row.get("what_is_visible")),
                _plain(row.get("apparent_use")),
                _plain(row.get("confidence")),
            ]
            for row in observations
        ],
    )
    if year_table:
        tables.append(year_table)
    change_table = _table(
        "What changed", ["Between", "What changed"],
        [
            [_plain(row.get("period")), _plain(row.get("what_changed"))]
            for row in changes
        ],
    )
    if change_table:
        tables.append(change_table)

    advisories: list[str] = []
    gap_note = _plain(payload.get("advisory"))
    if gap_note and len(images) != len(years):
        # Only worth saying when the timeline actually has holes — on a full
        # ladder it would just restate the years already on screen.
        advisories.append(gap_note)
    if not reading:
        # The photos are the deliverable; say plainly why the written read is
        # missing and never let silence read as "nothing was found". The §11.3
        # ladder message (no key / budget cap / rejected key / unavailable)
        # rides through verbatim when the builder supplied one.
        ladder_message = _plain(meta.get("message"))
        advisories.append(
            f"{ladder_message} The photos above are still the archive's own."
            if ladder_message
            else (
                "The photos are shown, but the written read is not available "
                "this time. No read does not mean a clean history."
            )
        )
    advisories.append(_HA_HONESTY)

    return {
        "card_key": "historical_aerials",
        "title": _CARD_TITLES["historical_aerials"],
        "icon": _CARD_ICONS["historical_aerials"],
        "order": _SECTION_INDEX["historical_aerials"],
        "summary": _summary_aerials(images),
        "hero": None,
        "confidence": "screened indicator",
        "status": "ok",
        "takeaway": (
            _ha_takeaway(reading, signals) if reading
            else "Older photos of this land are below."
        ),
        "facts": facts,
        "tables": tables,
        "links": [],
        "image": None,
        "images": images,
        "advisories": advisories[:6],
        "sources": sources,
        "checked_at": checked_at,
        # The §11 narrative overlay writes per-ADAPTER entries; this card is
        # derived and carries its own read, so it never takes one.
        "narrative": None,
        "retryable": False,
    }


# ── Bottom line (deterministic by default; §10.3 summary selectors) ────────


def _deterministic_summary_parts(results: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    fema = _facts_of(_as_dict(results.get("fema_nfhl")))
    zone = _text(fema.get("zone"))
    if zone == "X" and not (
        fema.get("in_floodplain") is True or fema.get("has_sfha") is True
    ):
        parts.append("minimal flood risk (Zone X)")
    elif zone:
        parts.append(f"flood zone {zone}")
    aadt = _facts_of(_as_dict(results.get("txdot_aadt")))
    counts = aadt.get("traffic_counts")
    if isinstance(counts, list) and counts:
        top = _as_dict(counts[0])
        top_aadt = _fmt_int(top.get("aadt"))
        road = _title_case(top.get("road_name"))
        if top_aadt:
            parts.append(
                f"about {top_aadt} vehicles a day"
                + (f" on {road}" if road else "")
            )
    zoning = _facts_of(_as_dict(results.get("austin_zoning")))
    ztype = _text(zoning.get("zoning_type") or zoning.get("base_zone"))
    jkind = (_text(zoning.get("jurisdiction_kind")) or "").lower()
    if ztype and ztype != "NO_CITY_ZONING" and _meaningful_zone(ztype):
        parts.append(f"zoned {ztype}")
    elif ztype == "NO_CITY_ZONING" or jkind in (
        "unincorporated", "unincorporated_county", "etj",
    ):
        parts.append("no city zoning")
    total_pois = _poi_total(_facts_of(_as_dict(results.get("osm_overpass"))))
    if total_pois:
        parts.append(f"{total_pois:,} places nearby")
    census = _facts_of(_as_dict(results.get("census_acs")))
    rings = _as_dict(census.get("rings"))
    for key, label in (("1mi", "a mile"), ("3mi", "three miles"),
                       ("5mi", "five miles")):
        pop = _fmt_int(_as_dict(rings.get(key)).get("population"))
        if pop:
            parts.append(f"{pop} people within {label}")
            break
    return parts


def _deterministic_bottom_line_text(run: Any) -> str:
    results = _as_dict(getattr(run, "adapter_results", None))
    parts = _deterministic_summary_parts(results)
    if parts:
        text = ", ".join(parts)
        return text[0].upper() + text[1:] + "."
    if results:
        return "What the public record shows for this site is below."
    return ""


def _run_rollup_checked_at(results: dict[str, Any]) -> str | None:
    """§10.1: oldest contributing successful source across the run."""
    stamped: list[tuple[datetime, str]] = []
    for result in results.values():
        if not isinstance(result, dict) or not _successful(result):
            continue
        iso_value = _iso(result.get("fetched_at"))
        parsed = _parse_ts(iso_value)
        if parsed is not None and iso_value is not None:
            stamped.append((parsed, iso_value))
    if not stamped:
        return None
    stamped.sort(key=lambda pair: pair[0])
    return stamped[0][1]


def _bottom_line(run: Any, derived: dict[str, Any]) -> dict[str, Any]:
    results = _as_dict(getattr(run, "adapter_results", None))
    checked_at = _run_rollup_checked_at(results)
    meta = _as_dict(derived.get("narrative_meta"))
    ai_text = _plain(derived.get("narrative_bottom_line"))
    if str(meta.get("status") or "") == "ok" and ai_text:
        return {"text": ai_text, "kind": "ai", "checked_at": checked_at}
    text = _deterministic_bottom_line_text(run)
    message = _plain(meta.get("message"))
    if message and str(meta.get("status") or "").startswith("fallback"):
        # §11.3: the ladder's plain message rides with the deterministic
        # bottom line so the report says why there are no written summaries.
        text = f"{text} {message}".strip()
    return {"text": text, "kind": "deterministic", "checked_at": checked_at}


# ── Manual follow-ups + anchor note ────────────────────────────────────────


def _manual_checks(derived: dict[str, Any]) -> list[dict[str, Any]]:
    items = derived.get("manual_lookups_needed")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        entry = _as_dict(item)
        label = _plain(entry.get("item"))
        if not label:
            continue
        for suffix in (" - manual lookup", " - fetch failed"):
            label = label.removesuffix(suffix)
        contact = _plain(entry.get("who_to_contact"))
        link = contact if contact and contact.startswith("http") else None
        reason = _plain(entry.get("why")) or "Worth confirming directly."
        if contact and link is None:
            # A non-URL pointer (an office name, "search the county page")
            # still reaches the reader — it must never be dropped just
            # because it isn't a link (§10.4 ported pointer).
            reason = f"{reason} Where to look: {contact}".strip()
        out.append({
            "label": label,
            "reason": reason,
            "link": link,
        })
    return out


# ── §9.4 freshness helpers (computed from settings — never hard-coded) ─────


def freshness_band(
    oldest_checked_at: Any, settings: Any, now: datetime | None = None,
) -> str | None:
    """current | expiring | expired, from site_check_freshness_days and
    site_check_expiring_notice_days. None when there is no basis date."""
    basis = _parse_ts(oldest_checked_at)
    if basis is None:
        return None
    now = now or datetime.now(UTC)
    freshness_days = int(settings.site_check_freshness_days)
    notice_days = int(settings.site_check_expiring_notice_days)
    age = now - basis
    if age >= timedelta(days=freshness_days):
        return "expired"
    if age >= timedelta(days=freshness_days - notice_days):
        return "expiring"
    return "current"


def refresh_after_iso(oldest_checked_at: Any, settings: Any) -> str | None:
    """The 'facts refresh after <date>' basis: oldest successful source date
    plus the configured freshness window."""
    basis = _parse_ts(oldest_checked_at)
    if basis is None:
        return None
    return (
        basis + timedelta(days=int(settings.site_check_freshness_days))
    ).isoformat()


def oldest_checked_at_for_run(run: Any) -> str | None:
    return _run_rollup_checked_at(_as_dict(getattr(run, "adapter_results", None)))


def headline_takeaway(run: Any) -> str | None:
    """Deterministic one-liner for list rows (§8.2)."""
    text = _deterministic_bottom_line_text(run)
    return text or None


# ── Public entry points ────────────────────────────────────────────────────


def build_cards(run: Any, settings: Any) -> dict[str, Any]:
    """The §10.1 CardsPayload v2 for a run that qualifies for cards.
    Deterministic by default; the §11 narrative overlay fills only
    Card.narrative and (when the call succeeded) the AI bottom line."""
    del settings  # freshness copy is the API layer's; kept for the contract
    results = _as_dict(getattr(run, "adapter_results", None))
    derived = _as_dict(getattr(run, "derived", None))
    narratives = _as_dict(derived.get("narratives"))
    try:
        retry_ids = {
            str(row.get("source_id"))
            for row in retryable_failed_sources_for_run(run)
        }
    except Exception:  # cards must render even if retry classification trips
        retry_ids = set()

    groups: list[dict[str, Any]] = []
    for key, title, slots in _GROUP_MAP:
        cards = []
        for slot in slots:
            if slot == "historical_aerials":
                # The one derived-backed slot (§19) — it has no adapter row.
                card = _historical_aerials_card(run, derived)
            else:
                card = _build_card(slot, run, results, narratives, retry_ids)
            if card is not None:
                cards.append(card)
        groups.append({"key": key, "title": title, "cards": cards})

    anchor_note = (
        ANCHOR_NOTE
        if str(getattr(run, "anchor_quality", "") or "") == "provisional"
        else None
    )
    return {
        "groups": groups,
        "bottom_line": _bottom_line(run, derived),
        "manual_checks": _manual_checks(derived),
        "anchor_note": anchor_note,
    }


def cards_or_none(run: Any, settings: Any) -> dict[str, Any] | None:
    """The §8.3 two-class rule (single-valued): cards exist for (a) any run
    with staged source results — completed, cancelled-partial,
    failed-partial — and (b) every terminal cancelled run even with ZERO
    staged results (the all-not_checked scaffold). Every other run — queued,
    running, or failed with nothing staged — gets None."""
    status = str(getattr(run, "status", "") or "")
    staged = bool(_as_dict(getattr(run, "adapter_results", None)))
    if status == "cancelled":
        return build_cards(run, settings)
    if status in ("completed", "failed") and staged:
        return build_cards(run, settings)
    return None


__all__ = [
    "ANCHOR_NOTE",
    "CARDS_SCHEMA_VERSION",
    "build_cards",
    "cards_or_none",
    "freshness_band",
    "headline_takeaway",
    "oldest_checked_at_for_run",
    "refresh_after_iso",
]
