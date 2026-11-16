"""Site Check ("Public Data") service package.

Per docs/specs/site_check.md (SPEC PASSED round 5). Phase A port of
the reference app's `public_intel` package at reference_backend @ 132d1f8, with
the §3.2 AI severance (no AI SDK imports, no gateway coupling, no
reader/navigator modules) and the §3.4 shell re-homing (PropDesk attribution, no shipped
secrets, PropDesk tables + jobs queue).

Subpackage layout:
  - contract.py        — AdapterResult + status enums
  - identity.py        — PropertyIdentity
  - registry.py        — adapter registry (38 registrations: 35 active,
                         3 wired-inactive, per spec §4)
  - adapters/<name>.py — one per source_id; deterministic HTTP only
  - hifld_power.py     — HIFLD fetcher extracted from the reference app's
                         data_center_public_sources.py (spec §3.1)
  - orchestrator.py    — wave dispatch, identity mutations, anchor
                         reconciliation, derived builders
  - service.py         — run lifecycle, reuse seed, retry contract
  - worker_handler.py  — jobs (kind="site_check") entry point
  - zoning_standards/  — curated-YAML engine (10 cities)
  - bg_centroids/      — Texas block-group centroids (census_acs)
"""
