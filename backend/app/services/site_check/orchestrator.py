"""Public-data orchestrator — dispatches every registered
adapter against one PropertyIdentity and records the results
on a SiteCheckRun row.

Per docs/specs/public_data.md §6.4 — lifts the legacy_services
PublicIntelCollector.collect_all shape with these specific
adaptations:
  - geocoding routes through the arcgis_geocode adapter (not
    Nominatim)
  - results land in `public_intel_runs.adapter_results` keyed
    by source_id (legacy_services kept them in a flat JSON file)
  - inactive adapters from the registry contribute their
    `coverage_status="not_yet_wired"` result to the run record
    rather than being silently skipped
  - per-call usage rows land in `public_data_provider_usage`
  - per-source try/except preserves legacy_services partial-
    failure semantics (run completes with `sources_failed`
    populated as long as at least one source returned data)
"""

from __future__ import annotations

import importlib
import logging
import math
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import requests
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config.settings import get_settings
from app.core.job_queue import LeaseLost, assert_lease, write_progress
from app.models import Job, SiteCheckCoverageGap, SiteCheckRun

# The narrative wave's builder (site_check.md §11) — optional until that
# module lands; without it runs are deterministic-only (§10.3 floor).
try:  # pragma: no cover - trivial import guard
    from app.services.site_check.narratives import build_narratives
except ImportError:  # narratives module not landed yet
    build_narratives = None
from app.services import audit
from app.services.site_check import registry, usage
from app.services.site_check.adapters._base import (
    AdapterInactiveError,
    AdapterResult,
    http_session,
    stub_inactive_result,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)
from app.services.site_check.entitlement_reader import build_entitlement_reads
from app.services.site_check.historical_aerials import build_historical_aerials
from app.services.site_check.identity import PropertyIdentity

log = logging.getLogger("propdesk.site_check.orchestrator")

TERMINAL_PUBLIC_INTEL_STATUSES = {"completed", "failed", "cancelled"}


def _refresh_if_terminal(
    session: Session,
    run: SiteCheckRun,
) -> SiteCheckRun | None:
    """Return a refreshed terminal run if another session already ended it.

    Do not refresh non-terminal rows. `session.refresh(run)` overwrites pending
    scalar and JSON assignments that `run_collect` is about to commit.
    """
    try:
        with session.no_autoflush:
            status = session.scalars(
                select(SiteCheckRun.status).where(SiteCheckRun.id == run.id)
            ).first()
    except Exception:
        log.debug(
            "site_check terminal status check failed for run_id=%s",
            getattr(run, "id", None),
            exc_info=True,
        )
        return None
    if status not in TERMINAL_PUBLIC_INTEL_STATUSES:
        return None
    try:
        session.refresh(run)
    except Exception:
        log.debug(
            "site_check terminal refresh failed for run_id=%s",
            getattr(run, "id", None),
            exc_info=True,
        )
        return None
    if run.status in TERMINAL_PUBLIC_INTEL_STATUSES:
        return run
    return None

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
    "plats_intel": "Active plats",
    "public_aerial_imagery": "Public aerial imagery",
    "usgs_elevation": "USGS parcel elevation",
    "txdot_extended": "Extended TxDOT data",
    "arcgis_places": "ArcGIS places",
    "eia_state_fuel_sales": "EIA state fuel use",
    "site_constraints": "Site constraints and infrastructure",
    "drainage_screen": "Drainage screen",
}


def _source_label(source_id: str) -> str:
    return _SOURCE_LABELS.get(
        source_id,
        source_id.replace("_", " ").title(),
    )


def _adapter_result_payload(
    result: AdapterResult,
    *,
    refresh_status: str | None = None,
) -> dict[str, Any]:
    """Serialize one result with its registry-owned cache contract."""
    payload = result.model_dump(mode="json")
    payload["result_schema_version"] = registry.result_schema_version(
        result.source_id
    )
    if refresh_status is not None:
        payload["refresh_status"] = refresh_status
    return payload


def _public_data_adapter_id(source_id: str) -> str:
    if source_id == "arcgis_geocode":
        return "geocoder"
    return source_id


def _failed_source_labels(failed: list[dict]) -> list[str]:
    labels: list[str] = []
    for entry in failed:
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not source:
            continue
        label = _source_label(source)
        if label not in labels:
            labels.append(label)
    return labels


def _progress_payload(progress: dict[str, Any]) -> dict[str, Any]:
    payload = dict(progress)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    return payload


def _write_public_intel_progress(
    session: Session,
    *,
    job_id: str | None,
    lease_token: str | None = None,
    progress: dict[str, Any],
) -> None:
    """Lease-fenced progress checkpoint.

    A progress write is NOT advisory bookkeeping — it is this worker's
    checkpoint (site_check.md §7.4). It commits everything staged since the
    last commit together with the guarded `jobs` UPDATE, so a lost lease must
    roll the whole unit back and stop the run BEFORE the next provider call.
    `LeaseLost` therefore propagates; only genuine infrastructure errors on an
    otherwise-valid lease are swallowed.
    """
    if not job_id or not lease_token:
        # No job (inline/direct call) or no held lease — every queue write
        # must be lease-fenced (site_check.md §7.4), so skip rather than
        # write unfenced. Staged state stays pending for the next commit.
        return
    try:
        write_progress(session, job_id, lease_token, _progress_payload(progress))
    except LeaseLost:
        raise
    except Exception:
        log.warning(
            "failed to write public intel progress job_id=%s",
            job_id,
            exc_info=True,
        )


def _source_progress(
    *,
    stage: str,
    status: str,
    message: str,
    source_id: str | None = None,
    completed_sources: int,
    total_sources: int,
    failed_sources_count: int = 0,
    failed_sources: list[dict] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "message": message,
        "completed_sources": completed_sources,
        "total_sources": total_sources,
        "failed_sources_count": failed_sources_count,
    }
    if failed_sources:
        payload["failed_source_labels"] = _failed_source_labels(failed_sources)
    if source_id:
        payload["current_source"] = source_id
        payload["current_source_label"] = _source_label(source_id)
        payload["public_data_adapter"] = _public_data_adapter_id(source_id)
        payload["public_data_adapter_status"] = status
        payload["public_data_adapters_completed"] = completed_sources
        payload["public_data_adapters_total"] = total_sources
    return payload


def _emit_source_progress(
    session: Session,
    *,
    job_id: str | None,
    lease_token: str | None = None,
    analysis_progress_callback: Callable[[dict[str, Any]], None] | None,
    progress: dict[str, Any],
) -> None:
    _write_public_intel_progress(
        session,
        job_id=job_id,
        lease_token=lease_token,
        progress=progress,
    )
    if analysis_progress_callback is None:
        return
    payload = dict(progress)
    payload["stage"] = "public_data"
    try:
        analysis_progress_callback(payload)
    except Exception:
        log.warning(
            "failed to write public-data adapter analysis progress",
            exc_info=True,
        )


# ── Per-source dispatch via importlib ─────────────────────────────


def _load_adapter_fetch(source_id: str):
    """Returns the `fetch` callable for the adapter module
    `app.services.site_check.adapters.<source_id>`. Inactive
    adapters' `fetch` raises AdapterInactiveError if invoked —
    the orchestrator's dispatch path short-circuits to
    `stub_inactive_result` BEFORE calling, so this is only
    invoked for active adapters."""
    module = importlib.import_module(
        f"app.services.site_check.adapters.{source_id}",
    )
    return module.fetch


def _classify_anchor(
    run, results: dict[str, dict],
) -> tuple[str, str]:
    """Decide whether the run's spatial anchor is the parcel
    polygon (verified) or the geocoded address (provisional).

    Rule:
      - parcel-verified: parcel adapter (tcad_parcel /
        wcad_parcel) returned a real polygon geometry the
        downstream adapters could query. Today no adapter
        does this — both surface the manual-lookup URL only.
      - geocode-provisional: any other case. Includes when
        parcel_id is supplied but no polygon was resolved.

    UI uses anchor_quality to render a banner; downstream
    consumers (Site Analysis, Q&A) can downgrade their
    confidence when reading provisional runs.
    """
    for parcel_source in ("tcad_parcel", "wcad_parcel"):
        facts = (
            (results.get(parcel_source) or {})
            .get("normalized_facts") or {}
        )
        if facts.get("parcel_polygon") or facts.get("acres"):
            return ("parcel", "verified")
    return ("geocode", "provisional")


# ── Authoritative-anchor reconciliation ──────────────────────────
# The CAD parcel polygon is ground truth for WHERE the property is; the
# address geocode is only a bootstrap to find it. When a parcel_id is
# supplied the geocode can land in the wrong region (the 2026-07-21
# "FM 971" → "FM 1971 Panola County" fuzzy match, 350km away) while the
# parcel still resolves correctly by PIN — and every spatial adapter then
# queries the wrong point. We resolve the parcel geometry BEFORE the
# spatial waves and, on a hard geocode↔parcel contradiction, re-anchor on
# the parcel centroid.

_PARCEL_SOURCES: tuple[str, ...] = ("wcad_parcel", "tcad_parcel")


def _iter_outer_rings(polygon: dict | None):
    """Yield the outer ring of each GeoJSON Polygon / MultiPolygon."""
    if not isinstance(polygon, dict):
        return
    geom_type = polygon.get("type")
    coords = polygon.get("coordinates") or []
    if geom_type == "Polygon" and coords:
        yield coords[0]
    elif geom_type == "MultiPolygon":
        for poly in coords:
            if poly:
                yield poly[0]


def _polygon_representative_point(
    polygon: dict | None,
) -> tuple[float, float] | None:
    """Vertex-mean of the outer ring(s) as a (lat, lng) representative
    point. Ample precision for a "is the anchor in the right place?"
    check and for re-anchoring within a parcel (downstream screens use
    radii); not a survey-grade centroid."""
    pts = [
        pt for ring in _iter_outer_rings(polygon)
        for pt in ring
        if isinstance(pt, (list, tuple)) and len(pt) >= 2
    ]
    if not pts:
        return None
    lng = sum(float(p[0]) for p in pts) / len(pts)
    lat = sum(float(p[1]) for p in pts) / len(pts)
    return (lat, lng)


