"""county_source_registry — per-jurisdiction source map for Travis County (v1).

Why this exists
---------------
PropDesk must never answer "unknown" for a parcel when an OFFICIAL source
exists but simply isn't wired as a queryable REST layer. Before this module the
flow was:

    jurisdiction_candidates → hard-coded per-adapter city list → unknown/manual

which buried real public sources (e.g. City of Manor's Future Land Use Map PDF
and parcel-specific rezoning staff reports) behind a generic "manual upload"
step. The Travis-coverage directive (Admin, 2026-05-31) replaces that with:

    parcel/point → jurisdiction resolver → normalized city key
                 → THIS registry → per-card source mode → adapter/source

so each card (zoning, future land use, permits, plats, entitlement) routes to
the correct city/county source and reports an honest, nuanced status instead of
collapsing every un-wired-but-published source into "unknown".

Honesty rules (load-bearing)
----------------------------
* A `SourceMode` describes HOW a card's answer can be obtained for a city, not
  whether we happened to wire an adapter. `OFFICIAL_PDF_OR_MAP` /
  `OFFICIAL_STAFF_REPORT` / `OFFICIAL_WEB_PORTAL` are official sources — they
  must surface with the source link, never as UNKNOWN.
* `UNKNOWN_NOT_AUDITED` is reserved for cities/cards we have NOT yet verified.
  It is explicit ("we have not checked"), not a silent empty payload, and is the
  honest default for the long tail of small Travis cities/villages.
* Every REST entry here is one that is ALREADY wired + verified in a shipping
  adapter (austin_zoning / future_land_use / entitlement_pipeline) OR was
  live-probed during this task. We do not assert REST availability we have not
  seen respond.

Scope: Travis County only in v1. The shape generalizes to other counties; the
`COUNTY_SOURCE_REGISTRY` dict is keyed by county then normalized city key.
Spec: docs/specs/travis_county_coverage.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SourceMode(str, Enum):
    """How a given card's answer is obtainable for a given jurisdiction.

    Ordered loosely best→worst. The first five are "covered" (an answer exists);
    the last three are honest non-answers. Maps to the directive's status list:
      COVERED_PROGRAMMATIC          -> REST_GIS
      COVERED_OFFICIAL_PDF_OR_MAP   -> OFFICIAL_PDF_OR_MAP
      COVERED_OFFICIAL_STAFF_REPORT -> OFFICIAL_STAFF_REPORT
      COVERED_OFFICIAL_WEB_PORTAL   -> OFFICIAL_WEB_PORTAL
      MANUAL_EXTRACTION_REQUIRED    -> MANUAL_EXTRACTION
      NOT_PUBLISHED_BY_JURISDICTION -> NOT_PUBLISHED
      SOURCE_ERROR                  -> (runtime only — set by the resolver/adapter)
      UNKNOWN (after all sources)   -> UNKNOWN_NOT_AUDITED
    """

    REST_GIS = "rest_gis"
    OFFICIAL_PDF_OR_MAP = "official_pdf_or_map"
    OFFICIAL_STAFF_REPORT = "official_staff_report"
    OFFICIAL_WEB_PORTAL = "official_web_portal"
    HTML_TABLE = "html_table"
    MANUAL_EXTRACTION = "manual_extraction"
    NOT_PUBLISHED = "not_published"
    UNKNOWN_NOT_AUDITED = "unknown_not_audited"


# Source modes that represent a real, citable official answer (vs an honest
# "we can't answer this card for this city yet").
COVERED_MODES: frozenset[SourceMode] = frozenset({
    SourceMode.REST_GIS,
    SourceMode.OFFICIAL_PDF_OR_MAP,
    SourceMode.OFFICIAL_STAFF_REPORT,
    SourceMode.OFFICIAL_WEB_PORTAL,
    SourceMode.HTML_TABLE,
})

CARDS: tuple[str, ...] = (
    "zoning",
    "future_land_use",
    "permits",
    "plats",
    "entitlement",
)


@dataclass(frozen=True)
class CardSource:
    """One card's source for one jurisdiction."""

    mode: SourceMode
    # Official URL the broker (or the document-read path) should use. For
    # REST_GIS this is the query endpoint; for PDF/map/staff-report it is the
    # official page or document.
    url: str | None = None
    # True only when a shipping adapter already auto-queries this source.
    wired: bool = False
    note: str = ""


