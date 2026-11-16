"""Checks the "Where PropRisk works" table (app/services/site_check/coverage.py)
against the adapters' own county and city lists, so the Settings screen
cannot silently drift from what the sources actually cover."""

from pathlib import Path

from app.services.site_check import coverage
from app.services.site_check.adapters import austin_zoning, future_land_use

CURATED_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "site_check" / "zoning_standards" / "curated"


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def test_zoning_cities_match_austin_zoning_adapter():
    # The 13 cities with full zoning-data coverage (the adapter's directly
    # wired set). Manor is covered only by a separate map download, so it is
    # named on its own in the Settings screen rather than counted here.
    wired_cities = {city.name for city in austin_zoning._WIRED_CITIES}
    assert set(coverage.ZONING_CITIES) == wired_cities
    downloadable_cities = {city.name for city in austin_zoning._DOWNLOADABLE_CITIES}
    assert downloadable_cities.isdisjoint(coverage.ZONING_CITIES)


def test_future_land_use_cities_match_adapter():
    adapter_keys = set(future_land_use._LAYERS_BY_JURISDICTION.keys())
    coverage_keys = {_slug(name) for name in coverage.FUTURE_LAND_USE_CITIES}
    assert coverage_keys == adapter_keys


def test_standards_cities_match_curated_files():
    curated_slugs = {path.stem for path in CURATED_DIR.glob("*.yaml")}
    coverage_slugs = {_slug(name) for name in coverage.STANDARDS_CITIES}
    assert coverage_slugs == curated_slugs


def test_counties_are_non_empty_and_named_once():
    names = [row["county"] for row in coverage.COUNTIES]
    assert len(names) == len(set(names)), "a county must appear only once in the table"
    assert all(row["level"] in ("Full", "Partial") for row in coverage.COUNTIES)


def test_get_coverage_shape():
    result = coverage.get_coverage()
    assert result["state"] == "Texas"
    assert {row["county"] for row in result["counties"]} == {row["county"] for row in coverage.COUNTIES}
    assert result["cities"]["zoning"] == coverage.ZONING_CITIES
    assert result["cities"]["standards"] == coverage.STANDARDS_CITIES
    assert result["cities"]["future_land_use"] == coverage.FUTURE_LAND_USE_CITIES
