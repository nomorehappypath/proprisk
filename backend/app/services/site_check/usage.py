"""Actual-provider-request accounting.

Per docs/specs/site_check.md §8.6. `site_check_provider_usage` records
**actual upstream provider requests**, not logical adapter outcomes:
every outbound HTTP request an adapter makes registers itself on a
per-run collector (`ProviderCallCollector`), and the collector is
flushed into one aggregate row per (run, provider, service) in the same
transaction as the run record that triggered it.

Why the collector exists (review finding 4, 2026-08-01): recording one
row per logical adapter OUTCOME hid real money. With Nominatim and
ArcGIS missing and Google returning ZERO_RESULTS, one paid Google
request had happened but the ledger wrote `arcgis/geocode_stored
calls=1` and no google row at all. Metering at the request boundary
makes failed, zero-result, retried and internal-fallback requests all
visible, and makes the recorded provider the one that was actually
billed.

Costs are exact integer MICRODOLLARS (1 USD = 1,000,000 µ$) end to end
— rate card, free-tier allocation, aggregation and persistence are all
integer arithmetic; float appears only in the render-time helpers at
the bottom of this module.

Free-tier accounting is global per provider per month and is allocated
ATOMICALLY: the decision is taken under a transaction-scoped Postgres
advisory lock keyed on (provider, service, month), so two concurrent
runs can never both consume the last free unit.

v0 = estimated cost. v1 = invoice reconciliation (separate spec).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import SiteCheckProviderUsage, SiteCheckRun

log = logging.getLogger("propdesk.site_check.usage")

# Lossless sub-cent accounting (site_check.md §8.6): 1 USD = 1,000,000
# microdollars. Aggregation happens in microdollars; rounding to
# cents/dollars only at render time.
MICRO_USD_PER_USD = 1_000_000

# Column widths on site_check_provider_usage — every provider/service
# label is clamped so an unexpected upstream host can never raise a
# DataError mid-run (analysis must never stop).
_PROVIDER_MAX = 40
_SERVICE_MAX = 60

# Sentinel used by the free providers below: "no meaningful monthly
# ceiling". Combined with unit_cost_micro=0 it short-circuits the
# free-tier allocation entirely (nothing to bill either way).
_UNLIMITED = 10_000_000


# ── Rate card (verified live 2026-05-04) ─────────────────────────


@dataclass(frozen=True)
class RateCard:
    """Free-tier units per month + per-unit cost past free tier.

    `unit_cost_micro` is INTEGER microdollars per billable unit — money
    never passes through a float (review finding 4): $4/1K = 4,000 µ$
    per call, $0.05/1K = 50 µ$ per call.

    Quoted from the live provider pricing pages on 2026-05-04
    (sources in docs/specs/public_data.md §9). Update with the
    same diligence as the spec — bump per-PR with the link to
    the price page note.
    """

    free_units_per_month: int
    unit_cost_micro: int  # past free tier, integer µ$ per unit


_RATES: dict[tuple[str, str], RateCard] = {
    # ── Nominatim (primary geocoder — free, ToS-limited) ──────
    # §8.6 geocoder-path contract: a Nominatim success records a
    # nominatim row at cost 0 and NO arcgis row.
    ("nominatim", "geocode_stored"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    # ── ArcGIS Location Platform ──────────────────────────────
    # Stored geocoding: $4/1K, NO free tier (we persist lat/lng
    # so we're in stored mode per spec §8.1).
    ("arcgis", "geocode_stored"): RateCard(
        free_units_per_month=0,
        unit_cost_micro=4_000,
    ),
    # Places Nearby: 500 free/mo, then $8/1K.
    ("arcgis", "places_nearby"): RateCard(
        free_units_per_month=500,
        unit_cost_micro=8_000,
    ),
    # Place Attributes (cheapest tier — Place attributes only,
    # not Address/Details/Location): 100 free/mo, $0.05/1K.
    ("arcgis", "places_attributes"): RateCard(
        free_units_per_month=100,
        unit_cost_micro=50,
    ),

    # ── Free providers — write the row anyway (audit) ─────────
    ("fema", "fetch"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("txdot", "fetch"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("census", "acs_5yr"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    # Census county-from-point lookup (address step, outside Texas).
    ("census", "county_lookup"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("osm", "fetch"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("austin_socrata", "fetch"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("tcad", "fetch"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    # ── Aerial imagery providers (TX-public, free) ────────────
    ("tnris", "orthoimagery_chip"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("usda_naip", "orthoimagery_chip"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("usgs_orthoimagery", "orthoimagery_chip"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),
    ("usgs", "elevation_point_query"): RateCard(
        free_units_per_month=_UNLIMITED,
        unit_cost_micro=0,
    ),

    # ── Google Maps Platform (Tier B fallback only) ──────────
    # Pro tier as of 2025-03-01 (replaces $200 generic credit).
    # NOTE: ("google", "geocode") is NOT in this table — its rate is
    # settings-driven (google_geocode_cost_micro_per_call, no hardcoded
    # price) and resolved in _rate_for below. Free tier is deliberately
    # 0: the platform key is SHARED across deployments, so this
    # database's monthly counter cannot see the key's true free-tier
    # position — pricing every REQUEST is the conservative estimate.
    ("google", "places_nearby"): RateCard(
        free_units_per_month=5_000,
        unit_cost_micro=32_000,
    ),
    ("google", "place_details"): RateCard(
        free_units_per_month=5_000,
        unit_cost_micro=17_000,
    ),
}


def _rate_for(provider: str, service: str) -> RateCard | None:
    """Rate-card lookup. Static entries come from _RATES; the Google
    geocode final-fallback tier (Admin 2026-08-01) is settings-driven
    so a price change is an .env edit, never a code edit. The settings
    value is already integer microdollars — it is used verbatim."""
    if (provider, service) == ("google", "geocode"):
        return RateCard(
            free_units_per_month=0,  # shared platform key — see _RATES note
            unit_cost_micro=int(
                get_settings().google_geocode_cost_micro_per_call
            ),
        )
    return _RATES.get((provider, service))


# ── Actual-request instrumentation (§8.6 request boundary) ───────


@dataclass(frozen=True)
class ProviderCallEvent:
    """One aggregated (provider, service) slice of actual requests."""

    provider: str
    service: str
    calls: int
    billable_units: int
    retried_calls: int


@dataclass(frozen=True)
class _Route:
    """Maps an outbound URL onto the ACTUAL billed upstream.

    Only upstreams whose identity differs from the calling adapter's
    registry row are listed — i.e. every provider we hold a key for
    (and therefore pay for) plus the geocoder tiers, which deliberately
    bill under three different provider names from one source_id. Every
    other host is attributed to the source's registry provider/service
    via `attribute_to`, which is the truth for single-provider
    adapters and keeps the §8.6 row names stable.
    """

    host: str
    path_prefix: str | None
    provider: str
    service: str


# Order matters: the first matching route wins, so a more specific
# path_prefix must precede the host-wide catch-all.
_ROUTES: tuple[_Route, ...] = (
    # Geocoder tiers — three real upstreams behind one source_id.
    _Route("nominatim.openstreetmap.org", None, "nominatim", "geocode_stored"),
    # Free Census county map, asked by the address step outside Texas.
    _Route("tigerweb.geo.census.gov", None, "census", "county_lookup"),
    _Route("geocode-api.arcgis.com", None, "arcgis", "geocode_stored"),
    _Route(
        "maps.googleapis.com", "/maps/api/geocode", "google", "geocode",
    ),
    # ArcGIS Location Platform Places (also reached as the OSM
    # internal fallback, where the calling source is osm_overpass).
    _Route(
        "places-api.arcgis.com",
        "/arcgis/rest/services/places-service/v1/places/near-point",
        "arcgis", "places_nearby",
    ),
    _Route("places-api.arcgis.com", None, "arcgis", "places_attributes"),
    # Google Places (not wired in v0; routed so a future adapter
    # cannot land unmetered).
    _Route(
        "maps.googleapis.com", "/maps/api/place/nearbysearch",
        "google", "places_nearby",
    ),
    _Route(
        "maps.googleapis.com", "/maps/api/place/details",
        "google", "place_details",
    ),
    # Overpass mirrors — the OSM fallback path above means an
    # osm_overpass run can legitimately bill two different providers.
    _Route("overpass-api.de", None, "osm", "fetch"),
    _Route("overpass.kumi.systems", None, "osm", "fetch"),
    _Route("overpass.private.coffee", None, "osm", "fetch"),
)


def classify_request(url: str) -> tuple[str, str] | None:
    """(provider, service) for an outbound URL, or None when the host is
    not one of the identity-bearing upstreams in `_ROUTES`."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    path = parts.path or "/"
    for route in _ROUTES:
        if host != route.host:
            continue
        if route.path_prefix and not path.startswith(route.path_prefix):
            continue
        return route.provider, route.service
    return None