@dataclass(frozen=True)
class JurisdictionSources:
    """Per-card source map for one jurisdiction, plus its official portals."""

    display_name: str
    zoning: CardSource
    future_land_use: CardSource
    permits: CardSource
    plats: CardSource
    entitlement: CardSource
    # Official city landing pages used in broker-facing next-step copy.
    zoning_portal_url: str | None = None
    flu_map_url: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def card(self, card: str) -> CardSource:
        return getattr(self, card)


def _city_key(name: str | None) -> str:
    """Normalize a jurisdiction name to a registry key.

    Strips 'City of ' / 'Village of ' prefixes and ' ETJ' suffixes, lowercases,
    collapses spaces to underscores. 'City of Manor' -> 'manor';
    'Village of The Hills' -> 'the_hills'; 'Manor ETJ' -> 'manor'.
    """
    s = (name or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("development agreement,"):
        s = s.split(",", 1)[1].strip()
        low = s.lower()
    for prefix in ("city of ", "village of ", "town of "):
        if low.startswith(prefix):
            s = s[len(prefix):]
            low = s.lower()
            break
    if low.endswith(" etj"):
        s = s[: -len(" etj")]
    s = s.replace(",", " ")
    return "_".join(part for part in s.strip().lower().split() if part)


# ── REST endpoints already wired + verified in shipping adapters ─────────────
# These mirror austin_zoning._WIRED_CITIES / future_land_use._LAYERS_BY_JURISDICTION
# / entitlement_pipeline. Kept as references (not re-imported) so this registry
# documents WHAT is wired without creating an import cycle.

_AUSTIN_ZONING_REST = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "Publish_Zoning_AGOL/FeatureServer/0/query"
)
_AUSTIN_FLU_REST = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/services/"
    "PLANNINGCADASTRE_future_land_use_map/FeatureServer/0/query"
)
_PFLUG_ZONING_REST = (
    "https://maps.pflugervilletx.gov/arcgis/rest/services/Planning/"
    "Zoning_Districts/MapServer/0/query"
)
_PFLUG_FLU_REST = (
    "https://maps.pflugervilletx.gov/arcgis/rest/services/Planning/"
    "Future_Land_Use/FeatureServer/0/query"
)
_CEDAR_PARK_ZONING_REST = (
    "https://gisrest.cedarparktexas.gov/cpgis/rest/services/Planning/"
    "Zoning/MapServer/3/query"
)
_CEDAR_PARK_FLU_REST = (
    "https://gisrest.cedarparktexas.gov/cpgis/rest/services/Planning/"
    "FutureLandUse/MapServer/0/query"
)

# Official Manor sources (verified during this task, 2026-05-31):
#   - Manor's GIS org (services5.arcgis.com/L9cGEA18yPzG7Xak) Zoning/Parcels
#     layers return HTTP 499 "Token Required" — NOT publicly queryable.
#   - Manor publishes the Future Land Use Map + zoning map as official PDFs and
#     parcel-specific designations in rezoning/SUP staff reports + annexation
#     ordinances (the path ChatGPT used to read C-2 / Commercial Corridor).
_MANOR_ZONING_PORTAL = "https://www.manortx.gov/252/Zoning"
_MANOR_MAPS_PORTAL = "https://www.manortx.gov/266/Maps-Exhibits"
_MANOR_CODE_ZONING = (
    "https://library.municode.com/tx/manor/codes/code_of_ordinances"
    "?nodeId=COOR_CH14ZO"
)
_MANOR_BUILDING_PERMITS = "https://www.manortx.gov/187/Building-Permits"
_MANOR_GOVWELL = "https://app.govwell.com/manor"
_MANOR_MEETINGS = (
    "https://meetings.municode.com/PublishPage/index?"
    "cid=MANORTX&ppid=6e8791e4-3a6b-49c1-8f3f-03e061bae9d7&p=1"
)
_TRAVIS_COUNTY_DEVELOPMENT = (
    "https://www.traviscountytx.gov/tnr/development-services"
)
_TRAVIS_COUNTY_CLERK_OPR = "https://www.tccsearch.org/"


def _manual(url: str | None = None, note: str = "") -> CardSource:
    return CardSource(mode=SourceMode.MANUAL_EXTRACTION, url=url, note=note)


def _official_portal(url: str, note: str = "") -> CardSource:
    return CardSource(mode=SourceMode.OFFICIAL_WEB_PORTAL, url=url, note=note)


