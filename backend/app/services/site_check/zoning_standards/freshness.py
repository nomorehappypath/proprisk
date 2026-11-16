"""
core/zoning_standards/freshness.py

YAML-freshness checker for the curated zoning rules. Cities
periodically revise their UDCs (Bond Bills, density bonuses,
overlay adjustments, parking-reduction ordinances). Without
periodic re-verification the curated YAML drifts from current
code and the engine starts citing rules that have been amended.

This module provides:

  - `check_city_freshness(city)` — single-city report
  - `check_all_cities()` — all curated cities at once
  - `report_stale_cities(threshold_days)` — quick filter for
    the maintenance task surface
  - `format_maintenance_report()` — human-readable digest the
    operator (or a scheduled job) can read on demand

Freshness thresholds:
  - 0-90 days   → CURRENT (no action)
  - 91-180 days → STALE (re-verify when next touching the city)
  - 181+ days   → OVERDUE (active risk; queue for re-verification)
  - missing date / invalid → INVALID (yaml malformed)

Future hook: when the project moves to a real CRE app server,
this checker is what a scheduled task hits to email the
operator a weekly maintenance report. Today it's a CLI / library
function — runnable on demand.

Per spec Rule #7, the module is intentionally narrow: dates +
classification + report formatting. No automated re-curation
yet — that's the curator's job (or a future curator agent).
The freshness checker SURFACES the work; it doesn't do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum

from app.services.site_check.zoning_standards.engine import (
    _load_city,
    list_curated_cities,
    reload_caches,
)

# ── Thresholds ──────────────────────────────────────────────────


CURRENT_THRESHOLD_DAYS = 90
STALE_THRESHOLD_DAYS = 180
# 181+ days = OVERDUE


class FreshnessStatus(str, Enum):
    """Where a city's curated YAML sits on the freshness scale."""

    CURRENT = "current"
    STALE = "stale"
    OVERDUE = "overdue"
    INVALID = "invalid"


@dataclass(frozen=True)
class FreshnessReport:
    city: str
    status: FreshnessStatus
    last_verified: date | None
    days_since_verification: int | None
    udc_version: str | None
    udc_source_url: str | None
    district_count: int
    notes: str


def _classify(days: int) -> FreshnessStatus:
    if days <= CURRENT_THRESHOLD_DAYS:
        return FreshnessStatus.CURRENT
    if days <= STALE_THRESHOLD_DAYS:
        return FreshnessStatus.STALE
    return FreshnessStatus.OVERDUE


def check_city_freshness(
    city: str, *, today: date | None = None,
) -> FreshnessReport:
    """Single-city freshness report. `today` injectable for
    deterministic testing."""
    today = today or datetime.now(tz=UTC).astimezone().date()
    cs = _load_city(city)
    if cs is None:
        return FreshnessReport(
            city=city,
            status=FreshnessStatus.INVALID,
            last_verified=None,
            days_since_verification=None,
            udc_version=None,
            udc_source_url=None,
            district_count=0,
            notes="No curated YAML found OR file failed schema validation.",
        )

    try:
        verified = datetime.strptime(
            cs.last_verified, "%Y-%m-%d",
        ).replace(tzinfo=UTC).date()
    except (ValueError, TypeError):
        return FreshnessReport(
            city=city,
            status=FreshnessStatus.INVALID,
            last_verified=None,
            days_since_verification=None,
            udc_version=cs.udc_version,
            udc_source_url=cs.udc_source_url,
            district_count=len(cs.districts),
            notes=f"Invalid last_verified date: {cs.last_verified!r}",
        )

    days = (today - verified).days
    status = _classify(days)

    if status == FreshnessStatus.CURRENT:
        notes = "No action — recent verification."
    elif status == FreshnessStatus.STALE:
        notes = (
            "Re-verify next time you touch this city's "
            "districts. Cross-check 1-2 numeric standards "
            "against current published UDC."
        )
    else:
        notes = (
            "Re-verification overdue. Pull current UDC + "
            "re-extract any district whose standards are "
            "load-bearing for active deals. Bump last_verified "
            "after the pass."
        )

    return FreshnessReport(
        city=city,
        status=status,
        last_verified=verified,
        days_since_verification=days,
        udc_version=cs.udc_version,
        udc_source_url=cs.udc_source_url,
        district_count=len(cs.districts),
        notes=notes,
    )


