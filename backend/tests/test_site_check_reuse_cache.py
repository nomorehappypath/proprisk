"""Site Check reuse seed, freshness ladder, global cache, and provider
usage aggregation (docs/specs/site_check.md §8.6, §9, §17/§18).

Covers: per-source reusability + ORIGINAL fetched_at, schema-version
bump, anchor tolerance, transitive dependency invalidation
(_REFRESH_DEPENDENCIES fixpoint), the exact freshness boundary at a
NON-default configured value, fresh-mode cache bypass (stale-fallback
included), cache never storing failures / tenant data, the concurrent
upsert, and microdollar aggregation with display rounding.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

import pytest
from sqlalchemy import select

from app.config.settings import settings as settings_obj
from app.core import job_queue
from app.db import SessionLocal
from app.models import SiteCheckProviderUsage, SiteCheckRun, SiteCheckSourceCache
from app.services.site_check import orchestrator, registry, source_cache, usage
from app.services.site_check import service as sc_service
from app.services.site_check.contract import AdapterResult
from app.services.site_check.identity import PropertyIdentity

ADDRESS = "4200 Ranch Rd 12, San Marcos TX"


def _now():
    return datetime.now(UTC)


@pytest.fixture
def tenant(make_tenant):
    return make_tenant("Reuse Office")


@pytest.fixture
def user(make_user, tenant):
    return make_user(tenant, "reuse@example.com")


def _result(source_id, facts, *, age_days=1.0, errors=None, schema_version=None):
    version = (
        schema_version
        if schema_version is not None
        else registry.result_schema_version(source_id)
    )
    return {
        "source_id": source_id,
        "source_access_status": "PROGRAMMATIC_PUBLIC",
        "data_confidence_status": "verified_public_fact",
        "coverage_status": "covered",
        "normalized_facts": facts,
        "errors": errors or [],
        "fetched_at": (_now() - timedelta(days=age_days)).isoformat(),
        "result_schema_version": version,
    }


def _geocode(lat=29.88, lng=-97.93, *, age_days=1.0):
    return _result(
        "arcgis_geocode",
        {"lat": lat, "lng": lng, "county": "hays", "source": "nominatim"},
        age_days=age_days,
    )


def _completed_run(db, tenant, user, results, *, lat=29.88, lng=-97.93,
                   created_offset_minutes=0):
    run = SiteCheckRun(
        tenant_id=tenant.id,
        requested_by=user.id,
        address=ADDRESS,
        normalized_address=sc_service.normalize_address(ADDRESS),
        county="hays",
        lat=lat,
        lng=lng,
        status="completed",
        mode="check",
        adapter_results=results,
        sources_queried=[],
        sources_failed=[],
        derived={},
        created_at=_now() - timedelta(minutes=created_offset_minutes),
        finished_at=_now(),
    )
    db.add(run)
    db.commit()
    return run


def _seed(db, tenant, **kwargs):
    return sc_service.build_refresh_reuse_seed(
        db, tenant_id=tenant.id, address=ADDRESS, county="hays",
        parcel_id=None, **kwargs,
    )


# ── §9.2 reuse seed ───────────────────────────────────────────────────


def test_seed_reuses_fresh_sources_with_original_fetched_at(db, tenant, user):
    fema = _result("fema_nfhl", {"zone": "AE"}, age_days=30)
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(),
        # fema_nfhl is parcel-dependent in the ported invalidation graph —
        # a reusable county parcel result keeps it alive.
        "tcad_parcel": _result("tcad_parcel", {"parcel_id": "P1"}, age_days=30),
        "fema_nfhl": fema,
    })
    seed = _seed(db, tenant)
    assert "fema_nfhl" in seed["adapter_results"]
    reused = seed["adapter_results"]["fema_nfhl"]
    assert reused["refresh_status"] == "reused_recent"
    assert reused["fetched_at"] == fema["fetched_at"]  # ORIGINAL, never extended
    assert seed["cached_lat"] == pytest.approx(29.88)
    entry = next(q for q in seed["sources_queried"] if q["source"] == "fema_nfhl")
    assert entry["status"] == "reused_recent"


def test_seed_never_crosses_tenants(db, tenant, user, make_tenant, make_user):
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(),
        "fema_nfhl": _result("fema_nfhl", {"zone": "AE"}),
    })
    other = make_tenant("Foreign Office")
    seed = _seed(db, other)
    assert seed["adapter_results"] == {}


def test_schema_version_bump_invalidates_source_and_dependents(db, tenant, user):
    """§17 reuse-seed GWT: a stored result_schema_version differing from
    the current registry version refetches that source AND its
    dependents (via the ported _REFRESH_DEPENDENCIES)."""
    current = registry.result_schema_version("jurisdiction_resolver")
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(),
        # jurisdiction stored under an OLD schema version → stale.
        "jurisdiction_resolver": _result(
            "jurisdiction_resolver", {"verdict": "city_limits"},
            schema_version=current + 1,
        ),
        "austin_zoning": _result("austin_zoning", {"base_zone": "GR"}),
        "district_overlays": _result("district_overlays", {"mud_districts": []}),
        "tax_intel": _result("tax_intel", {"rates": []}),
        "tcad_parcel": _result("tcad_parcel", {"parcel_id": "P1"}),
        "fema_nfhl": _result("fema_nfhl", {"zone": "X"}),
    })
    seed = _seed(db, tenant)
    reused = set(seed["adapter_results"])
    assert "jurisdiction_resolver" not in reused          # version mismatch
    assert "austin_zoning" not in reused                  # ← jurisdiction_resolver
    assert "district_overlays" not in reused              # ← austin_zoning
    assert "tax_intel" not in reused                      # ← district_overlays (transitive)
    assert "fema_nfhl" in reused                          # untouched branch


def test_no_reusable_geocode_empties_the_seed(db, tenant, user):
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(age_days=500),  # far past freshness
        "fema_nfhl": _result("fema_nfhl", {"zone": "X"}, age_days=1),
    })
    seed = _seed(db, tenant)
    assert seed["adapter_results"] == {}
    assert seed["cached_lat"] is None


def test_anchor_moved_beyond_tolerance_drops_old_locations_facts(db, tenant, user):
    """A corrected anchor never resurrects the old location's facts."""
    # Older run anchored ~1km away carries the only soil result.
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(lat=29.89, lng=-97.94, age_days=40),
        "soil_intel": _result("soil_intel", {"soils": ["clay"]}, age_days=40),
    }, lat=29.89, lng=-97.94, created_offset_minutes=60)
    # Newest run defines the anchor.
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(lat=29.88, lng=-97.93, age_days=1),
    }, created_offset_minutes=0)
    seed = _seed(db, tenant)
    assert "soil_intel" not in seed["adapter_results"]
    assert "arcgis_geocode" in seed["adapter_results"]