def _official_pdf(url: str, note: str = "") -> CardSource:
    return CardSource(mode=SourceMode.OFFICIAL_PDF_OR_MAP, url=url, note=note)


def _staff_report(url: str, note: str = "") -> CardSource:
    return CardSource(mode=SourceMode.OFFICIAL_STAFF_REPORT, url=url, note=note)


def _not_published(url: str | None = None, note: str = "") -> CardSource:
    return CardSource(mode=SourceMode.NOT_PUBLISHED, url=url, note=note)


def _unaudited(url: str | None = None) -> CardSource:
    return CardSource(
        mode=SourceMode.UNKNOWN_NOT_AUDITED,
        url=url,
        note="Source not yet audited for this jurisdiction.",
    )


# ── Travis County registry ───────────────────────────────────────────────────
# Every jurisdiction the Travis municipal-jurisdictions service returns (25 city
# limits + matching ETJs), enumerated live on 2026-05-31. Cities with verified
# wired REST get REST_GIS(wired=True); Manor gets its verified official-PDF /
# staff-report modes; the long tail is honestly UNKNOWN_NOT_AUDITED with the
# city portal where known (so the card shows "official source, not yet audited"
# rather than a false unknown/unincorporated).

_TRAVIS: dict[str, JurisdictionSources] = {
    "austin": JurisdictionSources(
        display_name="Austin",
        zoning=CardSource(SourceMode.REST_GIS, _AUSTIN_ZONING_REST, wired=True),
        future_land_use=CardSource(SourceMode.REST_GIS, _AUSTIN_FLU_REST, wired=True),
        permits=_official_portal(
            "https://www.austintexas.gov/page/records-and-research",
            "Austin Development Services records page links AB+C public search "
            "for permits, site plans, variances, maps, and related documents.",
        ),
        plats=_official_portal(
            "https://www.austintexas.gov/siteplans",
            "Austin site-plan and land-use applications; recorded plat image "
            "authority remains Travis County Clerk OPR.",
        ),
        entitlement=CardSource(
            SourceMode.HTML_TABLE,
            "https://data.austintexas.gov/",
            wired=True,
            note="Austin Socrata case feeds (entitlement_pipeline).",
        ),
        zoning_portal_url="https://www.austintexas.gov/development-services",
    ),
    "pflugerville": JurisdictionSources(
        display_name="Pflugerville",
        zoning=CardSource(SourceMode.REST_GIS, _PFLUG_ZONING_REST, wired=True),
        future_land_use=CardSource(SourceMode.REST_GIS, _PFLUG_FLU_REST, wired=True),
        permits=_official_portal(
            "https://pflugervilletx.gov/202/Development-Services-Center",
            "Planning and Development Services entry point for permit and "
            "development applications.",
        ),
        plats=_staff_report(
            "https://www.pflugervilletx.gov/236/Planning-Department",
            "Current Planning / site development / subdivision resources; "
            "recorded plat image authority remains Travis County Clerk OPR.",
        ),
        entitlement=_staff_report(
            "https://www.pflugervilletx.gov/236/Planning-Department",
            "Planning Department page for current planning, site development, "
            "and agenda-driven entitlement actions.",
        ),
        zoning_portal_url=(
            "https://pflugervilletx.gov/202/Development-Services-Center"
        ),
    ),
    "cedar_park": JurisdictionSources(
        display_name="Cedar Park",
        zoning=CardSource(SourceMode.REST_GIS, _CEDAR_PARK_ZONING_REST, wired=True),
        future_land_use=CardSource(SourceMode.REST_GIS, _CEDAR_PARK_FLU_REST, wired=True),
        permits=CardSource(
            SourceMode.REST_GIS,
            "https://gisrest.cedarparktexas.gov/cpgis/rest/services/",
            wired=True,
            note="Cedar Park MPN_Permits (entitlement_pipeline).",
        ),
        plats=_staff_report(
            "https://www.cedarparktexas.gov/591/Planning-Zoning-Agendas",
            "P&Z packets include plat items; recorded plat image authority "
            "remains county clerk OPR.",
        ),
        entitlement=CardSource(
            SourceMode.REST_GIS,
            "https://www.cedarparktexas.gov/591/Planning-Zoning-Agendas",
            wired=True,
        ),
        zoning_portal_url="https://www.cedarparktexas.gov/680/Find-View",
    ),
    # ── Manor: jurisdiction REST works; zoning/FLU REST are token-gated, so the
    # official answer lives in PDFs + parcel staff reports (verified 2026-05-31).
    "manor": JurisdictionSources(
        display_name="Manor",
        zoning=CardSource(
            SourceMode.OFFICIAL_PDF_OR_MAP,
            _MANOR_ZONING_PORTAL,
            note=(
                "Manor GIS zoning layer is token-gated (HTTP 499). Official "
                "zoning map PDF + Municode Ch.14 zoning code are public; "
                "parcel zoning (e.g. C-2 Medium Commercial) is established in "
                "rezoning/SUP staff reports + annexation ordinances."
            ),
        ),
        future_land_use=CardSource(
            SourceMode.OFFICIAL_PDF_OR_MAP,
            _MANOR_MAPS_PORTAL,
            note=(
                "Manor Future Land Use Map published as official PDF under "
                "Maps & Exhibits; parcel designation (e.g. Commercial "
                "Corridor) also stated in rezoning staff reports."
            ),
        ),
        permits=CardSource(
            SourceMode.OFFICIAL_WEB_PORTAL,
            _MANOR_GOVWELL,
            note=(
                "GovWell is the official intake portal for current Manor "
                "building permits; planning/zoning applications are exposed "
                "as a GovWell public record type and cross-checked with packets."
            ),
        ),
        plats=CardSource(
            SourceMode.OFFICIAL_STAFF_REPORT,
            _MANOR_MEETINGS,
            note=(
                "Recorded plats surface in Manor P&Z final-plat agenda items "
                "(e.g. 'FM 1100 Manor 12 Plat') + Travis County Clerk OPR."
            ),
        ),
        entitlement=CardSource(
            SourceMode.OFFICIAL_STAFF_REPORT,
            _MANOR_MEETINGS,
            note=(
                "Recent P&Z agenda packets plus GovWell Planning & Zoning "
                "Applications are the official Manor entitlement entry points."
            ),
        ),
        zoning_portal_url=_MANOR_ZONING_PORTAL,
        flu_map_url=_MANOR_MAPS_PORTAL,
    ),
}

