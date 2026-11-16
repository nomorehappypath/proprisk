"""Public Data (internal codename site-check) endpoints — spec
docs/specs/site_check.md §8.

Contracts: every route is tenant-filtered and member-accessible; cross-
tenant ids answer 404 both directions on EVERY endpoint. Error bodies are
the house `{"error": "<plain English>"}` envelope. Every ACCEPTED create /
cancel / retry / delete writes exactly one user-attributed audit row in
the same transaction (§8.7); rejected requests and idempotent repeats
write none.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import NOT_FOUND, current_user, plain_error
from app.config.settings import get_settings
from app.db import get_db
from app.models import Job, SiteCheckRun, User
from app.services import audit
from app.services.site_check import service as sc_service

# Importing the worker handler registers the site_check kind's handler +
# recovery reconciler with the queue (§7.6 read-path reconciliation needs
# the reconciler in the API process too).
from app.services.site_check import worker_handler as _worker_handler  # noqa: F401
from app.services.site_check.map_context import build_map_context
from app.services.site_check.map_contract import SiteCheckMapContext

log = logging.getLogger("propdesk.site_check.api")

router = APIRouter(prefix="/site-check", tags=["public-data"])

ALREADY_FINISHED = "This lookup already finished."
CANCEL_FIRST = "Cancel the running lookup first."
_ACTIVE = ("queued", "running")
MAP_STILL_RUNNING = "This lookup is still running — the map opens once it finishes."
MAP_NO_RESULTS = "No public data was stored for this lookup, so there is nothing to map."
MAP_NO_LOCATION = (
    "This lookup has no map location, so the property could not be placed on the map."
)


class CreateBody(BaseModel):
    address: str
    parcel_id: str | None = None
    mode: str = "check"


class RetryBody(BaseModel):
    source_ids: list[str] | None = None


# ── Shared helpers ─────────────────────────────────────────────────────


def _get_run(db: Session, user: User, run_id: str) -> SiteCheckRun:
    """Tenant-scoped fetch with §7.6 read-path reconciliation; cross-
    tenant and unknown ids both read as missing (404)."""
    run = sc_service.get_run(db, tenant_id=user.tenant_id, run_id=run_id)
    if run is None:
        raise plain_error(404, NOT_FOUND)
    return run


def _parse_dt(value: Any) -> datetime | None:
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


def _successful_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return False
    if isinstance(errors, str) and errors.strip():
        return False
    facts = result.get("normalized_facts")
    return isinstance(facts, dict) and bool(facts)


def oldest_checked_at(run: SiteCheckRun) -> datetime | None:
    """§9.4: the oldest successful source's checked-on date — the basis
    for the run header and the freshness band."""
    oldest: datetime | None = None
    for result in (run.adapter_results or {}).values():
        if not _successful_result(result):
            continue
        fetched = _parse_dt(result.get("fetched_at"))
        if fetched is None:
            continue
        if oldest is None or fetched < oldest:
            oldest = fetched
    return oldest


def freshness_band(oldest: datetime | None, *, now: datetime | None = None) -> str | None:
    """§9.4 band, COMPUTED from site_check_freshness_days and
    site_check_expiring_notice_days — nothing hard-codes 180."""
    if oldest is None:
        return None
    settings_obj = get_settings()
    freshness = timedelta(days=settings_obj.site_check_freshness_days)
    notice = timedelta(days=settings_obj.site_check_expiring_notice_days)
    age = (now or datetime.now(UTC)) - oldest
    if age >= freshness:
        return "expired"
    if age >= freshness - notice:
        return "expiring"
    return "current"


def _cards_for_run(run: SiteCheckRun) -> dict | None:
    """§8.3 two-class rule: cards are non-null for (a) any TERMINAL run
    with staged source results, and (b) every terminal cancelled run even
    with zero staged results (the all-not_checked scaffold). Everything
    else — queued, running, failed-with-nothing-staged — is null."""
    if run.status in _ACTIVE:
        return None
    has_staged = bool(run.adapter_results)
    if not has_staged and run.status != "cancelled":
        return None
    builder = _resolve_cards_builder()
    if builder is None:
        return None
    try:
        return builder(run, get_settings())
    except Exception:
        log.exception("cards builder failed for run_id=%s", run.id)
        return None


def _cards_schema_version() -> int:
    try:
        from app.services.site_check.cards import CARDS_SCHEMA_VERSION
    except ImportError:
        return 1
    return CARDS_SCHEMA_VERSION


def _resolve_cards_builder():
    """The §10 CardsPayload builder (cards wave module). Resolved at call
    time so it can land/patch independently of this router."""
    try:
        from app.services.site_check.cards import build_cards
    except ImportError:
        return None
    return build_cards


def _headline_takeaway(run: SiteCheckRun) -> str | None:
    """§8.2 deterministic one-liner. Prefers the cards builder's bottom
    line; falls back to a plain deterministic summary."""
    cards = _cards_for_run(run)
    if isinstance(cards, dict):
        text = ((cards.get("bottom_line") or {}).get("text") or "").strip()
        if text:
            return text
    if run.status == "completed":
        total = sum(
            1 for r in (run.adapter_results or {}).values()
            if _successful_result(r)
        )
        failed = len(run.sources_failed or [])
        line = f"Checked {total} public data sources."
        if failed:
            line += f" {failed} couldn't answer."
        return line
    return None


def _job_for_progress(db: Session, run: SiteCheckRun) -> Job | None:
    """The job row that drives the detail progress: an ACTIVE retry job
    when one exists (§8.4 — the run stays completed while retrying),
    else the run's collect job."""
    retry_job = sc_service._active_retry_job_for_run(db, run=run)
    if retry_job is not None:
        return retry_job
    return sc_service._job_for_run(db, run)