def test_freshness_boundary_at_non_default_value(db, tenant, user, monkeypatch):
    """§9.1/§17: F=10 in settings — age 9 reused, age exactly 10 refetched.
    Every threshold derives from the configured value."""
    monkeypatch.setattr(settings_obj, "site_check_freshness_days", 10)
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(age_days=1),
        "tcad_parcel": _result("tcad_parcel", {"parcel_id": "P1"}, age_days=1),
        "fema_nfhl": _result("fema_nfhl", {"zone": "X"}, age_days=9),
        "soil_intel": _result("soil_intel", {"soils": []}, age_days=10),
        "hydro_intel": _result("hydro_intel", {"streams": []}, age_days=10.5),
    })
    seed = _seed(db, tenant)
    reused = set(seed["adapter_results"])
    assert "fema_nfhl" in reused          # 9 < 10
    assert "soil_intel" not in reused     # exactly the boundary → refetch
    assert "hydro_intel" not in reused    # past it


def test_fresh_mode_returns_empty_seed(db, tenant, user):
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(),
        "fema_nfhl": _result("fema_nfhl", {"zone": "X"}),
    })
    seed = _seed(db, tenant, force_refresh=True)
    assert seed["adapter_results"] == {}
    assert seed["sources_queried"] == []
    assert seed["cached_lat"] is None


# ── §9.3 global source cache ──────────────────────────────────────────


def _cached_payload(facts=None):
    return {
        "source_id": "fema_nfhl",
        "normalized_facts": facts or {"zone": "X"},
        "errors": [],
    }


def test_cache_fresh_hit_makes_no_provider_call(db):
    calls = []
    assert source_cache.store(
        db, source_id="fema_nfhl", scope_key="pt:29.88,-97.93",
        producer_version=3, payload=_cached_payload(),
    )
    db.commit()
    out = source_cache.get_or_fetch(
        db, source_id="fema_nfhl", scope_key="pt:29.88,-97.93",
        producer_version=3,
        fetch=lambda: calls.append(1) or _cached_payload(),
    )
    assert calls == []            # no provider call on a fresh hit
    assert out["from_cache"] is True and out["stale"] is False


