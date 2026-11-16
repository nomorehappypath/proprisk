"""Global source cache — read-through semantics (site_check.md §9.3).

`site_check_source_cache` is the SOLE non-tenant domain table: it holds
adapter-style payloads of PUBLIC facts only (no narratives, no tenant or
user data). Contract, ported from the reference app's `public_source_cache`:

  - fresh hit → no provider call;
  - stale/missing → fetch; only a SUCCESSFUL newer fetch overwrites a
    stored payload (atomic ON CONFLICT upsert — concurrent writers safe);
  - empty-facts / failure payloads are NEVER cached;
  - expired-row stale-fallback when a refresh fails, flagged
    `stale=True` (surfaced on the card as "from an older saved copy");
  - `force_refresh=True` (mode="fresh", §9.1) bypasses EVERY read —
    including the stale-fallback, which is FORBIDDEN in fresh mode; a
    successful fresh result still updates the cache.

Default TTL = `site_check_freshness_days`; per-call `ttl_days` override
allowed (Phase B's decade-stable aerial eras may set longer). Phase A
ships the contract; no ported adapter reads the cache at the pinned SHA.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models import SiteCheckSourceCache

log = logging.getLogger("propdesk.site_check.source_cache")

# Payload keys that would smuggle tenant/user/narrative data into the
# shared cache — a payload carrying any of these is refused (§9.3: public
# facts only; asserted by test).
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "tenant_id",
    "user_id",
    "requested_by",
    "narrative",
    "narratives",
    "card_narratives",
    "card_narratives_meta",
    "narrative_meta",
})


def cache_key(source_id: str, scope_key: str, producer_version: int) -> str:
    """sha256 over the scope triple — the stable §9.3 cache_key."""
    raw = f"{source_id}|{scope_key}|{producer_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cacheable(payload: Any) -> bool:
    """§9.3: only successful, non-empty PUBLIC fact payloads are cached."""
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return False
    if isinstance(errors, str) and errors.strip():
        return False
    facts = payload.get("normalized_facts")
    if not isinstance(facts, dict) or not facts:
        return False
    if _FORBIDDEN_PAYLOAD_KEYS & set(payload):
        log.warning(
            "refusing to cache payload carrying tenant/user/narrative keys: %s",
            sorted(_FORBIDDEN_PAYLOAD_KEYS & set(payload)),
        )
        return False
    return bool(_FORBIDDEN_PAYLOAD_KEYS.isdisjoint(facts))


def store(
    session: Session,
    *,
    source_id: str,
    scope_key: str,
    producer_version: int,
    payload: dict,
    source_metadata: dict | None = None,
    ttl_days: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomic upsert of a SUCCESSFUL fetch. Concurrent writers are safe:
    ON CONFLICT updates the row only when this fetch is at least as new
    as the stored one. Returns False (and writes nothing) for payloads
    that must not be cached. Caller commits."""
    if not _cacheable(payload):
        return False
    now = now or datetime.now(UTC)
    days = ttl_days if ttl_days is not None else get_settings().site_check_freshness_days
    stmt = pg_insert(SiteCheckSourceCache).values(
        id=str(uuid.uuid4()),
        source_id=source_id,
        scope_key=scope_key,
        producer_version=producer_version,
        cache_key=cache_key(source_id, scope_key, producer_version),
        payload=payload,
        source_metadata=source_metadata,
        fetched_at=now,
        expires_at=now + timedelta(days=days),
    ).on_conflict_do_update(
        constraint="uq_site_check_source_cache_scope",
        set_={
            "payload": payload,
            "source_metadata": source_metadata,
            "fetched_at": now,
            "expires_at": now + timedelta(days=days),
        },
        where=(SiteCheckSourceCache.fetched_at <= now),
    )
    session.execute(stmt)
    return True


def get_or_fetch(
    session: Session,
    *,
    source_id: str,
    scope_key: str,
    producer_version: int,
    fetch: Callable[[], dict],
    ttl_days: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """§9.3 read-through. Returns
    `{payload, from_cache, stale, fetched_at}`.

    - fresh cache hit → stored payload, no provider call;
    - stale/missing → `fetch()`; success upserts and returns live;
    - fetch failed + an expired row exists → stale-fallback
      (`stale=True`) — EXCEPT in force_refresh mode, where the failure
      propagates honestly;
    - `force_refresh=True` → NO cache read of any kind; a successful
      result still updates the cache. Caller commits.
    """
    now = datetime.now(UTC)
    row: SiteCheckSourceCache | None = None
    if not force_refresh:
        with session.no_autoflush:
            row = session.scalars(
                select(SiteCheckSourceCache).where(
                    SiteCheckSourceCache.source_id == source_id,
                    SiteCheckSourceCache.scope_key == scope_key,
                    SiteCheckSourceCache.producer_version == producer_version,
                )
            ).first()
        if row is not None:
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > now:
                return {
                    "payload": row.payload,
                    "from_cache": True,
                    "stale": False,
                    "fetched_at": row.fetched_at,
                }

    try:
        payload = fetch()
    except Exception:
        if not force_refresh and row is not None:
            # Expired-row stale-fallback (§9.3) — flagged so the card
            # says "from an older saved copy". FORBIDDEN in fresh mode.
            log.warning(
                "live fetch failed for %s/%s — serving the expired cached copy",
                source_id, scope_key,
            )
            return {
                "payload": row.payload,
                "from_cache": True,
                "stale": True,
                "fetched_at": row.fetched_at,
            }
        raise

    store(
        session,
        source_id=source_id,
        scope_key=scope_key,
        producer_version=producer_version,
        payload=payload,
        ttl_days=ttl_days,
        now=now,
    )
    return {
        "payload": payload,
        "from_cache": False,
        "stale": False,
        "fetched_at": now,
    }


__all__ = ["cache_key", "get_or_fetch", "store"]
