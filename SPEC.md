# PropRisk — Specification

PropRisk is the open-source, single-user edition of the **Public Data** part of PropDesk
(its "site check" feature). You type a property address
(optionally a parcel ID); PropRisk queries dozens of free public sources plus optional paid
services, and shows what the public record says about the site: flood zone, zoning,
traffic, utilities, soils, wetlands, environmental records, plats, nearby schools and more,
as result cards, a historical-aerial gallery and an interactive map.

This document tells a builder exactly what to copy, remove and change to produce PropRisk
from the two PropDesk copies, and tells a future maintainer how the result is put together.
Every statement about the current code was checked against these copies:

- `propdesk_public/app_repo/web/` — PropDesk screens (Vite + React 19 + TypeScript).
- `propdesk_public/backend_repo/backend/` — PropDesk server (Python 3.12, FastAPI,
  SQLAlchemy 2, Alembic, PostgreSQL, a background worker).

Both copies are read-only inputs; they are never edited.

## Owner's answers

- Which computers should the launch and stop scripts work on? — Mac only
- The written summaries and aerial-photo reading use paid AI (OpenAI). What monthly spending
  limit should PropRisk start with? The user can change it later. — $20 a month, as PropDesk today
- Which open-source licence should PropRisk use on GitHub? — Keep PropDesk's licence (AGPL):
  anyone who changes it and offers it to others must share their changes
  (Changed by the owner on 2026-11-17: PropRisk is now Apache License 2.0, the same as
  NoMoreHappyPath. See RELEASE_NOTES.md.)
- How do you use it? — Desktop app
- Who uses it? — Just me
- What will it hold? — My own private information
- If it stops for a day, what happens? — No big deal
- Internet connection? — Always online
- Is there existing data to bring in? — No
- If its data were lost, what happens? — Nothing, it can be rebuilt
- How much recent data could you afford to lose? — Nothing

No question was skipped, so there are no "Assumed:" lines.

How the answers were read: "Desktop app" is met by a local web app opened in the Mac's
browser by a launch script (the PropDesk screens are a web app; no packaging is asked for).
"My own private information" is the user's own keys and lookup history, kept on their own
Mac. "If its data were lost — nothing, it can be rebuilt" and "afford to lose — nothing" are
read together: lookups are re-runnable from public sources, so nothing irreplaceable is
stored; keys are re-typed. No backup is built (chapter 9).

---

## 1. Functional

### 1.0 Owner direction, decisions, and what is not included

Owner's direction (summary): strip PropDesk down to the Public Data part, recreate it 100% in
its own folder named PropRisk, make it open source, store every API key, code or token in a
SQLite database, keep everything else the same, no users and no sign-in, a launch script and
a stop script for the local PC, keep the code and languages as they are, document it well
enough that a stranger understands what is what. Owner's changes to the first proposal: a
Settings menu with setup screens for keys and secrets and a list of what the app needs as
input; the Settings menu shows where the app works (Texas) and the connected counties; a copy
of this document in `propdesk_public/Specs/`.

Decisions (each with its one reason):

| Decision | Choice | Reason |
|---|---|---|
| Lookup database | PostgreSQL, unchanged | Owner: "the rest remains the same". The job queue relies on PostgreSQL-only features (`FOR UPDATE SKIP LOCKED`, `lock_timeout`, JSONB, partial unique indexes). |
| Where PostgreSQL runs | A private PostgreSQL cluster inside `PropRisk/data/postgres`, own port, started and stopped by the scripts | No setup of a system database, no clash with other projects on the Mac. |
| Key and secret store | One SQLite file `PropRisk/data/proprisk_secrets.sqlite3` | Owner's rule. Python's built-in `sqlite3`; no new dependency. |
| Sign-in | None. One built-in local office and owner row | Owner: no users. Keeping the rows lets the kept code run unchanged. |
| Serving the screens | The FastAPI server serves the built screens and the API on one port | One address to open; one process fewer to manage. |
| Platform | macOS only | Owner's answer. |
| Licence | Apache-2.0 (was AGPL-3.0-or-later until 2026-11-17) | Owner's answer; changed by the owner to match NoMoreHappyPath. |
| AI cap default | $20.00 a month | Owner's answer (same as PropDesk). |

Not included: multiple users, sign-in, hosting on a server, Linux or Windows scripts,
backups, importing PropDesk data, new data sources, any change to what a lookup shows.

### 1.1 Folder and name

Everything lives in `PropRisk/` at the top of this project:

```
PropRisk/
  README.md            what it is, install, launch, stop, settings, troubleshooting
  LICENSE              Apache License 2.0 text (AGPL-3.0 until 2026-11-17)
  SECURITY.md          what is and is not protected, how to report a problem
  .gitignore           data/, node_modules/, dist/, .venv/, caches, *.sqlite3, .env*
  backend/             Python server + worker (from backend_repo/backend)
  web/                 React screens (from app_repo/web)
  scripts/             launch.sh, stop.sh, keys.sh, test.sh
  docs/                ARCHITECTURE.md, DATA_SOURCES.md, MAINTENANCE.md
  data/                created at first launch; never in git (database, secrets, logs, pids)
```

Every visible "PropDesk" becomes "PropRisk": page title, top bar, manifest name, AI prompt
text (`narratives.py:56` "PropDesk Public Data"), card wording in `cards.py`,
`entitlement_pipeline.py`, `entitlement_reader.py:62-70`, `county_clerk_portals.py`, logger
names `propdesk.*` → `proprisk.*`, outbound User-Agent strings (see 1.7). `desk.example.com`
(Vite `allowedHosts`) and `support@reference.app` (User-Agent contact) are removed.

