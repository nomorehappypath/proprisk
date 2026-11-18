# Spec: let anyone add a county or a state to PropRisk (with NMHP)

Status: Codex PASS on the whole spec, 2026-11-17: sections 1–2.4 in round 3, and
section 2.5 (standard jobs and formats) in round 2. Phase A done 2026-11-17
(Codex PASS in round 2; built by hand because NMHP was busy, on the owner's word).
Phase C built 2026-11-17 by hand on the owner's word, with Denver, Colorado as its
test (Codex review in progress). Phases B onward are meant to be built by NMHP. Phase B was built by hand, on the
owner's word, as was Phase C.
Owner: Gerard. Builder: NMHP (NoMoreHappyPath).

## In one paragraph

PropRisk works today for seven Texas counties. We keep everything that works and
repackage the county- and state-specific parts so that **adding a county is filling
in one data file**, and **adding a state is one folder plus that state's own data
sources**. We also put a plain step-by-step recipe in the repo, written for an AI
build tool to follow. Someone who wants their county opens PropRisk in NMHP and types
"Add Comal County, Texas to PropRisk". NMHP reads the recipe, writes the
requirements, fills in the file, runs the one check command, and hands back a
pull request with proof. No technology degree needed. Any other AI coding tool
(Claude Code, Codex, Cursor) can follow the same recipe.

PropRisk stays a single-user app on one PC. Nothing in this spec adds servers,
accounts, plugins or a region database.

## 1. The problem (what the code does today)

- **County knowledge is scattered.** Each source file keeps its own private table
  of "which map service to call for which county". To add one county today you
  must find and edit about ten files, some over 3,000 lines:

  | Where | What it holds per county |
  |---|---|
  | `adapters/jurisdiction_resolver.py` `_COUNTY_JURISDICTION_GIS` | city limits and ETJ map layers |
  | `adapters/tcad_parcel.py` `_COUNTY_PARCEL_LAYERS`, `_CAD_PROPERTY_SEARCH_BY_COUNTY` | parcel map layer and field names, appraisal-district search page |
  | `orchestrator.py` `_TEXAS_COUNTIES_WITH_PARCEL_ADAPTER` | which counties have a parcel source |
  | `adapters/district_overlays.py` `_LAYERS_BY_COUNTY` | MUD, ESD, WCID, school and subdivision layers |
  | `adapters/fire_district.py` `_LAYERS_BY_COUNTY` | fire-station layer |
  | `county_clerk_portals.py` `_COUNTY_CLERK_PORTALS` | county clerk website and lookup steps |
  | `adapters/plats_intel.py` `_COUNTY_SUBDIVISION_SCREENS` + one summary function per county | subdivision/plat layer |
  | `coverage.py` `COUNTIES` | the Settings "Where PropRisk works" table, typed by hand |

- **Texas is assumed everywhere.** There is no "state" on a lookup. The address
  finder searches inside a Texas box and adds ", Texas"
  (`adapters/arcgis_geocode.py:79`, `:197`, `:215`). Census demographics read a
  Texas-only file (`bg_centroids_48.json`). About a dozen sources are Texas agencies
  (TxDOT, TCEQ, TWDB, TEA, TxGIO, data.texas.gov). County names alone are not
  unique across states (Williamson, Caldwell and Washington counties exist in
  several states), so a bare county name would match the wrong place.
- **No recipe.** There's no guide that tells a person or an AI tool how to add a
  county or a state, and no single command that proves a new county works.

## 2. The new shape

### 2.1 One folder per state, one file per county

```
backend/app/regions/
  _template/
    state.yaml          a commented blank state file
    county.yaml         a commented blank county file
  tx/
    state.yaml
    counties/
      travis.yaml  williamson.yaml  hays.yaml  bastrop.yaml
      caldwell.yaml  burnet.yaml  blanco.yaml
```

Plain YAML (`pyyaml` is already a dependency; the zoning-standards files already
use it). One small loader, `backend/app/regions/loader.py`, reads the folder once
at startup. It checks each file against a pydantic model and refuses to start
with a plain message naming the file and the field if one is wrong.

**`state.yaml`**

```yaml
code: TX                 # two-letter postal code; folder name is its lowercase
name: Texas
fips: "48"               # census state code
search_box: [-106.65, 25.84, -93.51, 36.50]   # west, south, east, north — address-finder bias
census_centroids: bg_centroids_48.json        # optional; demographics card is "not connected" without it
# fips above is also the ACS "state=" code
statewide_summary:       # lines shown on Settings under "Statewide sources"
  - TxDOT traffic, roads, bridges and projects
  - ...
```

**`counties/<name>.yaml`** — every section is optional except the first block.
A section the county doesn't publish is simply left out.

```yaml
name: Comal
level: Partial           # Full or Partial — see 2.4
summary: City limits and ETJ, parcel layer, clerk portal     # the Settings table text
sample_address: "199 Main Plaza, New Braunfels, TX 78130"    # a real address in the county; the check uses it

jurisdiction:            # → jurisdiction_resolver (same keys as today's _COUNTY_JURISDICTION_GIS entry)
  source: Comal County GIS
  source_url: https://...
  city_limits:           # always a list; tried in order, as today
    - url: https://.../FeatureServer/0/query
      name_field: CITY_NAME
      where: "..."             # optional filter (Hays uses it)
      fixed_name: "..."        # optional (Hays San Marcos layer)
      ignore_values: [...]     # optional
  etj:
    - url: https://.../FeatureServer/1/query
      name_field: CITY_NAME
  limited_purpose: [...]       # optional
  no_hit_advisories: [...]     # optional list of strings, as Hays and Bastrop have today
  unincorporated_on_no_hit: false   # optional; see the rule below

parcels:                 # → tcad_parcel (same keys as today's _COUNTY_PARCEL_LAYERS entries)
  source_label: Comal Appraisal District parcels
  query_url: https://.../query
  id_fields: [PROP_ID]
  acre_fields: [ACRES]
  ...
appraisal_search_url: https://esearch.comalad.org/

districts:               # → district_overlays; kind is one of subdivision, mud, wcid, esd, school
  - label: Comal County ESDs
    kind: esd
    urls: [https://.../query]

fire_stations:           # → fire_district
  url: https://.../query

clerk:                   # → county_clerk_portals (same keys as today's entries)
  name: Comal County Clerk Official Public Records
  url: https://...
  deed_instructions: "1) ... 2) ..."
  plat_instructions: "..."

subdivisions:            # → plats_intel's county subdivision screen
  source_label: Comal County subdivisions
  layer_url: https://.../FeatureServer/3
  query_url: https://.../FeatureServer/3/query
  name_field: SUBDIV_NAME          # field names the generic summary reads
  code_field: SUBDIV_ID            # optional
```

Every key in these sections is a key the adapter's existing table already uses.
**We move data, not logic.**

Lists keep their order. Today the first fallback URL that answers without an error
wins, even when its answer is empty (`district_overlays.py:300`), and the loader
keeps the YAML order exactly.

**Rule for "no city found" (new counties only).** `unincorporated_on_no_hit`
defaults to **false** when the county file sets it to nothing. A point that falls
outside every city-limits and ETJ layer is reported as "not confirmed", never as
"unincorporated", unless the contributor sets it to true. The contributor sets true
only after checking that the county's layers list every city and ETJ. The seven
existing counties keep their current values, written explicitly into their files
(Bastrop stays false, `jurisdiction_resolver.py:533`). A county with no
`jurisdiction` section never runs the county lookup and never concludes
"unincorporated".

### 2.2 What changes in the code

1. **Adapters read the loader instead of their private tables.** For each table
   in section 1, the adapter calls `regions.county(state, county)` and reads the
   matching section. The tables themselves are deleted once their content is in
   the YAML files.
2. **Special code stays special.** Code that is truly specific to one place stays
   in Python, unchanged:
   - the Travis, Williamson and Hays plat paths in `plats_intel.py`;
   - the Bastrop and Caldwell subdivision summary functions;
   - the WCAD adapter;
   - the Travis fire fallback;
   - Austin historic districts, Capital Metro transit, and city zoning and
     entitlement configs.

   **New counties take a generic subdivision path.** The plat dispatch
   (`plats_intel.py:248`) routes any county whose file has a `subdivisions` section
   to `_fetch_county_subdivision_screen`; it no longer checks a fixed set of names.
   - A new generic summary of about 20 lines reads `name_field` / `code_field`
     from the file.
   - That path today needs a parcel polygon and always reads a clerk portal
     (`plats_intel.py:577`, `:707`). For a file-driven county:
     - with no parcel polygon, it returns the existing "needs a parcel" result;
     - with no `clerk` section, it leaves out the clerk steps instead of
       crashing.
   - A subdivision name from this layer is shown only as "subdivision nearby".
     It never counts as proof that a plat was recorded.
3. **Settings is built from the files.** `coverage.COUNTIES` and `STATEWIDE` come
   from the loader. A county appears on the screen only if it has a file.
   `test_coverage_matches_adapters` changes to check the files instead of the
   hand-typed list.
4. **A lookup carries a state.** `PropertyIdentity` and `site_check_runs` get
   `state` (two letters).
   - **Where the state comes from:**
     1. The address finder keeps the state that each provider already returns and
        today throws away: Nominatim's `address.state`, ArcGIS's `RegionAbbr`, and
        Google's `administrative_area_level_1` (`arcgis_geocode.py:250`, `:302`,
        `:446`, `:624`).
     2. The address text is checked against **all 50 states plus DC** (name or
        postal code), whether or not PropRisk has a folder for that state. This
        way a Tennessee address is never sent as "…, Tennessee, Texas".
   - **How it is settled:**

     | Address text | Provider result | Outcome |
     |---|---|---|
     | names a state | agrees | that state |
     | names a state | disagrees | **unresolved**: the lookup stops at the address step with "The address and the map disagree on the state: check the address" |
