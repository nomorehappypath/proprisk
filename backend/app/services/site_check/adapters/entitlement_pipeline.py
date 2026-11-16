"""entitlement_pipeline adapter — Austin Socrata case feeds + per-city portals.

Ported verbatim from legacy_services/core/public_intel_adapters/
entitlement_pipeline.py (May 2026). Identity / contract import
paths adjusted; SOURCE_ID matches registry; coverage_status
field added.

Austin only programmatic — Socrata zoning cases (edir-dcnf) +
site plan cases (mavg-96ck). Other cities get HTML_OR_PDF_ONLY
with portal URL + manual lookup steps. Always SCREENED — the
public feed lags real council action by days/weeks.

Requires `identity.jurisdiction_candidates` populated by the
orchestrator (after austin_zoning resolves).
"""

from __future__ import annotations

import html
import logging
import math
import re
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import requests

from app.config.settings import get_settings
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
from app.services.site_check.county_source_registry import (
    SourceMode,
    get_jurisdiction_sources,
)

log = logging.getLogger("propdesk.site_check.entitlement_pipeline")

SOURCE_ID = "entitlement_pipeline"

_AUSTIN_ZONING_CASES = (
    "https://data.austintexas.gov/resource/edir-dcnf.json"
)
_AUSTIN_SITE_PLAN_CASES = (
    "https://data.austintexas.gov/resource/mavg-96ck.json"
)

_LAYERS_BY_JURISDICTION: dict[str, list[tuple[str, str, str]]] = {
    "austin": [
        ("Austin zoning cases", _AUSTIN_ZONING_CASES, "zoning_case"),
        ("Austin site plan cases", _AUSTIN_SITE_PLAN_CASES, "site_plan_case"),
    ],
}

_TIMEOUT = 25
_DEFAULT_RADIUS_M = 800

_PIPELINE_UPLOAD_TYPES = [
    "agenda packet",
    "staff report",
    "case screenshot",
    "notice letter",
]

_CASE_PORTALS: dict[str, dict[str, str]] = {
    "hutto": {
        "name": "Hutto Planning & Development",
        "url": "https://www.huttotx.gov/154/Planning-Development",
        "instructions": (
            "1) Open Hutto P&D page. 2) Click 'Current Cases' "
            "or 'Planning Commission Agendas'. 3) Search by "
            "address; download recent agenda packets within "
            "1/2 mile."
        ),
    },
    "pflugerville": {
        "name": "Pflugerville Planning Department",
        "url": "https://www.pflugervilletx.gov/city-government/departments/planning",
        "instructions": (
            "1) Open Pflugerville Planning page. 2) Click "
            "'Current Cases' or 'Agenda Center → Planning "
            "Commission'. 3) Pull packets from the last 6 "
            "months that fall within 1/2 mile of the property."
        ),
    },
    "cedar_park": {
        "name": "Cedar Park Planning & Zoning Agendas",
        "url": "https://www.cedarparktexas.gov/591/Planning-Zoning-Agendas",
        "instructions": (
            "1) Open Cedar Park P&Z page. 2) Browse recent "
            "agendas (6 months). 3) Capture cases within "
            "1/2 mile of the property."
        ),
    },
    "georgetown": {
        "name": "Georgetown Planning Department",
        "url": "https://planning.georgetown.org/",
        "instructions": (
            "1) Open Georgetown Planning page. 2) Click "
            "'Active Cases' or browse Planning Commission "
            "agendas. 3) Capture cases within 1/2 mile."
        ),
    },
    "round_rock": {
        "name": "Round Rock Planning & Development Cases",
        "url": "https://www.roundrocktexas.gov/departments/planning-and-development-services/",
        "instructions": (
            "1) Open Round Rock P&D page. 2) Use 'Active "
            "Development Cases' viewer. 3) Capture cases "
            "within 1/2 mile."
        ),
    },
    "manor": {
        "name": "Manor GovWell Planning & Zoning Applications",
        "url": "https://app.govwell.com/manor",
        "instructions": (
            "1) Open Manor GovWell. 2) Use Planning & Zoning "
            "Applications for subdivision plats, site plans, zoning, "
            "specific use permits, and variances. 3) Search the parcel "
            "address, TCAD account, and nearby street names; verify against "
            "recent P&Z agenda packets."
        ),
    },
    "buda": {
        "name": "Buda Planning Department",
        "url": "https://www.budatx.gov/318/Planning",
        "instructions": (
            "1) Open Buda Planning page. 2) Browse Planning "
            "& Zoning Commission agendas (last 6 months). "
            "3) Capture cases within 1/2 mile."
        ),
    },
    "kyle": {
        "name": "Kyle Planning Department",
        "url": "https://www.cityofkyle.com/planning",
        "instructions": (
            "1) Open Kyle Planning page. 2) Browse 'Current "
            "Projects' or Planning & Zoning agendas. "
            "3) Capture cases within 1/2 mile."
        ),
    },
    "dripping_springs": {
        "name": "Dripping Springs Planning",
        "url": "https://www.cityofdrippingsprings.com/page/planning",
        "instructions": (
            "1) Open Dripping Springs Planning page. 2) Browse "
            "P&Z Commission agendas. 3) Capture nearby cases."
        ),
    },
    "leander": {
        "name": "Leander Planning Department",
        "url": "https://www.leandertx.gov/planning",
        "instructions": (
            "1) Open Leander Planning page. 2) Browse Planning "
            "& Zoning Commission agendas. 3) Capture cases "
            "within 1/2 mile."
        ),
    },
    "taylor": {
        "name": "Taylor Planning & Development",
        "url": "https://www.taylortx.gov/198/Planning-Development",
        "instructions": (
            "1) Open Taylor P&D page. 2) Browse recent Planning "
            "& Zoning Commission agendas. 3) Capture nearby "
            "cases."
        ),
    },
    "liberty_hill": {
        "name": "Liberty Hill Planning & Development",
        "url": "https://www.libertyhilltx.gov/",
        "instructions": (
            "1) Open Liberty Hill city resources. 2) Find Planning "
            "or Development Services. 3) Search recent agendas, "
            "current projects, or application materials for nearby cases."
        ),
    },
}


_MANUAL_SOURCE_POINTERS: dict[str, list[dict[str, str]]] = {
    "cedar_park": [
        {
            "label": "Cedar Park Application and Permitting",
            "url": "https://www.cedarparktexas.gov/984/Application-and-Permitting",
            "note": (
                "Planning and zoning applications, site development permits, "
                "subdivision applications, and permit intake."
            ),
        },
        {
            "label": "Cedar Park Atlas / Find & View",
            "url": "https://www.cedarparktexas.gov/680/Find-View",
            "note": (
                "Interactive maps and Atlas links for zoning, permitting, "
                "planning, and property context."
            ),
        },
        {
            "label": "Cedar Park Planning & Zoning Agendas",
            "url": "https://www.cedarparktexas.gov/591/Planning-Zoning-Agendas",
            "note": "Agendas, packets, and minutes for P&Z items.",
        },
        {
            "label": "Cedar Park Public Notices",
            "url": "https://www.cedarparktexas.gov/507/Public-Notices",
            "note": "Current City Council and P&Z hearing notices.",
        },
        {
            "label": "Cedar Park official zoning GIS layer",
            "url": "https://gisrest.cedarparktexas.gov/cpgis/rest/services/Planning/Zoning/MapServer/3",
            "note": "Official ArcGIS zoning district layer.",
        },
    ],
    "georgetown": [
        {
            "label": "Georgetown Planning Department",
            "url": "https://planning.georgetown.org/",
            "note": "Planning applications, development manual, and cases.",
        },
        {
            "label": "Georgetown GIS interactive maps / rezoning layers",
            "url": "https://maps.georgetown.org/",
            "note": "Interactive planning, zoning, and rezoning map resources.",
        },
    ],
    "manor": [
        {
            "label": "Manor GovWell Planning & Zoning Applications",
            "url": "https://app.govwell.com/manor",
            "note": (
                "Current GovWell intake for subdivision plats, site plans, "
                "zoning, SUPs, and variances."
            ),
        },
        {
            "label": "Manor P&Z agendas and packets",
            "url": (
                "https://meetings.municode.com/PublishPage/index?"
                "cid=MANORTX&ppid=6e8791e4-3a6b-49c1-8f3f-03e061bae9d7&p=1"
            ),
            "note": (
                "Public P&Z agenda packets with rezoning, plat, SUP, "
                "site-plan, and variance items."
            ),
        },
        {
            "label": "Manor Building Permits / GovWell notice",
            "url": "https://www.manortx.gov/187/Building-Permits",
            "note": (
                "Official city page states Building Permit and Planning & "
                "Zoning applications moved to GovWell effective May 27, 2026."
            ),
        },
    ],
    "unincorporated_williamson": [
        {
            "label": "Williamson County Development Services & Drainage",
            "url": "https://www.wilcotx.gov/639/Development-Services-Drainage",
            "note": (
                "County subdivision, drainage, and land-development authority "
                "for unincorporated parcels."
            ),
        },
        {
            "label": "Williamson County Commissioners Court agendas",
            "url": "https://www.wilcotx.gov/AgendaCenter/Commissioners-Court-8/",
            "note": (
                "Agenda packets and subdivision-related county approvals where "
                "relevant."
            ),
        },
    ],
    "unincorporated_travis": [
        {
            "label": "Travis County Development Services",
            "url": "https://www.traviscountytx.gov/tnr/development-services",
            "note": "Development outside corporate city limits, including subdivision and floodplain review.",
        },
        {
            "label": "Travis County Permitting Center",
            "url": "https://www.traviscountytx.gov/tnr/development-services/permitting-center",
            "note": "County permitting intake and permit guidance.",
        },
    ],
    "unincorporated_hays": [
        {
            "label": "Hays County Development Services",
            "url": "https://www.hayscountytx.gov/development-services",
            "note": "County development and permitting resources.",
        },
        {
            "label": "Hays County Planning",
            "url": "https://www.hayscountytx.gov/county-planning",
            "note": "Plat reviews, land subdivisions, and county planning.",
        },
        {
            "label": "Hays County GIS Development Services",
            "url": "https://hays-county-haysgis.hub.arcgis.com/pages/development-services",
            "note": "GIS resources for county development services.",
        },
    ],
    "unincorporated_bastrop": [
        {
            "label": "Bastrop County Development Services",
            "url": "https://www.bastropcounty.gov/page/ds.home",
            "note": "County development services and permitting.",
        },
        {
            "label": "Bastrop County Land Divisions / Planning & Platting",
            "url": "https://www.bastropcounty.gov/page/dsen.land_divisions",
            "note": "Plat, replat, amending plat, and parcel-compliance applications.",
        },
    ],
    "unincorporated_caldwell": [
        {
            "label": "Caldwell County ordinances",
            "url": "https://www.co.caldwell.tx.us/page/ordinances",
            "note": "County development and flood-damage prevention ordinances.",
        },
        {
            "label": "Caldwell County Development Ordinance",
            "url": "https://www.co.caldwell.tx.us/upload/page/0180/CALDWELL%20COUNTY%20DEVELOPMENT%20ORDINANCE.pdf",
            "note": "Subdivision, platting, and site/subdivision construction permitting in unincorporated Caldwell County.",
        },
    ],
    "unincorporated_burnet": [
        {
            "label": "Burnet County Development Services",
            "url": "https://www.burnetcountytexas.org/page/environ.home",
            "note": "County development services resources.",
        },
        {
            "label": "Burnet County Subdivision Regulations",
            "url": "https://www.burnetcountytexas.org/upload/page/0119/SUBDIVISION%20REGULATIONS-08082019.pdf",
            "note": "County subdivision regulations and preliminary plat process.",
        },
    ],
    "unincorporated_blanco": [
        {
            "label": "Blanco County development rules candidate",
            "url": "https://newtools.cira.state.tx.us/upload/page/4027/docs/11-28-23%20draft%20of%20BLANCO%20COUNTY%20rules%20and%20reg.pdf",
            "note": "Candidate county development rules; verify current adoption before relying on it.",
        },
    ],
    "san_marcos": [
        {
            "label": "San Marcos Planning & Development Services",
            "url": "https://www.sanmarcostx.gov/3958/Planning-and-Development-Services",
            "note": "Planning, development process, construction, and code resources.",
        },
    ],
    "bastrop": [
        {
            "label": "City of Bastrop Development Services",
            "url": "https://www.cityofbastrop.org/page/plan.home",
            "note": "City development process, permits, maps, and long-range planning.",
        },
        {
            "label": "City of Bastrop maps",
            "url": "https://www.cityofbastrop.org/page/plan.maps",
            "note": "City maps and planning layers.",
        },
        {
            "label": "City of Bastrop P&Z agendas",
            "url": "https://www.cityofbastrop.org/page/cs.board_agendas-pz",
            "note": "Planning and Zoning agenda packets.",
        },
    ],
    "smithville": [
        {
            "label": "Smithville Planning and Zoning",
            "url": "https://www.ci.smithville.tx.us/236/Planning-and-Zoning",
            "note": "Planning, zoning, and agenda resources.",
        },
    ],
    "elgin": [
        {
            "label": "Elgin Planning & Development",
            "url": "https://www.elgintexas.gov/183/Planning-Development",
            "note": "Code enforcement, planning/development, permits, and inspections.",
        },
        {
            "label": "Elgin Planning & Zoning Division",
            "url": "https://www.elgintexas.gov/909/Planning-Zoning-Division",
            "note": "Planning and zoning division resources.",
        },
    ],
    "lockhart": [
        {
            "label": "Lockhart Development Services contacts",
            "url": "https://www.lockhart-tx.org/page/dev_services_contacts",
            "note": "Development Services contacts and process entry point.",
        },
        {
            "label": "Lockhart Development Approval Process",
            "url": "https://www.lockhart-tx.org/page/dev_services_approval_process",
            "note": "City development approval process.",
        },
        {
            "label": "Lockhart P&Z agendas",
            "url": "https://www.lockhart-tx.org/page/pz_commission_agendas",
            "note": "Planning and Zoning Commission agendas.",
        },
    ],
    "luling": [
        {
            "label": "Luling Planning Services",
            "url": "https://www.cityofluling.net/405/Planning-Services",
            "note": "Zoning, subdivision administration, annexation coordination, and comprehensive planning.",
        },
        {
            "label": "Luling permits/applications",
            "url": "https://www.cityofluling.net/296/Permits-Applications-Inspections",
            "note": "Permit, application, and inspection resources.",
        },
    ],
    "burnet": [
        {
            "label": "City of Burnet Development Services",
            "url": "https://www.cityofburnet.com/470/Development-Services",
            "note": "Zoning, development review, and P&Z / Council support.",
        },
        {
            "label": "City of Burnet Planning & Zoning",
            "url": "https://www.cityofburnet.com/481/Planning-Zoning",
            "note": "Zoning, subdivision, site-plan, annexation, variance, and special-use resources.",
        },
    ],
    "marble_falls": [
        {
            "label": "Marble Falls Development Services",
            "url": "https://marblefallstx.gov/153/Development-Services",
            "note": "Planning, building, GIS/mapping, zoning, and permitting.",
        },
        {
            "label": "Marble Falls Planning",
            "url": "https://marblefallstx.gov/337/Planning",
            "note": "Plats, zoning cases, variances, and development applications.",
        },
    ],
    "blanco": [
        {
            "label": "City of Blanco Planning & Zoning Commission",
            "url": "https://www.cityofblancotx.gov/1228/Planning-Zoning-Commission",
            "note": "P&Z applications and commission materials.",
        },
        {
            "label": "City of Blanco applications and procedures",
            "url": "https://ecode360.com/39650078",
            "note": "Applications and procedures for zoning and development approvals.",
        },
    ],
    "johnson_city": [
        {
            "label": "Johnson City Planning & Permits",
            "url": "https://www.johnsoncitytx.org/departments/permits/",
            "note": "Building permits, inspections, plats, zoning matters, and certificates of occupancy.",
        },
        {
            "label": "Johnson City P&Z archives",
            "url": "https://www.johnsoncitytx.org/meeting-taxonomy/planning-and-zoning-commission/",
            "note": "Planning and Zoning Commission archives.",
        },
    ],
}


