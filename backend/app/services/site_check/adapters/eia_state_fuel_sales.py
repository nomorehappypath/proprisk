"""eia_state_fuel_sales adapter — EIA SEDS gasoline/diesel anchor.

The originally proposed Prime Supplier series is not current enough
for 2026 feasibility work: the live EIA PET bulk file still carries
Texas prime-supplier rows, but the latest values stop at 2022-03.
For v1 this adapter uses current EIA State Energy Data System 2024
motor gasoline and distillate fuel oil consumption estimates instead.

This is an annual, state-level consumption anchor, not monthly retail
station sales. Downstream gas-feasibility code must treat it as a
weak market-context check, never as a primary gallons model.
"""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import StringIO

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "eia_state_fuel_sales"
EIA_SEDS_MOTOR_GASOLINE_URL = (
    "https://www.eia.gov/state/seds/data.php?"
    "incfile=%2Fstate%2Fseds%2Fsep_fuel%2Fhtml%2Ffuel_use_mg.html&sid=TX"
)
EIA_SEDS_DISTILLATE_URL = (
    "https://www.eia.gov/state/seds/data.php?"
    "incfile=/state/seds/sep_fuel/html/fuel_use_df.html&sid=TX"
)
EIA_SEDS_PHYSICAL_CSV_URL = (
    "https://www.eia.gov/state/seds/sep_update/use_all_phy_update.csv"
)
_TIMEOUT_S = 30
_BARREL_TO_GALLONS = 42
_TREND_YEARS = 5


class _TextTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_cell = False
        self._current: list[str] = []
        self.cells: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"td", "th"}:
            self._in_cell = True
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._in_cell:
            text = " ".join("".join(self._current).split())
            if text:
                self.cells.append(text)
            self._in_cell = False
            self._current = []


def _number(text: str) -> int | None:
    cleaned = re.sub(r"[^0-9.-]", "", text or "")
    if not cleaned:
        return None
    try:
        return round(float(cleaned))
    except ValueError:
        return None


def _extract_texas_row(html: str, fields: tuple[str, ...]) -> dict:
    parser = _TextTableParser()
    parser.feed(html)
    cells = parser.cells
    try:
        idx = cells.index("Texas")
    except ValueError as exc:
        raise ValueError("EIA SEDS Texas row not found") from exc
    values = cells[idx + 1:idx + 1 + len(fields)]
    if len(values) < len(fields):
        raise ValueError("EIA SEDS Texas row incomplete")
    parsed = [_number(value) for value in values]
    if any(value is None for value in parsed):
        raise ValueError("EIA SEDS Texas physical-unit values invalid")
    return dict(zip(fields, parsed, strict=True))


def _with_gallon_fields(row: dict) -> dict:
    out = dict(row)
    for key, value in row.items():
        if not key.endswith("_thousand_barrels"):
            continue
        gallon_key = key.replace("_thousand_barrels", "_annual_gallons")
        daily_key = key.replace("_thousand_barrels", "_daily_gallons")
        gallons = int(value) * 1000 * _BARREL_TO_GALLONS
        out[gallon_key] = gallons
        out[daily_key] = round(gallons / 365)
    return out


def _pct_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior in {None, 0}:
        return None
    return round(((float(current) - float(prior)) / float(prior)) * 100, 2)


def _trend_direction(pct: float | None, *, stable_band: float = 2.0) -> str:
    if pct is None:
        return "unknown"
    if pct >= stable_band:
        return "rising"
    if pct <= -stable_band:
        return "falling"
    return "stable"


def _series_points(row: dict[str, str]) -> list[dict]:
    points: list[dict] = []
    for key, value in row.items():
        if not key.isdigit():
            continue
        parsed = _number(value)
        if parsed is None:
            continue
        gallons = parsed * 1000 * _BARREL_TO_GALLONS
        points.append({
            "year": int(key),
            "thousand_barrels": parsed,
            "daily_gallons": round(gallons / 365),
        })
    return sorted(points, key=lambda item: item["year"])


def _trend_summary(points: list[dict]) -> dict:
    usable = [p for p in points if p.get("daily_gallons") is not None]
    if not usable:
        return {
            "available": False,
            "direction": "unknown",
            "points": [],
        }
    latest = usable[-1]
    by_year = {int(p["year"]): p for p in usable}
    latest_year = int(latest["year"])
    one_year = by_year.get(latest_year - 1)
    three_year = by_year.get(latest_year - 3)
    five_year = by_year.get(latest_year - 5)
    window = [p for p in usable if latest_year - _TREND_YEARS <= int(p["year"]) <= latest_year]
    avg_5yr = (
        round(sum(int(p["daily_gallons"]) for p in window) / len(window))
        if window
        else None
    )
    pct_5yr = _pct_change(latest.get("daily_gallons"), five_year.get("daily_gallons") if five_year else None)
    return {
        "available": True,
        "latest_year": latest_year,
        "latest_daily_gallons": latest.get("daily_gallons"),
        "change_1yr_pct": _pct_change(
            latest.get("daily_gallons"),
            one_year.get("daily_gallons") if one_year else None,
        ),
        "change_3yr_pct": _pct_change(
            latest.get("daily_gallons"),
            three_year.get("daily_gallons") if three_year else None,
        ),
        "change_5yr_pct": pct_5yr,
        "latest_vs_5yr_avg_pct": _pct_change(latest.get("daily_gallons"), avg_5yr),
        "direction": _trend_direction(pct_5yr),
        "points": usable[-6:],
    }


