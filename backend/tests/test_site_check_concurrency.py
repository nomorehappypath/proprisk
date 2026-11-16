"""Site Check concurrency regressions (docs/specs/site_check.md §7.4, §7.5,
§8.4, §8.5, §8.7) — Postgres, deterministic.

Every test here inverts a reproduction from the 2026-08-01 implementation
review:

  1. a lost lease still committed worker data and did not stop work;
  2. cancel and terminal completion were not one atomic race;
  3. a retry job lost every source after the first, admitted duplicates,
     and never exposed terminal progress.

Adapters never hit the network: `_load_adapter_fetch` is monkeypatched onto
a fake that RECORDS its invocations, so "made no further provider call" is a
real assertion, not an inference.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config.settings import get_settings
from app.core import job_queue
from app.core.job_queue import LeaseLost
from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, Job, SiteCheckRun
from app.services.site_check import orchestrator, registry
from app.services.site_check import service as sc_service
from app.services.site_check.contract import AdapterResult


def _now():
    return datetime.now(UTC)


@pytest.fixture
def tenant(local_tenant_user):
    # Every HTTP request resolves to the one fixed local office — this
    # file's primary flow goes through the API, so its rows must belong
    # to that same office.
    return local_tenant_user[0]


@pytest.fixture
def user(local_tenant_user):
    return local_tenant_user[1]


@pytest.fixture
def hdrs(login, user):
    return login("race@example.com")


def _audits(db, action, entity_id=None):
    stmt = select(AuditLog).where(AuditLog.action == action)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return db.scalars(stmt).all()


def _adapter_result(source_id, facts, *, errors=None):
    return AdapterResult(
        source_id=source_id,
        source_access_status="PROGRAMMATIC_PUBLIC",
        data_confidence_status="verified_public_fact",
        coverage_status="covered",
        normalized_facts=facts,
        errors=errors or [],
        fetched_at=_now(),
    )


def _queued_pair(db, tenant, user, address="1100 Congress Ave, Austin TX"):
    return sc_service.enqueue_run(
        db, tenant_id=tenant.id, requested_by=user.id, address=address,
    )


def _claim(db):
    job = job_queue.claim_next_job(db, worker_id="w-race")
    assert job is not None
    return job


@pytest.fixture
def recording_roster(monkeypatch):
    """geocode + fema + soils, no network. `calls` records EVERY provider
    invocation, so a test can assert a worker stopped before the next one."""
    roster = ["arcgis_geocode", "fema_nfhl", "soil_intel"]
    monkeypatch.setattr(registry, "all_source_ids", lambda: list(roster))
    monkeypatch.setattr(orchestrator, "build_narratives", lambda *a, **k: {})
    monkeypatch.setattr(
        orchestrator, "build_historical_aerials", lambda *a, **k: {},
    )
    monkeypatch.setattr(
        orchestrator, "build_entitlement_reads", lambda *a, **k: {},
    )
    calls: list[str] = []

    def install(on_fetch=None):
        def loader(source_id):
            def fetch(identity, session=None, config=None):
                calls.append(source_id)
                if on_fetch is not None:
                    on_fetch(source_id)
                if source_id == "arcgis_geocode":
                    return _adapter_result(source_id, {
                        "lat": 30.2672, "lng": -97.7431,
                        "county": "travis", "source": "nominatim",
                    })
                return _adapter_result(source_id, {"fact": source_id})
            return fetch
        monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)

    return calls, install


# ── Finding 1: a lost lease rolls back and stops the work ──────────────


def test_zero_row_fence_rolls_back_dirty_orm_state(db, tenant, user):
    """THE round-4 reproduction, inverted. A run object is made dirty, the
    lease is invalidated, and a fenced write is attempted: it must raise
    LeaseLost and a FRESH session must see none of the dirty mutation."""
    run, job = _queued_pair(db, tenant, user)
    claimed = _claim(db)
    token = claimed.lease_token

    # Dirty, uncommitted worker state of every kind the orchestrator stages.
    run = db.get(SiteCheckRun, run.id)
    run.county = "DIRTY-COUNTY"
    run.lat, run.lng = 1.5, 2.5
    run.adapter_results = {"fema_nfhl": {"normalized_facts": {"zone": "X"}}}
    run.status = "running"
    db.add(run)

    # Another actor takes the lease away (stale recovery / supersede).
    thief = SessionLocal()
    try:
        stolen = thief.get(Job, job.id)
        stolen.lease_token = None
        thief.commit()
    finally:
        thief.close()

    with pytest.raises(LeaseLost):
        job_queue.write_progress(db, job.id, token, {"stage": "sources"})

    probe = SessionLocal()
    try:
        fresh = probe.get(SiteCheckRun, run.id)
        assert fresh.county == ""            # never persisted
        assert fresh.lat is None and fresh.lng is None
        assert not (fresh.adapter_results or {})
        assert fresh.status == "queued"
    finally:
        probe.close()


@pytest.mark.parametrize("parallel", [True, False])
@pytest.mark.parametrize("thief", ["recovery", "supersede"])
def test_lease_loss_between_provider_result_and_progress_write(
    db, tenant, user, recording_roster, monkeypatch, parallel, thief,
):
    """Recovery / supersede injected BETWEEN a provider result and the
    progress write that follows it: the staged unit rolls back, no later
    source is fetched, and the run keeps the state the winning actor left."""
    from app.config.settings import settings as settings_obj
    monkeypatch.setattr(settings_obj, "public_intel_parallel_enabled", parallel)
    calls, install = recording_roster
    run, _job = _queued_pair(db, tenant, user)
    claimed = _claim(db)

    def steal_lease_after_geocode(source_id):
        if source_id != "arcgis_geocode":
            return
        stealer = SessionLocal()
        try:
            if thief == "recovery":
                stale = stealer.get(Job, claimed.id)
                stale.heartbeat_at = _now() - timedelta(hours=1)
                stealer.commit()
                assert job_queue.recover_stale_job(stealer, claimed.id) == "requeued"
            else:
                sc_service.create_run(
                    stealer, tenant_id=tenant.id, requested_by=user.id,
                    address=run.address,
                )
        finally:
            stealer.close()

    install(on_fetch=steal_lease_after_geocode)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id, lease_token=claimed.lease_token,
    )

    # Stopped BEFORE the next provider call — only the geocoder ever ran.
    assert calls == ["arcgis_geocode"]
    # No late state: the geocode staging never reached the database.
    probe = SessionLocal()
    try:
        fresh = probe.get(SiteCheckRun, run.id)
        assert fresh.lat is None and fresh.lng is None
        assert not (fresh.adapter_results or {})
        assert fresh.status != "completed"
        if thief == "recovery":
            assert fresh.status == "queued"      # the sweeper's outcome
        else:
            assert fresh.status == "cancelled"   # superseded
            assert fresh.error_code == "superseded_by_new_check"
        assert out.status == fresh.status
    finally:
        probe.close()


def test_queued_to_running_transition_is_lease_fenced(
    db, tenant, user, recording_roster,
):
    """§7.2 queued → running is a worker write like any other: a worker
    whose lease was already invalidated cannot flip the run to running, and
    never reaches the geocoder."""
    calls, install = recording_roster
    install()
    run, job = _queued_pair(db, tenant, user)
    claimed = _claim(db)
    token = claimed.lease_token

    thief = SessionLocal()
    try:
        thief.get(Job, job.id).lease_token = None
        thief.commit()
    finally:
        thief.close()

    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=token,
    )
    assert calls == []                     # no provider call at all
    assert out.status == "queued"
    probe = SessionLocal()
    try:
        assert probe.get(SiteCheckRun, run.id).status == "queued"
        assert probe.get(SiteCheckRun, run.id).started_at is None
    finally:
        probe.close()


def test_completed_run_persists_terminal_progress_in_the_job(
    client, db, tenant, user, hdrs, recording_roster,
):
    """Finding 3's terminal-progress leg on the FULL-RUN path: the reviewer
    saw a completed lookup whose detail still said
    `stage=summarizing,status=running`. Terminal progress now rides the
    terminal job transaction, so a succeeded job can never carry a
    running-looking stage — on the row OR on the detail poll."""
    _calls, install = recording_roster
    install()
    run, _job = _queued_pair(db, tenant, user)
    claimed = _claim(db)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id, lease_token=claimed.lease_token,
    )
    assert out.status == "completed"
    db.expire_all()
    finished = db.get(Job, claimed.id)
    assert finished.status == "succeeded"
    assert finished.progress["stage"] == "completed"
    assert finished.progress["status"] == "complete"

    detail = client.get(f"/api/site-check/runs/{run.id}", headers=hdrs).json()
    assert detail["status"] == "completed"
    assert detail["progress"]["stage"] == "completed"
    assert detail["progress"]["status"] == "complete"
    assert detail["retry_active"] is False


def test_failed_run_persists_terminal_progress_in_the_job(
    client, db, tenant, user, recording_roster, hdrs, monkeypatch,
):
    """Same guarantee on the failure path: a failed job never leaves a
    running-looking stage behind."""
    _calls, _install = recording_roster

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            return _adapter_result(source_id, {}, errors=["no match"])
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)
    run, _job = _queued_pair(db, tenant, user)
    claimed = _claim(db)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id, lease_token=claimed.lease_token,
    )
    assert out.status == "failed" and out.error_code == "geocode_failed"
    db.expire_all()
    assert db.get(Job, claimed.id).progress["status"] == "failed"
    detail = client.get(f"/api/site-check/runs/{run.id}", headers=hdrs).json()
    assert detail["progress"]["status"] == "failed"
    assert detail["retry_active"] is False


# ── Finding 2: cancel vs terminal completion is ONE atomic race ────────


def test_completion_wins_writes_no_cancel_mutation_and_no_audit(
    client, db, tenant, user, hdrs, monkeypatch,
):
    """THE round-4 reproduction, inverted. Worker completion commits after
    the endpoint read `running` but before it mutates: the endpoint answers
    with the ACTUAL state, the completed row gains NO cancel_requested_at,
    and NO `site_check.run_cancelled` audit row is written."""
    from app.api import site_check as api

    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()

    real_get_run = api._get_run

    def get_run_then_complete(session, current, run_id):
        found = real_get_run(session, current, run_id)
        winner = SessionLocal()
        try:  # the worker's terminal transaction commits FIRST
            target = winner.get(SiteCheckRun, run_id)
            target.status = "completed"
            target.finished_at = _now()
            terminal = winner.get(Job, job.id)
            terminal.status = "succeeded"
            terminal.finished_at = _now()
            winner.commit()
        finally:
            winner.close()
        return found

    monkeypatch.setattr(api, "_get_run", get_run_then_complete)
    resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
    assert resp.status_code == 202
    assert resp.json()["status"] == "completed"  # the ACTUAL final state

    db.expire_all()
    final = db.get(SiteCheckRun, run.id)
    assert final.status == "completed"
    assert final.cancel_requested_at is None      # the loser mutated nothing
    assert _audits(db, "site_check.run_cancelled", run.id) == []


def test_cancel_wins_then_the_worker_honors_it_with_one_audit(
    client, db, tenant, user, hdrs, recording_roster,
):
    """Cancel commits first: the flag + the ONE audit row land, and the
    worker's next checkpoint terminalizes run + job `cancelled` together —
    without writing a second cancellation audit row."""
    calls, install = recording_roster
    install()
    run, _job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()
    claimed = _claim(db)

    resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).cancel_requested_at is not None
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1

    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id, lease_token=claimed.lease_token,
    )
    assert out.status == "cancelled"
    assert calls == []                       # pre-Wave-0 checkpoint
    db.expire_all()
    assert db.get(Job, claimed.id).status == "cancelled"
    assert db.get(Job, claimed.id).progress["status"] == "cancelled"
    # STILL exactly one cancellation audit row (§8.7).
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1


def test_two_simultaneous_first_cancels_write_exactly_one_audit(
    db, tenant, user, hdrs,
):
    """Two concurrent FIRST cancels both pass the null check; only the
    transaction that commits the mutation writes the audit row (§8.5/§8.7)."""
    run, _job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()

    barrier = threading.Barrier(2)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def cancel():
        local = TestClient(app, base_url="http://127.0.0.1")
        barrier.wait(timeout=10)
        resp = local.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
        with lock:
            results.append((resp.status_code, resp.json().get("status")))

    threads = [threading.Thread(target=cancel) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2
    assert {code for code, _ in results} == {202}
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).cancel_requested_at is not None
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1


# ── Finding 3: one retry job owns its whole source list ────────────────


_TRANSIENT = "The public site timed out (503 server error)"


def _completed_run_with_two_failed_sources(db, tenant, user):
    """A completed run whose fema AND soils results are transient failures —
    both classify as retryable, so an omitted-body retry picks up BOTH."""
    run, job = _queued_pair(db, tenant, user, address="9400 Cameron Rd, Austin TX")
    job.status = "succeeded"
    job.finished_at = _now()
    old = (_now() - timedelta(days=3)).isoformat()

    def payload(source_id, facts, errors, access, coverage):
        return {
            "source_id": source_id,
            "source_access_status": access,
            "data_confidence_status": "verified_public_fact",
            "coverage_status": coverage,
            "normalized_facts": facts,
            "errors": errors,
            "fetched_at": old,
            "result_schema_version": registry.result_schema_version(source_id),
        }

    run.status = "completed"
    run.lat, run.lng, run.county = 30.28, -97.72, "travis"
    run.finished_at = _now()
    run.adapter_results = {
        "arcgis_geocode": payload(
            "arcgis_geocode",
            {"lat": 30.28, "lng": -97.72, "county": "travis",
             "source": "nominatim"},
            [], "PROGRAMMATIC_PUBLIC", "covered",
        ),
        "census_acs": payload(
            "census_acs", {"population_1mi": 12_000},
            [], "PROGRAMMATIC_PUBLIC", "covered",
        ),
        "fema_nfhl": payload(
            "fema_nfhl", {}, [_TRANSIENT],
            "PROGRAMMATIC_PUBLIC_BUT_FRAGILE", "partial",
        ),
        "soil_intel": payload(
            "soil_intel", {}, [_TRANSIENT],
            "PROGRAMMATIC_PUBLIC_BUT_FRAGILE", "partial",
        ),
    }
    run.sources_queried = [
        {"source": s, "status": "fetched"}
        for s in ("arcgis_geocode", "census_acs", "fema_nfhl", "soil_intel")
    ]
    run.sources_failed = [
        {"source": "fema_nfhl", "error": _TRANSIENT, "failed_at": old},
        {"source": "soil_intel", "error": _TRANSIENT, "failed_at": old},
    ]
    run.derived = {"card_narratives": {"census_acs": "12,000 people nearby."}}
    db.commit()
    db.refresh(run)
    assert {
        row["source_id"]
        for row in sc_service.retryable_failed_sources_for_run(run)
    } == {"fema_nfhl", "soil_intel"}
    return run


def test_omitted_body_retry_persists_every_source_through_the_worker(
    client, db, tenant, user, hdrs, monkeypatch,
):
    """THE round-4 reproduction, inverted, through the REAL API + registered
    worker handler: an omitted-body retry over TWO retryable sources keeps
    BOTH results, terminalizes the job exactly once, exposes TERMINAL
    progress on the detail poll, and writes ONE accepted-retry audit row."""
    run = _completed_run_with_two_failed_sources(db, tenant, user)

    fetched: list[str] = []

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            fetched.append(source_id)
            return _adapter_result(source_id, {"refreshed": source_id})
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)

    resp = client.post(
        f"/api/site-check/runs/{run.id}/retry-sources", headers=hdrs,
    )  # omitted body = every currently retryable source
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    db.expire_all()
    assert set(db.get(Job, job_id).payload["retry_source_ids"]) == {
        "fema_nfhl", "soil_intel",
    }

    # §8.3 `retry_active` is TRUE while the retry job is non-terminal.
    queued_detail = client.get(
        f"/api/site-check/runs/{run.id}", headers=hdrs,
    ).json()
    assert queued_detail["retry_active"] is True

    claimed = job_queue.claim_next_job(db, worker_id="w-retry")
    assert claimed is not None and claimed.id == job_id
    running_detail = client.get(
        f"/api/site-check/runs/{run.id}", headers=hdrs,
    ).json()
    assert running_detail["retry_active"] is True

    job_queue.get_handler("site_check")(db, claimed)

    db.expire_all()
    out = db.get(SiteCheckRun, run.id)
    assert sorted(fetched) == ["fema_nfhl", "soil_intel"]
    # BOTH results persisted — the second source no longer rolls back.
    for source_id in ("fema_nfhl", "soil_intel"):
        entry = out.adapter_results[source_id]
        assert entry["normalized_facts"] == {"refreshed": source_id}
        assert not entry["errors"]
    assert out.sources_failed == []
    retried = [q for q in out.sources_queried if q.get("status") == "retried"]
    assert sorted(q["source"] for q in retried) == ["fema_nfhl", "soil_intel"]
    # Untouched sources preserved.
    assert out.adapter_results["census_acs"]["normalized_facts"] == {
        "population_1mi": 12_000,
    }

    # Terminalized EXACTLY once, with terminal progress in the same
    # transaction — the worker's mark_done backstop is a no-op.
    finished = db.get(Job, job_id)
    assert finished.status == "succeeded"
    assert finished.progress["status"] == "complete"
    assert finished.progress["completed_sources"] == 2
    assert job_queue.mark_done(db, job_id, claimed.lease_token) is False

    # The detail poll the app runs for 180 s shows a TERMINAL progress.
    detail = client.get(f"/api/site-check/runs/{run.id}", headers=hdrs).json()
    assert detail["status"] == "completed"
    assert detail["progress"]["status"] == "complete"
    assert detail["progress"]["stage"] == "source_retry"
    assert detail["retryable_failed_sources"] == []
    # …and FALSE the moment the retry job terminalizes (the client's settle
    # signal — never derived from the progress JSON).
    assert detail["retry_active"] is False

    # §8.7: exactly ONE accepted-retry audit row; one completion row per
    # retried source.
    assert len(_audits(db, "site_check.run_retry", run.id)) == 1
    assert len(_audits(db, "site_check.run_retry_completed", run.id)) == 2


def test_still_failing_retry_stops_immediately_with_terminal_progress(
    client, db, tenant, user, hdrs, monkeypatch,
):
    """A retry whose sources are STILL down also stops immediately: the job
    is terminal, progress reads `failed`, and the sources stay retryable."""
    run = _completed_run_with_two_failed_sources(db, tenant, user)

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            raise RuntimeError("the public site timed out again (503)")
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)
    resp = client.post(
        f"/api/site-check/runs/{run.id}/retry-sources", headers=hdrs,
    )
    job_id = resp.json()["job_id"]
    assert client.get(
        f"/api/site-check/runs/{run.id}", headers=hdrs,
    ).json()["retry_active"] is True
    claimed = job_queue.claim_next_job(db, worker_id="w-retry")
    job_queue.get_handler("site_check")(db, claimed)

    db.expire_all()
    finished = db.get(Job, job_id)
    assert finished.status == "succeeded"        # the JOB ran fine…
    assert finished.progress["status"] == "failed"  # …the sources did not
    assert finished.progress["failed_sources_count"] == 2
    detail = client.get(f"/api/site-check/runs/{run.id}", headers=hdrs).json()
    assert detail["progress"]["status"] == "failed"
    # A still-failed retry stops IMMEDIATELY too — no 180 s poll.
    assert detail["retry_active"] is False
    assert {
        row["source_id"] for row in detail["retryable_failed_sources"]
    } == {"fema_nfhl", "soil_intel"}


def test_retry_abandons_mid_list_when_the_lease_is_taken(
    db, tenant, user, monkeypatch,
):
    """A retry that loses its lease between two sources rolls the staged
    unit back and makes NO further provider call."""
    run = _completed_run_with_two_failed_sources(db, tenant, user)
    _run, job, created = sc_service.enqueue_source_retry(
        db, tenant_id=tenant.id, run_id=run.id, requested_by=user.id,
    )
    assert created is True
    claimed = job_queue.claim_next_job(db, worker_id="w-retry")
    ordered = list(claimed.payload["retry_source_ids"])
    fetched: list[str] = []

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            fetched.append(source_id)
            if source_id == ordered[0]:
                thief = SessionLocal()
                try:
                    thief.get(Job, job.id).lease_token = None
                    thief.commit()
                finally:
                    thief.close()
            return _adapter_result(source_id, {"refreshed": source_id})
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)
    job_queue.get_handler("site_check")(db, claimed)

    assert fetched == [ordered[0]]        # stopped before the second source
    probe = SessionLocal()
    try:
        fresh = probe.get(SiteCheckRun, run.id)
        # Nothing staged: the first source's result rolled back with the fence.
        assert fresh.adapter_results[ordered[0]]["errors"] == [_TRANSIENT]
        assert probe.get(Job, job.id).status == "running"
        assert not probe.scalars(
            select(AuditLog).where(
                AuditLog.action == "site_check.run_retry_completed"
            )
        ).all()
    finally:
        probe.close()


# ── Finding 3: retry admission is serialized ──────────────────────────


def test_concurrent_same_source_admissions_create_one_job_and_one_audit(
    db, tenant, user,
):
    """THE round-4 barrier reproduction, inverted: two concurrent same-source
    admissions yield ONE job, one accepted audit row, and one created=True."""
    run = _completed_run_with_two_failed_sources(db, tenant, user)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def admit():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            _r, job, created = sc_service.enqueue_source_retry(
                session, tenant_id=tenant.id, run_id=run.id,
                requested_by=user.id, source_ids=["fema_nfhl"],
            )
            with lock:
                outcomes.append((job.id, created))
        except Exception as exc:  # pragma: no cover - failure diagnostics
            with lock:
                errors.append(exc)
        finally:
            session.rollback()
            session.close()

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(outcomes) == 2
    assert len({job_id for job_id, _ in outcomes}) == 1      # the SAME job
    assert sorted(created for _, created in outcomes) == [False, True]
    db.expire_all()
    active = db.scalars(
        select(Job).where(Job.status.in_(["queued", "running"]))
    ).all()
    assert len([j for j in active if (j.payload or {}).get("retry_source_ids")]) == 1
    assert len(_audits(db, "site_check.run_retry", run.id)) == 1


def test_concurrent_different_source_admission_is_refused(
    db, tenant, user,
):
    """Different sources while one retry runs → exactly one accepted job and
    one 409 refusal (§8.4 serialization)."""
    run = _completed_run_with_two_failed_sources(db, tenant, user)
    barrier = threading.Barrier(2)
    accepted: list[str] = []
    refused: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def admit(source_id):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            _r, job, created = sc_service.enqueue_source_retry(
                session, tenant_id=tenant.id, run_id=run.id,
                requested_by=user.id, source_ids=[source_id],
            )
            with lock:
                accepted.append((job.id, created))
        except sc_service.RetryRefused as exc:
            with lock:
                refused.append(str(exc))
        except Exception as exc:  # pragma: no cover - failure diagnostics
            with lock:
                errors.append(exc)
        finally:
            session.rollback()
            session.close()

    threads = [
        threading.Thread(target=admit, args=("fema_nfhl",)),
        threading.Thread(target=admit, args=("soil_intel",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(accepted) == 1 and accepted[0][1] is True
    assert refused == ["A refresh is already running for this lookup."]
    db.expire_all()
    assert len(_audits(db, "site_check.run_retry", run.id)) == 1


def test_active_retry_job_invariant_is_database_enforced(db, tenant, user):
    """§8.4 is DB-enforced, not check-then-insert: a second active retry job
    for the same run cannot be inserted even bypassing the service."""
    from sqlalchemy.exc import IntegrityError

    run = _completed_run_with_two_failed_sources(db, tenant, user)

    def retry_job():
        return Job(
            tenant_id=tenant.id, kind="site_check", status="queued",
            max_attempts=3,
            payload={"run_id": run.id, "retry_source_ids": ["fema_nfhl"]},
        )

    db.add(retry_job())
    db.commit()
    db.add(retry_job())
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ── Cancel vs STALE RECOVERY (reviewer round-2 finding, 2026-08-01) ────────
# Recovery used to lock the job first and read the run unlocked, so it could
# commit failed/worker_lost OVER a cancellation the API had already accepted
# and audited. Both paths now share one run→job lock order.

def _make_stale(db, job, *, at_cap: bool):
    """Age the job past the stale cutoff, at or below the attempt cap."""
    job.status = "running"
    job.attempts = job.max_attempts if at_cap else max(0, job.max_attempts - 1)
    job.heartbeat_at = _now() - timedelta(
        seconds=get_settings().job_stale_after_seconds + 60
    )
    db.commit()


@pytest.mark.parametrize("at_cap", [True, False], ids=["at_cap", "below_cap"])
def test_accepted_cancel_survives_stale_recovery(
    client, db, tenant, user, hdrs, at_cap,
):
    """THE round-2 reproduction, inverted. Real sequence: the user cancels a
    RUNNING lookup (accepted, audited), the worker meanwhile died, and the
    sweeper reaches the now-stale job afterwards. Recovery must observe the
    accepted cancellation under the run lock and terminalize run + job
    `cancelled` — never failed/worker_lost, never requeued — at AND below the
    attempt cap, preserving the single request-time audit row."""
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    job.status = "running"
    job.attempts = job.max_attempts if at_cap else max(0, job.max_attempts - 1)
    job.heartbeat_at = _now()          # still fresh: cancel is accepted first
    db.commit()

    resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
    assert resp.status_code == 202
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).cancel_requested_at is not None

    # The worker never came back; the job goes stale and the sweeper arrives.
    stale = db.get(Job, job.id)
    stale.heartbeat_at = _now() - timedelta(
        seconds=get_settings().job_stale_after_seconds + 60
    )
    db.commit()

    recovery = SessionLocal()
    try:
        outcome = job_queue.recover_stale_job(recovery, job.id)
    finally:
        recovery.close()
    assert outcome == "cancelled"

    db.expire_all()
    final_run, final_job = db.get(SiteCheckRun, run.id), db.get(Job, job.id)
    assert final_run.status == "cancelled"
    assert final_run.error_code != "worker_lost"
    assert final_job.status == "cancelled"
    assert final_job.error_code == "cancelled_by_user"
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1


def test_cancel_commits_while_recovery_waits_on_the_run_lock(
    db, tenant, user,
):
    """The reviewer's overlapping order, made deterministic. The cancelling
    transaction holds the run lock and commits its flag; stale recovery,
    started meanwhile and AT THE ATTEMPT CAP (so its default outcome would be
    worker_lost), blocks at its prelock on that same run row, then re-reads
    under the lock and yields `cancelled`. Before the shared run→job lock
    order, recovery held only the job lock and committed worker_lost over the
    accepted cancellation."""
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    job.status = "running"
    job.attempts = job.max_attempts          # default outcome: worker_lost
    job.heartbeat_at = _now() - timedelta(
        seconds=get_settings().job_stale_after_seconds + 60
    )
    db.commit()

    outcome: list[str | None] = []
    started = threading.Event()

    def recover():
        session = SessionLocal()
        try:
            started.set()
            outcome.append(job_queue.recover_stale_job(session, job.id))
        finally:
            session.close()

    cancel_session = SessionLocal()
    worker = threading.Thread(target=recover)
    try:
        # The cancelling transaction takes the run lock FIRST and keeps it.
        locked = cancel_session.execute(
            select(SiteCheckRun)
            .where(SiteCheckRun.id == run.id)
            .with_for_update()
        ).scalar_one()
        worker.start()
        started.wait(timeout=5)
        time.sleep(0.3)
        assert not outcome, "recovery decided without holding the run lock"

        locked.cancel_requested_at = _now()
        cancel_session.commit()              # cancel wins, lock released
    finally:
        cancel_session.close()
    worker.join(timeout=10)

    assert outcome == ["cancelled"]
    db.expire_all()
    final_run, final_job = db.get(SiteCheckRun, run.id), db.get(Job, job.id)
    assert final_run.status == "cancelled"
    assert final_run.error_code != "worker_lost"
    assert final_job.status == "cancelled"


@pytest.mark.parametrize("at_cap", [True, False], ids=["at_cap", "below_cap"])
def test_stale_recovery_wins_then_cancel_mutates_nothing(
    client, db, tenant, user, hdrs, at_cap,
):
    """The other winner order: recovery commits first. At the cap the run is
    terminal `failed/worker_lost` → the cancel request answers with the ACTUAL
    state and writes no flag and no audit. Below the cap the run is requeued
    (still active), so a cancellation is legitimately accepted."""
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()
    _make_stale(db, job, at_cap=at_cap)

    recovery = SessionLocal()
    try:
        outcome = job_queue.recover_stale_job(recovery, job.id)
    finally:
        recovery.close()
    assert outcome == ("failed" if at_cap else "requeued")

    resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
    db.expire_all()
    final = db.get(SiteCheckRun, run.id)
    audits = _audits(db, "site_check.run_cancelled", run.id)

    if at_cap:
        assert resp.status_code in (202, 409)
        if resp.status_code == 202:
            assert resp.json()["status"] == "failed"   # the ACTUAL state
        assert final.status == "failed"
        assert final.error_code == "worker_lost"
        assert final.cancel_requested_at is None       # loser mutated nothing
        assert audits == []
    else:
        assert resp.status_code == 202
        assert final.status in ("queued", "cancelled")
        assert final.cancel_requested_at is not None
        assert len(audits) == 1


def test_read_path_failure_loses_to_a_cancel_committed_after_it_loaded(
    client, db, tenant, user, hdrs,
):
    """Reviewer round 3, inverted. The read-path session loads a RUNNING run
    with no cancellation flag (its ORM instance is now cached), a concurrent
    cancel commits the flag + audit, and only THEN does the read path
    terminalize. Taking the lock is not enough — the decision must be made on
    values re-read under it (populate_existing), or the accepted cancellation
    is overwritten with worker_lost."""
    run, _job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()

    reader = SessionLocal()
    try:
        # (1) the read path loads the run BEFORE any cancellation exists
        cached = reader.get(SiteCheckRun, run.id)
        assert cached.cancel_requested_at is None
        assert cached.status == "running"

        # (2) a real cancel request commits the flag + its single audit
        resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
        assert resp.status_code == 202
        db.expire_all()
        assert db.get(SiteCheckRun, run.id).cancel_requested_at is not None

        # (3) the read path now terminalizes from its stale in-session copy
        out = sc_service._fail_active_run(
            reader, cached,
            error_code="worker_lost",
            failure_reason="The lookup stopped unexpectedly. Run it again.",
        )
        assert out.status == "cancelled", (
            "read path decided on pre-lock values and overwrote the "
            "accepted cancellation"
        )
        assert out.error_code != "worker_lost"
    finally:
        reader.close()

    db.expire_all()
    final = db.get(SiteCheckRun, run.id)
    assert final.status == "cancelled"
    assert final.error_code != "worker_lost"
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1


def test_read_path_failure_first_then_cancel_mutates_nothing(
    client, db, tenant, user, hdrs,
):
    """The opposite winner order: the read path terminalizes `worker_lost`
    first; the later cancel request must observe the terminal state and write
    NO cancellation flag and NO audit row."""
    run, _job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.commit()

    reader = SessionLocal()
    try:
        cached = reader.get(SiteCheckRun, run.id)
        out = sc_service._fail_active_run(
            reader, cached,
            error_code="worker_lost",
            failure_reason="The lookup stopped unexpectedly. Run it again.",
        )
        assert out.status == "failed"
    finally:
        reader.close()

    resp = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs)
    assert resp.status_code in (202, 409)
    if resp.status_code == 202:
        assert resp.json()["status"] == "failed"      # the ACTUAL state

    db.expire_all()
    final = db.get(SiteCheckRun, run.id)
    assert final.status == "failed"
    assert final.error_code == "worker_lost"
    assert final.cancel_requested_at is None
    assert _audits(db, "site_check.run_cancelled", run.id) == []


def test_recovery_cancelled_job_carries_terminal_progress(db, tenant, user):
    """Reviewer round 3 non-blocking note, closed: the recovery-cancelled
    branch must set terminal progress in the SAME transaction (§7.8) — a
    terminal job may never retain a running-looking stage."""
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    run.cancel_requested_at = _now()
    job.status = "running"
    job.progress = {"stage": "sources", "status": "running"}
    job.heartbeat_at = _now() - timedelta(
        seconds=get_settings().job_stale_after_seconds + 60
    )
    db.commit()

    recovery = SessionLocal()
    try:
        assert job_queue.recover_stale_job(recovery, job.id) == "cancelled"
    finally:
        recovery.close()

    db.expire_all()
    final = db.get(Job, job.id)
    assert final.status == "cancelled"
    assert final.progress["status"] == "cancelled"
    assert final.progress["stage"] != "sources"