def _point_in_ring(lat: float, lng: float, ring: list) -> bool:
    """Ray-casting point-in-polygon on a single ring of [lng, lat] pairs."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        try:
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
        except (TypeError, ValueError, IndexError):
            j = i
            continue
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lat: float, lng: float, polygon: dict | None) -> bool:
    return any(
        _point_in_ring(lat, lng, ring)
        for ring in _iter_outer_rings(polygon)
    )


def _haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float,
) -> float:
    r = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _resolve_parcel_geometry(
    identity: PropertyIdentity, *, sess: requests.Session,
) -> dict | None:
    """Fetch the supplied parcel's polygon by PIN via the county parcel
    adapter(s). Returns the first normalized_facts carrying a
    `parcel_polygon`, or None. Read-only + side-effect-free: this call is
    invisible to run bookkeeping (the parcel source still runs normally in
    its wave); it exists only to reconcile the anchor."""
    for source_id in _PARCEL_SOURCES:
        try:
            result = _load_adapter_fetch(source_id)(identity, session=sess)
        except Exception as exc:  # never let reconciliation break the run
            log.debug("anchor recon: %s fetch failed: %s", source_id, exc)
            continue
        facts = getattr(result, "normalized_facts", None) or {}
        if facts.get("parcel_polygon"):
            return facts
    return None


def _reconcile_anchor_with_parcel(
    run: SiteCheckRun,
    identity: PropertyIdentity,
    *,
    sess: requests.Session,
) -> PropertyIdentity:
    """When a parcel_id is supplied, ensure the geocode anchor actually
    lies on/near that parcel. On a hard contradiction (anchor is NOT inside
    the polygon AND is farther than the configured threshold from it),
    override `run.lat/lng` with the parcel centroid so every downstream
    adapter queries the real property. Returns the (possibly re-anchored)
    identity. No-op when there is no parcel_id, no anchor, or no polygon."""
    if not run.parcel_id or run.lat is None or run.lng is None:
        return identity

    facts = _resolve_parcel_geometry(identity, sess=sess)
    if not facts:
        return identity
    rep = _polygon_representative_point(facts.get("parcel_polygon"))
    if rep is None:
        return identity
    rep_lat, rep_lng = rep

    inside = _point_in_polygon(run.lat, run.lng, facts.get("parcel_polygon"))
    dist_km = _haversine_km(run.lat, run.lng, rep_lat, rep_lng)
    max_km = get_settings().public_intel_anchor_parcel_max_km
    if inside or dist_km <= max_km:
        return identity  # geocode agrees with the parcel — keep it

    log.warning(
        "anchor reconciliation: geocode (%.5f, %.5f) is %.0f km from parcel "
        "%s (%.5f, %.5f) and outside its polygon — re-anchoring on the "
        "parcel centroid",
        run.lat, run.lng, dist_km, run.parcel_id, rep_lat, rep_lng,
    )
    prior_lat, prior_lng = run.lat, run.lng
    run.lat = rep_lat
    run.lng = rep_lng
    run.geocode_source = "parcel_centroid"

    # Annotate the geocode audit trail with the correction so the UI /
    # reviewers can see the anchor was moved and why.
    geo_payload = dict((run.adapter_results or {}).get("arcgis_geocode") or {})
    if geo_payload:
        advisories = list(geo_payload.get("advisories") or [])
        advisories.append(
            f"Anchor corrected: the geocoded point was {dist_km:.0f} km from "
            f"parcel {run.parcel_id} and outside its boundary (likely a "
            f"wrong-road/region geocode match). Re-anchored on the CAD parcel "
            f"centroid."
        )
        geo_payload["advisories"] = advisories
        geo_payload["anchor_correction"] = {
            "prior_lat": prior_lat,
            "prior_lng": prior_lng,
            "corrected_lat": rep_lat,
            "corrected_lng": rep_lng,
            "distance_km": round(dist_km, 1),
            "parcel_id": run.parcel_id,
        }
        run.adapter_results = {
            **(run.adapter_results or {}),
            "arcgis_geocode": geo_payload,
        }

    return identity.model_copy(update={"lat": rep_lat, "lng": rep_lng})


# Counties with wired parcel adapters today. Anything else in Texas produces a
# typed `not_supported_in_jurisdiction` signal via the coverage_gap service.
# `tcad_parcel` is the legacy source_id, but it now dispatches to configured
# county parcel polygon layers for Travis, Hays, Bastrop, and Blanco. WCAD stays
# separate because Williamson has its own candidate/parcel semantics.
_TEXAS_COUNTIES_WITH_PARCEL_ADAPTER = frozenset({
    "travis",
    "williamson",
    "hays",
    "bastrop",
    "blanco",
})


def _maybe_record_parcel_coverage_gap(
    session,
    *,
    run,
    identity,
) -> None:
    """If the run's identity points at a Texas county we don't
    have a parcel adapter for, record a coverage_gap_request
    (per-customer-deduped) so the operator sees the demand
    signal in the admin queue.

    Best-effort: logs and swallows on failure. A broken
    coverage-capture path must never fail the underlying
    public-intel run.

    Why this lives here, not inside each parcel adapter: the
    "no adapter for this county" case is the ABSENCE of any
    adapter response. The orchestrator is the only layer that
    knows what was attempted versus what succeeded. Per the
    AI-for-judgment/code-for-plumbing rule, this is plumbing.
    """
    try:
        county = (identity.county or "").strip().lower()
        if not county:
            return
        if county in _TEXAS_COUNTIES_WITH_PARCEL_ADAPTER:
            return
        # Capability slug matches the spec's example
        # ("county_parcel_adapter_missing"). Tenant-deduped per
        # site_check.md §6 — one row per (tenant, capability,
        # county), DB-enforced: ON CONFLICT DO NOTHING against the
        # unique constraint, so a concurrent same-county run can never
        # abort the completion commit. Rows are operator-facing demand
        # signals, never shown to users.
        session.execute(
            pg_insert(SiteCheckCoverageGap).values(
                id=str(uuid.uuid4()),
                tenant_id=run.tenant_id,
                capability="parcel_adapter",
                county=county.title(),
                reason="county_parcel_adapter_missing",
                created_at=datetime.now(UTC),
            ).on_conflict_do_nothing(
                constraint=(
                    "uq_site_check_coverage_gaps_tenant_capability_county"
                ),
            )
        )
    except Exception:
        log.warning(
            "coverage_gap: failed to record parcel gap for "
            "run=%s county=%s — continuing without it",
            run.id, getattr(identity, "county", None),
            exc_info=True,
        )


def _jurisdiction_verdict(results: dict[str, dict]) -> dict | None:
    """The jurisdiction_resolver verdict (Wave 0.5) for city-aware adapters to
    consume — None when the resolver didn't run / county not wired."""
    facts = (results.get("jurisdiction_resolver") or {}).get("normalized_facts")
    return facts if isinstance(facts, dict) else None


def _build_adapter_config(
    source_id: str,
    results: dict[str, dict],
    *,
    project_context: dict[str, Any] | None = None,
) -> dict | None:
    """Per-adapter context that depends on a prior adapter's
    result. Used when an adapter needs more than the property
    identity to do its job — e.g. tax_intel needs the list
    of district names that district_overlays surfaced.

    Returns None when the adapter has no upstream dependency
    (most adapters fall through here)."""
    if source_id == "tax_intel":
        overlays = (
            results.get("district_overlays") or {}
        ).get("normalized_facts") or {}
        names: list[str] = []
        # 6 buckets — verbatim from legacy_services
        # core/public_intel.py lines 830-834. ESDs were missing
        # from my prior list, which dropped Hutto WilCo's "ESD #3"
        # from tax_intel queries.
        for bucket in (
            "mud_districts", "wcid_districts",
            "esd_districts", "pid_districts",
            "tirz_districts", "school_districts",
        ):
            for entry in overlays.get(bucket) or []:
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str) and name and name != "Unnamed":
                        names.append(name)
        return {"district_names": names}
    if source_id == "austin_zoning":
        return {"jurisdiction": _jurisdiction_verdict(results)}
    if source_id == "twdb_ccn":
        return {"jurisdiction": _jurisdiction_verdict(results)}
    if source_id == "txdot_extended":
        return {"upstream_results": results}
    if source_id == "zoning_standards":
        # district = austin_zoning's resolved code. Prefer
        # `zoning_type` over `base_zone`: zoning_type carries
        # the city-prefixed ETJ marker ("AUSTIN_ETJ") that
        # zoning_standards short-circuits on; base_zone for
        # the same parcel is just "ETJ" which fails the
        # endswith("_ETJ") test and falls into "district not
        # curated" (legacy_services parity gap caught 2026-05-05).
        # For Cedar Park, zoning_type is a display label
        # ("LB - LOCAL BUSINESS") while base_zone is the curated
        # rules key ("LB"), so use base_zone there.
        zoning_facts = (
            results.get("austin_zoning") or {}
        ).get("normalized_facts") or {}
        zoning_type = zoning_facts.get("zoning_type")
        base_zone = zoning_facts.get("base_zone")
        if str(zoning_facts.get("jurisdiction") or "").lower() == "cedar park":
            district = base_zone or zoning_type or None
        else:
            district = zoning_type or base_zone or None
        return {
            "district": district,
            "upstream_results": results,
            "jurisdiction": _jurisdiction_verdict(results),
        }
    if source_id == "entitlement_pipeline":
        return {
            "upstream_results": results,
            "jurisdiction": _jurisdiction_verdict(results),
        }
    if source_id == "site_constraints":
        return {"upstream_results": results}
    if source_id == "drainage_screen":
        return {"upstream_results": results}
    if source_id == "plats_intel":
        from app.core import secret_store

        settings_obj = get_settings()
        brave_secret = secret_store.get_secret("brave_search")
        brave_key = brave_secret.get_secret_value() if brave_secret else None
        bing_secret = secret_store.get_secret("bing_search")
        bing_key = bing_secret.get_secret_value() if bing_secret else None
        return {
            "upstream_results": results,
            "project_context": project_context or {},
            "public_plat_discovery_enabled": (
                settings_obj.public_plat_discovery_enabled
            ),
            "public_plat_search_provider": (
                settings_obj.public_plat_search_provider
            ),
            "searxng_search_url": settings_obj.public_plat_searxng_url,
            "brave_search_api_key": brave_key,
            "bing_search_api_key": bing_key,
        }
    # FLU document-read moved to the orchestrator (enrich_flu_with_reading),
    # which holds the DB session + license + model for the metered AI call; the
    # future_land_use adapter no longer needs a read config.
    return None


# ── Parallel fan-out (spec: docs/specs/public_intel_parallel_fetch.md) ─

# Adapters that need verified parcel/CAD geometry but then produce inputs for
# other city-aware consumers. They run after Wave 1, before Wave 2.
_POST_PARCEL_SOURCES: frozenset[str] = frozenset({
    "austin_zoning",
    "fema_nfhl",
    "usgs_elevation",
})

# Adapters that depend on prior-wave facts: austin_zoning identity mutation,
# parcel/CAD results, or district facts. Must run AFTER Wave 1 has resolved
# base public-data facts and after the post-parcel zoning wave.
_WAVE_2_SOURCES: frozenset[str] = frozenset({
    "drainage_screen",
    "txdot_extended",
    "future_land_use",
    "district_overlays",
    "entitlement_pipeline",
    "plats_intel",
    "zoning_standards",
})

# Adapters that depend on a Wave-2 adapter's result via
# _build_adapter_config (tax_intel reads district_overlays).
_WAVE_3_SOURCES: frozenset[str] = frozenset({"tax_intel", "site_constraints"})


def _source_waves(source_ids: list[str]) -> tuple[
    list[str], list[str], list[str], list[str],
]:
    non_geocode_sources = [
        s for s in source_ids
        if s not in ("arcgis_geocode", "jurisdiction_resolver")
    ]
    wave_1_sources = [
        s for s in non_geocode_sources
        if (
            s not in _POST_PARCEL_SOURCES
            and s not in _WAVE_2_SOURCES
            and s not in _WAVE_3_SOURCES
        )
    ]
    post_parcel_sources = [
        s for s in non_geocode_sources if s in _POST_PARCEL_SOURCES
    ]
    wave_2_sources = [
        s for s in non_geocode_sources if s in _WAVE_2_SOURCES
    ]
    wave_3_sources = [
        s for s in non_geocode_sources if s in _WAVE_3_SOURCES
    ]
    return wave_1_sources, post_parcel_sources, wave_2_sources, wave_3_sources


def _provider_cap_key(
    reg: registry.AdapterRegistration, caps: dict[str, int],
) -> str:
    """Resolve which concurrency-cap bucket an adapter belongs to.
    Preference order: explicit `provider_concurrency_group` (if it
    matches a cap), then `provider` (if it matches a cap), then
    `default`."""
    provider_group = getattr(reg, "provider_concurrency_group", None)
    if provider_group and provider_group in caps:
        return provider_group
    provider = getattr(reg, "provider", None)
    if provider and provider in caps:
        return provider
    return "default"