# Audited long-tail: every name the Travis County Municipal Jurisdictions layer
# can return is now explicit. These sources are not all programmatic feeds, but
# they are official entry points (planning pages, permitting portals, agenda
# packets, or a deliberate NOT_PUBLISHED finding) rather than unknown/not-audited
# placeholders. ETJ-only jurisdictions (Buda, Coupland, Dripping Springs, Hutto,
# Taylor) are included because the resolver can emit them for Travis parcels.


def _travis_official_sources(
    *,
    display_name: str,
    portal: str,
    zoning_url: str | None = None,
    flu_url: str | None = None,
    permits_url: str | None = None,
    plats_url: str | None = None,
    entitlement_url: str | None = None,
    aliases: tuple[str, ...] = (),
) -> JurisdictionSources:
    zoning_source = zoning_url or portal
    flu_source = flu_url or zoning_source
    permit_source = permits_url or portal
    entitlement_source = entitlement_url or portal
    plat_source = plats_url or entitlement_source
    return JurisdictionSources(
        display_name=display_name,
        zoning=_official_portal(
            zoning_source,
            f"Official {display_name} zoning/planning source; manual "
            "extraction required until a structured layer is wired.",
        ),
        future_land_use=_official_portal(
            flu_source,
            f"Official {display_name} planning / future-land-use entry point; "
            "read the adopted map or plan document before relying on it.",
        ),
        permits=_official_portal(
            permit_source,
            f"Official {display_name} permit or development-services portal.",
        ),
        plats=_staff_report(
            plat_source,
            f"Official {display_name} plat/subdivision agenda or planning "
            "entry point; recorded plat images remain in Travis County Clerk OPR.",
        ),
        entitlement=_staff_report(
            entitlement_source,
            f"Official {display_name} entitlement entry point for rezonings, "
            "site plans, subdivisions, variances, SUPs, or agenda packets.",
        ),
        zoning_portal_url=zoning_source,
        flu_map_url=flu_url,
        aliases=aliases,
    )