def _clamp(value: str, limit: int) -> str:
    cleaned = (value or "unknown").strip() or "unknown"
    return cleaned[:limit]


def _fallback_labels(url: str) -> tuple[str, str]:
    """Last-resort attribution for a request made with no source
    context and no route: the host itself. Never silently dropped —
    an unattributed request is still a request that happened."""
    host = (urlsplit(url).hostname or "unknown").lower()
    return _clamp(host, _PROVIDER_MAX), "fetch"


# Per-thread attribution + logical-request state. The orchestrator fans
# adapters out across a ThreadPoolExecutor, so both must be thread-local
# and both are entered INSIDE the worker thread.
_local = threading.local()


@contextmanager
def attribute_to(provider: str, service: str) -> Iterator[None]:
    """Default (provider, service) for requests this thread makes to
    hosts that are not in `_ROUTES`. Set by the orchestrator around one
    adapter dispatch from its registry row; routed hosts always win, so
    the geocoder tiers and the OSM→ArcGIS fallback still bill under
    their real upstream."""
    previous = getattr(_local, "attribution", None)
    _local.attribution = (
        _clamp(provider, _PROVIDER_MAX), _clamp(service, _SERVICE_MAX),
    )
    try:
        yield
    finally:
        _local.attribution = previous


@contextmanager
def logical_request() -> Iterator[None]:
    """Mark a retry loop: the first request per (provider, service)
    inside the block is the original, every further one is a BILLED
    retry (`retried_calls`). Nesting is safe — only the outermost
    block owns the state, so the resilient-HTTP engine and an adapter
    loop wrapping it do not double-count."""
    if getattr(_local, "attempt_depth", 0):
        _local.attempt_depth += 1
        try:
            yield
        finally:
            _local.attempt_depth -= 1
        return
    _local.attempt_depth = 1
    _local.attempt_seen = set()
    try:
        yield
    finally:
        _local.attempt_depth = 0
        _local.attempt_seen = None