def _adapter_worker(
    source_id: str,
    *,
    identity: PropertyIdentity,
    sess: requests.Session,
    results_snapshot: dict[str, dict],
    project_context: dict[str, Any] | None,
    semaphore: threading.Semaphore,
) -> tuple[str, Any]:
    """Thread worker: acquire the per-provider semaphore, dispatch
    the adapter, return (source_id, outcome). NEVER raises — captures
    exceptions as the outcome so the main thread persists them
    uniformly. `results_snapshot` is the read-only results dict at
    submit time (used by _build_adapter_config for inter-adapter
    config). Wave-boundary mutations to identity + results are applied
    by the orchestrator before submitting the next wave; within a
    wave the snapshot is stable.
    """
    with semaphore:
        adapter_config = _build_adapter_config(
            source_id,
            results_snapshot,
            project_context=project_context,
        )
        try:
            fetch_fn = _load_adapter_fetch(source_id)
            reg = registry.get(source_id)
            # Attribution for hosts outside the paid-upstream route
            # table (finding 4): requests made under this dispatch bill
            # to the running source's registry provider/service.
            with usage.attribute_to(reg.provider, reg.service):
                outcome: Any = fetch_fn(
                    identity, session=sess, config=adapter_config,
                )
        except AdapterInactiveError as exc:
            outcome = exc
        except Exception as exc:
            outcome = exc
    return source_id, outcome


def _apply_identity_mutations(
    identity: PropertyIdentity,
    *,
    completed_sources_this_wave: list[str],
    results: dict[str, dict],
) -> PropertyIdentity:
    """Apply parcel + zoning mutations after a wave completes.
    Mirrors the inline mutations in the legacy sequential loop —
    same fields, same order (parcel first, then austin_zoning).
    Pure function; returns a new identity (model_copy).
    """
    for parcel_source in ("tcad_parcel", "wcad_parcel"):
        if parcel_source not in completed_sources_this_wave:
            continue
        parcel_facts = (
            (results.get(parcel_source) or {}).get("normalized_facts") or {}
        )
        updates: dict[str, Any] = {}
        if parcel_facts.get("parcel_id"):
            updates["parcel_id"] = parcel_facts.get("parcel_id")
        acreage = (
            parcel_facts.get("acreage") or parcel_facts.get("acres")
        )
        if acreage:
            updates["acreage"] = acreage
        if parcel_facts.get("parcel_polygon"):
            updates["parcel_geometry"] = parcel_facts.get("parcel_polygon")
        if updates:
            identity = identity.model_copy(update=updates)

    if "austin_zoning" in completed_sources_this_wave:
        zoning_facts = (
            (results.get("austin_zoning") or {}).get("normalized_facts")
            or {}
        )
        j = zoning_facts.get("jurisdiction")
        if isinstance(j, str) and j.strip():
            clean = (
                j.replace(" ETJ", "")
                 .replace(" (unincorporated)", "")
                 .strip()
            )
            if clean:
                identity = identity.model_copy(update={
                    "jurisdiction_candidates": [
                        *identity.jurisdiction_candidates,
                        clean,
                    ],
                })
    return identity


def _build_project_context(
    session: Session,
    run: SiteCheckRun,
) -> dict[str, Any]:
    """Optional text context for adapters that support project-aware search.

    PropDesk has no projects or uploaded documents, so Phase A passes an
    EMPTY project_context (site_check.md §3.3). The plats adapter treats it
    as optional enrichment and handles the empty dict.
    """
    del session, run  # signature kept stable for future context sources
    return {}


def _run_parallel_wave(
    session: Session,
    *,
    run: SiteCheckRun,
    provider_calls: usage.ProviderCallCollector,
    wave_sources: list[str],
    identity: PropertyIdentity,
    sess: requests.Session,
    results: dict[str, dict],
    queried: list[dict],
    failed: list[dict],
    project_context: dict[str, Any] | None,
    completed_sources: int,
    total_sources: int,
    job_id: str | None,
    lease_token: str | None,
    analysis_progress_callback: Callable[[dict[str, Any]], None] | None,
    semaphores: dict[str, threading.Semaphore],
    max_workers: int,
    caps: dict[str, int],
) -> tuple[int, PropertyIdentity]:
    """Run one wave of public-data adapters in parallel.

    Inactive adapters short-circuit on the main thread (no provider
    call, no semaphore). Active adapters submit to the
    ThreadPoolExecutor with their per-provider semaphore. Results are
    persisted on the main thread as `as_completed` returns them.
    After the wave, parcel + zoning identity mutations are applied
    in deterministic order.

    Returns (updated completed_sources count, mutated identity).
    """
    if not wave_sources:
        return completed_sources, identity

    active_sources: list[str] = []

    # ── 1. Main-thread pre-pass: short-circuit inactive sources ──
    for source_id in wave_sources:
        reg = registry.get(source_id)
        label = _source_label(source_id)
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="sources",
                status="running",
                message=f"Checking {label}.",
                source_id=source_id,
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )
        if not reg.active_in_v0:
            results[source_id] = _adapter_result_payload(
                stub_inactive_result(source_id),
            )
            queried.append({
                "source": source_id, "status": "not_yet_wired",
            })
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status="skipped",
                    message=f"{label} is not wired yet; skipped.",
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )
            continue
        active_sources.append(source_id)

    if not active_sources:
        return completed_sources, identity

    # ── 2. Submit active adapters to the pool ───────────────────
    # Snapshot results so _build_adapter_config sees a stable view
    # within this wave. Prior-wave mutations are already in `results`.
    results_snapshot = dict(results)
    default_cap = caps.get("default", 4)

    def _sem_for(reg: registry.AdapterRegistration) -> threading.Semaphore:
        key = _provider_cap_key(reg, caps)
        if key not in semaphores:
            semaphores[key] = threading.Semaphore(
                max(1, caps.get(key, default_cap)),
            )
        return semaphores[key]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for source_id in active_sources:
            reg = registry.get(source_id)
            fut = pool.submit(
                _adapter_worker,
                source_id,
                identity=identity,
                sess=sess,
                results_snapshot=results_snapshot,
                project_context=project_context,
                semaphore=_sem_for(reg),
            )
            futures[fut] = source_id

        # ── 3. Collect + persist on main thread ─────────────────
        for fut in as_completed(futures):
            source_id, outcome = fut.result()
            reg = registry.get(source_id)
            label = _source_label(source_id)

            if isinstance(outcome, AdapterInactiveError):
                # Registry says active but the file is a stub.
                log.error(
                    "registry-stub mismatch on %r — "
                    "fix the activation matrix",
                    source_id,
                )
                results[source_id] = _adapter_result_payload(
                    stub_inactive_result(source_id),
                )
                failed.append({
                    "source": source_id, "error": "registry-stub mismatch",
                })
                completed_sources += 1
                _emit_source_progress(
                    session,
                    job_id=job_id,
                    lease_token=lease_token,
                    analysis_progress_callback=analysis_progress_callback,
                    progress=_source_progress(
                        stage="sources",
                        status="failed",
                        message=f"{label} could not be checked.",
                        source_id=source_id,
                        completed_sources=completed_sources,
                        total_sources=total_sources,
                        failed_sources_count=len(failed),
                        failed_sources=failed,
                    ),
                )
                continue

            if isinstance(outcome, Exception):
                log.warning(
                    "adapter %r crashed: %s", source_id, outcome,
                )
                failed_result = _adapter_result_payload(
                    AdapterResult(
                        source_id=source_id,
                        source_access_status=(
                            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
                        ),
                        data_confidence_status=DataConfidenceStatus.UNKNOWN,
                        coverage_status=CoverageStatus.PARTIAL,
                        normalized_facts={},
                        errors=[f"adapter crashed: {outcome}"],
                        fetched_at=datetime.now(UTC),
                    ),
                    refresh_status="fetched_failed",
                )
                results[source_id] = failed_result
                failed.append({
                    "source": source_id, "error": str(outcome),
                })
                usage.flush_collector(
                    session, run=run, collector=provider_calls,
                )
                completed_sources += 1
                _emit_source_progress(
                    session,
                    job_id=job_id,
                    lease_token=lease_token,
                    analysis_progress_callback=analysis_progress_callback,
                    progress=_source_progress(
                        stage="sources",
                        status="failed",
                        message=(
                            f"{label} failed; continuing with other "
                            "sources."
                        ),
                        source_id=source_id,
                        completed_sources=completed_sources,
                        total_sources=total_sources,
                        failed_sources_count=len(failed),
                        failed_sources=failed,
                    ),
                )
                continue

            # Successful AdapterResult (may carry soft-fail errors).
            result: AdapterResult = outcome
            result_payload = _adapter_result_payload(
                result,
                refresh_status=(
                    "fetched_failed" if result.errors else "fetched"
                ),
            )
            results[source_id] = result_payload
            queried.append({
                "source": source_id,
                "status": result_payload["refresh_status"],
            })
            if result.errors:
                failed.append({
                    "source": source_id,
                    "error": "; ".join(result.errors),
                })
            usage.flush_collector(
                session, run=run, collector=provider_calls,
            )
            completed_sources += 1
            terminal_status = "complete"
            terminal_message = f"Finished {label}."
            if result.errors:
                terminal_status = "failed"
                terminal_message = (
                    f"{label} returned issues; continuing with other "
                    "sources."
                )
            elif result.coverage_status in {
                CoverageStatus.NOT_YET_WIRED,
                CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
            }:
                terminal_status = "skipped"
                terminal_message = (
                    f"{label} is not supported here; skipped."
                )
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status=terminal_status,
                    message=terminal_message,
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )

    # ── 4. Apply identity mutations from this wave's results ────
    identity = _apply_identity_mutations(
        identity,
        completed_sources_this_wave=active_sources,
        results=results,
    )
    return completed_sources, identity


# ── Narrative hook (site_check.md §3.2 / §11) ─────────────────────


# ── Cancellation checkpoints (site_check.md §7.5) ─────────────────

CANCELLED_MESSAGE = "This lookup was cancelled before it finished."

# Rendered verbatim by the report screen when geocoding fails (§8.3
# broker-safe rule) — mirrors the client's geocode_failed fallback copy.
_GEOCODE_FAILED_REASON = (
    "We couldn't find this address — check the spelling or add the city."
)


def _cancel_requested(session: Session, run_id: str) -> bool:
    """Fresh DB read of cancel_requested_at. READ COMMITTED sees the
    API's committed flag even mid-run; never trusts the identity map."""
    try:
        with session.no_autoflush:
            value = session.execute(
                select(SiteCheckRun.cancel_requested_at)
                .where(SiteCheckRun.id == run_id)
            ).scalar_one_or_none()
    except Exception:
        log.debug(
            "site_check cancel-flag check failed for run_id=%s",
            run_id, exc_info=True,
        )
        return False
    return value is not None


def _lock_run_row(session: Session, run_id: str):
    """`SELECT status, cancel_requested_at … FOR UPDATE` on the run row — the
    ONE serialization point shared by the worker's terminal transitions and
    the §8.5 cancel endpoint (finding 2). Whichever transaction commits first
    wins; the loser sees the winner's state under the lock and refuses.

    Column-only + `no_autoflush` on purpose: the caller usually has pending
    JSON assignments on the run instance that must NOT be flushed before we
    know whether this transition is still allowed.
    """
    with session.no_autoflush:
        return session.execute(
            select(SiteCheckRun.status, SiteCheckRun.cancel_requested_at)
            .where(SiteCheckRun.id == run_id)
            .with_for_update()
        ).first()


def _abandon(session: Session, run: SiteCheckRun, why: str) -> SiteCheckRun:
    """Drop every staged mutation and hand back the row as another actor left
    it. No further provider call or state write may follow."""
    session.rollback()
    try:
        session.refresh(run)
    except Exception:
        log.debug("refresh after abandoned %s failed", why, exc_info=True)
    log.info("site_check worker abandoned (%s) run_id=%s", why, run.id)
    return run