### 1.2 What is kept, unchanged in behaviour

Screens (routes unchanged):

| Route | Screen | Source file |
|---|---|---|
| `/public-data` | New lookup: address, optional parcel ID, mode "check" (reuse fresh saved results) or "fresh"; recent lookups | `pages/publicdata/NewLookupPage.tsx` |
| `/public-data/history` | Lookup history with search and paging | `pages/publicdata/HistoryPage.tsx` |
| `/public-data/runs/:id` | One lookup: live progress, every result card, era (historical aerial) gallery, cancel, retry failed sources, delete | `pages/publicdata/RunPage.tsx`, `EraGallery.tsx`, `CategoryIcon.tsx` |
| `/public-data/runs/:id/map` | Property map with all layers | `MapView.tsx`, `PropertyMapPanel.tsx` |

Server endpoints (all under `/api/site-check`, unchanged): `POST ""` start a lookup (202),
`GET /runs` list, `GET /runs/{id}` read one, `GET /runs/{id}/map` map context,
`POST /runs/{id}/cancel`, `POST /runs/{id}/retry-sources`, `DELETE /runs/{id}`; plus
`GET /health` and `GET /api/health`.

Everything under `app/services/site_check/` is kept: 38 registered sources (35 active, 3 switched
off: `txdot_county_vmt`, `eia_state_fuel_sales`, and `arcgis_places`, which runs only as the
nearby-places fallback; the comment at `registry.py:61` still says "34 active", the dict holds 35),
the orchestrator, cards, narratives, historical aerials, entitlement reader, map context,
source cache and 180-day reuse rule, provider usage metering, coverage gaps, county source
registry, county clerk portals, the curated zoning standards (10 city YAML files) and the
Texas block-group centroids file. Also kept: the job queue (`app/core/job_queue.py`), the
worker (`app/worker.py`, minus the bill-reminder loop), the AI gateway with its monthly cap
(`app/services/gateway.py`), the audit log (`app/services/audit.py`), the error envelope
(`{"error": "<plain sentence>"}`) and every Public Data input check (address 1–512
characters, parcel ID up to 64, mode `check|fresh`, page size 1–200).

Behaviour rule: a lookup in PropRisk produces the same cards, map and messages as PropDesk
for the same source answers. Code is changed only where it depended on sign-in, offices,
secrets read from the environment, or PropDesk branding; each such change is listed in 1.5.

### 1.3 What is removed

| Area | Server files removed | Screen files removed |
|---|---|---|
| Sign-in, accounts, roles, activation keys | `api/auth.py`, `core/security.py`, `core/activation_keys.py`, `services/users.py`, `scripts/admin.py` | `pages/LoginPage.tsx`, `store/auth.ts` |
| Launcher, properties, categories, settings, usage | `api/platform.py` (its usage summary and AI-key parts move to the new `api/settings.py`), `api/categories.py` | `pages/LauncherPage.tsx`, `PropertiesPage.tsx`, `CategoriesPage.tsx`, `SettingsPage.tsx`, `UsagePage.tsx` (its Public-data section and source names move into Settings), `components/CategoryOptions.tsx`, `lib/categories.ts` |
| Check/receipt scanner and ledger | `api/checks.py`, `services/extraction.py`, `services/pdf_scanner.py`, `services/suggestions.py`, `services/storage.py` | `pages/scanner/*`, `components/CaptureModal.tsx`, `InvoiceMiniForm.tsx`, `lib/capture.ts`, `lib/invoices.ts`, `public/vendor/opencv.js`, `types/jscanify.d.ts` |
| Bills diary, reminders, web push | `api/bills.py`, `api/notifications.py`, `services/reminders.py`, `scripts/generate_vapid_keys.py`, worker `_reminder_loop` | `pages/diary/*`, `lib/notifications.ts`, `public/sw.js` and its registration in `main.tsx` |
| Backups | `api/backup.py`, `services/backup.py`, `scripts/nightly_backup.py` | — |
| Server deployment | `backend_repo/scripts/*.sh`, `docs/deployment/ubuntu_propdesk.md` | — |
| Vault encryption | `core/vault.py` (keys move to the SQLite store, see 1.6) | — |
| Old migrations | all 14 files in `alembic/versions/` (replaced by one baseline, see chapter 2) | — |
| Tests of removed features | `test_auth`, `test_roles`, `test_isolation`, `test_tenant_only_keys`, `test_backup`, `test_backup_script`, `test_bills_and_notifications`, `test_brick2`, `test_brick3`, `test_category_tree`, `test_income_direction`, `test_invoice_attachments`, `test_scanner`, `test_transfers`, `test_migrations` (replaced by `test_baseline_matches_models`) | `scripts/diary_smoke.mjs`, `paid_from_smoke.mjs`, `_*_probe.mjs`, `_layout_shots.mjs`, `_poll_stability.mjs` |

Database tables removed: `auth_audit`, `properties`, `categories`, `checks`,
`attached_invoices`, `bills`, `push_subscriptions`, `reminder_deliveries`.
Python packages removed when nothing kept imports them: `passlib`, `argon2-cffi`,
`python-jose`, `email-validator`, `cryptography` (only the vault used it), `pillow`,
`pillow-heif`, `pywebpush`, `python-multipart` (no kept route takes form data). `openpyxl`
stays: the switched-off `txdot_county_vmt` source imports it
(`adapters/txdot_county_vmt.py:66`), and `tifffile` stays for `drainage_screen.py:345`.
npm package removed: `jscanify`.

