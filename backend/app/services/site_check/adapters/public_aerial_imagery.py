"""public_aerial_imagery adapter — public-domain aerial chip.

Activated 2026-05-31 after the §13 gate-4 live probe (see
docs/specs/_poc/public_aerial_imagery_2026-05-04.md). For a geocoded
property this returns a deterministic USGS National Map `exportImage`
URL, validated by one server-side liveness GET. The browser renders
that URL directly:

  - public-domain federal orthoimagery (NAIP + USGS, 6in-1m),
  - anonymous — no API key,
  - no per-open billing, no cached bytes in v0.

Pure function, no DB writes (per _base.py §contract). The orchestrator
lifts the returned facts into `derived.project_imagery` via
`_build_derived`. Caching the rendered bytes as a frozen artifact is a
v1 concern that only matters once the deferred aerial vision-pass lands
(site_analysis_workflow.md §7.4, Tier B/C); v0 references the live URL.

Confidence is `screened_indicator` per public_data.md §11.6 — imagery is
visual context, never a `verified_public_fact`.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import requests

from app.config.settings import get_settings
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "public_aerial_imagery"

_DEG_PER_METRE = 1.0 / 111_320.0

# Defaults — every one is overridable via settings (see _config()).
_DEFAULT_BASE_URL = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/"
    "USGSImageryOnly/MapServer/export"
)
_DEFAULT_IMAGE_SIZE_PX = 640
_DEFAULT_RADIUS_M = 250        # ~500 m square context view
_DEFAULT_MIN_RADIUS_M = 120
_DEFAULT_MAX_RADIUS_M = 800
_DEFAULT_TIMEOUT_S = 20
_DEFAULT_IMAGE_FORMAT = "jpg"
_DEFAULT_TTL_DAYS = 365

_PROVIDER_LABEL = "USGS National Map"
_ATTRIBUTION = "Imagery: USGS National Map / NAIP (public domain)"
_CAPTION = (
    "Aerial imagery — USGS National Map. Visual context only; "
    "not field-verified."
)


class _Config:
    """Resolved, env-overridable config. Read from settings with
    getattr defaults so test SimpleNamespace stand-ins still work."""

    def __init__(self, settings_obj: Any) -> None:
        g = lambda name, default: getattr(settings_obj, name, default)
        self.base_url: str = g(
            "public_aerial_imagery_export_base_url", _DEFAULT_BASE_URL,
        )
        self.size_px: int = int(
            g("public_aerial_imagery_image_size_px", _DEFAULT_IMAGE_SIZE_PX)
        )
        self.default_radius_m: int = int(
            g("public_aerial_imagery_default_radius_m", _DEFAULT_RADIUS_M)
        )
        self.min_radius_m: int = int(
            g("public_aerial_imagery_min_radius_m", _DEFAULT_MIN_RADIUS_M)
        )
        self.max_radius_m: int = int(
            g("public_aerial_imagery_max_radius_m", _DEFAULT_MAX_RADIUS_M)
        )
        self.timeout_s: int = int(
            g("public_aerial_imagery_timeout_s", _DEFAULT_TIMEOUT_S)
        )
        self.image_format: str = g(
            "public_aerial_imagery_image_format", _DEFAULT_IMAGE_FORMAT,
        )
        self.ttl_days: int = int(
            g("public_aerial_imagery_ttl_days", _DEFAULT_TTL_DAYS)
        )


def _config(adapter_config: dict | None) -> _Config:
    return _Config(get_settings())


# ── Envelope sizing ───────────────────────────────────────────────


def _radius_m(identity: PropertyIdentity, cfg: _Config) -> int:
    """Pick the half-span of the view box, in metres.

    Prefer a size that comfortably frames the parcel: if acreage is
    known, frame ~1.5x the parcel's square-equivalent side so the lot
    sits inside with surrounding context; otherwise a fixed default.
    Clamped to [min, max] so a 0.1-ac pad and a 200-ac tract both
    produce a usable chip.
    """
    radius = cfg.default_radius_m
    if identity.acreage is not None and identity.acreage > 0:
        side_m = math.sqrt(float(identity.acreage) * 4047.0)
        radius = math.ceil(side_m * 1.5)
    return max(cfg.min_radius_m, min(cfg.max_radius_m, radius))


def _bounds_from_geometry(geometry: Any) -> tuple[float, float, float, float] | None:
    """(xmin, ymin, xmax, ymax) in lng/lat from a GeoJSON-shaped
    polygon, or None when the geometry is missing/unparseable.
    Defensive — parcel_geometry is rarely present and shapes vary."""
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)):
        return None
    xs: list[float] = []
    ys: list[float] = []

    def _walk(node: Any) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) == 2
            and all(isinstance(v, (int, float)) for v in node)
        ):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(coords)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _envelope(identity: PropertyIdentity, cfg: _Config) -> dict:
    """Square (in metres) view envelope around the property, in
    EPSG:4326. Uses the parcel polygon bounds (padded) when available,
    else a centroid radius. lng span is widened by 1/cos(lat) so the
    box is metric-square; imageSR=3857 keeps the chip visually square.
    """
    lat = float(identity.lat)  # caller guarantees not None
    lng = float(identity.lng)
    radius_m = _radius_m(identity, cfg)

    bounds = _bounds_from_geometry(identity.parcel_geometry)
    if bounds is not None:
        xmin, ymin, xmax, ymax = bounds
        # Center on the parcel; pad to at least the radius half-span so
        # a sliver parcel still gets context, then keep it square.
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        half_lat = max(
            (ymax - ymin) / 2.0 * 1.3,
            radius_m * _DEG_PER_METRE,
        )
        half_lng = max(
            (xmax - xmin) / 2.0 * 1.3,
            radius_m * _DEG_PER_METRE / max(0.01, math.cos(math.radians(cy))),
        )
        return {
            "xmin": cx - half_lng, "ymin": cy - half_lat,
            "xmax": cx + half_lng, "ymax": cy + half_lat,
            "center_lat": cy, "center_lng": cx, "radius_m": radius_m,
            "anchor": "parcel_geometry",
        }

    dlat = radius_m * _DEG_PER_METRE
    dlng = radius_m * _DEG_PER_METRE / max(0.01, math.cos(math.radians(lat)))
    return {
        "xmin": lng - dlng, "ymin": lat - dlat,
        "xmax": lng + dlng, "ymax": lat + dlat,
        "center_lat": lat, "center_lng": lng, "radius_m": radius_m,
        "anchor": "centroid",
    }


def _build_url(env: dict, cfg: _Config) -> str:
    """Deterministic USGS exportImage URL. Same inputs → same URL, so
    re-runs of the same address render the identical chip."""
    bbox = f"{env['xmin']},{env['ymin']},{env['xmax']},{env['ymax']}"
    params = {
        "bbox": bbox,
        "bboxSR": "4326",
        "imageSR": "3857",
        "size": f"{cfg.size_px},{cfg.size_px}",
        "format": cfg.image_format,
        "transparent": "false",
        "f": "image",
    }
    return f"{cfg.base_url}?{urlencode(params)}"


# ── Liveness validation ───────────────────────────────────────────


def _validate_image_url(
    sess: requests.Session, url: str, cfg: _Config,
) -> tuple[bool, str | None]:
    """One server-side GET to confirm the endpoint serves a real image
    (ArcGIS can answer 200 with a JSON error envelope, which would
    otherwise reach the UI as a broken <img>). 3 attempts, 1s/2s/4s
    backoff on 429/5xx. Returns (ok, error_text). Streams headers only
    — the bytes are not stored; the browser fetches for display.
    """
    last_err: str | None = None
    for attempt in range(3):
        resp = None
        try:
            resp = sess.get(url, timeout=cfg.timeout_s, stream=True)
            status = resp.status_code
            if status in (429, 500, 502, 503, 504):
                last_err = f"USGS transient HTTP {status}"
                raise requests.HTTPError(last_err, response=resp)
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                # 200 but not an image → ArcGIS error envelope or HTML.
                return False, (
                    f"USGS returned non-image content-type {content_type!r}"
                )
            return True, None
        except (requests.ConnectionError, requests.Timeout,
                requests.HTTPError) as exc:
            last_err = str(exc)
            if attempt == 2:
                return False, last_err
            time.sleep(1 * (2 ** attempt))
        finally:
            if resp is not None:
                resp.close()
    return False, last_err or "USGS validation failed"


# ── Adapter entrypoint ────────────────────────────────────────────


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    cfg = _config(config)

    if identity.lat is None or identity.lng is None:
        # Honest absence — no anchor, so no chip. Empty facts keep
        # derived.project_imagery null; the UI hides the image cleanly.
        return _no_image(
            "lat/lng required for public_aerial_imagery — geocode first",
        )

    sess = http_session(session)
    env = _envelope(identity, cfg)
    url = _build_url(env, cfg)

    ok, err = _validate_image_url(sess, url, cfg)
    if not ok:
        return _no_image(
            f"USGS aerial imagery unavailable: {err or 'unknown error'}",
        )

    facts: dict[str, Any] = {
        "image_url": url,
        "bbox": {
            "xmin": env["xmin"], "ymin": env["ymin"],
            "xmax": env["xmax"], "ymax": env["ymax"], "sr": 4326,
        },
        "center": {"lat": env["center_lat"], "lng": env["center_lng"]},
        "size_px": {"w": cfg.size_px, "h": cfg.size_px},
        "radius_m": env["radius_m"],
        "anchor": env["anchor"],
        "provider": _PROVIDER_LABEL,
        "source": "USGS National Map (USGSImageryOnly)",
        "attribution": _ATTRIBUTION,
        "caption": _CAPTION,
        "license": "public domain",
        "is_field_verified": False,
    }

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=facts,
        fetched_at=datetime.now(UTC),
        # The orthoimage is static — the orchestrator reuses this chip
        # across refreshes until it is this old (per Admin 2026-05-31).
        ttl_seconds=cfg.ttl_days * 86_400,
        identifier_used="lat_lng",
    )


def _no_image(reason: str) -> AdapterResult:
    """No chip produced (no geocode anchor, or USGS unavailable). Honest
    non-success: coverage=PARTIAL (the adapter could not complete), empty
    facts so `derived.project_imagery` stays null and the UI hides the
    surface. NOT `covered` — a failed/incomplete fetch must not read as a
    successful one (review finding 2026-05-31)."""
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={},
        advisories=[reason],
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