| names a state | gives no state | the named state (never the database's TX default) |
     | names no state | gives a state | the provider's state |
     | names no state | gives no state | TX, today's default. The run is marked "state assumed: Texas" so the screen can say so. |

     A known non-Texas result is never relabeled TX.
   - **Search bias:** the address finder uses the named state's `search_box`
     (built-in boxes for all states sit in one small table next to the state
     list) instead of the hard-coded Texas box. With no state named, it keeps the
     Texas bias, as today.
   - **Every rebuilt identity carries the state:** fresh, reused-coordinate and
     retry identities all copy `run.state` (`orchestrator.py:1437`, `:2400`). The
     state check in item 5 runs before cached results are reused and on retries.
5. **Texas-only behavior is switched off outside Texas, in two places.**
   - **Whole sources.** `AdapterRegistration` gets `states: tuple[str, ...] = ()`,
     where empty means everywhere. Texas-only sources get `("TX",)`:
     - the TxDOT family, `twdb_ccn`, `schools_intel`, `tax_intel`;
     - `wcad_parcel`, `austin_zoning`, `future_land_use`, `zoning_standards`,
       `entitlement_pipeline`, `historic_intel`, `transit_intel`.

     For any other state, the orchestrator does not call them. It records them as
     `NOT_SUPPORTED_IN_JURISDICTION`, "Not connected for <State> yet".
     `historical_aerials` (TxGIO imagery) runs outside the registry
     (`orchestrator.py:2148`), so it gets the same state check where it is called.
   - **Texas parts inside shared sources.** Five sources mix county-file data or
     nationwide data with Texas extras. Each gets an explicit `state == "TX"`
     guard around the Texas part only:

     | Source | Texas part that gets the guard |
     |---|---|
     | `district_overlays` | the TCEQ statewide MUD/WCID layers (`:185`) |
     | `jurisdiction_resolver` | the Texas statewide fallbacks (`:549`) |
     | `tcad_parcel` | the TxGIO statewide parcel fallback, WCAD delegation, Travis handling (`:186`) |
     | `environmental_screening` | the TCEQ and data.texas.gov queries; EPA ECHO still runs (`:179`) |
     | `plats_intel`, `fire_district`, `county_clerk_portals` | their per-county special branches |
| `site_constraints` | the Edwards Aquifer query (`:109`). Pipelines, habitat and infrastructure still run. Its summary rows for TWDB/TxDOT inputs (`:1544`) say "not connected for <State> yet", not "no mapped feature" or "0 projects". |
| `cards.py` jurisdiction card (`:1488`) | outside Texas, it states only what was found: inside city limits, inside an ETJ, or outside both. It adds "zoning and development authority not checked". The Texas wording ("County rules only — no city zoning", ETJ review authority) appears only for TX. |

   - **Skipped Texas parts are never shown as findings** (Codex review of the
     Phase C reorder, 2026-11-17):
     - Outside Texas, `environmental_screening` marks the state tank, cleanup and
       regulatory-history checks as not connected. Its card, collapsed summary and
       advisories say "State environmental records: Not connected for <State>
       yet". They never describe skipped checks as zero records, "no records
       found", or a temporary outage (`cards.py:3206`, `:4074`,
       `environmental_screening.py:767`). EPA ECHO is still reported on its own,
       including its real empty and failure results. Phase C also corrects the
       environmental summary's source-status keys (`cards.py:4074`).
     - Outside Texas, the skipped Edwards Aquifer check is shown as "Not connected
       for <State> yet" in the `site_constraints` coverage table and the
       environmental map details, never as zero zones or "no mapped feature"
       (`site_constraints.py:1603`, `map_context.py:2051`).
     - Map layers that mix sources credit only the sources used for that lookup.
       Nationwide utility and environmental features must not suggest that TWDB,
       PUC, TCEQ, or an Edwards or CCN screen supplied Colorado findings
       (`map_context.py:2564`, `:2598`, `:2915`).
     - None of this needs C2's `provider_name` format.

   - **County keys.** Every bespoke `county == "<texas county>"` comparison reads
     the county through one helper, `texas_county(identity)`. It returns the county
     only when the state is TX. The places to change are the ones Codex listed in
     review round 1 (section 9):
     - `tcad_parcel` and `wcad_parcel`;
     - `plats_intel` (`:234`, `:1260`, `:2418`, `:2812`);
     - `fire_district`, `historic_intel` and `transit_intel` (whose empty-county
       path must now say "not covered", not run Capital Metro);
     - `county_clerk_portals` (both tables, `:429`, `:449`);
     - `entitlement_pipeline` (`:543`, `:1858`, `:1889`, `:2718`);
     - `county_source_registry`;
     - `service.py` (`:313`, `:476`);
     - `orchestrator.py` (`:542`, `:3028`).

     A test runs a "Williamson County, Tennessee" lookup through the recorded
     HTTP layer. A call to any Texas **service URL** fails the test. The test
     matches full service paths, not hosts, because Texas and nationwide layers
     share ArcGIS hostnames. The state-settling test covers all five rows of the
     table above.
