"""txdot_extended adapter - broker-grade TxDOT synthesis.

This is not another raw TxDOT fetcher in the normal Public Data path.
The orchestrator runs it after the independent TxDOT adapters and passes
their results in config["upstream_results"]. The adapter then builds one
consumer-facing transportation screen without duplicating provider calls.

Crash data is intentionally a capability/status block. TxDOT CRIS public
crash extracts are credentialed/registered data, not an open ArcGIS layer
like AADT/projects/roadway/bridges. Until credentials or an authorized
extract ingestion path exist, this adapter must not report "zero crashes"
or send the broker to a manual lookup as if the system were complete.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

SOURCE_ID = "txdot_extended"
_RADIUS_MI = 5.0


_FUNCTIONAL_CLASS_LABELS = {
    1: "Interstate",
    2: "Other freeway / expressway",
    3: "Other principal arterial",
    4: "Minor arterial",
    5: "Major collector",
    6: "Minor collector",
    7: "Local road",
}


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for txdot_extended")

    sess = http_session(session)
    upstream = {}
    if isinstance(config, dict):
        maybe_results = config.get("upstream_results")
        if isinstance(maybe_results, dict):
            upstream = maybe_results

    facts_by_source: dict[str, dict[str, Any]] = {}
    input_status: dict[str, str] = {}
    errors: list[str] = []

    # A missing input is fetched through the same per-state choice as
    # normal dispatch (spec 2.6): a state's own road source, the Texas
    # adapter only for Texas, otherwise "not connected".
    from app.services.site_check.orchestrator import _load_adapter_fetch

    for source_id in (
        "txdot_aadt",
        "txdot_projects",
        "txdot_roadway",
        "txdot_bridges",
    ):
        facts, status, source_errors = _facts_from_upstream(
            upstream, source_id,
        )
        if status == "missing":
            result = _load_adapter_fetch(source_id)(identity, session=sess)
            if result.coverage_status in (
                CoverageStatus.NOT_YET_WIRED,
                CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
            ):
                facts, status = {}, str(result.coverage_status.value)
            else:
                facts = result.normalized_facts or {}
                status = "queried"
            source_errors = list(result.errors or [])
        facts_by_source[source_id] = facts
        input_status[source_id] = status
        for error in source_errors:
            errors.append(f"{source_id}: {error}")

    traffic_counts = _traffic_counts(facts_by_source.get("txdot_aadt"))
    roadways = _roadways(facts_by_source.get("txdot_roadway"))
    projects = _projects(facts_by_source.get("txdot_projects"))
    bridges = _bridges(facts_by_source.get("txdot_bridges"))

    normalized = {
        "source": "TxDOT extended public-data synthesis",
        "radius_miles": _RADIUS_MI,
        "input_status": input_status,
        "traffic_exposure": _traffic_exposure(traffic_counts),
        "roadway_context": _roadway_context(roadways),
        "project_pipeline": _project_pipeline(projects),
        "bridge_constraints": _bridge_constraints(
            bridges,
            facts_by_source.get("txdot_bridges") or {},
        ),
        "crash_data": _crash_data_status(),
        "broker_read": _broker_read(traffic_counts, projects, bridges),
        "why_it_matters": (
            "TxDOT exposure, planned construction, and bridge/access "
            "constraints shape driveway feasibility, truck routing, timing "
            "risk, and whether a traffic study is likely to be worth buying."
        ),
        "next_step": (
            "Use this as the automated transportation screen. If crash "
            "severity is material to the decision, configure authorized TxDOT "
            "CRIS public-extract ingestion so PropDesk can compute crash "
            "metrics from the official extract instead of treating safety as "
            "unknown."
        ),
    }

    state = (getattr(identity, "state", "TX") or "TX").upper()
    if state != "TX":
        connected = [
            facts_by_source[sid] for sid, status in input_status.items()
            if status in ("reused", "queried")
        ]
        if not connected:
            from app.services.site_check.orchestrator import (
                not_connected_result,
            )
            return not_connected_result(SOURCE_ID, state)
        normalized = _for_other_state(normalized, connected, state, bridges)

    has_any_signal = any((
        traffic_counts,
        roadways,
        projects,
        bridges,
    ))
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if has_any_signal else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=normalized,
        errors=errors or None,
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _for_other_state(
    normalized: dict[str, Any],
    connected: list[dict[str, Any]],
    state: str,
    bridges: list[dict[str, Any]],
) -> dict[str, Any]:
    """The same summary for a state whose own road sources answered:
    its agency named instead of TxDOT, no Texas-only crash system, and
    no claim the inputs cannot back (spec 2.6)."""
    from app.regions import states as state_names

    # The four road inputs can come from different agencies (CDOT, FHWA,
    # the bridge inventory), so the summary names the state's public road
    # data, and each card names its own agency.
    name = state_names.name_of(state)
    agency = name

    def swap(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("TxDOT", agency)
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        return value

    out = swap(normalized)
    out["source"] = f"{agency} public-data synthesis"
    out["provider_short"] = agency
    out["crash_data"] = {
        "source": None,
        "integration_status": "not_connected",
        "available_in_this_run": False,
        "is_zero_crash_result": False,
        "read": (
            f"Crash records are not connected for {name} yet. This run does "
            "not contain crash metrics and must not be read as a zero-crash "
            "result."
        ),
    }
    out["next_step"] = (
        "Use this as the automated transportation screen. Crash records are "
        f"not connected for {name} yet, so treat safety as unknown."
    )
    pipeline = out.get("project_pipeline") or {}
    largest = pipeline.get("largest_project") or {}
    if pipeline.get("count_within_radius") and not largest.get("estimated_cost_usd"):
        cost = largest.get("cost_range")
        pipeline["read"] = (
            f"{pipeline['count_within_radius']} {agency} project(s) are mapped "
            "within the screened radius"
            + (f"; published cost range of the first: {cost}." if cost else ".")
        )
    constraints = out.get("bridge_constraints") or {}
    if bridges and any(b.get("posting_status") == "unknown" for b in bridges):
        posted = constraints.get("posted_bridge_count") or 0
        low = constraints.get("low_condition_bridge_count") or 0
        if not posted:
            constraints["posted_bridge_count"] = None
            constraints["constraint_level"] = (
                "posted_or_low_condition_bridge_nearby" if low
                else "posting_unknown_in_screened_set"
            )
            constraints["read"] = (
                f"{low} low-condition bridge indicator(s) found; whether any "
                f"nearby bridge is weight-posted is not published by {agency} "
                "for these bridges."
            )
    return out


def _facts_from_upstream(
    upstream: dict[str, Any],
    source_id: str,
) -> tuple[dict[str, Any], str, list[str]]:
    if source_id not in upstream:
        return {}, "missing", []
    result = upstream.get(source_id)
    if not isinstance(result, dict):
        return {}, "unusable_upstream_shape", []
    coverage = result.get("coverage_status")
    if coverage in {
        CoverageStatus.NOT_YET_WIRED.value,
        CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION.value,
    }:
        return {}, str(coverage), list(result.get("errors") or [])
    facts = result.get("normalized_facts")
    if not isinstance(facts, dict):
        facts = {}
    return facts, "reused", list(result.get("errors") or [])


def _traffic_counts(facts: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (facts or {}).get("traffic_counts")
    if not isinstance(rows, list):
        return []
    cleaned = [row for row in rows if isinstance(row, dict)]
    return sorted(
        cleaned,
        key=lambda row: int(row.get("aadt") or 0),
        reverse=True,
    )


def _roadways(facts: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (facts or {}).get("roadways")
    if not isinstance(rows, list):
        return []
    cleaned = [row for row in rows if isinstance(row, dict)]
    return sorted(
        cleaned,
        key=lambda row: (
            1 if row.get("distance_mi") is None else 0,
            float(row.get("distance_mi") or 999),
            -(int(row.get("speed_limit") or 0)),
            -(int(row.get("num_lanes") or 0)),
        ),
    )


def _projects(facts: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (facts or {}).get("items") or (facts or {}).get("projects")
    if not isinstance(rows, list):
        return []
    cleaned = [row for row in rows if isinstance(row, dict)]
    return sorted(
        cleaned,
        key=lambda row: float(row.get("estimated_cost_usd") or 0),
        reverse=True,
    )


def _bridges(facts: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (facts or {}).get("items")
    if not isinstance(rows, list):
        return []
    cleaned = [row for row in rows if isinstance(row, dict)]
    return sorted(
        cleaned,
        key=lambda row: float(row.get("distance_mi") or 999),
    )


def _traffic_exposure(counts: list[dict[str, Any]]) -> dict[str, Any]:
    top = counts[0] if counts else None
    top_aadt = int(top.get("aadt") or 0) if top else None
    return {
        "top_aadt": top,
        "traffic_counts_considered": len(counts),
        "aadt_band": _aadt_band(top_aadt),
        "high_volume_roads": [
            row for row in counts if int(row.get("aadt") or 0) >= 20_000
        ][:5],
        "read": (
            f"Top mapped traffic count is {top_aadt:,} AADT on "
            f"{top.get('road_name') or 'the leading TxDOT segment'}."
            if top and top_aadt
            else "No usable TxDOT traffic count was available in the screened radius."
        ),
    }


def _roadway_context(roadways: list[dict[str, Any]]) -> dict[str, Any]:
    primary = roadways[0] if roadways else None
    highest_speed = max(
        (_int_or_none(row.get("speed_limit")) or 0 for row in roadways),
        default=0,
    ) or None
    largest_lane_count = max(
        (_int_or_none(row.get("num_lanes")) or 0 for row in roadways),
        default=0,
    ) or None
    engineering_details = _roadway_engineering_details(roadways)
    return {
        "primary_roadway": _with_functional_label(primary),
        "roadways": [_with_functional_label(row) for row in roadways[:8]],
        "roadway_count": len(roadways),
        "highest_speed_mph": highest_speed,
        "largest_lane_count": largest_lane_count,
        "engineering_details": engineering_details,
        "access_fields_requiring_field_verification": (
            engineering_details["fields_requiring_verification"]
        ),
        "read": (
            f"Primary screened roadway is {primary.get('road_name') or 'unknown'} "
            f"at {highest_speed or 'unknown'} mph. "
            f"{engineering_details['read']}"
            if primary else
            "No roadway inventory segment was available in the screened radius."
        ),
    }


def _roadway_engineering_details(
    roadways: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = roadways[0] if roadways else None
    fields = ("row_width", "crossover_regulation", "turn_lane_presence")
    resolved_fields = [
        field for field in fields
        if primary is not None
        and primary.get(field) not in (None, "", "manual_verification_required")
    ]
    unresolved = set()
    for row in roadways:
        manual_fields = row.get("manual_access_fields")
        if isinstance(manual_fields, list):
            unresolved.update(
                str(field) for field in manual_fields
                if str(field) in {
                    "row_width",
                    "crossover_regulation",
                    "turn_lane_presence",
                    "stacking_length_ft",
                }
            )
    if roadways:
        unresolved.add("stacking_length_ft")
    source_fields = [
        "ROW_MIN",
        "MED_TYPE",
        "MED_WID",
        "ACES_CTRL",
        "ACCEL_DECEL_LANE",
        "RT_TURN_LANE",
        "LT_TURN_LANE",
        "LANE_WIDTH",
    ]
    return {
        "source": "TxDOT Roadway Inventory",
        "source_fields": source_fields,
        "primary": {
            "road_name": primary.get("road_name"),
            "row_width_ft": primary.get("row_width_ft"),
            "lane_width_ft": primary.get("lane_width_ft"),
            "median_type_label": primary.get("median_type_label"),
            "median_width_ft": primary.get("median_width_ft"),
            "access_control_label": primary.get("access_control_label"),
            "crossover_regulation": primary.get("crossover_regulation"),
            "crossover_regulation_label": primary.get("crossover_regulation_label"),
            "turn_lane_presence": primary.get("turn_lane_presence"),
            "turn_lane_presence_label": primary.get("turn_lane_presence_label"),
            "stacking_length_status": primary.get("stacking_length_status"),
        } if primary else None,
        "resolved_fields": resolved_fields,
        "fields_requiring_verification": sorted(unresolved),
        "confidence": "screened_indicator" if resolved_fields else "unknown",
        "read": (
            "ROW/median/access-control/turn-lane signals are screened from "
            "TxDOT inventory where populated; stacking/storage length and "
            "site driveway approval still need TIA/site-plan/as-built or field evidence."
            if resolved_fields else
            "TxDOT inventory did not resolve ROW, crossover, or turn-lane "
            "details for the primary road; plan or field verification remains required."
        ),
    }


def _project_pipeline(projects: list[dict[str, Any]]) -> dict[str, Any]:
    largest = projects[0] if projects else None
    near_term_cutoff = datetime.now(UTC).year + 2
    near_term = [
        project for project in projects
        if _year_from(project.get("estimated_let_date")) is not None
        and (
            _year_from(project.get("estimated_let_date")) or 9999
        ) <= near_term_cutoff
    ]
    return {
        "count_within_radius": len(projects),
        "items": projects[:10],
        "largest_project": largest,
        "near_term_projects": near_term[:5],
        "read": (
            f"{len(projects)} TxDOT project(s) are mapped within the screened radius; "
            f"largest visible estimate is {_money(largest.get('estimated_cost_usd'))}."
            if largest else
            "No mapped TxDOT projects were found in the screened radius."
        ),
    }


def _bridge_constraints(
    bridges: list[dict[str, Any]],
    bridge_facts: dict[str, Any],
) -> dict[str, Any]:
    posted = [
        bridge for bridge in bridges
        if bridge.get("posted_weight_us_tons")
        or str(bridge.get("load_restriction") or "").upper() == "Y"
    ]
    low_condition = [
        bridge for bridge in bridges
        if (_int_or_none(bridge.get("worst_cond_rtg")) or 9) <= 4
    ]
    return {
        "count_within_radius": len(bridges),
        "nearest_distance_mi": bridge_facts.get("nearest_distance_mi"),
        "posted_bridge_count": len(posted),
        "low_condition_bridge_count": len(low_condition),
        "items": bridges[:6],
        "constraint_level": (
            "posted_or_low_condition_bridge_nearby"
            if posted or low_condition else
            "no_posted_bridge_constraint_in_screened_set"
            if bridges else "no_bridge_records_in_screened_set"
        ),
        "read": (
            f"{len(posted)} posted-weight and {len(low_condition)} low-condition "
            "bridge indicator(s) found in the screened set."
            if posted or low_condition else
            "No posted-weight bridge constraint appears in the screened TxDOT bridge set."
            if bridges else
            "No TxDOT bridge records were found in the screened radius."
        ),
    }


def _crash_data_status() -> dict[str, Any]:
    return {
        "source": "TxDOT CRIS",
        "access_mode": "credentialed_public_extract",
        "integration_status": "credentials_not_configured",
        "available_in_this_run": False,
        "is_zero_crash_result": False,
        "official_access_url": "https://cris.dot.state.tx.us/",
        "extract_guide_url": (
            "https://www.txdot.gov/content/dam/docs/division/trf/"
            "crash-records/cris-guide.pdf"
        ),
        "metrics_when_configured": [
            "3-5 year crash count within radius",
            "fatal and suspected-serious injury crashes",
            "intersection/frontage-road related crash share",
            "pedestrian/cyclist crash indicators",
            "crash rate normalized against AADT where segment matching is available",
        ],
        "read": (
            "Crash records require authorized TxDOT CRIS public-extract "
            "ingestion. This run does not contain crash metrics and must not "
            "be read as a zero-crash result."
        ),
    }


def _broker_read(
    traffic_counts: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    bridges: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if traffic_counts:
        top = traffic_counts[0]
        parts.append(
            f"top traffic exposure {int(top.get('aadt') or 0):,} AADT "
            f"on {top.get('road_name') or 'the leading TxDOT segment'}"
        )
    if projects:
        parts.append(f"{len(projects)} mapped TxDOT project(s)")
    if bridges:
        parts.append(f"{len(bridges)} nearby TxDOT bridge record(s)")
    if not parts:
        return (
            "PropDesk checked the extended TxDOT screen, but no usable "
            "transportation signal was returned for this radius."
        )
    return (
        "PropDesk combined the public TxDOT transportation layers: "
        + "; ".join(parts)
        + "."
    )


def _with_functional_label(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    fc = _int_or_none(out.get("functional_class"))
    if fc is not None:
        out["functional_class_label"] = _FUNCTIONAL_CLASS_LABELS.get(
            fc,
            f"class {fc}",
        )
    return out


def _aadt_band(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 40_000:
        return "very_high"
    if value >= 20_000:
        return "high"
    if value >= 10_000:
        return "moderate"
    return "low"


def _year_from(value: Any) -> int | None:
    text = str(value or "")
    if len(text) < 4:
        return None
    try:
        year = int(text[:4])
    except ValueError:
        return None
    return year if 1900 <= year <= 2100 else None


def _money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "unknown cost"
    return f"${amount:,.0f}"


def _int_or_none(value: Any) -> int | None:
    try:
        if value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _failed(reason: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "source": "TxDOT extended public-data synthesis",
            "crash_data": _crash_data_status(),
        },
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
