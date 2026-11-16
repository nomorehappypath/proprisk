"""The one place every PropRisk failure ends up (SPEC.md chapter 8,
following the nmhp-error/1 contract): uncaught FastAPI exceptions, uncaught
thread and top-level errors, worker job failures, and browser errors posted
from the screens. Writes one JSON line per event to a local file, rotated
so it never grows without bound, and optionally forwards the same event to
an external receiver. Never raises back into its caller.
"""

import hashlib
import json
import logging
import os
import secrets
import stat
import sys
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.settings import settings

log = logging.getLogger("proprisk.error_sink")

# The contract this module implements: directives/ERROR_SINK_CONTRACT.md,
# schema version "nmhp-error/1".
_SCHEMA = "nmhp-error/1"

_SECRET_HEADER_NAMES = {"authorization", "cookie"}
_ROTATE_MAX_BYTES = 10 * 1024 * 1024
_ROTATE_KEEP = 5

_rate_lock = threading.Lock()
_rate_window: dict[str, tuple[int, float]] = {}  # fingerprint -> (count, window_start)


def _reference() -> str:
    return secrets.token_hex(2).upper() + "-" + secrets.token_hex(2).upper()


def _fingerprint(where: str, message: str) -> str:
    return hashlib.sha256(f"{where}|{message}".encode()).hexdigest()[:16]


def _known_secret_values() -> list[str]:
    try:
        from app.core import secret_store

        values = []
        for row in secret_store.list_secrets():
            if row["is_set"]:
                secret = secret_store.get_secret(row["name"])
                if secret is not None:
                    values.append(secret.get_secret_value())
        return values
    except Exception:
        return []


def _scrub(text: str) -> str:
    for value in _known_secret_values():
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _scrub_value(value: Any) -> Any:
    # Recurses into dicts/lists/tuples so a secret nested inside a context
    # value (not just a bare top-level string) is still caught - a secret
    # reaching the error log was found to leak exactly this way.
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    return value


def _scrub_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if key.lower() in _SECRET_HEADER_NAMES or key.lower() in ("body", "request_body"):
            continue
        cleaned[key] = _scrub_value(value)
    return cleaned


def _log_path() -> Path:
    return Path(settings.error_sink_file)


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < _ROTATE_MAX_BYTES:
            return
        for index in range(_ROTATE_KEEP - 1, 0, -1):
            older = path.with_suffix(path.suffix + f".{index}")
            newer = path.with_suffix(path.suffix + f".{index - 1}") if index > 1 else path
            if newer.exists():
                newer.replace(older)
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _write_event(event: dict[str, Any]) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, stat.S_IRWXU)
        _rotate_if_needed(path)
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        if is_new:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        log.exception("could not write to the error log")


def _forward(event: dict[str, Any]) -> None:
    url = settings.error_sink_url
    if not url:
        return
    try:
        from app.core import secret_store

        token = secret_store.get_secret("error_sink_token")
        import httpx

        headers = {"Content-Type": "application/json"}
        if token is not None and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        httpx.post(url, json=event, headers=headers, timeout=2.0)
    except Exception:
        # A dead or slow receiver must never crash the app or slow a request.
        log.debug("error-sink forwarding failed", exc_info=True)


def _rate_limited(fingerprint: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        count, window_start = _rate_window.get(fingerprint, (0, now))
        if now - window_start >= 60:
            count, window_start = 0, now
        count += 1
        _rate_window[fingerprint] = (count, window_start)
        return count > settings.error_sink_rate


def report_error(
    error: BaseException | str,
    *,
    where: str,
    context: dict[str, Any] | None = None,
    source: str = "server",
    level: str = "error",
    kind: str | None = None,
    request_id: str | None = None,
) -> str:
    """Records one error event (nmhp-error/1 contract,
    directives/ERROR_SINK_CONTRACT.md) and returns a short reference code the
    user can be shown. `source`/`kind`/`level`/`request_id` let a caller that
    already knows them (the browser report, a fatal exit) set them directly;
    everything else is derived. Safe to call from anywhere; never raises."""
    reference = _reference()
    try:
        message = str(error)
        fingerprint = _fingerprint(where, message)
        if _rate_limited(fingerprint):
            return reference
        resolved_kind = kind or (type(error).__name__ if isinstance(error, BaseException) else "error")
        event = {
            "schema": _SCHEMA,
            "id": str(uuid.uuid4()),
            "app": settings.app_name,
            "version": settings.app_version,
            "env": settings.environment,
            "source": source,
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level,
            "kind": resolved_kind[:128],
            "message": _scrub(message)[:2000],
            "stack": _scrub("".join(traceback.format_exception(error)))[:16000]
            if isinstance(error, BaseException)
            else None,
            "where": where[:256],
            "request_id": request_id,
            "reference": reference,
            "fingerprint": fingerprint,
            "context": _scrub_context(context),
        }
        _write_event(event)
        _forward(event)
    except Exception:
        log.exception("error sink itself failed")
    return reference


def install() -> None:
    """Catches what Python would otherwise print and lose: uncaught
    exceptions on the main thread and on any other thread."""
    default_thread_hook = threading.excepthook

    def _on_exception(exc_type, exc_value, exc_tb) -> None:
        if exc_value is not None:
            report_error(exc_value, where="uncaught.main_thread")
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _on_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            thread_name = args.thread.name if args.thread else "?"
            report_error(args.exc_value, where=f"uncaught.thread.{thread_name}")
        default_thread_hook(args)

    sys.excepthook = _on_exception
    threading.excepthook = _on_thread_exception
