"""Which source answers a card's slot for a lookup (spec 2.6).

A state or a county can fill any slot with a small module:

    regions/<st>/sources/<slot>.py            statewide
    regions/<st>/sources/<county>/<slot>.py   one county

`pick(slot, state, county)` looks for the county module, then the state
module. It returns None when neither exists; the caller then uses the
built-in adapter if that adapter covers the state (Texas today), or says
"Not connected for <State> yet".
"""

from __future__ import annotations

import importlib
import re
from functools import lru_cache
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parent
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _county_folder(county: str | None) -> str:
    name = (county or "").strip().lower().removesuffix(" county").strip()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


@lru_cache(maxsize=None)
def _load(state: str, county: str, slot: str) -> ModuleType | None:
    parts = [state] + ([county] if county else [])
    path = _ROOT.joinpath(state, "sources", *parts[1:], f"{slot}.py")
    if not path.is_file():
        return None
    dotted = ".".join(["app", "regions", state, "sources", *parts[1:], slot])
    return importlib.import_module(dotted)


def pick(slot: str, state: str | None, county: str | None = None) -> ModuleType | None:
    """The state's or county's own module for this slot, or None."""
    st = (state or "TX").strip().lower()
    if not _NAME.match(st) or not _NAME.match(slot):
        return None
    county_dir = _county_folder(county)
    if county_dir and _NAME.match(county_dir):
        module = _load(st, county_dir, slot)
        if module is not None:
            return module
    return _load(st, "", slot)


def modules_for(state: str, county: str | None = None) -> dict[str, ModuleType]:
    """Every source module of a state (county=None) or of one county."""
    st = state.strip().lower()
    folder = _ROOT / st / "sources"
    if county:
        folder = folder / _county_folder(county)
    found: dict[str, ModuleType] = {}
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        module = (
            _load(st, _county_folder(county), path.stem) if county
            else _load(st, "", path.stem)
        )
        if module is not None:
            found[path.stem] = module
    return found