def test_fresh_mode_bypasses_populated_cache_and_stale_fallback(db, monkeypatch):
    """§9.1 REQUIRED: mode=fresh reads NOTHING from the cache — a failing
    source reports honestly instead of serving an old copy — and a
    successful fresh result still updates the cache."""
    stale_moment = _now() - timedelta(days=400)
    row = SiteCheckSourceCache(
        source_id="fema_nfhl", scope_key="pt:1,1", producer_version=3,
        cache_key=source_cache.cache_key("fema_nfhl", "pt:1,1", 3),
        payload=_cached_payload({"zone": "OLD"}),
        fetched_at=stale_moment, expires_at=stale_moment + timedelta(days=1),
    )
    db.add(row)
    db.commit()

    calls = []

    def live_fetch():
        calls.append(1)
        return _cached_payload({"zone": "NEW"})

    out = source_cache.get_or_fetch(
        db, source_id="fema_nfhl", scope_key="pt:1,1", producer_version=3,
        fetch=live_fetch, force_refresh=True,
    )
    db.commit()
    assert calls == [1]                       # live call happened
    assert out["from_cache"] is False
    db.expire_all()
    stored = db.scalars(select(SiteCheckSourceCache)).one()
    assert stored.payload["normalized_facts"]["zone"] == "NEW"  # cache updated

    # Failure in fresh mode: NO stale-fallback, the error propagates.
    def failing_fetch():
        raise TimeoutError("provider down")

    with pytest.raises(TimeoutError):
        source_cache.get_or_fetch(
            db, source_id="fema_nfhl", scope_key="pt:1,1", producer_version=3,
            fetch=failing_fetch, force_refresh=True,
        )


def test_cache_stale_fallback_flags_stale_in_check_mode(db):
    stale_moment = _now() - timedelta(days=400)
    db.add(SiteCheckSourceCache(
        source_id="fema_nfhl", scope_key="pt:2,2", producer_version=3,
        cache_key=source_cache.cache_key("fema_nfhl", "pt:2,2", 3),
        payload=_cached_payload({"zone": "OLD"}),
        fetched_at=stale_moment, expires_at=stale_moment + timedelta(days=1),
    ))
    db.commit()

    def failing_fetch():
        raise TimeoutError("provider down")

    out = source_cache.get_or_fetch(
        db, source_id="fema_nfhl", scope_key="pt:2,2", producer_version=3,
        fetch=failing_fetch,
    )
    assert out["stale"] is True               # "from an older saved copy"
    assert out["payload"]["normalized_facts"]["zone"] == "OLD"


def test_cache_never_stores_failures_empty_facts_or_tenant_data(db):
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key="a", producer_version=3,
        payload={"normalized_facts": {}, "errors": []},          # empty facts
    )
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key="b", producer_version=3,
        payload={"normalized_facts": {"zone": "X"}, "errors": ["HTTP 500"]},
    )
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key="c", producer_version=3,
        payload={"normalized_facts": {"zone": "X"}, "errors": [],
                 "tenant_id": "t-1"},                            # tenant data
    )
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key="d", producer_version=3,
        payload={"normalized_facts": {"zone": "X"}, "errors": [],
                 "card_narratives": {"x": {}}},                  # narrative data
    )
    db.commit()
    assert db.scalars(select(SiteCheckSourceCache)).all() == []


def test_cache_ttl_defaults_to_freshness_days(db, monkeypatch):
    monkeypatch.setattr(settings_obj, "site_check_freshness_days", 10)
    moment = _now()
    source_cache.store(
        db, source_id="fema_nfhl", scope_key="ttl", producer_version=3,
        payload=_cached_payload(), now=moment,
    )
    db.commit()
    row = db.scalars(select(SiteCheckSourceCache)).one()
    assert row.expires_at.astimezone(UTC) == moment + timedelta(days=10)


def test_cache_upsert_race_keeps_one_row_newest_write(db):
    """Two writers, one row; only the newer successful fetch overwrites."""
    early = _now() - timedelta(hours=2)
    late = _now()
    source_cache.store(db, source_id="fema_nfhl", scope_key="race",
                       producer_version=3,
                       payload=_cached_payload({"zone": "LATE"}), now=late)
    db.commit()
    # An older concurrent writer lands afterwards — it must NOT clobber.
    source_cache.store(db, source_id="fema_nfhl", scope_key="race",
                       producer_version=3,
                       payload=_cached_payload({"zone": "EARLY"}), now=early)
    db.commit()
    rows = db.scalars(select(SiteCheckSourceCache).where(
        SiteCheckSourceCache.scope_key == "race",
    )).all()
    assert len(rows) == 1
    assert rows[0].payload["normalized_facts"]["zone"] == "LATE"