_TRAVIS_AUDITED: dict[str, JurisdictionSources] = {
    "bee_cave": _travis_official_sources(
        display_name="Bee Cave",
        portal="https://www.beecavetexas.gov/government/departments/"
        "planning_development",
        entitlement_url="https://www.beecavetexas.gov/government/agendas_minutes",
    ),
    "briarcliff": _travis_official_sources(
        display_name="Briarcliff",
        portal="https://www.briarclifftx.com/home/",
        permits_url="https://www.briarclifftx.com/building-permits-1/",
        entitlement_url="https://www.briarclifftx.com/agendas-minutes/",
    ),
    "buda": _travis_official_sources(
        display_name="Buda",
        portal="https://www.budatx.gov/112/Planning-Engineering",
        permits_url="https://www.budatx.gov/175/Building-Permits",
        entitlement_url="https://www.budatx.gov/AgendaCenter",
    ),
    "coupland": _travis_official_sources(
        display_name="Coupland",
        portal="https://www.cityofcouplandtx.us/",
        entitlement_url="https://www.cityofcouplandtx.us/meetings",
    ),
    "creedmoor": _travis_official_sources(
        display_name="Creedmoor",
        portal="https://www.cityofcreedmoortx.gov/",
        zoning_url="https://www.cityofcreedmoortx.gov/administration/page/zoning",
        entitlement_url="https://www.cityofcreedmoortx.gov/meetings",
    ),
    "dripping_springs": _travis_official_sources(
        display_name="Dripping Springs",
        portal="https://www.cityofdrippingsprings.com/page/planning",
        permits_url="https://www.cityofdrippingsprings.com/page/building.home",
        entitlement_url="https://www.cityofdrippingsprings.com/page/cs.agendas",
    ),
    "elgin": _travis_official_sources(
        display_name="Elgin",
        portal="https://www.elgintexas.gov/183/Planning-Development",
        zoning_url="https://www.elgintexas.gov/909/Planning-Zoning-Division",
        flu_url="https://www.elgintexas.gov/936/Maps",
        entitlement_url="https://www.elgintexas.gov/AgendaCenter",
    ),
    "hutto": _travis_official_sources(
        display_name="Hutto",
        portal="https://www.huttotx.gov/166/Development-Services",
        zoning_url="https://www.huttotx.gov/310/Zoning-Information",
        entitlement_url="https://www.huttotx.gov/AgendaCenter",
    ),
    "jonestown": _travis_official_sources(
        display_name="Jonestown",
        portal="https://www.jonestowntx.gov/pages/development-services",
        entitlement_url="https://www.jonestowntx.gov/pages/agendas-minutes",
    ),
    "lago_vista": _travis_official_sources(
        display_name="Lago Vista",
        portal="https://www.lagovistatexas.gov/172/Planning-Zoning",
        permits_url="https://www.lagovistatexas.gov/153/Building-Permits",
        entitlement_url="https://www.lagovistatexas.gov/AgendaCenter",
    ),
    "lakeway": _travis_official_sources(
        display_name="Lakeway",
        portal="https://www.lakeway-tx.gov/67/Building-Development",
        entitlement_url="https://www.lakeway-tx.gov/Archive.aspx",
    ),
    "leander": _travis_official_sources(
        display_name="Leander",
        portal="https://www.leandertx.gov/631/Planning-Department",
        permits_url="https://www.leandertx.gov/338/Development-Hub",
        entitlement_url="https://www.leandertx.gov/AgendaCenter",
    ),
    "mustang_ridge": _travis_official_sources(
        display_name="Mustang Ridge",
        portal="https://www.mustangridgetx.gov/",
        permits_url="https://www.mustangridgetx.gov/building-permits",
        entitlement_url="https://www.mustangridgetx.gov/meetings",
    ),
    "point_venture": _travis_official_sources(
        display_name="Point Venture",
        portal="https://www.vopv.org/",
        permits_url="https://www.vopv.org/faq",
        entitlement_url="https://www.vopv.org/agenda-minutes",
    ),
    "rollingwood": _travis_official_sources(
        display_name="Rollingwood",
        portal="https://www.rollingwoodtx.gov/planning-development",
        permits_url="https://www.rollingwoodtx.gov/planning-development/"
        "page/permitting-process-residential-and-commercial-building",
        entitlement_url="https://www.rollingwoodtx.gov/bc-pzc",
    ),
    "round_rock": _travis_official_sources(
        display_name="Round Rock",
        portal="https://www.roundrocktexas.gov/city-departments/"
        "planning-and-development-services/",
        entitlement_url="https://www.roundrocktexas.gov/city-departments/"
        "planning-and-development-services/zoning/",
    ),
    "san_leanna": _travis_official_sources(
        display_name="San Leanna",
        portal="https://www.sanleannatx.com/",
        entitlement_url="https://www.sanleannatx.com/meeting-agendas",
    ),
    "sunset_valley": _travis_official_sources(
        display_name="Sunset Valley",
        portal="https://www.sunsetvalley.org/departments/planning-development",
        permits_url="https://sunsetvalleytx.govbuilt.com/advancedforms",
        entitlement_url="https://www.sunsetvalley.org/AgendaCenter",
    ),
    "taylor": _travis_official_sources(
        display_name="Taylor",
        portal="https://www.taylortx.gov/198/Planning-Development",
        flu_url="https://www.taylortx.gov/197/Comprehensive-Plan",
        entitlement_url="https://www.taylortx.gov/AgendaCenter",
    ),
    "the_hills": _travis_official_sources(
        display_name="The Hills",
        portal="https://www.thehillstx.gov/92/Building-Planning-Zoning",
        flu_url="https://www.thehillstx.gov/2294/Comprehensive-Plan",
        entitlement_url="https://www.thehillstx.gov/AgendaCenter",
    ),
    "volente": _travis_official_sources(
        display_name="Volente",
        portal="https://www.villageofvolente-tx.gov/",
        entitlement_url="https://www.villageofvolente-tx.gov/index.asp?"
        "SEC=EDBE540C-C114-45AB-BD83-3DCEBC38C178",
    ),
    "west_lake_hills": _travis_official_sources(
        display_name="West Lake Hills",
        portal="https://www.westlakehills.gov/95/Building-Development",
        entitlement_url="https://www.westlakehills.gov/AgendaCenter",
    ),
}