def _historical_seds_series(sess: requests.Session) -> dict:
    resp = sess.get(
        EIA_SEDS_PHYSICAL_CSV_URL,
        timeout=_TIMEOUT_S,
        headers={"Accept": "text/csv,*/*"},
    )
    resp.raise_for_status()
    rows: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(StringIO(resp.text)):
        if row.get("State") != "TX":
            continue
        msn = row.get("MSN")
        if msn in {"MGACP", "DFACP"}:
            rows[msn] = row
    if "MGACP" not in rows:
        raise ValueError("EIA SEDS historical Texas motor-gasoline transportation row missing")

    gasoline_points = _series_points(rows["MGACP"])
    diesel_points = _series_points(rows.get("DFACP", {}))
    return {
        "source_url": EIA_SEDS_PHYSICAL_CSV_URL,
        "gasoline_transportation_daily_gallons": gasoline_points,
        "diesel_transportation_daily_gallons": diesel_points,
        "trend_summary": {
            "gasoline_transportation_daily_gallons": _trend_summary(gasoline_points),
            "diesel_transportation_daily_gallons": _trend_summary(diesel_points),
        },
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    sess = http_session(session)
    errors: list[str] = []
    try:
        gas_resp = sess.get(EIA_SEDS_MOTOR_GASOLINE_URL, timeout=_TIMEOUT_S)
        gas_resp.raise_for_status()
        gas_row = _extract_texas_row(
            gas_resp.text,
            (
                "commercial_thousand_barrels",
                "industrial_thousand_barrels",
                "transportation_thousand_barrels",
                "total_thousand_barrels",
            ),
        )
    except Exception as exc:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=(
                SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            ),
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={},
            errors=[f"EIA SEDS motor gasoline fetch failed: {exc}"],
            fetched_at=datetime.now(UTC),
        )

    distillate_row: dict[str, int] = {}
    try:
        diesel_resp = sess.get(EIA_SEDS_DISTILLATE_URL, timeout=_TIMEOUT_S)
        diesel_resp.raise_for_status()
        distillate_row = _extract_texas_row(
            diesel_resp.text,
            (
                "residential_thousand_barrels",
                "commercial_thousand_barrels",
                "industrial_thousand_barrels",
                "transportation_thousand_barrels",
                "electric_power_thousand_barrels",
                "total_thousand_barrels",
            ),
        )
    except Exception as exc:
        errors.append(f"EIA SEDS distillate fetch failed: {exc}")

    historical: dict = {}
    try:
        historical = _historical_seds_series(sess)
    except Exception as exc:
        errors.append(f"EIA SEDS historical time-series fetch failed: {exc}")

    gasoline = {
        "fuel": "motor_gasoline",
        "source_url": EIA_SEDS_MOTOR_GASOLINE_URL,
        **_with_gallon_fields(gas_row),
    }
    diesel_distillate = {
        "fuel": "distillate_fuel_oil",
        "source_url": EIA_SEDS_DISTILLATE_URL,
        **_with_gallon_fields(distillate_row),
    } if distillate_row else None
    facts = {
        "source_dataset": "EIA State Energy Data System",
        "source_url": EIA_SEDS_MOTOR_GASOLINE_URL,
        "state": "Texas",
        "state_abbr": "TX",
        "latest_year": 2024,
        "fuel": "motor_gasoline",
        "units": "thousand_barrels",
        **gas_row,
        "total_annual_gallons": gasoline["total_annual_gallons"],
        "transportation_annual_gallons": (
            gasoline["transportation_annual_gallons"]
        ),
        "total_daily_gallons": gasoline["total_daily_gallons"],
        "transportation_daily_gallons": (
            gasoline["transportation_daily_gallons"]
        ),
        "gasoline": gasoline,
        "gasoline_total_daily_gallons": gasoline["total_daily_gallons"],
        "gasoline_transportation_daily_gallons": (
            gasoline["transportation_daily_gallons"]
        ),
        "anchor_quality": "annual_state_consumption_estimate",
        "not_retail_station_sales": True,
        "prime_supplier_rejected_reason": (
            "EIA Prime Supplier Texas motor-gasoline series is "
            "discontinued/stale; live PET bulk latest period is 2022-03."
        ),
    }
    if historical:
        facts.update({
            "historical_series": historical,
            "trend_summary": historical["trend_summary"],
        })
    if diesel_distillate:
        facts.update({
            "diesel_distillate": diesel_distillate,
            "diesel_total_daily_gallons": (
                diesel_distillate["total_daily_gallons"]
            ),
            "diesel_transportation_daily_gallons": (
                diesel_distillate["transportation_daily_gallons"]
            ),
        })
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            if errors else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=(
            CoverageStatus.PARTIAL if errors else CoverageStatus.COVERED
        ),
        normalized_facts=facts,
        errors=errors,
        fetched_at=datetime.now(UTC),
        identifier_used="state:TX",
        advisories=[
            ("EIA SEDS is annual state consumption, not monthly retail station "
            "sales. Use only as weak fuel-demand context.")
        ],
    )


__all__ = ["SOURCE_ID", "fetch"]
