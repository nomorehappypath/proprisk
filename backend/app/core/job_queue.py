"""Generic background-job queue (docs/specs/site_check.md §7). Postgres only.

Design contract:
- **Atomic claim** (§7.3): `SELECT … FOR UPDATE SKIP LOCKED` picks a candidate,
  then a guarded `UPDATE … WHERE status='queued'` flips it to running with a
  fresh lease_token and attempts+1 — two workers can never claim the same job.
- **Lease fencing** (§7.4): every worker-side write is
  `UPDATE … WHERE lease_token = :my_token AND status='running'`; a late writer
  whose job was recovered matches 0 rows and must abandon silently. A
  zero-row fence ROLLS THE WHOLE STAGED UNIT BACK and raises `LeaseLost` —
  the session is never committed under a lost lease, so no run/result/usage/
  audit state written since the last commit can leak out. Terminal states are
  never overwritten.
- **One commit per guarded mutation**: every commit a worker makes rides a
  successful lease-guarded `jobs` UPDATE in the SAME transaction. No helper
  commits unrelated state.
- **ONE stale-recovery primitive**: `recover_stale_job` is the only code path
  that transitions a stale job. Both callers — the worker's sweeper thread and
  the API read-path reconciler — use it, so the outcome never depends on who
  noticed first. Per-kind reconcilers keep the paired run row in lockstep in
  the SAME transaction.
- **Kind registries**: handlers do the work; recovery reconcilers pair the
  job transition with its run row (registered by each job kind's service —
  the site_check handler registers in its own module).
"""

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.models import Job

# A handler does the job's work: handler(session, job). Return → the worker
# marks the job done; raise → the worker marks it failed (handler_error).
Handler = Callable[[Session, Job], None]
# A recovery reconciler pairs the run row with the job's stale-recovery
# outcome ("requeued" | "failed" | "cancelled") in the same transaction.
RecoveryReconciler = Callable[[Session, Job, str], None]
# A recovery prelock locks the job's PAIRED DOMAIN ROW (for site_check, the
# run) BEFORE the job row is locked — establishing the one lock order every
# terminal path uses: domain row → job row (§7.4). It returns True when that
# row already carries an accepted cancellation, which forces the recovery
# outcome to "cancelled" instead of requeue/worker_lost.
RecoveryPrelock = Callable[[Session, Job], bool]

_HANDLERS: dict[str, Handler] = {}
_RECOVERY_RECONCILERS: dict[str, RecoveryReconciler] = {}
_RECOVERY_PRELOCKS: dict[str, RecoveryPrelock] = {}

RETRY_MESSAGE = "Retrying after an interruption."
WORKER_LOST_MESSAGE = "The lookup stopped unexpectedly. Run it again."
CANCELLED_BY_USER_MESSAGE = "You cancelled this lookup."


class LeaseLost(Exception):
    """A lease-fenced write matched 0 rows (§7.4).

    The job was recovered, superseded, cancelled or otherwise terminalized by
    another actor, so this worker no longer owns the work. The failing write
    has already rolled the session back — the holder must abandon immediately
    and make NO further provider calls or state writes.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"lease lost for job {job_id}")
        self.job_id = job_id


# ── Kind registries ────────────────────────────────────────────────────────

def register_handler(kind: str, handler: Handler) -> None:
    _HANDLERS[kind] = handler


def get_handler(kind: str) -> Handler | None:
    return _HANDLERS.get(kind)


def register_recovery_reconciler(kind: str, reconciler: RecoveryReconciler) -> None:
    _RECOVERY_RECONCILERS[kind] = reconciler


def register_recovery_prelock(kind: str, prelock: RecoveryPrelock) -> None:
    _RECOVERY_PRELOCKS[kind] = prelock


# ── Enqueue (§7.3) ─────────────────────────────────────────────────────────

def enqueue_job(
    session: Session,
    *,
    tenant_id: str,
    kind: str,
    payload: dict | None = None,
    progress: dict | None = None,
    max_attempts: int | None = None,
) -> Job:
    """Add ONE queued job to the CALLER's open transaction. The caller commits
    run row + job + audit together (or nothing) — spec §7.3 atomic enqueue."""
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload,
        progress=progress,
        status="queued",
        attempts=0,
        max_attempts=max_attempts if max_attempts is not None else settings.job_max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def enqueue_jobs(session: Session, specs: list[dict[str, Any]]) -> list[Job]:
    """Atomic multi-row enqueue: every spec is a kwargs dict for enqueue_job.
    All rows commit together; any failure rolls the whole batch back."""
    try:
        jobs = [enqueue_job(session, **spec) for spec in specs]
        session.commit()
    except Exception:
        session.rollback()
        raise
    return jobs


# ── Claim (§7.3) ───────────────────────────────────────────────────────────

def claim_next_job(
    session: Session, *, worker_id: str, kinds: list[str] | None = None
) -> Job | None:
    """Atomically claim the oldest queued job. Returns the running Job (with
    its fresh lease_token loaded) or None when nothing is claimable."""
    candidate_sql = (
        "SELECT id FROM jobs WHERE status = 'queued'"
        + (" AND kind = ANY(:kinds)" if kinds else "")
        + " ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = session.execute(
        text(candidate_sql), {"kinds": kinds} if kinds else {}
    ).first()
    if row is None:
        session.rollback()
        return None
    lease_token = str(uuid.uuid4())
    claimed = session.execute(
        update(Job)
        .where(Job.id == row.id, Job.status == "queued")
        .values(
            status="running",
            claimed_by=worker_id,
            lease_token=lease_token,
            claimed_at=func.now(),
            heartbeat_at=func.now(),
            attempts=Job.attempts + 1,
        )
    )
    if claimed.rowcount != 1:  # raced away despite SKIP LOCKED — give up cleanly
        session.rollback()
        return None
    session.commit()
    # populate_existing: never serve a stale identity-map copy of the row.
    return session.get(Job, row.id, populate_existing=True)


# ── Heartbeat (§7.4) ───────────────────────────────────────────────────────

def pump_heartbeats(session: Session, held_tokens: list[str]) -> int:
    """ONE bulk heartbeat UPDATE for all in-process jobs, guarded by the held
    lease tokens (a recovered/reassigned job is never touched). Called by the
    worker's dedicated watchdog thread — work threads never write heartbeats."""
    if not held_tokens:
        return 0
    result = session.execute(
        update(Job)
        .where(Job.status == "running", Job.lease_token.in_(held_tokens))
        .values(heartbeat_at=func.now())
    )
    session.commit()
    return result.rowcount


