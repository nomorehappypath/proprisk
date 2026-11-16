"""Adapter base + activation status types.

Per docs/specs/public_data.md §6.3 — every adapter exposes one
function `fetch_<source_id>(identity, *, session, config) ->
AdapterResult`. Pure function; no DB writes; no LLM calls.

Active vs wired-inactive distinction (§4 activation matrix):
  - Active adapters do real provider calls.
  - Wired-inactive adapters return AdapterResult with
    coverage_status="not_yet_wired" so downstream consumers see
    a real result and apply the floor mechanic.
  - If something forces a wired-inactive adapter to do a real
    fetch (programming error), it raises AdapterInactiveError.

This file holds the shared error type + a small helper that
inactive adapters use to emit the correct stub result.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config.settings import get_settings
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.usage import (
    ProviderCallCollector,
    note_session_request,
)


class AdapterInactiveError(Exception):
    """Raised when code forces a wired-inactive adapter to
    perform a real fetch. Inactive adapters are stubs by design —
    they should only be invoked through the registry, which
    short-circuits to a `coverage_status="not_yet_wired"` result.
    Hitting this error means the activation matrix and the
    runtime call path drifted apart."""


def stub_inactive_result(source_id: str) -> AdapterResult:
    """Standard "this adapter exists in the registry but is not
    yet activated" result. Used by every wired-inactive adapter
    file (and by the orchestrator's no-op path)."""
    return AdapterResult(
        source_id=source_id,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
        ),
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_YET_WIRED,
        normalized_facts={},
        advisories=[
            (f"adapter {source_id!r} is wired-inactive — graduate "
            "per public_data.md §15.1 governance to activate.")
        ],
    )


class _PublicIntelSession(requests.Session):
    """Session with a per-request read-timeout floor.

    Adapters historically carried their own too-short timeouts
    (20-35s). Public GIS layers legitimately take 40-60s in their
    slow mode, and an early client abort discards the server's work
    and restarts it on retry — which is how single provider hiccups
    became user-facing "public site timed out" cards on most sources
    (2026-07-15). The floor lifts every request to the shared patient
    per-attempt timeout; explicitly larger values (e.g. TxDOT's 90s)
    are respected.

    It is also the §8.6 provider-usage measuring point: EVERY outbound
    request registers itself on the run's collector before the result
    is known, so failed, zero-result, retried and internal-fallback
    requests are all metered — the ledger counts what the provider
    actually served, not what the adapter concluded."""

    def request(self, method, url, **kwargs):  # type: ignore[override]
        floor = get_settings().public_intel_http_timeout_seconds
        timeout = kwargs.get("timeout")
        if timeout is None or (
            isinstance(timeout, (int, float)) and timeout < floor
        ):
            kwargs["timeout"] = floor
        response = None
        try:
            response = super().request(method, url, **kwargs)
            return response
        finally:
            note_session_request(self, url, response)


def attach_collector(
    sess: requests.Session, collector: ProviderCallCollector | None,
) -> requests.Session:
    """Point a session (and its retry-free sibling) at one run's
    provider-call collector. Idempotent; a None collector detaches."""
    sess.provider_call_collector = collector  # type: ignore[attr-defined]
    sibling = getattr(sess, "no_transport_retry", None)
    if sibling is not None and sibling is not sess:
        sibling.provider_call_collector = collector  # type: ignore[attr-defined]
    return sess


def http_session(
    base: requests.Session | None = None,
    *,
    collector: ProviderCallCollector | None = None,
) -> requests.Session:
    """Reuse a single Session per orchestrator run to keep
    connection pools warm. Adapters accept an optional session
    argument; orchestrator constructs one and threads through.

    The constructed session is hardened for every adapter at once:
    a read-timeout floor (see _PublicIntelSession) plus transport
    retries with exponential backoff for connection errors and HTTP
    5xx/429 (Retry-After respected). Read timeouts are deliberately
    NOT retried at the transport — the app-level engine in
    _resilient_http owns read-timeout patience where it is wired
    (TxDOT, site constraints), and retrying them on both layers
    would multiply worst-case wall time. raise_on_status=False keeps
    each adapter's own raise_for_status error semantics intact after
    status retries are exhausted.

    Exactly one retry layer per request: the session carries a
    retry-free sibling under `.no_transport_retry` (same timeout
    floor, same headers, no transport retries). The app-level engine
    in _resilient_http sends through that sibling when present, so a
    failing mirror is never hit multiple times at the transport
    before ring rotation moves on (review finding 2026-07-15).
    Adapters that do NOT use the engine keep full transport
    protection.

    `collector` is the run's §8.6 provider-call collector. The
    orchestrator builds one per run and passes it here once; every
    adapter then threads the same session through, so an adapter that
    calls a second provider internally (osm_overpass → ArcGIS Places)
    meters both upstreams without knowing the ledger exists."""
    if base is not None:
        if collector is not None:
            attach_collector(base, collector)
        return base
    settings = get_settings()
    s = _PublicIntelSession()
    s.headers.update({
        "User-Agent": (
            "PropDesk/1.0 (propdesk_control_plane site-check; "
            "support@reference.app)"
        ),
        "Accept": "application/json",
    })
    retry = Retry(
        total=None,
        connect=2,
        read=False,  # re-raise ReadTimeout immediately, untouched
        status=2,
        other=0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET", "POST", "HEAD"}),
        backoff_factor=settings.public_intel_http_backoff_base_seconds,
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    plain = _PublicIntelSession()
    plain.headers = s.headers  # shared view — one identity for both
    s.no_transport_retry = plain  # type: ignore[attr-defined]
    return attach_collector(s, collector)


__all__ = [
    "AdapterInactiveError",
    "AdapterResult",
    "PropertyIdentity",
    "ProviderCallCollector",
    "attach_collector",
    "http_session",
    "stub_inactive_result",
]