for _key, _sources in _TRAVIS_AUDITED.items():
    if _key not in _TRAVIS:
        _TRAVIS[_key] = _sources

_TRAVIS["webberville"] = JurisdictionSources(
    display_name="Webberville",
    zoning=_not_published(
        _TRAVIS_COUNTY_DEVELOPMENT,
        "No official Webberville planning/zoning web portal was found in the "
        "2026-05-31 Travis audit; verify with the village office and Travis "
        "County Development Services.",
    ),
    future_land_use=_not_published(
        _TRAVIS_COUNTY_DEVELOPMENT,
        "No official Webberville future-land-use map source was found in the "
        "2026-05-31 Travis audit.",
    ),
    permits=_not_published(
        _TRAVIS_COUNTY_DEVELOPMENT,
        "No official Webberville permit portal was found; use Travis County "
        "Development Services where county authority applies and verify with "
        "the village office.",
    ),
    plats=_official_portal(
        _TRAVIS_COUNTY_CLERK_OPR,
        "Recorded plat authority is Travis County Clerk OPR; also verify any "
        "city subdivision action directly with Webberville.",
    ),
    entitlement=_not_published(
        _TRAVIS_COUNTY_DEVELOPMENT,
        "No official Webberville entitlement portal or agenda archive was found "
        "in the 2026-05-31 audit.",
    ),
    zoning_portal_url=_TRAVIS_COUNTY_DEVELOPMENT,
)


# ── Williamson County registry ───────────────────────────────────────────────
# Williamson is the county where coverage already works end-to-end (Admin's
# "all cards" benchmark). Encoding it here locks that working state as a
# regression guard and lets the coverage report cover both counties. REST/curated
# entries mirror what austin_zoning._WIRED_CITIES, future_land_use, the
# zoning_standards/curated/*.yaml set, and entitlement_pipeline actually wire.
# Jurisdictions enumerated live from gis.wilco.org on 2026-05-31.