Tests kept: every `test_site_check_*` file (14), `test_job_queue`, `test_health`,
`site_check_fixture_replay.py`, `record_site_check_fixtures.py`, `test_gateway_models` (AI
pricing arithmetic, unchanged); `test_envelopes` without `test_duplicate_409_includes_duplicate_of`
(scanner); `test_vault_budget` without `test_vault_set_masked_delete`,
`test_invalid_key_rejected` and `test_admin_cli_provider_usage_platform_wide_smoke` (vault and
admin CLI removed; their place is taken by the secret-store and Settings tests in chapter 7),
with `test_key_resolution_tenant_only` rewritten to read the key from the secret store.
Kept tests that sign in (`login` fixture, `Authorization` headers; used in 9 site-check test
files) call the API as the local owner instead. Tests that use a second office (for example
`tenant_b` / `user_b` in `test_site_check_api.py`) keep checking separation at the service
layer, where `tenant_id` still filters every query; HTTP-level cross-office checks that need a
second signed-in user are removed, because PropRisk has only one office.
Settings removed: `jwt_secret`, `activation_key_hmac_secret`, `user_access_ttl_hours`,
`min_password_len`, `vault_secret`, `openai_api_key`, `openai_vision_model` (scanner),
reminder and VAPID fields, backup fields, `max_upload_mb`, `draft_ttl_hours`,
`ledger_row_cap`, the launcher app catalogue, and the five secret fields
`arcgis_api_key`, `census_api_key`, `google_maps_api_key`,
`public_plat_brave_search_api_key`, `public_plat_bing_search_api_key` (they move to the
secret store). The builder confirms each removal with a search before deleting.

### 1.4 Single user, no sign-in

- No login screen, no accounts, no roles. Opening `http://127.0.0.1:8765/` shows the New lookup
  screen (`/` redirects to `/public-data`).
- The baseline migration creates one office row (`tenants`, name "Local", budget 2000 cents)
  and one user row (`users`, display name "Local owner", role `owner`), both with fixed ids.
- `app/api/deps.py`: `current_user` returns that user from the database with no header check;
  `current_tenant` returns its office. The kept site-check API keeps calling these unchanged.
- `users` keeps only `id`, `tenant_id`, `display_name`, `role`, `created_at`;
  `tenants` keeps `id`, `name`, `monthly_budget_cents`, `created_at`.
- The top bar shows "PropRisk" (link to New lookup), "History" and "Settings". No "Log out".
- `web/src/lib/api.ts` sends no `Authorization` header and no longer clears a token on 401.

### 1.5 Changed code, file by file

| File | Change |
|---|---|
| `backend/app/main.py` | Mount only the site-check router, the new settings router and the client-errors route; serve `web/dist` (static files, any non-`/api` path falls back to `index.html`); install the error sink handlers; add the Host/Origin check (chapter 7). |
| `backend/app/models.py` | Keep `Tenant`, `User` (slimmed), `AuditLog`, `AiUsage`, `Job`, `SiteCheckRun`, `SiteCheckSourceCache`, `SiteCheckProviderUsage`, `SiteCheckCoverageGap`; drop the rest; drop `Tenant.openai_key_encrypted` and `Tenant.owner_names`. |
| `backend/app/api/deps.py` | Local owner, as in 1.4. |
| `backend/app/config/settings.py` | Remove the settings listed in 1.3; default `database_url` points at the private cluster (`postgresql+psycopg://proprisk@127.0.0.1:55432/proprisk`); add `proprisk_port` (8765), `proprisk_data_dir` (`../data`), `secrets_db_path`; remove the reminder/VAPID cross-checks; keep every site-check field, default and bound. |
| `backend/app/services/gateway.py` | `resolve_key(tenant)` reads the `openai` secret from the SQLite store; same return value and `NoTenantKey` when missing. Key source label stays `"tenant"`. |
| `adapters/arcgis_geocode.py:288,290,394,397`, `adapters/arcgis_places.py:85-86`, `adapters/census_acs.py:112-113`, `orchestrator.py:659-665` | Read the key from the secret store (`secret_store.get_secret("arcgis")` etc., returning `SecretStr | None`) instead of `settings`. Read at call time, so a key saved in Settings reaches the worker without a restart. Nothing else in these files changes. |
| `backend/app/worker.py` | Remove the reminder loop; send thread and job failures to the error sink. |
| User-Agent strings (see 1.7) | Rebrand only. |
| `web/src/App.tsx` | Routes from 1.2 plus `/settings/*`; new top bar; no auth guard. |
| `web/src/main.tsx` | Remove service-worker registration; install the browser error reporter. |
| `web/index.html`, `public/manifest.webmanifest` | Title and app name "PropRisk". |
| `web/vite.config.ts` | Remove `allowedHosts` and `host: true` (dev binds 127.0.0.1); proxy `/api` to `127.0.0.1:8765`. |
| `web/package.json` | Name `proprisk-web`; scripts trimmed to `dev`, `build`, `lint`, `preview`, `smoke:ui` (Public Data part), `smoke:map`, `test`. |
| `web/scripts/ui_smoke.mjs` | Keep the Public Data section (fixture-driven render checks from line 280 on); drop login, scanner, diary steps. Keep `map_smoke.mjs`, `capture_fixtures.mjs`, `fixtures/public_data/*`. |
| `backend/tests/conftest.py` | No login fixtures; the client works against the local owner; `make_tenant` stays for tests that need a second office row. |

