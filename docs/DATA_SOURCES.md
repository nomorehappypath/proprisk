# Data sources

Every source PropRisk queries for a lookup, and the three AI steps that
read and summarize what the sources found. This is the registry at
`backend/app/services/site_check/registry.py`; "(off)" means the source is
switched off there. See `docs/ARCHITECTURE.md` for how a lookup runs these,
and the Settings → "Where PropRisk works" screen (chapter 1.8 of SPEC.md)
for which counties and cities each one actually covers today.

| Source id | Returns | Services called | Key needed | Coverage |
|---|---|---|---|---|
| arcgis_geocode | address → location, county | OpenStreetMap Nominatim, then ArcGIS, then Google | arcgis, google_maps (optional) | US search, assumes Texas |
| jurisdiction_resolver | city limits / ETJ / unincorporated | Travis, Williamson, Bastrop county GIS; ArcGIS Online | — | Travis, Williamson, Hays, Bastrop |
| fema_nfhl | flood zone, LOMA/LOMR | hazards.fema.gov | — | nationwide |
| txdot_aadt | traffic counts | TxDOT ArcGIS | — | Texas |
| txdot_aadt_history | historical traffic counts | TxDOT ArcGIS | — | Texas |
| txdot_county_vmt (off) | county vehicle miles | txdot.gov | — | Texas |
| txdot_projects | planned and active road projects | TxDOT ArcGIS, ftp.txdot.gov | — | Texas |
| txdot_roadway | road class, lanes, speed, right-of-way | TxDOT ArcGIS | — | Texas |
| txdot_bridges | nearby bridges and condition | TxDOT ArcGIS | — | Texas |
| census_acs | demographics in 1/3/5-mile rings | api.census.gov | census | Texas |
| osm_overpass | nearby places, 16 categories | overpass-api.de (ArcGIS Places fallback) | arcgis for fallback | worldwide |
| eia_state_fuel_sales (off) | state fuel use | eia.gov | — | Texas |
| arcgis_places (off; fallback only) | nearby places | places-api.arcgis.com | arcgis | nationwide |
| austin_zoning | city zoning district | city GIS servers | — | 13 cities |
| tcad_parcel | parcel shape and acreage from a parcel ID | county appraisal and TxGIO layers | — | Travis, Hays, Bastrop, Blanco, Texas-wide layer |
| public_aerial_imagery | modern aerial photo | basemap.nationalmap.gov | — | nationwide |
| usgs_elevation | ground elevations | epqs.nationalmap.gov | — | nationwide |
| txdot_extended | transportation summary | (built from other sources) | — | Texas |
| wcad_parcel | Williamson parcel | ArcGIS Online | — | Williamson |
| drainage_screen | drainage and ponding screen, storm context | USGS elevation and hydrography, NOAA Atlas 14 | — | lower 48 states |
| wetlands_nwi | wetlands | USFWS / USGS | — | nationwide |
| twdb_ccn | water/sewer provider, nearest sewer | TWDB, TCEQ | — | Texas |
| electric_power_infrastructure | nearest power line and substation | HIFLD | — | nationwide |
| district_overlays | subdivision, MUD/WCID, ESD/PID/TIRZ, school district | county GIS, TCEQ | — | Travis, Williamson, Hays, Bastrop; MUD/WCID Texas-wide |
| future_land_use | future land use | city GIS servers | — | 8 cities |
| environmental_screening | leaking tanks, EPA facilities, contamination layers | TCEQ, EPA ECHO, data.texas.gov | — | Texas + nationwide |
| entitlement_pipeline | nearby zoning and site-plan cases, subdivisions, portal links | Austin open data, Manor GovWell, Cedar Park, San Marcos, county GIS | — | Austin, Manor, Cedar Park, San Marcos; links for about 20 cities |
| zoning_standards | setbacks, height, permitted uses | (curated files) | — | 10 cities |
| fire_district | nearest fire station, ESD | county and city GIS | — | Travis, Williamson, Bastrop |
| soil_intel | soil map units | NRCS Soil Data Access | — | nationwide |
| hydro_intel | named streams within about 500 m | USGS hydrography | — | nationwide |
| historic_intel | historic districts and landmarks | ArcGIS Online | — | Austin/Travis; THC link elsewhere |
| schools_intel | schools within about 5 miles | TEA | — | Texas |
| tax_intel | district debt-report links | data.texas.gov | — | Texas |
| transit_intel | bus stops and rail | Capital Metro ArcGIS | — | Travis, Williamson |
| broadband_intel | FCC broadband map link | — | — | nationwide (link) |
| plats_intel | plats, clerk lookup steps, recorded-plat numbers by web search | county GIS, Williamson clerk, bing.com result pages (no key) / Brave API / Bing API (retired Aug 2025) | brave_search, bing_search (optional) | Hays, Travis, Williamson, Bastrop, Caldwell; clerk portals also Burnet, Blanco |
| site_constraints | pipelines, Edwards Aquifer, critical habitat, infrastructure | DOE NETL, ArcGIS Online, Overpass mirrors | — | nationwide; aquifer Texas |
| historical_aerials (AI step) | aerial photos 1981–2022 + AI reading of past land use | TxGIO imagery, OpenAI | openai | Texas |
| entitlement_reader (AI step) | AI read of city planning pages, every case re-checked | OpenAI web search, city pages | openai | cities with a case portal |
| narratives (AI step) | plain-English summary per card | OpenAI | openai | every card |