def _honor_cancellation(
    session: Session,
    run: SiteCheckRun,
    *,
    job_id: str | None,
    lease_token: str | None,
    results: dict[str, dict],
    queried: list[dict],
    failed: list[dict],
) -> SiteCheckRun | None:
    """§7.5 checkpoint: when cancel_requested_at is set, stage the
    completed waves (if any) lease-fenced and terminalize run + job
    `cancelled` TOGETHER in one transaction. ALWAYS `cancelled`, never
    `failed` — including the pre-Wave-0 zero-staged case. Returns the
    terminal run when the cancel was honored (caller returns it), None
    when no cancel is pending.

    Serialization (finding 2): the terminal transition takes the run row
    lock, so it cannot interleave with the §8.5 cancel endpoint. The run
    write is fenced transitively too: the guarded job UPDATE
    (WHERE lease_token = ours AND status = 'running') rides the SAME
    transaction — if the lease was invalidated (supersede / stale
    recovery), it matches 0 rows, the staged unit rolls back and
    `LeaseLost` stops the worker. The one `site_check.run_cancelled`
    audit row was already written by the API at request time; the worker
    writes none here.
    """
    if not _cancel_requested(session, run.id):
        return None
    locked = _lock_run_row(session, run.id)
    if locked is None:
        return _abandon(session, run, "run disappeared at cancel checkpoint")
    if locked.cancel_requested_at is None:
        # The flag was rolled back between the unlocked read and the lock —
        # nothing to honor. Do NOT roll back here: staged-but-uncommitted
        # work belongs to the caller, which commits it at its next fenced
        # checkpoint (that commit also releases this row lock).
        return None
    if locked.status in TERMINAL_PUBLIC_INTEL_STATUSES:
        # Another actor terminalized this run first — it wins.
        return _abandon(session, run, "run already terminal at cancel checkpoint")
    if job_id and lease_token:
        fenced = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == lease_token,
                Job.status == "running",
            )
            .values(
                status="cancelled",
                error_code="cancelled_by_user",
                error_message=CANCELLED_MESSAGE,
                finished_at=func.now(),
                # Terminal progress rides the terminal job transaction so the
                # detail poll can never show a live-looking stale stage
                # against a terminal job (finding 3).
                progress=_progress_payload({
                    "stage": "cancelled",
                    "status": "cancelled",
                    "message": CANCELLED_MESSAGE,
                    "completed_sources": len(results or {}),
                    "total_sources": len(results or {}),
                    "failed_sources_count": len(failed or []),
                }),
            )
        )
        if fenced.rowcount != 1:
            # Lease no longer ours — another actor already terminalized
            # this work (supersede or recovery). Roll the staged unit back
            # and stop before any further work (§7.4).
            session.rollback()
            raise LeaseLost(job_id)
    run.adapter_results = deepcopy(results or {})
    run.sources_queried = deepcopy(queried or [])
    run.sources_failed = deepcopy(failed or [])
    run.derived = deepcopy(_build_derived(results or {}))
    for _attr in ("adapter_results", "sources_queried",
                  "sources_failed", "derived"):
        getattr(run, _attr)
        flag_modified(run, _attr)
    run.status = "cancelled"
    run.error_code = run.error_code or "cancelled_by_user"
    run.failure_reason = CANCELLED_MESSAGE
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()
    session.refresh(run)
    log.info(
        "site_check run cancelled at checkpoint run_id=%s staged_sources=%d",
        run.id, len(results or {}),
    )
    return run


# ── Run lifecycle ─────────────────────────────────────────────────


