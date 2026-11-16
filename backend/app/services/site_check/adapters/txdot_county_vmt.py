"""txdot_county_vmt adapter — TxDOT DISCOS county VMT allocator.

Reads the FY2024 District and County Statistics workbook and extracts
county-level "Daily Vehicle Miles on State System". This is explicitly
on-system only: TxDOT-maintained roads, not every city/county street.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

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

SOURCE_ID = "txdot_county_vmt"
DISCOS_WORKBOOK_URLS = {
    2025: (
        "https://www.txdot.gov/content/dam/docs/division/str/discos/"
        "fy2025-district-and-county-statistics.xlsx"
    ),
    2024: (
        "https://www.dot.state.tx.us/apps-cg/discos/"
        "FY2024_DISTRICT_and_COUNTY_STATISTICS.xlsx"
    ),
    2023: (
        "https://www.dot.state.tx.us/apps-cg/discos/"
        "FY2023_DISTRICT_and_COUNTY_STATISTICS.xlsx"
    ),
    2022: (
        "https://www.dot.state.tx.us/apps-cg/discos/"
        "FY2022_DISTRICT_and_COUNTY_STATISTICS.xlsx"
    ),
    2021: (
        "https://www.dot.state.tx.us/apps-cg/discos/"
        "FY2021_DISTRICT_and_COUNTY_STATISTICS.xlsx"
    ),
    2020: (
        "https://www.dot.state.tx.us/apps-cg/discos/"
        "FY2020_DISTRICT_and_COUNTY_STATISTICS.xlsx"
    ),
}
DISCOS_WORKBOOK_URL = DISCOS_WORKBOOK_URLS[2025]
_TIMEOUT_S = 45
_SHEET_NAMES = ("COUNTY_STATISTICS", "County Statistics")


def _normalize_county(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = text.removesuffix(" county")
    return text


def _load_workbook_from_response(resp: requests.Response):
    try:
        import openpyxl
    except Exception as exc:
        raise RuntimeError("openpyxl is required for TxDOT workbook parsing") from exc

    return openpyxl.load_workbook(
        BytesIO(resp.content),
        data_only=True,
        read_only=True,
    )


def _county_sheet(workbook):
    for sheet_name in _SHEET_NAMES:
        if sheet_name in workbook.sheetnames:
            return workbook[sheet_name]
    raise ValueError(
        "TxDOT workbook missing county statistics sheet "
        f"{' / '.join(_SHEET_NAMES)!r}"
    )


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


def _series_summary(series: list[dict], key: str) -> dict:
    usable = [item for item in series if item.get(key) is not None]
    if not usable:
        return {"available": False, "direction": "unknown", "points": []}
    usable = sorted(usable, key=lambda item: int(item["fiscal_year"]))
    latest = usable[-1]
    by_year = {int(item["fiscal_year"]): item for item in usable}
    latest_year = int(latest["fiscal_year"])
    prior_1 = by_year.get(latest_year - 1)
    prior_3 = by_year.get(latest_year - 3)
    prior_5 = by_year.get(latest_year - 5)
    pct_5yr = _pct_change(latest.get(key), prior_5.get(key) if prior_5 else None)
    return {
        "available": True,
        "latest_fiscal_year": latest_year,
        "latest_value": latest.get(key),
        "change_1yr_pct": _pct_change(latest.get(key), prior_1.get(key) if prior_1 else None),
        "change_3yr_pct": _pct_change(latest.get(key), prior_3.get(key) if prior_3 else None),
        "change_5yr_pct": pct_5yr,
        "direction": _trend_direction(pct_5yr),
        "points": usable[-6:],
    }


def _extract_county(workbook, county: str) -> dict:
    ws = _county_sheet(workbook)
    target = _normalize_county(county)
    matched: dict | None = None
    for row in ws.iter_rows(values_only=True):
        county_name = row[0]
        if not county_name:
            continue
        name = str(county_name).strip()
        if name.lower() in {"county name", "county"}:
            continue
        normalized = _normalize_county(name)
        if normalized in {"state total", "total"}:
            break
        if _normalize_county(name) == target:
            daily_vehicle_miles = float(row[12] or 0)
            reported_share = float(row[13] or 0)
            matched = {
                "county": name.title(),
                "district_abbr": row[2],
                "population_2023": int(row[10] or 0),
                "population_percent": float(row[11] or 0),
                "county_on_system_daily_vehicle_miles": daily_vehicle_miles,
                "county_vmt_share_reported": reported_share,
            }
    if matched is None:
        raise ValueError(f"county {county!r} not found in TxDOT workbook")
    share = matched["county_vmt_share_reported"]
    matched["county_vmt_share"] = share
    matched["state_on_system_daily_vehicle_miles"] = (
        matched["county_on_system_daily_vehicle_miles"] / share
        if share else None
    )
    population = matched.get("population_2023")
    matched["county_on_system_daily_vmt_per_capita"] = (
        matched["county_on_system_daily_vehicle_miles"] / population
        if population else None
    )
    return matched


def _fetch_county_workbook(
    sess: requests.Session,
    *,
    fiscal_year: int,
    county: str,
) -> dict:
    url = DISCOS_WORKBOOK_URLS[fiscal_year]
    resp = sess.get(url, timeout=_TIMEOUT_S, headers={"Accept": "*/*"})
    resp.raise_for_status()
    workbook = _load_workbook_from_response(resp)
    facts = _extract_county(workbook, county)
    facts.update({
        "fiscal_year": fiscal_year,
        "vmt_calendar_year": fiscal_year - 1,
        "source_url": url,
    })
    return facts


def _history_for_county(
    sess: requests.Session,
    *,
    county: str,
    errors: list[str],
) -> list[dict]:
    series: list[dict] = []
    for fiscal_year in sorted(DISCOS_WORKBOOK_URLS):
        try:
            facts = _fetch_county_workbook(sess, fiscal_year=fiscal_year, county=county)
        except Exception as exc:
            errors.append(f"TxDOT DISCOS FY{fiscal_year} history fetch failed: {exc}")
            continue
        series.append({
            "fiscal_year": facts["fiscal_year"],
            "vmt_calendar_year": facts["vmt_calendar_year"],
            "county": facts["county"],
            "population": facts.get("population_2023"),
            "population_percent": facts.get("population_percent"),
            "county_on_system_daily_vehicle_miles": facts.get(
                "county_on_system_daily_vehicle_miles"
            ),
            "county_vmt_share": facts.get("county_vmt_share"),
            "county_on_system_daily_vmt_per_capita": facts.get(
                "county_on_system_daily_vmt_per_capita"
            ),
            "source_url": facts.get("source_url"),
        })
    return series


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    county = _normalize_county(identity.county)
    if not county:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={},
            errors=["county required for txdot_county_vmt"],
            fetched_at=datetime.now(UTC),
        )

    sess = http_session(session)
    errors: list[str] = []
    try:
        facts = _fetch_county_workbook(
            sess,
            fiscal_year=max(DISCOS_WORKBOOK_URLS),
            county=county,
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
            errors=[f"TxDOT DISCOS county VMT fetch failed: {exc}"],
            fetched_at=datetime.now(UTC),
        )

    history = _history_for_county(sess, county=county, errors=errors)
    trend_summary = {
        "county_on_system_daily_vehicle_miles": _series_summary(
            history, "county_on_system_daily_vehicle_miles"
        ),
        "county_vmt_share": _series_summary(history, "county_vmt_share"),
        "county_on_system_daily_vmt_per_capita": _series_summary(
            history, "county_on_system_daily_vmt_per_capita"
        ),
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            if errors else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.PARTIAL if errors else CoverageStatus.COVERED,
        normalized_facts={
            "source_dataset": "TxDOT District and County Statistics",
            "is_on_system_only": True,
            "historical_series": history,
            "trend_summary": trend_summary,
            **facts,
        },
        errors=errors or None,
        fetched_at=datetime.now(UTC),
        identifier_used=f"county:{county}",
        advisories=[
            ("TxDOT DISCOS county VMT is on-system only and excludes local "
            "city/county streets. Use as a county allocation proxy, not a "
            "complete measure of driving activity.")
        ],
    )


__all__ = ["SOURCE_ID", "fetch"]