# ── Lease-fenced writes (§7.4) ─────────────────────────────────────────────

def try_fenced_write(
    session: Session, job_id: str, lease_token: str, **values: Any
) -> bool:
    """Lease-fenced job write, BOOLEAN variant — for the few callers that
    genuinely branch on the outcome (the worker's terminal backstops).

    On a match the whole staged unit commits together with the guarded job
    mutation. On 0 rows the lease is gone: the session is ROLLED BACK (never
    committed) so nothing staged since the last commit — run, results, usage,
    audit — can leak out, and False is returned.
    """
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_token == lease_token, Job.status == "running")
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True


def fenced_write(session: Session, job_id: str, lease_token: str, **values: Any) -> None:
    """Lease-fenced job write — the PRIMARY signal. Commits the staged unit
    with the guarded job mutation, or rolls back and raises `LeaseLost`."""
    if not try_fenced_write(session, job_id, lease_token, **values):
        raise LeaseLost(job_id)


def lease_is_valid(session: Session, job_id: str, lease_token: str) -> bool:
    """Read-only fence check — is this lease still ours and running?"""
    return session.execute(
        select(Job.id).where(
            Job.id == job_id,
            Job.lease_token == lease_token,
            Job.status == "running",
        )
    ).first() is not None


def assert_lease(session: Session, job_id: str | None, lease_token: str | None) -> None:
    """Raise `LeaseLost` unless the lease is still held. A no-op for inline /
    direct (job-less) calls. Cheap enough to run at every wave boundary so a
    worker stops BEFORE the next provider call."""
    if not job_id or not lease_token:
        return
    if not lease_is_valid(session, job_id, lease_token):
        session.rollback()
        raise LeaseLost(job_id)


def write_progress(session: Session, job_id: str, lease_token: str, progress: dict) -> None:
    """Lease-fenced progress write (never touches heartbeat_at). Raises
    `LeaseLost` — a progress write is the worker's checkpoint."""
    fenced_write(session, job_id, lease_token, progress=progress)


def try_write_progress(
    session: Session, job_id: str, lease_token: str, progress: dict
) -> bool:
    """Boolean variant of `write_progress` for callers that must branch."""
    return try_fenced_write(session, job_id, lease_token, progress=progress)


def mark_done(session: Session, job_id: str, lease_token: str) -> bool:
    """Lease-fenced terminal transition running → succeeded. A late call from
    an old lease (or onto an already-terminal job) matches 0 rows: no-op.
    Boolean by design — the worker logs the no-op and moves on."""
    return try_fenced_write(
        session, job_id, lease_token, status="succeeded", finished_at=func.now()
    )


def mark_failed(
    session: Session, job_id: str, lease_token: str, *, error_code: str, error_message: str
) -> bool:
    """Lease-fenced terminal transition running → failed (handler errors ONLY,
    never a cancellation — §7.2). Boolean by design, like `mark_done`."""
    return try_fenced_write(
        session,
        job_id,
        lease_token,
        status="failed",
        error_code=error_code,
        error_message=error_message,
        finished_at=func.now(),
    )