_GEORGETOWN_ZONING_REST = (
    "https://gis.georgetowntexas.gov/arcgis/rest/services/SystemsEngineering/"
    "DevelopmentPipelineBase_External_WebMap/MapServer/0/query"
)
_GEORGETOWN_FLU_REST = (
    "https://gis.georgetowntexas.gov/arcgis/rest/services/PublicWebMaps/"
    "FutureLandUse_WebMap/MapServer/655/query"
)
_HUTTO_ZONING_REST = (
    "https://services.arcgis.com/YZhxlqU7ABWQBGTG/arcgis/rest/services/"
    "Hutto_Zoning_Districts/FeatureServer/0/query"
)
_HUTTO_FLU_REST = (
    "https://services.arcgis.com/YZhxlqU7ABWQBGTG/arcgis/rest/services/"
    "Future_Land_Use/FeatureServer/0/query"
)
_ROUND_ROCK_ZONING_REST = (
    "https://maps.roundrocktexas.gov/arcgis/rest/services/Planning/"
    "Planning_Multi/MapServer/12/query"
)
_ROUND_ROCK_FLU_REST = (
    "https://maps.roundrocktexas.gov/arcgis/rest/services/Planning/"
    "Planning_Multi/MapServer/11/query"
)
_LEANDER_ZONING_REST = (
    "https://services1.arcgis.com/L0MLvN0Ay0iEjnCT/arcgis/rest/services/"
    "Leander_Current_Zoning/FeatureServer/3/query"
)
_TAYLOR_ZONING_REST = (
    "https://services7.arcgis.com/SQVxkeGOcRYhZqOD/arcgis/rest/services/"
    "Zoning_011720/FeatureServer/46/query"
)


def _rest(url: str, note: str = "") -> CardSource:
    return CardSource(SourceMode.REST_GIS, url, wired=True, note=note)


_WILLIAMSON: dict[str, JurisdictionSources] = {
    "cedar_park": JurisdictionSources(
        display_name="Cedar Park",
        zoning=_rest(_CEDAR_PARK_ZONING_REST),
        future_land_use=_rest(_CEDAR_PARK_FLU_REST),
        permits=_rest(
            "https://gisrest.cedarparktexas.gov/cpgis/rest/services/",
            "Cedar Park MPN_Permits (entitlement_pipeline).",
        ),
        plats=_manual("https://www.cedarparktexas.gov/680/Find-View"),
        entitlement=_rest("https://www.cedarparktexas.gov/591/Planning-Zoning-Agendas"),
        zoning_portal_url="https://www.cedarparktexas.gov/680/Find-View",
    ),
    "georgetown": JurisdictionSources(
        display_name="Georgetown",
        zoning=_rest(_GEORGETOWN_ZONING_REST),
        future_land_use=_rest(_GEORGETOWN_FLU_REST),
        permits=_manual("https://gus.georgetown.org/"),
        plats=_manual("https://gus.georgetown.org/"),
        entitlement=_manual("https://gus.georgetown.org/"),
        zoning_portal_url="https://gus.georgetown.org/",
    ),
    "hutto": JurisdictionSources(
        display_name="Hutto",
        zoning=_rest(_HUTTO_ZONING_REST),
        future_land_use=_rest(_HUTTO_FLU_REST),
        permits=_manual("https://www.huttotx.gov/310/Zoning-Information"),
        plats=_manual("https://www.huttotx.gov/310/Zoning-Information"),
        entitlement=_manual("https://www.huttotx.gov/310/Zoning-Information"),
        zoning_portal_url="https://www.huttotx.gov/310/Zoning-Information",
    ),
    "round_rock": JurisdictionSources(
        display_name="Round Rock",
        zoning=_rest(_ROUND_ROCK_ZONING_REST),
        future_land_use=_rest(_ROUND_ROCK_FLU_REST),
        permits=_manual(
            "https://www.roundrocktexas.gov/city-departments/"
            "planning-and-development-services/",
        ),
        plats=_manual(
            "https://www.roundrocktexas.gov/city-departments/"
            "planning-and-development-services/",
        ),
        entitlement=_manual(
            "https://www.roundrocktexas.gov/city-departments/"
            "planning-and-development-services/",
        ),
        zoning_portal_url=(
            "https://www.roundrocktexas.gov/city-departments/"
            "planning-and-development-services/"
        ),
    ),
    "leander": JurisdictionSources(
        display_name="Leander",
        zoning=_rest(_LEANDER_ZONING_REST),
        future_land_use=_unaudited("https://www.leandertx.gov/planning"),
        permits=_manual("https://www.leandertx.gov/planning"),
        plats=_manual("https://www.leandertx.gov/planning"),
        entitlement=_manual("https://www.leandertx.gov/planning"),
        zoning_portal_url="https://www.leandertx.gov/planning",
    ),
    "taylor": JurisdictionSources(
        display_name="Taylor",
        zoning=_rest(_TAYLOR_ZONING_REST),
        future_land_use=_manual("https://www.taylortx.gov/197/Comprehensive-Plan"),
        permits=_manual("https://www.taylortx.gov/"),
        plats=_manual("https://www.taylortx.gov/"),
        entitlement=_manual("https://www.taylortx.gov/"),
        zoning_portal_url="https://www.taylortx.gov/",
    ),
}