def test_cache_upsert_two_concurrent_writers_one_row_newest_wins(db):
    """§18: TWO CONCURRENT writers (real threads + sessions, barrier
    start) racing the same scope — exactly one row survives and the
    NEWEST fetch's payload wins regardless of commit order."""
    barrier = Barrier(2)
    early = _now() - timedelta(hours=2)
    late = _now()
    errors: list[Exception] = []

    def writer(zone, moment):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            source_cache.store(
                session, source_id="fema_nfhl", scope_key="race-threads",
                producer_version=3,
                payload=_cached_payload({"zone": zone}), now=moment,
            )
            session.commit()
        except Exception as exc:  # surfaced below — a racer must not die
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [
        Thread(target=writer, args=("EARLY", early)),
        Thread(target=writer, args=("LATE", late)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    rows = db.scalars(select(SiteCheckSourceCache).where(
        SiteCheckSourceCache.scope_key == "race-threads",
    )).all()
    assert len(rows) == 1
    assert rows[0].payload["normalized_facts"]["zone"] == "LATE"


@pytest.mark.parametrize(
    "forbidden_key", sorted(source_cache._FORBIDDEN_PAYLOAD_KEYS),
)
def test_cache_refuses_every_forbidden_key_everywhere(db, forbidden_key):
    """§9.3: EVERY forbidden key (tenant_id, user_id, requested_by, and
    all narrative keys) is refused both at the payload top level and
    inside normalized_facts."""
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key=f"fk-top-{forbidden_key}",
        producer_version=3,
        payload={"normalized_facts": {"zone": "X"}, "errors": [],
                 forbidden_key: "smuggled"},
    )
    assert not source_cache.store(
        db, source_id="fema_nfhl", scope_key=f"fk-facts-{forbidden_key}",
        producer_version=3,
        payload={"normalized_facts": {"zone": "X", forbidden_key: "smuggled"},
                 "errors": []},
    )
    db.commit()
    assert db.scalars(select(SiteCheckSourceCache)).all() == []


# ── §9.1/§9.2 fresh-mode + whole-run reuse legs (§17 GWT 7/8/19) ──────


def test_fresh_mode_skips_prior_run_aerial_chip_reuse(db, tenant, user):
    """NB: mode="fresh" must bypass the orchestrator's prior-run aerial
    chip fallback exactly like every other reuse path (§9.1)."""
    chip = _result(
        "public_aerial_imagery",
        {"image_url": "https://imagery.example/usgs.jpg",
         "center": {"lat": 29.88, "lng": -97.93}},
        age_days=1,
    )
    _completed_run(db, tenant, user, {"public_aerial_imagery": chip},
                   created_offset_minutes=5)
    current = _completed_run(db, tenant, user, {})
    identity = PropertyIdentity(
        address=ADDRESS, lat=29.88, lng=-97.93, county="hays",
    )
    reused = orchestrator._reusable_imagery_from_prior_runs(
        db, current, identity, force_refresh=False,
    )
    assert reused is not None
    assert reused["normalized_facts"]["image_url"] == (
        "https://imagery.example/usgs.jpg"
    )
    assert orchestrator._reusable_imagery_from_prior_runs(
        db, current, identity, force_refresh=True,
    ) is None


def _fake_loader(fetched):
    def loader(source_id):
        def fetch(identity, session=None, config=None):
            fetched.append(source_id)
            facts = (
                {"lat": 29.88, "lng": -97.93, "county": "hays",
                 "source": "nominatim"}
                if source_id == "arcgis_geocode" else {"value": 1}
            )
            return AdapterResult(
                source_id=source_id,
                source_access_status="PROGRAMMATIC_PUBLIC",
                data_confidence_status="verified_public_fact",
                coverage_status="covered",
                normalized_facts=facts,
                errors=[],
                fetched_at=_now(),
            )
        return fetch
    return loader


def test_reused_sources_make_zero_provider_calls_and_usage_rows(
    db, tenant, user, monkeypatch,
):
    """§17 GWT 7 (no-provider-call leg) through the REAL run_collect: a
    30-day-old completed run makes every source reusable — the new
    check completes with ZERO adapter fetches and ZERO provider-usage
    rows, and the reused entries keep their ORIGINAL fetched_at."""
    fema = _result("fema_nfhl", {"zone": "AE"}, age_days=30)
    _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(age_days=30),
        "tcad_parcel": _result("tcad_parcel", {"parcel_id": "P1"}, age_days=30),
        "fema_nfhl": fema,
    })
    monkeypatch.setattr(
        registry, "all_source_ids",
        lambda: ["arcgis_geocode", "tcad_parcel", "fema_nfhl"],
    )
    monkeypatch.setattr(orchestrator, "build_narratives", lambda *a, **k: {})
    fetched: list[str] = []
    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", _fake_loader(fetched))

    run, _job, _superseded = sc_service.create_run(
        db, tenant_id=tenant.id, requested_by=user.id,
        address=ADDRESS, mode="check",
    )
    assert "fema_nfhl" in run.adapter_results  # seeded
    claimed = job_queue.claim_next_job(db, worker_id="w-reuse")
    assert claimed is not None
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id,
        lease_token=claimed.lease_token,
    )
    assert out.status == "completed"
    assert fetched == []  # ZERO provider calls — everything reused
    assert out.adapter_results["fema_nfhl"]["fetched_at"] == fema["fetched_at"]
    assert out.adapter_results["fema_nfhl"]["refresh_status"] == "reused_recent"
    rows = db.scalars(select(SiteCheckProviderUsage).where(
        SiteCheckProviderUsage.run_id == run.id,
    )).all()
    assert rows == []  # ZERO usage rows for reused sources