## A state's or county's own sources

Outside Texas, a card's slot can be answered by the state's or the county's own
source (`backend/app/regions/<state>/sources/`, recipe in `docs/EXTENDING.md`,
Job D). All need no key. Colorado and Denver today:

| Slot | Colorado / Denver source | Data as of |
|---|---|---|
| txdot_aadt | FHWA HPMS Colorado 2025 (geo.dot.gov); DRCOG 2021–2025 counts | 2020–2025 |
| txdot_aadt_history | CDOT AADT, all years (state highways) | 1997–2025 |
| txdot_roadway | FHWA HPMS Colorado 2025 | 2025 |
| txdot_projects | CDOT GoSheet projects and Ten-Year Plan | layer edit date |
| txdot_bridges | National Bridge Inventory (geo.dot.gov); CDOT condition ratings and weight limits | 2023–2026 |
| transit_intel (Denver) | RTD bus stops and light-rail stations (CDOT-hosted) | 2021–2022 |
| austin_zoning, zoning_standards, future_land_use, entitlement_pipeline, historic_intel (Denver) | Denver Open Data: zoning, Denver Zoning Code (curated), Blueprint Denver, rezonings, site plans, permits, landmarks | each layer's edit date |
| twdb_ccn | EPA water system boundaries; Denver Water; Metro Water Recovery; DOLA districts; Denver sanitary mains | 2022–2026 |
| schools_intel | Census school districts; CDE school locations; Urban Institute enrollment | 2026 |
| tax_intel | DOLA property tax map | 2025 |
| state_environmental_records | Colorado OPS storage tanks and releases; CDPHE Superfund, RCRA corrective action, institutional controls, solid waste, closed landfills, brownfields | 2026 |
| historical_aerials (Denver) | DRCOG aerial photography, 2022 (CC BY 3.0) | 2022 |

## When a source is slow or down

Unchanged from the source it was ported from. Values live in
`backend/app/config/settings.py`:

- Every outside call waits at least 60 seconds
  (`public_intel_http_timeout_seconds`) and retries connection errors and
  HTTP 429/5xx twice with backoff
  (`public_intel_http_backoff_base_seconds`, honouring `Retry-After`).
  TxDOT calls use 4 attempts of up to 90 seconds. Other limits: water/sewer
  queries 25s, historical aerials 25s, entitlement checks 20s × 2.
- Up to 8 sources run in parallel (`public_intel_max_workers`), at most 2
  TxDOT and 4 others at a time (`public_intel_provider_concurrency_caps`).
  A slow source holds the lookup until its own retries run out — there is
  no overall time limit per source.
- One failed source fails only its own entry: the orchestrator catches it,
  stores it as failed and partial, and moves on; the result page offers
  "retry" for it. After an HTTP 429 that source pauses for 300 seconds
  (`public_intel_rate_limit_cooldown_seconds`).
- Only an address that no geocoder can place fails the whole lookup. The AI
  steps never fail a lookup: a missing key, the monthly cap, a bad key or
  an OpenAI error each leaves the card without its AI part.
- Saved source answers are reused for 180 days in "check" mode
  (`site_check_freshness_days`).