6. **Cache and "check" reuse include the state.**
   - The run-reuse match (`service.py:232`) compares state as well as address,
     parcel and county.
   - The cache scope of every state-dependent source gets the state added: county
     sources, Texas sources, `historical_aerials` (`historical_aerials.py:775`) and
     `entitlement_reader` (`entitlement_reader.py:713`). That changes their
     cache keys, so old entries without a state are simply never matched again.
     No cleanup step is needed.
7. **Database: one small migration and a stopped upgrade.**
   - **The migration.** Users already have a v1 database, so we add migration
     `0002_state_column.py` instead of editing the baseline:
     - add `state` (2 letters, not null, server default `TX`) to
       `site_check_runs` and `site_check_coverage_gaps`;
     - replace the coverage-gap unique key with (tenant, capability, state,
       county).

     The model, the gap writer's insert values and its `ON CONFLICT` target
     (`orchestrator.py:554`) change in the same commit.
   - **The upgrade is stopped, never live.** The release note and `launch.sh` say
     the same thing:
     1. `scripts/stop.sh` (the worker drains or hands back its job, as today);
     2. update;
     3. `scripts/launch.sh`, which runs the migration before it starts the
        server and worker.

     `launch.sh` already migrates only on the startup path (`launch.sh:139`). One
     change: if it finds the app already running on an older version, it says
     "Stop PropRisk first, then launch again" instead of "already running".
8. **Census follows the state file.**
   - `census_acs` reads the centroid file path, its cache and the ACS `state=`
     code from `state.yaml` (`census_centroids` and `fips`) instead of the Texas
     constants (`census_acs.py:123`, `:173`).
   - Its registration has no fixed state list. It runs for any state whose
     `state.yaml` names a centroid file, and otherwise it says "not connected".
   - `scripts/build_census_centroids.py <fips>` builds that file from the Census
     Bureau's public block-group file, in the same format as today's Texas file.
   - Adding census for a new state needs no Python edit.

### 2.3 The recipe kit (what NMHP and other AI tools read)

| File | What it is |
|---|---|
| `AGENTS.md` (repo root, plus a `CLAUDE.md` that points to it) | 10 lines: "To add a county or a state, follow `docs/EXTENDING.md`. Never edit Python to add a county unless the recipe says so." AI tools read this file automatically. |
| `docs/EXTENDING.md` | The recipe, in plain English, with four jobs (below). Each job lists what to find, where public agencies usually publish it, how to fill the file, the check to run, and what "done" means. |
| `backend/app/regions/_template/` | The commented blank files to copy. |
| `scripts/check_region.sh <state> [<county>]` | The one proof command (2.4). |
| `CONTRIBUTING.md` | How to send it back, using NMHP's existing features (below). |

**How a contributor gets from NMHP to a pull request.** This uses only features
NMHP already has: "Adopt existing" a folder, and upload-on-accept, which pushes to
a configured repository and branch.
1. Fork `nomorehappypath/proprisk` on GitHub and clone the fork to your computer.
2. In NMHP, choose **Adopt existing** and pick that folder.
3. Set NMHP's upload to a new branch in **your fork**, for example
   `add-comal-county`.
4. Give NMHP the task ("Add Comal County, Texas to PropRisk"). When you accept the
   finished work, NMHP pushes it to your fork.
5. On GitHub, press **Compare & pull request** to the main PropRisk repo and paste
   the `check_region.sh` output.

Step 5 is done by hand: NMHP does not open pull requests to someone else's repo,
and this spec doesn't add that.

The four jobs in `EXTENDING.md`:

- **A. Add a county to a state we already have.**
  1. Copy the template.
  2. For each section, search the county's GIS hub and its appraisal-district and
     clerk sites, and the state's GIS portal. Record the URL found, or write
     "not published".
  3. Fill in the file and run the check.
- **B. Upgrade a Partial county to Full.** Same as A, for the missing sections only.
- **C. Add a new state.**
  1. Write `state.yaml`.
  2. Optionally build its census file.
  3. Add at least one county using job A.

  Lookups in that state then run every nationwide source plus that county's data.
- **D. Fill a state's jobs** (2.5). For each job in the job list:
  - keep `national`;
  - or point it at the state agency's map layer with a field mapping (no code);
  - or set it to `none`.

  Only a job whose standard format can't come from a single map layer needs a new
  adapter. For that, follow "Adding a source" in `docs/MAINTENANCE.md`; it must
  pass the standard-format check.

`EXTENDING.md` also has a **requirements template**: a short table, one row per
section, with the columns *found / URL / not published*. NMHP's Product step
fills it in as the task's requirements for the owner to approve before anything is
built. This is how "NMHP specs it and does it" works without a special NMHP
integration. The repo carries its own instructions.

### 2.4 The check: one command, plain answers

`scripts/check_region.sh tx comal` needs no app running. It does three things, in
order:

1. **File check.** The file loads and every field has the right type and name.
2. **Live check.** It treats the two kinds of URL differently:
   - **Map-layer URLs** (`.../query`). It sends one point query at the
     `sample_address` location with the layer's own `where` filter. An ArcGIS
     error body that comes back with HTTP 200 counts as an error, not as an empty
     answer. Each layer is reported as *answered*, *answered but empty here* or
     *error*, with the URL.
     - In a fallback list, only one entry needs to answer.
     - The others that fail are listed as warnings.
   - **Web pages** (clerk, appraisal search, source landing pages). It checks only
     that the page opens.
3. **Real lookup.** It starts a throwaway PostgreSQL, the same way the test suite
   does (`tests/conftest.py:38`), and adds the fixed office/owner rows and one run
   for `sample_address`. It calls `run_collect` in fresh mode
   (`orchestrator.py:1287`), builds the real cards (`cards.py:4902`), prints each
   card's status, and then runs the coverage test.

It ends with **PASS** or **FAIL** and one plain line per problem.
- `check_region.sh tx` with no county checks `state.yaml` and then every county
  in the folder.
- A state folder with no counties gets the file check only.

**The level is judged from the real lookup's stored facts, using four tests.** No
new fields are needed.

| Question | Passes when |
|---|---|
| Jurisdiction | the resolver returned a city, ETJ or confirmed unincorporated, not "not confirmed" |
| Parcel | exactly one parcel came back for the point |
| Districts | every configured district layer answered without error. Zero matches counts as an answer. |
| Clerk | the county's clerk portal URL and steps are present. "Manual verification" is the expected status here, and it passes. |

- **Full** needs all four.
- **Partial** needs at least one answer that comes from the county's own file. A
  nationwide or statewide answer doesn't count.

