"""Worker concurrency target (SPEC.md chapter 6): 10 lookups queued at once
all finish with at most `worker_max_concurrency` (3) running at a time."""

import threading
import time

from app.config.settings import settings
from app.core import job_queue
from app.worker import Worker

JOB_COUNT = 10
JOB_KIND = "test_concurrency_probe"


def test_worker_runs_at_most_three_lookups(db, local_tenant_user):
    tenant, _user = local_tenant_user
    lock = threading.Lock()
    state = {"current": 0, "max_seen": 0, "finished": 0}

    def probe_handler(session, job):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.3)
        with lock:
            state["current"] -= 1
            state["finished"] += 1

    job_queue.register_handler(JOB_KIND, probe_handler)
    try:
        specs = [
            {"tenant_id": tenant.id, "kind": JOB_KIND, "payload": {}}
            for _ in range(JOB_COUNT)
        ]
        job_queue.enqueue_jobs(db, specs)

        worker = Worker(worker_id="test-concurrency-worker")
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and state["finished"] < JOB_COUNT:
                time.sleep(0.1)
        finally:
            worker.stop()
            thread.join(timeout=10)

        assert state["finished"] == JOB_COUNT, state
        assert state["max_seen"] <= settings.worker_max_concurrency, state
        assert state["max_seen"] >= 2, "never observed real overlap — test proves nothing"
    finally:
        job_queue._HANDLERS.pop(JOB_KIND, None)