New files: `backend/app/core/secret_store.py`, `backend/app/core/error_sink.py`,
`backend/app/api/settings.py`, `backend/app/api/client_errors.py`,
`backend/app/services/site_check/coverage.py`, `backend/app/cli/keys.py`,
`backend/alembic/versions/0001_proprisk_baseline.py`, `web/src/pages/settings/*`,
`web/src/lib/settings.ts`, `web/src/lib/errorReporter.ts`, the four scripts and the docs.

### 1.6 Keys and secrets (SQLite store)

- File: `PropRisk/data/proprisk_secrets.sqlite3`, created at first launch with permissions
  0600, in a folder with 0700. One table:
  `secrets(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`.
- Allowed names (anything else is refused): `openai`, `arcgis`, `census`, `google_maps`,
  `brave_search`, `bing_search`, `error_sink_token`. One non-secret value lives in the same
  store as `contact_email` (optional, added to outbound User-Agent strings, see 1.7).
- `secret_store.py` exposes `get_secret(name) -> SecretStr | None`,
  `set_secret(name, value) -> None`, `delete_secret(name) -> bool`,
  `list_secrets() -> list[{name, is_set, last4, updated_at}]`. Each call opens, uses and
  closes its own connection (safe across the server and worker processes; SQLite handles the
  locking). Values are stored as typed; the owner asked for no extra security, and the file
  never leaves the Mac.
- Values are never logged, never in error reports, never returned in full by any endpoint.
- Environment variables and `.env` files are no longer read for any secret. An old
  `ARCGIS_API_KEY`-style variable is ignored.
- Missing key = PropDesk's "no key" behaviour: the source or AI step is skipped and its card
  says so in plain words; the lookup still finishes.
- `scripts/keys.sh list | set <name> | remove <name>`: `set` prompts for the value without
  echo, so it never appears in the shell history or the process list.

| Name | Used by | Required? | Without it | Where to get it and what it costs |
|---|---|---|---|---|
| `openai` | AI card summaries, historical-aerial reading, entitlement reading | Optional | No AI summaries; aerial gallery shows photos without the AI reading; no entitlement reading | https://platform.openai.com/api-keys — Paid per use (prepaid credit on the OpenAI account; PropRisk stops AI calls at the monthly cap, $20.00 by default) |
| `census` | Demographics card (US Census ACS) | Optional but recommended | Demographics card is empty (`census_acs.py:369-380`) | https://api.census.gov/data/key_signup.html — Free |
| `arcgis` | Address-finding fallback 2 and nearby-places fallback | Optional | Address finding uses OpenStreetMap, then Google if set; places fallback off | https://developers.arcgis.com/sign-up/ — Free monthly allowance; past it the service stops unless pay-as-you-go is switched on (limits: https://location.arcgis.com/pricing/) |
| `google_maps` | Address-finding final fallback | Optional | Last fallback skipped; an address no tier finds fails the lookup | https://console.cloud.google.com/google/maps-apis — Paid per call after a free monthly allowance (10,000 geocoding calls since March 2025); a billing account is required (https://developers.google.com/maps/billing-and-pricing/pricing) |
| `brave_search` | Recorded-plat web search | Optional | Plat search uses an old `bing_search` key if one is set, else the free web-page search (PropRisk reads ordinary bing.com result pages, no key; this is not the retired Bing Search API) | https://api-dashboard.search.brave.com — Paid per search (metered billing with a small monthly credit; a card is required; no free plan since early 2026; price: https://api-dashboard.search.brave.com/documentation/pricing) |
| `bing_search` | Recorded-plat web search | Optional (retired) | Plat search uses a Brave key if set, else the free web-page search described above | Retired — no new keys; it was a Paid service. Microsoft retired the Bing Search APIs on 11 August 2025 and closed sign-ups (https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement). Replacement: `brave_search`. The slot stays only because the kept code still reads it, so a key from an older or private agreement keeps working at that agreement's price. |
| `error_sink_token` | Forwarding errors to a receiver (chapter 8) | Optional | Errors stay in the local file only | Not a provider key: whoever runs the error receiver issues it; free on PropRisk's side. The README's error section explains how to set it. |

Prices and free allowances above were checked on 15 November 2026 and change over time; the
"What PropRisk needs" screen shows each provider's link and free/paid status from this table
and links the provider's own pricing page. The optional contact email (1.7) is free: it is the
user's own address.

The `error_sink_token` living in SQLite instead of the environment is a deliberate change
from the error-sink contract, because the owner's rule puts every token in SQLite.
`ERROR_SINK_URL`, `ERROR_SINK_FILE` and `ERROR_SINK_RATE` stay environment settings.

### 1.7 Outbound identification

Several public services (OpenStreetMap Nominatim, Overpass, Census) ask every caller to
identify itself. PropRisk sends `PropRisk/<version> (open-source property lookup)`, plus
`; contact <email>` when `contact_email` is set in Settings. This replaces
`PropDesk/1.0 (propdesk_control_plane site-check)` and `support@reference.app`
(`adapters/_base.py:153-155`, `hifld_power.py:52`, `plats_intel.py:2184-2186`,
`osm_overpass.py:40`, `census_acs.py:53`, `arcgis_geocode.py:231-233` and the
`PropDesk/1.0 <purpose>` strings in `entitlement_reader.py`, `jurisdiction_resolver.py`,
`austin_zoning.py`, `tcad_parcel.py`, `plats_intel.py`, `entitlement_pipeline.py`). The
"What PropRisk needs" screen recommends setting a contact email.

### 1.8 Settings menu