def check_all_cities(
    *, today: date | None = None,
) -> dict[str, FreshnessReport]:
    """Freshness reports for every curated city. Returns a
    dict keyed by city name."""
    return {
        c: check_city_freshness(c, today=today)
        for c in list_curated_cities()
    }


def report_stale_cities(
    *,
    threshold_days: int = CURRENT_THRESHOLD_DAYS,
    today: date | None = None,
) -> list[FreshnessReport]:
    """All cities older than `threshold_days`. Default returns
    anything not CURRENT (>90 days). Sorted oldest-first so
    the maintenance task processes the highest-risk cities
    before the borderline ones."""
    today = today or datetime.now(tz=UTC).astimezone().date()
    out: list[FreshnessReport] = []
    for city in list_curated_cities():
        report = check_city_freshness(city, today=today)
        if report.days_since_verification is None:
            out.append(report)  # INVALID — also wants attention
            continue
        if report.days_since_verification > threshold_days:
            out.append(report)
    return sorted(
        out,
        key=lambda r: (
            r.days_since_verification or 0,
        ),
        reverse=True,
    )


def format_maintenance_report(
    *, today: date | None = None,
) -> str:
    """Human-readable digest. Suitable for emailing or logging
    on a scheduled cadence."""
    today = today or datetime.now(tz=UTC).astimezone().date()
    reports = check_all_cities(today=today)

    by_status: dict[FreshnessStatus, list[FreshnessReport]] = {
        FreshnessStatus.OVERDUE: [],
        FreshnessStatus.STALE: [],
        FreshnessStatus.CURRENT: [],
        FreshnessStatus.INVALID: [],
    }
    for r in reports.values():
        by_status[r.status].append(r)

    lines: list[str] = [
        f"Zoning standards freshness report — {today.isoformat()}",
        "",
        f"Total curated cities: {len(reports)}",
        (f"  CURRENT  (≤{CURRENT_THRESHOLD_DAYS}d): "
        f"{len(by_status[FreshnessStatus.CURRENT])}"),
        (f"  STALE    ({CURRENT_THRESHOLD_DAYS+1}-"
        f"{STALE_THRESHOLD_DAYS}d): "
        f"{len(by_status[FreshnessStatus.STALE])}"),
        (f"  OVERDUE  (>{STALE_THRESHOLD_DAYS}d): "
        f"{len(by_status[FreshnessStatus.OVERDUE])}"),
        (f"  INVALID  (broken yaml / missing date): "
        f"{len(by_status[FreshnessStatus.INVALID])}"),
        "",
    ]

    for status in (
        FreshnessStatus.OVERDUE,
        FreshnessStatus.STALE,
        FreshnessStatus.INVALID,
        FreshnessStatus.CURRENT,
    ):
        bucket = sorted(
            by_status[status],
            key=lambda r: (
                -(r.days_since_verification or 0),
                r.city,
            ),
        )
        if not bucket:
            continue
        lines.append(f"── {status.value.upper()} ──")
        for r in bucket:
            age = (
                f"{r.days_since_verification}d"
                if r.days_since_verification is not None
                else "n/a"
            )
            verified = (
                r.last_verified.isoformat()
                if r.last_verified else "n/a"
            )
            lines.append(
                f"  {r.city:<20} "
                f"verified={verified}  age={age}  "
                f"districts={r.district_count}"
            )
            if status != FreshnessStatus.CURRENT:
                lines.append(f"      → {r.notes}")
                if r.udc_source_url:
                    lines.append(
                        f"      UDC source: {r.udc_source_url}"
                    )
        lines.append("")

    return "\n".join(lines).rstrip()


__all__ = [
    "CURRENT_THRESHOLD_DAYS",
    "STALE_THRESHOLD_DAYS",
    "FreshnessReport",
    "FreshnessStatus",
    "check_all_cities",
    "check_city_freshness",
    "format_maintenance_report",
    "report_stale_cities",
]


# ── CLI entry point ────────────────────────────────────────────


if __name__ == "__main__":
    # Lets the operator run:
    #     venv/bin/python -m core.zoning_standards.freshness
    # to get the maintenance digest on demand.
    reload_caches()
    print(format_maintenance_report())