def test_failed_fresh_run_leaves_prior_completed_runs_intact(
    db, tenant, user, monkeypatch,
):
    """§17 GWT 8 (harmless-failure leg): a mode="fresh" re-check that
    FAILS leaves the prior completed run — its status, facts, and
    finished_at — byte-for-byte untouched."""
    prior = _completed_run(db, tenant, user, {
        "arcgis_geocode": _geocode(),
        "fema_nfhl": _result("fema_nfhl", {"zone": "AE"}),
    })
    before_results = deepcopy(prior.adapter_results)
    before_finished = prior.finished_at

    monkeypatch.setattr(
        registry, "all_source_ids",
        lambda: ["arcgis_geocode", "fema_nfhl"],
    )
    monkeypatch.setattr(orchestrator, "build_narratives", lambda *a, **k: {})

    def loader(source_id):
        def fetch(identity, session=None, config=None):
            return AdapterResult(
                source_id=source_id,
                source_access_status="PROGRAMMATIC_PUBLIC_BUT_FRAGILE",
                data_confidence_status="unknown",
                coverage_status="partial",
                normalized_facts={},
                errors=["no match for this address"],
                fetched_at=_now(),
            )
        return fetch

    monkeypatch.setattr(orchestrator, "_load_adapter_fetch", loader)
    run, _job, superseded = sc_service.create_run(
        db, tenant_id=tenant.id, requested_by=user.id,
        address=ADDRESS, mode="fresh",
    )
    assert superseded == []          # a COMPLETED prior is never superseded
    assert run.adapter_results == {}  # fresh: empty seed (§9.1)
    claimed = job_queue.claim_next_job(db, worker_id="w-fresh")
    assert claimed is not None
    out = orchestrator.run_collect(
        db, run_id=run.id, job_id=claimed.id,
        lease_token=claimed.lease_token,
    )
    assert out.status == "failed"
    assert out.error_code == "geocode_failed"

    db.expire_all()
    kept = db.get(SiteCheckRun, prior.id)
    assert kept.status == "completed"
    assert kept.adapter_results == before_results
    assert kept.finished_at == before_finished
    assert kept.error_code is None


# ── §8.6 microdollar aggregation ──────────────────────────────────────


def test_usage_rows_aggregate_atomically_per_run_provider_service(db, tenant, user):
    run = _completed_run(db, tenant, user, {})
    for _ in range(3):
        usage.record_call(db, run=run, provider="arcgis",
                          service="geocode_stored")
    db.commit()
    rows = db.scalars(select(SiteCheckProviderUsage).where(
        SiteCheckProviderUsage.run_id == run.id,
    )).all()
    assert len(rows) == 1                     # ONE aggregate row
    row = rows[0]
    assert row.calls == 3 and row.billable_units == 3
    # 3 × 4,000 µ$ — aggregation in microdollars…
    assert row.estimated_cost_microdollars == 12_000
    # …rounding to cents/dollars only at render time.
    assert usage.run_total_cost_usd(db, run_id=run.id) == pytest.approx(0.012)
    assert usage.per_provider_breakdown(db, run_id=run.id) == {
        "arcgis": pytest.approx(0.012),
    }


def test_usage_retried_calls_counted(db, tenant, user):
    run = _completed_run(db, tenant, user, {})
    usage.record_call(db, run=run, provider="fema", service="fetch")
    usage.record_call(db, run=run, provider="fema", service="fetch", retried=True)
    db.commit()
    row = db.scalars(select(SiteCheckProviderUsage)).one()
    assert row.calls == 2 and row.retried_calls == 1
    assert row.estimated_cost_microdollars == 0  # free provider, audit row anyway