Reached from "Settings" in the top bar. Route `/settings` opens a menu with four screens:

**a. What PropRisk needs** (`/settings/needs`). One checklist, two groups:

- *On this Mac* (shown for reference; checked by the launch script): PostgreSQL 14 or newer,
  Python 3.12 with `uv`, Node 20 or newer — each with its `brew install` command.
- *Keys and settings*: one row per entry of the table in 1.6 plus "Contact email". Each row:
  name, what it is for, Required/Optional/Recommended, what stops working without it, where
  to get it (the sign-up link from the table in 1.6) and whether it is free or paid (from the
  same table), current state (Set …last4 / Not set), and a "Set up" button to its panel on
  screen b.
- The Bing Search row is marked "Retired by Microsoft (August 2025) — use Brave Search
  instead" with Brave's sign-up link; its "Set up" button stays only for an existing key.

**b. Keys and secrets** (`/settings/keys`). One setup panel per name in 1.6:

- Shows Set (last 4 characters, date saved) or Not set.
- "Add" / "Replace" opens a hidden (password-type) field and Save / Cancel.
- Save sends the value once; the field is cleared after saving and the value is never shown
  again. Bounds: 8–512 characters, no spaces or control characters; else "Please check the
  key — it should be 8 to 512 characters with no spaces."
- `openai` is checked with OpenAI (`models.list()`) before it is saved, as PropDesk's
  `put_ai_key` does today (`api/platform.py:196-231`); a failing key (or OpenAI not
  answering) is not saved and the user sees PropDesk's message, word for word:
  "That key didn't work — check it and try again."
  The typed value stays in the field so the user can correct it.
- "Remove" asks once to confirm, then deletes it.
- Saving and removing write one audit row each (`secret.set` / `secret.delete`, last 4
  characters only), as PropDesk does for its AI key (`ai_key.set` / `ai_key.delete`).
- Contact email: an email field (up to 254 characters, basic shape check).

**c. AI spending** (`/settings/spending`):

- This month's AI spend and the monthly cap (default $20.00), a warning line when spend
  reaches the existing `budget_warn_threshold`, and "AI is paused until next month" when the
  cap blocks new AI calls (the existing `blocked` rule).
- Change the cap: whole or cents dollars, $0.00 to $10,000.00; $0 turns AI off.
- This month's public-data usage, as PropDesk's Usage page shows it today: number of lookups,
  calls to public sources, estimated paid-service cost, per source, using the existing plain
  source names (moved from `UsagePage.tsx`).

**d. Where PropRisk works** (`/settings/coverage`). Read from one coverage table,
`app/services/site_check/coverage.py`, served by `GET /api/settings/coverage`:

- **State:** Texas (most sources assume Texas: the address finder adds ", Texas"; Census
  centroids are Texas-only).
- **Connected counties** (state as of this document; the screen always shows the table):

| County | Level | What is connected |
|---|---|---|
| Travis | Full | City limits and ETJ, parcel layers, districts (MUD, ESD, PID, TIRZ, school), fire stations, plats and clerk portal, historic districts (Austin), transit (Capital Metro), city zoning, future land use and planning cases for its cities |
| Williamson | Full | City limits and ETJ, parcel lookup (WCAD), districts, fire (via Round Rock), plats and clerk portal, transit, zoning for its cities |
| Hays | Full | City limits and ETJ, parcel layer, districts, plats and clerk portal, zoning for Buda, Kyle, Dripping Springs, San Marcos |
| Bastrop | Full | City limits and ETJ, parcel layer, districts, fire, plats and clerk portal |
| Caldwell | Partial | Plat records and clerk portal, water/sewer service area |
| Burnet | Partial | County clerk portal and city planning-portal links |
| Blanco | Partial | Parcel layer, county clerk portal link |

- **Cities with zoning data** (13): Austin, Pflugerville, Cedar Park, Georgetown, Hutto, Round
  Rock, Buda, Kyle, Dripping Springs, San Marcos, Leander, Taylor, Elgin (Manor by map
  download). Cities with written zoning standards (10): Austin, Buda, Cedar Park, Dripping
  Springs, Georgetown, Hutto, Kyle, Manor, Pflugerville, Round Rock. Future land use (8):
  Austin, Buda, Kyle, Round Rock, Pflugerville, Hutto, Georgetown, Cedar Park.
- **Texas-wide sources**: TxDOT traffic, roads, bridges and projects; TCEQ and TWDB water
  and sewer service areas; TEA schools; TCEQ environmental records; Texas historical aerial
  photos; Texas parcel layer (TxGIO); HB 1378 district debt reports.
- **Nationwide sources**: FEMA flood zones, USGS elevation and streams, NRCS soils, National
  Wetlands Inventory, EPA ECHO facilities, HIFLD power lines and substations, OpenStreetMap
  places and infrastructure, modern aerial imagery, FCC broadband link.
- Everywhere else in Texas, a lookup still runs; county-specific cards say the county is not
  connected yet.

The builder fills `coverage.py` from the adapters, not from this table; this table is the
expected result. A test (`test_coverage_matches_adapters`) checks every county and city in
`coverage.py` against the adapters' own county and city lists, so the screen cannot drift.

**Missing-key notice.** While `census` or `openai` is not set, the New lookup screen shows one
line above the form: "Some results need a key — see What PropRisk needs." linking to
`/settings/needs`. Lookups run without it. Only these two are named because without them a
card or summary is empty; the other keys are fallbacks that most lookups never reach.

New endpoints (all under `/api/settings`, JSON only):

