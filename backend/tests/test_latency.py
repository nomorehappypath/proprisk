"""Latency targets (SPEC.md chapter 5) on a test copy with 1,000 stored
lookups: starting a lookup only validates and queues it (no network on the
request path), and reading history comes straight from the local
database."""

import time
import uuid
from datetime import UTC, datetime

from app.models import SiteCheckRun
from app.services.site_check import service as sc_service

ROW_COUNT = 1000


def _seed_runs(db, tenant_id, user_id):
    now = datetime.now(UTC)
    db.bulk_save_objects(
        [
            SiteCheckRun(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                requested_by=user_id,
                address=f"{n} Seed Rd, Austin, TX",
                normalized_address=sc_service.normalize_address(f"{n} Seed Rd, Austin, TX"),
                county="travis",
                status="completed",
                created_at=now,
            )
            for n in range(ROW_COUNT)
        ]
    )
    db.commit()


def test_create_lookup_latency(client, db, local_tenant_user):
    tenant, user = local_tenant_user
    _seed_runs(db, tenant.id, user.id)

    timings = []
    for n in range(20):
        started = time.monotonic()
        resp = client.post(
            "/api/site-check",
            json={"address": f"{n} Latency Test Ave, Austin, TX", "mode": "fresh"},
        )
        timings.append((time.monotonic() - started) * 1000)
        assert resp.status_code == 202, resp.text

    slow = [t for t in timings if t >= 500]
    assert not slow, f"{len(slow)}/20 create-lookup requests took >=500ms: {timings}"


def test_history_latency(client, db, local_tenant_user):
    tenant, user = local_tenant_user
    _seed_runs(db, tenant.id, user.id)

    timings = []
    for _ in range(20):
        started = time.monotonic()
        resp = client.get("/api/site-check/runs?page=1&page_size=20")
        timings.append((time.monotonic() - started) * 1000)
        assert resp.status_code == 200

    slow = [t for t in timings if t >= 300]
    assert not slow, f"{len(slow)}/20 history requests took >=300ms: {timings}"