def run_collect(
    session: Session,
    *,
    run_id: str,
    job_id: str | None = None,
    lease_token: str | None = None,
    sess: requests.Session | None = None,
    analysis_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> SiteCheckRun:
    """Public entry point (§7.4 abandon path).

    Wraps the collect body so that a `LeaseLost` raised by ANY lease-fenced
    checkpoint — progress write, wave-boundary assertion, terminal staging —
    unwinds the whole run immediately: the staged unit is already rolled
    back, no further provider call happens, and the row is returned exactly
    as the actor that took the lease left it.
    """
    try:
        return _collect(
            session,
            run_id=run_id,
            job_id=job_id,
            lease_token=lease_token,
            sess=sess,
            analysis_progress_callback=analysis_progress_callback,
        )
    except LeaseLost:
        session.rollback()
        session.expire_all()
        log.info(
            "site_check run abandoned (lease lost) run_id=%s job_id=%s",
            run_id, job_id,
        )
        return session.get(SiteCheckRun, run_id, populate_existing=True)


def _collect(
    session: Session,
    *,
    run_id: str,
    job_id: str | None = None,
    lease_token: str | None = None,
    sess: requests.Session | None = None,
    analysis_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> SiteCheckRun:
    """Execute one fetch end-to-end. The SiteCheckRun row must
    already exist (created by the service layer when the user
    triggered the fetch); this function:

      1. Marks the run `running`, sets started_at.
      2. Geocodes (or re-uses cached lat/lng if present).
      3. Builds a PropertyIdentity.
      4. Iterates every registered source_id:
         - Active → invokes fetch(identity), records the
           AdapterResult + a usage row.
         - Inactive → records stub_inactive_result(source_id);
           NO usage row (no provider call happened).
         Per-source try/except — one failing adapter does NOT
         fail the run.
      5. Writes adapter_results, sources_queried,
         sources_failed, derived facts.
      6. Marks `completed` (or `failed` on geocode/all-sources
         failure) + finished_at, and writes the
         `site_check.run_completed` audit row (§8.7).

    Returns the refreshed run row.
    """
    run = session.get(SiteCheckRun, run_id)
    if run is None:
        raise ValueError(f"SiteCheckRun {run_id} not found")
    terminal = _refresh_if_terminal(session, run)
    if terminal is not None:
        return terminal

    # §7.5 checkpoint (a): BEFORE Wave 0. A cancel that landed while the
    # job sat queued (or between claim and start) terminalizes run + job
    # cancelled together — zero fetched sources staged.
    cancelled = _honor_cancellation(
        session, run,
        job_id=job_id,
        lease_token=lease_token,
        results=dict(run.adapter_results or {}),
        queried=list(run.sources_queried or []),
        failed=list(run.sources_failed or []),
    )
    if cancelled is not None:
        return cancelled

    # Zoning Navigator packet seeding SEVERED for Phase A
    # (site_check.md §3.2 — Phase C surface).

    # queued → running. Lease-fenced like every other worker write: the
    # guarded job UPDATE (which doubles as the "started" progress write)
    # rides the SAME transaction, so a worker whose lease was already
    # invalidated can never flip a superseded/recovered run back to running.
    run.status = "running"
    run.started_at = datetime.now(UTC)
    session.add(run)
    if job_id and lease_token:
        started = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == lease_token,
                Job.status == "running",
            )
            .values(progress=_progress_payload({
                "stage": "starting",
                "status": "running",
                "message": "Starting the lookup.",
                "completed_sources": 0,
                "total_sources": 0,
                "failed_sources_count": 0,
            }))
        )
        if started.rowcount != 1:
            session.rollback()
            raise LeaseLost(job_id)
    session.commit()
    session.refresh(run)

    provider_calls = usage.ProviderCallCollector()
    sess = http_session(sess, collector=provider_calls)
    source_ids = registry.all_source_ids()
    total_sources = len(source_ids)
    completed_sources = 0
    _emit_source_progress(
        session,
        job_id=job_id,
        lease_token=lease_token,
        analysis_progress_callback=analysis_progress_callback,
        progress=_source_progress(
            stage="geocoding",
            status="running",
            message="Locating the property address.",
            source_id="arcgis_geocode",
            completed_sources=completed_sources,
            total_sources=total_sources,
        ),
    )

    # ── 1. Geocoding ─────────────────────────────────────────
    if run.lat is not None and run.lng is not None:
        # Cached from a prior run on the same project — re-use.
        county = _resolve_county(
            explicit=run.county,
            geocode_county=None,
            address=run.address,
        )
        run.county = county or ""
        identity = PropertyIdentity(
            address=run.address,
            lat=run.lat,
            lng=run.lng,
            county=county,
            parcel_id=run.parcel_id,
        )
        completed_sources += 1
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="geocoding",
                status="complete",
                message="Using saved property coordinates.",
                source_id="arcgis_geocode",
                completed_sources=completed_sources,
                total_sources=total_sources,
            ),
        )
    else:
        try:
            with usage.attribute_to("arcgis", "geocode_stored"):
                geocode_result = _load_adapter_fetch("arcgis_geocode")(
                    PropertyIdentity(
                        address=run.address,
                        county=run.county or None,
                        parcel_id=run.parcel_id,
                    ),
                    session=sess,
                )
        except Exception as exc:
            log.exception("geocoder crashed unexpectedly")
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="geocoding",
                    status="failed",
                    message="We couldn't place this address on the map.",
                    source_id="arcgis_geocode",
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=1,
                ),
            )
            log.warning("site_check geocoder crashed for run %s: %s", run.id, exc)
            return _fail_run(
                session, run, error_code="geocode_failed",
                # failure_reason renders verbatim on the report screen —
                # plain copy only; the exception stays in the log.
                reason=_GEOCODE_FAILED_REASON,
                job_id=job_id,
                lease_token=lease_token,
            )

        # Record the geocode call regardless of outcome (audit).
        # §8.6 attribution contract: record the ACTUAL upstream
        # geocoder — a Nominatim success writes a `nominatim` row
        # (cost 0) and NO arcgis row; only an actual ArcGIS fallback
        # writes an `arcgis` row (documented divergence from the
        # the reference app logical-geocode row). A Google final-fallback
        # resolution (Admin 2026-08-01) writes ONE `google`/`geocode`
        # row priced from google_geocode_cost_micro_per_call — and no
        # nominatim/arcgis cost rows for tiers that didn't resolve.
        # Reviewer finding 4 (2026-08-01): the provider must NEVER be
        # chosen from the RESULT — a paid Google request that returned
        # ZERO_RESULTS was billed to arcgis. Every actual request was
        # already registered at the HTTP boundary by the collector;
        # flushing writes one row per provider/service truly called.
        geocode_provider = str(
            (geocode_result.normalized_facts or {}).get("source")
            or "arcgis"
        )
        usage.flush_collector(
            session, run=run, collector=provider_calls,
        )

        if geocode_result.errors or not geocode_result.normalized_facts:
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="geocoding",
                    status="failed",
                    message="We couldn't place this address on the map.",
                    source_id="arcgis_geocode",
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=1,
                ),
            )
            log.info(
                "site_check geocode returned no coordinates for run %s: %s",
                run.id, "; ".join(geocode_result.errors or []) or "no errors",
            )
            return _fail_run(
                session, run, error_code="geocode_failed",
                # failure_reason renders verbatim on the report screen —
                # never raw provider error text (§8.3 broker-safe rule).
                reason=_GEOCODE_FAILED_REASON,
                job_id=job_id,
                lease_token=lease_token,
            )

        lat = geocode_result.normalized_facts.get("lat")
        lng = geocode_result.normalized_facts.get("lng")
        explicit_county = _normalize_county(run.county)
        county = _resolve_county(
            explicit=run.county,
            geocode_county=geocode_result.normalized_facts.get("county"),
            address=run.address,
        )
        run.lat = lat
        run.lng = lng
        run.county = county or ""
        run.geocode_source = geocode_provider
        identity = PropertyIdentity(
            address=run.address,
            lat=lat, lng=lng,
            county=county,
            parcel_id=run.parcel_id,
            identifier_resolution_log=[{
                "field": "county",
                "source": (
                    "user" if explicit_county
                    else "geocode_or_address_inference"
                ),
                "value": county,
            }],
        )
        # Persist the geocode result on the run too.
        geocode_payload = _adapter_result_payload(
            geocode_result,
            refresh_status="fetched",
        )
        run.adapter_results = {
            **(run.adapter_results or {}),
            "arcgis_geocode": geocode_payload,
        }
        completed_sources += 1
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="geocoding",
                status="complete",
                message="Address located. Checking public data sources.",
                source_id="arcgis_geocode",
                completed_sources=completed_sources,
                total_sources=total_sources,
            ),
        )

    # ── 1b. Authoritative-anchor reconciliation ──────────────
    # Before any jurisdiction / spatial adapter runs, make sure the anchor
    # actually sits on the supplied parcel. A wrong-region geocode
    # (FM 971 → FM 1971 Panola, 2026-07-21) is re-anchored on the CAD
    # parcel centroid here so Wave 0.5 + every wave below query the real
    # property, not a point 350km away. No-op without a parcel_id/polygon.
    if identity is not None:
        identity = _reconcile_anchor_with_parcel(run, identity, sess=sess)

    # ── 2. Iterate every registered adapter ──────────────────
    queried: list[dict] = list(run.sources_queried or [])
    failed: list[dict] = list(run.sources_failed or [])
    results: dict[str, dict] = dict(run.adapter_results or {})
    project_context = _build_project_context(session, run)

    # §7.5 checkpoint (b): Wave 0 (geocode) → Wave 0.5 boundary.
    cancelled = _honor_cancellation(
        session, run,
        job_id=job_id, lease_token=lease_token,
        results=results, queried=queried, failed=failed,
    )
    if cancelled is not None:
        return cancelled
    reused_source_ids = {
        str(entry.get("source"))
        for entry in queried
        if isinstance(entry, dict) and entry.get("status") == "reused_recent"
    }

    # ── Wave 0.5: jurisdiction resolver (hard prerequisite) ──
    # Authoritative city-limits / ETJ / unincorporated verdict BEFORE any
    # city-aware adapter runs, so zoning + entitlement stop guessing "partial".
    # Per docs/specs/jurisdiction_resolver.md (reviewer REVIEW_PASSED). Written to
    # adapter_results (audit) here + derived.jurisdiction (canonical) in
    # _build_derived. Excluded from the wave fan-out below like arcgis_geocode.
    if "jurisdiction_resolver" in source_ids and identity is not None:
        if "jurisdiction_resolver" not in reused_source_ids:
            try:
                # Required (not belt-and-braces): county GIS hosts are
                # not in the paid-upstream route table.
                with usage.attribute_to("arcgis", "jurisdiction_resolve"):
                    jr_result = _load_adapter_fetch("jurisdiction_resolver")(
                        identity, session=sess,
                    )
                jr_payload = _adapter_result_payload(
                    jr_result,
                    refresh_status=(
                        "fetched_failed" if jr_result.errors else "fetched"
                    ),
                )
                results["jurisdiction_resolver"] = jr_payload
            except Exception as exc:
                log.warning("jurisdiction_resolver failed: %s", exc)
            # Record the provider attempt only when it was actually called.
            queried.append({"source": "jurisdiction_resolver", "status": "fetched"})
            usage.flush_collector(
                session, run=run, collector=provider_calls,
            )
        completed_sources += 1
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="jurisdiction",
                status="complete",
                message=(
                    "Using recent jurisdiction data."
                    if "jurisdiction_resolver" in reused_source_ids
                    else "Worked out which city or county has authority here."
                ),
                source_id="jurisdiction_resolver",
                completed_sources=completed_sources,
                total_sources=total_sources,
            ),
        )

    # Apply identity mutations supplied by reused parcel/zoning results before
    # dispatching sources that consume those values.
    identity = _apply_identity_mutations(
        identity,
        completed_sources_this_wave=[
            source_id
            for source_id in ("tcad_parcel", "wcad_parcel", "austin_zoning")
            if source_id in reused_source_ids
        ],
        results=results,
    )

    # Remove every recent reusable source from provider dispatch. Reused rows
    # were already copied into results with their original fetched_at.
    for source_id in sorted(reused_source_ids - {
        "arcgis_geocode", "jurisdiction_resolver",
    }):
        if source_id not in source_ids:
            continue
        source_ids.remove(source_id)
        completed_sources += 1
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="sources",
                status="reused",
                message=f"Using recent {_source_label(source_id)} data.",
                source_id=source_id,
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )

    # ── Aerial imagery fallback reuse for legacy/direct runs ──
    # The orthoimage does not change between runs, so re-validating USGS on
    # every re-check is wasted work. The service normally seeds this via the
    # generic freshness-window contract. Direct/legacy calls retain this
    # fallback when the chip is still within the same TTL and anchored on the
    # same point; drop it from the dispatch list so neither path re-fetches
    # it. A fresh fetch happens on the first run, on anchor change, once the
    # cached chip ages past the TTL — and ALWAYS on mode="fresh"
    # (force_refresh, site_check.md §9.1).
    force_refresh = str(getattr(run, "mode", "") or "") == "fresh"
    reused_imagery = _reusable_imagery_from_prior_runs(
        session, run, identity, force_refresh=force_refresh,
    )
    if reused_imagery is not None and _AERIAL_SOURCE_ID in source_ids:
        results[_AERIAL_SOURCE_ID] = reused_imagery
        source_ids = [s for s in source_ids if s != _AERIAL_SOURCE_ID]
        queried.append({"source": _AERIAL_SOURCE_ID, "status": "reused_cached"})
        completed_sources += 1

    settings_obj = get_settings()
    if settings_obj.public_intel_parallel_enabled:
        # ── 2a. PARALLEL PATH (4 waves) ──
        # Per docs/specs/public_intel_parallel_fetch.md. Wave 0 is
        # geocode (done above). Wave 1 fans out parcel + all
        # truly-independent adapters. The post-parcel wave runs zoning with
        # verified parcel geometry when available. Wave 2 runs adapters that
        # consume austin_zoning's identity mutation. Wave 3 runs tax_intel
        # (depends on district_overlays via _build_adapter_config).
        if not any(
            isinstance(entry, dict) and entry.get("source") == "arcgis_geocode"
            for entry in queried
        ):
            queried.append({"source": "arcgis_geocode", "status": "fetched"})

        (
            wave_1_sources,
            post_parcel_sources,
            wave_2_sources,
            wave_3_sources,
        ) = _source_waves(source_ids)

        max_workers = settings_obj.public_intel_max_workers
        caps = dict(
            settings_obj.public_intel_provider_concurrency_caps or {},
        )
        semaphores: dict[str, threading.Semaphore] = {}

        for wave_sources in (
            wave_1_sources,
            post_parcel_sources,
            wave_2_sources,
            wave_3_sources,
        ):
            # §7.5 checkpoint (b): every wave boundary. In-flight adapters
            # of the previous wave finished; later waves don't start.
            cancelled = _honor_cancellation(
                session, run,
                job_id=job_id, lease_token=lease_token,
                results=results, queried=queried, failed=failed,
            )
            if cancelled is not None:
                return cancelled
            # §7.4 abandon checkpoint: stop BEFORE the next wave's provider
            # calls if the lease was taken from us (recovery / supersede).
            assert_lease(session, job_id, lease_token)
            completed_sources, identity = _run_parallel_wave(
                session,
                run=run,
                provider_calls=provider_calls,
                wave_sources=wave_sources,
                identity=identity,
                sess=sess,
                results=results,
                queried=queried,
                failed=failed,
                project_context=project_context,
                completed_sources=completed_sources,
                total_sources=total_sources,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                semaphores=semaphores,
                max_workers=max_workers,
                caps=caps,
            )
            # …and again after the wave, so a lease lost while the wave ran
            # abandons instead of starting the next one.
            assert_lease(session, job_id, lease_token)

        # Skip the legacy sequential loop below — the parallel path
        # produced identical results + queried + failed bookkeeping.
        source_ids_to_iterate: list[str] = []
    else:
        # ── 2b. SEQUENTIAL PATH (legacy) ──
        # Untouched for-loop. Active until the parallel feature flag
        # ships ON by default.
        (
            wave_1_sources,
            post_parcel_sources,
            wave_2_sources,
            wave_3_sources,
        ) = _source_waves(source_ids)
        source_ids_to_iterate = [
            *wave_1_sources,
            *post_parcel_sources,
            *wave_2_sources,
            *wave_3_sources,
        ]

    # §7.5 checkpoint (b) markers for the sequential path: the first
    # source of each wave is a wave boundary.
    wave_boundary_sources = {
        wave[0]
        for wave in (
            wave_1_sources, post_parcel_sources,
            wave_2_sources, wave_3_sources,
        )
        if wave
    }

    for source_id in source_ids_to_iterate:
        if source_id in wave_boundary_sources:
            cancelled = _honor_cancellation(
                session, run,
                job_id=job_id, lease_token=lease_token,
                results=results, queried=queried, failed=failed,
            )
            if cancelled is not None:
                return cancelled
            assert_lease(session, job_id, lease_token)
        if source_id in ("arcgis_geocode", "jurisdiction_resolver"):
            # Already done above as hard prerequisites (geocode + Wave 0.5).
            queried.append({"source": source_id})
            continue

        reg = registry.get(source_id)

        if not reg.active_in_v0:
            # Wired-inactive — short-circuit. NO provider call
            # happens, NO usage row written.
            label = _source_label(source_id)
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status="running",
                    message=f"Checking {label}.",
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )
            results[source_id] = _adapter_result_payload(
                stub_inactive_result(source_id),
            )
            queried.append({"source": source_id, "status": "not_yet_wired"})
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status="skipped",
                    message=f"{label} is not wired yet; skipped.",
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )
            continue

        label = _source_label(source_id)
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="sources",
                status="running",
                message=f"Checking {label}.",
                source_id=source_id,
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )

        # Active adapter dispatch. Per-adapter config carries
        # context that depends on a prior adapter's result —
        # tax_intel, for example, needs the list of MUD/WCID/
        # ISD names that district_overlays surfaced.
        adapter_config = _build_adapter_config(
            source_id,
            results,
            project_context=project_context,
        )
        try:
            fetch_fn = _load_adapter_fetch(source_id)
            with usage.attribute_to(reg.provider, reg.service):
                result = fetch_fn(
                    identity, session=sess, config=adapter_config,
                )
        except AdapterInactiveError:
            # Bug — registry says active but the file is a stub.
            log.error(
                "registry-stub mismatch on %r — fix the activation matrix",
                source_id,
            )
            results[source_id] = _adapter_result_payload(
                stub_inactive_result(source_id),
            )
            failed.append({
                "source": source_id,
                "error": "registry-stub mismatch",
            })
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status="failed",
                    message=f"{label} could not be checked.",
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )
            continue
        except Exception as exc:
            log.warning(
                "adapter %r crashed: %s", source_id, exc,
            )
            failed_result = _adapter_result_payload(
                AdapterResult(
                    source_id=source_id,
                    source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
                    data_confidence_status=DataConfidenceStatus.UNKNOWN,
                    coverage_status=CoverageStatus.PARTIAL,
                    normalized_facts={},
                    errors=[f"adapter crashed: {exc}"],
                    fetched_at=datetime.now(UTC),
                ),
                refresh_status="fetched_failed",
            )
            results[source_id] = failed_result
            failed.append({"source": source_id, "error": str(exc)})
            usage.flush_collector(
                session, run=run, collector=provider_calls,
            )
            completed_sources += 1
            _emit_source_progress(
                session,
                job_id=job_id,
                lease_token=lease_token,
                analysis_progress_callback=analysis_progress_callback,
                progress=_source_progress(
                    stage="sources",
                    status="failed",
                    message=f"{label} failed; continuing with other sources.",
                    source_id=source_id,
                    completed_sources=completed_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                ),
            )
            continue

        # Successful adapter call (may still be a soft-fail
        # AdapterResult with errors populated; that's fine).
        result_payload = _adapter_result_payload(
            result,
            refresh_status=(
                "fetched_failed" if result.errors else "fetched"
            ),
        )
        results[source_id] = result_payload
        queried.append({
            "source": source_id,
            "status": result_payload["refresh_status"],
        })
        if result.errors:
            failed.append({
                "source": source_id,
                "error": "; ".join(result.errors),
            })
        usage.flush_collector(
            session, run=run, collector=provider_calls,
        )
        completed_sources += 1
        terminal_status = "complete"
        terminal_message = f"Finished {label}."
        if result.errors:
            terminal_status = "failed"
            terminal_message = (
                f"{label} returned issues; continuing with other sources."
            )
        elif result.coverage_status in {
            CoverageStatus.NOT_YET_WIRED,
            CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        }:
            terminal_status = "skipped"
            terminal_message = f"{label} is not supported here; skipped."
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="sources",
                status=terminal_status,
                message=terminal_message,
                source_id=source_id,
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )

        if source_id in ("tcad_parcel", "wcad_parcel"):
            parcel_facts = result.normalized_facts or {}
            updates: dict[str, Any] = {}
            if parcel_facts.get("parcel_id"):
                updates["parcel_id"] = parcel_facts.get("parcel_id")
            acreage = (
                parcel_facts.get("acreage")
                or parcel_facts.get("acres")
            )
            if acreage:
                updates["acreage"] = acreage
            if parcel_facts.get("parcel_polygon"):
                updates["parcel_geometry"] = parcel_facts.get(
                    "parcel_polygon",
                )
            if updates:
                identity = identity.model_copy(update=updates)

        # After austin_zoning resolves, thread its jurisdiction
        # back into the identity so downstream city-aware
        # adapters (future_land_use, district_overlays,
        # zoning_standards, entitlement_pipeline) can dispatch
        # to the right city layer. legacy_services does the same
        # upstream of FLU; matching the suffix-stripping logic
        # is required — "Austin ETJ" must collapse to "Austin"
        # so the per-city layer dispatch finds it (per
        # legacy_services/core/public_intel.py lines 594-602).
        if source_id == "austin_zoning":
            j = (
                (result.normalized_facts or {})
                .get("jurisdiction")
            )
            if isinstance(j, str) and j.strip():
                clean = (
                    j.replace(" ETJ", "")
                     .replace(" (unincorporated)", "")
                     .strip()
                )
                if clean:
                    identity = identity.model_copy(update={
                        "jurisdiction_candidates": [
                            *identity.jurisdiction_candidates,
                            clean,
                        ],
                    })

    # `_refresh_if_terminal` calls `session.refresh(run)`, which discards
    # pending JSON assignments. Do the final cancellation/terminal check before
    # staging the JSON payloads for commit.
    terminal = _refresh_if_terminal(session, run)
    if terminal is not None:
        return terminal

    # §7.5 checkpoint (c): before final staging/commit.
    cancelled = _honor_cancellation(
        session, run,
        job_id=job_id, lease_token=lease_token,
        results=results, queried=queried, failed=failed,
    )
    if cancelled is not None:
        return cancelled

    run.adapter_results = deepcopy(results)
    run.sources_queried = deepcopy(queried)
    run.sources_failed = deepcopy(failed)
    derived = _build_derived(results)
    _emit_source_progress(
        session,
        job_id=job_id,
        lease_token=lease_token,
        analysis_progress_callback=analysis_progress_callback,
        progress=_source_progress(
            stage="summarizing",
            status="running",
            message="Preparing broker summary cards.",
            completed_sources=completed_sources,
            total_sources=total_sources,
            failed_sources_count=len(failed),
            failed_sources=failed,
        ),
    )
    # AI enrichment SEVERANCE (site_check.md §3.2): the the reference app plat /
    # zoning / FLU document reads are Phase B; the Anthropic card-narrative
    # call is replaced by the PropDesk OpenAI narrative builder below
    # (site_check.md §11), called at the same point in the flow as the reference app's
    # build_card_narratives (pinned orchestrator line 1835). The
    # historical-aerials build (pinned orchestrator line 1841) is RESTORED
    # here (§19) with its AI half rebuilt on OpenAI vision.
    run.adapter_results = deepcopy(results)

    # "What this land was before" (§19) — post-staging enrichment: the era
    # aerial ladder plus one metered vision read. Settings-gated, non-fatal,
    # and preceded by a lease-fenced progress write so a worker that already
    # lost its lease stops BEFORE the provider calls (§7.4), exactly like
    # every other checkpoint.
    if getattr(settings_obj, "historical_aerials_enabled", True):
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="historical_aerials",
                status="running",
                message="Looking at older photos of this land.",
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )
        try:
            derived.update(
                build_historical_aerials(session, run, settings_obj) or {}
            )
        except Exception:
            # LAST-RESORT guard: the builder owns its own degradation ladder;
            # if it still raises, the report keeps every other card and NEVER
            # fails for a historical-photo problem.
            log.exception(
                "historical aerials builder crashed for run_id=%s — "
                "continuing without that card", run.id,
            )
            derived.setdefault("historical_aerials_meta", {
                "status": "fallback_provider_error",
                "model": settings_obj.site_check_vision_model,
                "error_kind": "historical_aerials_builder_crashed",
            })

    # "What is being built near here" (§20.28) — post-staging enrichment: one
    # metered web-search read of the city's own planning pages, with every
    # returned case re-fetched and verified before it may render. Settings-
    # gated, non-fatal, and preceded by a lease-fenced progress write so a
    # worker that already lost its lease stops BEFORE the provider calls.
    if getattr(settings_obj, "entitlement_read_enabled", True):
        _emit_source_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            analysis_progress_callback=analysis_progress_callback,
            progress=_source_progress(
                stage="entitlement_read",
                status="running",
                message="Reading the city's planning pages.",
                completed_sources=completed_sources,
                total_sources=total_sources,
                failed_sources_count=len(failed),
                failed_sources=failed,
            ),
        )
        try:
            derived.update(
                build_entitlement_reads(
                    session, run, results, settings_obj,
                    # The run's own hardened session: the validator's
                    # source fetches get the timeout floor and the §8.6
                    # provider-call collector like every adapter request,
                    # and an offline test's replay session reaches them
                    # instead of a socket.
                    http=sess,
                ) or {}
            )
            # The builder writes its verified cases into `results`, which was
            # already copied onto the run above — re-assign so the card sees
            # them.
            run.adapter_results = deepcopy(results)
        except Exception:
            # LAST-RESORT guard: the builder owns its own degradation ladder;
            # if it still raises, the card keeps every deterministic county
            # plat record and the run NEVER fails for a reading problem.
            log.exception(
                "entitlement reader crashed for run_id=%s — continuing with "
                "the county records only", run.id,
            )
            derived.setdefault("entitlement_read_meta", {
                "status": "fallback_provider_error",
                "model": getattr(settings_obj, "entitlement_read_model", ""),
                "error_kind": "entitlement_reader_crashed",
            })

    if build_narratives is not None:
        try:
            derived.update(
                build_narratives(session, run, results, settings_obj) or {}
            )
        except Exception:
            # LAST-RESORT guard (§11.3): the narratives module owns its
            # own degradation ladder; if it still raises, the run stays
            # deterministic-only and NEVER fails for a narrative problem.
            log.exception(
                "narrative builder crashed for run_id=%s — "
                "degrading to deterministic cards", run.id,
            )
            derived.setdefault("narrative_meta", {
                "status": "fallback_provider_error",
                "model": settings_obj.site_check_narrative_model,
                "error_kind": "narrative_builder_crashed",
            })
    run.sources_queried = deepcopy(queried)
    run.sources_failed = deepcopy(failed)
    run.derived = deepcopy(derived)
    # A metered narrative call above may COMMIT its usage row, which
    # EXPIRES this run instance; flag_modified raises on an expired,
    # never-reloaded attribute (the reference app live failure 2026-07-22: every
    # refresh crashed right here after an AI call, with all adapter work
    # already saved). Touching the attribute first loads it back into the
    # instance state, making the flag safe regardless of what committed
    # in between.
    for _attr in ("adapter_results", "sources_queried",
                  "sources_failed", "derived"):
        getattr(run, _attr)
        flag_modified(run, _attr)
    # Anchor classification (per reviewer 2026-05-05 directive).
    # `parcel` requires the parcel adapter (tcad_parcel /
    # wcad_parcel) to have actually returned a polygon
    # geometry — TCAD removed live API May 2026; WCAD now
    # supplies polygons for exact Williamson matches. Where no
    # parcel polygon is available, the run remains geocode-
    # anchored / provisional and the UI shows that banner.
    run.anchor_kind, run.anchor_quality = _classify_anchor(
        run, results,
    )

    # Zoning Navigator packet build SEVERED for Phase A
    # (site_check.md §3.2 — pinned orchestrator 1880–1900 / 2067–2074 /
    # 2089–2094 and the official_zoning_records merges at 2130/2183 are
    # a Phase C surface).

    # Gated Self-Improvement Loop (spec ACCEPTED 2026-05-20) —
    # Phase 1 pilot: missing Texas county parcel adapter. After
    # all adapters have run, if the identity's county is in
    # Texas (the v1 scope) but is NOT one of {travis, williamson}
    # (the two counties with wired parcel adapters), record a
    # coverage gap so the operator sees the demand signal in
    # the admin queue. Per-customer-deduped via the service.
    _maybe_record_parcel_coverage_gap(
        session, run=run, identity=identity,
    )

    # §8.5 terminal race (finding 2): completion and cancellation serialize
    # on the run row. Whichever transaction commits first wins — if a cancel
    # got the lock first, its flag is visible here and this worker honors the
    # cancellation instead of completing; if another actor already
    # terminalized the run, this worker abandons and mutates nothing.
    locked = _lock_run_row(session, run.id)
    if locked is None:
        return _abandon(session, run, "run disappeared at final staging")
    if locked.status in TERMINAL_PUBLIC_INTEL_STATUSES:
        return _abandon(session, run, "run already terminal at final staging")
    if locked.cancel_requested_at is not None:
        # Cancel won the race. §7.5: an honored cancellation has exactly ONE
        # terminal outcome — `cancelled`, with this run's staged results.
        cancelled = _honor_cancellation(
            session, run,
            job_id=job_id, lease_token=lease_token,
            results=results, queried=queried, failed=failed,
        )
        if cancelled is not None:
            return cancelled

    # §7.5 pre-commit fence (same transitive pattern as the cancel
    # checkpoint): terminalize the JOB with a lease-guarded UPDATE in
    # the SAME transaction as the run staging. If the lease was
    # invalidated (supersede / stale recovery), it matches 0 rows and
    # this worker abandons — a superseded-cancelled run can never be
    # overwritten back to completed (live defect 2026-08-01). The terminal
    # PROGRESS rides this same guarded UPDATE, so the detail poll never sees
    # `stage=summarizing,status=running` against a succeeded job (finding 3).
    if job_id and lease_token:
        fenced = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == lease_token,
                Job.status == "running",
            )
            .values(
                status="succeeded",
                finished_at=func.now(),
                progress=_progress_payload(_source_progress(
                    stage="completed",
                    status="complete",
                    message="Public Data fetch is complete.",
                    completed_sources=total_sources,
                    total_sources=total_sources,
                    failed_sources_count=len(failed),
                    failed_sources=failed,
                )),
            )
        )
        if fenced.rowcount != 1:
            session.rollback()
            raise LeaseLost(job_id)
    run.status = "completed"
    run.error_code = None
    run.failure_reason = None
    run.finished_at = datetime.now(UTC)
    session.add(run)

    # Run-completion audit row (site_check.md §8.7) — worker-side
    # terminal transition, requested_by carried from the run. The
    # `detail` JSON carries the rollup summary so the audit log can
    # answer "what did this lookup cost / which sources contributed?"
    # without joining through site_check_provider_usage.
    cost_total = usage.run_total_cost_usd(
        session, run_id=run.id,
    )
    per_provider = usage.per_provider_breakdown(
        session, run_id=run.id,
    )
    audit.log(
        session,
        tenant_id=run.tenant_id,
        user_id=run.requested_by,
        action="site_check.run_completed",
        entity_type="site_check_run",
        entity_id=run.id,
        detail={
            "address": run.address,
            "lat": run.lat,
            "lng": run.lng,
            "adapters_active": [
                sid for sid, reg in registry.REGISTRY.items()
                if reg.active_in_v0
            ],
            "sources_failed_count": len(failed),
            "estimated_cost_usd": round(cost_total, 4),
            "per_provider": {
                k: round(v, 4) for k, v in per_provider.items()
            },
        },
    )

    session.commit()
    session.refresh(run)
    return run


