"""Pydantic DTOs for the Public Data map projection
(GET /api/site-check/runs/{run_id}/map).

Ported from the reference app `app/schemas/public_intel.py` map shapes. Field names
and types are held IDENTICAL to the source contract so the map payload
stays interchangeable; only the class prefix and the dropped `project_id`
(PropDesk has no project entity — runs are tenant-scoped) differ.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SiteCheckMapCoordinate(BaseModel):
    lat: float
    lng: float


class SiteCheckMapBounds(BaseModel):
    west: float
    south: float
    east: float
    north: float


class SiteCheckMapBasemap(BaseModel):
    id: str
    label: str
    kind: str = "aerial"
    tile_url: str
    tile_size: int = 256
    min_zoom: int = 0
    max_zoom: int = 16
    attribution: str
    provider_label: str
    resolution_label: str
    detail_advisory: str
    road_vector_url: str | None = None
    road_glyphs_url: str | None = None


class SiteCheckMapLayer(BaseModel):
    id: str
    label: str
    kind: str
    available: bool
    status: str
    tile_url: str | None = None
    tile_size: int = 256
    opacity: float = 0.62
    geojson: dict[str, Any] | None = None
    attribution: str
    advisory: str | None = None
    legend: list[dict[str, str]] = Field(default_factory=list)
    details: list[dict[str, str]] = Field(default_factory=list)


class SiteCheckMapCategory(BaseModel):
    id: str
    label: str
    group: str
    icon: str
    count: int
    default_selected: bool = False


class SiteCheckMapPlace(BaseModel):
    id: str
    group: str
    category: str
    name: str
    lat: float
    lng: float
    address: str | None = None
    brand: str | None = None
    source: str
    confidence: str
    distance_m: float
    distance_basis: str
    nearest_subject: SiteCheckMapCoordinate


class SiteCheckMapFeature(BaseModel):
    id: str
    kind: str
    title: str
    subtitle: str | None = None
    source: str
    confidence: str
    geometry: dict[str, Any]
    details: list[dict[str, str]] = Field(default_factory=list)
    map_properties: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )


class SiteCheckMapContext(BaseModel):
    run_id: str
    fetched_at: datetime | None = None
    address: str
    center: SiteCheckMapCoordinate
    bounds: SiteCheckMapBounds
    subject_anchor: str
    parcel_geojson: dict[str, Any] | None = None
    basemaps: list[SiteCheckMapBasemap]
    layers: list[SiteCheckMapLayer]
    categories: list[SiteCheckMapCategory]
    places: list[SiteCheckMapPlace]
    context_features: list[SiteCheckMapFeature] = Field(default_factory=list)
    advisories: list[str] = Field(default_factory=list)