| Method and path | Body / answer |
|---|---|
| `GET /needs` | the checklist rows with current state |
| `GET /keys` | `[{name, label, is_set, last4, updated_at}]` — never the value |
| `PUT /keys/{name}` | `{value}` → 204; 404 unknown name; 422 bad shape or failed OpenAI check |
| `DELETE /keys/{name}` | 204 (also when not set) |
| `GET /spending` | `{month, spend_cents, budget_cents, warn, blocked, public_data:{…}}` (the kept usage summary minus per-user and per-app parts) |
| `PUT /spending/cap` | `{budget_cents}` 0–1,000,000 → 204 |
| `GET /coverage` | `{state, counties:[…], cities:{zoning, standards, future_land_use}, statewide:[…], nationwide:[…]}` |
| `POST /api/client-errors` | browser error report (chapter 8) → 204 |

### 1.9 Launch and stop (macOS)

`scripts/launch.sh` (Bash, `set -euo pipefail`, every step prints one plain line):

1. Refuses on anything but macOS ("PropRisk's scripts run on a Mac only.").
2. Adds `/opt/homebrew/bin`, `/usr/local/bin` and the newest
   `$(brew --prefix)/opt/postgresql@*/bin` to `PATH`; checks `uv`, `node` ≥ 20, `npm`,
   `initdb`, `pg_ctl`, `postgres` ≥ 14. Missing → names it, prints the `brew install` line,
   exits 1, starts nothing.
3. If PropRisk already runs (pid files in `data/run/` point at live processes and
   `/api/health` answers) → prints "PropRisk is already running at http://127.0.0.1:8765",
   opens it, exits 0. Stale pid files are removed.
4. If port 8765 (or `PROPRISK_PORT`) or 55432 (or `PROPRISK_DB_PORT`) is taken by another
   program → says which port and how to choose another, exits 1.
5. First run, or when the lock files changed: `uv sync --frozen` in `backend/`, `npm ci` and
   `npm run build` in `web/` (rebuild when a hash of `web/src`, `index.html` and the lock
   file differs from the stamp in `data/run/web_build.sha256`).
6. First run: `initdb -D data/postgres -U proprisk --auth=trust` (only this Mac user's
   processes can reach it: it listens on 127.0.0.1 and a socket inside `data/run/`), create
   database `proprisk`.
7. Starts PostgreSQL: `pg_ctl -D data/postgres -l data/logs/postgres.log -o "-p 55432 -k
   <data/run> -c listen_addresses=127.0.0.1" start -w`.
8. `uv run alembic upgrade head`; creates the secret store if missing.
9. Starts the server (`uv run uvicorn app.main:app --host 127.0.0.1 --port 8765`) and the
   worker (`uv run python -m app.worker`) in the background with `nohup`, output to
   `data/logs/server.log` and `data/logs/worker.log`, pids to `data/run/`.
10. Waits up to 60 seconds for `/api/health`; on success prints the address and runs
    `open http://127.0.0.1:8765`; on failure stops what it started, prints the last 20 lines
    of the failing log and exits 1.

`scripts/stop.sh`: sends the worker SIGTERM (it finishes or hands back in-flight work, the
existing graceful shutdown) and waits up to 30 seconds, then SIGKILL; same for the server
(10 seconds); `pg_ctl stop -m fast`; removes pid files; prints what it stopped, or "PropRisk
was not running." Exit 0 in both cases. A lookup cut off by stop is picked up by the existing
stale-job recovery at the next launch (`job_stale_after_seconds`, `job_max_attempts`).

`scripts/test.sh`: creates a throwaway PostgreSQL cluster in a temp folder on a free port,
runs `uv run pytest` and `npm run build && npm run test` against recorded fixtures, removes
the cluster, exits with the tests' status. No network calls.

`scripts/keys.sh`: see 1.6.

Reset: stop, delete `data/`, launch (documented in the README).

### 1.10 Acceptance of the built app (owner's path)

On the owner's Mac: run `scripts/launch.sh` → the browser opens PropRisk with no sign-in →
Settings → What PropRisk needs lists every input and its state → Keys and secrets: add the
OpenAI and Census keys (a wrong OpenAI key is refused with a plain message) → Where PropRisk
works shows Texas and the seven counties → New lookup for `1100 Congress Ave, Austin, TX` →
progress runs to the end → result cards, era gallery and map show → the lookup is in History
→ cancel a second lookup, retry a failed source, delete a lookup → `scripts/stop.sh` stops
everything → launch again: history and keys are still there.

### 1.11 Source inventory (kept unchanged)

All 38 registered sources (`registry.py` `REGISTRY`) and the 3 AI steps. "(off)" = switched off in the registry.

| Source id | Returns | Services called | Key | Coverage |
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

**When a source is slow or down** (unchanged from PropDesk; values in `app/config/settings.py`):

- Every outside call waits at least 60 seconds (`public_intel_http_timeout_seconds`; shorter
  per-module values are raised to it, `adapters/_base.py:84-92`) and retries connection errors
  and HTTP 429/5xx twice with backoff (`public_intel_http_backoff_base_seconds` 1.5, honouring
  Retry-After). TxDOT calls use 4 attempts of up to 90 seconds (`txdot_arcgis_*`). Other
  limits: water/sewer queries 25 s, historical aerials 25 s, entitlement checks 20 s × 2.
- Up to 8 sources run in parallel (`public_intel_max_workers`), at most 2 TxDOT and 4 others
  at a time (`public_intel_provider_concurrency_caps`). There is no overall time limit per
  source: a slow source holds the lookup until its own retries run out.
