"""Central error sink contract (SPEC.md chapter 8): one event per server
failure and per browser report, written with every required field and the
same reference the user sees; a dead forwarding receiver never crashes the
app or slows a request."""

import json
import time

from app.config.settings import settings
from app.core import secret_store
from app.core.error_sink import report_error


def _read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_error_sink_server_event(tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))

    reference = report_error(ValueError("boom"), where="test.server", context={"run_id": "abc"})

    events = _read_events(log_path)
    assert len(events) == 1
    event = events[0]
    assert event["reference"] == reference
    assert event["where"] == "test.server"
    assert "boom" in event["message"]
    assert event["stack"] and "ValueError" in event["stack"]
    assert event["context"] == {"run_id": "abc"}
    assert "time" in event


def test_error_sink_event_has_nmhp_error_1_fields(tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))

    reference = report_error(ValueError("boom"), where="test.server")

    event = _read_events(log_path)[0]
    assert event["schema"] == "nmhp-error/1"
    assert event["id"]
    assert event["app"] == settings.app_name
    assert event["version"] == settings.app_version
    assert event["env"] == settings.environment
    assert event["source"] == "server"
    assert event["time"]
    assert event["level"] == "error"
    assert event["kind"] == "ValueError"
    assert event["request_id"] is None
    assert event["reference"] == reference
    assert len(event["fingerprint"]) == 16


def test_secret_in_nested_context_is_scrubbed(tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))
    planted = "sk-planted-nested-secret-9f8e7d"
    secret_store.set_secret("openai", planted)
    try:
        report_error(
            RuntimeError("failed"),
            where="test.nested_secret",
            context={"detail": {"inner": [planted, {"deeper": planted}]}},
        )
        logged = log_path.read_text()
        assert planted not in logged
        assert logged.count("[redacted]") >= 2
    finally:
        secret_store.delete_secret("openai")


def test_error_sink_browser_event(client, tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))

    resp = client.post(
        "/api/client-errors",
        json={"kind": "error", "message": "TypeError: x is not a function", "url": "/public-data"},
    )
    assert resp.status_code == 204

    events = _read_events(log_path)
    assert len(events) == 1
    assert events[0]["where"] == "browser"
    assert "TypeError" in events[0]["message"]


def test_error_sink_dead_receiver(tmp_path, monkeypatch):
    log_path = tmp_path / "errors.jsonl"
    monkeypatch.setattr(settings, "error_sink_file", str(log_path))
    # A port nothing listens on, so the forwarding POST fails fast.
    monkeypatch.setattr(settings, "error_sink_url", "http://127.0.0.1:1")
    secret_store.set_secret("error_sink_token", "tok-not-real-aaaaaaaa")

    # One untimed call first: httpx's own one-time import/connection-pool
    # setup cost belongs to the process, not to any one request's slowdown.
    report_error(RuntimeError("warm-up"), where="test.dead_receiver.warmup")
    log_path.write_text("")

    started = time.monotonic()
    reference = report_error(RuntimeError("still logged locally"), where="test.dead_receiver")
    elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms < 200, f"forwarding to a dead receiver took {elapsed_ms:.0f}ms"
    events = _read_events(log_path)
    assert events[0]["reference"] == reference