# Williamson long tail (small cities the resolver can return, not yet audited).
_WILLIAMSON_UNAUDITED: dict[str, tuple[str, str | None]] = {
    "austin": ("Austin", "https://www.austintexas.gov/development-services"),
    "bartlett": ("Bartlett", None),
    "coupland": ("Coupland", None),
    "florence": ("Florence", None),
    "granger": ("Granger", None),
    "jarrell": ("Jarrell", None),
    "liberty_hill": ("Liberty Hill", "https://www.libertyhilltx.gov/"),
    "manor": ("Manor", _MANOR_ZONING_PORTAL),
    "pflugerville": ("Pflugerville", None),
    "sun_city": ("Sun City", None),
    "thorndale": ("Thorndale", None),
    "thrall": ("Thrall", None),
    "weir": ("Weir", None),
}
for _key, (_disp, _portal) in _WILLIAMSON_UNAUDITED.items():
    if _key in _WILLIAMSON:
        continue
    _WILLIAMSON[_key] = JurisdictionSources(
        display_name=_disp,
        zoning=_unaudited(_portal),
        future_land_use=_unaudited(_portal),
        permits=_unaudited(_portal),
        plats=_unaudited(_portal),
        entitlement=_unaudited(_portal),
        zoning_portal_url=_portal,
    )


COUNTY_SOURCE_REGISTRY: dict[str, dict[str, JurisdictionSources]] = {
    "travis": _TRAVIS,
    "williamson": _WILLIAMSON,
}


# ── Public API ───────────────────────────────────────────────────────────────


def get_jurisdiction_sources(
    county: str | None, jurisdiction_name: str | None,
) -> JurisdictionSources | None:
    """Look up the per-card source map for a (county, jurisdiction) pair.

    `jurisdiction_name` may be any form the resolver emits ("City of Manor",
    "Manor", "Manor ETJ") — it is normalized to the registry key. Returns None
    when the county isn't registered or the city isn't listed (caller then keeps
    its existing honest fallback)."""
    county_key = (county or "").strip().lower()
    county_key = county_key.removesuffix(" county")
    county_map = COUNTY_SOURCE_REGISTRY.get(county_key)
    if not county_map:
        return None
    key = _city_key(jurisdiction_name)
    direct = county_map.get(key)
    if direct is not None:
        return direct
    for sources in county_map.values():
        if key in {_city_key(alias) for alias in sources.aliases}:
            return sources
    return None


def get_card_source(
    county: str | None, jurisdiction_name: str | None, card: str,
) -> CardSource | None:
    sources = get_jurisdiction_sources(county, jurisdiction_name)
    if sources is None or card not in CARDS:
        return None
    return sources.card(card)


def coverage_report(county: str = "travis") -> list[dict[str, str]]:
    """Flat per-jurisdiction x per-card coverage report for `county`.

    One row per (jurisdiction, card) with its source mode + url. Drives the
    directive's deliverable #4 (a coverage report listing every city/ETJ and
    whether each card is programmatic / official-PDF / staff-report / not
    published / not audited). Pure data — no network, no AI."""
    county_map = COUNTY_SOURCE_REGISTRY.get(county.strip().lower(), {})
    rows: list[dict[str, str]] = []
    for key in sorted(county_map):
        src = county_map[key]
        for card in CARDS:
            cs = src.card(card)
            rows.append({
                "county": county.strip().lower(),
                "jurisdiction_key": key,
                "jurisdiction": src.display_name,
                "card": card,
                "source_mode": cs.mode.value,
                "covered": str(cs.mode in COVERED_MODES).lower(),
                "wired": str(cs.wired).lower(),
                "url": cs.url or "",
            })
    return rows


__all__ = [
    "CARDS",
    "COUNTY_SOURCE_REGISTRY",
    "COVERED_MODES",
    "CardSource",
    "JurisdictionSources",
    "SourceMode",
    "coverage_report",
    "get_card_source",
    "get_jurisdiction_sources",
]
