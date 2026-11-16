"""PropertyIdentity — typed input every public-data adapter takes.

Lifted verbatim from legacy_services/core/property_identity.py per
docs/specs/public_data.md §6.1. Single shared input object
captures everything an adapter might need to know about which
property the fetch is being run against.

All fields except `address` are optional. An adapter that only
needs lat/lng (FEMA, demographics) reads what it needs and
ignores the rest. An adapter that needs parcel ID (subdivision
overlays, official records) checks `parcel_id is not None`
first and emits a manual_upload request when it isn't.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class PropertyIdentity(BaseModel):
    """The single typed input object every public-data adapter
    accepts."""

    # ── Required input ──────────────────────────────────────
    address: str = Field(
        min_length=1, max_length=500,
        description=(
            "Free-text street address. Always provided by the "
            "user or extracted from the project corpus."
        ),
    )

    # ── User-supplied optional ──────────────────────────────
    lat: float | None = Field(
        default=None, ge=-90.0, le=90.0,
        description=(
            "Latitude. Resolver populates from geocoding when "
            "absent. Adapters that need geometry queries (FEMA, "
            "wetlands) require this and emit an error result "
            "when None."
        ),
    )
    lng: float | None = Field(
        default=None, ge=-180.0, le=180.0,
        description="Longitude. Same notes as lat.",
    )
    county: str | None = Field(
        default=None, max_length=64,
        description=(
            "Texas county name, lowercase ('travis', "
            "'williamson'). Drives jurisdictional dispatch — "
            "WCAD only fires for Williamson, TCAD-adjacent "
            "queries only fire for Travis, etc."
        ),
    )
    parcel_id: str | None = Field(
        default=None, max_length=64,
        description=(
            "Parcel identifier (e.g. 'R123456' from WCAD, "
            "TCAD account number). Anchors lookups for "
            "subdivision overlays, official records, and "
            "exact-parcel WCAD queries."
        ),
    )
    cad_account: str | None = Field(
        default=None, max_length=64,
        description=(
            "CAD account number. Distinct from parcel_id in "
            "some counties — TCAD uses a property_id AND a "
            "geo_id."
        ),
    )
    acreage: float | None = Field(
        default=None, gt=0,
        description=(
            "Parcel acreage. Drives FEMA envelope sizing; "
            "wetlands and environmental screening will use it "
            "the same way."
        ),
    )

    # ── Resolver-derived ────────────────────────────────────
    jurisdiction_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "City names whose bbox / jurisdiction layer covers "
            "this lat/lng. Empty list means 'no candidate city — "
            "treat as unincorporated' (statutorily certain in "
            "TX). Multiple entries means a boundary-straddle "
            "case."
        ),
    )
    parcel_geometry: dict[str, Any] | None = Field(
        default=None,
        description=(
            "GeoJSON-shaped polygon of the parcel boundary "
            "when known. Wetlands, FEMA, and environmental "
            "adapters prefer this over a centroid-radius "
            "envelope when available."
        ),
    )

    # ── Multi-parcel + boundary handling ────────────────────
    is_multi_parcel: bool = Field(
        default=False,
        description=(
            "True when one street address resolves to multiple "
            "parcel IDs. Adapters that need exact parcel "
            "boundaries should NOT silently pick one — either "
            "union the geometry OR emit a manual_upload request."
        ),
    )
    boundary_straddle_jurisdictions: list[str] = Field(
        default_factory=list,
        description=(
            "Jurisdictions whose boundaries the parcel crosses. "
            "Empty list means no straddle. Memo language must "
            "reflect that the answer depends on which side of "
            "the boundary the user is asking about."
        ),
    )

    # ── Audit trail ─────────────────────────────────────────
    identifier_resolution_log: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Append-only log of how each derived field was "
            "produced (e.g., 'lat from ArcGIS cached "
            "2026-04-30', 'county from coordinate-in-bbox "
            "Williamson')."
        ),
    )

    # ── Validators ──────────────────────────────────────────

    @field_validator("address")
    @classmethod
    def _strip_address(cls, v: str) -> str:
        return v.strip()

    @field_validator("county")
    @classmethod
    def _normalize_county(cls, v: str | None) -> str | None:
        # Lowercase + strip — dispatcher expects 'travis' /
        # 'williamson', not 'Travis County'.
        if v is None:
            return None
        cleaned = v.strip().lower()
        cleaned = cleaned.removesuffix(" county")
        return cleaned or None

    @field_validator("parcel_id", "cad_account")
    @classmethod
    def _strip_identifier(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip().upper()
        return cleaned or None

    # ── Query point ─────────────────────────────────────────

    def query_point(self) -> tuple[float | None, float | None]:
        """Point for "what is AT this property?" polygon lookups (zoning, FLU,
        overlay districts): the parcel interior point when the boundary is
        known, else the geocoded address point. Returns ``(lat, lng)``.

        The geocoded address usually sits on the road edge in front of the lot,
        which can fall in the wrong jurisdiction / land-use polygon; the parcel
        interior point lands inside the property. Distance/proximity lookups
        (schools, competitors, traffic) and the parcel finders must keep using
        ``lat``/``lng`` directly — this is only for point-in-polygon queries.
        """
        interior = _interior_point(self.parcel_geometry)
        if interior is not None:
            return interior
        return self.lat, self.lng


def _interior_point(
    polygon: dict[str, Any] | None,
) -> tuple[float, float] | None:
    """A point guaranteed inside the parcel polygon, as ``(lat, lng)``.

    Prefers shapely's ``representative_point`` (always interior — correct even
    for concave / L-shaped / multi-part parcels where a vertex average can land
    OUTSIDE the lot); falls back to the exterior-ring vertex mean; returns None
    when no usable polygon is present. Never raises."""
    if not isinstance(polygon, dict) or not polygon.get("coordinates"):
        return None
    try:
        from shapely.geometry import shape
        from shapely.validation import make_valid

        geom = shape(polygon)
        if not geom.is_valid:
            geom = make_valid(geom)
        if not geom.is_empty:
            point = geom.representative_point()
            return (float(point.y), float(point.x))  # (lat, lng)
    except Exception:  # noqa: S110 — ported: shapely path is optional, falls back to ring-average centroid below
        pass
    try:
        ring = polygon["coordinates"][0]
        pts = [p for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not pts:
            return None
        lng = sum(float(p[0]) for p in pts) / len(pts)
        lat = sum(float(p[1]) for p in pts) / len(pts)
        return (lat, lng)
    except Exception:
        return None


def from_legacy_inputs(
    *,
    address: str,
    lat: float | None = None,
    lng: float | None = None,
    county: str | None = None,
    parcel_id: str | None = None,
    acreage: float | None = None,
) -> PropertyIdentity:
    """Map the legacy_services legacy collector signature to a
    PropertyIdentity. Lets call sites that thought in the old
    shape pass a typed identity without restructuring.

    No active geocoding here — caller is expected to have
    already populated lat/lng (the orchestrator does that
    upstream via the arcgis_geocode adapter)."""
    return PropertyIdentity(
        address=address,
        lat=lat, lng=lng,
        county=county,
        parcel_id=parcel_id,
        acreage=acreage,
    )


__all__ = [
    "PropertyIdentity",
    "from_legacy_inputs",
]