def _normalize_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("&", "and")
    text = text.replace(" county", "")
    text = text.replace("(unincorporated)", "")
    text = text.replace(" etj", "")
    return "_".join(part for part in text.replace("-", " ").split() if part)


def _adapter_facts(config: Any, source_id: str) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    upstream = config.get("upstream_results")
    if not isinstance(upstream, dict):
        return {}
    raw = upstream.get(source_id)
    if not isinstance(raw, dict):
        return {}
    facts = raw.get("normalized_facts")
    if isinstance(facts, dict):
        return facts
    return raw


def _text_from_values(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text_from_values(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text_from_values(v) for v in value)
    return str(value or "")


def _has_cedar_park_signal(
    identity: PropertyIdentity,
    config: Any,
) -> bool:
    haystack = " ".join(
        [
            identity.address or "",
            _text_from_values(_adapter_facts(config, "twdb_ccn")),
            _text_from_values(_adapter_facts(config, "plats_intel")),
        ],
    ).lower()
    return any(
        signal in haystack
        for signal in (
            "cedar park",
            "local business",
            "planning department",
            "gas station services",
            "gasoline service station",
        )
    )


def _jurisdiction_context(
    identity: PropertyIdentity,
    config: Any,
) -> dict[str, str | None]:
    zoning = _adapter_facts(config, "austin_zoning")
    raw_jurisdiction = (
        zoning.get("jurisdiction")
        or (identity.jurisdiction_candidates[0]
            if identity.jurisdiction_candidates else None)
    )
    county = _normalize_key(identity.county or zoning.get("county")) or None
    jurisdiction = (
        str(raw_jurisdiction).strip() if raw_jurisdiction else None
    )
    raw_kind = str(zoning.get("jurisdiction_kind") or "").strip().lower()
    lower_jurisdiction = (jurisdiction or "").lower()
    if raw_kind in {"city", "etj", "unincorporated", "unknown"}:
        jurisdiction_kind = raw_kind
    elif "unincorporated" in lower_jurisdiction:
        jurisdiction_kind = "unincorporated"
    elif " etj" in lower_jurisdiction:
        jurisdiction_kind = "etj"
    elif jurisdiction:
        jurisdiction_kind = "city"
    else:
        jurisdiction_kind = "unknown"

    clean_jurisdiction = jurisdiction
    if clean_jurisdiction:
        clean_jurisdiction = (
            clean_jurisdiction
            .replace(" ETJ", "")
            .replace(" (unincorporated)", "")
            .strip()
        )

    jurisdiction_key = _normalize_key(clean_jurisdiction)
    if jurisdiction_kind == "unincorporated" and county:
        jurisdiction_key = f"unincorporated_{county}"

    return {
        "county": county,
        "jurisdiction": clean_jurisdiction,
        "jurisdiction_kind": jurisdiction_kind,
        "jurisdiction_key": jurisdiction_key or None,
    }


def _resolve_programmatic_jurisdiction(
    identity: PropertyIdentity,
    config: Any,
) -> str | None:
    context = _jurisdiction_context(identity, config)
    jurisdiction_key = context.get("jurisdiction_key")
    if jurisdiction_key in _LAYERS_BY_JURISDICTION:
        return jurisdiction_key
    return None


# County GIS subdivision layers that expose recorded + in-review subdivision /
# platting activity (a real, queryable entitlement signal). PropDesk queries
# these within the search radius BEFORE falling back to manual agenda pointers,
# so it always tries a connection first (§20.28 product rule). Same public
# ArcGIS as plats / jurisdiction — no gated scraping.
_COUNTY_ENTITLEMENT_GIS: dict[str, dict[str, str]] = {
    "williamson": {
        "url": (
            "https://gis.wilco.org/arcgis/rest/services/public/"
            "county_subdivisions/MapServer/0/query"
        ),
        "source_label": "Williamson County's recorded subdivision map",
    },
    "bastrop": {
        "url": (
            "https://maps.co.bastrop.tx.us/server/rest/services/"
            "Viewers/PublicGISViewerService/MapServer/13/query"
        ),
        "source_label": "Bastrop County's recorded subdivision map",
    },
    "hays": {
        "url": (
            "https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/"
            "rest/services/Subdivisions/FeatureServer/4/query"
        ),
        "source_label": "Hays County's recorded subdivision map",
    },
    "travis": {
        "url": (
            "https://gis.traviscountytx.gov/server1/rest/services/"
            "Boundaries_and_Jurisdictions/Travis_County_Subdivisions/"
            "MapServer/0/query"
        ),
        "source_label": "Travis County's recorded subdivision map",
    },
}
_ENTITLEMENT_GIS_TIMEOUT = 20

# City-owned public project/permit layers. These are stronger than county
# subdivision context because they are the city's own permit/project map, but
# they can still lag current agenda action, so the card treats them as a public
# screen rather than a clean current-entitlement certificate.
_CITY_ENTITLEMENT_GIS: dict[str, dict[str, str]] = {
    "cedar_park": {
        "project_url": (
            "https://gisrest.cedarparktexas.gov/cpgis/rest/services/"
            "Planning/MPN_Permits/MapServer/0/query"
        ),
        "source_label": "Cedar Park MPN permit/project map",
        "source_url": (
            "https://gisrest.cedarparktexas.gov/cpgis/rest/services/"
            "Planning/MPN_Permits/MapServer/0"
        ),
    },
    "san_marcos": {
        "project_url": (
            "https://smgis.sanmarcostx.gov/arcgis/rest/services/"
            "Planning/CoSM_BuildingPermits/FeatureServer/0/query"
        ),
        "source_label": "San Marcos Building Permits map",
        "source_url": (
            "https://smgis.sanmarcostx.gov/arcgis/rest/services/"
            "Planning/CoSM_BuildingPermits/FeatureServer/0"
        ),
        "candidate_counties": ["hays"],
        "bbox": (29.75, 30.00, -98.15, -97.85),
    },
}
_DISPLAYED_CITY_RECORD_LIMIT = 12
_ACTIVE_STATUS_TOKENS = (
    "approved",
    "issued",
    "pending",
    "under review",
    "awaiting revision",
    "in review",
    "construction",
)
_INACTIVE_STATUS_TOKENS = ("closed", "completed", "expired", "co issued")
_MATERIAL_PROJECT_TOKENS = (
    "site plan",
    "site work",
    "site development",
    "watershed protection",
    "subdivision",
    "commercial",
    "picp",
    "parking lot",
    "restaurant",
    "retail",
    "fuel",
    "gas",
    "store",
    "parish hall",
    "church",
    "office",
    "development",
)
_BACKGROUND_PROJECT_TOKENS = (
    "irrigation",
    "solar",
    "patio",
    "pergola",
    "water softener",
    "hvac",
    "single family",
    "residential",
)

_MANOR_GOVWELL_GRAPHQL_URL = "https://app.govwell.com/api/graphql"
_MANOR_GOVWELL_PORTAL_URL = "https://app.govwell.com/manor"
_MANOR_PZ_PACKETS_URL = (
    "https://meetings.municode.com/PublishPage/index?"
    "cid=MANORTX&ppid=6e8791e4-3a6b-49c1-8f3f-03e061bae9d7&p=1"
)
_MANOR_PUBLIC_SOURCE_LABEL = "Manor GovWell + Municode P&Z packets"
_MANOR_RECENT_MEETING_LIMIT = 6
_MANOR_AGENDA_ITEM_LIMIT = 12
_MANOR_AGENDA_THREAD_LIMIT = 8
_MANOR_ENTITLEMENT_TERMS = (
    "specific use permit",
    "special use permit",
    "site plan",
    "preliminary plat",
    "final plat",
    "short form final plat",
    "subdivision",
    "concept plan",
    "zoning application",
    "zoning amendment",
    "rezoning",
    "variance",
)
_MANOR_SIGNAL_LABELS = (
    ("specific use permit", "SUP"),
    ("special use permit", "SUP"),
    ("site plan", "Site plan"),
    ("short form final plat", "Final plat"),
    ("preliminary plat", "Preliminary plat"),
    ("final plat", "Final plat"),
    ("concept plan", "Concept plan"),
    ("variance", "Variance"),
    ("rezoning", "Rezoning"),
    ("zoning amendment", "Zoning"),
    ("zoning application", "Zoning"),
    ("subdivision", "Subdivision"),
)


_COMPASS_POINTS = (
    "north", "north-east", "east", "south-east",
    "south", "south-west", "west", "north-west",
)


def _subdivision_haversine_m(
    lat1: float, lng1: float, lat2: float, lng2: float,
) -> float:
    radius = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lam = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(d_lam / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _compass_from(lat1: float, lng1: float, lat2: float, lng2: float) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lam = math.radians(lng2 - lng1)
    y = math.sin(d_lam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(d_lam)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return _COMPASS_POINTS[int((bearing + 22.5) // 45) % 8]


def _nearest_boundary_point(
    rings: Any, lat: float, lng: float,
) -> tuple[float, float] | None:
    """The point on a polygon's boundary closest to the site, in lat/lng.

    ArcGIS returns every polygon whose area INTERSECTS the search buffer, so
    the honest "how far is it" is the distance to the nearest EDGE, not to the
    centroid — a 100-acre plat's centroid can sit half a mile past its own
    fence line. Segment distances are solved in a local equirectangular frame
    (metres); over a few km the projection error is far below the precision the
    card renders (tenths of a mile)."""
    if not isinstance(rings, list) or not rings:
        return None
    scale = math.cos(math.radians(lat)) or 1e-9
    best: tuple[float, float, float] | None = None  # (d2, x, y) in local metres
    for ring in rings:
        if not isinstance(ring, list) or not ring:
            continue
        points: list[tuple[float, float]] = []
        for point in ring:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                p_lng, p_lat = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                continue
            points.append((
                (p_lng - lng) * scale * 111_320.0,
                (p_lat - lat) * 110_540.0,
            ))
        if not points:
            continue
        for index, (ax, ay) in enumerate(points):
            bx, by = points[(index + 1) % len(points)]
            dx, dy = bx - ax, by - ay
            span = dx * dx + dy * dy
            if span <= 0:
                cx, cy = ax, ay
            else:
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / span))
                cx, cy = ax + t * dx, ay + t * dy
            d2 = cx * cx + cy * cy
            if best is None or d2 < best[0]:
                best = (d2, cx, cy)
    if best is None:
        return None
    _d2, x, y = best
    return (lat + y / 110_540.0, lng + x / (scale * 111_320.0))


def _polygon_centroid(rings: Any) -> tuple[float, float] | None:
    """Approximate a county polygon centroid for a map-only project marker."""
    if not isinstance(rings, list):
        return None
    best: tuple[float, float, float] | None = None
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 3:
            continue
        points: list[tuple[float, float]] = []
        for point in ring:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                points.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(points) < 3:
            continue
        if points[0] != points[-1]:
            points.append(points[0])
        twice_area = 0.0
        centroid_x = 0.0
        centroid_y = 0.0
        for (x1, y1), (x2, y2) in pairwise(points):
            cross = x1 * y2 - x2 * y1
            twice_area += cross
            centroid_x += (x1 + x2) * cross
            centroid_y += (y1 + y2) * cross
        area = abs(twice_area) / 2
        if area:
            candidate = (
                centroid_x / (3 * twice_area),
                centroid_y / (3 * twice_area),
                area,
            )
        else:
            candidate = (
                sum(x for x, _ in points[:-1]) / (len(points) - 1),
                sum(y for _, y in points[:-1]) / (len(points) - 1),
                0.0,
            )
        if best is None or candidate[2] > best[2]:
            best = candidate
    if best is None:
        return None
    lng, lat, _area = best
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def _subdivision_geometry_facts(
    feat: dict[str, Any], lat: float | None, lng: float | None,
) -> dict[str, Any]:
    """`distance_m` + `direction` for one plat polygon, or an empty dict when
    the layer returned no usable geometry (the card then omits the column
    rather than guessing)."""
    if lat is None or lng is None:
        return {}
    geometry = feat.get("geometry")
    if not isinstance(geometry, dict):
        return {}
    nearest = _nearest_boundary_point(geometry.get("rings"), lat, lng)
    if nearest is None:
        return {}
    near_lat, near_lng = nearest
    distance_m = _subdivision_haversine_m(lat, lng, near_lat, near_lng)
    facts: dict[str, Any] = {"distance_m": round(distance_m, 1)}
    centroid = _polygon_centroid(geometry.get("rings"))
    if centroid is not None:
        facts["map_lat"], facts["map_lng"] = centroid
    if distance_m >= 25:
        # Under ~25 m a compass word is noise, not information.
        facts["direction"] = _compass_from(lat, lng, near_lat, near_lng)
    return facts


def _query_nearby_subdivision_cases(
    session: requests.Session, *, url: str, lat: float, lng: float, radius_m: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Spatial query: nearby recorded/in-review subdivisions within radius_m.

    Geometry is requested (in WGS84) so each case can carry an honest distance
    and direction from the site — without it the card would list a plat a mile
    away next to one on the fence line with nothing to tell them apart."""
    try:
        resp = session.get(
            url,
            params={
                "geometry": f"{lng},{lat}",
                "geometryType": "esriGeometryPoint",
                "distance": radius_m,
                "units": "esriSRUnit_Meter",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "f": "json",
            },
            timeout=_ENTITLEMENT_GIS_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-entitlement"},
        )
        resp.raise_for_status()
        features = resp.json().get("features") or []
    except Exception as exc:
        return None, str(exc)[:200]
    cases: list[dict[str, Any]] = []
    for feat in features:
        attrs = feat.get("attributes") or {}
        name = (
            attrs.get("SUBDIVISION_NAME")
            or attrs.get("SUB_NAME")
            or attrs.get("PLATTED_NAME")
            or attrs.get("Name")
            or attrs.get("NAME")
            or attrs.get("desc_")
            or attrs.get("code")
            or attrs.get("name")
        )
        if not name:
            continue
        case: dict[str, Any] = {
            "name": str(name),
            "status": str(
                attrs.get("SUBDIVISION_STATUS")
                or attrs.get("status")
                or attrs.get("STATUS")
                or attrs.get("Status")
                or attrs.get("PLATTED")
                or attrs.get("WORK_TYPE")
                or attrs.get("stage")
                or attrs.get("Cat")
                or ""
            ).strip(),
            # PLAT_FILE_DATE / FINAL_APP_DATE carry the date on the OLDER
            # Williamson rows (pre-2000 plats): without them the card showed
            # a blank date for 4 of the 7 Hutto cases.
            "final_plat_date_ms": (
                attrs.get("FINAL_SUB_DATE")
                or attrs.get("PLAT_FILE_DATE")
                or attrs.get("FINAL_APP_DATE")
                or attrs.get("last_edited_date")
                or attrs.get("created_date")
            ),
            "agency": str(
                attrs.get("FINAL_AGENCY")
                or attrs.get("PRELIM_AGENCY")
                or attrs.get("sudb_review")
                or attrs.get("govt_agency")
                or attrs.get("Jurisdiction")
                or attrs.get("MGO_PLN")
                or ""
            ).strip(),
            "document_number": str(
                attrs.get("DOCUMENT_NUMBER")
                or attrs.get("Document")
                or attrs.get("Vol_Page")
                or attrs.get("App_No")
                or attrs.get("MGO_PLN")
                or ""
            ).strip() or None,
            # Scale — the difference between a lot split and 258 new homes.
            "lot_count": _to_int(
                attrs.get("NUMBER_LOTS") or attrs.get("LOTS") or attrs.get("Lots")
            ),
            "acres": _to_float(attrs.get("ACRES") or attrs.get("Acres")),
        }
        case.update(_subdivision_geometry_facts(feat, lat, lng))
        cases.append(case)
    # Nearest first: what is on the fence line matters more than what is old.
    cases.sort(key=lambda c: (
        c.get("distance_m") if c.get("distance_m") is not None else float("inf"),
        -(c.get("final_plat_date_ms") or 0),
    ))
    return cases, None


def _query_arcgis_radius_features(
    session: requests.Session,
    *,
    url: str,
    lat: float,
    lng: float,
    radius_m: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        resp = session.get(
            url,
            params={
                "geometry": f"{lng},{lat}",
                "geometryType": "esriGeometryPoint",
                "distance": radius_m,
                "units": "esriSRUnit_Meter",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "false",
                "resultRecordCount": 2000,
                "f": "json",
            },
            timeout=_ENTITLEMENT_GIS_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-entitlement"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            details = payload["error"].get("details") or []
            message = payload["error"].get("message") or "ArcGIS query failed"
            return None, "; ".join([message, *map(str, details)])[:200]
        features = payload.get("features")
        if not isinstance(features, list):
            return None, "unexpected ArcGIS response shape"
        return features, None
    except Exception as exc:
        return None, str(exc)[:200]


def _clean_string(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").split()).strip()


def _clean_optional_string(value: object) -> str | None:
    text = _clean_string(value)
    if not text or text.lower() in {"none", "none."}:
        return None
    return text


def _to_int(value: object) -> int | None:
    text = _clean_string(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    text = _clean_string(value)
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _first_attr(attrs: dict[str, Any], *names: str) -> object:
    for name in names:
        value = attrs.get(name)
        if value not in (None, ""):
            return value
    return None


def _year_from_ms(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=UTC).year
    except (OSError, OverflowError, ValueError):
        return None


def _project_status_bucket(status: str) -> str:
    lower = status.lower()
    if any(token in lower for token in _INACTIVE_STATUS_TOKENS):
        return "closed_or_complete"
    if any(token in lower for token in _ACTIVE_STATUS_TOKENS):
        return "not_marked_closed"
    return "unknown"


def _project_relevance(name: str, description: str, project_number: str) -> str:
    haystack = f"{project_number} {name} {description}".lower()
    if project_number.upper().startswith(("SD-", "SP-", "Z-", "FP-")):
        return "site_development_or_entitlement"
    if any(token in haystack for token in _BACKGROUND_PROJECT_TOKENS):
        return "background_or_minor_permit"
    if any(token in haystack for token in _MATERIAL_PROJECT_TOKENS):
        return "site_development_or_entitlement"
    return "permit_project_record"


def _normalize_city_project_record(attrs: dict[str, Any]) -> dict[str, Any]:
    project_number = _clean_string(
        _first_attr(attrs, "ProjectNumber", "PROJECTNUMBER", "case_number")
    )
    name = _clean_string(
        _first_attr(attrs, "ProjectName", "PROJECTNAME", "project_name")
    )
    description = _clean_string(
        " ".join(
            _clean_string(value)
            for value in (
                _first_attr(attrs, "ProjectDescription", "DESCRIPTION"),
                attrs.get("TYPE"),
                attrs.get("LANDUSE"),
                attrs.get("project_description"),
            )
            if _clean_string(value)
        )
    )
    status = _clean_string(
        _first_attr(attrs, "ProjectStatus", "STATUS", "status")
    )
    year = (
        _to_int(_first_attr(attrs, "ProjectYear", "PROJECTYEAR"))
        or _year_from_ms(_first_attr(attrs, "APPLIED", "ISSUED"))
    )
    relevance = _project_relevance(name, description, project_number)
    bucket = _project_status_bucket(status)
    return {
        "project_number": project_number,
        "project_year": year,
        "project_name": name,
        "project_description": description,
        "project_status": status,
        "status_bucket": bucket,
        "address": _clean_string(_first_attr(attrs, "Address", "ADDRESS")),
        "zoning": _clean_optional_string(_first_attr(attrs, "Zoning", "ZONING")),
        "files": _clean_optional_string(_first_attr(attrs, "Files", "FILES")),
        "permit_id": _clean_optional_string(
            _first_attr(attrs, "PermitID", "PERMITID")
        ),
        "land_use": _clean_optional_string(_first_attr(attrs, "LANDUSE")),
        "permit_type": _clean_optional_string(_first_attr(attrs, "TYPE")),
        "relevance": relevance,
        "primary_signal": (
            "not marked closed/complete in the city layer"
            if bucket == "not_marked_closed"
            else (
                "site-development / entitlement-style record"
                if relevance == "site_development_or_entitlement"
                else "background permit record"
            )
        ),
    }


def _city_project_sort_key(record: dict[str, Any]) -> tuple[int, int, int, str]:
    status_rank = 1 if record.get("status_bucket") == "not_marked_closed" else 0
    relevance_rank = (
        1 if record.get("relevance") == "site_development_or_entitlement" else 0
    )
    year = record.get("project_year")
    return (
        relevance_rank,
        status_rank,
        year if isinstance(year, int) else 0,
        str(record.get("project_number") or ""),
    )


def _bbox_contains(bbox: object, lat: float, lng: float) -> bool:
    if (
        not isinstance(bbox, tuple)
        or len(bbox) != 4
    ):
        return False
    lat_min, lat_max, lng_min, lng_max = bbox
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _city_entitlement_enrichment(
    session: requests.Session | None,
    *,
    jurisdiction_key: str | None,
    lat: float,
    lng: float,
    radius_m: int,
) -> dict[str, Any] | None:
    city_cfg = _CITY_ENTITLEMENT_GIS.get(jurisdiction_key or "")
    if city_cfg is None or lat is None or lng is None:
        return None
    features, error = _query_arcgis_radius_features(
        http_session(session),
        url=city_cfg["project_url"],
        lat=lat,
        lng=lng,
        radius_m=radius_m,
    )
    if error is not None or features is None:
        return {
            "city_entitlement_source": city_cfg["source_label"],
            "city_entitlement_source_url": city_cfg["source_url"],
            "city_entitlement_error": error or "city project layer failed",
        }

    records = [
        _normalize_city_project_record(feat.get("attributes") or {})
        for feat in features
    ]
    records = [
        record for record in records
        if record.get("project_number") or record.get("project_name")
    ]
    records.sort(key=_city_project_sort_key, reverse=True)
    status_counts = Counter(
        str(record.get("project_status") or "Unknown") for record in records
    )
    not_closed_count = sum(
        1 for record in records
        if record.get("status_bucket") == "not_marked_closed"
    )
    material_count = sum(
        1 for record in records
        if record.get("relevance") == "site_development_or_entitlement"
    )
    material_records = [
        record for record in records
        if record.get("relevance") == "site_development_or_entitlement"
    ]
    years = [
        year for year in (record.get("project_year") for record in records)
        if isinstance(year, int)
    ]
    latest_year = max(years) if years else None
    current_year = datetime.now(UTC).year
    stale = latest_year is not None and latest_year < current_year - 2
    radius_mi = round(radius_m / 1609.34, 1)
    return {
        # Primary card rows are material-only. Totals below preserve source
        # audit context without making minor/noisy permit rows the broker read.
        "city_entitlement_records": (
            material_records[:_DISPLAYED_CITY_RECORD_LIMIT]
        ),
        "city_entitlement_records_role": "material_primary",
        "city_entitlement_suppressed_non_material_count": (
            len(records) - len(material_records)
        ),
        "city_entitlement_record_count": len(records),
        "city_entitlement_not_closed_count": not_closed_count,
        "city_entitlement_material_count": material_count,
        "city_entitlement_status_counts": dict(status_counts),
        "city_entitlement_latest_year": latest_year,
        "city_entitlement_feed_stale": stale,
        "city_entitlement_source": city_cfg["source_label"],
        "city_entitlement_source_url": city_cfg["source_url"],
        "city_entitlement_radius_mi": radius_mi,
    }


def _strip_html_to_text(markup: str) -> str:
    without_script = re.sub(
        r"(?is)<(script|style).*?</\1>", " ", markup or "",
    )
    return " ".join(
        html.unescape(re.sub(r"(?s)<[^>]+>", " ", without_script)).split()
    )


def _identity_entitlement_search_terms(identity: PropertyIdentity) -> list[str]:
    terms: list[str] = []
    for value in (
        identity.parcel_id,
        identity.cad_account,
        identity.address,
    ):
        text = _clean_string(value)
        if text:
            terms.append(text)
    if identity.address:
        address = identity.address.upper()
        street_number = re.search(r"\b\d{3,}\b", address)
        if street_number:
            terms.append(street_number.group(0))
        for match in re.finditer(
            r"\b(?:FM|F\.M\.|US|HWY|HIGHWAY|SH|CR)\s*-?\s*\d+\b",
            address,
        ):
            road = " ".join(match.group(0).replace(".", "").split())
            terms.append(road)
            terms.append(road.replace(" ", ""))
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = _clean_string(term)
        key = cleaned.lower()
        if len(cleaned) < 4 or key in seen or key in {"manor", "travis"}:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique[:8]


def _manor_govwell_summary(
    session: requests.Session,
) -> tuple[dict[str, Any] | None, str | None]:
    query = """
      query ManorGovWellPublicSources($slug: String!) {
        organizationBySlug(slug: $slug) {
          id
          slug
          displayName
          canSearchEntities
          gisSetup
          isPublicGISSearchEnabled
          publishedViewDashboards {
            id
            uuid
            name
            summary
            recordType {
              id
              displayName
            }
          }
        }
        recordTypes: getRecordTypesForOrganizationHomePage(slug: $slug) {
          id
          displayName
          moduleType
          applyDescription
        }
      }
    """
    try:
        resp = session.post(
            _MANOR_GOVWELL_GRAPHQL_URL,
            json={
                "operationName": "ManorGovWellPublicSources",
                "query": query,
                "variables": {"slug": "manor"},
            },
            timeout=_ENTITLEMENT_GIS_TIMEOUT,
            headers={
                "User-Agent": "PropDesk/1.0 public-entitlement",
                "Origin": "https://app.govwell.com",
                "Referer": _MANOR_GOVWELL_PORTAL_URL,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return None, str(exc)[:200]
    if payload.get("errors"):
        first = payload["errors"][0]
        return None, _clean_string(first.get("message") or "GovWell error")[:200]
    data = payload.get("data") or {}
    org = data.get("organizationBySlug") or {}
    record_types = data.get("recordTypes") or []
    dashboards = org.get("publishedViewDashboards") or []
    pz_record_types = [
        record_type for record_type in record_types
        if re.search(
            r"planning|zoning|site|plat|subdivision|variance|use permit",
            _clean_string(record_type.get("displayName")),
            re.IGNORECASE,
        )
    ]
    pz_dashboard_present = any(
        re.search(
            r"planning|zoning|site|plat|subdivision",
            " ".join(
                _clean_string(value)
                for value in (
                    dashboard.get("name"),
                    dashboard.get("summary"),
                    (dashboard.get("recordType") or {}).get("displayName"),
                )
            ),
            re.IGNORECASE,
        )
        for dashboard in dashboards
    )
    return {
        "govwell_organization_id": org.get("id"),
        "govwell_display_name": org.get("displayName"),
        "govwell_can_search_entities": bool(org.get("canSearchEntities")),
        "govwell_is_public_gis_search_enabled": bool(
            org.get("isPublicGISSearchEnabled")
        ),
        "govwell_gis_setup": org.get("gisSetup"),
        "govwell_record_types": [
            {
                "id": record_type.get("id"),
                "display_name": record_type.get("displayName"),
                "module_type": record_type.get("moduleType"),
            }
            for record_type in record_types
        ],
        "govwell_pz_record_type_present": bool(pz_record_types),
        "govwell_pz_record_types": [
            _clean_string(record_type.get("displayName"))
            for record_type in pz_record_types
        ],
        "govwell_public_dashboards": [
            {
                "id": dashboard.get("id"),
                "uuid": dashboard.get("uuid"),
                "name": dashboard.get("name"),
                "summary": dashboard.get("summary"),
                "record_type": (
                    dashboard.get("recordType") or {}
                ).get("displayName"),
            }
            for dashboard in dashboards
        ],
        "govwell_public_pz_dashboard_present": pz_dashboard_present,
    }, None


def _manor_meeting_rows(markup: str) -> list[dict[str, str]]:
    meetings: list[dict[str, str]] = []
    for row_html in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", markup or ""):
        row_text = _strip_html_to_text(row_html)
        if "Planning" not in row_text and "Zoning" not in row_text:
            continue
        date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", row_text)
        links = [
            html.unescape(link)
            for link in re.findall(r'href="([^"]+)"', row_html)
        ]
        html_packet = next(
            (link for link in links if "adaHtmlDocument" in link and "ip=true" in link),
            "",
        )
        html_agenda = next(
            (link for link in links if "adaHtmlDocument" in link and "ip=false" in link),
            "",
        )
        pdf_packet = next(
            (link for link in links if "MEET-Packet" in link),
            "",
        )
        if not date_match or not html_packet:
            continue
        meetings.append({
            "meeting_name": row_text.split(date_match.group(0))[0].strip(),
            "meeting_date": date_match.group(0),
            "html_packet_url": html_packet,
            "html_agenda_url": html_agenda,
            "pdf_packet_url": pdf_packet,
        })
        if len(meetings) >= _MANOR_RECENT_MEETING_LIMIT:
            break
    return meetings


def _parse_meeting_year(date_text: str) -> int | None:
    try:
        return datetime.strptime(date_text, "%m/%d/%Y").replace(tzinfo=UTC).year
    except ValueError:
        return None


def _agenda_item_address(text: str) -> str:
    match = re.search(
        r"(?:located at|also known as|near the intersection of)\s+"
        r"(.{8,120}?)(?:\.| Applicant:| Owner:|$)",
        text,
        re.IGNORECASE,
    )
    return _clean_string(match.group(1)) if match else ""


def _agenda_item_title(text: str) -> str:
    title = re.split(r"\s+(?:Applicant:|Owner:|[a-zA-Z]_\w+\.pdf|\ba\.)", text)[0]
    title = re.sub(r"^\d+\.\s+", "", title)
    title = re.sub(
        r"^(?:Conduct a public hearing on|Conduct the public hearing on|"
        r"Consideration, discussion, and possible action on|"
        r"Consideration, discussion, and possible action to approve)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return _clean_string(title)[:240]


def _agenda_case_key(record: dict[str, Any]) -> str:
    text = _clean_string(
        record.get("project_description") or record.get("project_name")
    ).lower()
    for term in sorted(_MANOR_ENTITLEMENT_TERMS, key=len, reverse=True):
        index = text.find(term)
        if index >= 0:
            return re.sub(r"[^a-z0-9]+", " ", text[index:index + 220]).strip()
    return re.sub(
        r"[^a-z0-9]+", " ", _clean_string(record.get("project_name")).lower(),
    ).strip()


def _agenda_meeting_sort_value(date_text: Any) -> datetime:
    try:
        return datetime.strptime(str(date_text), "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _agenda_tcad_parcels(text: str) -> list[str]:
    parcels: list[str] = []
    for match in re.findall(r"\bTCAD parcels?\s+([0-9,\sand]+)", text, re.IGNORECASE):
        parcels.extend(re.findall(r"\d{4,}", match))
    return list(dict.fromkeys(parcels))


def _agenda_signal_labels(record: dict[str, Any]) -> list[str]:
    terms = " ".join(
        _clean_string(term)
        for term in (record.get("matched_entitlement_terms") or [])
    ).lower()
    labels: list[str] = []
    for term, label in _MANOR_SIGNAL_LABELS:
        if term in terms and label not in labels:
            labels.append(label)
    return labels


def _agenda_project_label(record: dict[str, Any]) -> str:
    text = _clean_string(
        record.get("project_description") or record.get("project_name")
    )
    for pattern in (
        (r"\bfor\s+(?:the\s+)?([A-Z0-9][A-Za-z0-9 ,&'./-]{3,90}?)(?:,?\s+being\b|,\s+"
        r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d)|"
        r",?\s+also known as\b|\.|$)"),
        (r"\bbeing\s+([A-Z][A-Za-z0-9 &'./-]{3,90}?)(?:,\s+also known as\b|"
        r"\s+also known as\b|,|\.)"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        label = _clean_string(match.group(1))
        if not re.search(
            r"\bsquare feet\b|\bacres?\b|\blots?\b|^TCAD parcel\b",
            label,
            re.IGNORECASE,
        ):
            return label
    address = _clean_string(record.get("address"))
    signals = _agenda_signal_labels(record)
    if address:
        return _clean_string(f"{address} {signals[0] if signals else 'P&Z item'}")
    return _clean_string(record.get("project_name"))[:120] or "P&Z agenda item"


def _agenda_project_identity(record: dict[str, Any]) -> tuple[str, str, list[str]]:
    text = _clean_string(
        record.get("project_description") or record.get("project_name")
    )
    parcels = _agenda_tcad_parcels(text)
    label = _agenda_project_label(record)
    if parcels:
        return f"tcad:{','.join(parcels)}", label, parcels
    address = _clean_string(record.get("address"))
    if address:
        key = re.sub(r"[^a-z0-9]+", " ", address.lower()).strip()
        return f"address:{key}", label, parcels
    key = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    return f"title:{key}", label, parcels


def _manor_agenda_project_threads(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        key, label, parcels = _agenda_project_identity(record)
        thread = grouped.setdefault(
            key,
            {
                "project_key": key,
                "project_name": label,
                "records": [],
                "signals": [],
                "matched_terms": [],
                "addresses": [],
                "tcad_parcels": [],
            },
        )
        thread["records"].append(record)
        for signal in _agenda_signal_labels(record):
            if signal not in thread["signals"]:
                thread["signals"].append(signal)
        for term in record.get("matched_terms") or []:
            if term not in thread["matched_terms"]:
                thread["matched_terms"].append(term)
        address = _clean_string(record.get("address"))
        if address and address not in thread["addresses"]:
            thread["addresses"].append(address)
        for parcel in parcels:
            if parcel not in thread["tcad_parcels"]:
                thread["tcad_parcels"].append(parcel)

    threads: list[dict[str, Any]] = []
    for thread in grouped.values():
        thread_records = sorted(
            thread["records"],
            key=lambda record: _agenda_meeting_sort_value(
                record.get("meeting_date")
            ),
            reverse=True,
        )
        latest = thread_records[0]
        dates = list(dict.fromkeys(
            str(record.get("meeting_date"))
            for record in thread_records
            if record.get("meeting_date")
        ))
        signals = thread["signals"] or [
            _clean_string(latest.get("primary_signal")) or "P&Z agenda item"
        ]
        property_match = bool(thread["matched_terms"])
        threads.append({
            "project_key": thread["project_key"],
            "project_name": thread["project_name"],
            "latest_meeting_date": latest.get("meeting_date"),
            "latest_project_number": latest.get("project_number"),
            "latest_status": f"P&Z packet {latest.get('meeting_date')}",
            "project_year": latest.get("project_year"),
            "meeting_dates": dates,
            "packet_item_count": len(thread_records),
            "prior_appearances_count": max(0, len(thread_records) - 1),
            "signals": signals,
            "primary_signal": " / ".join(signals[:3]),
            "property_match": property_match,
            "property_match_label": (
                "Matched parcel/address terms" if property_match else "No"
            ),
            "matched_terms": thread["matched_terms"],
            "addresses": thread["addresses"],
            "tcad_parcels": thread["tcad_parcels"],
            "latest_source_url": latest.get("source_url"),
        })
    threads.sort(
        key=lambda thread: (
            bool(thread.get("property_match")),
            _agenda_meeting_sort_value(thread.get("latest_meeting_date")),
            int(thread.get("packet_item_count") or 0),
        ),
        reverse=True,
    )
    return threads


def _manor_agenda_items_from_packet(
    plain_text: str,
    *,
    meeting: dict[str, str],
    search_terms: list[str],
) -> list[dict[str, Any]]:
    starts = list(
        re.finditer(
            r"\b(\d{1,2})\.\s+(?=(?:Conduct|Consideration|Discuss|"
            r"Hold|Receive|Review|Presentation))",
            plain_text,
            re.IGNORECASE,
        )
    )
    records: list[dict[str, Any]] = []
    lowered_terms = [term.lower() for term in search_terms]
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(plain_text)
        item_text = _clean_string(plain_text[match.start():end])
        lower = item_text.lower()
        matched_entitlement_terms = [
            term for term in _MANOR_ENTITLEMENT_TERMS if term in lower
        ]
        if not matched_entitlement_terms:
            continue
        matched_identity_terms = [
            search_terms[i]
            for i, term in enumerate(lowered_terms)
            if term in lower
        ]
        item_number = match.group(1)
        year = _parse_meeting_year(meeting["meeting_date"])
        primary_signal = "recent Manor P&Z agenda item"
        if matched_identity_terms:
            primary_signal += " matching parcel/address terms"
        records.append({
            "project_number": (
                f"Manor P&Z {meeting['meeting_date']} item {item_number}"
            ),
            "project_year": year,
            "project_name": _agenda_item_title(item_text),
            "project_description": item_text[:700],
            "project_status": f"P&Z packet {meeting['meeting_date']}",
            "address": _agenda_item_address(item_text),
            "zoning": None,
            "files": None,
            "permit_id": None,
            "relevance": "site_development_or_entitlement",
            "status_bucket": "agenda_item",
            "primary_signal": primary_signal,
            "meeting_name": meeting["meeting_name"],
            "meeting_date": meeting["meeting_date"],
            "source_url": meeting["html_packet_url"],
            "matched_terms": matched_identity_terms,
            "matched_entitlement_terms": matched_entitlement_terms,
        })
    return records


def _manor_pz_agenda_screen(
    session: requests.Session,
    *,
    identity: PropertyIdentity,
) -> tuple[dict[str, Any], str | None]:
    search_terms = _identity_entitlement_search_terms(identity)
    try:
        resp = session.get(
            _MANOR_PZ_PACKETS_URL,
            timeout=_ENTITLEMENT_GIS_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-entitlement"},
        )
        resp.raise_for_status()
        meetings = _manor_meeting_rows(resp.text)
    except Exception as exc:
        return {"city_entitlement_search_terms": search_terms}, str(exc)[:200]

    records: list[dict[str, Any]] = []
    packet_errors: list[str] = []
    for meeting in meetings:
        try:
            packet_resp = session.get(
                meeting["html_packet_url"],
                timeout=_ENTITLEMENT_GIS_TIMEOUT,
                headers={"User-Agent": "PropDesk/1.0 public-entitlement"},
            )
            packet_resp.raise_for_status()
            packet_text = _strip_html_to_text(packet_resp.text)
        except Exception as exc:
            packet_errors.append(str(exc)[:120])
            continue
        records.extend(
            _manor_agenda_items_from_packet(
                packet_text,
                meeting=meeting,
                search_terms=search_terms,
            )
        )
    deduped_records: list[dict[str, Any]] = []
    seen_case_keys: set[tuple[str, str]] = set()
    for record in records:
        key = (str(record.get("meeting_date") or ""), _agenda_case_key(record))
        if key in seen_case_keys:
            continue
        seen_case_keys.add(key)
        deduped_records.append(record)
    records = deduped_records
    records.sort(
        key=lambda record: (
            bool(record.get("matched_terms")),
            record.get("project_year") or 0,
            record.get("meeting_date") or "",
        ),
        reverse=True,
    )
    target_match_count = sum(1 for record in records if record.get("matched_terms"))
    project_threads = _manor_agenda_project_threads(records)
    latest_year = max(
        [
            year for year in (record.get("project_year") for record in records)
            if isinstance(year, int)
        ],
        default=None,
    )
    return {
        "city_entitlement_records": records[:_MANOR_AGENDA_ITEM_LIMIT],
        "city_entitlement_records_role": "agenda_packet_screen",
        "city_entitlement_record_count": len(records),
        "city_entitlement_project_threads": (
            project_threads[:_MANOR_AGENDA_THREAD_LIMIT]
        ),
        "city_entitlement_project_thread_count": len(project_threads),
        "city_entitlement_repeated_project_item_count": (
            len(records) - len(project_threads)
        ),
        "city_entitlement_not_closed_count": target_match_count,
        "city_entitlement_material_count": len(project_threads),
        "city_entitlement_status_counts": {
            "recent_pz_agenda_item": len(records),
        },
        "city_entitlement_latest_year": latest_year,
        "city_entitlement_feed_stale": False,
        "city_entitlement_source": _MANOR_PUBLIC_SOURCE_LABEL,
        "city_entitlement_source_url": _MANOR_PZ_PACKETS_URL,
        "city_entitlement_source_kind": "agenda_packet_screen",
        "city_entitlement_agenda_meetings_checked": len(meetings),
        "city_entitlement_target_match_count": target_match_count,
        "city_entitlement_search_terms": search_terms,
        "city_entitlement_packet_errors": packet_errors,
    }, None


def _city_public_portal_enrichment(
    session: requests.Session | None,
    *,
    jurisdiction_key: str | None,
    identity: PropertyIdentity,
) -> dict[str, Any] | None:
    if jurisdiction_key != "manor":
        return None
    public_session = http_session(session)
    govwell, govwell_error = _manor_govwell_summary(public_session)
    agenda, agenda_error = _manor_pz_agenda_screen(
        public_session,
        identity=identity,
    )
    if govwell is None and agenda_error:
        return {
            "city_entitlement_source": _MANOR_PUBLIC_SOURCE_LABEL,
            "city_entitlement_source_url": _MANOR_GOVWELL_PORTAL_URL,
            "city_entitlement_error": (
                f"GovWell: {govwell_error or 'not available'}; "
                f"P&Z packets: {agenda_error}"
            ),
        }
    enrichment = {
        **(govwell or {}),
        **agenda,
        "city_entitlement_source": _MANOR_PUBLIC_SOURCE_LABEL,
        "city_entitlement_source_url": _MANOR_PZ_PACKETS_URL,
        "city_entitlement_source_kind": "agenda_packet_screen",
        "govwell_portal_url": _MANOR_GOVWELL_PORTAL_URL,
    }
    if govwell_error:
        enrichment["govwell_error"] = govwell_error
    if agenda_error:
        enrichment["city_entitlement_error"] = agenda_error
    return enrichment


def _candidate_city_entitlement_enrichment(
    session: requests.Session | None,
    *,
    county: str | None,
    lat: float,
    lng: float,
    radius_m: int,
) -> tuple[str | None, dict[str, Any] | None]:
    county_key = _normalize_key(county)
    if not county_key:
        return None, None
    for jurisdiction_key, cfg in _CITY_ENTITLEMENT_GIS.items():
        if county_key not in (cfg.get("candidate_counties") or []):
            continue
        if not _bbox_contains(cfg.get("bbox"), lat, lng):
            continue
        enrichment = _city_entitlement_enrichment(
            session,
            jurisdiction_key=jurisdiction_key,
            lat=lat,
            lng=lng,
            radius_m=radius_m,
        )
        if enrichment is not None:
            enrichment["city_entitlement_candidate_only"] = True
            return jurisdiction_key, enrichment
    return None, None


def _county_gis_subdivision_enrichment(
    session: requests.Session | None, *,
    county: str, lat: float, lng: float, radius_m: int,
) -> dict[str, Any] | None:
    """Try-first enrichment: query the county subdivisions GIS for nearby
    platting activity. Returns facts to MERGE into the not-wired result (so the
    card keeps the city manual pointers AND shows that PropDesk actually
    checked a real public source). Returns None on no-config / failure / no
    coordinates — the caller keeps the plain manual state."""
    settings = get_settings()
    gis_cfg = _COUNTY_ENTITLEMENT_GIS.get((county or "").lower())
    if gis_cfg is None or lat is None or lng is None:
        return None
    if not settings.public_entitlement_gis_enabled:
        return None
    # The recorded-plat sweep runs on its OWN radius, wider than the city
    # case-feed radius: recorded plats are the area's residential SUPPLY
    # pipeline, and half a mile is too tight to show the projects a buyer is
    # actually competing with. (Chandler Rd / Hutto: 7 plats at 0.5 mi, 41 at
    # 2 mi — including every large subdivision the owner asked about.)
    sweep_radius_m = int(
        getattr(settings, "public_entitlement_subdivision_radius_m", radius_m)
        or radius_m
    )
    cases, error = _query_nearby_subdivision_cases(
        http_session(session), url=gis_cfg["url"],
        lat=lat, lng=lng, radius_m=sweep_radius_m,
    )
    if error is not None or cases is None:
        return None
    radius_mi = round(sweep_radius_m / 1609.34, 1)
    display_limit = int(
        getattr(settings, "public_entitlement_subdivision_display_limit", 12) or 12
    )
    facts: dict[str, Any] = {
        "subdivision_cases": cases[:display_limit],
        "subdivision_case_count": len(cases),
        "subdivision_radius_m": sweep_radius_m,
        "county_gis_source": gis_cfg["source_label"],
        "advisory_suffix": (
            f" PropDesk did read {gis_cfg['source_label']} and found "
            f"{len(cases)} plat{'' if len(cases) == 1 else 's'} recorded "
            f"within {radius_mi} mi — those are listed above."
        ),
    }
    facts.update(_subdivision_scale_facts(cases))
    return facts


def _subdivision_scale_facts(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Derived supply signal: how many lots are platted nearby, and which
    single plat is the biggest. Only the nearest few rows fit on a card, so
    without this the largest project in the area can fall off the bottom."""
    with_lots = [
        case for case in cases
        if isinstance(case.get("lot_count"), int) and case["lot_count"] > 0
    ]
    if not with_lots:
        return {}
    largest = max(with_lots, key=lambda case: case["lot_count"])
    return {
        "subdivision_total_lots": sum(case["lot_count"] for case in with_lots),
        "subdivision_lot_counted_cases": len(with_lots),
        "subdivision_largest": {
            "name": largest.get("name"),
            "lot_count": largest.get("lot_count"),
            "acres": largest.get("acres"),
            "distance_m": largest.get("distance_m"),
            "direction": largest.get("direction"),
            "final_plat_date_ms": largest.get("final_plat_date_ms"),
        },
    }


def _manual_pointer_labels(
    manual_source_pointers: list[dict[str, str]],
    *,
    limit: int = 2,
) -> list[str]:
    out: list[str] = []
    for pointer in manual_source_pointers:
        label = (pointer.get("label") or "").strip()
        if label:
            out.append(label)
        if len(out) >= limit:
            break
    return out


_REGISTRY_POINTER_CARDS: tuple[str, ...] = ("entitlement", "permits", "plats")


def _source_mode_label(mode: SourceMode) -> str:
    return mode.value.replace("_", " ")


def _registry_entitlement_sources(
    *,
    county: str | None,
    jurisdiction_key: str | None,
):
    if _normalize_key(county) != "travis" or not jurisdiction_key:
        return None
    sources = get_jurisdiction_sources(county, jurisdiction_key)
    if sources is None:
        return None
    card = sources.entitlement
    if card.mode == SourceMode.UNKNOWN_NOT_AUDITED:
        return None
    return sources, card


def _registry_source_pointers(
    *,
    county: str | None,
    jurisdiction_key: str | None,
) -> list[dict[str, str]]:
    if _normalize_key(county) != "travis":
        return []
    sources = get_jurisdiction_sources(county, jurisdiction_key)
    if sources is None:
        return []
    pointers: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    label_by_card = {
        "entitlement": f"{sources.display_name} entitlement source",
        "permits": f"{sources.display_name} permit/development portal",
        "plats": f"{sources.display_name} plat/subdivision source",
    }
    for card_name in _REGISTRY_POINTER_CARDS:
        card = sources.card(card_name)
        if (
            card.mode == SourceMode.UNKNOWN_NOT_AUDITED
            or not card.url
            or card.url in seen_urls
        ):
            continue
        seen_urls.add(card.url)
        pointers.append({
            "label": label_by_card[card_name],
            "url": card.url,
            "note": (
                f"Source mode: {_source_mode_label(card.mode)}. "
                f"{card.note}".strip()
            ),
        })
    return pointers


def _dedupe_source_pointers(
    pointers: list[dict[str, str]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pointer in pointers:
        key = (pointer.get("label", ""), pointer.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(pointer)
    return out


def _city_entitlement_read(
    *,
    jurisdiction: str | None,
    record_count: int,
    not_closed_count: int,
    material_count: int,
    latest_year: int | None,
    stale: bool,
    source_label: str,
    manual_source_pointers: list[dict[str, str]],
    radius_m: int,
) -> dict[str, Any]:
    label = jurisdiction or "This city"
    pointer_labels = _manual_pointer_labels(manual_source_pointers)
    pointer_text = (
        " and ".join(pointer_labels)
        if pointer_labels else "the city permitting portal and recent agendas"
    )
    radius_mi = round(radius_m / 1609.34, 1)
    latest_text = (
        f"The latest project year in the layer is {latest_year}."
        if latest_year else "The layer did not expose a project year."
    )
    if record_count:
        broker_read = (
            f"PropDesk queried {label}'s public permit/project map and found "
            f"{material_count} entitlement-relevant project "
            f"record{'' if material_count == 1 else 's'} within about "
            f"{radius_mi} mi, out of {record_count} total permit/project "
            f"record{'' if record_count == 1 else 's'}. {not_closed_count} "
            f"{'record is' if not_closed_count == 1 else 'records are'} not "
            "marked closed/expired/complete in that layer. "
            f"{latest_text} "
        )
        if stale:
            broker_read += (
                "Because the city layer appears stale, treat these rows as a "
                "public project screen and historical context; verify current "
                "active cases in the city portal/agendas before relying on it."
            )
        else:
            broker_read += (
                "Treat this as a public project screen; verify any material "
                "row against the city portal/agendas before relying on it."
            )
    else:
        broker_read = (
            f"PropDesk queried {label}'s public permit/project map and did not "
            f"find project records within about {radius_mi} mi. This is not a "
            "clean no-case guarantee; verify current city agendas and permit "
            "intake for material entitlement items."
        )

    if material_count:
        headline = (
            f"{material_count} entitlement-relevant {label} project "
            f"record{'' if material_count == 1 else 's'} found"
        )
    elif record_count:
        headline = "City permit/project layer checked: no material records flagged"
    else:
        headline = "City permit/project layer checked: no nearby records"

    return {
        "headline": headline,
        "broker_read": broker_read,
        "why_it_matters": (
            "City permit/project records are more relevant than county plat "
            "background because they can reveal site-development permits, "
            "unclosed construction activity, and entitlement-style project "
            "filings near the property."
        ),
        "next_step": (
            f"Review the listed {source_label} records first, then search "
            f"{pointer_text} for current rezoning, site-development, "
            "subdivision, P&Z, or council items that are not captured in the "
            "map layer."
        ),
        "activity_summary": (
            f"{material_count} entitlement-relevant city project "
            f"record{'' if material_count == 1 else 's'}; "
            f"{not_closed_count} not marked closed/complete; "
            f"{record_count} total city permit/project records checked; "
            f"latest layer year: {latest_year or 'unknown'}."
        ),
        "activity_relevance": (
            "city_project_screen_stale"
            if stale else "city_project_screen"
        ),
        "raw_rows_role": "primary_result",
        "manual_check_required": True,
        "city_public_layer_checked": True,
        "city_feed_stale": stale,
    }


def _city_agenda_entitlement_read(
    *,
    jurisdiction: str | None,
    record_count: int,
    target_match_count: int,
    meeting_count: int,
    project_thread_count: int,
    govwell_pz_present: bool,
    public_pz_dashboard_present: bool,
    search_terms: list[str],
    manual_source_pointers: list[dict[str, str]],
) -> dict[str, Any]:
    label = jurisdiction or "This city"
    pointer_labels = _manual_pointer_labels(manual_source_pointers)
    pointer_text = (
        " and ".join(pointer_labels)
        if pointer_labels else "GovWell and recent P&Z agenda packets"
    )
    terms_text = ", ".join(search_terms[:4]) if search_terms else "parcel terms"
    dashboard_text = (
        "a public P&Z dashboard is published"
        if public_pz_dashboard_present
        else (
            "GovWell publishes Building Records publicly today, but not a "
            "public P&Z case dashboard"
        )
    )
    if target_match_count:
        headline = (
            f"{target_match_count} {label} P&Z agenda item"
            f"{'' if target_match_count == 1 else 's'} matched this parcel/address"
        )
        broker_read = (
            f"PropDesk checked Manor's current GovWell portal and recent P&Z "
            f"agenda packets. GovWell exposes a Planning & Zoning Applications "
            f"module, and "
            f"the packet screen found {target_match_count} entitlement-style "
            f"agenda item{'' if target_match_count == 1 else 's'} matching "
            f"{terms_text}. "
        )
        raw_rows_role = "primary_result"
    else:
        headline = (
            f"No recent public {label} P&Z packet match found for this property"
        )
        broker_read = (
            f"PropDesk checked Manor's current GovWell portal. GovWell exposes "
            f"a Planning & Zoning Applications module, but {dashboard_text}. "
            f"PropDesk then screened {meeting_count} recent P&Z agenda packet"
            f"{'' if meeting_count == 1 else 's'} and found no packet item "
            f"matching {terms_text}. "
        )
        raw_rows_role = "audit_context"
    broker_read += (
        f"The recent-packet screen found {record_count} entitlement-style "
        f"citywide item{'' if record_count == 1 else 's'} grouped into "
        f"{project_thread_count} project thread"
        f"{'' if project_thread_count == 1 else 's'}. "
        "Treat this as a public negative screen for the subject property, not "
        "a final no-case certificate, because the public packet text is not a "
        "parcel-geocoded current case feed."
    )
    return {
        "headline": headline,
        "broker_read": broker_read,
        "why_it_matters": (
            "Manor's P&Z packet items reveal active SUPs, plats, rezonings, "
            "site-plan hearings, and variances even when the permit portal "
            "does not publish a parcel-geocoded case dashboard."
        ),
        "next_step": (
            f"Use {pointer_text} as the verification path for formal case "
            "status. Search the TCAD account, address, legal description, "
            "owner/applicant names, and nearby street names only when a current "
            "case-status certificate is needed; do not treat the citywide "
            "agenda context below as a subject-property match."
        ),
        "activity_summary": (
            f"{meeting_count} recent Manor P&Z meeting packet"
            f"{'' if meeting_count == 1 else 's'} checked; {record_count} "
            f"entitlement-style agenda item{'' if record_count == 1 else 's'}; "
            f"{project_thread_count} consolidated project thread"
            f"{'' if project_thread_count == 1 else 's'}; "
            f"{target_match_count} parcel/address term match"
            f"{'' if target_match_count == 1 else 'es'}; GovWell P&Z module: "
            f"{'present' if govwell_pz_present else 'not found'}."
        ),
        "activity_relevance": "city_agenda_packet_screen",
        "raw_rows_role": raw_rows_role,
        "manual_check_required": True,
        "city_public_source_checked": True,
        "city_public_source_kind": "agenda_packet_screen",
        "govwell_public_pz_dashboard_present": public_pz_dashboard_present,
    }


def _registry_entitlement_read(
    *,
    jurisdiction: str | None,
    jurisdiction_display: str,
    source_mode: SourceMode,
    source_url: str | None,
    source_note: str,
    subdivision_count: int | None,
    county_gis_source: str | None,
    manual_source_pointers: list[dict[str, str]],
    radius_m: int,
) -> dict[str, Any]:
    pointer_labels = _manual_pointer_labels(manual_source_pointers, limit=3)
    pointer_text = (
        " and ".join(pointer_labels)
        if pointer_labels else "the official planning / permitting sources"
    )
    radius_mi = round(radius_m / 1609.34, 1)
    mode_text = _source_mode_label(source_mode)
    county_text = ""
    if subdivision_count is not None:
        county_text = (
            f" PropDesk also checked {county_gis_source or 'the county GIS'} "
            f"for subdivision / platting activity within about {radius_mi} mi "
            f"and found {subdivision_count} record"
            f"{'' if subdivision_count == 1 else 's'}."
        )
    if source_mode == SourceMode.NOT_PUBLISHED:
        headline = f"{jurisdiction_display} entitlement source not published"
        broker_read = (
            f"PropDesk audited {jurisdiction_display}'s Travis County source "
            "entry points and did not find a published structured city "
            "entitlement portal or agenda archive. This is an audited "
            "not-published finding, not an unknown source gap."
            f"{county_text}"
        )
    else:
        headline = f"{jurisdiction_display} official entitlement source mapped"
        broker_read = (
            f"PropDesk mapped {jurisdiction_display}'s entitlement entry point "
            f"to an official {mode_text} source"
            f"{f' ({source_url})' if source_url else ''}. This source is not "
            "a parcel-geocoded automated case feed, so active cases still "
            "require a targeted source search by address, legal description, "
            "owner/applicant, and nearby street names."
            f"{county_text}"
        )
    return {
        "headline": headline,
        "broker_read": broker_read,
        "why_it_matters": (
            "For Travis County parcels, city and ETJ entitlement authority can "
            "sit with different municipalities. PropDesk now maps the specific "
            "official entry point for the resolved jurisdiction instead of "
            "falling back to a generic manual search."
        ),
        "next_step": (
            f"Search {pointer_text} for the parcel address, TCAD account, legal "
            "description, owner/applicant names, and adjacent street names. "
            "Open any agenda packet, staff report, permit record, or portal "
            "case that matches the diligence radius."
        ),
        "activity_summary": (
            f"Official source mode: {mode_text}; "
            f"source URL: {source_url or 'not published'}; "
            f"county subdivision records checked: "
            f"{subdivision_count if subdivision_count is not None else 'pending'}."
        ),
        "activity_relevance": "official_source_registry",
        "raw_rows_role": "audit_only",
        "manual_check_required": True,
        "city_public_source_checked": False,
        "city_public_source_kind": "official_registry_source",
        "source_note": source_note,
    }


def _miles(distance_m: Any) -> str | None:
    """Plain distance a reader can picture. Under a tenth of a mile the tenths
    round to 0.0, so those are given in feet."""
    try:
        metres = float(distance_m)
    except (TypeError, ValueError):
        return None
    if metres < 0:
        return None
    if metres < 160:
        return f"{int(round(metres * 3.28084 / 10) * 10)} ft"
    return f"{metres / 1609.34:.1f} mi"


def _case_place_phrase(case: dict[str, Any]) -> str:
    """"0.6 mi south" / "0.6 mi away" / "" — never implies a bearing the
    geometry did not give us."""
    distance = _miles(case.get("distance_m"))
    if not distance:
        return ""
    direction = _clean_string(case.get("direction"))
    return f"{distance} {direction}" if direction else f"{distance} away"


def _not_wired_entitlement_read(
    *,
    jurisdiction: str | None,
    jurisdiction_kind: str,
    subdivision_count: int | None,
    county_gis_source: str | None,
    manual_source_pointers: list[dict[str, str]],
    radius_m: int,
    subdivision_cases: list[dict[str, Any]] | None = None,
    largest: dict[str, Any] | None = None,
    total_lots: int | None = None,
) -> dict[str, Any]:
    """The read for a city with no machine-readable case feed.

    LEADS with the recorded plats actually found (that IS the answer to "what
    is being built near me"); the missing city feed is stated after, as the
    limit on the answer rather than as the answer."""
    label = jurisdiction or "This jurisdiction"
    pointer_labels = _manual_pointer_labels(manual_source_pointers)
    pointer_text = (
        " and ".join(pointer_labels)
        if pointer_labels else "the city planning portal and recent agendas"
    )
    radius_mi = round(radius_m / 1609.34, 1)
    cases = subdivision_cases or []
    count = subdivision_count if subdivision_count is not None else len(cases)
    has_county_background = subdivision_count is not None

    if has_county_background and count:
        nearest = cases[0] if cases else {}
        nearest_name = _clean_string(nearest.get("name")) or "the nearest plat"
        place = _case_place_phrase(nearest)
        nearest_phrase = f"{nearest_name}, {place}" if place else nearest_name
        lead = (
            f"{count} recorded subdivision plat{'' if count == 1 else 's'} "
            f"within {radius_mi} mi — nearest is {nearest_phrase}."
        )
        if largest and largest.get("lot_count"):
            largest_place = _case_place_phrase(largest)
            largest_name = _clean_string(largest.get("name")) or "one plat"
            lead += (
                f" The largest is {largest_name} at "
                f"{largest['lot_count']} lots"
                + (f", {largest_place}" if largest_place else "")
                + "."
            )
        if total_lots:
            lead += (
                f" About {total_lots:,} lots are platted within "
                f"{radius_mi} mi of this site."
            )
        headline = "Recorded development nearby"
        broker_read = (
            lead + " These are plats filed with the county — they show what "
            "has been approved to build around this land, not what is "
            f"approved on it. {label} publishes its live case list only as "
            "web pages, so an active rezoning or site plan filed in the last "
            "few weeks may not appear above."
        )
        activity_summary = (
            f"{count} recorded county plat{'' if count == 1 else 's'} within "
            f"{radius_mi} mi"
            + (f"; about {total_lots:,} lots" if total_lots else "")
            + "."
        )
        activity_relevance = "nearby_recorded_activity"
    elif has_county_background:
        headline = "No recorded plats nearby"
        broker_read = (
            f"The county's plat records show nothing recorded within "
            f"{radius_mi} mi of this site. {label} publishes its live case "
            "list only as web pages, so this is not proof that nothing is in "
            "progress — check the planning page below."
        )
        activity_summary = f"No recorded county plats within {radius_mi} mi."
        activity_relevance = "nearby_none_found"
    else:
        headline = "Manual city entitlement check required"
        broker_read = (
            f"{label} publishes its development cases only as web pages, and "
            "no county plat records were reachable for this site. Treat this "
            "as a manual city case check, not as a clean no-case result."
        )
        activity_summary = "No structured city entitlement feed is wired."
        activity_relevance = "manual_required"

    return {
        "headline": headline,
        "broker_read": broker_read,
        "why_it_matters": (
            "What is approved to be built around a site sets the competition "
            "it will sell into, the traffic and utility load it shares, and "
            "how fast the area's prices move. Recorded plats show what is "
            "already locked in; a live city case can change that within "
            "weeks."
        ),
        "next_step": (
            f"Search {pointer_text} using the parcel address, legal "
            "description, and nearby street names; pull any active or recent "
            "rezoning, site-development, subdivision, P&Z, or council items "
            "within the diligence radius."
        ),
        "activity_summary": activity_summary,
        "activity_relevance": activity_relevance,
        "raw_rows_role": (
            "primary_result" if activity_relevance == "nearby_recorded_activity"
            else "audit_only"
        ),
        "manual_check_required": True,
        "jurisdiction_kind": jurisdiction_kind,
        "county_gis_source": county_gis_source,
    }


def _programmatic_entitlement_read(
    *,
    jurisdiction: str,
    zoning_count: int,
    site_plan_count: int,
    radius_m: int,
) -> dict[str, Any]:
    total = zoning_count + site_plan_count
    if total:
        headline = "Nearby entitlement cases found"
        broker_read = (
            f"PropDesk queried {jurisdiction.title()}'s structured case feeds "
            f"and found {zoning_count} zoning case(s) and {site_plan_count} "
            "site-plan case(s) near the property."
        )
    else:
        headline = "Structured entitlement feed checked"
        broker_read = (
            f"PropDesk queried {jurisdiction.title()}'s structured case feeds "
            "and did not find nearby zoning or site-plan cases in the screened "
            "radius."
        )
    return {
        "headline": headline,
        "broker_read": broker_read,
        "why_it_matters": (
            "Nearby active cases can signal planned development, entitlement "
            "pressure, construction disruption, or future competitive supply."
        ),
        "next_step": (
            "Review any listed cases for relevance, then verify material items "
            "against the city's case portal because open-data feeds can lag "
            "official agenda action."
        ),
        "activity_summary": (
            f"{total} structured case record{'' if total == 1 else 's'} "
            f"within {radius_m}m."
        ),
        "activity_relevance": (
            "programmatic_case_result" if total else "programmatic_clear"
        ),
        "raw_rows_role": "primary_result",
        "manual_check_required": False,
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    radius_m: int = _DEFAULT_RADIUS_M,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    context = _jurisdiction_context(identity, config)
    jurisdiction = _resolve_programmatic_jurisdiction(identity, config)
    if jurisdiction is None:
        return _jurisdiction_not_wired_result(
            identity,
            config=config,
            context=context,
            radius_m=radius_m,
            session=session,
        )

    sess = http_session(session)

    cases_by_kind: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    successful = 0

    for display_name, url, kind in _LAYERS_BY_JURISDICTION[jurisdiction]:
        records, error = _query_socrata(
            sess, url, identity.lat, identity.lng, radius_m,
        )
        if error:
            errors.append(f"{display_name}: {error}")
            continue
        successful += 1
        cases_by_kind.setdefault(kind, []).extend(records)

    if successful == 0:
        confidence = DataConfidenceStatus.UNKNOWN
        status = "unknown"
    else:
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        any_cases = any(cases_by_kind.values())
        status = "mixed" if any_cases else "clear"

    zoning_case_count = len(cases_by_kind.get("zoning_case", []))
    site_plan_case_count = len(cases_by_kind.get("site_plan_case", []))
    advisory = _build_advisory(cases_by_kind, radius_m, jurisdiction)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=(
            CoverageStatus.COVERED if successful else CoverageStatus.PARTIAL
        ),
        normalized_facts={
            "status": status,
            "source_status": "queried" if successful else "query_failed",
            "county": context.get("county"),
            "jurisdiction": jurisdiction,
            "jurisdiction_kind": context.get("jurisdiction_kind"),
            "search_radius_m": radius_m,
            "zoning_cases": cases_by_kind.get("zoning_case", []),
            "site_plan_cases": cases_by_kind.get("site_plan_case", []),
            "entitlement_read": _programmatic_entitlement_read(
                jurisdiction=jurisdiction,
                zoning_count=zoning_case_count,
                site_plan_count=site_plan_case_count,
                radius_m=radius_m,
            ),
            **(
                {
                    "zoning_case_count": zoning_case_count,
                    "site_plan_case_count": site_plan_case_count,
                }
                if successful else {}
            ),
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PIPELINE_UPLOAD_TYPES),
            reason=(
                "Open-data case feeds lag real council action "
                "and may miss small filings. Upload an agenda "
                "packet, staff report, or notice letter for "
                "anything material."
            ),
        ),
    )


def _query_socrata(
    session: requests.Session, url: str,
    lat: float, lng: float, radius_m: int,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "$where": f"within_circle(location, {lat}, {lng}, {radius_m})",
        "$limit": 50,
    }
    try:
        resp = session.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return [], "unexpected response shape"
        return [_normalize_case(rec) for rec in data], None
    except Exception as exc:
        log.warning("Socrata case query failed (%s): %s", url, exc)
        return [], str(exc)[:200]


def _normalize_case(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_number": (
            record.get("case_number") or record.get("case_no")
            or record.get("permit_number") or record.get("folderrsn")
            or record.get("id") or ""
        ),
        "case_name": (
            record.get("case_name") or record.get("project_name")
            or record.get("name") or ""
        ),
        "case_type": (
            record.get("case_type") or record.get("sub_type") or ""
        ),
        "address": (
            record.get("site_address") or record.get("street_address")
            or record.get("address") or record.get("location_address")
            or ""
        ),
        "status": (
            record.get("detailed_status") or record.get("status")
            or record.get("case_status") or record.get("current_status")
            or ""
        ),
        "filed_date": (
            record.get("application_start_date")
            or record.get("filed_date") or record.get("submitted_date")
            or record.get("status_date") or ""
        ),
        "raw": record,
    }


def _build_advisory(
    cases_by_kind: dict[str, list[dict[str, Any]]],
    radius_m: int, jurisdiction: str,
) -> str:
    parts: list[str] = []
    z_count = len(cases_by_kind.get("zoning_case") or [])
    s_count = len(cases_by_kind.get("site_plan_case") or [])
    if z_count:
        parts.append(
            f"{z_count} zoning case(s) within {radius_m}m of "
            "the property. Review for impact on neighborhood "
            "context, density, or use mix."
        )
    if s_count:
        parts.append(
            f"{s_count} site plan case(s) within {radius_m}m. "
            "Review for proposed nearby development."
        )
    if not z_count and not s_count:
        parts.append(
            f"No active zoning or site-plan cases found within "
            f"{radius_m}m of the property in {jurisdiction.title()}'s "
            "open-data feed."
        )
    parts.append(
        "Open-data feeds lag real council action — verify "
        "anything material against the city's case portal."
    )
    return " ".join(parts)


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "source_status": "not_checked",
            "county": None,
            "jurisdiction": None,
            "jurisdiction_kind": "unknown",
            "search_radius_m": _DEFAULT_RADIUS_M,
            "advisory": (
                "Coordinates required for entitlement-pipeline "
                "scan. Upload an agenda packet or staff report."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for case feed query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PIPELINE_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Upload an agenda "
                "packet / staff report / notice letter."
            ),
        ),
    )


def _jurisdiction_not_wired_result(
    identity: PropertyIdentity,
    *,
    config: Any,
    context: dict[str, str | None],
    radius_m: int,
    session: requests.Session | None = None,
) -> AdapterResult:
    jurisdiction = context.get("jurisdiction")
    jurisdiction_kind = context.get("jurisdiction_kind") or "unknown"
    county = context.get("county") or _normalize_key(identity.county) or None
    jurisdiction_key = context.get("jurisdiction_key")
    label = jurisdiction or (
        f"{county.title()} County" if county else "this jurisdiction"
    )
    manual_source_pointers = _manual_source_pointers(
        jurisdiction_key,
        jurisdiction_kind=jurisdiction_kind,
        county=county,
    )
    cedar_park_signal = _has_cedar_park_signal(identity, config)
    if (
        cedar_park_signal
        and jurisdiction_key != "cedar_park"
        and jurisdiction_key == "unincorporated_williamson"
    ):
        manual_source_pointers = [
            *_MANUAL_SOURCE_POINTERS["cedar_park"],
            *manual_source_pointers,
        ]
    registry_sources = _registry_entitlement_sources(
        county=county,
        jurisdiction_key=jurisdiction_key,
    )
    registry_source_pointers = _registry_source_pointers(
        county=county,
        jurisdiction_key=jurisdiction_key,
    )
    if registry_source_pointers:
        manual_source_pointers = _dedupe_source_pointers([
            *registry_source_pointers,
            *manual_source_pointers,
        ])
    portal = _CASE_PORTALS.get(jurisdiction_key or "")
    advisory = (
        f"{jurisdiction or 'This city'} does not publish its live case list "
        "in a form PropDesk can read directly. Open the planning page linked "
        "below to look up rezonings, site plans, subdivisions, and recent "
        "meeting agendas by address."
    )
    facts: dict[str, Any] = {
        "status": "not_checked",
        "source_status": "not_wired_for_jurisdiction",
        "county": county,
        "jurisdiction": jurisdiction,
        "jurisdiction_kind": jurisdiction_kind,
        "search_radius_m": radius_m,
        "advisory": advisory,
        "entitlement_read": _not_wired_entitlement_read(
            jurisdiction=jurisdiction,
            jurisdiction_kind=jurisdiction_kind,
            subdivision_count=None,
            county_gis_source=None,
            manual_source_pointers=manual_source_pointers,
            radius_m=radius_m,
        ),
        "contradiction_flags": (
            ["cedar_park_signal_requires_city_check"]
            if cedar_park_signal and jurisdiction_key == "unincorporated_williamson"
            else []
        ),
    }
    if manual_source_pointers:
        facts["manual_source_pointers"] = manual_source_pointers
    if portal:
        facts["case_portal_name"] = portal["name"]
        facts["case_portal_url"] = portal["url"]
        facts["manual_instructions"] = portal["instructions"]

    city_enrichment = _city_entitlement_enrichment(
        session,
        jurisdiction_key=jurisdiction_key,
        lat=identity.lat,
        lng=identity.lng,
        radius_m=radius_m,
    )
    if city_enrichment is None:
        city_enrichment = _city_public_portal_enrichment(
            session,
            jurisdiction_key=jurisdiction_key,
            identity=identity,
        )
    city_entitlement_label = jurisdiction
    if city_enrichment is None and jurisdiction_kind == "unknown":
        candidate_key, city_enrichment = _candidate_city_entitlement_enrichment(
            session,
            county=county,
            lat=identity.lat,
            lng=identity.lng,
            radius_m=radius_m,
        )
        if candidate_key:
            city_entitlement_label = (
                candidate_key.replace("_", " ").title() + " area"
            )
            if not manual_source_pointers:
                manual_source_pointers = _manual_source_pointers(
                    candidate_key,
                    jurisdiction_kind="city",
                    county=county,
                )
                if manual_source_pointers:
                    facts["manual_source_pointers"] = manual_source_pointers
    city_source_checked = False
    official_registry_source_checked = False
    if city_enrichment is not None:
        facts.update(city_enrichment)
        city_source_checked = "city_entitlement_record_count" in city_enrichment
        if city_source_checked:
            count = int(city_enrichment["city_entitlement_record_count"])
            not_closed_count = int(
                city_enrichment["city_entitlement_not_closed_count"]
            )
            facts["source_status"] = "queried_city_public_layers"
            facts["status"] = "mixed" if count else "clear"
            if (
                city_enrichment.get("city_entitlement_source_kind")
                == "agenda_packet_screen"
            ):
                target_match_count = int(
                    city_enrichment.get(
                        "city_entitlement_target_match_count", 0,
                    )
                )
                meeting_count = int(
                    city_enrichment.get(
                        "city_entitlement_agenda_meetings_checked", 0,
                    )
                )
                facts["advisory"] = (
                    f"{city_enrichment['city_entitlement_source']} was queried. "
                    "PropDesk checked GovWell public portal metadata and "
                    "screened recent Manor P&Z agenda packets for entitlement "
                    "items. Agenda packet rows are a public screen, not a "
                    "parcel-geocoded current case certificate."
                )
                if city_enrichment.get("city_entitlement_error"):
                    facts["advisory"] += (
                        " One public sub-source returned an error; use the "
                        "manual pointers to verify the latest portal state."
                    )
                facts["entitlement_read"] = _city_agenda_entitlement_read(
                    jurisdiction=city_entitlement_label,
                    record_count=count,
                    target_match_count=target_match_count,
                    meeting_count=meeting_count,
                    project_thread_count=int(
                        city_enrichment.get(
                            "city_entitlement_project_thread_count", count,
                        )
                    ),
                    govwell_pz_present=bool(
                        city_enrichment.get("govwell_pz_record_type_present")
                    ),
                    public_pz_dashboard_present=bool(
                        city_enrichment.get(
                            "govwell_public_pz_dashboard_present"
                        )
                    ),
                    search_terms=list(
                        city_enrichment.get(
                            "city_entitlement_search_terms", [],
                        )
                    ),
                    manual_source_pointers=manual_source_pointers,
                )
            else:
                facts["advisory"] = (
                    f"{city_enrichment['city_entitlement_source']} was queried "
                    "for nearby city permit/project records. This is a public "
                    "screen, not a current entitlement certificate."
                )
                if city_enrichment.get("city_entitlement_feed_stale"):
                    facts["advisory"] += (
                        " The latest project year in the layer appears older than "
                        "the current diligence period, so current agendas/permit "
                        "intake still need verification."
                    )
                if city_enrichment.get("city_entitlement_candidate_only"):
                    facts["advisory"] += (
                        " The layer was checked as a nearby city project source; "
                        "it does not prove jurisdiction by itself."
                    )
                facts["entitlement_read"] = _city_entitlement_read(
                    jurisdiction=city_entitlement_label,
                    record_count=count,
                    not_closed_count=not_closed_count,
                    material_count=int(
                        city_enrichment["city_entitlement_material_count"]
                    ),
                    latest_year=city_enrichment.get(
                        "city_entitlement_latest_year"
                    ),
                    stale=bool(city_enrichment.get("city_entitlement_feed_stale")),
                    source_label=str(city_enrichment["city_entitlement_source"]),
                    manual_source_pointers=manual_source_pointers,
                    radius_m=radius_m,
                )
        elif city_enrichment.get("city_entitlement_error"):
            facts["advisory"] += (
                f" PropDesk also tried {city_enrichment['city_entitlement_source']} "
                "but the public city layer did not respond cleanly."
            )
    if city_enrichment is None and registry_sources is not None:
        source_registry, source_card = registry_sources
        official_registry_source_checked = True
        facts.update({
            "source_status": "official_source_registered",
            "status": (
                "not_published_by_jurisdiction"
                if source_card.mode == SourceMode.NOT_PUBLISHED
                else "official_source_available"
            ),
            "city_entitlement_source": (
                f"{source_registry.display_name} entitlement source registry"
            ),
            "city_entitlement_source_url": source_card.url,
            "city_entitlement_source_kind": "official_registry_source",
            "city_entitlement_source_mode": source_card.mode.value,
            "city_entitlement_source_note": source_card.note,
            "advisory": (
                f"PropDesk has an explicit Travis County source entry for "
                f"{source_registry.display_name}: "
                f"{_source_mode_label(source_card.mode)}. "
                "This is not a structured parcel-geocoded city feed; use the "
                "official pointers for current case verification."
            ),
        })
        facts["entitlement_read"] = _registry_entitlement_read(
            jurisdiction=jurisdiction,
            jurisdiction_display=source_registry.display_name,
            source_mode=source_card.mode,
            source_url=source_card.url,
            source_note=source_card.note,
            subdivision_count=None,
            county_gis_source=None,
            manual_source_pointers=manual_source_pointers,
            radius_m=radius_m,
        )
    # Try-first (§20.28): before settling for manual pointers, query the county
    # subdivisions GIS for nearby platting activity and merge it in. Keeps the
    # city manual pointers (city case feeds aren't wired) but proves PropDesk
    # actually checked a real public source first.
    enrichment = _county_gis_subdivision_enrichment(
        session, county=county or "", lat=identity.lat, lng=identity.lng,
        radius_m=radius_m,
    )
    if enrichment is not None:
        for key, value in enrichment.items():
            if key != "advisory_suffix":
                facts[key] = value
        facts["advisory"] = facts["advisory"] + enrichment["advisory_suffix"]
        if official_registry_source_checked and registry_sources is not None:
            source_registry, source_card = registry_sources
            facts["entitlement_read"] = _registry_entitlement_read(
                jurisdiction=jurisdiction,
                jurisdiction_display=source_registry.display_name,
                source_mode=source_card.mode,
                source_url=source_card.url,
                source_note=source_card.note,
                subdivision_count=enrichment["subdivision_case_count"],
                county_gis_source=enrichment["county_gis_source"],
                manual_source_pointers=manual_source_pointers,
                radius_m=radius_m,
            )
        elif not city_source_checked:
            facts["entitlement_read"] = _not_wired_entitlement_read(
                jurisdiction=jurisdiction,
                jurisdiction_kind=jurisdiction_kind,
                subdivision_count=enrichment["subdivision_case_count"],
                county_gis_source=enrichment["county_gis_source"],
                manual_source_pointers=manual_source_pointers,
                radius_m=enrichment.get("subdivision_radius_m") or radius_m,
                subdivision_cases=enrichment["subdivision_cases"],
                largest=enrichment.get("subdivision_largest"),
                total_lots=enrichment.get("subdivision_total_lots"),
            )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC
            if city_source_checked
            else (
                SourceAccessStatus.HTML_OR_PDF_ONLY
                if official_registry_source_checked or manual_source_pointers
                else SourceAccessStatus.JURISDICTION_NOT_COVERED
            )
        ),
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if city_source_checked or official_registry_source_checked
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.PARTIAL
            if city_source_checked or official_registry_source_checked
            else CoverageStatus.NOT_YET_WIRED
        ),
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_PIPELINE_UPLOAD_TYPES),
            reason=(
                (
                    f"Use the mapped official {label} source pointers and "
                    "upload agenda packets, staff reports, case screenshots, "
                    "or notice letters for anything material."
                )
                if official_registry_source_checked
                else (
                    f"No structured feed is wired for {label}. Use the manual "
                    "source pointers and upload agenda packets, staff reports, "
                    "case screenshots, or notice letters for anything material."
                )
            ),
        ),
    )


def _manual_source_pointers(
    jurisdiction_key: str | None,
    *,
    jurisdiction_kind: str,
    county: str | None,
) -> list[dict[str, str]]:
    if jurisdiction_key in _MANUAL_SOURCE_POINTERS:
        return list(_MANUAL_SOURCE_POINTERS[jurisdiction_key])
    if (
        jurisdiction_kind == "unincorporated"
        and county == "williamson"
    ):
        return list(_MANUAL_SOURCE_POINTERS["unincorporated_williamson"])
    registry_pointers = _registry_source_pointers(
        county=county,
        jurisdiction_key=jurisdiction_key,
    )
    if registry_pointers:
        return registry_pointers
    portal = _CASE_PORTALS.get(jurisdiction_key or "")
    if not portal:
        return []
    return [
        {
            "label": portal["name"],
            "url": portal["url"],
            "note": portal["instructions"],
        },
    ]


__all__ = ["SOURCE_ID", "fetch"]