# ── Stale recovery (§7.4 — THE one primitive) ──────────────────────────────

def _stale_cutoff():
    """SQL expression: now() minus the stale threshold — all staleness
    comparisons run against DB time (house clock rule)."""
    return func.now() - func.make_interval(0, 0, 0, 0, 0, 0, settings.job_stale_after_seconds)


def recover_stale_job(session: Session, job_id: str) -> str | None:
    """THE stale-recovery primitive — the ONLY code path that transitions a
    stale job; both the sweeper thread and the read-path reconciler call it.

    LOCK ORDER (reviewer finding, round 2 — 2026-08-01): the paired DOMAIN
    ROW is locked FIRST via the kind's registered prelock, then the job row.
    Cancellation, enqueue-supersede and every worker terminal transition use
    that same run→job order, so recovery can no longer commit `worker_lost`
    over a cancellation that was accepted while recovery held only the job
    lock. An unlocked snapshot read is used ONLY to discover the kind and the
    paired row id; the job is re-validated as stale-running after both locks
    are held.

    In one transaction, for a job running with heartbeat_at past the stale
    cutoff: an accepted cancellation on the paired row → cancel job + run
    TOGETHER; below the attempt cap → invalidate the lease and requeue (paired
    run follows via the kind's registered reconciler); at the cap → invalidate
    the lease and fail job + run TOGETHER (worker_lost). Returns "requeued",
    "failed", "cancelled", or None when the job wasn't stale-running."""
    # (1) Unlocked snapshot: discover the kind + paired row. Decides nothing.
    snapshot = session.get(Job, job_id)
    if snapshot is None or snapshot.status != "running":
        session.rollback()
        return None
    kind = snapshot.kind
    session.rollback()  # release the snapshot's read transaction

    # (2) Lock the paired domain row FIRST (the shared lock order).
    prelock = _RECOVERY_PRELOCKS.get(kind)
    cancel_accepted = prelock(session, snapshot) if prelock is not None else False

    # (3) Now lock and RE-VALIDATE the job under both locks.
    job = session.execute(
        select(Job)
        .where(
            Job.id == job_id,
            Job.status == "running",
            Job.heartbeat_at < _stale_cutoff(),
        )
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        session.rollback()
        return None

    # Both branches invalidate the lease, so the late-writer guarantee covers
    # read-path-recovered jobs too.
    job.lease_token = None
    job.claimed_by = None
    if cancel_accepted:
        # An honored cancellation is ALWAYS `cancelled` — never failed,
        # never requeued — at or below the attempt cap (§7.5).
        job.status = "cancelled"
        job.error_code = "cancelled_by_user"
        job.error_message = CANCELLED_BY_USER_MESSAGE
        job.finished_at = func.now()
        # §7.8: terminal progress rides the SAME transaction as the terminal
        # status. Without this the job ended `cancelled` while its progress
        # still said {stage: sources, status: running} (reviewer round 3,
        # non-blocking note) — a terminal job must never look live.
        job.progress = {
            **(job.progress or {}),
            "stage": "cancelled",
            "status": "cancelled",
            "message": CANCELLED_BY_USER_MESSAGE,
        }
        outcome = "cancelled"
    elif job.attempts < job.max_attempts:
        job.status = "queued"
        job.claimed_at = None
        job.heartbeat_at = None
        job.progress = {**(job.progress or {}), "message": RETRY_MESSAGE}
        outcome = "requeued"
    else:
        job.status = "failed"
        job.error_code = "worker_lost"
        job.error_message = WORKER_LOST_MESSAGE
        job.finished_at = func.now()
        job.progress = {           # §7.8, same rule as the cancelled branch
            **(job.progress or {}),
            "stage": "failed",
            "status": "failed",
            "message": WORKER_LOST_MESSAGE,
        }
        outcome = "failed"

    reconciler = _RECOVERY_RECONCILERS.get(job.kind)
    if reconciler is not None:
        reconciler(session, job, outcome)  # pairs the run row in the SAME transaction
    session.commit()
    return outcome


def sweep_stale_jobs(session: Session) -> list[tuple[str, str]]:
    """Recover every stale running job via the one primitive. Called by the
    worker at startup and every job_sweep_interval_seconds (§7.4)."""
    stale_ids = session.execute(
        select(Job.id).where(Job.status == "running", Job.heartbeat_at < _stale_cutoff())
    ).scalars().all()
    session.rollback()  # end the read transaction before per-job transactions
    outcomes = []
    for job_id in stale_ids:
        outcome = recover_stale_job(session, job_id)
        if outcome is not None:
            outcomes.append((job_id, outcome))
    return outcomes
