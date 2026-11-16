"""Site Check brick 12 — background-job foundation (spec site_check.md §7).

Exactly-once claim, lease fencing, the ONE stale-recovery primitive (sweeper
and read path identical), heartbeat watchdog scoping, atomic multi-row
enqueue. Postgres only (house harness)."""

import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core import job_queue
from app.db import SessionLocal
from app.models import Job


@pytest.fixture
def tenant(make_tenant):
    return make_tenant("Jobs Co")


@pytest.fixture
def fresh_session():
    sessions = []

    def _make():
        s = SessionLocal()
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.rollback()
        s.close()


def _enqueue(db, tenant, **overrides) -> Job:
    job = job_queue.enqueue_job(
        db, tenant_id=tenant.id, kind=overrides.pop("kind", "site_check"),
        payload=overrides.pop("payload", {"run_id": "r1", "mode": "check"}),
        **overrides,
    )
    db.commit()
    return job


def _make_stale(db, job_id: str) -> None:
    """Backdate the heartbeat past the stale cutoff (DB time, per house rule)."""
    db.execute(text(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = :id"
    ), {"id": job_id})
    db.commit()


def test_two_workers_claim_exactly_once(db, tenant, fresh_session):
    """§7.3 REQUIRED: two workers can never claim the same job."""
    _enqueue(db, tenant)
    barrier = threading.Barrier(2)
    results: list[Job | None] = [None, None]

    def attempt(i: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait()
            results[i] = job_queue.claim_next_job(session, worker_id=f"w{i}")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    job = db.execute(select(Job)).scalars().one()
    assert job.status == "running" and job.attempts == 1
    assert job.lease_token == winners[0].lease_token


def test_claim_stamps_lease_and_worker(db, tenant, fresh_session):
    _enqueue(db, tenant)
    session = fresh_session()
    job = job_queue.claim_next_job(session, worker_id="worker-1", kinds=["site_check"])
    assert job is not None
    assert job.status == "running"
    assert job.claimed_by == "worker-1"
    assert job.lease_token and job.claimed_at and job.heartbeat_at
    # Kind filter: nothing else claimable
    assert job_queue.claim_next_job(session, worker_id="worker-1") is None


def test_late_stale_writer_matches_zero_rows(db, tenant, fresh_session):
    """§7.4 REQUIRED late-writer test: worker A goes stale mid-run, the
    sweeper requeues, worker B completes; A wakes and tries to write → 0 rows,
    the job keeps B's terminal result."""
    _enqueue(db, tenant)
    session_a = fresh_session()
    job_a = job_queue.claim_next_job(session_a, worker_id="A")
    token_a = job_a.lease_token
    _make_stale(db, job_a.id)

    sweep_session = fresh_session()
    assert job_queue.sweep_stale_jobs(sweep_session) == [(job_a.id, "requeued")]

    session_b = fresh_session()
    job_b = job_queue.claim_next_job(session_b, worker_id="B")
    assert job_b.id == job_a.id and job_b.lease_token != token_a
    assert job_b.attempts == 2
    job_queue.write_progress(session_b, job_b.id, job_b.lease_token,
                             {"message": "halfway"})
    assert job_queue.mark_done(session_b, job_b.id, job_b.lease_token) is True

    # A wakes with its dead lease: every fenced write is a no-op. The
    # progress checkpoint is the PRIMARY signal — it raises LeaseLost so a
    # late worker can never carry on to its next provider call.
    with pytest.raises(job_queue.LeaseLost):
        job_queue.write_progress(session_a, job_a.id, token_a, {"message": "stale"})
    assert job_queue.try_write_progress(
        session_a, job_a.id, token_a, {"message": "stale"}
    ) is False
    assert job_queue.mark_done(session_a, job_a.id, token_a) is False
    assert job_queue.mark_failed(session_a, job_a.id, token_a,
                                 error_code="x", error_message="x") is False

    db.expire_all()
    job = db.get(Job, job_a.id)
    assert job.status == "succeeded"  # terminal state never overwritten
    assert job.progress == {"message": "halfway"}


def test_recover_stale_job_requeues_below_cap_and_fails_at_cap(db, tenant, fresh_session):
    """§7.4: the primitive requeues below the attempt cap (lease invalidated,
    retry message staged) and fails job+run together at the cap."""
    calls: list[tuple[str, str]] = []
    job_queue.register_recovery_reconciler(
        "recover_test", lambda session, job, outcome: calls.append((job.id, outcome))
    )
    try:
        job_row = _enqueue(db, tenant, kind="recover_test")
        session = fresh_session()
        claimed = job_queue.claim_next_job(session, worker_id="A", kinds=["recover_test"])
        _make_stale(db, claimed.id)

        recover_session = fresh_session()
        assert job_queue.recover_stale_job(recover_session, claimed.id) == "requeued"
        db.expire_all()
        job = db.get(Job, job_row.id)
        assert job.status == "queued"
        assert job.lease_token is None and job.claimed_by is None
        assert job.heartbeat_at is None and job.claimed_at is None
        assert job.progress["message"] == job_queue.RETRY_MESSAGE
        assert calls == [(job.id, "requeued")]

        # Second (final) attempt goes stale too → fail at the cap.
        claimed = job_queue.claim_next_job(session, worker_id="B", kinds=["recover_test"])
        assert claimed.attempts == 2
        _make_stale(db, claimed.id)
        assert job_queue.recover_stale_job(recover_session, claimed.id) == "failed"
        db.expire_all()
        job = db.get(Job, job_row.id)
        assert job.status == "failed"
        assert job.error_code == "worker_lost"
        assert job.error_message == job_queue.WORKER_LOST_MESSAGE
        assert job.lease_token is None and job.finished_at is not None
        assert calls[-1] == (job.id, "failed")
    finally:
        job_queue._RECOVERY_RECONCILERS.pop("recover_test", None)


def test_read_path_and_sweeper_produce_identical_outcomes(db, tenant, fresh_session):
    """§7.4 REQUIRED: a poll arriving after the stale cutoff but before the
    sweep invokes the SAME primitive — the outcome is identical to the
    unpolled job's."""
    job_polled = _enqueue(db, tenant, payload={"run_id": "polled"})
    job_swept = _enqueue(db, tenant, payload={"run_id": "swept"})
    session = fresh_session()
    a = job_queue.claim_next_job(session, worker_id="A")
    b = job_queue.claim_next_job(session, worker_id="A")
    assert {a.id, b.id} == {job_polled.id, job_swept.id}
    _make_stale(db, a.id)
    _make_stale(db, b.id)

    # Read path recovers one directly; the sweeper picks up the other.
    read_session = fresh_session()
    assert job_queue.recover_stale_job(read_session, job_polled.id) == "requeued"
    sweep_session = fresh_session()
    assert job_queue.sweep_stale_jobs(sweep_session) == [(job_swept.id, "requeued")]

    db.expire_all()
    polled, swept = db.get(Job, job_polled.id), db.get(Job, job_swept.id)
    for field in ("status", "lease_token", "claimed_by", "heartbeat_at",
                  "claimed_at", "attempts", "error_code"):
        assert getattr(polled, field) == getattr(swept, field)
    assert polled.progress["message"] == swept.progress["message"] == job_queue.RETRY_MESSAGE


def test_recover_ignores_fresh_and_terminal_jobs(db, tenant, fresh_session):
    _enqueue(db, tenant)
    session = fresh_session()
    job = job_queue.claim_next_job(session, worker_id="A")
    recover_session = fresh_session()
    # Fresh heartbeat (blocked-but-alive worker): never recovered.
    assert job_queue.recover_stale_job(recover_session, job.id) is None
    assert job_queue.sweep_stale_jobs(recover_session) == []
    # Terminal: never recovered either.
    assert job_queue.mark_done(session, job.id, job.lease_token) is True
    _make_stale(db, job.id)  # backdate anyway
    assert job_queue.recover_stale_job(recover_session, job.id) is None
    db.expire_all()
    assert db.get(Job, job.id).status == "succeeded"


def test_heartbeat_watchdog_updates_only_held_leases(db, tenant, fresh_session):
    """§7.4: the bulk heartbeat UPDATE is guarded by the held lease tokens."""
    _enqueue(db, tenant, payload={"run_id": "one"})
    _enqueue(db, tenant, payload={"run_id": "two"})
    session = fresh_session()
    first = job_queue.claim_next_job(session, worker_id="A")
    second = job_queue.claim_next_job(session, worker_id="B")
    db.execute(text("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes'"))
    db.commit()

    pump_session = fresh_session()
    assert job_queue.pump_heartbeats(pump_session, [first.lease_token]) == 1
    db.expire_all()
    ages = {
        row.id: row.age for row in db.execute(text(
            "SELECT id, now() - heartbeat_at AS age FROM jobs"
        ))
    }
    assert ages[first.id].total_seconds() < 60      # pumped
    assert ages[second.id].total_seconds() > 240    # untouched
    assert job_queue.pump_heartbeats(pump_session, []) == 0


def test_atomic_multi_row_enqueue_rolls_back_on_failure(db, tenant):
    """§7.3: the multi-row enqueue helper commits all rows or none."""
    with pytest.raises(IntegrityError):
        job_queue.enqueue_jobs(db, [
            {"tenant_id": tenant.id, "kind": "site_check", "payload": {"run_id": "ok"}},
            {"tenant_id": "no-such-tenant", "kind": "site_check"},  # FK violation
        ])
    assert db.execute(select(Job)).scalars().first() is None

    jobs = job_queue.enqueue_jobs(db, [
        {"tenant_id": tenant.id, "kind": "site_check", "payload": {"run_id": "a"}},
        {"tenant_id": tenant.id, "kind": "site_check", "payload": {"run_id": "b"}},
    ])
    assert len(jobs) == 2
    assert all(j.status == "queued" and j.max_attempts >= 1 for j in jobs)


def test_handler_registry(db):
    def handler(session, job):
        pass

    job_queue.register_handler("registry_test", handler)
    try:
        assert job_queue.get_handler("registry_test") is handler
        assert job_queue.get_handler("unknown_kind") is None
    finally:
        job_queue._HANDLERS.pop("registry_test", None)
