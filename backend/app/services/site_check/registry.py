"""Adapter registry — the activation matrix in code form.

Per docs/specs/public_data.md §4 — every legacy_services adapter
maps to one row here. `active_in_v0=True` means the orchestrator
calls the adapter's real `fetch_*` function. False means the
orchestrator returns the stub `not_yet_wired` result without
invoking the adapter.

Adding a new value: lift the legacy_services adapter file into
`adapters/<source_id>.py`, flip `active_in_v0=True` here, add
the per-adapter test fixture to the smoke matrix. Activation PR
per spec §15.1 governance.

Removing a value: never. Even deprecated adapters stay here
returning `coverage_status="not_yet_wired"` so old rows in
public_intel_runs.adapter_results continue to make sense.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterRegistration:
    """Single row of the activation matrix.

    `provider` + `service` are the DEFAULT attribution for the
    site_check_provider_usage rows this source's requests produce —
    the orchestrator sets them per dispatch and the §8.6 request
    collector applies them to any host it does not route itself
    (the identity-bearing upstreams — geocoder tiers, ArcGIS Places —
    always bill under their real provider instead). Free adapters
    still write rows, at cost 0, so this is always populated.

    `needed_by_manor` is informational — drives the v0 activation
    decision, recorded so future readers see why an adapter is
    active or not.
    """

    source_id: str
    legacy_origin: str  # human-readable port note
    active_in_v0: bool
    provider: str
    service: str
    needed_by_manor: bool
    # Version of the reusable persisted result contract. Version 0 remains
    # compatible with snapshots created before central schema stamping.
    result_schema_version: int = 0
    notes: str = ""
    # Per-provider concurrency group for the parallel orchestrator
    # (per docs/specs/public_intel_parallel_fetch.md §1.4). Defaults
    # to None — the orchestrator falls back to `provider` then to the
    # "default" cap. Set this when multiple distinct `provider` values
    # share a real upstream (e.g., several jurisdiction adapters all
    # hitting Socrata).
    provider_concurrency_group: str | None = None
    # States this source covers. Empty means everywhere. A Texas-only
    # source is ("TX",): outside Texas the orchestrator never calls it and
    # records "Not connected for <State> yet" instead.
    states: tuple[str, ...] = ()


# Single source of truth for activation. Mirrors the §4 matrix
# row-for-row. Counts as of 2026-07-13: 34 active,
# 3 wired-inactive (txdot_county_vmt, eia_state_fuel_sales,
# arcgis_places as the demoted OSM-fallback-only path), 0 pending-POC.
REGISTRY: dict[str, AdapterRegistration] = {
    # ── ACTIVE in v0 (34) ────────────────────────────────────
    "arcgis_geocode": AdapterRegistration(
        source_id="arcgis_geocode",
        legacy_origin="NEW (replaces Nominatim)",
        active_in_v0=True,
        provider="arcgis",
        service="geocode_stored",
        needed_by_manor=True,
        notes=(
            "Per-project lat/lng cache; stored geocoding $4/1K no free tier. "
            "Google Geocoding is the FINAL fallback tier (Admin 2026-08-01). "
            "Metering is per REQUEST, not per resolution (§8.6): each tier "
            "this source attempts writes its own row — nominatim, arcgis, and "
            "google/geocode at settings.google_geocode_cost_micro_per_call — "
            "so a tier that was called and missed is still charged."
        ),
    ),
    "jurisdiction_resolver": AdapterRegistration(
        source_id="jurisdiction_resolver",
        legacy_origin="NEW (point-in-polygon jurisdiction verdict)",
        active_in_v0=True,
        provider="arcgis",
        service="jurisdiction_resolve",
        needed_by_manor=False,
        result_schema_version=1,
        notes=(
            "Wave 0.5 hard prerequisite (run after geocode, before city-aware "
            "adapters). City-limits/ETJ/unincorporated verdict from county GIS; "
            "per docs/specs/jurisdiction_resolver.md. Excluded from the parallel "
            "fan-out by the orchestrator like arcgis_geocode."
        ),
    ),
    "fema_nfhl": AdapterRegistration(
        source_id="fema_nfhl",
        legacy_origin="core/public_intel.py::_fetch_fema_flood_zone",
        active_in_v0=True,
        provider="fema",
        service="fetch",
        needed_by_manor=True,
        result_schema_version=3,
    ),
    "txdot_aadt": AdapterRegistration(
        source_id="txdot_aadt",
        states=("TX",),
        legacy_origin="core/public_intel.py::_fetch_txdot_traffic",
        active_in_v0=True,
        provider="txdot",
        service="fetch",
        needed_by_manor=True,
        result_schema_version=1,
        notes="Manor AADT = 38,651 — calibration anchor",
    ),
    "txdot_aadt_history": AdapterRegistration(
        source_id="txdot_aadt_history",
        states=("TX",),
        legacy_origin="NEW (historical TxDOT traffic-count stations)",
        active_in_v0=True,
        provider="txdot",
        service="aadt_history",
        needed_by_manor=False,
        notes=(
            "Nearby annual count-station history is reusable Public Data; "
            "trend screening does not establish driveway capture or site demand."
        ),
    ),
    "txdot_county_vmt": AdapterRegistration(
        source_id="txdot_county_vmt",
        states=("TX",),
        legacy_origin="NEW (gas station feasibility top-down allocator)",
        active_in_v0=False,
        provider="txdot",
        service="county_vmt",
        needed_by_manor=False,
        notes=(
            "FY2024 DISCOS workbook; on-system daily vehicle miles only, "
            "used as a weak county allocation proxy for gas feasibility."
        ),
    ),
    "txdot_projects": AdapterRegistration(
        source_id="txdot_projects",
        states=("TX",),
        legacy_origin="core/public_intel.py::_fetch_txdot_projects",
        active_in_v0=True,
        provider="txdot",
        service="fetch",
        needed_by_manor=True,
        result_schema_version=1,
    ),
    "txdot_roadway": AdapterRegistration(
        source_id="txdot_roadway",
        states=("TX",),
        legacy_origin=(
            "core/public_intel.py::_fetch_txdot_roadway_info "
            "+ fetch_txdot_roadway_extended"
        ),
        active_in_v0=True,
        provider="txdot",
        service="fetch",
        needed_by_manor=True,
        result_schema_version=1,
        notes=(
            "Screens ROW_MIN, median/access-control, lane-width, and "
            "turn-lane indicators when TxDOT inventory populates them; "
            "stacking/storage length and site-specific driveway approval "
            "still require plan/TIA/as-built or field evidence."
        ),
    ),
    "txdot_bridges": AdapterRegistration(
        source_id="txdot_bridges",
        states=("TX",),
        legacy_origin="core/public_intel.py::fetch_txdot_bridges",
        active_in_v0=True,
        provider="txdot",
        service="fetch",
        needed_by_manor=True,
        notes="Truck-traffic scorer needs bridge weight restrictions",
    ),
    "census_acs": AdapterRegistration(
        source_id="census_acs",
        legacy_origin="core/demographic_intel.py",
        active_in_v0=True,
        provider="census",
        service="acs_5yr",
        needed_by_manor=True,
        notes="1/3/5 mi rings; needs CRE_CENSUS_API_KEY env var",
    ),
    "osm_overpass": AdapterRegistration(
        source_id="osm_overpass",
        legacy_origin="core/poi_intel.py",
        active_in_v0=True,
        provider="osm",
        service="fetch",
        needed_by_manor=True,
        notes="POI baseline; brand attribution patchy (10-20% mistag)",
    ),
    "eia_state_fuel_sales": AdapterRegistration(
        source_id="eia_state_fuel_sales",
        legacy_origin="NEW (gas station feasibility state anchor)",
        active_in_v0=False,
        provider="eia",
        service="state_fuel_sales",
        needed_by_manor=False,
        notes=(
            "Uses current EIA SEDS annual motor-gasoline consumption. "
            "Prime Supplier Texas series is stale/discontinued at 2022-03."
        ),
    ),
    "arcgis_places": AdapterRegistration(
        source_id="arcgis_places",
        legacy_origin="NEW (Manor POC gated cv2 brand path)",
        active_in_v0=False,  # demoted 2026-05-04 — OSM is primary, ArcGIS is fallback only
        provider="arcgis",
        service="places_nearby",
        needed_by_manor=False,
        notes=(
            "Demoted from active 2026-05-04 after live coverage "
            "comparison: OSM Overpass returns 416 POIs at Manor "
            "(including IMST RSA fuel competitors with brand=* "
            "tags) vs ArcGIS 200 (mostly non-CRE noise — recording "
            "studios, polo clubs, painters). ArcGIS Places is now "
            "called ONLY by osm_overpass.fetch as a fallback when "
            "OSM returns empty + errors (legacy_services pattern, "
            "substituting ArcGIS for Google for cost — $0.50/1K "
            "ArcGIS vs $5/1K Google geocoding equivalent)."
        ),
    ),
    "austin_zoning": AdapterRegistration(
        source_id="austin_zoning",
        states=("TX",),
        legacy_origin=(
            "core/zoning_fetchers.py::fetch_austin_zoning "
            "(via fetch_zoning_intel dispatcher)"
        ),
        active_in_v0=True,
        provider="austin_socrata",
        service="fetch",
        needed_by_manor=False,
        result_schema_version=1,
        notes=(
            "Generic jurisdiction-aware dispatcher: configured official city "
            "sources return parcel zoning inside city limits; ETJ and "
            "unincorporated verdicts never invent a municipal district."
        ),
    ),
    "tcad_parcel": AdapterRegistration(
        source_id="tcad_parcel",
        legacy_origin="core/public_intel.py::_fetch_travis_cad",
        active_in_v0=True,
        provider="tcad",
        service="fetch",
        needed_by_manor=True,
        result_schema_version=1,
        notes="Direct lookup via property_tax_id; no auto-derivation",
    ),

    # ── ACTIVATED FROM POC (1) ───────────────────────────────
    "public_aerial_imagery": AdapterRegistration(
        source_id="public_aerial_imagery",
        legacy_origin="NEW (TX-public; not paid map tiles)",
        active_in_v0=True,  # graduated 2026-05-31 per §13 gate-4 live probe
        provider="usgs_orthoimagery",
        service="orthoimagery_chip",
        needed_by_manor=False,
        notes=(
            "Public-domain only (USGS National Map / NAIP). Activated "
            "2026-05-31 after the live probe in "
            "docs/specs/_poc/public_aerial_imagery_2026-05-04.md "
            "(200 image/jpeg, anonymous, $0). Coordinate-only → parallel "
            "Wave 1. Returns a deterministic exportImage URL; the browser "
            "renders it. NO paid tiles, NO cached bytes in v0."
        ),
    ),
    "usgs_elevation": AdapterRegistration(
        source_id="usgs_elevation",
        legacy_origin="NEW (USGS 3DEP EPQS parcel screening)",
        active_in_v0=True,
        provider="usgs",
        service="elevation_point_query",
        needed_by_manor=False,
        notes=(
            "Free interpolated DEM samples for useful map context; "
            "screening only, never survey-grade elevation."
        ),
    ),

    # ── ACTIVE LONG-TAIL / WIRED-INACTIVE REMAINDER ──────────
    # Each is a registry stub — adapter file is a stub that
    # raises AdapterInactiveError if forced. Activation per
    # spec §15.1 governance: copy legacy_services adapter file +
    # flip active_in_v0=True + add smoke fixtures + run Manor
    # matrix to confirm no regressions.
    "txdot_extended": AdapterRegistration(
        source_id="txdot_extended",
        states=("TX",),
        legacy_origin="fetch_txdot_all_projects + fetch_txdot_crash_data",
        active_in_v0=True,
        provider="txdot",
        service="fetch",
        needed_by_manor=False,
        notes=(
            "Synthesizes AADT/projects/roadway/bridges from prior adapter "
            "results; CRIS crash metrics require authorized public-extract "
            "ingestion and surface as credentials_not_configured until wired."
        ),
    ),
    "wcad_parcel": AdapterRegistration(
        source_id="wcad_parcel",
        states=("TX",),
        legacy_origin="NEW (Williamson CAD public ArcGIS parcel layer)",
        active_in_v0=True,
        provider="wcad",
        service="fetch",
        needed_by_manor=False,
        result_schema_version=1,
        notes=(
            "Exact parcel only when point intersects one WCAD polygon "
            "or user supplies PIN; intersection addresses return "
            "nearby candidates without auto-picking."
        ),
    ),
    "drainage_screen": AdapterRegistration(
        source_id="drainage_screen",
        legacy_origin=(
            "new capability — docs/specs/feasibility_screens_free_data.md §4 "
            "(SPEC_APPROVED 2026-07-15)"
        ),
        active_in_v0=True,
        provider="usgs_3dep",
        service="drainage_screen",
        needed_by_manor=False,
        result_schema_version=1,
        notes=(
            "3DEP DEM on a metric EPSG:5070 grid + D8 screening; NHD "
            "cross-check fetched by the adapter itself; Atlas 14 context. "
            "CONUS-only v1."
        ),
    ),
    "wetlands_nwi": AdapterRegistration(
        source_id="wetlands_nwi",
        legacy_origin="public_intel_adapters/wetlands.py",
        active_in_v0=True,
        provider="usfws_nwi",
        service="fetch",
        needed_by_manor=False,
        result_schema_version=2,
    ),
    "twdb_ccn": AdapterRegistration(
        source_id="twdb_ccn",
        states=("TX",),
        legacy_origin="public_intel_adapters/ccn.py",
        active_in_v0=True,
        provider="twdb",
        service="fetch",
        needed_by_manor=False,
        # v2 (2026-07-22): sewer source moved to PUCT after TWDB removed its
        # sewer service (silent 404); facts gain wastewater_nearest. Cached v1
        # results predate working sewer screening and must recompute.
        result_schema_version=2,
    ),
    "electric_power_infrastructure": AdapterRegistration(
        source_id="electric_power_infrastructure",
        legacy_origin="data_center_public_sources.py HIFLD power path",
        active_in_v0=True,
        provider="hifld",
        service="electric_grid_proximity",
        needed_by_manor=False,
        notes=(
            "Free transmission-line and substation proximity for every property; "
            "never implies three-phase service, capacity, headroom, easement, "
            "interconnection, cost, or a right to connect."
        ),
    ),
    "district_overlays": AdapterRegistration(
        source_id="district_overlays",
        legacy_origin="public_intel_adapters/district_overlays.py",
        active_in_v0=True,
        provider="various_district_layers",
        service="fetch",
        needed_by_manor=False,
        result_schema_version=1,
    ),
    "future_land_use": AdapterRegistration(
        source_id="future_land_use",
        states=("TX",),
        legacy_origin="public_intel_adapters/future_land_use.py",
        active_in_v0=True,
        provider="various_jurisdiction_flu",
        service="fetch",
        needed_by_manor=False,
    ),
    "environmental_screening": AdapterRegistration(
        source_id="environmental_screening",
        legacy_origin="public_intel_adapters/environmental.py",
        active_in_v0=True,
        provider="tceq_epa",
        service="fetch",
        needed_by_manor=False,
        # v1 (2026-07-22): tceq_screen contamination layers +
        # tceq_address_registry + manual_records_links. Bumping invalidates
        # pre-expansion results in the 100-day refresh-reuse window — a
        # reused old-schema result rendered the OLD card on prod even after
        # deploy (Admin, 2026-07-22).
        result_schema_version=1,
        notes="TCEQ LPST + EPA ECHO + contamination layers + regulatory history",
    ),
    "entitlement_pipeline": AdapterRegistration(
        source_id="entitlement_pipeline",
        states=("TX",),
        legacy_origin="public_intel_adapters/entitlement_pipeline.py",
        active_in_v0=True,
        provider="various",
        service="fetch",
        needed_by_manor=False,
    ),
    "zoning_standards": AdapterRegistration(
        source_id="zoning_standards",
        states=("TX",),
        legacy_origin="public_intel_adapters/zoning_standards.py",
        active_in_v0=True,
        provider="various_jurisdiction_codes",
        service="fetch",
        needed_by_manor=False,
    ),
    "fire_district": AdapterRegistration(
        source_id="fire_district",
        legacy_origin=(
            "public_intel_adapters/fire_district.py + fire_intel.py"
        ),
        active_in_v0=True,
        provider="various_esd",
        service="fetch",
        needed_by_manor=False,
    ),
    "soil_intel": AdapterRegistration(
        source_id="soil_intel",
        legacy_origin="public_intel_adapters/soil_intel.py",
        active_in_v0=True,
        provider="usda_nrcs",
        service="fetch",
        needed_by_manor=False,
    ),
    "hydro_intel": AdapterRegistration(
        source_id="hydro_intel",
        legacy_origin="public_intel_adapters/hydro_intel.py",
        active_in_v0=True,
        provider="usgs_nhd",
        service="fetch",
        needed_by_manor=False,
    ),
    "historic_intel": AdapterRegistration(
        source_id="historic_intel",
        states=("TX",),
        legacy_origin="public_intel_adapters/historic_intel.py",
        active_in_v0=True,
        provider="thc",
        service="fetch",
        needed_by_manor=False,
    ),
    "schools_intel": AdapterRegistration(
        source_id="schools_intel",
        states=("TX",),
        legacy_origin="public_intel_adapters/schools_intel.py",
        active_in_v0=True,
        provider="tea",
        service="fetch",
        needed_by_manor=False,
    ),
    "tax_intel": AdapterRegistration(
        source_id="tax_intel",
        states=("TX",),
        legacy_origin="public_intel_adapters/tax_intel.py",
        active_in_v0=True,
        provider="various_appraisal_districts",
        service="fetch",
        needed_by_manor=False,
    ),
    "transit_intel": AdapterRegistration(
        source_id="transit_intel",
        states=("TX",),
        legacy_origin="public_intel_adapters/transit_intel.py",
        active_in_v0=True,
        provider="capmetro_etc",
        service="fetch",
        needed_by_manor=False,
    ),
    "broadband_intel": AdapterRegistration(
        source_id="broadband_intel",
        legacy_origin="public_intel_adapters/broadband_intel.py",
        active_in_v0=True,
        provider="fcc",
        service="fetch",
        needed_by_manor=False,
    ),
    "plats_intel": AdapterRegistration(
        source_id="plats_intel",
        legacy_origin="public_intel_adapters/plats_intel.py",
        active_in_v0=True,
        provider="county_clerks",
        service="fetch",
        needed_by_manor=False,
    ),
    "site_constraints": AdapterRegistration(
        source_id="site_constraints",
        legacy_origin="NEW (consolidated public site-constraints resolution)",
        active_in_v0=True,
        provider="public_site_constraints",
        service="resolve",
        needed_by_manor=False,
        # v4 (2026-07-22): material_findings + degraded_sources. Same reuse
        # rationale as environmental_screening v1.
        result_schema_version=4,
        notes=(
            "Runs after the ordinary Public Data adapters. Consolidates utility, "
            "access, environmental, regulatory, district, market, easement, and "
            "provisional-envelope facts; supplements them with free HIFLD, TCEQ, "
            "USFWS, and OSM screens. Non-public engineering/title facts remain "
            "explicit verification requirements."
        ),
    ),
}


def active_source_ids() -> list[str]:
    """Source IDs the orchestrator should call for real on
    every fetch. Order is dispatch order — adapters with
    coordinate-only deps run before adapters that need
    jurisdiction/parcel data."""
    return [
        sid for sid, reg in REGISTRY.items()
        if reg.active_in_v0
    ]


def all_source_ids() -> list[str]:
    """Every source_id in the registry — active + inactive +
    pending. The orchestrator iterates this so inactive adapters
    contribute their `not_yet_wired` result to the run, never
    silently disappear."""
    return list(REGISTRY.keys())


def get(source_id: str) -> AdapterRegistration:
    return REGISTRY[source_id]


def result_schema_version(source_id: str) -> int:
    """Return the registry-owned reusable result contract version."""
    # Tests and integrations may provide legacy lightweight registrations.
    value = getattr(get(source_id), "result_schema_version", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"Invalid result_schema_version for {source_id!r}: {value!r}"
        )
    return value


__all__ = [
    "REGISTRY",
    "AdapterRegistration",
    "active_source_ids",
    "all_source_ids",
    "get",
    "result_schema_version",
]
