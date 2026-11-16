"""Site Check API contract tests (docs/specs/site_check.md §7.5, §8, §17).

Postgres only (house conftest). Covers: atomic enqueue + rollback,
enqueue-supersede + lease invalidation, concurrent same-subject creates,
cancel races, retry eligibility matrix + idempotency, delete rules,
tenancy 404s both directions, the §8.3 two-class cards rule, and the
§9.4 freshness fields.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import job_queue
from app.db import SessionLocal
from app.models import AuditLog, Job, SiteCheckProviderUsage, SiteCheckRun
from app.services.site_check import registry
from app.services.site_check import service as sc_service


def NOW():
    return datetime.now(UTC)


@pytest.fixture
def tenant_a(local_tenant_user):
    # Every HTTP request resolves to the one fixed local office — the
    # primary flow in this file goes through the API, so "tenant_a" must
    # be that same office, not a second one of its own.
    return local_tenant_user[0]


@pytest.fixture
def tenant_b(make_tenant):
    # A genuinely separate office, for the tenant-isolation checks below,
    # which assert separation at the service layer — PropRisk has only one
    # HTTP identity, so a second office can't be reached over HTTP.
    return make_tenant("Beta Office")


@pytest.fixture
def user_a(local_tenant_user):
    return local_tenant_user[1]


@pytest.fixture
def user_b(make_user, tenant_b):
    return make_user(tenant_b, "beta@example.com")


@pytest.fixture
def hdrs_a(login, user_a):
    return login("alpha@example.com")


def _result(source_id, facts, *, errors=None, coverage="covered",
            access="PROGRAMMATIC_PUBLIC", fetched_at=None, schema_version=None):
    version = (
        schema_version
        if schema_version is not None
        else registry.result_schema_version(source_id)
    )
    return {
        "source_id": source_id,
        "source_access_status": access,
        "data_confidence_status": "verified_public_fact",
        "coverage_status": coverage,
        "normalized_facts": facts,
        "errors": errors or [],
        "fetched_at": (fetched_at or NOW()).isoformat(),
        "result_schema_version": version,
    }


def _mk_run(db, tenant, user, *, address="100 Congress Ave, Austin TX",
            status="completed", parcel_id=None, adapter_results=None,
            sources_failed=None, mode="check", finished=True):
    run = SiteCheckRun(
        tenant_id=tenant.id,
        requested_by=user.id,
        address=address,
        normalized_address=sc_service.normalize_address(address),
        parcel_id=parcel_id,
        county="travis",
        status=status,
        mode=mode,
        adapter_results=adapter_results or {},
        sources_queried=[],
        sources_failed=sources_failed or [],
        derived={},
        finished_at=NOW() if finished and status not in ("queued", "running") else None,
    )
    db.add(run)
    db.commit()
    return run


def _audits(db, action, entity_id=None):
    stmt = select(AuditLog).where(AuditLog.action == action)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return db.scalars(stmt).all()


def _job_for(db, run):
    return next(
        (j for j in db.scalars(select(Job).where(Job.kind == "site_check")).all()
         if (j.payload or {}).get("run_id") == run.id),
        None,
    )


# ── §8.1 create + §7.3 atomic enqueue ─────────────────────────────────


def test_create_returns_202_with_run_job_and_audit(client, db, hdrs_a):
    resp = client.post("/api/site-check", json={"address": "701 Brazos St, Austin TX"},
                       headers=hdrs_a)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    run = db.get(SiteCheckRun, body["run_id"])
    job = db.get(Job, body["job_id"])
    assert run.status == "queued"
    assert run.normalized_address == "701 brazos st, austin tx"
    assert run.mode == "check"
    assert job.status == "queued"
    assert job.kind == "site_check"
    assert job.payload == {"run_id": run.id, "mode": "check"}
    assert len(_audits(db, "site_check.run_created", run.id)) == 1


def test_create_structural_validation_only(client, hdrs_a):
    assert client.post("/api/site-check", json={"address": "  "},
                       headers=hdrs_a).status_code == 422
    assert client.post("/api/site-check", json={"address": "x" * 513},
                       headers=hdrs_a).status_code == 422
    assert client.post("/api/site-check",
                       json={"address": "1 Main St", "parcel_id": "p" * 65},
                       headers=hdrs_a).status_code == 422
    resp = client.post("/api/site-check",
                       json={"address": "1 Main St", "mode": "retry_failed"},
                       headers=hdrs_a)
    assert resp.status_code == 422
    assert "error" in resp.json()


def test_atomic_enqueue_rolls_back_all_three(db, tenant_a, user_a, monkeypatch):
    """§7.3/§18: an injected failure inside the enqueue transaction leaves
    NO run, NO job, NO audit row."""
    real_log = sc_service.audit.log

    def boom(session, **kwargs):
        if kwargs.get("action") == "site_check.run_created":
            raise RuntimeError("injected audit failure")
        return real_log(session, **kwargs)

    monkeypatch.setattr(sc_service.audit, "log", boom)
    with pytest.raises(RuntimeError):
        sc_service.create_run(
            db, tenant_id=tenant_a.id, requested_by=user_a.id,
            address="900 Lavaca St, Austin TX",
        )
    db.rollback()
    assert db.scalars(select(SiteCheckRun)).all() == []
    assert db.scalars(select(Job)).all() == []
    assert _audits(db, "site_check.run_created") == []


# ── §7.5 supersede ────────────────────────────────────────────────────


def test_supersede_cancels_old_run_and_invalidates_lease(client, db, hdrs_a):
    first = client.post("/api/site-check", json={"address": "12 Oak Ln"},
                        headers=hdrs_a).json()
    old_run = db.get(SiteCheckRun, first["run_id"])
    # Simulate a worker holding the old job's lease.
    claimed = job_queue.claim_next_job(db, worker_id="w-old")
    assert claimed is not None and claimed.id == first["job_id"]
    old_token = claimed.lease_token
    old_run.adapter_results = {"fema_nfhl": _result("fema_nfhl", {"zone": "X"})}
    db.commit()

    second = client.post("/api/site-check", json={"address": "12 Oak Ln"},
                         headers=hdrs_a).json()
    db.expire_all()
    old_run = db.get(SiteCheckRun, first["run_id"])
    old_job = db.get(Job, first["job_id"])
    assert old_run.status == "cancelled"
    assert old_run.error_code == "superseded_by_new_check"
    # Staged results retained and readable.
    assert "fema_nfhl" in (old_run.adapter_results or {})
    assert old_job.status == "cancelled"
    assert old_job.lease_token is None
    # The stale worker's fenced writes match 0 rows (late-writer guarantee)
    # and raise LeaseLost so it abandons instead of carrying on.
    with pytest.raises(job_queue.LeaseLost):
        job_queue.write_progress(db, old_job.id, old_token, {"x": 1})
    db.rollback()

    new_run = db.get(SiteCheckRun, second["run_id"])
    assert new_run.status == "queued"
    # Exact audit counts: one cancel (old) + one create (new) for request 2.
    assert len(_audits(db, "site_check.run_cancelled", old_run.id)) == 1
    assert len(_audits(db, "site_check.run_created")) == 2


def test_concurrent_same_subject_creates_leave_exactly_one_active(
    db, tenant_a, user_a,
):
    barrier = threading.Barrier(2)
    errors = []

    def create():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            sc_service.create_run(
                session, tenant_id=tenant_a.id, requested_by=user_a.id,
                address="77 Race Condition Rd",
            )
        except Exception as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []

    runs = db.scalars(
        select(SiteCheckRun).where(SiteCheckRun.tenant_id == tenant_a.id)
    ).all()
    active = [r for r in runs if r.status in ("queued", "running")]
    assert len(runs) == 2
    assert len(active) == 1  # the §7.5 invariant
    # Audits sum correctly: 2 creates + 1 supersede-cancel.
    assert len(_audits(db, "site_check.run_created")) == 2
    assert len(_audits(db, "site_check.run_cancelled")) == 1


def test_partial_unique_index_refuses_second_active_subject_row(
    db, tenant_a, user_a,
):
    """§7.5: the one-active-run invariant is DB-enforced, not
    check-then-insert."""
    from sqlalchemy.exc import IntegrityError

    def active_row():
        return SiteCheckRun(
            tenant_id=tenant_a.id, requested_by=user_a.id,
            address="9 Contested Ct",
            normalized_address=sc_service.normalize_address("9 Contested Ct"),
            status="queued", mode="check",
        )

    db.add(active_row())
    db.commit()
    db.add(active_row())
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # A TERMINAL same-subject row is fine (the index is partial).
    done = active_row()
    done.status = "completed"
    db.add(done)
    db.commit()


def test_unique_index_violation_retries_once_then_plain_409(
    client, db, hdrs_a, monkeypatch,
):
    """Both attempts losing the index race ends in the plain 409 (§7.5)."""
    from sqlalchemy.exc import IntegrityError

    attempts = []

    def always_conflicting_insert(session, **kwargs):
        attempts.append(1)
        raise IntegrityError("duplicate", None, Exception(
            "uq_site_check_runs_active_subject"
        ))

    monkeypatch.setattr(sc_service, "_insert_run_and_job",
                        always_conflicting_insert)
    resp = client.post("/api/site-check", json={"address": "9 Contested Ct"},
                       headers=hdrs_a)
    assert resp.status_code == 409
    assert len(attempts) == 2  # one retry, then refused
    assert resp.json() == {"error": "A lookup for this address is already running."}


# ── §7.5/§8.5 cancel races ────────────────────────────────────────────


def test_cancel_queued_run_cancels_both_immediately(client, db, hdrs_a):
    created = client.post("/api/site-check", json={"address": "5 Quick St"},
                          headers=hdrs_a).json()
    resp = client.post(f"/api/site-check/runs/{created['run_id']}/cancel",
                       headers=hdrs_a)
    assert resp.status_code == 202
    db.expire_all()
    run = db.get(SiteCheckRun, created["run_id"])
    job = db.get(Job, created["job_id"])
    assert run.status == "cancelled"
    assert job.status == "cancelled"
    assert run.cancel_requested_at is not None
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1
    # Cancel of a now-terminal run → 409 plain message.
    resp = client.post(f"/api/site-check/runs/{created['run_id']}/cancel",
                       headers=hdrs_a)
    assert resp.status_code == 409
    assert resp.json() == {"error": "This lookup already finished."}


def test_cancel_running_run_sets_flag_and_is_idempotent(client, db, tenant_a,
                                                        user_a, hdrs_a):
    run = _mk_run(db, tenant_a, user_a, status="running", finished=False)
    job = Job(tenant_id=tenant_a.id, kind="site_check",
              payload={"run_id": run.id, "mode": "check"}, status="running",
              attempts=1, max_attempts=2, lease_token="tok-1",
              claimed_by="w1", claimed_at=NOW(), heartbeat_at=NOW())
    db.add(job)
    db.commit()

    first = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs_a)
    assert first.status_code == 202
    db.expire_all()
    run = db.get(SiteCheckRun, run.id)
    assert run.status == "running"  # worker honors the flag at a checkpoint
    assert run.cancel_requested_at is not None
    assert db.get(Job, job.id).status == "running"  # job untouched (§7.5)
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1

    repeat = client.post(f"/api/site-check/runs/{run.id}/cancel", headers=hdrs_a)
    assert repeat.status_code == 202  # idempotent
    assert len(_audits(db, "site_check.run_cancelled", run.id)) == 1  # no second row


# ── §8.4 retry eligibility matrix ─────────────────────────────────────


def _failed_source_run(db, tenant, user, *, source="fema_nfhl",
                       errors=None, coverage="partial",
                       access="PROGRAMMATIC_PUBLIC_BUT_FRAGILE",
                       facts=None, extra_results=None, failed_at=None):
    errors = errors if errors is not None else ["HTTP 503 server error"]
    results = {
        "arcgis_geocode": _result("arcgis_geocode",
                                  {"lat": 30.1, "lng": -97.7, "county": "travis"}),
        source: _result(source, facts or {}, errors=errors,
                        coverage=coverage, access=access),
    }
    results.update(extra_results or {})
    return _mk_run(
        db, tenant, user, adapter_results=results,
        sources_failed=[{
            "source": source,
            "error": "; ".join(errors) if errors else "",
            "failed_at": (failed_at or NOW()).isoformat(),
        }] if errors else [],
    )


def test_retry_accepts_then_idempotent_same_job(client, db, tenant_a, user_a, hdrs_a):
    run = _failed_source_run(db, tenant_a, user_a)
    first = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                        json={"source_ids": ["fema_nfhl"]}, headers=hdrs_a)
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    job = db.get(Job, job_id)
    assert job.payload == {"run_id": run.id, "retry_source_ids": ["fema_nfhl"]}
    # Run STAYS completed while retrying.
    assert first.json()["status"] == "completed"
    assert len(_audits(db, "site_check.run_retry", run.id)) == 1

    second = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                         json={"source_ids": ["fema_nfhl"]}, headers=hdrs_a)
    assert second.status_code == 202
    assert second.json()["job_id"] == job_id  # SAME job, idempotent
    assert len(_audits(db, "site_check.run_retry", run.id)) == 1  # no second audit

    omitted = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                          json={}, headers=hdrs_a)
    assert omitted.status_code == 202
    assert omitted.json()["job_id"] == job_id


def test_retry_different_sources_while_one_runs_is_409(client, db, tenant_a,
                                                       user_a, hdrs_a):
    run = _failed_source_run(
        db, tenant_a, user_a,
        extra_results={
            "soil_intel": _result("soil_intel", {}, errors=["HTTP 502 bad gateway"],
                                  coverage="partial",
                                  access="PROGRAMMATIC_PUBLIC_BUT_FRAGILE"),
        },
    )
    run.sources_failed = [
        *run.sources_failed,
        {"source": "soil_intel", "error": "HTTP 502 bad gateway",
         "failed_at": NOW().isoformat()},
    ]
    db.commit()
    assert client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                       json={"source_ids": ["fema_nfhl"]},
                       headers=hdrs_a).status_code == 202
    resp = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                       json={"source_ids": ["soil_intel"]}, headers=hdrs_a)
    assert resp.status_code == 409
    assert resp.json() == {"error": "A refresh is already running for this lookup."}


@pytest.mark.parametrize("kwargs", [
    # Coverage says the jurisdiction is simply not covered — never retryable.
    {"coverage": "not_supported_in_jurisdiction", "access": "PROGRAMMATIC_PUBLIC",
     "errors": ["HTTP 503 server error"]},
    # Access is human-only.
    {"coverage": "partial", "access": "HTML_OR_PDF_ONLY",
     "errors": ["HTTP 503 server error"]},
    # Non-retryable error token.
    {"coverage": "partial", "access": "PROGRAMMATIC_PUBLIC",
     "errors": ["api key not configured"]},
])
def test_retry_non_retryable_states_409(client, db, tenant_a, user_a, hdrs_a, kwargs):
    run = _failed_source_run(db, tenant_a, user_a, **kwargs)
    resp = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                       json={"source_ids": ["fema_nfhl"]}, headers=hdrs_a)
    assert resp.status_code == 409
    assert "error" in resp.json()


def test_retry_cooldown_409_carries_retry_message(client, db, tenant_a,
                                                  user_a, hdrs_a):
    run = _failed_source_run(
        db, tenant_a, user_a,
        errors=["HTTP 429 rate limit"],
        failed_at=NOW(),  # still inside public_intel_rate_limit_cooldown_seconds
    )
    detail = client.get(f"/api/site-check/runs/{run.id}", headers=hdrs_a).json()
    rows = detail["retryable_failed_sources"]
    assert rows and rows[0]["retry_status"] == "cooldown"
    assert rows[0]["retry_after_seconds"] > 0
    resp = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                       json={"source_ids": ["fema_nfhl"]}, headers=hdrs_a)
    assert resp.status_code == 409
    assert resp.json()["error"]  # plain retry_message travels in the envelope


def test_retry_requires_completed_run(client, db, tenant_a, user_a, hdrs_a):
    run = _mk_run(db, tenant_a, user_a, status="failed")
    resp = client.post(f"/api/site-check/runs/{run.id}/retry-sources",
                       json={}, headers=hdrs_a)
    assert resp.status_code == 409
    assert resp.json() == {"error": "You can refresh sources only on a finished report."}


# ── §8.5 delete ───────────────────────────────────────────────────────


def test_delete_active_409_terminal_ok_usage_survives(client, db, tenant_a,
                                                      user_a, hdrs_a):
    active = _mk_run(db, tenant_a, user_a, address="2 Busy Rd",
                     status="queued", finished=False)
    job = Job(tenant_id=tenant_a.id, kind="site_check",
              payload={"run_id": active.id, "mode": "check"}, status="queued",
              attempts=0, max_attempts=2)
    db.add(job)
    db.commit()
    resp = client.delete(f"/api/site-check/runs/{active.id}", headers=hdrs_a)
    assert resp.status_code == 409
    assert resp.json() == {"error": "Cancel the running lookup first."}

    done = _mk_run(db, tenant_a, user_a, address="3 Done Rd")
    done_job = Job(tenant_id=tenant_a.id, kind="site_check",
                   payload={"run_id": done.id, "mode": "check"},
                   status="succeeded", attempts=1, max_attempts=2)
    usage_row = SiteCheckProviderUsage(
        tenant_id=tenant_a.id, run_id=done.id, provider="nominatim",
        service="geocode_stored", calls=1, billable_units=1, retried_calls=0,
        estimated_cost_microdollars=0,
    )
    db.add_all([done_job, usage_row])
    db.commit()
    done_id, done_job_id, usage_id = done.id, done_job.id, usage_row.id

    resp = client.delete(f"/api/site-check/runs/{done_id}", headers=hdrs_a)
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(SiteCheckRun, done_id) is None
    assert db.get(Job, done_job_id) is None  # job rows go with the run
    survivor = db.get(SiteCheckProviderUsage, usage_id)
    assert survivor is not None and survivor.run_id is None  # SET NULL
    assert len(_audits(db, "site_check.run_deleted", done_id)) == 1
    assert _audits(db, "site_check.run_deleted", done_id)[0].detail["address"] == "3 Done Rd"


# ── Tenancy: 404 both directions on EVERY endpoint ────────────────────


def test_cross_tenant_ids_404_both_directions(db, tenant_a, user_a,
                                              tenant_b, user_b):
    """PropRisk has only one signed-in office over HTTP, so this now checks
    tenant isolation at the service layer (every mutation endpoint 404s via
    `_get_run`, which is backed by `sc_service.get_run` — see
    app/api/site_check.py) instead of through two different signed-in
    users."""
    run_a = _mk_run(db, tenant_a, user_a, address="A St")
    run_b = _mk_run(db, tenant_b, user_b, address="B St")

    # A run from the "other" office is invisible by id in either direction.
    assert sc_service.get_run(db, tenant_id=tenant_a.id, run_id=run_b.id) is None
    assert sc_service.get_run(db, tenant_id=tenant_b.id, run_id=run_a.id) is None
    # Each office still sees its own run by id.
    assert sc_service.get_run(db, tenant_id=tenant_a.id, run_id=run_a.id).id == run_a.id
    assert sc_service.get_run(db, tenant_id=tenant_b.id, run_id=run_b.id).id == run_b.id

    # History/list is tenant-scoped too.
    mine_a = sc_service.get_history(db, tenant_id=tenant_a.id)
    assert [r.id for r in mine_a] == [run_a.id]
    mine_b = sc_service.get_history(db, tenant_id=tenant_b.id)
    assert [r.id for r in mine_b] == [run_b.id]


# ── §8.2 list + §8.3 detail (two-class cards rule, progress, freshness) ─


def _stub_cards_builder():
    def build(run, settings):
        return {
            "groups": [{"key": "flood", "title": "Flood & water", "cards": []}],
            "bottom_line": {"text": "Stub bottom line.", "kind": "deterministic",
                            "checked_at": None},
            "manual_checks": [],
        }
    return build


def test_list_newest_first_with_search(client, db, tenant_a, user_a, hdrs_a):
    _mk_run(db, tenant_a, user_a, address="100 Old Mill Rd")
    newer = _mk_run(db, tenant_a, user_a, address="200 New Bridge Ave")
    page = client.get("/api/site-check/runs", headers=hdrs_a).json()
    assert page["total"] == 2
    assert page["runs"][0]["id"] == newer.id  # newest first
    found = client.get("/api/site-check/runs?query=bridge", headers=hdrs_a).json()
    assert found["total"] == 1
    assert found["runs"][0]["id"] == newer.id


def test_detail_two_class_cards_rule(client, db, tenant_a, user_a, hdrs_a,
                                     monkeypatch):
    import app.api.site_check as api_mod
    monkeypatch.setattr(api_mod, "_resolve_cards_builder", _stub_cards_builder)

    staged = {"fema_nfhl": _result("fema_nfhl", {"zone": "X"})}
    completed = _mk_run(db, tenant_a, user_a, address="1 Done", adapter_results=staged)
    cancelled_partial = _mk_run(db, tenant_a, user_a, address="2 CancP",
                                status="cancelled", adapter_results=staged)
    cancelled_zero = _mk_run(db, tenant_a, user_a, address="3 CancZ",
                             status="cancelled")
    failed_zero = _mk_run(db, tenant_a, user_a, address="4 FailZ", status="failed")
    failed_partial = _mk_run(db, tenant_a, user_a, address="5 FailP",
                             status="failed", adapter_results=staged)

    def cards_of(run):
        return client.get(f"/api/site-check/runs/{run.id}", headers=hdrs_a).json()["cards"]

    assert cards_of(completed) is not None
    assert cards_of(cancelled_partial) is not None
    # The sole zero-staged exception: a terminal cancelled run still serves
    # the payload (all-not_checked scaffold).
    assert cards_of(cancelled_zero) is not None
    assert cards_of(failed_zero) is None  # plain failure only
    assert cards_of(failed_partial) is not None

    # Queued run: cards null, progress present with job-status vocabulary.
    created = client.post("/api/site-check", json={"address": "6 Queued Way"},
                          headers=hdrs_a).json()
    detail = client.get(f"/api/site-check/runs/{created['run_id']}",
                        headers=hdrs_a).json()
    assert detail["cards"] is None
    assert detail["progress"]["status"] == "queued"
    assert isinstance(detail["progress"]["elapsed_seconds"], int)


def test_detail_freshness_fields_derive_from_settings(client, db, tenant_a,
                                                      user_a, hdrs_a, monkeypatch):
    from app.config.settings import settings as settings_obj
    monkeypatch.setattr(settings_obj, "site_check_freshness_days", 10)
    monkeypatch.setattr(settings_obj, "site_check_expiring_notice_days", 3)

    def run_aged(days, address):
        fetched = NOW() - timedelta(days=days)
        return _mk_run(db, tenant_a, user_a, address=address, adapter_results={
            "fema_nfhl": _result("fema_nfhl", {"zone": "X"}, fetched_at=fetched),
        })

    current = run_aged(5, "10 Fresh St")     # age < F - notice (7)
    expiring = run_aged(8, "11 Amber St")    # F - notice <= age < F
    boundary = run_aged(7, "12 Edge St")     # age == F - notice exactly → expiring
    expired = run_aged(10, "13 Stale St")    # age == F exactly → expired

    def detail(run):
        return client.get(f"/api/site-check/runs/{run.id}", headers=hdrs_a).json()

    assert detail(current)["freshness"] == "current"
    assert detail(expiring)["freshness"] == "expiring"
    assert detail(boundary)["freshness"] == "expiring"
    d = detail(expired)
    assert d["freshness"] == "expired"
    assert d["freshness_days"] == 10  # the client interpolates the configured F
    # refresh_after = oldest checked + F days.
    oldest = datetime.fromisoformat(d["refresh_after"])
    assert abs((oldest - NOW()).total_seconds()) < 120  # 10d old + 10d ≈ now

    # List badges use the same ladder.
    page = client.get("/api/site-check/runs?query=stale", headers=hdrs_a).json()
    assert page["runs"][0]["freshness"] == "expired"
    assert page["runs"][0]["oldest_checked_at"] is not None
