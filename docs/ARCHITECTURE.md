# Architecture

PropRisk is one server process, one background worker process, and one
private PostgreSQL database, all on the owner's own Mac. The server also
serves the built screens, so opening `http://127.0.0.1:8765/` is the whole
app. There is no sign-in: every request resolves to the one fixed local
office and owner the baseline migration creates.

## How a lookup flows

1. The owner types an address (and optionally a parcel ID) on the New
   Lookup screen and submits it.
2. The browser calls `POST /api/site-check`. The server validates the
   input, writes one `site_check_runs` row (`status=queued`) and one
   `jobs` row in the same transaction, and answers `202` immediately — no
   outside network call happens on the request path.
3. The worker process (`backend/app/worker.py`) polls the `jobs` table,
   claims the job, and hands it to the site-check handler
   (`backend/app/services/site_check/worker_handler.py`).
4. The orchestrator (`orchestrator.py`) runs the registered sources
   (`registry.py`) in parallel waves, geocoding the address first, then
   fanning out to every applicable source adapter
   (`adapters/*.py`), respecting the concurrency caps and retry rules in
   `app/config/settings.py`. Each source's answer is cached
   (`source_cache.py`) and reused for 180 days in "check" mode.
5. `cards.py` composes the adapters' raw facts into the result cards the
   screen renders; `narratives.py`, `historical_aerials.py` and
   `entitlement_reader.py` add the three AI-written parts (each optional,
   gated by the OpenAI key and the monthly spending cap in
   `app/services/gateway.py`).
6. The orchestrator writes the finished result back onto the
   `site_check_runs` row and marks the job done. The screen, which has
   been polling `GET /api/site-check/runs/{id}`, renders the cards, the
   historical-aerial gallery, and (via `map_context.py`) the interactive
   map.
7. The lookup now appears in History (`GET /api/site-check/runs`), can be
   cancelled while running, retried per failed source, or deleted.

A lookup cut off by `scripts/stop.sh` is picked up again by the worker's
own stale-job recovery the next time `scripts/launch.sh` runs — nothing is
silently lost.

## Processes

| Process | What it does | Started by |
|---|---|---|
| PostgreSQL | Stores every table below, in `data/postgres`, port 55432, 127.0.0.1 only | `scripts/launch.sh` |
| Server (`uvicorn app.main:app`) | Serves `/api/*` and the built screens (`web/dist`) | `scripts/launch.sh` |
| Worker (`python -m app.worker`) | Runs queued lookups; heartbeat, stale-job sweeper, graceful drain | `scripts/launch.sh` |

## Folder map

```
PropRisk/
  README.md            what PropRisk is, install, launch, stop, settings, troubleshooting
  LICENSE               Apache License 2.0 text (NOTICE: copyright and contact)
  SECURITY.md           the trust model and what is/isn't protected
  SPEC.md                (copied from propdesk_public/Specs/) the full build specification
  .gitignore
  backend/               Python server + worker
    app/
      main.py            FastAPI app: routers, the Host/Origin guard, static-file fallback
      worker.py          the background job runner
      models.py          every kept SQLAlchemy table
      db.py              the SQLAlchemy engine/session
      api/
        site_check.py    the kept Public Data endpoints (POST/GET/.../cancel/retry/delete)
        settings.py       the Settings menu's endpoints (needs, keys, spending, coverage)
        client_errors.py  receives browser error reports
        deps.py           the fixed local office/owner dependency functions
      core/
        secret_store.py   the one SQLite key/secret store
        error_sink.py     the central error log, with optional forwarding
        local_identity.py the two fixed ids the baseline migration inserts
        job_queue.py      the generic background-job primitives the worker uses
      config/settings.py  every non-secret tunable (no secrets live here)
      regions/            one folder per state, one YAML file per county (loader.py checks them at startup; _template/ holds the blank files)
      services/
        gateway.py        AI key resolution, budget reservation, metering
        audit.py           the one-row-per-mutation audit log
        site_check/        the whole Public Data engine — see below
      cli/keys.py          the command-line secret-store tool behind scripts/keys.sh
      cli/check_region.py  the county/state proof command behind scripts/check_region.sh
    alembic/versions/0001_proprisk_baseline.py   the one migration: every table, the two fixed rows
    tests/                 the kept and adjusted test suite
  web/                    React screens
    src/
      App.tsx              routes and the top bar (no sign-in)
      pages/publicdata/    the four kept Public Data screens
      pages/settings/      the four new Settings screens
      lib/                 typed API clients (publicData.ts, settings.ts, api.ts, errorReporter.ts)
      components/Busy.tsx  the shared loading indicator
    scripts/               ui_smoke.mjs, map_smoke.mjs, capture_fixtures.mjs, fixtures/
  scripts/                launch.sh, stop.sh, test.sh, keys.sh, check_region.sh
  docs/                   this file, DATA_SOURCES.md, MAINTENANCE.md, EXTENDING.md (add a county)
  data/                   created at first launch; never in git — database, secrets, logs, pids
```

### `backend/app/services/site_check/` — the Public Data engine

| File | What it does |
|---|---|
| `registry.py` | The list of every source: 38 registered, 35 active, 3 switched off |
| `orchestrator.py` | Runs the registered sources in parallel waves, anchors the subject, reconciles the geocoded point against a supplied parcel |
| `adapters/*.py` | One module per source — calls the outside service, returns a typed `AdapterResult` |
| `identity.py` | The `PropertyIdentity` type every adapter takes |
| `contract.py` | The `AdapterResult` contract every adapter returns |
| `cards.py` | Builds the result cards the screen renders from the adapters' raw facts |
| `narratives.py` | The per-card AI summary step |
| `historical_aerials.py` | The era-photo gallery and its AI "what was here before" reading |
| `entitlement_reader.py` | The AI read of a city's own planning pages, with every case re-verified |
| `source_cache.py` | The 180-day read-through cache of public source facts |
| `usage.py` | Per-run, per-provider call and estimated-cost metering |
| `coverage.py` | The data behind Settings → "Where PropRisk works" (county rows come from `app/regions/`) |
| `county_source_registry.py`, `county_clerk_portals.py` | Which county-specific data/portal a lookup should use (clerk portals come from `app/regions/`) |
| `map_context.py`, `map_contract.py` | Builds the interactive map's layers from a finished run |
| `service.py` | The create/read/cancel/retry/delete business logic the API layer calls |
| `worker_handler.py` | Registers the `site_check` job kind with the worker |

## Data stored

PostgreSQL (`backend/app/models.py`): `tenants` and `users` (one fixed row
each — the "office" and "owner" every request resolves to), `audit_log`,
`ai_usage` (AI spend metering), `jobs` (the background job queue),
`site_check_runs`, `site_check_source_cache`, `site_check_provider_usage`,
`site_check_coverage_gaps`.

SQLite (`data/proprisk_secrets.sqlite3`): one `secrets` table — see
`backend/app/core/secret_store.py` and SPEC.md chapter 1.6.

## Why this shape

PropRisk is PropDesk's Public Data feature with everything else removed:
the same backend and frontend stacks, the same site-check engine, kept
unchanged, because the owner's direction was "keep everything else the
same." The only new code is what single-user, no-sign-in, locally-stored
operation requires: the secret store, the Settings menu, the launch/stop
scripts, and the central error sink.
