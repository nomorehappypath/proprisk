"""PropRisk background worker — runs Public Data lookups from the jobs queue.

One process, `worker_max_concurrency` jobs in flight:
- a dedicated heartbeat watchdog thread pumps ALL held leases in one bulk
  UPDATE every `job_heartbeat_interval_seconds` — work threads never write
  heartbeats, so a blocked adapter call cannot starve liveness;
- a sweeper thread runs the stale-recovery primitive at startup and every
  `job_sweep_interval_seconds`;
- each unit of work gets its own session with Postgres lock_timeout so a
  blocked terminal transition raises instead of wedging the thread;
- SIGTERM/SIGINT drain in-flight jobs before exit (graceful shutdown).

Local dev runs it foreground:  uv run python -m app.worker
Production runs the same command under systemd (propdesk-worker.service).
"""

import logging
import os
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.core import job_queue
from app.core.error_sink import report_error
from app.db import SessionLocal
from app.models import Job

log = logging.getLogger("propdesk.worker")


def _work_session() -> Session:
    """Per-work-unit session with the §7.7 lock_timeout guard. Postgres SET
    takes no bind parameters; the value is a settings int, not user input."""
    session = SessionLocal()
    session.execute(text(f"SET lock_timeout = '{int(settings.job_lock_timeout_seconds)}s'"))
    session.commit()  # SET LOCAL semantics not needed; keep it session-wide
    return session


class Worker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._stop = threading.Event()
        self._held: dict[str, str] = {}  # job_id → lease_token
        self._held_lock = threading.Lock()

    # ── lease bookkeeping (read by the watchdog thread) ────────────────

    def _hold(self, job: Job) -> None:
        with self._held_lock:
            self._held[job.id] = job.lease_token

    def _drop(self, job: Job) -> None:
        with self._held_lock:
            self._held.pop(job.id, None)

    def held_tokens(self) -> list[str]:
        with self._held_lock:
            return list(self._held.values())

    # ── background threads ─────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Dedicated watchdog thread (§7.4): one bulk UPDATE per tick, guarded
        by the held lease tokens. Keeps pumping while draining on shutdown."""
        while True:
            tokens = self.held_tokens()
            if tokens:
                session = SessionLocal()
                try:
                    job_queue.pump_heartbeats(session, tokens)
                except Exception as exc:
                    log.exception("heartbeat pump failed")
                    report_error(exc, where="worker.heartbeat_loop")
                    session.rollback()
                finally:
                    session.close()
            if self._stop.is_set():
                if not self.held_tokens():
                    break
                time.sleep(min(1.0, settings.job_heartbeat_interval_seconds))
            else:
                self._stop.wait(settings.job_heartbeat_interval_seconds)

    def _sweep_once(self) -> None:
        session = SessionLocal()
        try:
            recovered = job_queue.sweep_stale_jobs(session)
            for job_id, outcome in recovered:
                log.info("stale job %s recovered: %s", job_id, outcome)
        except Exception as exc:
            log.exception("stale-job sweep failed")
            report_error(exc, where="worker.sweep_loop")
            session.rollback()
        finally:
            session.close()

    def _sweep_loop(self) -> None:
        """Sweeper thread (§7.4): at startup AND every sweep interval."""
        self._sweep_once()
        while not self._stop.wait(settings.job_sweep_interval_seconds):
            self._sweep_once()

    # ── job execution ──────────────────────────────────────────────────

    def _run_job(self, job: Job) -> None:
        self._hold(job)
        session: Session | None = None
        try:
            session = _work_session()
            handler = job_queue.get_handler(job.kind)
            if handler is None:
                job_queue.mark_failed(
                    session, job.id, job.lease_token,
                    error_code="no_handler",
                    error_message="This job type has no registered worker.",
                )
                return
            try:
                handler(session, job)
            except Exception as exc:
                log.exception("handler for job %s (%s) raised", job.id, job.kind)
                report_error(exc, where=f"worker.job.{job.kind}", context={"job_id": job.id})
                session.rollback()
                if not job_queue.mark_failed(
                    session, job.id, job.lease_token,
                    error_code="handler_error",
                    error_message="Something went wrong while running this job.",
                ):
                    log.info("job %s: lease no longer held — abandoning failure write", job.id)
                return
            # Handler returned: succeeded — unless the handler already
            # finished the job itself or the lease was recovered meanwhile
            # (0 rows matched → no-op, terminal state preserved).
            if not job_queue.mark_done(session, job.id, job.lease_token):
                log.info("job %s: already terminal or lease lost — mark_done was a no-op", job.id)
        except Exception as exc:
            log.exception("job %s bookkeeping failed", job.id)
            report_error(exc, where="worker.job_bookkeeping", context={"job_id": job.id})
            if session is not None:
                session.rollback()
        finally:
            self._drop(job)
            if session is not None:
                session.close()

    # ── main loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("worker %s starting (concurrency %s)",
                 self.worker_id, settings.worker_max_concurrency)
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        sweeper = threading.Thread(target=self._sweep_loop, name="sweeper", daemon=True)
        heartbeat.start()
        sweeper.start()

        in_flight: set[Future] = set()
        with ThreadPoolExecutor(max_workers=settings.worker_max_concurrency) as executor:
            while not self._stop.is_set():
                in_flight = {f for f in in_flight if not f.done()}
                if len(in_flight) >= settings.worker_max_concurrency:
                    self._stop.wait(settings.worker_poll_interval_seconds)
                    continue
                session = SessionLocal()
                try:
                    job = job_queue.claim_next_job(session, worker_id=self.worker_id)
                finally:
                    session.close()
                if job is None:
                    self._stop.wait(settings.worker_poll_interval_seconds)
                    continue
                in_flight.add(executor.submit(self._run_job, job))
            # Graceful shutdown: stop claiming, drain in-flight jobs.
            log.info("worker %s draining %d in-flight job(s)", self.worker_id, len(in_flight))
        heartbeat.join(timeout=settings.job_heartbeat_interval_seconds * 2)
        log.info("worker %s stopped", self.worker_id)

    def stop(self, *_args) -> None:
        self._stop.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from app.core.error_sink import install as install_error_sink

    install_error_sink()
    # Kind registrations (handlers + recovery reconcilers) side-effect on
    # import — one line per job kind.
    import app.services.site_check.worker_handler  # noqa: F401

    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
