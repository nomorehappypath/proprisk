"""
core/zoning_standards/engine.py

Reads per-city YAML files from `curated/` and exposes lookup
functions over the rules. Pure data layer — no HTTP, no LLM,
no inference. The output of any query is a curated rule from
the YAML, with the source code section as the citation.

Three primary entry points:

  - `get_district_rules(city, district)` — full DistrictRules
    object for one district (or None if not curated yet).
  - `is_use_permitted(city, district, use_id)` — checks
    permitted/conditional/prohibited and returns the matching
    UseEntry or None.
  - `get_dimensional_standard(city, district, key)` — single
    standard with citation.

The engine caches the parsed YAML files in process memory so
repeated queries don't re-read the disk.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

import yaml

from app.services.site_check.zoning_standards.schema import (
    CitedValue,
    CityZoningStandards,
    DistrictRules,
    UseEntry,
)

logger = logging.getLogger(__name__)


_CURATED_DIR = Path(__file__).parent / "curated"


# ── City file loading ──────────────────────────────────────────


@cache
def _load_city(city: str) -> CityZoningStandards | None:
    """Parse `curated/{city}.yaml` into a CityZoningStandards.
    Returns None when the file doesn't exist (city not yet
    curated). Cached forever — restart to pick up changes."""
    city_lc = (city or "").strip().lower()
    if not city_lc:
        return None
    path = _CURATED_DIR / f"{city_lc}.yaml"
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(
            "zoning_standards: failed to parse %s: %s",
            path, exc,
        )
        return None
    try:
        return CityZoningStandards.model_validate(raw)
    except Exception as exc:
        logger.error(
            "zoning_standards: schema validation failed for %s: %s",
            path, exc,
        )
        return None


def list_curated_cities() -> list[str]:
    """Returns the set of city names with a YAML file under
    `curated/`. Used by the adapter to honestly report which
    cities have full standards coverage."""
    if not _CURATED_DIR.exists():
        return []
    return sorted(
        p.stem for p in _CURATED_DIR.glob("*.yaml")
    )


def reload_caches() -> None:
    """Drop the LRU cache. Used by tests; production restarts
    pick up edits naturally."""
    _load_city.cache_clear()


# ── Lookups ────────────────────────────────────────────────────


def get_district_rules(
    city: str, district: str,
) -> DistrictRules | None:
    """Full rule set for one district in one city. Returns
    None when the city isn't curated OR the district key isn't
    present (which is the same answer as 'we have not yet
    curated this district')."""
    cs = _load_city(city)
    if cs is None:
        return None
    return cs.districts.get(district)


def is_use_permitted(
    city: str, district: str, use_id: str,
) -> tuple[str | None, UseEntry | None]:
    """Check whether `use_id` is permitted/conditional/
    prohibited in the (city, district) tuple.

    Returns (status, entry) where status is one of:
      'permitted', 'conditional', 'prohibited', None

    None status means the use isn't listed for this district
    — which under most TX UDCs means "prohibited unless
    listed elsewhere," but the safest memo language is "not
    explicitly addressed; verify with planning."
    """
    rules = get_district_rules(city, district)
    if rules is None:
        return None, None
    for entry in rules.permitted_uses:
        if entry.use_id == use_id:
            return "permitted", entry
    for entry in rules.conditional_uses:
        if entry.use_id == use_id:
            return "conditional", entry
    for entry in rules.prohibited_uses:
        if entry.use_id == use_id:
            return "prohibited", entry
    return None, None


def get_dimensional_standard(
    city: str, district: str, key: str,
) -> CitedValue | None:
    """Single dimensional standard. Returns None when not
    curated."""
    rules = get_district_rules(city, district)
    if rules is None:
        return None
    return rules.dimensional_standards.get(key)


def get_parking_rule(
    city: str, district: str, use_id: str,
) -> ParkingRule | None:  # noqa: F821 (forward ref OK at runtime)
    """First parking rule matching this use in this district."""
    rules = get_district_rules(city, district)
    if rules is None:
        return None
    for rule in rules.parking:
        if rule.use_id == use_id:
            return rule
    return None


__all__ = [
    "get_dimensional_standard",
    "get_district_rules",
    "get_parking_rule",
    "is_use_permitted",
    "list_curated_cities",
    "reload_caches",
]


if __name__ == "__main__":
    print("curated cities:", list_curated_cities())
