"""jobs handler for kind='site_check'.

Per docs/specs/site_check.md §7. The worker (`app/worker.py`) drains
kind='site_check' jobs to this function; it reads the run_id off the
job's payload and runs the orchestrator end-to-end.

Contract with `app.worker`:
  - succeeds → return None (worker marks job succeeded)
  - any exception → worker marks job failed with a plain-English
    error_message and the run is left in whatever state the
    orchestrator put it in (likely 'failed' via _fail_run; on a true
    crash the orchestrator may not have flipped status — the §7.4/§7.6
    stale-recovery paths handle it)

This file lives in the site_check package so the entire
adapter-orchestrator-handler triangle moves together if/when
we extract the package later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import job_queue
from app.models import Job, SiteCheckRun
from app.services.site_check.orchestrator import (
    CANCELLED_MESSAGE,
    run_collect,
    run_retry_sources,
)

log = logging.getLogger("propdesk.site_check.worker")


def handle_site_check(session: Session, job: Job) -> None:
    """Worker entry point for kind='site_check'.

    Reads `run_id` from job.payload (site_check.md §7.1), runs the
    orchestrator. The orchestrator owns its own status transitions on
    the SiteCheckRun row (running → completed / failed). The worker
    handles the jobs row's lifecycle.
    """
    payload = job.payload or {}
    run_id = payload.get("run_id")
    if not run_id:
        raise RuntimeError(
            f"site_check job {job.id} missing run_id in payload "
            "— bug in service.enqueue_run"
        )

    # Multi-tenant isolation: defense-in-depth check that the
    # SiteCheckRun belongs to the same tenant as the job. The enqueue
    # path is tenant-scoped, so a mismatch only fires on an internal
    # bug — but the second wall keeps one tenant's worker run from ever
    # fetching public data on another tenant's behalf.
    run = session.get(SiteCheckRun, run_id)
    if run is None:
        raise RuntimeError(
            f"site_check handler: SiteCheckRun {run_id!r} not "
            f"found for job {job.id}"
        )
    if run.tenant_id != job.tenant_id:
        raise RuntimeError(
            f"site_check handler: SiteCheckRun {run_id!r} "
            f"tenant mismatch (run.tenant_id={run.tenant_id!r}, "
            f"job.tenant_id={job.tenant_id!r}); refusing to "
            "process across tenants"
        )

    log.info(
        "site_check handler start job_id=%s run_id=%s tenant_id=%s",
        job.id, run_id, job.tenant_id,
    )

    retry_source_ids = payload.get("retry_source_ids") or []
    if retry_source_ids:
        # ONE retry job owns its COMPLETE source list under ONE lease and
        # terminalizes exactly once, after the list finishes (§8.4). Looping
        # a per-source terminal call here made every source after the first
        # lose the fence and roll its result back.
        run = run_retry_sources(
            session,
            run_id=run_id,
            source_ids=[str(s) for s in retry_source_ids],
            job_id=job.id,
            lease_token=job.lease_token,
        )
    else:
        run = run_collect(
            session,
            run_id=run_id,
            job_id=job.id,
            lease_token=job.lease_token,
        )

    log.info(
        "site_check handler done job_id=%s run_id=%s "
        "status=%s sources_queried=%d sources_failed=%d",
        job.id, run.id, run.status,
        len(run.sources_queried or []),
        len(run.sources_failed or []),
    )


def prelock_site_check_recovery(session: Session, job: Job) -> bool:
    """Recovery prelock (reviewer finding, round 2 — 2026-08-01): lock the
    paired run row BEFORE `recover_stale_job` locks the job row, so stale
    recovery joins the same run→job lock order that cancellation,
    enqueue-supersede and the worker's terminal transitions use.

    Returns True when the run already carries an ACCEPTED cancellation
    (`cancel_requested_at` set while still active) — the primitive then
    terminalizes job + run as `cancelled` instead of requeue/worker_lost.
    Before this, recovery held only the job lock, read the run unlocked, and
    could commit `failed/worker_lost` over a cancellation the API had already
    accepted and audited.
    """
    run_id = (job.payload or {}).get("run_id")
    if not run_id:
        return False
    locked = session.execute(
        select(SiteCheckRun)
        .where(
            SiteCheckRun.id == run_id,
            SiteCheckRun.tenant_id == job.tenant_id,
        )
        .with_for_update()
        # Same identity-map trap as the read path (reviewer round 3): the
        # read-path caller's session already owns this run, so the lock must
        # be paired with a refreshed read or the cancellation check sees
        # pre-lock values.
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if locked is None:
        return False
    return (
        locked.cancel_requested_at is not None
        and locked.status in ("queued", "running")
    )


def reconcile_site_check_recovery(
    session: Session, job: Job, outcome: str,
) -> None:
    """Per-kind recovery reconciler (site_check.md §7.4/§7.6): pairs the
    SiteCheckRun row with `recover_stale_job`'s outcome in the SAME
    transaction (the primitive commits). The run row is ALREADY LOCKED by
    `prelock_site_check_recovery`.

      - "cancelled": the API's cancellation was accepted first → active run →
        `cancelled` (never `failed`), preserving the single request-time
        `site_check.run_cancelled` audit the API wrote.
      - "requeued" (attempts < cap): active run → queued; staged results
        retained (the job progress already says "Retrying after an
        interruption.").
      - "failed" (at the cap): active run → failed `worker_lost` with the
        plain message "The lookup stopped unexpectedly. Run it again."

    A run that is already terminal is never touched — a stale RETRY job
    belongs to a completed run, which stays completed (§8.4).
    """
    run_id = (job.payload or {}).get("run_id")
    if not run_id:
        return
    run = session.get(SiteCheckRun, run_id)
    if run is None or run.tenant_id != job.tenant_id:
        return
    if run.status not in ("queued", "running"):
        return
    if outcome == "cancelled":
        run.status = "cancelled"
        run.error_code = run.error_code or "cancelled_by_user"
        run.failure_reason = CANCELLED_MESSAGE
        run.finished_at = datetime.now(UTC)
    elif outcome == "requeued":
        run.status = "queued"
    else:
        run.status = "failed"
        run.error_code = "worker_lost"
        run.failure_reason = job_queue.WORKER_LOST_MESSAGE
        run.finished_at = datetime.now(UTC)
    session.add(run)


# Kind registration (site_check.md §7): importing this module wires the
# 'site_check' kind into the queue — the worker process (app.worker) and
# the API (read-path reconciliation via recover_stale_job) both import it.
job_queue.register_handler("site_check", handle_site_check)
job_queue.register_recovery_reconciler("site_check", reconcile_site_check_recovery)
job_queue.register_recovery_prelock("site_check", prelock_site_check_recovery)
