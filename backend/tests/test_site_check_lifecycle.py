"""Site Check run-lifecycle wiring (docs/specs/site_check.md §7.4–§7.6,
§8.6, §11.3): kind registration, recovery-reconciler pairing, read-path
vs sweeper parity, the §7.5 worker cancel checkpoints, geocoder usage
attribution, and the narrative last-resort guard.

Adapters never hit the network: `_load_adapter_fetch` and the registry
roster are monkeypatched onto a two-source run.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import job_queue
from app.db import SessionLocal
from app.models import AuditLog, Job, SiteCheckProviderUsage, SiteCheckRun
from app.services.site_check import orchestrator, registry
from app.services.site_check import service as sc_service
from app.services.site_check.contract import AdapterResult
from app.services.site_check.worker_handler import (
    handle_site_check,
    reconcile_site_check_recovery,
)


def _now():
    return datetime.now(UTC)


@pytest.fixture
def tenant(make_tenant):
    return make_tenant("Lifecycle Office")


@pytest.fixture
def user(make_user, tenant):
    return make_user(tenant, "lifecycle@example.com")


def _queued_pair(db, tenant, user, address="800 Guadalupe St, Austin TX"):
    return sc_service.enqueue_run(
        db, tenant_id=tenant.id, requested_by=user.id, address=address,
    )


def _claim(db):
    job = job_queue.claim_next_job(db, worker_id="w-test")
    assert job is not None
    return job


def _stale_running_pair(db, tenant, user, *, attempts, max_attempts=2):
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    job.status = "running"
    job.attempts = attempts
    job.max_attempts = max_attempts
    job.lease_token = "stale-token"
    job.claimed_by = "w-dead"
    job.claimed_at = _now() - timedelta(minutes=30)
    job.heartbeat_at = _now() - timedelta(minutes=30)
    db.commit()
    return run, job


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


@pytest.fixture
def two_source_roster(monkeypatch):
    """run_collect over exactly geocode + fema, no network — the OpenAI
    narrative builder included (tests that exercise the narrative path
    override this stub with their own)."""
    def fake_roster():
        return ["arcgis_geocode", "fema_nfhl"]
    monkeypatch.setattr(registry, "all_source_ids", fake_roster)
    monkeypatch.setattr(orchestrator, "build_narratives", lambda *a, **k: {})

    calls = {"fetched": []}

    def make_fetch(geocode_source="nominatim", geocode_errors=None):
        def loader(source_id):
            def fetch(identity, session=None, config=None):
                calls["fetched"].append(source_id)
                if source_id == "arcgis_geocode":
                    return _adapter_result(source_id, {} if geocode_errors else {
                        "lat": 30.2672, "lng": -97.7431,
                        "county": "travis", "source": geocode_source,
                    }, errors=geocode_errors)
                return _adapter_result(source_id, {"flood_zone": "X"})
            return fetch
        return loader

    return calls, make_fetch


# ── Registration ──────────────────────────────────────────────────────


def test_site_check_kind_is_registered():
    assert job_queue.get_handler("site_check") is handle_site_check
    assert job_queue._RECOVERY_RECONCILERS["site_check"] is (
        reconcile_site_check_recovery
    )


def test_handler_refuses_cross_tenant_run(db, tenant, user, make_tenant):
    other = make_tenant("Other Office")
    _run, job = _queued_pair(db, tenant, user)
    job.tenant_id = other.id  # simulate corruption
    db.commit()
    with pytest.raises(RuntimeError, match="tenant mismatch"):
        handle_site_check(db, job)


# ── §7.4/§7.6 recovery pairing ────────────────────────────────────────


def test_recover_below_cap_requeues_job_and_run_together(db, tenant, user):
    run, job = _stale_running_pair(db, tenant, user, attempts=1)
    outcome = job_queue.recover_stale_job(db, job.id)
    assert outcome == "requeued"
    db.expire_all()
    assert db.get(Job, job.id).status == "queued"
    assert db.get(Job, job.id).lease_token is None
    assert db.get(SiteCheckRun, run.id).status == "queued"
    assert db.get(Job, job.id).progress["message"] == "Retrying after an interruption."


def test_recover_at_cap_fails_job_and_run_together(db, tenant, user):
    run, job = _stale_running_pair(db, tenant, user, attempts=2)
    outcome = job_queue.recover_stale_job(db, job.id)
    assert outcome == "failed"
    db.expire_all()
    job = db.get(Job, job.id)
    run = db.get(SiteCheckRun, run.id)
    assert job.status == "failed" and job.error_code == "worker_lost"
    assert run.status == "failed" and run.error_code == "worker_lost"
    assert run.failure_reason == "The lookup stopped unexpectedly. Run it again."


def test_recovery_never_touches_a_completed_run(db, tenant, user):
    """A stale RETRY job belongs to a completed run — the run stays
    completed (§8.4)."""
    run, job = _stale_running_pair(db, tenant, user, attempts=2)
    run.status = "completed"
    run.finished_at = _now()
    job.payload = {"run_id": run.id, "retry_source_ids": ["fema_nfhl"]}
    db.commit()
    assert job_queue.recover_stale_job(db, job.id) == "failed"
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).status == "completed"


def test_read_path_invokes_the_same_primitive_as_the_sweeper(db, tenant, user):
    """§7.6: polling a stale run before the sweep produces the identical
    outcome — requeued below the cap, lease invalidated."""
    run, job = _stale_running_pair(db, tenant, user, attempts=1)
    polled = sc_service.get_run(db, tenant_id=tenant.id, run_id=run.id)
    assert polled.status == "queued"  # same as the sweeper's outcome
    db.expire_all()
    assert db.get(Job, job.id).status == "queued"
    assert db.get(Job, job.id).lease_token is None


@pytest.mark.parametrize("job_status,expect_status,expect_code", [
    ("cancelled", "cancelled", "cancelled_by_user"),
    ("failed", "failed", "boom_code"),
    ("succeeded", "failed", "inconsistent_worker_state"),
])
def test_read_path_reconciles_terminal_job_drift(db, tenant, user, job_status,
                                                 expect_status, expect_code):
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    job.status = job_status
    job.error_code = "boom_code" if job_status == "failed" else None
    job.error_message = "It broke." if job_status == "failed" else None
    job.finished_at = _now()
    db.commit()
    polled = sc_service.get_run(db, tenant_id=tenant.id, run_id=run.id)
    assert polled.status == expect_status
    if expect_status == "failed":
        assert polled.error_code == expect_code


def test_read_path_at_attempt_cap_fails_via_get_run(db, tenant, user):
    """§7.6 at the cap THROUGH the real poll: get_run on a stale running
    run whose job already burned its attempts produces the sweeper's
    at-cap outcome — job AND run failed `worker_lost` together."""
    run, job = _stale_running_pair(db, tenant, user, attempts=2)
    polled = sc_service.get_run(db, tenant_id=tenant.id, run_id=run.id)
    assert polled.status == "failed"
    assert polled.error_code == "worker_lost"
    assert polled.failure_reason == "The lookup stopped unexpectedly. Run it again."
    db.expire_all()
    recovered = db.get(Job, job.id)
    assert recovered.status == "failed" and recovered.error_code == "worker_lost"
    assert recovered.lease_token is None


def test_read_path_missing_job_fails_worker_lost(db, tenant, user):
    run, job = _queued_pair(db, tenant, user)
    run.status = "running"
    db.delete(job)
    db.commit()
    polled = sc_service.get_run(db, tenant_id=tenant.id, run_id=run.id)
    assert polled.status == "failed"
    assert polled.error_code == "worker_lost"


# ── §7.5 worker cancel checkpoints ────────────────────────────────────


def test_cancel_pre_wave0_zero_staged_ends_cancelled_not_failed(db, tenant, user):
    """The round-3/1 case: cancel honored at the pre-Wave-0 checkpoint —
    run + job end cancelled TOGETHER with zero staged results; no adapter
    (not even the geocoder) runs."""
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    run = db.get(SiteCheckRun, run.id)
    run.cancel_requested_at = _now()
    db.commit()

    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
    )
    assert out.status == "cancelled"  # NEVER failed
    assert out.adapter_results == {}
    assert out.failure_reason == "This lookup was cancelled before it finished."
    db.expire_all()
    assert db.get(Job, job.id).status == "cancelled"  # terminalized together


@pytest.mark.parametrize("parallel", [True, False])
def test_cancel_at_wave_boundary_stages_completed_waves(db, tenant, user,
                                                        two_source_roster,
                                                        monkeypatch, parallel):
    """Cancel lands mid-Wave-0: the geocode result is staged lease-fenced,
    fema never starts, run + job end cancelled. Exercised on BOTH the
    parallel path and the sequential (feature-flag-off) path."""
    from app.config.settings import settings as settings_obj
    monkeypatch.setattr(settings_obj, "public_intel_parallel_enabled", parallel)
    calls, make_fetch = two_source_roster
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    loader = make_fetch()

    def cancel_during_geocode(source_id):
        fetch = loader(source_id)
        if source_id != "arcgis_geocode":
            return fetch

        def geocode_then_cancel(identity, session=None, config=None):
            result = fetch(identity, session=session, config=config)
            flag_session = SessionLocal()
            try:
                target = flag_session.get(SiteCheckRun, run.id)
                target.cancel_requested_at = _now()
                flag_session.commit()
            finally:
                flag_session.close()
            return result
        return geocode_then_cancel

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", cancel_during_geocode)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
    )
    assert out.status == "cancelled"
    assert "arcgis_geocode" in (out.adapter_results or {})  # staged wave kept
    assert "fema_nfhl" not in (out.adapter_results or {})   # later wave never ran
    assert calls["fetched"] == ["arcgis_geocode"]
    db.expire_all()
    assert db.get(Job, job.id).status == "cancelled"


def test_cancel_checkpoint_abandons_when_lease_was_invalidated(db, tenant, user):
    """§7.4 late-writer: a checkpoint whose fenced job write matches 0
    rows rolls its staged unit back, raises LeaseLost and never flips the
    run."""
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    run = db.get(SiteCheckRun, run.id)
    run.cancel_requested_at = _now()
    db.commit()
    # Another actor (supersede / recovery) invalidated the lease.
    job_row = db.get(Job, job.id)
    stale_token = job_row.lease_token
    job_row.lease_token = None
    db.commit()

    with pytest.raises(job_queue.LeaseLost):
        orchestrator._honor_cancellation(
            db, db.get(SiteCheckRun, run.id),
            job_id=job.id, lease_token=stale_token,
            results={}, queried=[], failed=[],
        )
    db.rollback()
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).status == "queued"  # untouched
    assert db.get(Job, job.id).status == "running"


# ── §8.6 geocoder attribution + full-run wiring ───────────────────────


def _usage_rows(db, run_id):
    return {
        (r.provider, r.service): r
        for r in db.scalars(
            select(SiteCheckProviderUsage).where(
                SiteCheckProviderUsage.run_id == run_id
            )
        ).all()
    }


def test_mocked_adapters_fabricate_no_provider_rows(db, tenant, user,
                                                   two_source_roster,
                                                   monkeypatch):
    """Reviewer finding 4 (2026-08-01): metering is per actual REQUEST at
    the HTTP boundary, never derived from a logical outcome. These three
    tests previously asserted the rejected outcome-based contract ("no
    rows for tiers that didn't resolve"), which is precisely how a paid
    Google request returning ZERO_RESULTS got billed to arcgis.

    With every adapter mocked, NO outbound request happens — so the
    ledger must stay empty rather than invent rows from the result's
    `source`. Real per-request attribution (nominatim/arcgis/google
    tiers, zero-results, billed retries, OSM→ArcGIS fallback) is covered
    against the real adapters offline in
    tests/test_site_check_provider_metering.py.
    """
    for source in ("nominatim", "arcgis", "google"):
        _calls, make_fetch = two_source_roster
        monkeypatch.setattr(orchestrator, "_load_adapter_fetch",
                            make_fetch(geocode_source=source))
        run, _ = _queued_pair(db, tenant, user)
        job = _claim(db)
        out = orchestrator.run_collect(
            db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
        )
        assert out.status == "completed"
        assert out.geocode_source == source
        assert _usage_rows(db, run.id) == {}, (
            f"{source}: no request was made, so no provider row may exist"
        )
def test_geocode_failure_fails_run_asynchronously_with_plain_state(
    db, tenant, user, two_source_roster, monkeypatch,
):
    calls, make_fetch = two_source_roster
    monkeypatch.setattr(
        orchestrator, "_load_adapter_fetch",
        make_fetch(geocode_errors=["no match for this address"]),
    )
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
    )
    assert out.status == "failed"
    assert out.error_code == "geocode_failed"
    assert calls["fetched"] == ["arcgis_geocode"]  # nothing else ran


# ── §11.3 narrative last-resort guard ─────────────────────────────────


def test_narrative_builder_crash_degrades_run_never_fails(db, tenant, user,
                                                          two_source_roster,
                                                          monkeypatch):
    _calls, make_fetch = two_source_roster
    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", make_fetch())

    def exploding_narratives(session, run, adapter_results, settings):
        raise RuntimeError("narrative pipeline exploded")

    monkeypatch.setattr(orchestrator, "build_narratives", exploding_narratives)
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
    )
    assert out.status == "completed"  # the run NEVER fails for narratives
    meta = (out.derived or {}).get("narrative_meta")
    assert meta is not None
    assert meta["status"] == "fallback_provider_error"


@pytest.mark.parametrize("parallel", [True, False])
def test_worker_handler_end_to_end_marks_run_completed(db, tenant, user,
                                                       two_source_roster,
                                                       monkeypatch, parallel):
    """The registered handler drives run_collect off the job payload and
    the job finishes via the worker contract — on both dispatch paths."""
    from app.config.settings import settings as settings_obj
    monkeypatch.setattr(settings_obj, "public_intel_parallel_enabled", parallel)
    _calls, make_fetch = two_source_roster
    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", make_fetch())
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    handler = job_queue.get_handler(job.kind)
    handler(db, job)
    # The fenced success staging (§7.5 pre-commit fence) terminalizes the
    # job inside the handler's transaction; the worker's mark_done is a
    # no-op backstop (0 rows) and must return False.
    assert job_queue.mark_done(db, job.id, job.lease_token) is False
    db.expire_all()
    assert db.get(SiteCheckRun, run.id).status == "completed"
    assert db.get(Job, job.id).status == "succeeded"


class TestTerminalStagingFence:
    """Live defect 2026-08-01: a worker whose lease was invalidated by a
    same-subject supersede overwrote the cancelled run back to completed.
    Every terminal staging (success, failure, retry) now rides a
    lease-guarded job UPDATE in the same transaction."""

    def test_fail_run_abandons_when_lease_invalidated(self, db, tenant, user):
        from app.core import job_queue
        from app.services.site_check import orchestrator
        from app.services.site_check import service as svc

        run, job, _ = svc.create_run(
            db, tenant_id=tenant.id,
            requested_by=user.id,
            address="1 Fence Test Rd, Austin, TX", mode="fresh",
        )
        claimed = job_queue.claim_next_job(db, kinds=["site_check"], worker_id="w1")
        assert claimed is not None and claimed.id == job.id
        held = claimed.lease_token
        # Simulate supersede: cancel run+job, invalidate lease.
        run.status = "cancelled"
        run.error_code = "superseded_by_new_check"
        claimed.status = "cancelled"
        claimed.lease_token = None
        db.add_all([run, claimed])
        db.commit()

        out = orchestrator._fail_run(
            db, run, error_code="geocode_failed", reason="x",
            job_id=job.id, lease_token=held,
        )
        db.refresh(run)
        assert run.status == "cancelled"
        assert run.error_code == "superseded_by_new_check"
        assert out.status == "cancelled"


# ── §17 GWT 4 (orchestrator leg): a failing adapter never fails the run ─


def test_failing_adapter_still_completes_run_with_failed_entry(
    db, tenant, user, two_source_roster, monkeypatch,
):
    """A transient non-geocode adapter failure through the REAL
    run_collect: the run completes, the failed source carries a
    `fetched_failed` staged entry + a sources_failed row, and the §8.4
    classifier sees exactly that source as retryable."""
    _calls, make_fetch = two_source_roster
    loader = make_fetch()

    def failing(source_id):
        if source_id != "fema_nfhl":
            return loader(source_id)

        def boom(identity, session=None, config=None):
            raise RuntimeError("the public site timed out")
        return boom

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", failing)
    run, _ = _queued_pair(db, tenant, user)
    job = _claim(db)
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
    )
    assert out.status == "completed"  # one failing source NEVER fails the run
    entry = out.adapter_results["fema_nfhl"]
    assert entry["refresh_status"] == "fetched_failed"
    assert entry["errors"] and "timed out" in entry["errors"][0]
    failed_rows = [
        f for f in out.sources_failed if f.get("source") == "fema_nfhl"
    ]
    assert len(failed_rows) == 1 and "timed out" in failed_rows[0]["error"]
    db.expire_all()
    assert db.get(Job, job.id).status == "succeeded"
    retryable = sc_service.retryable_failed_sources_for_run(out)
    assert [row["source_id"] for row in retryable] == ["fema_nfhl"]


# ── §8.4 GWT 5: retry through the WORKER mutates only the retried source ─


def _seeded_completed_run_for_retry(db, tenant, user):
    """A completed run with one healthy source (census) + one transient
    failure (fema), both narratives present, original job terminal."""
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
            "fema_nfhl", {},
            ["The public site timed out (503 server error)"],
            "PROGRAMMATIC_PUBLIC_BUT_FRAGILE", "partial",
        ),
    }
    run.sources_queried = [
        {"source": s, "status": "fetched"}
        for s in ("arcgis_geocode", "census_acs", "fema_nfhl")
    ]
    run.sources_failed = [{
        "source": "fema_nfhl",
        "error": "The public site timed out (503 server error)",
        "failed_at": old,
    }]
    run.derived = {
        "card_narratives": {
            "census_acs": "About 12,000 people live within a mile.",
            "fema_nfhl": "STALE flood narrative that must be dropped.",
        },
        "card_narratives_meta": {"status": "complete", "model": "gpt-5.6-terra"},
    }
    db.commit()
    db.refresh(run)
    return run


def test_retry_through_worker_mutates_only_the_retried_source(
    db, tenant, user, monkeypatch,
):
    """§8.4 "Semantics" / §17 GWT 5 THEN clause, driven through the REAL
    service enqueue + claim + registered worker handler: only the
    retried source's facts and checked-on date change; the other
    source's facts AND narrative are byte-identical (ported
    partially_preserved_after_source_retry semantics); sources_queried
    gains the `retried` entry; the run stays completed THROUGHOUT; the
    worker-side run_retry_completed audit row exists."""
    run = _seeded_completed_run_for_retry(db, tenant, user)
    before = deepcopy(run.adapter_results)
    census_narrative_before = run.derived["card_narratives"]["census_acs"]

    out_run, job, created = sc_service.enqueue_source_retry(
        db, tenant_id=tenant.id, run_id=run.id,
        requested_by=user.id, source_ids=["fema_nfhl"],
    )
    assert created is True
    assert out_run.status == "completed"
    assert job.payload == {"run_id": run.id, "retry_source_ids": ["fema_nfhl"]}

    claimed = job_queue.claim_next_job(db, worker_id="w-retry")
    assert claimed is not None and claimed.id == job.id

    observed = {"sources": []}

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            observed["sources"].append(source_id)
            probe = SessionLocal()
            try:
                # The run must be `completed` even MID-retry (§8.4).
                observed["status_mid_retry"] = probe.get(
                    SiteCheckRun, run.id,
                ).status
            finally:
                probe.close()
            return _adapter_result(
                source_id, {"flood_zone": "AE", "in_floodplain": True},
            )
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)
    handler = job_queue.get_handler("site_check")
    handler(db, claimed)

    db.expire_all()
    out = db.get(SiteCheckRun, run.id)
    assert out.status == "completed"           # stayed completed…
    assert observed["status_mid_retry"] == "completed"  # …THROUGHOUT
    assert observed["sources"] == ["fema_nfhl"]  # ONLY the retried source ran

    # Untouched sources: facts AND checked-on date byte-identical.
    assert out.adapter_results["census_acs"] == before["census_acs"]
    assert out.adapter_results["arcgis_geocode"] == before["arcgis_geocode"]

    # Retried source: replaced facts, new fetched_at, errors cleared,
    # registry schema version stamped.
    fema = out.adapter_results["fema_nfhl"]
    assert fema["normalized_facts"] == {"flood_zone": "AE", "in_floodplain": True}
    assert fema["fetched_at"] != before["fema_nfhl"]["fetched_at"]
    assert not fema["errors"]
    assert fema["result_schema_version"] == (
        registry.result_schema_version("fema_nfhl")
    )

    # sources_queried gained the retried entry; the failed row dropped.
    retried_entries = [
        q for q in out.sources_queried if q.get("status") == "retried"
    ]
    assert [q["source"] for q in retried_entries] == ["fema_nfhl"]
    assert retried_entries[0]["retried_at"]
    assert all(f.get("source") != "fema_nfhl" for f in out.sources_failed or [])

    # Ported narrative preservation: the OTHER source's narrative is
    # byte-identical; the retried source's stale narrative is dropped;
    # meta records the partially-preserved state.
    narratives = out.derived["card_narratives"]
    assert narratives["census_acs"] == census_narrative_before
    assert "fema_nfhl" not in narratives
    meta = out.derived["card_narratives_meta"]
    assert meta["status"] == "partially_preserved_after_source_retry"
    assert meta["retried_source_id"] == "fema_nfhl"

    # Worker-side audit row, attributed to the run's requester (§8.7).
    audits = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "site_check.run_retry_completed")
        .where(AuditLog.entity_id == run.id)
    ).all()
    assert len(audits) == 1
    assert audits[0].user_id == user.id
    assert audits[0].detail["retried_source_id"] == "fema_nfhl"
    assert audits[0].detail["source_failed"] is False

    # The retry job terminalized through the fenced staging.
    assert db.get(Job, claimed.id).status == "succeeded"
