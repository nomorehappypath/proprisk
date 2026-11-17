"""Reads the region files: one folder per state, one YAML file per county.

    app/regions/<state code, lowercase>/state.yaml
    app/regions/<state code, lowercase>/counties/<county>.yaml

Every file is checked against the models below when the app starts. A wrong
file stops startup with a message naming the file and the field. The adapters
get back plain dicts in the same shapes their old in-code tables had, so data
moved into these files without any change to adapter logic.

Commented blank files to copy: app/regions/_template/. Recipe: docs/EXTENDING.md.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

REGIONS_DIR = Path(__file__).resolve().parent


class RegionFileError(RuntimeError):
    """A region file is missing, unreadable, or has a wrong field."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StateFile(_Strict):
    code: str = Field(pattern=r"^[A-Z]{2}$")
    name: str
    fips: str = Field(pattern=r"^\d{2}$")
    search_box: tuple[float, float, float, float]
    census_centroids: str | None = None
    statewide_summary: list[str] = []


class MapLayer(_Strict):
    url: str
    name_field: str
    where: str | None = None
    fixed_name: str | None = None
    ignore_values: list[str] | None = None


class Jurisdiction(_Strict):
    source: str
    source_url: str
    city_limits: list[MapLayer] = []
    etj: list[MapLayer] = []
    limited_purpose: list[MapLayer] | None = None
    no_hit_advisories: list[str] | None = None
    unincorporated_on_no_hit: bool | None = None


class Parcels(_Strict):
    source_label: str
    source_url: str
    query_url: str
    id_fields: list[str]
    numeric_id_fields: list[str] | None = None
    cad_account_fields: list[str] | None = None
    owner_fields: list[str] | None = None
    situs_fields: list[str] | None = None
    legal_fields: list[str] | None = None
    acre_fields: list[str] | None = None
    sqft_area_fields: list[str] | None = None
    value_fields: list[str] | None = None
    land_value_fields: list[str] | None = None
    out_fields: str
    source_status: str | None = None
    source_advisory: str | None = None


class District(_Strict):
    label: str
    kind: Literal["subdivision", "mud", "wcid", "esd", "school"]
    urls: list[str] = Field(min_length=1)


class FireStations(_Strict):
    url: str


class Clerk(_Strict):
    name: str
    url: str
    plat_url: str | None = None
    deed_instructions: str
    plat_instructions: str
    fallback_url: str | None = None
    fallback_label: str | None = None
    fallback_note: str | None = None


class Subdivisions(_Strict):
    source_label: str
    layer_url: str
    query_url: str
    name_field: str | None = None
    code_field: str | None = None


class CountyFile(_Strict):
    name: str
    level: Literal["Full", "Partial"]
    summary: str
    sample_address: str
    jurisdiction: Jurisdiction | None = None
    parcels: Parcels | None = None
    appraisal_search_url: str | None = None
    districts: list[District] | None = None
    fire_stations: FireStations | None = None
    clerk: Clerk | None = None
    subdivisions: Subdivisions | None = None


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """A key written twice would silently replace the first one; refuse it."""

    def construct_mapping(self, node, deep=False):
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"'{key}' is written twice", key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _empty_keys(value: Any, where: str = "") -> list[str]:
    """Keys written with no value. Leave a section out instead of leaving it empty."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{where}.{key}" if where else str(key)
            found += [path] if item is None else _empty_keys(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _empty_keys(item, f"{where}.{index}")
    return found


def _read(path: Path, model: type[_Strict]) -> dict[str, Any]:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDuplicateKeysLoader)  # a SafeLoader
    except (OSError, yaml.YAMLError) as exc:
        raise RegionFileError(f"{path}: cannot read this file ({exc})") from exc
    empty = _empty_keys(raw)
    if empty:
        raise RegionFileError(
            f"{path}: {', '.join(empty)}: has no value (fill it in, or remove the line)"
        )
    try:
        parsed = model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(file)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise RegionFileError(f"{path}: {problems}") from exc
    # exclude_unset keeps the old table shapes: a key the file leaves out is
    # absent from the dict, exactly as it was absent from the in-code table.
    return parsed.model_dump(exclude_unset=True)


@lru_cache(maxsize=1)
def load_regions(root: Path = REGIONS_DIR) -> dict[str, dict[str, Any]]:
    """{"TX": {"state": {...}, "counties": {"travis": {...}, ...}}, ...}.
    County keys are the file names (lowercase)."""
    regions: dict[str, dict[str, Any]] = {}
    for state_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if state_dir.name.startswith(("_", ".")) or state_dir.name == "__pycache__":
            continue
        state_path = state_dir / "state.yaml"
        if not state_path.is_file():
            raise RegionFileError(f"{state_dir}: missing state.yaml")
        state = _read(state_path, StateFile)
        if state["code"].lower() != state_dir.name:
            raise RegionFileError(
                f"{state_path}: code: {state['code']} does not match the folder name "
                f"'{state_dir.name}' (the folder must be the code in lowercase)"
            )
        counties = {
            path.stem: _read(path, CountyFile)
            for path in sorted((state_dir / "counties").glob("*.yaml"))
        }
        regions[state["code"]] = {"state": state, "counties": counties}
    return regions


def state(code: str) -> dict[str, Any] | None:
    entry = load_regions().get(code.upper())
    return entry["state"] if entry else None


def counties(code: str) -> dict[str, dict[str, Any]]:
    """Every county file of a state, keyed by file name, in file-name order."""
    entry = load_regions().get(code.upper())
    return entry["counties"] if entry else {}


def county_sections(code: str, section: str) -> dict[str, Any]:
    """{county key: that county's `section`} for every county file that has it."""
    return {
        key: data[section]
        for key, data in counties(code).items()
        if section in data
    }