- One failed source fails only its own entry: the orchestrator catches it
  (`orchestrator.py:789-794`), stores it as failed and partial, and moves on; the result page
  offers "retry" for it (`POST /runs/{id}/retry-sources`). After an HTTP 429 that source pauses
  for 300 seconds (`public_intel_rate_limit_cooldown_seconds`).
- Only an address that no geocoder can place fails the whole lookup (`geocode_failed`,
  `orchestrator.py:1491`, `:1543`). The AI steps never fail a lookup: a missing key, the
  monthly cap, a bad key or an OpenAI error each leaves the card without its AI part.
- Saved source answers are reused for 180 days in "check" mode (`site_check_freshness_days`).

---

## 2. Back end and infrastructure

- Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2, Alembic, psycopg 3, httpx/requests, shapely,
  pyproj, pypdf, numpy, tifffile, openpyxl, pyyaml, openai — unchanged versions from `uv.lock`,
  minus the removed packages (1.3). `uv` manages the environment.
- Two processes plus the database: the server (API + built screens) and the worker (runs
  lookups from the `jobs` table; up to `worker_max_concurrency` = 3 at once; heartbeat,
  sweeper, graceful drain unchanged).
- PostgreSQL 14+ private cluster in `data/postgres`, port 55432, 127.0.0.1 only.
- SQLite secret store in `data/proprisk_secrets.sqlite3`.
- `alembic/versions/0001_proprisk_baseline.py` creates the kept tables exactly as the last
  PropDesk migration leaves them (same columns, types, indexes, partial unique indexes such
  as one active lookup per address and one active retry job per run), minus the dropped
  columns in 1.5, and inserts the local office and owner rows. A schema comparison test
  (`test_baseline_matches_models`) checks the migration against the models.
- All tunables stay in `app/config/settings.py`, overridable by environment variables with the
  same names as today (pydantic-settings; a `.env` beside `backend/` is still read, for
  non-secret settings only).

Targets:
- Fresh database built by the baseline migration matches the models with no difference: test_baseline_matches_models
- All kept server tests pass offline, at least 16 test files with no network access: yes
- No secret setting left in settings.py: yes
- Lookup cut off by a stop is queued again for the next worker: test_recover_below_cap_requeues_job_and_run_together

## 3. Front end

- Vite 8 + React 19 + TypeScript 6 + react-router 7 + maplibre-gl 6, unchanged; built once by
  the launch script into `web/dist` and served by the server.
- The four Public Data screens are copied without change except imports of removed modules.
- New: `pages/settings/SettingsMenu.tsx`, `NeedsPage.tsx`, `KeysPage.tsx`, `SpendingPage.tsx`,
  `CoveragePage.tsx`; `lib/settings.ts` (typed calls to `/api/settings`); new top bar.
  They reuse `index.css` classes and `components/Busy.tsx`; each shows loading, empty and
  error states with the house plain messages.
- `index.css` is copied whole; rules whose class names appear in no kept file (searched
  first) are removed, so the Public Data screens look exactly as before.
- Works in current Safari, Chrome and Firefox on macOS, window width 360 px and up (the
  existing layout already adapts to phone width).

Targets:
- Public Data render checks from the PropDesk smoke suite pass on PropRisk fixtures: test_ui_smoke_public_data
- Each of the 4 Settings screens shows its loading, empty and error state: test_settings_screens_states
- Saved key never shown again in full on any screen or response: yes
- npm run build finishes with 0 type errors: yes

## 4. Networking

- Server binds 127.0.0.1:8765 (`PROPRISK_PORT`); PostgreSQL 127.0.0.1:55432
  (`PROPRISK_DB_PORT`) plus a socket in `data/run/`. Nothing listens on other interfaces.
- Outbound HTTPS to the public sources listed in `docs/DATA_SOURCES.md`, OpenAI, and map
  tiles (`basemap.nationalmap.gov`, `tiles.openfreemap.org`) from the browser.
- No inbound access from other machines; no CORS origins configured.

Targets:
- Server and database refuse connections from another machine on the same network: yes
- Launch fails with a plain message within 5 seconds when port 8765 is taken: test_launch_port_taken

## 5. Latency

- Starting a lookup only validates and queues it (no network on the request path, as today);
  the work runs in the worker. A full lookup takes minutes, bounded by the slowest source's
  retries (no overall time limit per source exists today; unchanged).
- Screen reads come from the local database.

Targets:
- Start-a-lookup request answered in under 500 ms for 20 of 20 requests on a test copy with 1,000 stored lookups: test_create_lookup_latency
- History page request answered in under 300 ms for 20 of 20 requests on a test copy with 1,000 stored lookups: test_history_latency
- Launch on an already-installed Mac reaches a working address in under 60 seconds: test_launch_ready_time

## 6. Load

One user. At most 3 lookups run at once (`worker_max_concurrency`); others wait in the queue.
A second "check" lookup for an address that is already running is refused with the existing
plain message (409).

Targets:
- 10 lookups queued at once all finish with at most 3 running at a time on recorded fixtures: test_worker_runs_at_most_three_lookups
- Not applicable: one person on one Mac; no load beyond the queue above

## 7. Security

Users line: one person on their own Mac; no other accounts, no outsiders. No sign-in by the
owner's direction ("No need for extra security"). What still applies:

- **Secrets** only in the git-ignored SQLite store (0600); never in code, tests, recorded
  fixtures, logs, error reports or the repository; never returned in full. Recorded fixtures
  are scanned for `key=`, `token=`, `Authorization` and known key shapes before delivery, and
  scrubbed.