Counties that use special Python code (Williamson's WCAD) are judged by the same
four tests.

This requires one fix that is a real bug today. `district_overlays` reports a
layer as answered even when every layer failed, and treats an HTTP-200 ArcGIS
error as an empty answer (`:226`, `:264`, `:324`). It must record those layer
errors in its facts and lower its status to partial when a layer failed. Without
that, "Districts" can't be judged honestly.

### 2.5 Standard jobs and standard formats (every service, every level)

**The problem.** Today every result card reads a specific agency's data by that
agency's source name (`txdot_aadt`, `schools_intel`/TEA, `twdb_ccn`, and so on). It
also hard-codes the agency on screen. Examples:
- titles "TxDOT Projects", "Schools (TEA)", "Utilities (TWDB CCN)",
  "Tax / Debt (HB 1378)";
- map labels "Texas Department of Transportation";
- the credit line "Texas Geographic Information Office (TxGIO)".

A Colorado service can't be plugged in, because nothing says what data the card
needs. And if it could, the screen would still say TxDOT.

**The rule.**
1. **Every service does a named job.** Federal, state and county services all
   follow this one rule.
2. **Every job has one standard format.** The format is the exact fields the cards
   and map read today, taken from the code (a field inventory was made on
   2026-11-17).
   - **Each field keeps the unit it has today, and the format documents it.**
     Examples: `distance_mi` in miles, `distance_ft` in feet, `within_m` and
     `search_radius_m` in metres, `estimated_cost_usd` in dollars, `aadt` in
     vehicles per day.
   - There is no blanket unit change, so cards and saved lookups don't need
     converting.
   - Field mappings must declare a source unit when it differs (2.5 "field
     mapping"). The reader converts it.
3. **Every format also carries three standard fields.**
   - `provider_name`: shown on screen instead of hard-coded agency names.
   - `source_url`.
   - `data_as_of`: what period the data describes. It is one of four kinds, and
     never an invented day:

     | Kind | Example on screen |
     |---|---|
     | a full date | "as of 2025-06-20" |
     | a year | "2024 data" |
     | a range | "2018–2021 snapshot" |
     | `unknown` | "date not published by the source" |

     The time we fetched the data (`fetched_at`) is a different fact. It is never
     used in place of `data_as_of`.
4. **Each format's fields are marked required or optional.** A card shows what
   is present and leaves optional parts out. Example: speed limit is often blank in
   the national road data; Texas-only richer fields such as project award details
   stay optional.

The formats live in one file, `site_check/standard_formats.py`, with one pydantic
model per job.

**Which service does a job** is set per state in `state.yaml` and per county in
the county file:

```yaml
services:                       # in state.yaml
  traffic_counts: txdot_aadt            # an existing adapter
  schools: national                     # use the national default (below)
  road_projects: none                   # card says "Not connected for Colorado yet"
  water_providers:                      # a map layer + field mapping, no code
    layer: https://.../FeatureServer/0/query
    query: intersects                   # or "nearby" with radius_mi
    fields: {provider: PWS_Name, provider_id: PWSID}
```

There are three ways to fill a job, simplest first:
- **`national`** uses the national default in `regions/us/national.yaml`.
- **A map layer plus a field mapping** goes straight into the state or county
  file. Two pieces handle it:
  - **The layer reader** (`adapters/layer_reader.py`) runs the point or radius
    query, keeps the geometry, computes distance, and renames fields and converts
    their units using the mapping.
  - **The job's normalizer** turns those features into the job's format. There is
    one small normalizer per job, written once in Python and shared by every state.
    Examples: schools compute counts and nearest elementary/middle/high; bridges
    compute the nearest distance and condition level; water providers compute
    which polygon contains the point.

  Contributors only write the mapping. No transformation language is added.
- **An adapter module name** is for any source the reader plus the normalizer
  can't handle, even a single layer. Examples: traffic trends over years, the
  composite transportation summary, NAIP images. It must still pass the format
  check.

**How a job is picked: one function used everywhere.** `select_service(job, state,
county)` returns the implementation for that lookup. It checks, in this order:
1. the county file's `services:`;
2. the state's `services:`;
3. the national default.

- `none` written anywhere in that chain means **not connected** and stops the
  search.
- A job missing from all three is also not connected.

The same function is used for normal dispatch, for retries (today they import the
slot's module directly, `orchestrator.py:2583`) and for historical aerials (today
they bypass dispatch, `orchestrator.py:2148`). The Texas-only guards from 2.2
attach to the **implementation chosen**, not to the slot name. A national service
stored in the `txdot_aadt` slot runs in Colorado; a Texas service never runs there.

**Slots stay as they are inside the code.** Results are still stored under
today's source names. Some slots hold several jobs; each job is a separate
**part** of that slot's result:

| Slot (unchanged) | Jobs (parts) inside it |
|---|---|
| `txdot_aadt` / `txdot_aadt_history` | traffic counts / traffic history |
| `txdot_roadway`, `txdot_projects`, `txdot_bridges` | road information, road projects, bridges |
| `twdb_ccn` | water provider, sewer provider, statewide utility districts |
| `environmental_screening` | EPA facilities (national, always), state environmental records |
| `site_constraints` | its nationwide screens, plus the protected-aquifer part |
| `schools_intel`, `tax_intel`, `census_acs` | schools, district debt, demographics |
| `tcad_parcel` / `wcad_parcel` | parcel and tax record |
| `historical_aerials` (derived) | historical aerial photos |

Each part is selected and run on its own, then merged into its slot.
- **No part overwrites another.** The slot's facts gain
  `parts: {<job>: {coverage, provider_name, source_url, data_as_of, evidence}}`.
- **The card reads each part's coverage.** Example: water is connected and sewer is
  not.

#### The job list

The national defaults below were checked live on 2026-11-17 with a point query in
Austin. "None" means no free national source exists.

| Job | Level | Texas service today | National default for other states (verified) | Gap a new state must fill |
|---|---|---|---|---|
| flood zone, elevation, wetlands, soils, streams, drainage, power lines, pipelines and habitat, EPA facilities, places, current aerial, broadband | federal | same national services | already national | none |
| demographics | federal | Census ACS | Census ACS 5-year (free key required); block-group centroids from TIGERweb or TIGER `tl_YYYY_{ST}_bg` | build the state's centroid file |
| traffic counts (+ history) | state | TxDOT AADT | FHWA HPMS: national layer (2024) and per-state yearly layers (2020–2025) on geo.dot.gov | none (thinner on local roads) |
| road information | state | TxDOT Roadway Inventory | FHWA HPMS (class, lanes, ownership; speed limit often blank) | none |
| road projects | state | TxDOT project tracker | **none** | the state DOT's project or STIP layer, or `none` |
| bridges | state | TxDOT bridges | National Bridge Inventory via BTS (June 2025) | none |
| water provider | state | TWDB/PUC CCN | EPA Water System Service Area Boundaries v3 (2026; some areas modeled) | none |
| sewer provider | state | PUC sewer CCN | **none** | state or utility layer, or `none` |
| statewide utility districts | state | TCEQ MUD/WCID | **none** | state layer, or `none` |
| schools | state | TEA | NCES public school locations and enrollment (2024-25; no ratings) | optional: state ratings link |
| district debt | state | Texas HB 1378 reports | **none** | state source, or `none` |
| state environmental records | state | TCEQ (tanks, enforcement, registry) | EPA UST Finder leaking tanks, **frozen at 2018–2021**; `data_as_of` shows that on screen | state environmental agency layer (preferred) |
| protected aquifer zones | state | Edwards Aquifer | **none** | state layer, or `none` |
| historical aerial photos | state | TxGIO historical imagery | USDA NAIP per year, 2004–2023 (plus 1996–2003), on the federal imagery platform | none |
| statewide parcels | state | TxGIO StratMap | **none free** (national parcel data is paid) | state GIS parcel layer, or county parcels only |
| city limits and ETJ, parcel and tax record, appraisal search, districts, fire stations, clerk, subdivisions | county | county files (2.1) | **none** | the county file |
| transit, historic districts, city zoning | county/city | Capital Metro, Austin, 13 city layers | **none** | stays code (out of scope, section 7) |

The **parcel and tax record** job's standard format holds the property record
fields the cards show:
- `parcel_id`, `owner_name`, `situs_address`, `acreage`, `market_value`;
- `legal_description`, `cad_account`, deed number and date;
- the parcel shape.

A county file maps its assessor's field names onto these, as Texas counties do
today with `id_fields`, `acre_fields`, `value_fields` and so on.

#### Honest limits travel with the data

Each part's `evidence` field says how much the data can be trusted:
`authoritative`, `modeled` or `snapshot`. The screen respects it:

- **Water areas.** EPA's modeled water boundaries carry `modeled`. The map and
  `site_constraints` label them "modeled service area: confirm with the
  provider", not "verified public fact" (`map_context.py:1375`,
  `site_constraints.py:1546`).
- **Missing parts.** When the sewer part is not connected, the utility card says
  "Sewer provider: not connected for <State> yet". It must not say "No
  certificated area covers this site" (`cards.py:2881`). That sentence is only
  true when a connected source looked and found nothing.
- **Tank-only environmental data.** When the state environmental part is only the
  EPA leaking-tank snapshot, the card says only tank records were checked, with
  the 2018–2021 date. It says nothing about enforcement or registry records.
- **Traffic history.** The national default for traffic history is `none`. HPMS
  yearly layers don't follow one count station across years, and showing them as
  a trend would be false. A state can connect real history through an adapter.

#### Saved lookups and reuse

- **Old saved lookups still open.** Cards keep reading payloads saved before this
  change; missing new fields show as empty, never as an error.
- **Changed slots get a new version.** Every slot whose stored shape changes gets
  a higher `result_schema_version` (`registry.py`). Its cache version goes up too,
  so old entries are not reused for a new lookup.
- **Reuse also checks the chosen services.** The run-reuse rule
  (`service.py:256`) and the source cache compare a short fingerprint of the
  services `select_service` chose, including their mappings.
  - If a state or county file changes which service does a job, results from the
    old choice are not reused. This includes changing a job to `none`.
  - Results that depend on them are not reused either. Example: the transportation
    summary depends on traffic, roads, projects and bridges.

#### What the check command adds

`check_region.sh <state>` runs every job at the state's `sample_address`
(a new required field in `state.yaml`). It checks each job's output against its
standard format and prints one line per job:
- *standard*: required fields present, units as documented, `provider_name`
  set, and `data_as_of` present (which may honestly be `unknown`);
- *not connected*: the state set it to `none`;
- *FAIL*: with the missing or wrong field named.

`check_region.sh <state> <county>` does the same for the county jobs.
`check_region.sh <state>` with no county runs, in order:
1. the file check;
2. the state jobs at the state's `sample_address`;
3. the full county check (2.4) for every county in the folder.

This replaces 2.4's "file check only" for a state folder with no counties: such a
state still gets steps 1 and 2. A state or
county passes only when every job is either *standard* or *not connected*. A
service that answers in a non-standard shape can never ship.

#### What changes for Texas (no lost data)

- **Existing Texas services produce the standard formats.** A test checks every
  recorded Texas fixture against its format.
- **Screen text uses the new fields.** Titles and map labels become neutral, such
  as "Traffic", "Road projects", "Schools", "Water and sewer" and "District debt",
  and the agency comes from `provider_name`. A Texas lookup still says "Texas
  Department of Transportation", now taken from the data.
- **The field inventory found five existing mismatches. They are fixed here,** since
  the format decides which name is right:
  1. the environmental summary reads `source_statuses.pst/echo`; the adapter writes
     `tceq_pst/epa_echo`;
  2. the map reads `edwards_aquifer_status`/`habitat_status`, which no source
     produces;
  3. `water_provider_operating` is read as a list in one place and a dict in
     another;
  4. traffic band `very_high`/`unknown` are not given a label;
  5. Travis parcel candidates lack the distance fields Williamson has.

### 2.6 State and county sources: every Texas card, in any state (Phase C2, owner's order 2026-11-18)

**Owner's order (2026-11-18).** "For each county / state you find the 1 to 1
equivalent source and develop the connection and test. This is what NMHP will do
if it was enhancing. So we need PropRisk to have these possibilities." A Denver
lookup must show every card a Texas lookup shows, filled from Colorado's and
Denver's own public sources, and any state or county must be able to do the same.

**This replaces 2.5's machinery with something smaller.** 2.5 designed YAML field
mappings, a shared layer reader, per-job normalizers, pydantic formats and a
service fingerprint. Most cards need real logic (nearest school per level, top
traffic segments, bridge condition, tank releases still open), so mappings alone
would not fill them, and the owner asked for no over-engineering. What 2.5 got
right is kept: one place picks the source, the card never lies about a part that
was not checked, the agency name comes from the data, and the check command
proves the shape. Section 7's "no Python per county" is lifted for sources only,
on the owner's order: a county file stays data; a source is a small module.

#### The rule

1. **A card's slot can be filled by a state or a county.** A source module lives
   in the state's folder:
   - `regions/<st>/sources/<slot>.py`: statewide (example: `co/sources/txdot_bridges.py`);
   - `regions/<st>/sources/<county>/<slot>.py`: one county (example:
     `co/sources/denver/austin_zoning.py`).

   The slot name is today's source name, unchanged, so saved lookups, cards, the
   map and retries keep working.
2. **One function picks the source** (`regions/sources.py: pick(slot, state, county)`):
   county module, then state module, then the built-in adapter if it covers the
   state (`registry.states`, Texas today), else **not connected**. The
   orchestrator's `_load_adapter_fetch` already wraps every call, retries included
   (Phase C); it now asks `pick`. Phase C's guard stays: a Texas adapter never runs
   for another state.
3. **A source module returns the same facts the Texas card reads**, plus three
   standard fields:
   - `provider_name` (shown on screen; "Colorado Department of Transportation");
   - `source_url`;
   - `data_as_of`: a date, a year, a range, or `unknown`, as 2.5 defines. Never
     the fetch time.

   The facts each card reads are listed once, per slot, in
   `site_check/card_fields.py` (taken from `cards.py` and `map_context.py`).
   Required fields must be present; optional ones may be missing, and the card
   leaves them out. A module declares `PROVIDER_NAME`, `VERSION` and `fetch(identity,
   session=None, config=None)`, the same signature as an adapter.
4. **Parts a state does not have are said plainly.** A module can mark a part of
   its slot `not_connected` (sewer provider outside Metro Water Recovery's area)
   or `not_applicable` (the Edwards Aquifer outside Texas: "Does not apply in
   Colorado"). Cards never turn a missing part into "none found" (2.5's honest
   limits).
5. **Data-only pieces stay data:**
   - Historical aerial photos: a county or state file lists its years and image
     services (`historical_aerials: [{year, image_service, credit}]`); the builder
     uses the same export request it uses for Texas.
   - Zoning standards: Denver's district standards are one curated file,
     `zoning_standards/curated/denver.yaml`, in the same format as Austin's.
6. **Shared adapters with a state part ask `pick` for that part.**
   - `environmental_screening`: the state-records part is the slot
     `state_environmental_records` (Texas keeps its TCEQ code; Colorado gets a
     module). EPA ECHO stays national.
   - `site_constraints` and `map_context` stop asking "is this Texas?" and ask
     "did the slot this part reads answer?". Water/sewer, districts, road projects
     and traffic layers appear for any state whose module answered.
   - `txdot_extended` (the transportation summary) already builds from the four
     road slots; it reads them in any state and names the agency from
     `provider_name`. When a road slot is missing it calls `pick`, never a Texas
     module directly.
7. **Screen text names no agency by itself.** When a result carries
   `provider_short`/`provider_name`, the card title is neutral plus that name
   ("Schools (CDE)", "Road Projects (CDOT)") and every hard-coded agency phrase
   on that card and its map layers ("TxDOT traffic count data", the TxGIO credit)
   reads from the data. A Texas result carries neither, so a Texas card keeps
   today's exact text; Texas adapters are not edited for this.
8. **Reuse follows the source that would answer now.** The run-reuse filter
   (`service.py`, today "Texas-registered slot, other state: skip") asks the same
   question as dispatch: which source answers this slot for this state and
   county now? A saved result is reused only when it came from that same module
   and `VERSION` (`source_module`/`source_version` in the facts; a built-in
   adapter's result has no module). Embedded parts stamp themselves too:
   `environmental_screening` stores the state-records module and version, and a
   change invalidates it and its dependents. A module that reads a data file
   (Denver's zoning standards) folds the file's hash into its version. The
   historical-aerials cache key includes each year's image-service URL, not
   only the year.
9. **The check.** `check_region.sh <st>` and `check_region.sh <st> <county>` run
   every source module of that state or county at the sample address and print
   one line per slot: *connected* (required fields present, `provider_name` and
   `data_as_of` set), *not connected*, or *FAIL* naming the missing field. A
   recorded test fixture per module checks the same thing offline.
10. **The kit.** `regions/_template/source.py` (a filled-in skeleton),
    `regions/arcgis.py` (one shared point/radius query helper with `outSR=4326`,
    paging and distance), `docs/EXTENDING.md` Job D "connect a state's or a
    county's own source for a card", and the README task text for NMHP.

#### Corrections from Codex (spec round 1, 2026-11-18), all part of C2

1. **Every path asks `pick`.** Besides `_load_adapter_fetch` (all six call
   sites), `txdot_extended`'s direct fallback calls go through the same
   function, and the historical-aerials builder reads the county or state
   `historical_aerials:` list. `zoning_standards` is Texas-registered, so Denver
   gets a module (`co/sources/denver/zoning_standards.py`) that calls the same
   curated-standards engine with `denver.yaml`. `card_fields.py` also lists the
   facts later adapters read (`_build_adapter_config`): Denver's zoning module
   puts the district code ("D-C", "U-SU-C") in `zoning_type` and `base_zone`.
   Execution and retry tests prove each path.
2. **Reuse** as item 8 above.
3. **Two cards gain fields, kept distinct from Texas's.**
   - Tax: `levies: [{name, total_mills, bond_mills, tax_year}]` and
     `total_mills`. The card shows a "Property tax levies" table; bond mills are
     labelled a bond levy, never outstanding debt; no district website is
     labelled a debt report.
   - Road projects: `cost_range` text ("$1,000,000 to $5,000,000") is shown as
     published. No exact dollar figure is invented; the transportation summary
     ranks projects without a number by name, not as zero.
4. **Utilities mean what they say.** The map and the card separate "service
   area contains the site" from "nearest nearby area", whatever the authority
   type, so a Metro Water Recovery area that contains the site says it serves
   the site. A part that did not answer stays unknown or not connected, never
   "no service area".
5. **Bridges.** The national inventory's posting evaluation is not a posting.
   A posted bridge comes only from CDOT's `Bridge_Weight_Limit` layer (a
   posted-limit record); otherwise posting shows as unknown, and neither the
   card nor the summary says "no posted-weight bridge".
6. **Districts and sewer mains carry through.** The constraint ledger counts
   metro districts with the rest. Denver's sanitary-main layer
   (`ODC_UTIL_SANMAIN_L`) feeds the sewer-main row and map layer for Denver.
7. **Known limits are on the cards and in the tests.**
   - Rezonings marked "Pending" carry their application date and the note that
     the city's status may be out of date.
   - The Colorado environmental card says dry-cleaner records are not connected.
   - The 2026 aerial is labelled "interim"; the aerials line says "photo
     coverage configured here starts in 2018" (Denver's 2016 tiles exist but
     are not used).
   - If the entitlement reader runs for Denver, it gets Denver's portal and its
     prompt names the lookup's own county and state, not "County, Texas".
   - Acceptance runs real Denver records with known answers (Larimer Square is
     historic; Denver Water serves 201 W Colfax; a known open tank release), not
     only field presence.

#### Colorado and Denver, card by card (all checked live on 2026-11-18)

| Card (slot) | Level | Colorado / Denver source | Gaps said on the card |
|---|---|---|---|
| Traffic counts (`txdot_aadt`) | CO | FHWA HPMS Colorado 2025 (`geo.dot.gov HPMS_FULL_CO_2025`), every road with a count | none |
| Traffic history (`txdot_aadt_history`) | CO | CDOT `AADT_All_Years_Data` (1997–2025, state highways) | local streets: no history |
| Roads (`txdot_roadway`) | CO | HPMS Colorado 2025 (class, lanes, speed, ownership), CDOT `Highways_Detailed` names | right-of-way width not published |
| Road projects (`txdot_projects`) | CO | CDOT `GoSheet_Projects` + `TenYearPlan_Approved_Lines` | cost is a range; no award data |
| Bridges (`txdot_bridges`) | CO | National Bridge Inventory (`geo.dot.gov`), CDOT `BridgeConditionforDashboard` ratings | posted only from CDOT's weight-limit layer; else unknown |
| Transportation summary (`txdot_extended`) | any | built from the four road slots | crash data not connected (as in Texas) |
| Transit (`transit_intel`) | Denver | RTD bus stops and light-rail stations (CDOT-hosted copies) | layer dates shown (2021–2022) |
| Zoning (`austin_zoning`) | Denver | Denver `ODC_ZONE_ZONING_A` (zone district, overlays) | none |
| Zoning standards (`zoning_standards`) | Denver | curated `denver.yaml` from the Denver Zoning Code | only districts curated; others say so |
| Future land use (`future_land_use`) | Denver | Blueprint Denver future places (`ODC_PLAN_BPFUTUREPLACES_A`) | none |
| Development activity (`entitlement_pipeline`) | Denver | Denver rezonings, site development plans, building permits | rezonings have no street address; stale "Pending" flagged |
| Historic (`historic_intel`) | Denver | Denver landmark districts and structures | no design-standard links |
| Utilities (`twdb_ccn`) | CO | water: EPA water system boundaries (+ Denver Water); sewer: Metro Water Recovery service area; districts: DOLA water/sanitation and metro districts | no certificate (CCN) regime in Colorado |
| Schools (`schools_intel`) | CO | Census school district at the point; CDE school locations; NCES/Urban Institute enrollment | ratings: link to CDE, no score |
| Tax / debt (`tax_intel`) | CO | DOLA property tax map 2025 (taxing units, mills, bond mills) | bond levy shown, not outstanding debt |
| State environmental records (`state_environmental_records`) | CO | Colorado OPS storage tanks and releases; CDPHE Superfund, RCRA corrective action, institutional controls, solid waste, brownfields | no enforcement or dry-cleaner source |
| Site constraints: water/sewer, future ROW, aquifer | CO | from the utility and road-project slots; aquifer `not_applicable` | no planned-ROW layer |
| Historical aerials | Denver | DRCOG aerials 2018, 2020, 2022, 2024, 2026 | configured coverage starts in 2018; 2026 interim |
| Statewide parcels (`wcad_parcel`, Williamson only) | — | not a Denver card; Denver parcels already come from `denver.yaml` | — |

#### Proof

- Every Texas lookup and test unchanged, apart from neutral titles with the
  agency now read from `provider_name`.
- `check_region.sh co` and `check_region.sh co denver` end with PASS, every slot
  *connected* or honestly *not connected / not applicable*.
- A real Denver lookup in the owner's PropRisk shows each card above filled from
  the Colorado or Denver source, with the agency named on the card and on the
  map, and no Texas agency credited.
- A test state with no source modules still gets "Not connected for <State>
  yet" on every one of these cards (Phase C's behaviour).

## 3. What the user sees

- **Settings → "Where PropRisk works":** the table gains a State column and is
  grouped by state. Within a state, Full counties come first, then alphabetical.
  The order of today's seven changes slightly; the content stays the same.
- **A lookup in a state with no folder still runs.** Nationwide cards answer, and
  Texas-only cards say "Not connected for <State> yet". This is the same honest
  pattern already used for unconnected Texas counties.
- No other screen changes.

## 4. First real use (the acceptance test)

The owner, not a script, opens PropRisk in NMHP and types:

> Add Comal County, Texas to PropRisk.

**Done** means all of these happened with no Python change for the county:
1. NMHP produced the filled requirements table and the owner approved it.
2. NMHP wrote `tx/counties/comal.yaml`.
3. `check_region.sh tx comal` passed.
4. A lookup on a real Comal address in the app shows Comal's data.
5. The work is pushed to a fork branch, and a pull request carries the check
   output (opened by hand, as described in 2.3).

Comal is the suggestion because it borders Hays, is growing fast, and publishes
county GIS. The owner can name any county.

## 5. Order of work

- **Phase A: move Texas into files (no behavior change).**
  1. Add the loader and the seven county files, moved from the existing tables.
  2. Switch the adapters to read the loader and delete the old tables.
  3. Build Settings from the files.
  4. Add a one-time test proving that every loaded value equals the old constant.
     It runs before the constants are deleted and is removed in the same change.

  Proof: the full existing suite passes unchanged, and a lookup on the same
  addresses before and after gives the same result cards.
- **Phase B: the kit.**
  1. `AGENTS.md`, `EXTENDING.md` (with the requirements template), the templates
     and `CONTRIBUTING.md`.
  2. `check_region.sh`.
  3. The `district_overlays` error fix.
  4. The conservative no-hit rule.
  5. The configuration-driven subdivision path.

  Proof: section 4, done by NMHP.

  **Order changed 2026-11-17 (owner):** the owner chose Denver, Colorado as the
  test for states. Phase C alone makes a Denver lookup work honestly (nationwide
  sources plus Denver's county file, Texas-only sources "not connected"), while C2
  adds nothing a Denver user sees. So the order is A, B, C, C2, C3.
- **Phase C: states (built first, on the owner's order of 2026-11-17).**
  1. The state field, the provider-state extraction and the state-settling table.
  2. `states` on the registry, the orchestrator skip and the historical-aerials
     check.
  3. The in-source Texas guards and the `texas_county()` helper.
  4. Cache scopes and run-reuse.
  5. Migration 0002, the gap writer and the `launch.sh` message.
  6. Census from the state file, and the centroid script.
  7. A Colorado `state.yaml` and a Denver county file (`co/counties/denver.yaml`),
     the owner's chosen test. Colorado's `services:` are not written yet (that is
     C2): every Texas-only source is simply skipped for Colorado, as item 2 says.

  Proof:
  - the "Williamson County, Tennessee" test, where no Texas host is called;
  - an address/provider disagreement test;
  - `check_region.sh` on a test state folder that has no counties;
  - the stopped upgrade of a v1 database with existing rows;
  - every Texas lookup unchanged;
  - **Denver, the owner's test:** a lookup on a real Denver address is settled as
    CO, calls no Texas service URL, shows the nationwide cards, says "Not
    connected for Colorado yet" on every Texas-only card, and shows Denver's own
    county data; `check_region.sh co denver` ends with PASS.
    The Denver test also checks the rendered collapsed summaries, expanded cards,
    advisories, and map-layer details and credits:
    - skipped Texas parts are shown as not connected, with no false zero or
      "no feature" findings and no Texas agency credited for Colorado facts;
    - recorded Denver cases cover EPA ECHO with records, with no records, and
      failing, plus nationwide utility shapes present;
    - the jurisdiction card states the boundary finding and says "zoning and
      development authority not checked".
- **Phase C2: state and county sources (2.6, owner's order 2026-11-18).** This
  replaces the earlier C2 plan (2.5's formats and layer reader).
  1. `regions/sources.py` `pick`, wired into `_load_adapter_fetch`; `card_fields.py`.
  2. `provider_name`/`data_as_of` on the Texas adapters; neutral titles; the
     map and site-constraint checks ask whether the slot answered.
  3. The state-records slot, the aerials list and the Denver zoning standards file.
  4. Colorado and Denver modules for every row of 2.6's table, each with a
     recorded fixture.
  5. `check_region.sh` per-slot lines; the template, `arcgis.py` helper, Job D,
     README task text.

  Proof: 2.6's proof list.
- **Phase C3 (after C2): national defaults.** `regions/us/national.yaml` entries
  for:
  - HPMS traffic counts (current only; history `none`) and roads;
  - NBI bridges, EPA water providers, NCES schools;
  - UST Finder (`snapshot`) and NAIP.

  Each goes through `layer_reader` plus its job normalizer, or a small adapter where
  needed (NAIP images).

  Proof: a test state using only `national` passes `check_region.sh`, with every
  job *standard* or *not connected*, through a real non-Texas lookup.

Each phase is reviewed and released on its own, with a release note.

## 6. Honest trade-offs

**What we gain:**
- A county is one reviewable file.
- Contributors and AI tools can't break other counties by accident.
- The Settings table can no longer drift from what the app actually covers.
- The same recipe works with any AI tool, which makes PropRisk a showcase for NMHP.

**What it costs and risks:**
- **Phase A touches working code in about ten files.** Mitigation: it moves data
  only, and the one-time equality test plus before/after lookups prove nothing
  changed.
- **YAML moves URL mistakes from code review into data review.** Mitigation: the
  live check in the check command.
- **Counties with odd data still need Python.** A county whose layers need special
  handling can't be done with a file alone. The recipe says so and sends that
  contributor to job D-style code work. We accept that; most counties publish
  standard ArcGIS layers.
- **A new state is thin at first.** It gets nationwide data plus its counties, but
  no state agencies until someone does job D. The Texas-only cards say so plainly.
- **Standard formats cost a one-time pass over cards.py, map_context.py and
  site_constraints.py.** That pass covers:
  - agency text;
  - per-part coverage and evidence wording;
  - the five mismatches;
  - reading old saved payloads;
  - the reuse fingerprint.

  It is the largest piece of this spec. In return, every later state needs no card
  changes.
- **National defaults are uneven.** Leaking-tank data is years old and school data
  has no ratings. `data_as_of` and `provider_name` make that visible instead of
  hiding it.
- **The live check depends on outside services being up.** A FAIL may be the
  county's server, not the file. The check names the URL so a person can tell.

## 7. Out of scope (on purpose)

- A plugin system, Python per county (except source modules, 2.6), a region database, downloading region files
  at runtime, or an in-app "add county" screen.
- Turning the remaining special Python (Austin zoning city list, entitlement city
  configs, transit, historic) into files. Cities are a later spec if contributors
  ask for them. Today, city zoning standards are already plain files.
- Any change to how lookups, cards, AI steps, keys or spending work.
- Multi-user, hosting, accounts.
- A built-in "send my county upstream" button. Contributions use normal GitHub pull
  requests.

## 8. Where this pattern goes next

The same three pieces can go into every NMHP-built repo that is meant to grow:
1. an `AGENTS.md` that points to the recipe;
2. a plain recipe with a requirements template;
3. one check command that prints PASS or FAIL.

That is a pattern for repos, not a feature of NMHP. It is not built here.

## 9. Codex review

**Round 1 (2026-11-17): ONE MORE.** Corrections, and where each was applied:

1. Complete jurisdiction schema, keep fallback order and no-hit policy, and make
   missing coverage conservative. Applied in 2.1.
2. State detection for all states, using the provider's state; handling for
   disagreement and missing state; the state carried through fresh, reused and
   retry identities. Applied in 2.2 item 4.
3. Guard every Texas path, including mixed sources, city logic and historical
   aerials; Census driven by the state file. Applied in 2.2 items 5 and 8.
4. State in cache scopes and run-reuse. Applied in 2.2 item 6.
5. Stopped upgrade, plus the gap writer and conflict target. Applied in 2.2 item 7.
6. Headless checker setup, URL types, state-only runs, factual Full/Partial tests,
   and keeping ArcGIS errors. Applied in 2.4.
7. Configuration-driven subdivision path, its parcel dependency and the
   missing-clerk case. Applied in 2.2 item 2.
8. NMHP adopt, upload and manual pull-request handoff. Applied in 2.3 and 4.

**Round 2 (2026-11-17): ONE MORE.** Corrections 4–8 were confirmed. Four gaps
remained, all now applied:
1. `no_hit_advisories` is a list of strings (2.1).
2. A named state with no provider state keeps the named state (2.2 item 4).
3. `site_constraints` Edwards Aquifer guard and "not connected" summaries; the
   test matches service paths, not hosts (2.2 item 5).
4. Neutral jurisdiction-card wording outside Texas (2.2 item 5).

**Section 2.5 review (new gate):** check that each job's standard format can be
built from the fields the cards actually read, and that the `services:` map,
`layer_reader` and slot reuse work with the orchestrator, cards, map, retries and
saved runs. Also check that each verified national default actually fits its
job's required fields, and that the Phase C2 → C3 → C order is safe. Flag
anything bigger than needed.

**Section 2.5 round 1: ONE MORE, 6 corrections, all applied.**
1. Job/slot parts, `select_service` precedence and the meaning of `none` vs
   omitted, used by dispatch, retries and aerials; guards on the chosen
   implementation.
2. Shared layer reader plus per-job normalizers; an adapter allowed whenever needed.
3. `evidence` per part and honest-limits wording; traffic history `none`
   nationally.
4. Saved-lookup compatibility, version bumps and the service-fingerprint reuse
   rule; units kept per field.
5. `data_as_of` kinds, including unknown, year and range.
6. Order C2 → C → C3; state-only check runs the state jobs plus every county.

**Section 2.5 round 2 (2026-11-17): PASS.** All 6 corrections were verified
against the code, with no new material problem and no unnecessary scope.

**Round 3 (2026-11-17): PASS.** All four round-2 corrections were verified
against the code, and the edits introduced no new material problem.

Round-1 questions (kept for reference):

1. **Equality proof.** Does any adapter build its per-county table from computed
   values (f-strings, shared base URLs, tuples of fallbacks) that YAML would lose or
   reorder? `district_overlays` uses ordered fallback URL tuples; `jurisdiction_resolver`
   mixes a dict and a list for `city_limits`. Is "list always" safe there?
2. **Hidden county comparisons.** Find every place `identity.county`, `run.county`
   or a bare county string drives a branch, including `cards.py`, `map_context.py`,
   `service.py`, `entitlement_pipeline.py` `candidate_counties` and `smoke_matrix.py`.
   Will the `texas_county()` helper cover all of them, or is there a path where an
   out-of-state same-named county reaches Texas code?
3. **State detection.** Does Nominatim (and the ArcGIS and Google tiers) reliably
   return the state for US results? What happens to an address with no state,
   typed by a user who lives near the Texas border?
4. **Cache.** Is anything in `source_cache.py` keyed in a way that a Texas answer
   could be reused for a non-Texas lookup at the same rounded point? (Unlikely by
   location, but check the key.)
5. **Migration 0002.** Is adding a column with a server default plus swapping the
   unique constraint safe on a v1 database with existing rows and a worker that
   might be mid-job during upgrade?
6. **Check command.** Can it run a real lookup without the server running (worker
   code plus a temporary database), or does it need the app started? Pick the simpler
   option that still proves the cards.
7. **Level rule.** Is "Full = the real lookup answers 4 core questions" testable from
   the run's stored result without new fields?
8. **Over-engineering.** Point out anything here that is bigger than needed to let a
   non-engineer, using NMHP, add one county or one state.
9. **NMHP fit.** Can NMHP today open an existing GitHub repo as a project and end with
   a pull request to that upstream? If not, what is the smallest step the recipe
   should tell the user to do by hand?
