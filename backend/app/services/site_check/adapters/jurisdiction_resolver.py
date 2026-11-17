"""jurisdiction_resolver — authoritative point-in-polygon jurisdiction verdict.

Determines whether a parcel is in a city's full-purpose limits, ETJ, or
unincorporated county, from authoritative public county/city GIS layers — so
zoning standards, ordinance applicability, and entitlement stop guessing
"Cedar Park · partial / unknown".

Spec: docs/specs/jurisdiction_resolver.md (reviewer REVIEW_PASSED 2026-05-28).
Same clean public-ArcGIS pattern as plat_reader._COUNTY_PLAT_GIS — NOT gated
clerk scraping. Per-county wired in `_COUNTY_JURISDICTION_GIS`; counties without
an entry return `unknown` + an honest manual step (never a guessed verdict).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

import requests

from app.regions import loader as regions
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

log = logging.getLogger("propdesk.site_check.jurisdiction_resolver")

SOURCE_ID = "jurisdiction_resolver"
_TIMEOUT = 15

# Per-county jurisdiction GIS capability map. Each entry points at the county's
# (or city's) public ArcGIS polygon layers. `limited_purpose` is optional — when
# absent the resolver returns full_purpose_city + a `limited_purpose_not_checked`
# advisory rather than collapsing limited-purpose into confident full-purpose
# (per reviewer contract answer #3).
# One entry per county file with a `jurisdiction:` section
# (app/regions/tx/counties/*.yaml).
_COUNTY_JURISDICTION_GIS: dict[str, dict[str, Any]] = regions.county_sections(
    "TX", "jurisdiction",
)

_STATEWIDE_CITY_LIMITS_GIS = {
    "source": "TxDOT City Boundaries (statewide city limits)",
    "source_url": (
        "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/"
        "services/TxDOT_City_Boundaries/FeatureServer"
    ),
    "url": (
        "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/"
        "services/TxDOT_City_Boundaries/FeatureServer/0/query"
    ),
    "name_field": "CITY_NM",
}
_TNRIS_CITY_LIMITS_GIS = {
    "source": "TNRIS StratMap City Boundaries (statewide city limits)",
    "source_url": (
        "https://services7.arcgis.com/hGTLI6IggobDDKZV/arcgis/rest/"
        "services/City_Boundaries/FeatureServer"
    ),
    "url": (
        "https://services7.arcgis.com/hGTLI6IggobDDKZV/arcgis/rest/"
        "services/City_Boundaries/FeatureServer/0/query"
    ),
    "name_field": "NAME",
}
_STATEWIDE_CITY_LIMIT_LAYERS = (
    _STATEWIDE_CITY_LIMITS_GIS,
    _TNRIS_CITY_LIMITS_GIS,
)


def _broker_summary(kind: str, *, city: str | None, source: str) -> str:
    city_label = (city or "the city").title()
    if kind == "full_purpose_city":
        return (
            f"PropDesk verified this point is inside {city_label} city limits "
            f"using {source}. City zoning and city development rules may apply."
        )
    if kind == "limited_purpose":
        return (
            f"PropDesk verified this point is in {city_label}'s limited-purpose "
            f"jurisdiction using {source}. Some city rules apply; confirm which."
        )
    if kind == "etj":
        return (
            f"PropDesk verified this point is in {city_label} ETJ, not city "
            "limits. City zoning usually does not apply, but subdivision/"
            "site-development review may still apply."
        )
    if kind == "unincorporated_county":
        return (
            "PropDesk found no city-limits or ETJ hit in the wired county "
            "layer. Treat this as unincorporated county unless later official "
            "evidence says otherwise."
        )
    if kind == "source_error":
        return (
            "PropDesk reached the county jurisdiction layer but it returned an "
            "error (it was temporarily unavailable or rejected the request), so "
            "no city-limits / ETJ verdict could be confirmed. This is NOT an "
            "'unincorporated' result — retry shortly, or verify city limits / "
            "ETJ on the official county or city map."
        )
    if source == _STATEWIDE_CITY_LIMITS_GIS["source"]:
        return (
            "PropDesk checked the statewide TxDOT city-boundary layer and did "
            "not find a city-limits hit, but this county's ETJ / full "
            "jurisdiction layers are not wired yet. Do not assume the parcel "
            "is unincorporated until an official county or city map confirms "
            "the ETJ/city-limits status."
        )
    return (
        "PropDesk could not verify jurisdiction from a wired public layer and "
        "did not guess. Do not assume county-only zoning or unincorporated "
        "status until an official city/county map or written confirmation "
        "supports it."
    )


def _clean_city_name(city: str | None) -> str | None:
    if city is None:
        return None
    cleaned = city.strip()
    if cleaned.lower().startswith("development agreement,"):
        cleaned = cleaned.split(",", 1)[1].strip()
    if cleaned.lower().startswith("city of "):
        cleaned = cleaned[len("city of "):].strip()
    if cleaned.lower().endswith(" etj"):
        cleaned = cleaned[: -len(" etj")].strip()
    return cleaned or None


def _name_indicates_etj(name: str | None) -> bool:
    """True when a matched jurisdiction name marks an ETJ rather than full-purpose city
    limits. Some county 'city' layers are MIXED city+ETJ layers (e.g. Travis taxmaps
    `CIT='City of Pflugerville ETJ'`, code 'PEJ'); a hit there is the ETJ, NOT city
    limits. Without this, an ETJ parcel is mis-read as in-city — flipping the access
    authority from the county to the city."""
    tokens = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return "etj" in tokens


def _statewide_city_limits_verdict(
    *,
    county: str | None,
    city: str,
    layer: dict[str, str],
    advisories: list[str],
) -> dict[str, Any]:
    kind = "full_purpose_city"
    city_name = _clean_city_name(city)
    return {
        "county": county,
        "source": layer["source"],
        "source_url": layer["source_url"],
        "jurisdiction_kind": kind,
        "city": city_name,
        "confidence": "authoritative",
        "advisories": advisories,
        "broker_summary": _broker_summary(
            kind,
            city=city_name,
            source=layer["source"],
        ),
    }


def _unknown_verdict(
    *,
    county: str | None,
    source: str | None,
    source_url: str | None,
    advisories: list[str],
) -> dict[str, Any]:
    kind = "unknown"
    return {
        "county": county,
        "source": source,
        "source_url": source_url,
        "jurisdiction_kind": kind,
        "city": None,
        "confidence": "unknown",
        "advisories": advisories,
        "broker_summary": _broker_summary(kind, city=None, source=source or ""),
    }


def _source_error_verdict(
    *,
    county: str | None,
    source: str | None,
    source_url: str | None,
    advisories: list[str],
) -> dict[str, Any]:
    """A wired authoritative layer errored and nothing else produced a hit. This
    is distinct from `unknown` (county not wired / clean no-hit) — it means the
    source itself failed, so the caller must surface a retry/manual path and must
    NOT fall through to a confident unincorporated verdict."""
    kind = "source_error"
    return {
        "county": county,
        "source": source,
        "source_url": source_url,
        "jurisdiction_kind": kind,
        "city": None,
        "confidence": "unknown",
        "advisories": advisories,
        "broker_summary": _broker_summary(kind, city=None, source=source or ""),
    }


class LayerResult(TypedDict):
    """Three-state outcome of one point-in-polygon layer query.

    The whole point of this type: a query that ERRORED (timeout, DNS, ArcGIS
    HTTP-200 `{"error": ...}` envelope, 499 token-gate, 5xx) must NOT be
    indistinguishable from a clean MISS (0 intersecting features). Collapsing
    error→None previously let a transient/auth failure on the city-limits layer
    be promoted to a confident "unincorporated_county" verdict. Per Admin's
    Travis-coverage directive: HIT / MISS / ERROR are distinct, and a verdict of
    unincorporated/unknown is only legitimate when every authoritative layer
    returned a clean MISS."""

    status: Literal["hit", "miss", "error"]
    name: str | None


def _query_layer(
    session: requests.Session,
    url: str,
    name_field: str,
    lat: float,
    lng: float,
    *,
    where: str | None = None,
    fixed_name: str | None = None,
    ignore_values: list[str] | None = None,
) -> LayerResult:
    """Query one polygon layer at the point. Returns a three-state LayerResult.

    - hit:   the point intersects a feature (name is the matched value, or "" when
             the feature matched but the name field was empty — still a real hit).
    - miss:  the layer responded cleanly with zero intersecting features (or the
             matched name is in `ignore_values`, e.g. "unincorporated").
    - error: the request failed OR ArcGIS returned an error envelope. NEVER
             treated as a clean miss by callers."""
    try:
        params = {
                "geometry": f"{lng},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                # Some Williamson layers qualify the field (e.g.
                # GIS.city_limits.CITY_NAME), so request all fields and match by
                # suffix rather than assuming the unqualified name.
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
        }
        if where:
            params["where"] = where
        resp = session.get(
            url,
            params=params,
            timeout=_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-jurisdiction"},
        )
        resp.raise_for_status()
        body = resp.json()
        # ArcGIS frequently returns HTTP 200 with an {"error": {...}} envelope
        # (token required = 499, invalid query = 400, etc.). That is an ERROR,
        # not a clean miss — treat it as such so it can't become a false verdict.
        if isinstance(body, dict) and body.get("error"):
            log.debug(
                "jurisdiction layer error envelope (%s): %s", url, body.get("error"),
            )
            return {"status": "error", "name": None}
        features = (body.get("features") if isinstance(body, dict) else None) or []
        if not features:
            return {"status": "miss", "name": None}
        if fixed_name:
            return {"status": "hit", "name": fixed_name}
        attrs = features[0].get("attributes") or {}
        name = attrs.get(name_field)
        if name is None:
            for key, value in attrs.items():
                if key.split(".")[-1] == name_field:
                    name = value
                    break
        if ignore_values and str(name or "").strip().lower() in {
            value.lower() for value in ignore_values
        }:
            # Sentinel value (e.g. "unincorporated") — a clean miss, not a city.
            return {"status": "miss", "name": None}
        # A feature matched. Return the name when present, else "" — still a hit.
        return {"status": "hit", "name": str(name).strip() if name else ""}
    except Exception as exc:
        log.debug("jurisdiction layer query failed (%s): %s", url, exc)
        return {"status": "error", "name": None}


def _point_in_layer(
    session: requests.Session,
    url: str,
    name_field: str,
    lat: float,
    lng: float,
    *,
    where: str | None = None,
    fixed_name: str | None = None,
    ignore_values: list[str] | None = None,
) -> str | None:
    """Backward-compatible thin wrapper over `_query_layer`. Returns the matched
    name on a hit, else None (error and miss both collapse to None here). New
    code should call `_query_layer` to keep the error/miss distinction."""
    result = _query_layer(
        session, url, name_field, lat, lng,
        where=where, fixed_name=fixed_name, ignore_values=ignore_values,
    )
    return result["name"] if result["status"] == "hit" else None


def _layer_entries(cfg: dict[str, Any], key: str) -> list[dict[str, Any]]:
    entry = cfg.get(key)
    if entry is None:
        return []
    if isinstance(entry, list):
        return entry
    return [entry]


def _layers_verdict(
    session: requests.Session,
    layers: list[dict[str, Any]],
    lat: float,
    lng: float,
) -> LayerResult:
    """Aggregate a group of layers (e.g. all city-limits layers) into one
    LayerResult. First hit wins. If nothing hits but at least one layer errored,
    the group is ERROR (so the caller can't mistake it for a clean miss). Only
    when every layer returned a clean miss is the group a MISS."""
    saw_error = False
    for layer in layers:
        result = _query_layer(
            session,
            layer["url"],
            layer["name_field"],
            lat,
            lng,
            where=layer.get("where"),
            fixed_name=layer.get("fixed_name"),
            ignore_values=layer.get("ignore_values"),
        )
        if result["status"] == "hit":
            return result
        if result["status"] == "error":
            saw_error = True
    return {"status": "error" if saw_error else "miss", "name": None}


def _statewide_city_limits_verdict_result(
    session: requests.Session,
    lat: float,
    lng: float,
) -> tuple[LayerResult, dict[str, str] | None]:
    saw_error = False
    for layer in _STATEWIDE_CITY_LIMIT_LAYERS:
        result = _query_layer(
            session,
            layer["url"],
            layer["name_field"],
            lat,
            lng,
        )
        if result["status"] == "hit":
            return result, layer
        if result["status"] == "error":
            saw_error = True
    return {"status": "error" if saw_error else "miss", "name": None}, None


def resolve_jurisdiction(
    identity: PropertyIdentity, *, session: requests.Session | None = None,
) -> dict[str, Any]:
    """Point-in-polygon jurisdiction verdict per docs/specs/jurisdiction_resolver.md.

    Precedence: city-limits → ETJ → unincorporated → unknown. City-limits wins
    over ETJ (a parcel inside city limits is full-purpose, even if the ETJ
    polygon geometrically includes it)."""
    county = (identity.county or "").lower().strip()
    cfg = _COUNTY_JURISDICTION_GIS.get(county)
    base = {
        "county": county or None,
        "source": cfg["source"] if cfg else None,
        "source_url": cfg["source_url"] if cfg else None,
    }
    if identity.lat is None or identity.lng is None:
        return _unknown_verdict(
            county=base["county"],
            source=base["source"],
            source_url=base["source_url"],
            advisories=["no_coordinates"],
        )

    sess = http_session(session)
    lat, lng = identity.lat, identity.lng

    if cfg is None:
        statewide, statewide_layer = _statewide_city_limits_verdict_result(
            sess, lat, lng,
        )
        if statewide["status"] == "hit":
            return _statewide_city_limits_verdict(
                county=county or None,
                city=statewide["name"] or "",
                layer=statewide_layer or _STATEWIDE_CITY_LIMITS_GIS,
                advisories=[
                    "statewide_city_limits_only",
                    "etj_not_checked",
                ],
            )
        if statewide["status"] == "error":
            # The only wired layer for an unwired county errored — do NOT claim
            # "not found"; surface a retry/manual path (error != clean miss).
            return _source_error_verdict(
                county=county or None,
                source=_STATEWIDE_CITY_LIMITS_GIS["source"],
                source_url=_STATEWIDE_CITY_LIMITS_GIS["source_url"],
                advisories=[
                    "county_not_wired",
                    "statewide_city_limits_source_error",
                    "etj_not_wired",
                ],
            )
        return _unknown_verdict(
            county=county or None,
            source=_STATEWIDE_CITY_LIMITS_GIS["source"],
            source_url=_STATEWIDE_CITY_LIMITS_GIS["source_url"],
            advisories=[
                "county_not_wired",
                "statewide_city_limits_no_hit",
                "etj_not_wired",
            ],
        )

    # ── Wired county. Track whether any authoritative layer ERRORED so a clean
    # "unincorporated" verdict is only emitted when every layer cleanly MISSED.
    layer_error = False

    city_result = _layers_verdict(
        sess, _layer_entries(cfg, "city_limits"), lat, lng,
    )
    if city_result["status"] == "hit" and not _name_indicates_etj(city_result["name"]):
        kind = "full_purpose_city"
        city_name = _clean_city_name(city_result["name"])
        advisories: list[str] = []
        if "limited_purpose" not in cfg:
            advisories.append("limited_purpose_not_checked")
        return {
            **base, "jurisdiction_kind": kind, "city": city_name,
            "confidence": "authoritative", "advisories": advisories,
            "broker_summary": _broker_summary(kind, city=city_name, source=cfg["source"]),
        }
    # A "city" layer that is actually a MIXED city+ETJ layer can hit on an ETJ polygon
    # (name carries an 'ETJ' marker). That is the ETJ, not full-purpose city limits —
    # classify it as ETJ so access authority correctly resolves to the county.
    if city_result["status"] == "hit" and _name_indicates_etj(city_result["name"]):
        etj_name = _clean_city_name(city_result["name"])
        return {
            **base, "jurisdiction_kind": "etj", "city": etj_name,
            "confidence": "authoritative", "advisories": [],
            "broker_summary": _broker_summary("etj", city=etj_name, source=cfg["source"]),
        }
    if city_result["status"] == "error":
        layer_error = True

    etj_layers = _layer_entries(cfg, "etj")
    etj_result = _layers_verdict(sess, etj_layers, lat, lng)
    if etj_result["status"] == "hit":
        kind = "etj"
        etj_name = _clean_city_name(etj_result["name"])
        return {
            **base, "jurisdiction_kind": kind, "city": etj_name,
            "confidence": "authoritative", "advisories": [],
            "broker_summary": _broker_summary(kind, city=etj_name, source=cfg["source"]),
        }
    if etj_result["status"] == "error":
        layer_error = True

    statewide, statewide_layer = _statewide_city_limits_verdict_result(
        sess, lat, lng,
    )
    if statewide["status"] == "hit":
        return _statewide_city_limits_verdict(
            county=county or None,
            city=statewide["name"] or "",
            layer=statewide_layer or _STATEWIDE_CITY_LIMITS_GIS,
            advisories=[
                *(cfg.get("no_hit_advisories") or ["county_jurisdiction_no_hit"]),
                "statewide_city_limits_crosscheck",
            ],
        )
    if statewide["status"] == "error":
        layer_error = True

    # CRITICAL: if any authoritative layer errored and nothing hit, we CANNOT
    # tell city-limits/ETJ/unincorporated apart. Return source_error, never a
    # confident unincorporated/unknown verdict (Travis-coverage directive).
    if layer_error:
        return _source_error_verdict(
            county=base["county"],
            source=base["source"],
            source_url=base["source_url"],
            advisories=[
                *(cfg.get("no_hit_advisories") or ["county_jurisdiction_no_hit"]),
                "jurisdiction_layer_source_error",
            ],
        )

    # Every authoritative layer returned a CLEAN miss from here on.
    if not cfg.get("unincorporated_on_no_hit", True):
        advisories = [
            *(cfg.get("no_hit_advisories") or ["county_jurisdiction_no_hit"]),
            "statewide_city_limits_no_hit",
        ]
        if not etj_layers and "etj_not_wired" not in advisories:
            advisories.append("etj_not_wired")
        return _unknown_verdict(
            county=base["county"],
            source=base["source"],
            source_url=base["source_url"],
            advisories=advisories,
        )

    kind = "unincorporated_county"
    return {
        **base, "jurisdiction_kind": kind, "city": None,
        "confidence": "authoritative", "advisories": [],
        "broker_summary": _broker_summary(kind, city=None, source=cfg["source"]),
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    """Registry-compatible entry. The orchestrator runs this in Wave 0.5 (after
    geocode, before the city-aware adapters) and writes the verdict to
    `derived.jurisdiction` + `adapter_results['jurisdiction_resolver']`."""
    verdict = resolve_jurisdiction(identity, session=session)
    kind = verdict["jurisdiction_kind"]
    resolved = kind not in ("unknown", "source_error")
    # source_error is a fragile-source signal, not a settled "not covered":
    # surface PROGRAMMATIC_PUBLIC_BUT_FRAGILE so the UI/downstream can offer a
    # retry instead of reading it as a clean unknown.
    access = (
        SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
        if kind == "source_error"
        else SourceAccessStatus.PROGRAMMATIC_PUBLIC
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=access,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT if resolved
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.COVERED if resolved
            else CoverageStatus.MANUAL_VERIFICATION_REQUIRED
        ),
        normalized_facts=verdict,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[verdict["broker_summary"]],
    )


__all__ = ["SOURCE_ID", "fetch", "resolve_jurisdiction"]
