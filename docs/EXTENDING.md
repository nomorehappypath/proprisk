# Extending PropRisk: add a county

This is the recipe for adding a county to PropRisk, or upgrading one. It is
written so that a person or an AI build tool (NoMoreHappyPath, Claude Code,
Codex, Cursor) can follow it step by step. **Adding a county is filling in one
data file. It needs no Python.**

What a county file is: one YAML file in `backend/app/regions/<state>/counties/`
that tells PropRisk where that county publishes its maps and records. The blank
file to copy, with every field explained, is
[`backend/app/regions/_template/county.yaml`](../backend/app/regions/_template/county.yaml).

The proof that a county works is one command:

```bash
scripts/check_region.sh tx comal
```

It needs no running app and no keys. It ends with **PASS** or **FAIL** and one
plain line per problem (section "The check" below).

## Job A: add a county to a state PropRisk already has

Today those states are Texas (`tx`) and Colorado (`co`). For a state with no
folder yet, do job C first.

1. **Copy the template.** Copy `backend/app/regions/_template/county.yaml` to
   `backend/app/regions/tx/counties/<county>.yaml`. Name the file with the
   county name in lowercase and `_` for spaces: `comal.yaml`, `fort_bend.yaml`.
2. **Fill in the four required lines.**
   - `name`: the county name without "County".
   - `level`: start with `Partial`. The check tells you if the county earns `Full`.
   - `summary`: one short line for the Settings screen, for example
     "City limits and ETJ, parcel layer, clerk portal".
   - `sample_address`: a real street address inside the county, such as the
     courthouse. The check runs a real lookup on it.
3. **Find each source.** Fill in the requirements table (below) one row at a
   time. For each row, look in this order:
   - the county's GIS or open-data hub (search "<county> County Texas GIS");
   - the county appraisal district's website (parcels and its property search);
   - the county clerk's website (official public records search);
   - the state's GIS portal (for Texas: the TxGIO data hub).

   A map layer is usable when it has an ArcGIS REST address ending in
   `/FeatureServer/<n>` or `/MapServer/<n>`. Its query address is that address
   plus `/query`. Open the layer's page in a browser to see its field names.
   When the county does not publish a source, write "not published" in the
   table and leave that section out of the file.
4. **Fill in the file.** Remove the `# ` in front of each section you found and
   put in the real addresses and field names. Leave out every section you did
   not find. Never leave a line with no value.
5. **Run the check** and fix every FAIL line:

   ```bash
   scripts/check_region.sh tx <county>
   ```

6. **Done means:**
   - the check ends with PASS;
   - the requirements table is filled in, one row per section;
   - no Python file was changed.

### The requirements table

Fill this in before writing the file. It is the task's requirements: the person
who asked for the county approves it before the file is written.

| Section | What it is | Found? | URL (or "not published") |
|---|---|---|---|
| `jurisdiction` city limits | City-limits map layer | | |
| `jurisdiction` ETJ | ETJ (extraterritorial jurisdiction) map layer | | |
| `parcels` | Appraisal district parcel map layer | | |
| `appraisal_search_url` | Appraisal district property-search page | | |
| `districts` | Special-district layers: MUD, WCID, ESD, school, subdivision | | |
| `fire_stations` | Fire-station point layer | | |
| `clerk` | County clerk official public records search | | |
| `subdivisions` | Subdivision polygon layer | | |
| `sample_address` | A real address inside the county | | |

### Rules that keep the answers honest

- **No city found is "not confirmed", not "unincorporated".** Set
  `unincorporated_on_no_hit: true` only after checking that the county's
  city-limits and ETJ layers list every city and ETJ in the county.
- **A subdivision name is not a recorded plat.** The `subdivisions` layer is
  shown as "subdivision nearby"; it never counts as proof a plat was recorded.
- **Lists are tried in order.** In `districts`, a second URL is a fallback used
  only when the first fails. In `jurisdiction`, every layer in the list is
  checked in order.

## Job B: upgrade a Partial county to Full

Same as job A, for the missing sections only. Run the check: its four level
tests (below) say what is still missing. When all four pass, the check says the
county can be set to `level: Full`.

## Job C: add a new state

A lookup reads its state from the address (put the state in the address, for
example `Denver, CO 80202`). A state with a folder gets its counties' data; in
any state, the nationwide sources run, and every Texas-only card says
"Not connected for <State> yet".

1. **Copy the template.** Copy
   [`_template/state.yaml`](../backend/app/regions/_template/state.yaml) to
   `backend/app/regions/<code>/state.yaml`, where `<code>` is the state's
   two-letter postal code in lowercase (`co`). Make an empty `counties/` folder
   next to it.
2. **Fill it in.** `code`, `name`, `fips` (the Census Bureau's two-digit state
   code) and `search_box` (west, south, east, north).
3. **Census figures (optional).** Build the state's block-group file:

   ```bash
   uv --directory backend run python ../scripts/build_census_centroids.py <fips>
   ```

   then set `census_centroids: bg_centroids_<fips>.json` in `state.yaml`.
4. **Run the check:** `scripts/check_region.sh <code>`. A state with no counties
   gets the file check only.
5. **Add at least one county** with job A. Colorado's Denver
   (`backend/app/regions/co/counties/denver.yaml`) is a worked example.

Done means: `scripts/check_region.sh <code>` ends with PASS and no Python file
was changed.

## Job D: fill a state's services (not yet possible)

Pointing a state at its own traffic, water, school and other agencies needs the
standard service formats (same spec, Phase C2). Not yet possible.

## The check

`scripts/check_region.sh <state> [<county>]`, for example `tx comal`. With no
county, it checks every county in the state. In order, it:

1. **File check.** Every file loads, and every field has the right name and type.
   A mistake stops here with the file and the field named.
2. **Settings tests.** The Settings screen's county list matches the files.
3. **For each county:**
   - **Live check.** Each map layer is asked about the sample address's point
     and is reported as *answered*, *answered, empty here* or *FAIL*. In a list,
     one layer answering is enough; the others that fail are warnings. Each web
     page (clerk, appraisal search, source pages) must open. A site that refuses
     automated checks is a warning: open it in a browser to confirm.
   - **Real lookup.** A full PropRisk lookup on the sample address, in a
     throwaway database, using none of your keys (so it never spends AI money).
     It prints each result card's status.
   - **Level tests.** Four questions, answered from what the lookup found:

     | Test | Passes when |
     |---|---|
     | Jurisdiction | the county's own layers placed the point in a city, an ETJ, or confirmed unincorporated |
     | Parcel | exactly one parcel came back for the point |
     | Districts | every district layer in the file answered (zero matches counts as an answer) |
     | Clerk | the clerk portal and its steps are present |

     **Full** needs all four. **Partial** needs at least one answer that comes
     from the county's own file.

## Sending it back

See [`CONTRIBUTING.md`](../CONTRIBUTING.md).