def _identity_for_source_retry(
    run: SiteCheckRun,
    results: dict[str, dict],
) -> PropertyIdentity:
    county = _resolve_county(
        explicit=run.county,
        geocode_county=None,
        address=run.address,
    )
    identity = PropertyIdentity(
        address=run.address,
        lat=run.lat,
        lng=run.lng,
        county=county,
        parcel_id=run.parcel_id,
    )
    return _apply_identity_mutations(
        identity,
        completed_sources_this_wave=list(results.keys()),
        results=results,
    )


def _drop_failed_entries_for_source(
    failed: list[dict],
    source_id: str,
) -> list[dict]:
    return [
        entry for entry in failed
        if not isinstance(entry, dict) or entry.get("source") != source_id
    ]


def _preserve_nonstale_narratives(
    *,
    existing_derived: dict[str, Any],
    derived: dict[str, Any],
    retried_source_id: str | None = None,
    retried_source_ids: list[str] | None = None,
) -> None:
    """Keep every narrative EXCEPT the retried sources' (ported
    `partially_preserved_after_source_retry`). One retry job can carry
    several sources, so the whole list is dropped in one pass."""
    retried = list(retried_source_ids or [])
    if retried_source_id is not None and retried_source_id not in retried:
        retried.append(retried_source_id)
    narratives = existing_derived.get("card_narratives")
    if isinstance(narratives, dict):
        kept = {
            key: value for key, value in narratives.items()
            if key not in retried
        }
        if kept:
            derived["card_narratives"] = kept
    meta = existing_derived.get("card_narratives_meta")
    if isinstance(meta, dict):
        derived["card_narratives_meta"] = {
            **meta,
            "status": "partially_preserved_after_source_retry",
            "retried_source_id": retried[-1] if retried else None,
            "retried_source_ids": retried,
        }