def _retry_active(db: Session, run: SiteCheckRun) -> bool:
    """§8.3: is a source retry genuinely still in flight?

    Derived from the JOB ROW's status (`queued`/`running`), never from the
    progress JSON — progress is a payload the worker writes, so a client that
    settles on it can hang on a stale stage against an already-terminal job.
    Flips to False in the same transaction that terminalizes the retry job.
    """
    return sc_service._active_retry_job_for_run(db, run=run) is not None


def _progress_payload(db: Session, run: SiteCheckRun) -> dict | None:
    job = _job_for_progress(db, run)
    if job is None:
        return None
    p = dict(job.progress or {})
    now = datetime.now(UTC)
    started = _parse_dt(run.started_at) or _parse_dt(job.claimed_at) or _parse_dt(job.created_at)
    ended = _parse_dt(job.finished_at) or now
    elapsed = max(0, int((ended - started).total_seconds())) if started else 0
    # While the job is live its OWN status ('queued' | 'running') is the
    # truth the client polls on; once terminal, the ported progress
    # vocabulary's last state stands.
    status = job.status if job.status in _ACTIVE else str(p.get("status") or job.status)
    return {
        "stage": str(p.get("stage") or job.status),
        "status": status,
        "message": p.get("message"),
        "completed_sources": int(p.get("completed_sources") or 0),
        "total_sources": int(p.get("total_sources") or 0),
        "failed_sources_count": int(p.get("failed_sources_count") or 0),
        "elapsed_seconds": elapsed,
        "updated_at": (
            p.get("updated_at")
            or (_parse_dt(job.heartbeat_at) or _parse_dt(job.created_at) or now).isoformat()
        ),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    return dt.isoformat()


# ── §8.1 Create ────────────────────────────────────────────────────────


@router.post("", status_code=202)
def create_lookup(
    body: CreateBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Structural validation ONLY (no network on the request path), then
    the §7.5 enqueue-supersede transaction. 202 is immediate; geocoding
    runs in the worker."""
    address = body.address or ""
    if not address.strip():
        raise plain_error(422, "Please fill in the address.")
    if len(address) > 512:
        raise plain_error(422, "That address is too long — please shorten it.")
    parcel_id = (body.parcel_id or "").strip() or None
    if parcel_id is not None and len(parcel_id) > 64:
        raise plain_error(422, "That parcel or property ID is too long.")
    if body.mode not in ("check", "fresh"):
        raise plain_error(422, "Please check the mode.")

    try:
        run, job, _superseded = sc_service.create_run(
            db,
            tenant_id=user.tenant_id,
            requested_by=user.id,
            address=address,
            parcel_id=parcel_id,
            mode=body.mode,
        )
    except sc_service.ActiveRunConflict as exc:
        raise plain_error(409, str(exc)) from exc
    return {"run_id": run.id, "job_id": job.id, "status": run.status}


# ── §8.2 List ──────────────────────────────────────────────────────────


@router.get("/runs")
def list_runs(
    query: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    count_stmt = (
        select(func.count())
        .select_from(SiteCheckRun)
        .where(SiteCheckRun.tenant_id == user.tenant_id)
    )
    normalized_query = sc_service.normalize_address(query)
    if normalized_query:
        count_stmt = count_stmt.where(
            SiteCheckRun.normalized_address.contains(normalized_query)
        )
    total = int(db.execute(count_stmt).scalar_one())

    runs = sc_service.get_history(
        db,
        tenant_id=user.tenant_id,
        query=query,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    summaries = []
    for run in runs:
        oldest = oldest_checked_at(run)
        summaries.append({
            "id": run.id,
            "address": run.address,
            "parcel_id": run.parcel_id,
            "county": run.county or None,
            "status": run.status,
            "error_code": run.error_code,
            "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at),
            "headline_takeaway": _headline_takeaway(run),
            "oldest_checked_at": _iso(oldest),
            "freshness": freshness_band(oldest),
        })
    return {"runs": summaries, "total": total, "page": page, "page_size": page_size}


# ── §8.3 Detail ────────────────────────────────────────────────────────


@router.get("/runs/{run_id}")
def run_detail(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    run = _get_run(db, user, run_id)
    oldest = oldest_checked_at(run)
    settings_obj = get_settings()
    freshness_days = settings_obj.site_check_freshness_days
    return {
        "id": run.id,
        "address": run.address,
        "parcel_id": run.parcel_id,
        "county": run.county or None,
        "lat": run.lat,
        "lng": run.lng,
        "anchor_kind": run.anchor_kind,
        "anchor_quality": run.anchor_quality,
        "status": run.status,
        "error_code": run.error_code,
        "failure_reason": run.failure_reason,
        "mode": run.mode,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "progress": _progress_payload(db, run),
        # Terminal-derived retry signal (§8.3): true ONLY while a retry job
        # for this run is non-terminal. The client settles its retry watcher
        # on this, not on the progress JSON.
        "retry_active": _retry_active(db, run),
        "cards": _cards_for_run(run),
        "retryable_failed_sources": (
            sc_service.retryable_failed_sources_for_run(run)
            if run.status == "completed" else []
        ),
        # Cards are composed on read, so the payload always carries the
        # CURRENT card schema — never the version stamped when the row was
        # written (v2 added tables/links/image to every card).
        "cards_schema_version": _cards_schema_version(),
        # §9.4 header/notice support (frontend renders these when present;
        # every value derives from settings — nothing hard-codes 180).
        "freshness": freshness_band(oldest),
        "freshness_days": freshness_days,
        "refresh_after": _iso(
            oldest + timedelta(days=freshness_days) if oldest else None
        ),
    }


# ── Map projection ─────────────────────────────────────────────────────


@router.get("/runs/{run_id}/map", response_model=SiteCheckMapContext)
def run_map(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SiteCheckMapContext:
    """Read-only map projection of one stored run.

    No provider calls and no writes: everything is projected from the
    already-stored `adapter_results`. Tenant-scoped like every other route
    (cross-tenant ids read as missing, 404). Served ONLY from a run that
    actually has staged source results — an active or empty run is a plain
    409, never a half-drawn map.
    """
    run = _get_run(db, user, run_id)
    if run.status in _ACTIVE:
        raise plain_error(409, MAP_STILL_RUNNING)
    if not run.adapter_results:
        raise plain_error(409, MAP_NO_RESULTS)
    try:
        return build_map_context(run)
    except ValueError as exc:
        log.info("site_check map unavailable run_id=%s reason=%s", run.id, exc)
        raise plain_error(409, MAP_NO_LOCATION) from exc


# ── §8.5 Cancel ────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """§7.5 cancel. Queued → run + job cancelled immediately; running →
    durable flag the worker honors at its next checkpoint; terminal →
    409; repeats while pending → 202 idempotent (no second audit row).

    Cancellation and the worker's terminal transition are ONE atomic race:
    both sides serialize on the run row (`SELECT … FOR UPDATE`), the first
    transaction to commit wins, and the loser mutates NOTHING — no
    `cancel_requested_at` on a row that finished first, and no
    `site_check.run_cancelled` audit row. The audit row is written only by
    the transaction that accepts the FIRST cancellation mutation (§8.7).
    """
    run = _get_run(db, user, run_id)
    if run.status not in _ACTIVE:
        # Already terminal when the request arrived — a plain refusal.
        raise plain_error(409, ALREADY_FINISHED)
    if run.cancel_requested_at is not None:
        # Idempotent repeat — already requested, no second audit row.
        return {"run_id": run.id, "status": run.status}

    db.rollback()  # a clean transaction to hold the run-row lock
    locked = db.execute(
        select(SiteCheckRun)
        .where(SiteCheckRun.id == run_id, SiteCheckRun.tenant_id == user.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if locked is None:
        db.rollback()
        raise plain_error(404, NOT_FOUND)
    if locked.status not in _ACTIVE or locked.cancel_requested_at is not None:
        # We lost the race under the lock: the worker terminalized first, or
        # another request already accepted the first cancellation. Answer
        # with the ACTUAL state, mutate nothing, write no audit row.
        actual = locked.status
        db.rollback()
        return {"run_id": run_id, "status": actual}

    now = datetime.now(UTC)
    locked.cancel_requested_at = now
    job = sc_service._job_for_run(db, locked)
    cancelled_now = False
    if locked.status == "queued" and job is not None:
        # Guarded flip: only an unclaimed job cancels immediately. If the
        # worker claimed it in this race, the durable flag wins at the
        # worker's next checkpoint instead.
        flipped = db.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "queued")
            .values(
                status="cancelled",
                error_code="cancelled_by_user",
                error_message="This lookup was cancelled.",
                finished_at=func.now(),
            )
        )
        cancelled_now = flipped.rowcount == 1
    if cancelled_now:
        locked.status = "cancelled"
        locked.error_code = "cancelled_by_user"
        locked.failure_reason = "This lookup was cancelled before it finished."
        locked.finished_at = now
    db.add(locked)
    # The one and only cancellation audit row — actor = the cancelling
    # user, recorded at request time (§7.5), written ONLY by the
    # transaction that accepts the first cancellation mutation.
    audit.log(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="site_check.run_cancelled",
        entity_type="site_check_run",
        entity_id=locked.id,
        detail={"address": locked.address, "immediate": cancelled_now},
    )
    db.commit()
    db.refresh(locked)
    return {"run_id": locked.id, "status": locked.status}


# ── §8.4 Source retry ──────────────────────────────────────────────────


@router.post("/runs/{run_id}/retry-sources", status_code=202)
def retry_sources(
    run_id: str,
    body: RetryBody | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    source_ids = (body.source_ids if body else None) or []
    try:
        run, job, _created = sc_service.enqueue_source_retry(
            db,
            tenant_id=user.tenant_id,
            run_id=run_id,
            requested_by=user.id,
            source_ids=source_ids,
        )
    except LookupError as exc:
        raise plain_error(404, NOT_FOUND) from exc
    except sc_service.RetryRefused as exc:
        raise plain_error(409, str(exc)) from exc
    return {"run_id": run.id, "job_id": job.id, "status": run.status}


# ── §8.5 Delete ────────────────────────────────────────────────────────


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Terminal runs only. Removes the run and its job rows; ai_usage,
    provider-usage (run_id SET NULL — accounting records), audit rows and
    the global cache are NOT deleted (§8.5)."""
    run = _get_run(db, user, run_id)
    if run.status in _ACTIVE:
        raise plain_error(409, CANCEL_FIRST)

    job_ids = [
        job.id
        for job in db.scalars(
            select(Job).where(
                Job.kind == "site_check",
                Job.tenant_id == user.tenant_id,
            )
        ).all()
        if (job.payload or {}).get("run_id") == run.id
    ]
    if job_ids:
        db.execute(sa_delete(Job).where(Job.id.in_(job_ids)))
    address_snapshot = run.address
    parcel_snapshot = run.parcel_id
    db.execute(sa_delete(SiteCheckRun).where(SiteCheckRun.id == run.id))
    audit.log(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="site_check.run_deleted",
        entity_type="site_check_run",
        entity_id=run_id,
        detail={"address": address_snapshot, "parcel_id": parcel_snapshot},
    )
    db.commit()
    return {"status": "deleted"}