- **Network exposure**: everything bound to 127.0.0.1. Because any web page open in the same
  browser could try to call `127.0.0.1:8765`, the server refuses a request whose `Host` is not
  `127.0.0.1:<port>` or `localhost:<port>`, and a state-changing request whose `Origin` is
  present and not the app's own; state-changing endpoints accept `application/json` only.
- **Input**: kept site-check bounds; key values 8–512 characters, no whitespace or control
  characters; key names from the allow-list; cap 0–1,000,000 cents; client error reports
  ≤ 16 KB. SQL only through SQLAlchemy/`sqlite3` parameters; launch scripts quote every
  variable and never `eval` input.
- **Costly calls**: AI calls are capped by the monthly cap (existing reservation logic); no
  public endpoint exists, so no rate limit is added.
- **Errors**: plain message with a reference; no stack in any response.
- **Dependencies**: `npm audit` and `uvx pip-audit` run once before delivery; any high or
  critical item is upgraded past or named with its reason.
- `SECURITY.md` says all of the above, including that anyone with access to the Mac user
  account can read the keys, and that the private PostgreSQL trusts local connections (it is
  reachable only from this Mac, and only its own files hold the data).

Targets:
- Secret scan of every delivered file, including recorded fixtures, finds 0 secrets: test_repository_secret_scan
- Request with a foreign Origin to change a key is refused: test_foreign_origin_refused
- Planted key value appears in no log line and no error event: test_secret_never_logged
- npm audit and pip-audit report 0 unexplained high or critical items: yes

## 8. Error handling

- One module, `app/core/error_sink.py`, one entry `report_error(error, where, context)`,
  following the nmhp-error/1 contract: FastAPI exception handler, `sys.excepthook`,
  `threading.excepthook`, the worker's job and thread failures, failed outbound calls already
  logged by adapters, and browser `error`/`unhandledrejection` posted to `/api/client-errors`
  by `web/src/lib/errorReporter.ts`.
- File `data/logs/errors.jsonl` (`ERROR_SINK_FILE`), rotated at 10 MB, 5 kept, 0600.
  Optional forwarding to `ERROR_SINK_URL` with the `error_sink_token` from the secret store.
  Rate `ERROR_SINK_RATE` (60 per minute per fingerprint). Scrubs every secret-store value,
  Authorization and Cookie headers, request bodies.
- The user sees the existing plain message plus "Reference: ABCD-1234".
- Not covered (README says so): hard kills, out-of-memory, failures before the sink starts.
- Source failures keep today's behaviour: one failed source marks only its card partial and
  retryable; only an address no geocoder finds fails the whole lookup; AI steps never fail a
  lookup.

Targets:
- Induced server failure writes 1 event with every required field and the user message carries the same reference: test_error_sink_server_event
- Browser failure arrives as 1 browser event: test_error_sink_browser_event
- Dead forwarding receiver neither crashes the app nor slows a request beyond 200 ms: test_error_sink_dead_receiver
- One failed source leaves the lookup finished with that source marked failed: test_failing_adapter_still_completes_run_with_failed_entry
- An address no geocoder finds fails the lookup with a plain message: test_geocode_failure_fails_run_asynchronously_with_plain_state

## 9. Backup and recovery

Not applicable: the owner said lost data can be rebuilt — lookups are re-run from public sources and keys are re-typed, so PropRisk keeps no backup; the README explains the reset.

## 10. Maintainability

- Code kept as is; changed and new files open with a note on what they do, and changed or new
  functions say what they take and return (the harness rule).
- Lint: `ruff` for Python (existing config), `oxlint` for the screens (existing config).
- Tests: `scripts/test.sh` runs everything offline against recorded fixtures
  (`tests/site_check_fixture_replay.py`); `tests/record_site_check_fixtures.py` re-records.
- Adding a data source, changing the coverage table and changing the database are each a
  step-by-step recipe in `docs/MAINTENANCE.md`.

Targets:
- ruff check reports 0 errors on backend/: yes
- oxlint reports 0 errors on web/: yes
- Whole test suite runs offline in under 10 minutes on a 2020-or-newer Mac: test_suite_offline
- Coverage table matches the adapters' county and city lists: test_coverage_matches_adapters

## 11. Documentation

- `README.md`: what PropRisk is (with a screenshot), what it covers, what it needs, install,
  launch, stop, Settings menu and every key, where data and logs live, reset,
  troubleshooting (port taken, PostgreSQL missing, lookup stuck), licence.
- `docs/ARCHITECTURE.md`: the parts and how a lookup flows from address to cards and map;
  folder map ("what is what") for every top-level folder and every site-check module.
- `docs/DATA_SOURCES.md`: one row per source and AI step (id, what it returns, services
  called, key needed, coverage), as listed in section 1.11.
- `docs/MAINTENANCE.md`: tests, recording fixtures, adding a source, updating coverage,
  changing the database, upgrading packages, reading the error log, and a table of every
  setting (default and what it changes) and every secret name.
- The harness's own `NOTES_FOR_NEXT_TASK.md` stays at the project root, outside `PropRisk/`.
- `SECURITY.md` and `LICENSE`.

Targets:
- README walk-through followed on a clean Mac account reaches a finished lookup: yes
- Every source in the registry has a row in DATA_SOURCES.md: test_data_sources_doc_complete
- Every top-level folder and every site-check module is described in ARCHITECTURE.md: yes

## 12. Support plan

Not applicable: no dedicated support plan was requested for this task.