def run_retry_source(
    session: Session,
    *,
    run_id: str,
    source_id: str,
    job_id: str | None = None,
    lease_token: str | None = None,
    sess: requests.Session | None = None,
) -> SiteCheckRun:
    """Single-source convenience wrapper over `run_retry_sources`."""
    return run_retry_sources(
        session,
        run_id=run_id,
        source_ids=[source_id],
        job_id=job_id,
        lease_token=lease_token,
        sess=sess,
    )


def run_retry_sources(
    session: Session,
    *,
    run_id: str,
    source_ids: list[str],
    job_id: str | None = None,
    lease_token: str | None = None,
    sess: requests.Session | None = None,
) -> SiteCheckRun:
    """Retry a LIST of adapters on an existing completed SiteCheckRun.

    ONE retry job owns its COMPLETE source list under ONE lease (§8.4,
    finding 3): every source stages under the same running fence and the job
    is terminalized EXACTLY ONCE, after the last source — so a later source
    can never find the fence already terminal and roll its result back.
    Terminal progress rides that terminal transaction, so the detail poll
    never shows a running-looking stage against a succeeded job.

    It does not clear the run, geocode again, or fan out unrelated adapters.
    Each retried source's result is replaced, the failed-source list is
    updated, and derived facts are rebuilt from the merged adapter_results.
    """
    try:
        return _retry_sources(
            session,
            run_id=run_id,
            source_ids=source_ids,
            job_id=job_id,
            lease_token=lease_token,
            sess=sess,
        )
    except LeaseLost:
        session.rollback()
        session.expire_all()
        log.info(
            "site_check retry abandoned (lease lost) run_id=%s job_id=%s",
            run_id, job_id,
        )
        return session.get(SiteCheckRun, run_id, populate_existing=True)


def _retry_sources(
    session: Session,
    *,
    run_id: str,
    source_ids: list[str],
    job_id: str | None = None,
    lease_token: str | None = None,
    sess: requests.Session | None = None,
) -> SiteCheckRun:
    run = session.get(SiteCheckRun, run_id)
    if run is None:
        raise ValueError(f"SiteCheckRun {run_id} not found")

    wanted = [str(s) for s in (source_ids or []) if s]
    if not wanted:
        raise ValueError("a source retry needs at least one source_id")
    for source_id in wanted:
        if source_id == "arcgis_geocode":
            raise ValueError("arcgis_geocode is a full-run prerequisite")
        if source_id not in registry.REGISTRY:
            raise ValueError(f"unknown public-intel source_id {source_id!r}")
        if not registry.get(source_id).active_in_v0:
            raise ValueError(f"source {source_id!r} is not active")

    provider_calls = usage.ProviderCallCollector()
    sess = http_session(sess, collector=provider_calls)
    results: dict[str, dict] = dict(run.adapter_results or {})
    queried = list(run.sources_queried or [])
    failed = list(run.sources_failed or [])
    existing_derived = dict(run.derived or {})
    project_context = _build_project_context(session, run)
    retried: list[str] = []
    still_failing: list[str] = []

    for index, source_id in enumerate(wanted, start=1):
        # §7.4 abandon checkpoint: stop BEFORE this source's provider call
        # if the lease is no longer ours.
        assert_lease(session, job_id, lease_token)
        reg = registry.get(source_id)
        label = _source_label(source_id)
        _write_public_intel_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            progress={
                "stage": "source_retry",
                "status": "running",
                "message": f"Refreshing {label}.",
                "current_source": source_id,
                "current_source_label": label,
                "retry_source_ids": wanted,
                "completed_sources": index - 1,
                "total_sources": len(wanted),
                "failed_sources_count": len(still_failing),
            },
        )

        failed = _drop_failed_entries_for_source(failed, source_id)
        identity = _identity_for_source_retry(run, results)
        retried_at = datetime.now(UTC).isoformat()
        previous_result = results.get(source_id) or {}
        adapter_config = _build_adapter_config(
            source_id,
            results,
            project_context=project_context,
        )
        try:
            fetch_fn = _load_adapter_fetch(source_id)
            with usage.attribute_to(reg.provider, reg.service):
                result = fetch_fn(
                    identity, session=sess, config=adapter_config,
                )
        except AdapterInactiveError as exc:
            result = stub_inactive_result(source_id)
            result.errors = [f"registry-stub mismatch: {exc}"]
        except Exception as exc:
            log.warning("source retry adapter %r crashed: %s", source_id, exc)
            result = AdapterResult(
                source_id=source_id,
                source_access_status=(
                    SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
                ),
                data_confidence_status=DataConfidenceStatus.UNKNOWN,
                coverage_status=CoverageStatus.PARTIAL,
                normalized_facts={},
                errors=[f"adapter crashed: {exc}"],
                fetched_at=datetime.now(UTC),
            )

        results[source_id] = _adapter_result_payload(result)
        queried.append({
            "source": source_id,
            "status": "retried",
            "retried_at": retried_at,
        })
        if result.errors:
            failed.append({
                "source": source_id,
                "error": "; ".join(result.errors),
                "retried_at": retried_at,
            })
            still_failing.append(source_id)
        retried.append(source_id)

        usage.flush_collector(
            session, run=run, collector=provider_calls,
        )

        derived = _build_derived(results)
        # plats_intel AI read SEVERED for Phase A (site_check.md §3.2 —
        # pinned orchestrator line 2353; plat_reader.py is a Phase B module).
        _preserve_nonstale_narratives(
            existing_derived=existing_derived,
            derived=derived,
            retried_source_ids=retried,
        )

        run.adapter_results = deepcopy(results)
        run.sources_queried = deepcopy(queried)
        run.sources_failed = deepcopy(failed)
        run.derived = deepcopy(derived)
        # A metered call above may COMMIT its usage row, which EXPIRES this
        # run instance; flag_modified raises on an expired, never-reloaded
        # attribute (the reference app live failure 2026-07-22: every refresh crashed
        # right here after an AI call, with all adapter work already saved).
        # Touching the attribute first loads it back into the instance
        # state, making the flag safe regardless of what committed in
        # between.
        for _attr in ("adapter_results", "sources_queried",
                      "sources_failed", "derived"):
            getattr(run, _attr)
            flag_modified(run, _attr)
        run.anchor_kind, run.anchor_quality = _classify_anchor(run, results)
        run.error_code = None
        run.failure_reason = None
        session.add(run)

        # Worker-side retry-completion audit row, one per retried source
        # (mirrors the ported behavior; the §8.4 accepted-request
        # `site_check.run_retry` row is written by the API layer).
        audit.log(
            session,
            tenant_id=run.tenant_id,
            user_id=run.requested_by,
            action="site_check.run_retry_completed",
            entity_type="site_check_run",
            entity_id=run.id,
            detail={
                "retried_source_id": source_id,
                "retried_source_ids": wanted,
                "had_errors": bool(
                    isinstance(previous_result, dict)
                    and previous_result.get("errors")
                ),
                "source_failed": bool(result.errors),
                "sources_failed_count": len(failed),
            },
        )

        # Fenced per-source checkpoint: THIS source's staged unit — result,
        # usage row, audit row — commits together with the guarded job
        # UPDATE, or the whole unit rolls back and the retry abandons. The
        # job stays `running`: only the terminal step below ends it.
        _write_public_intel_progress(
            session,
            job_id=job_id,
            lease_token=lease_token,
            progress={
                "stage": "source_retry",
                "status": "running",
                "message": (
                    f"{label} is still unavailable."
                    if result.errors else f"{label} refreshed."
                ),
                "current_source": source_id,
                "current_source_label": label,
                "retry_source_ids": wanted,
                "completed_sources": index,
                "total_sources": len(wanted),
                "failed_sources_count": len(still_failing),
            },
        )

    # ── ONE terminal transition for the WHOLE list (finding 3) ──────────
    # The job goes `succeeded` exactly once, after the last source, and its
    # TERMINAL progress rides the same guarded UPDATE. A stale-recovered
    # retry worker whose lease was invalidated matches 0 rows: the whole
    # staged unit rolls back and it abandons.
    last_label = _source_label(retried[-1])
    if len(retried) == 1:
        terminal_message = (
            f"{last_label} is still unavailable."
            if still_failing else f"{last_label} refreshed."
        )
    elif still_failing:
        terminal_message = (
            f"Refreshed {len(retried) - len(still_failing)} of "
            f"{len(retried)} sources; {len(still_failing)} still unavailable."
        )
    else:
        terminal_message = f"Refreshed {len(retried)} sources."
    terminal_progress = _progress_payload({
        "stage": "source_retry",
        "status": "failed" if still_failing else "complete",
        "message": terminal_message,
        "current_source": retried[-1],
        "current_source_label": last_label,
        "retry_source_ids": wanted,
        "completed_sources": len(retried),
        "total_sources": len(wanted),
        "failed_sources_count": len(still_failing),
    })
    if job_id and lease_token:
        fenced = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == lease_token,
                Job.status == "running",
            )
            .values(
                status="succeeded",
                finished_at=func.now(),
                progress=terminal_progress,
            )
        )
        if fenced.rowcount != 1:
            session.rollback()
            raise LeaseLost(job_id)

    session.commit()
    session.refresh(run)
    return run


# ── Failure helper ────────────────────────────────────────────────