def _is_first_attempt(key: tuple[str, str]) -> bool:
    seen = getattr(_local, "attempt_seen", None)
    if not getattr(_local, "attempt_depth", 0) or seen is None:
        return True  # no retry loop in scope: every request stands alone
    if key in seen:
        return False
    seen.add(key)
    return True


def _transport_retries(response: object | None) -> int:
    """Requests urllib3 replayed the call at the transport layer
    (5xx/429/connect). Those are real requests the provider received
    and bills, but they never reach Session.request — recover them
    from the retry history carried on the raw response."""
    raw = getattr(response, "raw", None)
    retries = getattr(raw, "retries", None)
    history = getattr(retries, "history", None)
    try:
        return len(history) if history else 0
    except TypeError:  # pragma: no cover - defensive
        return 0


class ProviderCallCollector:
    """Thread-safe accumulator of ACTUAL provider requests for one run.

    Every outbound request registers itself here (see
    `note_session_request`), including requests that fail, that return
    zero results, that are retried, and that an adapter makes to an
    internal fallback provider. `drain()` hands the accumulated slices
    to `flush_collector`, which writes them into the §8.6 aggregate
    rows inside the caller's transaction.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], list[int]] = {}
        self._lifetime: dict[tuple[str, str], list[int]] = {}

    # -- recording ------------------------------------------------
    def record(
        self,
        provider: str,
        service: str,
        *,
        calls: int = 1,
        billable_units: int | None = None,
        retried_calls: int = 0,
    ) -> None:
        """Register `calls` actual requests. `billable_units` defaults
        to the call count (per-call pricing); providers that bill per
        returned unit pass their own count."""
        if calls <= 0:
            return
        key = (
            _clamp(provider, _PROVIDER_MAX), _clamp(service, _SERVICE_MAX),
        )
        units = calls if billable_units is None else max(0, billable_units)
        delta = (calls, units, max(0, retried_calls))
        with self._lock:
            for bucket in (self._pending, self._lifetime):
                slot = bucket.setdefault(key, [0, 0, 0])
                slot[0] += delta[0]
                slot[1] += delta[1]
                slot[2] += delta[2]

    def note_request(self, url: str, *, transport_retries: int = 0) -> None:
        """Register ONE outbound HTTP request (plus any transport-layer
        replays of it). Called for successes AND failures alike."""
        routed = classify_request(url)
        if routed is None:
            routed = getattr(_local, "attribution", None)
        if routed is None:
            routed = _fallback_labels(url)
            log.debug("unattributed provider request to %s", routed[0])
        retried = 0 if _is_first_attempt(routed) else 1
        self.record(
            routed[0], routed[1],
            calls=1 + transport_retries,
            retried_calls=retried + transport_retries,
        )

    # -- reading --------------------------------------------------
    def drain(self) -> list[ProviderCallEvent]:
        """Take everything accumulated since the last drain, in a
        canonical (provider, service) order — the order the free-tier
        allocation locks are acquired in, which is what keeps two
        concurrent flushes deadlock-free."""
        with self._lock:
            pending, self._pending = self._pending, {}
        return [
            ProviderCallEvent(
                provider=provider, service=service,
                calls=slot[0], billable_units=slot[1],
                retried_calls=slot[2],
            )
            for (provider, service), slot in sorted(pending.items())
        ]

    def totals(self) -> list[ProviderCallEvent]:
        """Everything recorded for the lifetime of this collector,
        drained or not (inspection + tests)."""
        with self._lock:
            snapshot = {k: list(v) for k, v in self._lifetime.items()}
        return [
            ProviderCallEvent(
                provider=provider, service=service,
                calls=slot[0], billable_units=slot[1],
                retried_calls=slot[2],
            )
            for (provider, service), slot in sorted(snapshot.items())
        ]


def note_session_request(
    sess: object, url: str, response: object | None = None,
) -> None:
    """Register one request against the collector carried by `sess`
    (no collector attached → no-op). The single hook both the hardened
    orchestrator session and the offline replay session call."""
    collector = getattr(sess, "provider_call_collector", None)
    if collector is None:
        return
    collector.note_request(
        url, transport_retries=_transport_retries(response),
    )


# ── Persistence ─────────────────────────────────────────────────


def _start_of_month_utc() -> datetime:
    now = datetime.now(UTC)
    return now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )


def _used_units_this_month(
    session: Session, *, provider: str, service: str,
) -> int:
    """Sum of billable_units across the current month for one
    (provider, service). Drives the free-tier cursor."""
    stmt = select(func.coalesce(func.sum(
        SiteCheckProviderUsage.billable_units,
    ), 0)).where(
        SiteCheckProviderUsage.provider == provider,
        SiteCheckProviderUsage.service == service,
        SiteCheckProviderUsage.created_at >= _start_of_month_utc(),
    )
    return int(session.execute(stmt).scalar_one() or 0)


def _free_tier_lock_key(provider: str, service: str) -> int:
    """Stable 64-bit advisory-lock key for one (provider, service,
    month) allocation cursor."""
    period = _start_of_month_utc().strftime("%Y-%m")
    digest = hashlib.blake2b(
        f"site_check_free_tier|{provider}|{service}|{period}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


def _allocate_units(
    session: Session,
    *,
    provider: str,
    service: str,
    units: int,
    rate: RateCard | None,
) -> int:
    """Allocate `units` against the global monthly free tier and return
    the PAID unit count.

    The allocation is atomic (review finding 4): the read of the
    monthly cursor and the write that consumes it happen under a
    transaction-scoped Postgres advisory lock on (provider, service,
    month), so a concurrent run blocks until this transaction commits
    and then sees the units we just consumed. Two runs can therefore
    never both spend the last free unit.

    The lock is taken ONLY where an allocation decision can change the
    money: a provider with no free tier is always paid, and a provider
    whose past-free-tier rate is 0 is always free. That leaves the four
    genuinely metered Places services, and `drain()` hands them over in
    a canonical order, so concurrent flushes cannot deadlock.
    """
    if units <= 0:
        return 0
    if rate is None:
        return 0
    if rate.free_units_per_month <= 0:
        return units          # no free tier — every unit is paid
    if rate.unit_cost_micro <= 0:
        return 0              # free past the ceiling too — nothing to bill
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _free_tier_lock_key(provider, service)},
    )
    used = _used_units_this_month(
        session, provider=provider, service=service,
    )
    free_granted = max(0, min(units, rate.free_units_per_month - used))
    return units - free_granted


def record_call(
    session: Session,
    *,
    run: SiteCheckRun,
    provider: str,
    service: str,
    billable_units: int = 1,
    request_count: int = 1,
    retried: bool = False,
    retried_calls: int | None = None,
) -> None:
    """Record actual provider requests in the same transaction as the
    run. Caller commits.

    Rows aggregate per (run, provider, service) per site_check.md §8.6 —
    the increment is ONE atomic SQL statement (INSERT … ON CONFLICT DO
    UPDATE against the (run_id, provider, service) unique constraint), so
    concurrent adapter threads can never lose an increment the way a
    find-then-increment would.

    Free-tier accounting is a global per-month allocation resolved by
    `_allocate_units` under an advisory lock; the units that fall inside
    the ceiling cost 0 and only the overflow bills at the rate card, all
    in integer microdollars.
    """
    provider = _clamp(provider, _PROVIDER_MAX)
    service = _clamp(service, _SERVICE_MAX)
    rate = _rate_for(provider, service)
    if rate is None:
        log.warning(
            "no rate card for (%s, %s) — recording with cost=0",
            provider, service,
        )
    paid_units = _allocate_units(
        session, provider=provider, service=service,
        units=billable_units, rate=rate,
    )
    estimated_micro = paid_units * (rate.unit_cost_micro if rate else 0)
    retried_increment = (
        retried_calls if retried_calls is not None else (1 if retried else 0)
    )
    stmt = pg_insert(SiteCheckProviderUsage).values(
        tenant_id=run.tenant_id,
        run_id=run.id,
        provider=provider,
        service=service,
        calls=request_count,
        billable_units=billable_units,
        retried_calls=retried_increment,
        estimated_cost_microdollars=estimated_micro,
        created_at=datetime.now(UTC),
    ).on_conflict_do_update(
        constraint="uq_site_check_provider_usage_run_provider_service",
        set_={
            "calls": SiteCheckProviderUsage.calls + request_count,
            "billable_units": (
                SiteCheckProviderUsage.billable_units + billable_units
            ),
            "retried_calls": (
                SiteCheckProviderUsage.retried_calls + retried_increment
            ),
            "estimated_cost_microdollars": (
                SiteCheckProviderUsage.estimated_cost_microdollars
                + estimated_micro
            ),
        },
    )
    session.execute(stmt)


def flush_collector(
    session: Session,
    *,
    run: SiteCheckRun,
    collector: ProviderCallCollector | None,
) -> list[ProviderCallEvent]:
    """Persist every actual provider request the collector has seen
    since the previous flush, in the caller's transaction. Returns the
    drained slices (empty when nothing was called — a cache hit or a
    reuse-seeded source makes no request and therefore writes no row).
    """
    if collector is None:
        return []
    events = collector.drain()
    for event in events:
        record_call(
            session,
            run=run,
            provider=event.provider,
            service=event.service,
            billable_units=event.billable_units,
            request_count=event.calls,
            retried_calls=event.retried_calls,
        )
    return events


def run_total_cost_usd(
    session: Session, *, run_id: str,
) -> float:
    """Sum of estimated cost for one run, in USD (aggregated in
    microdollars, converted only here for display/rollup). Used by the
    §8.7 run-completion audit rollup."""
    stmt = select(func.coalesce(func.sum(
        SiteCheckProviderUsage.estimated_cost_microdollars,
    ), 0)).where(
        SiteCheckProviderUsage.run_id == run_id,
    )
    return int(session.execute(stmt).scalar_one() or 0) / MICRO_USD_PER_USD


def per_provider_breakdown(
    session: Session, *, run_id: str,
) -> dict[str, float]:
    """{provider: estimated_cost_usd} for one run (aggregated in
    microdollars, converted only here)."""
    stmt = (
        select(
            SiteCheckProviderUsage.provider,
            func.coalesce(func.sum(
                SiteCheckProviderUsage.estimated_cost_microdollars,
            ), 0),
        )
        .where(
            SiteCheckProviderUsage.run_id == run_id,
        )
        .group_by(SiteCheckProviderUsage.provider)
    )
    return {
        row[0]: int(row[1]) / MICRO_USD_PER_USD
        for row in session.execute(stmt)
    }


__all__ = [
    "ProviderCallCollector",
    "ProviderCallEvent",
    "RateCard",
    "attribute_to",
    "classify_request",
    "flush_collector",
    "logical_request",
    "note_session_request",
    "per_provider_breakdown",
    "record_call",
    "run_total_cost_usd",
]
