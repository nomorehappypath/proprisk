"""Shared resilient HTTP for public-data adapters.

Generalized from the TxDOT resilience work (2026-07-14): free public
providers (Esri/AGOL feature services, DOE/NETL ArcGIS, Overpass) are
latency-spiky — a request intermittently exceeds one attempt while the
service is loaded, and an instant retry re-fires into the same
overloaded window, which is how single hiccups became user-facing
"the public site timed out — refresh this source" cards.

Policy: settings-driven attempt count, per-attempt timeout, exponential
backoff + jitter between attempts. Retries cover read/connect timeouts,
connection errors, HTTP 5xx/429, and transient in-JSON ArcGIS 5xx/429
errors (including the AGOL disguised server-side timeout: code 400
"Cannot perform query"). Non-transient errors (bad query, token
required, 4xx other than 429) raise immediately — those are defects,
not load.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence

import requests

from app.config.settings import get_settings
from app.services.site_check.usage import logical_request

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


class TransientProviderError(RuntimeError):
    """In-JSON provider 5xx/429 — retryable server load, not a bad query."""


def transient_arcgis_error(data: dict) -> bool:
    """In-JSON ArcGIS errors with 5xx/429 codes are usually momentary
    server load (Esri returns them with HTTP 200); anything else (400
    bad query, token required) is a real defect — never retry those.

    One documented exception: AGOL hosted feature layers report a
    SERVER-SIDE query timeout as code 400 "Cannot perform query.
    Invalid query parameters." — identical params succeed when the
    service is healthy (observed live on TxDOT_AADT 2026-07-14: 29s
    success, then this 400, same request). That disguise is transient
    load, so it retries; other 400s still raise immediately."""
    error = data.get("error") or {}
    code = error.get("code")
    if code in TRANSIENT_HTTP_CODES:
        return True
    message = str(error.get("message") or "")
    return code == 400 and "Cannot perform query" in message


def arcgis_json_error(data: dict) -> Exception | None:
    """Classify an ArcGIS JSON payload: None when clean, a
    TransientProviderError for retryable in-JSON server load, or a
    RuntimeError for real defects (bad query, token required)."""
    if "error" not in data:
        return None
    exc = RuntimeError(
        f"ArcGIS error: {data['error'].get('message', data['error'])}"
    )
    if transient_arcgis_error(data):
        return TransientProviderError(str(exc))
    return exc


def resilient_json(
    sess: requests.Session,
    method: str,
    urls: str | Sequence[str],
    *,
    params: dict | None = None,
    data: dict | None = None,
    timeout: float | None = None,
    attempts: int | None = None,
    backoff_base: float | None = None,
    json_error: Callable[[dict], Exception | None] | None = None,
) -> dict:
    """Resilient JSON request against a public provider.

    `urls` may be one URL or a sequence of mirrors — attempts rotate
    through them (attempt 1 -> urls[0], attempt 2 -> urls[1], ...), so
    a mirror outage and a transient error share the same retry budget.
    `json_error` lets callers classify in-body provider errors that
    arrive with HTTP 200 (the ArcGIS pattern): return an exception to
    raise for the payload, and make it a TransientProviderError when
    it should consume a retry instead of failing outright. Defaults
    for timeout/attempts/backoff come from the public_intel_http_*
    settings; adapters with provider-specific tuning (TxDOT) pass
    their own values.
    """
    settings = get_settings()
    url_ring: Sequence[str] = (urls,) if isinstance(urls, str) else urls
    if not url_ring:
        raise ValueError("resilient_json requires at least one URL")
    total = attempts if attempts is not None else (
        settings.public_intel_http_attempts
    )
    per_attempt_timeout = timeout if timeout is not None else (
        settings.public_intel_http_timeout_seconds
    )
    base = backoff_base if backoff_base is not None else (
        settings.public_intel_http_backoff_base_seconds
    )
    # Exactly one retry layer per request: the hardened orchestrator
    # session exposes a retry-free sibling (same timeout floor and
    # headers) — send through it so a failing mirror is never hit
    # multiple times at the transport before ring rotation moves on.
    send_sess = getattr(sess, "no_transport_retry", sess)
    send = send_sess.post if method.upper() == "POST" else send_sess.get
    kwargs: dict = {"timeout": per_attempt_timeout}
    if params is not None:
        kwargs["params"] = params
    if data is not None:
        kwargs["data"] = data
    last_exc: Exception | None = None
    # One §8.6 logical request: every attempt after the first is a
    # BILLED retry on the provider ledger, whether it rotated to a
    # mirror or re-fired at the same URL.
    with logical_request():
        for attempt in range(1, total + 1):
            url = url_ring[(attempt - 1) % len(url_ring)]
            try:
                resp = send(url, **kwargs)
                resp.raise_for_status()
                payload = resp.json()
                if json_error is not None:
                    exc = json_error(payload)
                    if exc is not None:
                        raise exc
                return payload
            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError,
                TransientProviderError,
            ) as exc:
                status = getattr(
                    getattr(exc, "response", None), "status_code", None,
                )
                if (
                    isinstance(exc, requests.exceptions.HTTPError)
                    and status not in TRANSIENT_HTTP_CODES
                ):
                    raise  # 4xx other than 429 is a real defect, not load
                last_exc = exc
                if attempt < total and base > 0:
                    pause = base * (2 ** (attempt - 1))
                    time.sleep(pause + random.uniform(0, pause * 0.5))
    raise last_exc or RuntimeError("resilient request failed")


__all__ = [
    "TRANSIENT_HTTP_CODES",
    "TransientProviderError",
    "arcgis_json_error",
    "resilient_json",
    "transient_arcgis_error",
]
