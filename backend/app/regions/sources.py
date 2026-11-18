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
from functools import cache
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parent
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _county_folder(county: str | None) -> str:
    name = (county or "").strip().lower().removesuffix(" county").strip()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


@cache
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


def settle_coverage(slot: str, coverage: str, facts: dict, errors: list | None) -> str:
    """The honest coverage of a state's or county's own source (spec 2.6).

    A source that reported errors and returned none of the facts its card
    needs did not answer, whatever it says: "manual_verification_required",
    so the card says nothing was checked instead of "0" or "none". With some
    of those facts present, an answer with errors is at most "partial"."""
    from app.services.site_check.card_fields import REQUIRED

    if not errors or coverage in (
        "manual_verification_required", "not_supported_in_jurisdiction", "not_yet_wired",
    ):
        return coverage
    required = REQUIRED.get(slot, ())
    answered = any(facts.get(field) not in (None, "", [], {}) for field in required)
    if not answered:
        return "manual_verification_required"
    return "partial" if coverage == "covered" else coverage


def stamp(module: ModuleType | None) -> dict[str, object] | None:
    """What a result records about the module that produced it."""
    if module is None:
        return None
    return {"module": module.__name__, "version": str(getattr(module, "VERSION", 1))}


def result_matches_current_source(
    slot: str,
    facts: dict,
    *,
    state: str,
    county: str | None,
    built_in_covers: bool,
) -> bool:
    """May a saved result for `slot` be reused for this state and county?

    Only when the source that would answer now is the one that produced it:
    the same state or county module and VERSION, or the built-in adapter
    (no module stamp) when it covers the state. Embedded parts
    (`embedded_sources: {part_slot: {module, version}}`) are checked the
    same way.
    """
    own = stamp(pick(slot, state, county))
    saved_module = facts.get("source_module")
    if own is not None:
        if saved_module != own["module"] or str(facts.get("source_version")) != own["version"]:
            return False
    elif saved_module or not built_in_covers:
        return False
    # Parts answered inside this slot (the state environmental records inside
    # environmental_screening): the saved part must come from the source that
    # would answer now, including "none then, one now" (a newly connected
    # state source must be fetched, not skipped).
    from app.services.site_check.card_fields import EMBEDDED

    saved_parts = facts.get("embedded_sources") or {}
    for part_slot, parent in EMBEDDED.items():
        if parent != slot:
            continue
        now = stamp(pick(part_slot, state, county)) or {}
        saved = {k: str(v) for k, v in (saved_parts.get(part_slot) or {}).items()}
        if now != saved:
            return False
    return True
