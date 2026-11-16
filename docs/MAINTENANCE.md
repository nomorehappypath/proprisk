# Maintenance

Everything a future maintainer needs to run, test, extend and troubleshoot
PropRisk without re-reading SPEC.md end to end.

## Running the tests

```
scripts/test.sh
```

Starts a throwaway PostgreSQL cluster in a temp folder on a free port, runs
the backend's `uv run pytest` suite, then the screens' `npm run build` and
`npm run test` — all offline, against recorded fixtures, with no network
calls. Removes the throwaway cluster when done either way.

To run just the backend suite against your own already-running database:

```
cd backend
DATABASE_URL=postgresql+psycopg://proprisk@127.0.0.1:55432/proprisk_test uv run pytest
```

Lint:

```
cd backend && uv run ruff check .
cd web && npm run lint
```

## Recording and re-recording source fixtures

The adapter contract tests (`backend/tests/test_site_check_adapter_contracts.py`)
replay a captured raw payload per source, so the whole suite runs offline. To
record a fresh fixture for one source (for example after a provider changes
its response shape):

```
cd backend
uv run python tests/record_site_check_fixtures.py <source_id>
```

This calls the real provider once, saves the sanitized response under
`backend/tests/fixtures/site_check/<source_id>/`, and scans every fixture
for anything that looks like a key or token before it is written — a
fixture that fails that scan is not saved.

## Adding a data source

1. Write the adapter in `backend/app/services/site_check/adapters/`,
   returning the shared `AdapterResult` contract
   (`backend/app/services/site_check/contract.py`).
2. Register it in `registry.py` (id, provider, concurrency group, and
   whether it is active).
3. If it needs an API key: add the name to
   `backend/app/core/secret_store.py`'s `ALLOWED_NAMES`, add its row to the
   table in this file and to `backend/app/api/settings.py`'s `KEY_INFO`,
   and read it at call time via `secret_store.get_secret(name)` — never
   from `settings.py` or an environment variable.
4. Record a fixture for it (above) and add a contract test case.
5. Add its row to `docs/DATA_SOURCES.md`
   (`backend/tests/test_data_sources_doc_complete.py` checks this).
6. If it changes what county or city PropRisk covers, see "Updating
   coverage" below.

## Updating coverage

`backend/app/services/site_check/coverage.py` is the one file behind
Settings → "Where PropRisk works". Update its `COUNTIES`, `ZONING_CITIES`,
`STANDARDS_CITIES` or `FUTURE_LAND_USE_CITIES` lists to match whatever the
adapters now cover, then run:

```
cd backend && uv run pytest tests/test_coverage_matches_adapters.py
```

This test reads the same adapters' own city/county lists and fails if
`coverage.py` has drifted from them.

## Changing the database

There is one migration, `backend/alembic/versions/0001_proprisk_baseline.py`
— it is the whole schema, not a diff. To change a table:

1. Edit `backend/app/models.py`.
2. Edit the baseline migration to match (add/alter/drop the matching
   column or table). There is no "add a new migration on top" step yet,
   since this is the very first schema PropRisk ships.
3. Run `cd backend && uv run pytest tests/test_baseline_matches_models.py`
   — it builds a throwaway database from the migration and checks every
   table and column against the models.
4. If you are changing a table real users might already have data in
   (after PropRisk has shipped), write a proper new migration instead of
   editing the baseline, and migrate existing rows forward rather than
   dropping them.

## Upgrading packages

Backend: edit the pinned version in `backend/pyproject.toml`, then
`cd backend && uv lock && uv sync`, then run the test suite and actually
start the app (`scripts/launch.sh`) and run one real lookup. Versions are
pinned exactly (not `>=`) because the site-check adapters compare
fixture-recorded geometry down to floating-point precision — a newer
`shapely` or `pyproj` can shift the last few decimal places and fail a
contract test for a reason that has nothing to do with a real bug.

Frontend: `cd web && npm update <package>` (or edit `package.json`
directly for a major bump), then `npm run build` and `npm run lint`.

Run `uv run pip-audit` (backend) and `npm audit` (web) before any release;
resolve or explicitly accept any high/critical finding.

## Reading the error log

Every server, worker and browser failure lands in
`data/logs/errors.jsonl` — one JSON object per line, newest at the bottom,
rotated at 10 MB (5 kept). Each line has a `reference` code (the same one
shown to the owner), `at`, `where`, `message`, `stack` (server/worker
errors only) and a scrubbed `context`. Find the error a user saw by
searching for their reference code:

