"""Canonical 16-property smoke matrix — port of
legacy_services/scripts/smoke_public_intel.py.

Per docs/specs/public_data.md §5 — "smoke matrices port in
full, not as a subset" (memory feedback_port_dont_redesign).
The matrix exists because legacy_services caught real per-county
/ per-jurisdictional-kind regressions with it. Subsetting
silently drops that coverage.

Two consumers:
  - tests/test_public_intel_smoke_matrix.py — pytest suite.
    Skips by default; runs when env PUBLIC_INTEL_SMOKE_LIVE=1
    is set.
  - scripts/smoke_public_intel.py — standalone runner that
    prints a coverage matrix to stdout.

Each entry carries pre-resolved `lat` / `lng` (hardcoded from
prior legacy_services live geocoder runs) so the smoke suite
exercises the per-source ADAPTERS at known coordinates without
depending on the live geocoder. Geocoder behavior is covered
separately by tests/test_public_intel_orchestrator.py with a
mocked fetch.
"""

from __future__ import annotations

# Verbatim from legacy_services/scripts/smoke_public_intel.py
# DEFAULT_PROPERTIES + lat/lng captured from prior live runs
# of the legacy_services Nominatim/Google geocoder. lat/lng are
# the cached values from public_intel.json across the matrix.
PROPERTIES: list[dict] = [
    # ── Original baseline (5) ──
    {
        "label": "CR 130 & Chandler — unincorporated WilCo",
        "address": "County Road 130 & Chandler Road, Hutto TX 78634",
        "geocode_query": "County Road 130 and Chandler Road, Hutto, TX 78634",
        "county": "williamson",
        "lat": 30.541,
        "lng": -97.5436,
    },
    {
        "label": "13105 FM 1100 — Manor",
        "address": "13105 FM 1100, Manor TX 78653",
        "geocode_query": "13105 FM 1100, Manor, TX 78653",
        "county": "travis",
        "lat": 30.376,
        "lng": -97.566,
    },
    {
        "label": "7778 FM 973 — unincorporated Travis",
        "address": "7778 FM 973, Travis County TX",
        "geocode_query": "7778 FM 973, Austin, TX",
        "county": "travis",
        "lat": 30.142,
        "lng": -97.7,
    },
    {
        "label": "8509 Burleson — Austin 2-mile ETJ",
        "address": "8509 Burleson Road, Austin TX",
        "geocode_query": "8509 Burleson Road, Austin, TX",
        "county": "travis",
        "lat": 30.18,
        "lng": -97.717,
    },
    {
        "label": "1503 S 1st — Austin FULL purpose",
        "address": "1503 S 1st Street, Austin TX",
        "geocode_query": "1503 S 1st Street, Austin, TX",
        "county": "travis",
        "lat": 30.2401,
        "lng": -97.7574,
    },
    # ── Hays (Kyle) ──
    {
        "label": "168 Rieber Rd — Kyle area",
        "address": "168 Rieber Rd, Kyle, TX 78640",
        "geocode_query": "168 Rieber Rd, Kyle, TX 78640",
        "county": "hays",
        "lat": 30.005,
        "lng": -97.875,
    },
    {
        "label": "22431 IH-35 Frontage — Kyle",
        "address": "22431 South Interstate 35 Frontage Road, Kyle, TX 78640",
        "geocode_query": "22431 S IH 35 Frontage Rd, Kyle, TX 78640",
        "county": "hays",
        "lat": 29.974,
        "lng": -97.882,
    },
    # ── Williamson (Taylor) ──
    {
        "label": "452 TX-95 — Taylor",
        "address": "452 TX-95 Rds, Taylor, TX 76574",
        "geocode_query": "452 TX-95, Taylor, TX 76574",
        "county": "williamson",
        "lat": 30.585,
        "lng": -97.401,
    },
    {
        "label": "600 CR 405 — Taylor (unincorporated)",
        "address": "600 Co Rd 405, Taylor, TX 76574",
        "geocode_query": "600 County Road 405, Taylor, TX 76574",
        "county": "williamson",
        "lat": 30.601,
        "lng": -97.461,
    },
    # ── Travis (SE Austin / FM 812) ──
    {
        "label": "9800 FM 812 — SE Travis",
        "address": "9800 FM 812, Austin, TX 78719",
        "geocode_query": "9800 FM 812, Austin, TX 78719",
        "county": "travis",
        "lat": 30.156,
        "lng": -97.668,
    },
    {
        "label": "10445 S Hwy 183 — SE Austin",
        "address": "10445 S Hwy 183, Austin, TX 78747",
        "geocode_query": "10445 S Highway 183, Austin, TX 78747",
        "county": "travis",
        "lat": 30.144,
        "lng": -97.706,
    },
    # ── Williamson (Round Rock) ──
    {
        "label": "2111 A W Grimes — Round Rock",
        "address": "2111 A W Grimes Blvd, Round Rock, TX 78664",
        "geocode_query": "2111 A W Grimes Blvd, Round Rock, TX 78664",
        "county": "williamson",
        "lat": 30.521,
        "lng": -97.687,
    },
    {
        "label": "201 Gattis School Rd — Round Rock",
        "address": "201 Gattis School Rd, Round Rock, TX 78664",
        "geocode_query": "201 Gattis School Rd, Round Rock, TX 78664",
        "county": "williamson",
        "lat": 30.508,
        "lng": -97.673,
    },
    # ── Williamson (Leander) ──
    {
        "label": "289 183A Frontage — Leander",
        "address": "289 183A Frontage Road, Leander, TX 78641",
        "geocode_query": "289 183A Frontage Rd, Leander, TX 78641",
        "county": "williamson",
        "lat": 30.578,
        "lng": -97.853,
    },
    {
        "label": "15901 Ronald Reagan Bldg 2 — Leander",
        "address": "15901 Ronald Reagan Blvd Building 2, Leander, TX 78641",
        "geocode_query": "15901 Ronald Reagan Blvd, Leander, TX 78641",
        "county": "williamson",
        "lat": 30.604,
        "lng": -97.876,
    },
    {
        "label": "1000 Bonita Verde Dr — Leander",
        "address": "1000 Bonita Verde Dr, Leander, TX 78641",
        "geocode_query": "1000 Bonita Verde Dr, Leander, TX 78641",
        "county": "williamson",
        "lat": 30.594,
        "lng": -97.838,
    },
]


# Sanity: the matrix is exactly 16 entries. legacy_services has
# 16; we have 16. Adding/removing requires deliberate scope.
assert len(PROPERTIES) == 16, (
    f"smoke matrix must be 16 properties (legacy_services coverage floor); "
    f"got {len(PROPERTIES)}"
)
