"""Site Check service layer — enqueue / reuse seed / history.

Per docs/specs/site_check.md §7–§9. Responsibilities:

  - build_refresh_reuse_seed: the §9.2 freshness-window reuse seed
  - enqueue_run: create a SiteCheckRun row + a jobs row so the
    worker picks up the actual fetch
  - enqueue_source_retry: the §8.4 source-scoped retry
  - get_run / get_history: tenant-scoped read paths with the §7.6
    read-path reconciliation

The orchestrator (orchestrator.run_collect) does the actual
work; this service just brokers between the API layer and the
async queue.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.job_queue import enqueue_job, recover_stale_job
from app.models import Job, SiteCheckRun
from app.services import audit
from app.services.site_check import registry

log = logging.getLogger("propdesk.site_check.service")


_ACTIVE_STATUSES = {"queued", "running"}


def _reuse_max_age() -> timedelta:
    """The §9.1 freshness window. A user-triggered re-check is
    incremental: successful public-source facts stay reusable for
    `site_check_freshness_days` (default 180); a new run calls only
    sources that are missing, failed, expired, or invalidated by a
    refreshed upstream dependency. The original fetched_at is preserved
    across reuse so repeatedly re-checking never extends stale data
    indefinitely. Read from settings at call time — never a constant —
    so a configured non-default value drives every threshold."""
    return timedelta(days=get_settings().site_check_freshness_days)


def _job_stale_after() -> timedelta:
    """§7.4 stale cutoff, from settings (job_stale_after_seconds)."""
    return timedelta(seconds=get_settings().job_stale_after_seconds)

# These adapters consolidate upstream evidence. Reusing one after one of its
# inputs was refreshed would create a mixed-date answer with stale conclusions.
_REFRESH_DEPENDENCIES: dict[str, frozenset[str]] = {
    # Drainage consumes the parcel polygon (mask + run-on targeting); a
    # refreshed parcel invalidates the cached drainage screen.
    "drainage_screen": frozenset({"wcad_parcel", "tcad_parcel"}),
    "txdot_extended": frozenset({
        "txdot_aadt",
        "txdot_aadt_history",
        "txdot_county_vmt",
        "txdot_projects",
        "txdot_roadway",
        "txdot_bridges",
    }),
    # City zoning is interpreted only after the authoritative jurisdiction
    # verdict. Reusing zoning after jurisdiction refetches can combine a new
    # city-limit/ETJ answer with an old district conclusion.
    "austin_zoning": frozenset({"jurisdiction_resolver"}),
    "zoning_standards": frozenset({"austin_zoning"}),
    "future_land_use": frozenset({"austin_zoning"}),
    "district_overlays": frozenset({"austin_zoning"}),
    "entitlement_pipeline": frozenset({"austin_zoning"}),
    "tax_intel": frozenset({"district_overlays"}),
    "site_constraints": frozenset({
        # Access facts (Task B): refreshed roadway/jurisdiction/projects
        # results invalidate the reused site_constraints packet.
        "txdot_roadway",
        "txdot_projects",
        "jurisdiction_resolver",
        "austin_zoning",
        "district_overlays",
        "electric_power_infrastructure",
        "environmental_screening",
        "fema_nfhl",
        "hydro_intel",
        "osm_overpass",
        "plats_intel",
        "soil_intel",
        "tcad_parcel",
        "twdb_ccn",
        "txdot_extended",
        "wcad_parcel",
        "wetlands_nwi",
        "zoning_standards",
    }),
}

_SOURCE_LABELS = {
    "arcgis_geocode": "Address lookup",
    "fema_nfhl": "FEMA flood map",
    "txdot_aadt": "TxDOT traffic counts",
    "txdot_aadt_history": "Historical TxDOT traffic counts",
    "txdot_county_vmt": "TxDOT county VMT",
    "txdot_projects": "TxDOT projects",
    "txdot_roadway": "TxDOT roadway inventory",
    "txdot_bridges": "TxDOT bridges",
    "census_acs": "Census demographics",
    "osm_overpass": "Nearby places",
    "austin_zoning": "City zoning",
    "tcad_parcel": "County parcel record",
    "wcad_parcel": "County parcel record",
    "wetlands_nwi": "Wetlands screening",
    "twdb_ccn": "Water and wastewater CCN",
    "electric_power_infrastructure": "Electric transmission and substations",
    "district_overlays": "District overlays",
    "future_land_use": "Future land use",
    "environmental_screening": "Environmental screening",
    "entitlement_pipeline": "Entitlement pipeline",
    "zoning_standards": "Zoning standards",
    "fire_district": "Fire district",
    "soil_intel": "Soils",
    "hydro_intel": "Nearby waterways",
    "historic_intel": "Historic resources",
    "schools_intel": "Schools",
    "tax_intel": "Tax and debt",
    "transit_intel": "Transit",
    "broadband_intel": "Broadband",
    "plats_intel": "Plats",
    "public_aerial_imagery": "Public aerial imagery",
    "usgs_elevation": "USGS parcel elevation",
    "txdot_extended": "Extended TxDOT data",
    "arcgis_places": "ArcGIS places",
    "eia_state_fuel_sales": "EIA state fuel use",
}

_NON_RETRYABLE_COVERAGE = {
    "manual_verification_required",
    "not_supported_in_jurisdiction",
    "not_yet_wired",
}

_NON_RETRYABLE_ACCESS = {
    "HTML_OR_PDF_ONLY",
    "FORBIDDEN_BY_SOURCE",
    "JURISDICTION_NOT_COVERED",
    "NO_USABLE_PUBLIC_SOURCE_FOUND",
}

_TRANSIENT_ERROR_TOKENS = {
    "429",
    "500",
    "502",
    "503",
    "504",
    "bad gateway",
    "connection",
    "gateway",
    "provider down",
    "rate limit",
    "server error",
    "temporarily unavailable",
    "temporarily down",
    "temporary",
    "timed out",
    "timeout",
    "transient",
    "unavailable",
    "upstream",
}

def _rate_limit_retry_cooldown_seconds() -> int:
    """Provider 429 retry cooldown for the display fallback — same
    env-overridable setting the adapters use (default 5m). Must NOT be a
    hardcoded constant: a stale 30-min value here re-created a 30-minute
    'Refresh is paused until …' window after the real 5-min cooldown expired.
    """
    return get_settings().public_intel_rate_limit_cooldown_seconds

_HTTP_STATUS_RE = {
    code: re.compile(rf"(?<!\d){code}(?!\d)")
    for code in ("429", "500", "502", "503", "504")
}

_NON_RETRYABLE_ERROR_TOKENS = {
    "api key",
    "api_key",
    "env var",
    "forbidden",
    "lat/lng required",
    "login",
    "manual",
    "not configured",
    "not set",
    "not wired",
    "paid",
    "password",
    "unsupported",
}

def _source_label(source_id: str) -> str:
    return _SOURCE_LABELS.get(
        source_id,
        source_id.replace("_", " ").title(),
    )


def _normalize_county(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    cleaned = cleaned.removesuffix(" county")
    return cleaned


def normalize_address(value: Any) -> str:
    """THE address normalization (spec §7.5/§8.1): trim, casefold,
    whitespace-collapse. Used everywhere a subject key is built —
    enqueue, supersede, reuse seed, history search, and the partial
    unique index all compare exactly this value."""
    return " ".join(str(value or "").strip().casefold().split())


# Same normalization applied to other free-text comparisons (county,
# parcel id) — one function, one behavior.
_normalize_refresh_text = normalize_address


def _same_refresh_subject(
    run: SiteCheckRun,
    *,
    address: str,
    county: str,
    parcel_id: str | None,
) -> bool:
    """A cache entry is reusable only for the same stated property.

    A changed address or parcel id is a new subject and must never inherit the
    previous property's public facts. County is compared when both sides have
    a value because older runs sometimes predate county resolution.
    """
    if _normalize_refresh_text(run.address) != _normalize_refresh_text(address):
        return False
    old_parcel = _normalize_refresh_text(run.parcel_id)
    new_parcel = _normalize_refresh_text(parcel_id)
    if old_parcel and new_parcel and old_parcel != new_parcel:
        return False
    old_county = _normalize_county(run.county)
    new_county = _normalize_county(county)
    return not (old_county and new_county and old_county != new_county)


def _reusable_source_result(
    result: Any,
    *,
    now: datetime,
) -> bool:
    if not isinstance(result, dict):
        return False
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return False
    if isinstance(errors, str) and errors.strip():
        return False
    facts = result.get("normalized_facts")
    if not isinstance(facts, dict) or not facts:
        return False
    source_id = str(result.get("source_id") or "")
    try:
        current_version = registry.result_schema_version(source_id)
    except (KeyError, ValueError):
        return False
    stored_version = result.get("result_schema_version")
    if stored_version is None:
        # These sources stamped the version inside their facts before schema
        # metadata became a top-level AdapterResult contract.
        stored_version = facts.get("result_schema_version", 0)
    try:
        if int(stored_version) != current_version:
            return False
    except (TypeError, ValueError):
        return False
    fetched_at = _parse_utc_datetime(result.get("fetched_at"))
    if fetched_at is None:
        return False
    age = now - fetched_at
    return timedelta(0) <= age < _reuse_max_age()


def _invalidate_reused_dependents(
    reusable: dict[str, dict],
    *,
    county: str,
) -> dict[str, dict]:
    """Drop cached conclusions whose required input will be refreshed."""
    active_sources = {
        source_id
        for source_id in registry.all_source_ids()
        if registry.get(source_id).active_in_v0
    }
    missing = active_sources - set(reusable)

    # All spatial facts depend on a stable anchor. If geocoding itself is not
    # reusable, the provider must resolve a fresh anchor and every source must
    # be refreshed against it.
    if "arcgis_geocode" in missing:
        return {}

    # Parcel-aware adapters depend on the county's authoritative parcel path.
    parcel_source = "wcad_parcel" if _normalize_county(county) == "williamson" else "tcad_parcel"
    if parcel_source in missing:
        for source_id in (
            "austin_zoning",
            "fema_nfhl",
            "site_constraints",
            "usgs_elevation",
        ):
            reusable.pop(source_id, None)

    # Apply dependency invalidation transitively: removing one cached
    # consolidator may require the next wave's cached result to be rebuilt.
    changed = True
    while changed:
        changed = False
        missing = active_sources - set(reusable)
        for source_id, dependencies in _REFRESH_DEPENDENCIES.items():
            if source_id in reusable and dependencies & missing:
                reusable.pop(source_id, None)
                changed = True
    return reusable


def _empty_reuse_seed() -> dict[str, Any]:
    return {
        "adapter_results": {},
        "sources_queried": [],
        "derived": {},
        "cached_lat": None,
        "cached_lng": None,
        "cached_geocode_source": None,
    }


def build_refresh_reuse_seed(
    session: Session,
    *,
    tenant_id: str,
    address: str,
    county: str,
    parcel_id: str | None,
    now: datetime | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build the §9.2 freshness-window per-source reuse seed from run
    history.

    We scan the TENANT'S recent completed snapshots, newest first,
    because a transient failure in the latest run must not hide a
    still-current successful result from an earlier run. Results remain
    tenant/subject scoped — reuse never crosses tenants even though the
    facts are public. `force_refresh` (mode="fresh", §9.1) returns an
    empty seed so every source is re-fetched.
    """
    if force_refresh:
        return _empty_reuse_seed()
    now = now or datetime.now(UTC)
    candidates: dict[str, tuple[dict, SiteCheckRun]] = {}
    prior_runs = session.scalars(
        select(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == tenant_id)
        .where(SiteCheckRun.status == "completed")
        .order_by(desc(SiteCheckRun.created_at))
    ).all()
    matching_runs = [
        run
        for run in prior_runs
        if _same_refresh_subject(
            run,
            address=address,
            county=county,
            parcel_id=parcel_id,
        )
    ]
    anchor_run: SiteCheckRun | None = None
    anchor_lat: float | None = None
    anchor_lng: float | None = None
    for prior in matching_runs:
        geocode_result = (prior.adapter_results or {}).get("arcgis_geocode")
        if not _reusable_source_result(geocode_result, now=now):
            continue
        geocode_facts = _as_dict(geocode_result.get("normalized_facts"))
        try:
            anchor_lat = float(
                prior.lat if prior.lat is not None else geocode_facts.get("lat")
            )
            anchor_lng = float(
                prior.lng if prior.lng is not None else geocode_facts.get("lng")
            )
        except (TypeError, ValueError):
            continue
        anchor_run = prior
        break

    def same_anchor(prior: SiteCheckRun) -> bool:
        if anchor_lat is None or anchor_lng is None:
            return False
        try:
            return (
                abs(float(prior.lat) - anchor_lat) <= 1e-4
                and abs(float(prior.lng) - anchor_lng) <= 1e-4
            )
        except (TypeError, ValueError):
            return prior.id == (anchor_run.id if anchor_run else None)

    for prior in matching_runs:
        # A prior successful source at a materially different geocode is stale
        # for this subject, even if its timestamp is recent. This prevents a
        # corrected property anchor from resurrecting the old location's facts.
        if not same_anchor(prior):
            continue
        for source_id, result in (prior.adapter_results or {}).items():
            if source_id in candidates:
                continue
            if _reusable_source_result(result, now=now):
                candidates[source_id] = (deepcopy(result), prior)

    effective_county = county or (anchor_run.county if anchor_run else "")
    reusable = _invalidate_reused_dependents(
        {source_id: pair[0] for source_id, pair in candidates.items()},
        county=effective_county,
    )
    queried: list[dict[str, Any]] = []
    for source_id, result in reusable.items():
        prior = candidates[source_id][1]
        result["result_schema_version"] = registry.result_schema_version(source_id)
        result["refresh_status"] = "reused_recent"
        result["reused_from_run_id"] = prior.id
        queried.append({
            "source": source_id,
            "status": "reused_recent",
            "reused_from_run_id": prior.id,
            "original_fetched_at": result.get("fetched_at"),
        })

    geocode_reused = "arcgis_geocode" in reusable
    # Zoning Navigator packet seeding SEVERED for Phase A
    # (site_check.md §3.2 — Phase C surface).
    derived_seed: dict[str, Any] = {}
    return {
        "adapter_results": reusable,
        "sources_queried": queried,
        "derived": derived_seed,
        "cached_lat": anchor_run.lat if anchor_run and geocode_reused else None,
        "cached_lng": anchor_run.lng if anchor_run and geocode_reused else None,
        "cached_geocode_source": (
            anchor_run.geocode_source if anchor_run and geocode_reused else None
        ),
    }