```
grep '"reference":"ABCD-1234"' data/logs/errors.jsonl
```

Not covered: a hard kill, an out-of-memory condition, or a failure before
the sink itself starts (very early in `app/main.py` import, before
`install_error_sink()` runs).

## A removed test: the parity comparator

PropDesk's `backend_repo` ships a test file,
`tests/test_site_check_parity_comparator.py`, that loads a script,
`backend/scripts/parity_compare.py`, by file path — but that script is not
present anywhere in either PropDesk copy PropRisk was built from. The test
was already broken upstream, not broken by this build. Writing the script
would mean inventing comparator logic nobody specified, so the test file
was removed instead of carried over broken or guessed at. If a maintainer
later adds `backend/scripts/parity_compare.py` (the §13.1 "comparator is
code, versioned" script PropDesk's own spec describes), reintroducing a
test file like PropDesk's original is reasonable at that point.

## Every setting

Non-secret tunables live in `backend/app/config/settings.py`, each
overridable by an environment variable of the same name. Every API key and
token lives in the local secret store instead (next section) — never here.

| Setting | Default | What it changes |
|---|---|---|
| `app_name` | `'PropRisk'` | The name shown in the top bar and the `/health` response. |
| `environment` | `'dev'` | `dev` or `prod` — informational only. |
| `database_url` | `'postgresql+psycopg://proprisk@127.0.0.1:55432/proprisk'` | Where the server and worker connect for every table. |
| `proprisk_port` | `8765` | The port `scripts/launch.sh` binds the server to. |
| `proprisk_data_dir` | `'../data'` | Where logs, the database cluster and the secret store live. |
| `secrets_db_path` | `'../data/proprisk_secrets.sqlite3'` | The one SQLite file every key and token is stored in. |
| `error_sink_file` | `'../data/logs/errors.jsonl'` | Where error events are written. |
| `error_sink_url` | `None` | Optional URL to forward every error event to as well. |
| `error_sink_rate` | `60` | Max error events per minute per error fingerprint, before the rest are dropped. |
| `ai_prompt_cents_per_1k` | `0.25` | Legacy flat per-1k-token input rate (inert once a model has its own `openai_model_prices` entry). |
| `ai_completion_cents_per_1k` | `1.0` | Legacy flat per-1k-token output rate (same). |
| `ai_reserve_cents_per_call` | `5` | Reservation ceiling for an AI call with no token estimate. |
| `monthly_budget_cents_default` | `2000` | The AI spending cap in cents ($20.00) when none is set. |
| `budget_warn_threshold` | `0.8` | Fraction of the cap at which the Settings screen shows a warning. |
| `gateway_pending_timeout_minutes` | `10` | How long a reservation can sit unsettled before the reaper releases or finalizes it. |
| `openai_model_prices` | *(map of 4 priced models)* | Per-model USD-per-1000-token input/output rates and max output tokens. |
| `site_check_freshness_days` | `180` | How long a saved source answer counts as current in "check" mode. |
| `site_check_expiring_notice_days` | `30` | How long before the freshness deadline a result is marked "expiring". |
| `site_check_narrative_model` | `'gpt-5.6-terra'` | Model used for the per-card AI summary. |
| `site_check_vision_model` | `'gpt-5.6-terra'` | Model used for the historical-aerial reading. |
| `site_check_document_model` | `'gpt-5.6-terra'` | Declared for a future document-reading step; unused today. |
| `job_heartbeat_interval_seconds` | `30` | How often the worker's watchdog thread updates held job leases. |
| `job_stale_after_seconds` | `120` | A running job with no heartbeat this long is recovered as stale. |
| `job_sweep_interval_seconds` | `600` | How often the worker's sweeper thread checks for stale jobs. |
| `job_max_attempts` | `2` | How many times a failed job is retried before it is marked permanently failed. |
| `worker_max_concurrency` | `3` | How many lookups the worker runs at once. |
| `job_lock_timeout_seconds` | `30` | Postgres `lock_timeout` for one unit of job work. |
| `worker_poll_interval_seconds` | `1.0` | How often an idle worker checks the queue for new work. |
| `google_geocode_enabled` | `True` | Turns the Google Geocoding final-fallback tier on or off. |
| `google_geocode_cost_micro_per_call` | `5000` | Estimated cost per Google geocode request, for usage metering. |
| `public_intel_parallel_enabled` | `True` | Runs source adapters in parallel waves instead of one at a time. |
| `public_intel_max_workers` | `8` | Max concurrent adapter worker threads. |
| `public_intel_anchor_parcel_max_km` | `2.0` | Max km the geocoded point may sit from a supplied parcel before re-anchoring. |
| `public_intel_provider_concurrency_caps` | `{"txdot": 2, "default": 4}` | Per-provider concurrency caps within the parallel fetch. |
| `public_intel_rate_limit_cooldown_seconds` | `300` | Cooldown for a source after it is rate-limited (HTTP 429). |
| `public_aerial_imagery_ttl_days` | `100` | Aerial chip cache lifetime (capped at `site_check_freshness_days`). |
| `public_entitlement_gis_enabled` | `True` | Turns the county GIS subdivision sweep on or off for the entitlement card. |
| `public_entitlement_subdivision_radius_m` | `3200` | Radius for the recorded-plat sweep. |
| `public_entitlement_subdivision_display_limit` | `12` | Max recorded plats listed on the card. |
| `public_plat_discovery_enabled` | `True` | Turns free web/PDF plat-document discovery on or off. |
| `public_plat_search_provider` | `'bing_html'` | Which search backend plat discovery uses (`disabled`, `bing_html`, `searxng`, `brave`, `bing`). |
| `public_plat_searxng_url` | `None` | Base URL for a self-hosted SearXNG instance, if used. |
| `public_intel_http_attempts` | `3` | Default retry attempts for a public-provider HTTP request. |
| `public_intel_http_timeout_seconds` | `60` | Default per-attempt read timeout for a public-provider request. |
| `public_intel_http_backoff_base_seconds` | `1.5` | Base seconds for exponential backoff between retries. |
| `txdot_arcgis_timeout_seconds` | `90` | Per-attempt timeout for TxDOT ArcGIS spatial queries. |
| `txdot_arcgis_attempts` | `4` | Retry attempts for TxDOT ArcGIS queries. |
| `txdot_arcgis_backoff_base_seconds` | `1.5` | Backoff base for TxDOT ArcGIS retries. |
| `txdot_arcgis_geometry_precision` | `6` | Decimal places requested on TxDOT geometry responses. |
| `txdot_arcgis_max_allowable_offset_deg` | `0.00001` | Server-side line generalization for TxDOT geometry queries. |
| `environmental_screening_broad_radius_m` | `1609` | Search radius for the broader TCEQ contamination layers. |
| `environmental_registry_max_records` | `25` | Max records fetched per TCEQ regulatory-history dataset. |
| `nwi_fallback_coverage_probe_radius_m` | `5000` | Radius of the wide coverage probe for the wetlands fallback layer. |
| `ccn_water_query_urls` | *(2 URLs)* | Water CCN service-area layers, tried in order. |
| `ccn_wastewater_query_urls` | *(1 URL)* | Sewer CCN service-area layer. |
| `ccn_pws_operator_query_url` | *(1 URL)* | TCEQ operating-provider (PWS) service-area layer. |
| `ccn_query_timeout_seconds` | `25` | Read timeout for every CCN/PWS/TCEQ district query. |
| `ccn_point_buffer_m` | `200` | Buffer around the subject point for CCN membership queries. |
| `tceq_water_district_query_urls` | *(4 URLs)* | TCEQ Water District layers checked when no sewer CCN covers the parcel. |
| `sewer_nearest_search_radii_m` | `[500, 1000, 2000, 5000]` | Escalating search radii for the nearest-sewer-CCN search. |
| `drainage_screen_cell_size_m` | `10.0` | DEM cell size for the drainage screen's analysis grid. |
| `drainage_screen_window_m` | `1500` | Initial analysis-window edge length. |
| `drainage_screen_window_max_m` | `4000` | Cap on adaptive window expansion. |
| `drainage_screen_max_pixels` | `1442401` | Hard cap on decoded raster pixels. |
| `drainage_screen_max_download_bytes` | `25000000` | Hard cap on the streamed GeoTIFF download size. |
| `drainage_screen_dem_host` | `'elevation.nationalmap.gov'` | Allowed host for the elevation download. |
| `drainage_screen_dem_nodata_sentinel` | `-999999.0` | NoData sentinel when the GeoTIFF declares none. |
| `drainage_screen_depression_min_depth_m` | `0.15` | Minimum depth for a reported topographic depression. |
| `drainage_screen_depression_min_area_m2` | `100.0` | Minimum area for a reported topographic depression. |
| `drainage_screen_detention_max_slope_pct` | `2.0` | Max slope for a detention-candidate cell. |
| `drainage_screen_detention_max_accum_cells` | `50` | Max flow accumulation for a detention-candidate cell. |
| `drainage_screen_detention_min_area_m2` | `500.0` | Minimum area for a detention candidate. |
| `drainage_screen_flowpath_min_accum_cells` | `300` | Flow-accumulation threshold for a screened flow path. |
| `drainage_screen_max_regions_per_kind` | `25` | Map-output cap per drainage feature kind. |
| `drainage_screen_nhd_record_cap` | `200` | Record cap for the NHD flowline cross-check. |
| `drainage_screen_nhd_agree_distance_m` | `60.0` | Agreement distance for the NHD cross-check. |
| `drainage_screen_conus_bounds` | `'24.0,49.5,-125.0,-66.5'` | Bounding box outside which drainage screening is not yet supported. |
| `access_facts_frontage_max_ft` | `175.0` | Max distance from the parcel boundary for a frontage-road candidate. |
| `access_facts_frontage_max_mi` | `0.03` | Anchor-distance fallback for frontage when no parcel polygon exists. |
| `access_facts_connection_buffer_m` | `60.0` | Buffer for matching mapped road connections to the frontage. |
| `public_aerial_imagery_export_base_url` | *(USGS National Map URL)* | The imagery export endpoint used for the modern aerial chip. |
| `public_aerial_imagery_image_size_px` | `640` | Chip edge length in pixels. |
| `public_aerial_imagery_default_radius_m` | `250` | Default half-span of the chip's view box. |
| `public_aerial_imagery_min_radius_m` | `120` | Lower clamp on the view half-span. |
| `public_aerial_imagery_max_radius_m` | `800` | Upper clamp on the view half-span. |
| `public_aerial_imagery_timeout_s` | `20` | Timeout for validating the chip URL before it reaches the browser. |
| `public_aerial_imagery_image_format` | `'jpg'` | Image format requested from the imagery export endpoint. |
| `historical_aerials_enabled` | `True` | Turns the historical-aerials gallery and its AI reading on or off. |
| `historical_aerials_base_url` | *(TxGIO imagery URL)* | Root of the historical-imagery catalogue. |
| `historical_aerials_eras` | *(6 eras, 1981–2022)* | The photo-year → imagery-service map for the era ladder. |
| `historical_aerials_chip_size_px` | `512` | Edge length in pixels of each era chip. |
| `historical_aerials_timeout_seconds` | `25` | Timeout for fetching one era chip. |
| `historical_aerials_cache_ttl_days` | `3650` | Reuse window for a resolved era timeline (effectively permanent). |
| `entitlement_read_enabled` | `True` | Turns the AI city-planning-page read on or off. |
| `entitlement_read_model` | `'gpt-5.6-terra'` | Model used for the entitlement read (must support web search). |
| `entitlement_read_max_searches` | `10` | Budget for web searches during one entitlement read. |
| `entitlement_read_radius_m` | `3200` | How far around the site the model looks for city cases. |
| `entitlement_read_max_cases` | `12` | Max validated cases rendered from one read. |
| `entitlement_read_validation_timeout_s` | `20` | Per-URL timeout when re-verifying a cited case. |
| `entitlement_read_validation_max_attempts` | `2` | Max attempts to fetch one cited page for verification. |
| `entitlement_read_cache_ttl_days` | `7` | Reuse window for an entitlement read (short — active cases move fast). |

## Every secret name

Stored only in `data/proprisk_secrets.sqlite3` (see
`backend/app/core/secret_store.py`); never read from an environment
variable. Set and remove them from Settings → "Keys and secrets", or with
`scripts/keys.sh`.

| Name | Used by | Required? |
|---|---|---|
| `openai` | AI card summaries, historical-aerial reading, entitlement reading | Optional |
| `census` | Demographics card (US Census ACS) | Optional but recommended |
| `arcgis` | Address-finding fallback, nearby-places fallback | Optional |
| `google_maps` | Address-finding final fallback | Optional |
| `brave_search` | Recorded-plat web search | Optional |
| `bing_search` | Recorded-plat web search (retired by Microsoft, August 2025) | Optional |
| `error_sink_token` | Authenticates error forwarding to `error_sink_url`, if set | Optional |
| `contact_email` | Added to outbound User-Agent strings to public services (not a secret, but stored the same way) | Optional |