def _fail_run(
    session: Session,
    run: SiteCheckRun,
    *,
    error_code: str,
    reason: str,
    job_id: str | None = None,
    lease_token: str | None = None,
) -> SiteCheckRun:
    """Mark a run as failed. Prior completed runs are untouched rows —
    a failed run never destroys last-known-good facts (site_check.md
    §9.2)."""
    terminal = _refresh_if_terminal(session, run)
    if terminal is not None:
        return terminal
    # §8.5 terminal race (finding 2): serialize on the run row like every
    # other terminal transition.
    locked = _lock_run_row(session, run.id)
    if locked is None:
        return _abandon(session, run, "run disappeared at failure staging")
    if locked.status in TERMINAL_PUBLIC_INTEL_STATUSES:
        return _abandon(session, run, "run already terminal at failure staging")
    if locked.cancel_requested_at is not None:
        # §7.5: an honored cancellation has exactly ONE terminal outcome —
        # `cancelled`, never `failed`.
        cancelled = _honor_cancellation(
            session, run,
            job_id=job_id, lease_token=lease_token,
            results=dict(run.adapter_results or {}),
            queried=list(run.sources_queried or []),
            failed=list(run.sources_failed or []),
        )
        if cancelled is not None:
            return cancelled
    # Same §7.5 pre-commit fence as success staging: a worker whose
    # lease was invalidated must not fail a run another actor already
    # terminalized (job → failed + terminal progress ride the same
    # transaction).
    if job_id and lease_token:
        fenced = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_token == lease_token,
                Job.status == "running",
            )
            .values(
                status="failed",
                error_code=error_code,
                finished_at=func.now(),
                progress=_progress_payload({
                    "stage": "failed",
                    "status": "failed",
                    "message": "Public Data fetch failed.",
                    "error_code": error_code,
                    "error": reason[:2000],
                }),
            )
        )
        if fenced.rowcount != 1:
            session.rollback()
            raise LeaseLost(job_id)
    run.status = "failed"
    run.error_code = error_code
    run.failure_reason = reason
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


# ── Derived facts builder ─────────────────────────────────────────


def _build_derived(results: dict[str, dict]) -> dict[str, Any]:
    """Pull the consumer-facing fields out of per-adapter
    AdapterResults into the `derived` JSON shape downstream
    consumers (Site Analysis, Quick / Deep / Portfolio, Q&A)
    read.

    Per spec §7.3 — `derived` holds OUR normalized facts, not
    provider-proprietary records. ArcGIS Place records in
    particular are NEVER copied verbatim into `derived`.

    v0 implementation is intentionally thin — each active
    adapter contributes its own normalized_facts subset; as
    later adapters land they extend this map.
    """
    derived: dict[str, Any] = {}

    for source_id, result in results.items():
        facts = (result or {}).get("normalized_facts") or {}
        coverage = (result or {}).get("coverage_status")
        if coverage in {
            CoverageStatus.NOT_YET_WIRED.value,
            CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION.value,
        }:
            # Honest signal — record nothing in derived; the
            # adapter_results blob retains the not_yet_wired
            # marker for downstream consumers that want it.
            continue

        derived[source_id] = facts

    # Canonical jurisdiction verdict read path (jurisdiction_resolver spec,
    # reviewer contract #1): consumers read derived.jurisdiction; the full audit
    # stays at adapter_results["jurisdiction_resolver"].
    if "jurisdiction_resolver" in derived:
        derived["jurisdiction"] = derived["jurisdiction_resolver"]

    derived["manual_lookups_needed"] = _build_manual_lookups(results)

    # project_imagery — canonical consumer-facing slot (UI hero +
    # details Location section; future Site Analysis / Q&A reads).
    # Populated from the public_aerial_imagery adapter when it returned
    # a chip (activated 2026-05-31 per public_data.md §11.3); stays None
    # when there is no image (no lat/lng, or USGS unavailable) so the UI
    # hides the surface instead of showing a broken frame.
    derived["project_imagery"] = _project_imagery_from(
        derived.get("public_aerial_imagery"),
    )
    return derived


def _project_imagery_from(facts: Any) -> dict[str, Any] | None:
    """Compact, consumer-facing `project_imagery` lifted from the
    public_aerial_imagery adapter facts. None when no chip was produced
    (empty facts / missing image_url) — keeps the UI surface hidden."""
    if not isinstance(facts, dict):
        return None
    image_url = facts.get("image_url")
    if not isinstance(image_url, str) or not image_url:
        return None
    return {
        "image_url": image_url,
        "provider": facts.get("provider"),
        "source": facts.get("source"),
        "attribution": facts.get("attribution"),
        "caption": facts.get("caption"),
        "center": facts.get("center"),
        "bbox": facts.get("bbox"),
        "radius_m": facts.get("radius_m"),
        "is_field_verified": facts.get("is_field_verified", False),
    }


# ── Aerial imagery cross-run reuse fallback ─

_AERIAL_SOURCE_ID = "public_aerial_imagery"
_AERIAL_ANCHOR_EPS_DEG = 1e-4  # ~11 m — same property, not a re-geocode drift


def _parse_fetched_at(value: Any) -> datetime | None:
    """Parse a stored fetched_at — an ISO string after model_dump(json),
    or a datetime. Returns tz-aware UTC, or None when unparseable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _reusable_imagery_from_prior_runs(
    session: Session,
    run: SiteCheckRun,
    identity: PropertyIdentity | None,
    *,
    force_refresh: bool = False,
) -> dict | None:
    """The most recent completed prior run's aerial chip when it is still
    fresh (within min(site_check_freshness_days,
    public_aerial_imagery_ttl_days)) and anchored on the same point —
    else None.

    The orthoimage is static, so a re-check reuses it instead of
    re-validating USGS on every run. Freshness is measured from the ORIGINAL
    fetch (the reused result keeps its fetched_at), so the chip genuinely
    expires after its first capture, not after its last view.
    `force_refresh` (mode="fresh", site_check.md §9.1) skips reuse entirely.
    """
    if force_refresh:
        return None
    if identity is None or identity.lat is None or identity.lng is None:
        return None
    settings_obj = get_settings()
    freshness_days = int(
        getattr(settings_obj, "site_check_freshness_days", 180)
    )
    configured_days = int(
        getattr(settings_obj, "public_aerial_imagery_ttl_days", 100)
    )
    ttl_days = min(freshness_days, configured_days)
    if ttl_days <= 0:
        return None
    default_ttl_s = ttl_days * 86_400
    now = datetime.now(UTC)

    stmt = (
        select(SiteCheckRun)
        .where(
            # Tenant isolation: a chip is never reused across tenants even
            # though the imagery itself is public (site_check.md §9.2).
            SiteCheckRun.tenant_id == run.tenant_id,
            SiteCheckRun.normalized_address == run.normalized_address,
            SiteCheckRun.id != run.id,
            SiteCheckRun.status == "completed",
        )
        .order_by(SiteCheckRun.created_at.desc())
        .limit(5)
    )
    for prior in session.scalars(stmt).all():
        result = (prior.adapter_results or {}).get(_AERIAL_SOURCE_ID)
        if not isinstance(result, dict):
            continue
        facts = result.get("normalized_facts")
        if not isinstance(facts, dict) or not facts.get("image_url"):
            continue
        fetched_at = _parse_fetched_at(result.get("fetched_at"))
        if fetched_at is None:
            continue
        ttl_raw = result.get("ttl_seconds")
        source_ttl_s = (
            int(ttl_raw)
            if isinstance(ttl_raw, (int, float)) and ttl_raw > 0
            else default_ttl_s
        )
        ttl_s = min(default_ttl_s, source_ttl_s)
        if (now - fetched_at).total_seconds() > ttl_s:
            continue  # expired — keep scanning (older runs won't be fresher)
        center = facts.get("center")
        if not isinstance(center, dict):
            continue
        try:
            same_anchor = (
                abs(float(center["lat"]) - identity.lat) <= _AERIAL_ANCHOR_EPS_DEG
                and abs(float(center["lng"]) - identity.lng) <= _AERIAL_ANCHOR_EPS_DEG
            )
        except (KeyError, TypeError, ValueError):
            continue
        if same_anchor:
            return result
    return None


def _facts(results: dict[str, dict], source_id: str) -> dict[str, Any]:
    return (results.get(source_id) or {}).get("normalized_facts") or {}


def _resolve_county(
    *,
    explicit: str | None,
    geocode_county: object,
    address: str,
) -> str | None:
    explicit_clean = _normalize_county(explicit)
    if explicit_clean:
        return explicit_clean

    geocode_clean = _normalize_county(geocode_county)
    if geocode_clean:
        return geocode_clean

    # Last-resort UX fallback. This is not a legal boundary
    # engine; it prevents blank-county degradation when the
    # address contains strong local signals and the geocoder
    # omitted county attributes.
    addr = address.lower()
    if "williamson" in addr or any(
        token in addr
        for token in ("hutto", "round rock", "georgetown", "taylor")
    ):
        return "williamson"
    if "travis" in addr or any(
        token in addr
        for token in ("austin", "manor", "del valle", "pflugerville")
    ):
        return "travis"
    if "hays" in addr or any(
        token in addr
        for token in ("buda", "kyle", "dripping springs", "san marcos")
    ):
        return "hays"
    return None


def _normalize_county(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    cleaned = cleaned.removesuffix(" county")
    return cleaned or None


def _build_manual_lookups(results: dict[str, dict]) -> list[dict[str, str]]:
    """legacy_services-parity manual lookup cards.

    These are user-facing action cards, not provider facts. Keep
    the logic intentionally narrow: only the public-intel domains
    legacy_services already surfaced as manual follow-ups.
    """
    items: list[dict[str, str]] = []

    flood = _facts(results, "fema_nfhl")
    if flood.get("status") == "unknown":
        items.append({
            "item": "Flood zone - manual lookup",
            # Plain English only (§2/§14): no agency map jargon, no
            # acronyms. The link below is the ported pointer (§10.4).
            "why": (
                "FEMA's flood map didn't give a clear answer for this "
                "property. Search the address on FEMA's map site to see "
                "the flood zone yourself — or a flood determination "
                "company can confirm it for a small fee."
            ),
            "who_to_contact": "https://msc.fema.gov/portal/search",
            "estimated_cost": "Free",
            "priority": "HIGH",
        })

    # §20.28 #4: zoning / jurisdiction manual guidance is NOT duplicated here.
    # It now lives on the Zoning / Jurisdiction card (authoritative verdict from
    # jurisdiction_resolver + portal link + advisory) and the Zoning Standards
    # card (conflict / district). Repeating it as a "Manual Verification
    # Required" item was the redundant noise this item removed.

    traffic_counts = _facts(results, "txdot_aadt").get("traffic_counts") or []
    if not traffic_counts:
        items.append({
            "item": "Traffic counts - manual lookup",
            "why": (
                "No traffic count is published for the roads right at "
                "this property. Busier roads nearby usually have one — "
                "the state's traffic count map shows them road by road."
            ),
            "who_to_contact": (
                "https://www.txdot.gov/data-maps/traffic-count-maps.html"
            ),
            "estimated_cost": "Free",
            "priority": "MEDIUM",
        })

    projects = (
        _facts(results, "txdot_projects").get("items")
        or _facts(results, "txdot_projects").get("projects")
        or []
    )
    project_errors = results.get("txdot_projects", {}).get("errors") or []
    if not projects and project_errors:
        items.append({
            "item": "Road projects - fetch failed",
            "why": (
                "We couldn't reach the state's list of road projects "
                "this time. Run the lookup again later, or open the "
                "project tracker to see whether any road work is "
                "planned nearby."
            ),
            "who_to_contact": (
                "https://apps3.txdot.gov/apps-cq/project_tracker/"
            ),
            "estimated_cost": "Free",
            "priority": "MEDIUM",
        })

    return items


__all__ = ["run_collect", "run_retry_source", "run_retry_sources"]