def _successful_wcad_parcel_result(run: SiteCheckRun) -> bool:
    result = (run.adapter_results or {}).get("wcad_parcel") or {}
    if not isinstance(result, dict):
        return False
    if str(result.get("coverage_status") or "").strip() != "covered":
        return False
    access = str(result.get("source_access_status") or "").strip()
    if access != "PROGRAMMATIC_PUBLIC":
        return False
    facts = result.get("normalized_facts") or {}
    if not isinstance(facts, dict):
        return False
    county = _normalize_county(facts.get("county") or run.county)
    if county != "williamson":
        return False
    return bool(facts.get("parcel_polygon") or facts.get("parcel_id"))


def _superseded_internal_failure(
    run: SiteCheckRun,
    source_id: str,
) -> bool:
    # Williamson's authoritative parcel path is WCAD. If WCAD verified the
    # parcel, a transient tcad/statewide fallback failure is internal noise and
    # must not show as a broker-facing "County parcel record" retry row.
    return source_id == "tcad_parcel" and _successful_wcad_parcel_result(run)


def _as_error_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _failed_entry_for_source(
    run: SiteCheckRun,
    source_id: str,
) -> dict[str, Any] | None:
    for entry in reversed(run.sources_failed or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("source") == source_id:
            return dict(entry)
    return None


def _has_http_status(error_text: str, code: str) -> bool:
    return bool(_HTTP_STATUS_RE[code].search(error_text))


def _has_rate_limit_signal(error_text: str) -> bool:
    lowered = error_text.lower()
    return (
        _has_http_status(error_text, "429")
        or "rate limit" in lowered
        or "rate-limit" in lowered
        or "rate-limited" in lowered
    )


def _has_server_error_signal(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(
        _has_http_status(error_text, code)
        for code in ("500", "502", "503", "504")
    ) or "bad gateway" in lowered or "gateway" in lowered


def _has_transient_signal(error_text: str) -> bool:
    lowered = error_text.lower()
    if _has_rate_limit_signal(error_text) or _has_server_error_signal(error_text):
        return True
    return any(
        token in lowered
        for token in _TRANSIENT_ERROR_TOKENS
        if not token.isdigit()
    )


def _broker_cause(error_text: str) -> str:
    lowered = error_text.lower()
    if _has_rate_limit_signal(error_text):
        return (
            "We tried this source, but the public site rate-limited "
            "the request. Refresh just this source later."
        )
    if (
        "timeout" in lowered
        or "timed out" in lowered
        or _has_http_status(error_text, "504")
    ):
        return (
            "We tried this source, but the public site timed out. "
            "Refresh just this source when the site is responding again."
        )
    if _has_server_error_signal(error_text):
        return (
            "We tried this source, but the public site returned a "
            "temporary server error. Refresh just this source later."
        )
    return (
        "We tried this source, but the public site was temporarily "
        "unavailable. Refresh just this source later."
    )


def _broker_error_detail(source_id: str, error_text: str) -> str:
    """Return broker-safe retry detail without provider URLs or exceptions."""
    source_label = _source_label(source_id)
    lowered = error_text.lower()
    if _has_rate_limit_signal(error_text):
        return (
            f"{source_label} did not finish because the public data provider "
            "temporarily limited automated lookups. Refresh this source after "
            "the cooldown window."
        )
    if (
        "timeout" in lowered
        or "timed out" in lowered
        or _has_http_status(error_text, "504")
    ):
        return (
            f"{source_label} did not finish because the public data provider "
            "took too long to respond. Refresh this source when the public "
            "site is responding normally."
        )
    if _has_server_error_signal(error_text):
        return (
            f"{source_label} did not finish because the public data provider "
            "returned a temporary server error. No project data was changed; "
            "refresh this source later."
        )
    return (
        f"{source_label} did not finish because the public data provider was "
        "temporarily unavailable. Refresh this source later."
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _coerce_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _coerce_utc(parsed)


def _format_retry_time(value: datetime) -> str:
    return value.isoformat()


def _environmental_broker_cause(
    result: dict[str, Any],
    error_text: str,
) -> str | None:
    facts = _as_dict(result.get("normalized_facts"))
    statuses = _as_dict(facts.get("source_statuses"))
    pst_status = str(statuses.get("tceq_pst") or "").strip()
    echo_status = str(statuses.get("epa_echo") or "").strip()
    lowered = error_text.lower()
    if (
        (echo_status == "temporary_rate_limited" or "epa echo" in lowered)
        and ("429" in lowered or "rate limit" in lowered)
        and pst_status == "queried"
    ):
        pst_records = facts.get("pst_records")
        pst_count = len(pst_records) if isinstance(pst_records, list) else 0
        # Plain English (§2/§14): no agency acronyms in a user-bound cause.
        pst_read = (
            "The state's fuel-tank registry showed nothing in the area "
            "we screened"
            if pst_count == 0
            else (
                "The state's fuel-tank registry showed "
                f"{pst_count} record{'s' if pst_count != 1 else ''} nearby"
            )
        )
        return (
            f"{pst_read}. The federal environmental register asked us "
            "to slow down, so that half of the environmental screen is "
            "still open. Refresh this source again in a few minutes."
        )
    return None


def _retry_cooldown_for_result(
    result: dict[str, Any],
    *,
    error_text: str,
    last_failed_at: Any,
) -> dict[str, Any]:
    facts = _as_dict(result.get("normalized_facts"))
    source_cooldowns = _as_dict(facts.get("source_cooldowns"))
    now = datetime.now(UTC)
    disabled_until: datetime | None = None
    message: str | None = None

    for item in source_cooldowns.values():
        cooldown = _as_dict(item)
        candidate = _parse_utc_datetime(
            cooldown.get("retry_disabled_until")
            or cooldown.get("retry_after_at")
        )
        if candidate is None or candidate <= now:
            continue
        if disabled_until is None or candidate > disabled_until:
            disabled_until = candidate
            message = str(cooldown.get("message") or "").strip() or None

    if disabled_until is None and (
        _has_rate_limit_signal(error_text)
    ):
        failed_at = _parse_utc_datetime(last_failed_at)
        if failed_at is not None:
            candidate = failed_at + timedelta(
                seconds=_rate_limit_retry_cooldown_seconds(),
            )
            if candidate > now:
                disabled_until = candidate

    if disabled_until is None:
        return {}

    remaining_seconds = max(0, int((disabled_until - now).total_seconds()))
    retry_time = _format_retry_time(disabled_until)
    return {
        "retry_status": "cooldown",
        "retry_disabled_until": retry_time,
        "retry_after_seconds": remaining_seconds,
        "retry_message": message or (
            "This source is cooling down after a provider rate limit. "
            f"Try again after {retry_time}."
        ),
    }


def _retryable_failure_for_source(
    run: SiteCheckRun,
    source_id: str,
) -> dict[str, Any] | None:
    if source_id == "arcgis_geocode":
        return None
    if _superseded_internal_failure(run, source_id):
        return None
    reg = registry.REGISTRY.get(source_id)
    if reg is None or not reg.active_in_v0:
        return None

    result = (run.adapter_results or {}).get(source_id) or {}
    if not isinstance(result, dict):
        result = {}
    coverage = str(result.get("coverage_status") or "").strip()
    if coverage in _NON_RETRYABLE_COVERAGE:
        return None
    access = str(result.get("source_access_status") or "").strip()
    if access in _NON_RETRYABLE_ACCESS:
        return None

    failed_entry = _failed_entry_for_source(run, source_id)
    errors = _as_error_list(result.get("errors"))
    if failed_entry:
        errors.extend(_as_error_list(failed_entry.get("error")))
    error_text = "; ".join(dict.fromkeys(errors)).strip()
    if not error_text:
        return None

    lowered = error_text.lower()
    if any(token in lowered for token in _NON_RETRYABLE_ERROR_TOKENS):
        return None
    if (
        access != "PROGRAMMATIC_PUBLIC_BUT_FRAGILE"
        and not _has_transient_signal(error_text)
    ):
        return None

    last_failed_at = None
    if failed_entry:
        last_failed_at = (
            failed_entry.get("retried_at")
            or failed_entry.get("failed_at")
        )
    last_failed_at = last_failed_at or result.get("fetched_at")
    cause = (
        _environmental_broker_cause(result, error_text)
        if source_id == "environmental_screening"
        else None
    )
    row = {
        "source_id": source_id,
        "source_label": _source_label(source_id),
        "cause": cause or _broker_cause(error_text),
        "error_detail": _broker_error_detail(source_id, error_text),
        "last_failed_at": last_failed_at,
    }
    row.update(
        _retry_cooldown_for_result(
            result,
            error_text=error_text,
            last_failed_at=last_failed_at,
        ),
    )
    return row


def retryable_failed_sources_for_run(
    run: SiteCheckRun,
) -> list[dict[str, Any]]:
    """Return centralized failed-source retry rows for the UI.

    This is deliberately stricter than "any adapter has errors": manual-only,
    not-wired, unsupported, missing-key, and dependency states are not retryable
    transient public-site failures.
    """
    source_ids: list[str] = []
    for entry in run.sources_failed or []:
        if isinstance(entry, dict) and isinstance(entry.get("source"), str):
            source_ids.append(str(entry["source"]))
    for source_id, result in (run.adapter_results or {}).items():
        if not isinstance(result, dict):
            continue
        source_id_str = str(source_id)
        if result.get("errors"):
            source_ids.append(source_id_str)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        row = _retryable_failure_for_source(run, source_id)
        if row is not None:
            out.append(row)
    return out


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _job_for_run(session: Session, run: SiteCheckRun) -> Job | None:
    """Find the queue row that drives this SiteCheckRun.

    The run id lives in Job.payload (§7.1). Keep the SQL predicate
    broad and filter JSON in Python so the helper avoids
    dialect-specific JSON operators.
    """
    jobs = session.scalars(
        select(Job)
        .where(Job.kind == "site_check")
        .where(Job.tenant_id == run.tenant_id)
        .order_by(desc(Job.created_at))
    ).all()
    for job in jobs:
        if (job.payload or {}).get("run_id") == run.id:
            return job
    return None


def _fail_active_run(
    session: Session,
    run: SiteCheckRun,
    *,
    error_code: str,
    failure_reason: str,
) -> SiteCheckRun:
    """Read-path terminalization. Same class as the reviewer's round-2
    stale-recovery finding (2026-08-01), swept proactively: this path also
    used to flip an active run to `failed` with NO run lock and no
    cancellation check, so it could overwrite an accepted cancellation.
    It now takes the run lock (the shared run→job order), re-validates under
    it, and honors an accepted cancellation as `cancelled`."""
    locked = session.execute(
        select(SiteCheckRun)
        .where(SiteCheckRun.id == run.id)
        .with_for_update()
        # populate_existing is REQUIRED, not decorative (reviewer round 3,
        # 2026-08-01): this session already owns the SiteCheckRun instance
        # passed in, so without it SQLAlchemy hands back the identity-mapped
        # object with its PRE-LOCK attributes — the lock is taken but the
        # decision is made on stale values, and a cancellation committed by a
        # concurrent request is overwritten with worker_lost.
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if locked is None:
        return run
    if locked.status not in _ACTIVE_STATUSES:
        session.commit()          # someone else terminalized first
        session.refresh(locked)
        return locked
    if locked.cancel_requested_at is not None:
        locked.status = "cancelled"
        locked.error_code = locked.error_code or "cancelled_by_user"
        locked.failure_reason = "This lookup was cancelled."
        locked.finished_at = datetime.now(UTC)
        session.add(locked)
        session.commit()
        session.refresh(locked)
        log.info(
            "site_check reconcile: honored cancellation instead of %s "
            "run_id=%s", error_code, locked.id,
        )
        return locked
    run = locked
    run.status = "failed"
    run.error_code = error_code
    run.failure_reason = failure_reason
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()
    session.refresh(run)
    log.warning(
        "site_check reconcile: marked run failed run_id=%s error_code=%s",
        run.id, error_code,
    )
    return run


def _cancel_active_run(
    session: Session,
    run: SiteCheckRun,
    *,
    error_code: str = "cancelled_by_user",
    failure_reason: str = "Public data fetch was cancelled.",
) -> SiteCheckRun:
    if run.status not in _ACTIVE_STATUSES:
        return run
    run.status = "cancelled"
    run.error_code = error_code
    run.failure_reason = failure_reason
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()
    session.refresh(run)
    log.info(
        "site_check reconcile: marked run cancelled run_id=%s error_code=%s",
        run.id, error_code,
    )
    return run


def _clear_success_failure_metadata(
    session: Session,
    run: SiteCheckRun,
) -> SiteCheckRun:
    """A complete Public Data run is not a run-level failure.

    Per-source issues remain represented by `sources_failed` and
    `adapter_results[*].errors`, which drive the retry dropdown. The run-level
    `error_code`/`failure_reason` are only for terminal run failures. Clearing
    them here repairs old rows that were completed after a transient
    reconciliation race.
    """
    if run.status != "completed":
        return run
    if not (run.error_code or run.failure_reason):
        return run
    run.error_code = None
    run.failure_reason = None
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _reconcile_active_run(
    session: Session, run: SiteCheckRun,
) -> SiteCheckRun:
    """Read-path reconciliation (site_check.md §7.6).

    The UI polls while a run is queued/running. If the worker is
    stopped or dies after claiming a job, the jobs row can become
    terminal/stale while the SiteCheckRun row remains active.
    Reconcile that state on the read path so polling always terminates
    in a truthful state even if the sweeper has not run — and never
    produces a different outcome than the sweeper would.
    """
    run = _clear_success_failure_metadata(session, run)
    if run.status not in _ACTIVE_STATUSES:
        return run

    job = _job_for_run(session, run)
    if job is None:
        return _fail_active_run(
            session, run,
            error_code="worker_lost",
            failure_reason=(
                "The lookup stopped unexpectedly. Run it again."
            ),
        )

    if job.status == "cancelled":
        return _cancel_active_run(
            session,
            run,
            failure_reason="This lookup was cancelled.",
        )

    if job.status == "failed":
        return _fail_active_run(
            session, run,
            error_code=str(job.error_code or "worker_lost"),
            failure_reason=(
                job.error_message
                or "The lookup stopped unexpectedly. Run it again."
            ),
        )

    if job.status == "succeeded":
        return _fail_active_run(
            session, run,
            error_code="inconsistent_worker_state",
            failure_reason=(
                "The lookup finished in an unexpected state. "
                "Run it again."
            ),
        )

    heartbeat_at = _coerce_utc(job.heartbeat_at)
    if job.status == "running" and heartbeat_at is not None:
        age = datetime.now(UTC) - heartbeat_at
        if age > _job_stale_after():
            # §7.6: the read path invokes the SAME §7.4 stale-recovery
            # primitive as the sweeper (requeue below the attempt cap,
            # fail at the cap, lease invalidated either way) — it never
            # short-circuits to `failed` on its own.
            recover_stale_job(session, job.id)
            session.refresh(run)
            return run

    return run


def reconcile_active_runs_for_subject(
    session: Session,
    *,
    tenant_id: str,
    address: str,
    parcel_id: str | None = None,
) -> list[str]:
    """Reconcile active runs for one subject before supersede/gating."""
    changed: list[str] = []
    active_runs = session.scalars(
        select(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == tenant_id)
        .where(
            SiteCheckRun.normalized_address
            == _normalize_refresh_text(address)
        )
        .where(SiteCheckRun.status.in_(_ACTIVE_STATUSES))  # type: ignore[union-attr]
        .order_by(desc(SiteCheckRun.created_at))
    ).all()
    for run in active_runs:
        before = run.status
        reconciled = _reconcile_active_run(session, run)
        if reconciled.status != before:
            changed.append(reconciled.id)
    return changed


def cancel_active_runs_for_subject(
    session: Session,
    *,
    tenant_id: str,
    address: str,
    parcel_id: str | None = None,
) -> list[str]:
    """Terminalize active prior runs for the same subject before
    enqueuing their replacement (site_check.md §7.5 enqueue-supersede).

    Marks run + job cancelled with error_code `superseded_by_new_check`;
    already-staged results on the old run are retained and stay
    readable. The §7.5 advisory-lock + partial-unique-index guarantees
    are enforced by the API-layer enqueue transaction on top of this.
    """
    cancelled: list[str] = []
    now = datetime.now(UTC)
    runs = session.scalars(
        select(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == tenant_id)
        .where(
            SiteCheckRun.normalized_address
            == _normalize_refresh_text(address)
        )
        .where(SiteCheckRun.status.in_(_ACTIVE_STATUSES))  # type: ignore[union-attr]
    ).all()
    for run in runs:
        if parcel_id is not None and run.parcel_id not in (None, "", parcel_id):
            continue
        run.status = "cancelled"
        run.error_code = "superseded_by_new_check"
        run.failure_reason = "Superseded by a newer lookup."
        run.finished_at = now
        session.add(run)
        job = _job_for_run(session, run)
        if job is not None and job.status in _ACTIVE_STATUSES:
            job.status = "cancelled"
            job.finished_at = now
            job.error_code = "superseded_by_new_check"
            job.error_message = "Superseded by a newer lookup."
            # Invalidate the lease so the old worker's current-wave
            # writes match 0 rows (§7.5 step 2).
            job.lease_token = None
            session.add(job)
        cancelled.append(run.id)
    if cancelled:
        session.commit()
    return cancelled


def _insert_run_and_job(
    session: Session,
    *,
    tenant_id: str,
    requested_by: str,
    address: str,
    county: str = "",
    parcel_id: str | None = None,
    mode: str = "check",
    cached_lat: float | None = None,
    cached_lng: float | None = None,
    cached_geocode_source: str | None = None,
    adapter_results: dict[str, Any] | None = None,
    sources_queried: list[dict[str, Any]] | None = None,
    derived: dict[str, Any] | None = None,
) -> tuple[SiteCheckRun, Job]:
    """FLUSH-ONLY insert of a queued SiteCheckRun + its jobs row. Rides
    the CALLER's open transaction (§7.3 atomic enqueue: run + job +
    audit commit together or not at all).

    Cached coordinates and seeded source results are accepted from the
    §9.2 reuse-seed builder (mode="check"); mode="fresh" passes an empty
    seed. The seeded source results retain their original fetched_at
    values so a new run cannot extend their freshness. No network call
    happens here (§8.1) — geocoding runs in the worker.
    """
    run = SiteCheckRun(
        tenant_id=tenant_id,
        requested_by=requested_by,
        address=address,
        normalized_address=normalize_address(address),
        county=county,
        parcel_id=parcel_id,
        lat=cached_lat,
        lng=cached_lng,
        geocode_source=cached_geocode_source,
        adapter_results=deepcopy(adapter_results or {}),
        sources_queried=deepcopy(sources_queried or []),
        derived=deepcopy(derived or {}),
        mode=mode,
        status="queued",
    )
    session.add(run)
    session.flush()

    job = enqueue_job(
        session,
        tenant_id=tenant_id,
        kind="site_check",
        # The worker handler reads job.payload to find the run_id it
        # needs to drive (§7.1).
        payload={
            "run_id": run.id,
            "mode": mode,
        },
        progress={
            "stage": "queued",
            "status": "pending",
            "message": "The lookup is queued.",
            "address": address,
            "county": county or None,
            "parcel_id": parcel_id,
            "mode": mode,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    return run, job


def enqueue_run(
    session: Session,
    **kwargs: Any,
) -> tuple[SiteCheckRun, Job]:
    """Direct enqueue (dev/smoke paths). The §8.1 API path is
    `create_run`, which layers the §7.5 supersede transaction on top."""
    run, job = _insert_run_and_job(session, **kwargs)
    session.commit()
    session.refresh(run)
    session.refresh(job)
    log.info(
        "site_check run enqueued run_id=%s job_id=%s tenant_id=%s",
        run.id, job.id, run.tenant_id,
    )
    return run, job


class ActiveRunConflict(Exception):
    """Raised when the §7.5 unique-index backstop still refuses the
    insert after one retry — the API answers with a plain 409."""


def create_run(
    session: Session,
    *,
    tenant_id: str,
    requested_by: str,
    address: str,
    parcel_id: str | None = None,
    mode: str = "check",
) -> tuple[SiteCheckRun, Job, list[str]]:
    """The §7.5/§8.1 enqueue-supersede transaction. In ONE transaction:

      1. serialize on the subject via
         pg_advisory_xact_lock(hashtext(tenant|normalized|parcel));
      2. terminalize any active same-subject run + job → cancelled
         `superseded_by_new_check`, lease invalidated (staged results
         retained), with its `site_check.run_cancelled` audit row under
         the requesting user;
      3. build the reuse seed (mode="check") or an empty one
         (mode="fresh", force_refresh);
      4. insert run + job + `site_check.run_created` audit.

    A partial-unique-index violation retries the WHOLE transaction once,
    then raises ActiveRunConflict. Returns (run, job, superseded_ids).
    """
    normalized = normalize_address(address)
    subject_key = f"{tenant_id}|{normalized}|{parcel_id or ''}"
    last_error: IntegrityError | None = None
    for _attempt in range(2):
        try:
            session.rollback()  # a clean transaction for the advisory lock
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(subject_key)))
            )

            # 2. Supersede any active same-subject run (exact §7.5 subject:
            # tenant, normalized_address, coalesce(parcel_id, '')).
            superseded: list[str] = []
            active_runs = session.scalars(
                select(SiteCheckRun)
                .where(SiteCheckRun.tenant_id == tenant_id)
                .where(SiteCheckRun.normalized_address == normalized)
                .where(
                    func.coalesce(SiteCheckRun.parcel_id, "")
                    == (parcel_id or "")
                )
                .where(SiteCheckRun.status.in_(_ACTIVE_STATUSES))
                .with_for_update()
                # Class sweep (reviewer round 3): every entity-level lock read
                # must return CURRENT row values, never identity-mapped ones.
                .execution_options(populate_existing=True)
            ).all()
            now = datetime.now(UTC)
            for old in active_runs:
                old.status = "cancelled"
                old.error_code = "superseded_by_new_check"
                old.failure_reason = "A newer lookup for this address replaced this one."
                old.finished_at = now
                session.add(old)
                old_job = _job_for_run(session, old)
                if old_job is not None and old_job.status in _ACTIVE_STATUSES:
                    old_job.status = "cancelled"
                    old_job.error_code = "superseded_by_new_check"
                    old_job.error_message = (
                        "A newer lookup for this address replaced this one."
                    )
                    old_job.finished_at = now
                    # Invalidate the lease so the old worker's current-wave
                    # writes match 0 rows (§7.5 step 2).
                    old_job.lease_token = None
                    old_job.claimed_by = None
                    session.add(old_job)
                audit.log(
                    session,
                    tenant_id=tenant_id,
                    user_id=requested_by,
                    action="site_check.run_cancelled",
                    entity_type="site_check_run",
                    entity_id=old.id,
                    detail={
                        "reason": "superseded_by_new_check",
                        "address": old.address,
                    },
                )
                superseded.append(old.id)
            if superseded:
                # Flush the supersede UPDATEs BEFORE the new insert so the
                # partial unique index never sees two active subject rows
                # inside this transaction.
                session.flush()

            # 3. Reuse seed: §9.2 for mode="check"; empty for mode="fresh".
            seed = build_refresh_reuse_seed(
                session,
                tenant_id=tenant_id,
                address=address,
                county="",
                parcel_id=parcel_id,
                force_refresh=(mode == "fresh"),
            )

            # 4. Insert run + job + audit; commit everything together.
            run, job = _insert_run_and_job(
                session,
                tenant_id=tenant_id,
                requested_by=requested_by,
                address=address,
                parcel_id=parcel_id,
                mode=mode,
                cached_lat=seed["cached_lat"],
                cached_lng=seed["cached_lng"],
                cached_geocode_source=seed["cached_geocode_source"],
                adapter_results=seed["adapter_results"],
                sources_queried=seed["sources_queried"],
                derived=seed["derived"],
            )
            audit.log(
                session,
                tenant_id=tenant_id,
                user_id=requested_by,
                action="site_check.run_created",
                entity_type="site_check_run",
                entity_id=run.id,
                detail={
                    "address": address,
                    "parcel_id": parcel_id,
                    "mode": mode,
                    "superseded_run_ids": superseded,
                },
            )
            session.commit()
            session.refresh(run)
            session.refresh(job)
            log.info(
                "site_check run created run_id=%s job_id=%s tenant_id=%s "
                "mode=%s superseded=%d",
                run.id, job.id, tenant_id, mode, len(superseded),
            )
            return run, job, superseded
        except IntegrityError as exc:
            session.rollback()
            last_error = exc
            log.info(
                "site_check create raced the active-subject unique index "
                "(attempt %d) — %s", _attempt + 1, subject_key,
            )
    raise ActiveRunConflict(
        "A lookup for this address is already running."
    ) from last_error


def _active_retry_job_for_run(
    session: Session,
    *,
    run: SiteCheckRun,
) -> Job | None:
    """The active retry job for a run, if any (§8.4: at most ONE)."""
    jobs = session.scalars(
        select(Job)
        .where(Job.kind == "site_check")
        .where(Job.tenant_id == run.tenant_id)
        .where(Job.status.in_(["queued", "running"]))  # type: ignore[union-attr]
        .order_by(desc(Job.created_at))
    ).all()
    for job in jobs:
        payload = job.payload or {}
        if (
            payload.get("run_id") == run.id
            and payload.get("retry_source_ids")
        ):
            return job
    return None


class RetryRefused(Exception):
    """§8.4 refusal — the API answers 409 with this plain message."""


def enqueue_source_retry(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    requested_by: str,
    source_ids: list[str] | None = None,
) -> tuple[SiteCheckRun, Job, bool]:
    """Enqueue a source-scoped retry on a completed run (§8.4).

    Unlike a full lookup, this preserves unrelated adapter results and
    uses the existing run id. The worker branches on `retry_source_ids`
    in the job payload and calls the source-scoped orchestrator path.
    Omitted/empty source_ids = all currently retryable sources.

    Returns (run, job, created). created=False means an ACTIVE retry job
    for the same sources already exists (idempotent 202, no new audit
    row). A request for DIFFERENT sources while one runs raises
    RetryRefused (§8.4 serialization: at most ONE active retry job).

    Admission is serialized per run: `pg_advisory_xact_lock` on the run id
    so two concurrent admissions cannot interleave their check-then-insert,
    with the `uq_jobs_active_retry_per_run` partial unique index as the
    database-backed backstop (mirrors the §7.5 create/supersede pattern).
    """
    run = session.get(SiteCheckRun, run_id)
    if run is None or run.tenant_id != tenant_id:
        raise LookupError("no such lookup")
    # Reconciliation may commit, which would drop a transaction-scoped
    # advisory lock — run it BEFORE taking the lock.
    run = _reconcile_active_run(session, run)
    if run.status != "completed":
        raise RetryRefused(
            "You can refresh sources only on a finished report."
        )

    session.rollback()  # a clean transaction for the advisory lock
    session.execute(
        select(func.pg_advisory_xact_lock(
            func.hashtext(f"site_check_retry|{run_id}")
        ))
    )
    # Re-read under the lock: a concurrent admission that just committed its
    # job (and any results a finishing retry staged) must be visible here.
    session.refresh(run)

    retryable = {
        item["source_id"]: item
        for item in retryable_failed_sources_for_run(run)
    }
    explicit = [s for s in (source_ids or []) if s]
    if explicit:
        # An explicitly named source must be eligible right now — a
        # cooling or non-retryable source is a plain 409.
        for source_id in explicit:
            if source_id not in retryable:
                raise RetryRefused(
                    "This source can't be refreshed automatically."
                )
            row = retryable[source_id]
            if row.get("retry_status") == "cooldown":
                raise RetryRefused(
                    str(row.get("retry_message") or "").strip()
                    or (
                        "This source is taking a required pause. "
                        "Try again in a few minutes."
                    )
                )
        wanted = explicit
    else:
        # Implicit "refresh everything": §8.4 — all CURRENTLY
        # retryable sources; cooling sources are skipped, not fatal.
        wanted = [
            s for s, row in retryable.items()
            if row.get("retry_status") != "cooldown"
        ]
        if not wanted:
            cooling = [
                str(row.get("retry_message") or "").strip()
                for row in retryable.values()
                if row.get("retry_status") == "cooldown"
            ]
            raise RetryRefused(
                (cooling[0] if cooling and cooling[0] else None)
                or "No source on this report can be refreshed right now."
            )

    active = _active_retry_job_for_run(session, run=run)
    if active is not None:
        requested = set(source_ids or [])
        active_set = set((active.payload or {}).get("retry_source_ids") or [])
        if requested and requested != active_set:
            # §8.4: a second request for DIFFERENT sources while one runs.
            raise RetryRefused(
                "A refresh is already running for this lookup."
            )
        # §8.4 idempotent: return the EXISTING job while one is active.
        return run, active, False

    first_label = retryable[wanted[0]]["source_label"]
    try:
        job = enqueue_job(
            session,
            tenant_id=tenant_id,
            kind="site_check",
            payload={
                "run_id": run.id,
                "retry_source_ids": wanted,
            },
            progress={
                "stage": "source_retry",
                "status": "pending",
                "message": f"{first_label} refresh is queued.",
                "retry_source_ids": wanted,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        # §8.7: exactly one user-attributed audit row per ACCEPTED retry,
        # in the same transaction as the job insert.
        audit.log(
            session,
            tenant_id=tenant_id,
            user_id=requested_by,
            action="site_check.run_retry",
            entity_type="site_check_run",
            entity_id=run.id,
            detail={"source_ids": wanted},
        )
        session.commit()
    except IntegrityError:
        # The DB invariant refused a second active retry job for this run —
        # the advisory lock should already have prevented it, so this is the
        # backstop. Nothing was written (job AND audit roll back together).
        session.rollback()
        existing = _active_retry_job_for_run(session, run=run)
        if existing is None:
            raise RetryRefused(
                "A refresh is already running for this lookup."
            ) from None
        active_set = set((existing.payload or {}).get("retry_source_ids") or [])
        if set(wanted) != active_set:
            raise RetryRefused(
                "A refresh is already running for this lookup."
            ) from None
        return run, existing, False
    session.refresh(job)
    return run, job, True


def progress_for_run(
    session: Session,
    run: SiteCheckRun,
) -> dict[str, Any]:
    job = _job_for_run(session, run)
    if job is None or not isinstance(job.progress, dict):
        return {}
    return dict(job.progress)


def get_run(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
) -> SiteCheckRun | None:
    """One run by id, tenant-scoped (cross-tenant ids read as missing —
    the API layer 404s). Applies the §7.6 read-path reconciliation."""
    run = session.get(SiteCheckRun, run_id)
    if run is None or run.tenant_id != tenant_id:
        return None
    return _reconcile_active_run(session, run)


def get_latest_completed_for_subject(
    session: Session,
    *,
    tenant_id: str,
    address: str,
) -> SiteCheckRun | None:
    """Latest usable snapshot for a subject, ignoring an in-progress
    lookup."""
    return session.scalars(
        select(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == tenant_id)
        .where(
            SiteCheckRun.normalized_address
            == _normalize_refresh_text(address)
        )
        .where(SiteCheckRun.status == "completed")
        .order_by(desc(SiteCheckRun.created_at))
        .limit(1)
    ).first()


def get_history(
    session: Session,
    *,
    tenant_id: str,
    query: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[SiteCheckRun]:
    """Paginated tenant history (newest first, §8.2). `query` searches
    the address text."""
    stmt = (
        select(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == tenant_id)
        .order_by(desc(SiteCheckRun.created_at))
    )
    text = _normalize_refresh_text(query)
    if text:
        stmt = stmt.where(
            SiteCheckRun.normalized_address.contains(text)  # type: ignore[union-attr]
        )
    stmt = stmt.offset(offset).limit(limit)
    return [
        _clear_success_failure_metadata(session, run)
        for run in session.scalars(stmt)
    ]


__all__ = [
    "ActiveRunConflict",
    "RetryRefused",
    "build_refresh_reuse_seed",
    "cancel_active_runs_for_subject",
    "create_run",
    "enqueue_run",
    "enqueue_source_retry",
    "get_history",
    "get_latest_completed_for_subject",
    "get_run",
    "normalize_address",
    "progress_for_run",
    "reconcile_active_runs_for_subject",
    "retryable_failed_sources_for_run",
]
