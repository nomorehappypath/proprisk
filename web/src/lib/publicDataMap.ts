// Public Data property map — typed client + pure helpers.
//
// Ported from the reference app `publicIntelMap.ts`. The payload contract is the
// server's read-only projection of ONE stored lookup: the server decides every
// layer, every legend swatch, every distance and every advisory. Nothing here
// composes a fact — this layer only types the payload and turns it into the
// GeoJSON maplibre needs.
import type { Map as MapLibreMap } from 'maplibre-gl'

import { api } from './api'

// ── Payload contract (mirrors app/services/site_check/map_contract.py) ───

export interface MapCoordinate {
  lat: number
  lng: number
}

export interface MapBounds {
  west: number
  south: number
  east: number
  north: number
}

export interface MapBasemap {
  id: string
  label: string
  kind: string
  tile_url: string
  tile_size: number
  min_zoom: number
  max_zoom: number
  attribution: string
  provider_label: string
  resolution_label: string
  detail_advisory: string
  road_vector_url: string | null
  road_glyphs_url: string | null
}

export interface MapLegendEntry {
  label: string
  color?: string
}

export interface MapDetailEntry {
  label: string
  value: string
  url?: string
}

export interface MapLayer {
  id: string
  label: string
  kind: string // 'raster' | 'geojson'
  available: boolean
  status: string
  tile_url: string | null
  tile_size: number
  opacity: number
  geojson: Record<string, unknown> | null
  attribution: string
  advisory: string | null
  legend: MapLegendEntry[]
  details: MapDetailEntry[]
}

export interface MapCategory {
  id: string
  label: string
  group: string
  icon: string
  count: number
  default_selected: boolean
}

export interface MapPlace {
  id: string
  group: string
  category: string
  name: string
  lat: number
  lng: number
  address: string | null
  brand: string | null
  source: string
  confidence: string
  distance_m: number
  distance_basis: string // 'parcel_boundary' | 'address_pin'
  nearest_subject: MapCoordinate
}

export interface MapFeature {
  id: string
  kind: string
  title: string
  subtitle: string | null
  source: string
  confidence: string
  geometry: Record<string, unknown>
  details: MapDetailEntry[]
  map_properties: Record<string, unknown>
}

export interface MapContext {
  run_id: string
  fetched_at: string | null
  address: string
  center: MapCoordinate
  bounds: MapBounds
  subject_anchor: string // 'parcel' | 'address_pin'
  parcel_geojson: Record<string, unknown> | null
  basemaps: MapBasemap[]
  layers: MapLayer[]
  categories: MapCategory[]
  places: MapPlace[]
  context_features: MapFeature[]
  advisories: string[]
}

export const getRunMap = (runId: string) => api.get<MapContext>(`/site-check/runs/${runId}/map`)

// ── Layer catalogue ─────────────────────────────────────────────────────

// Server layer ids that this client actually draws from `layer.geojson`.
// A raster layer always draws (it becomes `overlay-<id>`), so it is not listed.
const RENDERED_GEOJSON_LAYER_IDS = new Set([
  'parcel', 'drainage', 'neighbors', 'projects', 'utilities', 'sewer_ccn',
  'water_districts', 'water_ccn', 'traffic', 'access', 'population_density',
  'regulatory', 'environmental', 'buildable_envelope', 'zoning',
])

export function hasDrawableLayer(layer: MapLayer): boolean {
  if (!layer.available) return false
  if (layer.kind === 'raster') return Boolean(layer.tile_url)
  if (layer.kind === 'geojson') return Boolean(layer.geojson)
  return false
}

export function hasMapRenderer(layer: MapLayer): boolean {
  return layer.kind === 'raster' || RENDERED_GEOJSON_LAYER_IDS.has(layer.id)
}

// One server layer id → every maplibre layer id that draws it. Toggling the
// switch flips them together.
const LAYER_RENDERERS: Record<string, string[]> = {
  neighbors: ['neighbor-parcels-fill', 'neighbor-parcels-line'],
  projects: ['public-project-points', 'public-project-label'],
  utilities: [
    'utility-territories-fill', 'utility-territories-line', 'utility-substations',
    'utility-area-labels', 'utility-line-labels',
  ],
  sewer_ccn: ['sewer-ccn-fill', 'sewer-ccn-line', 'sewer-ccn-label'],
  water_ccn: ['water-ccn-fill', 'water-ccn-line', 'water-ccn-label'],
  water_districts: ['water-districts-fill', 'water-districts-line', 'water-districts-label'],
  traffic: [
    'traffic-count-casing', 'traffic-count-lines', 'traffic-station-points',
    'traffic-line-labels', 'traffic-station-labels',
  ],
  access: [
    'access-roadway-casing', 'access-roadway-lines', 'access-frontage-casing',
    'access-frontage-lines', 'access-driveway-casing', 'access-driveway-lines',
    'access-frontage-labels', 'access-points',
  ],
  drainage: [
    'drainage-areas-fill', 'drainage-areas-line', 'drainage-flow-lines',
    'drainage-runon-points',
  ],
  environmental: [
    'environmental-constraints-fill', 'environmental-constraints-line',
    'environmental-constraints-label',
  ],
  population_density: [
    'population-density-fill', 'population-density-line', 'population-density-label',
  ],
  regulatory: [
    'regulatory-districts-fill', 'regulatory-districts-line', 'regulatory-districts-label',
  ],
  zoning: ['subject-zoning-label'],
  buildable_envelope: ['buildable-envelope-fill', 'buildable-envelope-line'],
}

const TOGGLEABLE_LAYER_IDS = [
  'parcel', 'neighbors', 'projects', 'utilities', 'sewer_ccn', 'water_ccn',
  'water_districts', 'traffic', 'flood', 'drainage', 'wetlands', 'zoning',
  'elevation', 'access', 'regulatory', 'environmental', 'population_density',
  'easements', 'buildable_envelope',
]

export function applyMapLayerVisibility(map: MapLibreMap, activeLayers: Set<string>): void {
  for (const layerId of TOGGLEABLE_LAYER_IDS) {
    const visible = activeLayers.has(layerId) ? 'visible' : 'none'
    const renderers = [
      `overlay-${layerId}`,
      `subject-${layerId}-fill`,
      `subject-${layerId}-line`,
      ...(LAYER_RENDERERS[layerId] ?? []),
    ]
    for (const mapLayerId of renderers) {
      if (map.getLayer(mapLayerId)) {
        map.setLayoutProperty(mapLayerId, 'visibility', visible)
      }
    }
  }
}

export function toggleMapLayerVisibility(
  map: MapLibreMap | null,
  current: Set<string>,
  layerId: string,
): Set<string> {
  const next = new Set(current)
  if (next.has(layerId)) next.delete(layerId)
  else next.add(layerId)
  if (map) applyMapLayerVisibility(map, next)
  return next
}

// ── Camera ──────────────────────────────────────────────────────────────

export const SUBJECT_CONTEXT_FEET = 1_000

/**
 * Camera target for a parcel-first map. The payload's bounds can span miles of
 * screening data — useful for overlays, far too wide to open on. This derives
 * the parcel/address extent and adds a fixed ring of context.
 */
export function subjectContextBounds(
  data: MapContext,
  contextFeet = SUBJECT_CONTEXT_FEET,
): [[number, number], [number, number]] {
  const points: Array<[number, number]> = []

  const visitCoordinates = (value: unknown): void => {
    if (!Array.isArray(value)) return
    if (
      value.length >= 2
      && typeof value[0] === 'number'
      && typeof value[1] === 'number'
      && Number.isFinite(value[0])
      && Number.isFinite(value[1])
    ) {
      points.push([value[0], value[1]])
      return
    }
    value.forEach(visitCoordinates)
  }
  const visitGeojson = (value: unknown): void => {
    if (!value || typeof value !== 'object') return
    const item = value as Record<string, unknown>
    if (item.coordinates) visitCoordinates(item.coordinates)
    if (Array.isArray(item.features)) item.features.forEach(visitGeojson)
    if (item.geometry) visitGeojson(item.geometry)
    if (Array.isArray(item.geometries)) item.geometries.forEach(visitGeojson)
  }

  visitGeojson(data.parcel_geojson)
  if (points.length === 0) {
    visitGeojson(data.layers.find((layer) => layer.id === 'parcel')?.geojson)
  }
  if (points.length === 0) {
    points.push([data.center.lng, data.center.lat])
  }

  const lngs = points.map(([lng]) => lng)
  const lats = points.map(([, lat]) => lat)
  const west = Math.min(...lngs)
  const south = Math.min(...lats)
  const east = Math.max(...lngs)
  const north = Math.max(...lats)
  const centerLat = (south + north) / 2

  // Local viewport conversion — accurate far beyond what a 1,000-foot context
  // ring needs, and stable across the Texas service area.
  const feetPerDegreeLatitude = 69 * 5_280
  const feetPerDegreeLongitude = Math.max(
    feetPerDegreeLatitude * Math.abs(Math.cos((centerLat * Math.PI) / 180)),
    1,
  )
  const safeContextFeet = Number.isFinite(contextFeet)
    ? Math.max(0, contextFeet)
    : SUBJECT_CONTEXT_FEET
  const latitudePadding = safeContextFeet / feetPerDegreeLatitude
  const longitudePadding = safeContextFeet / feetPerDegreeLongitude

  return [
    [west - longitudePadding, south - latitudePadding],
    [east + longitudePadding, north + latitudePadding],
  ]
}

export function isImageryDetailLimitReached(
  basemap: MapBasemap | undefined,
  zoom: number,
): boolean {
  return !!basemap && basemap.kind !== 'terrain' && zoom > basemap.max_zoom
}

// ── Formatting ──────────────────────────────────────────────────────────

export function formatMapDistance(distanceM: number): string {
  if (!Number.isFinite(distanceM) || distanceM < 0) return 'Distance unavailable'
  if (distanceM < 1) return 'On the property'
  const miles = distanceM / 1609.344
  if (miles < 0.25) return `${Math.round(distanceM * 3.28084).toLocaleString()} ft`
  return `${miles.toFixed(miles < 10 ? 2 : 1)} mi`
}

export function distanceBasisLabel(basis: string): string {
  return basis === 'parcel_boundary' ? 'from property boundary' : 'from address pin'
}

export function confidenceLabel(confidence: string): string {
  return confidence === 'verified_public_fact' ? 'Verified public fact' : 'Screening indicator'
}

// ── GeoJSON builders ────────────────────────────────────────────────────

export function mapPlacesFeatureCollection(places: MapPlace[], selected: Set<string>) {
  return {
    type: 'FeatureCollection' as const,
    features: places
      .filter((place) => selected.has(place.category))
      .map((place) => ({
        type: 'Feature' as const,
        id: place.id,
        geometry: { type: 'Point' as const, coordinates: [place.lng, place.lat] },
        properties: { id: place.id, category: place.category, name: place.name },
      })),
  }
}

export function mapMeasurementFeatureCollection(place: MapPlace | null) {
  if (!place) return { type: 'FeatureCollection' as const, features: [] }
  const start = [place.lng, place.lat]
  const end = [place.nearest_subject.lng, place.nearest_subject.lat]
  return {
    type: 'FeatureCollection' as const,
    features: [
      {
        type: 'Feature' as const,
        geometry: { type: 'LineString' as const, coordinates: [start, end] },
        properties: { kind: 'line' },
      },
      {
        type: 'Feature' as const,
        geometry: {
          type: 'Point' as const,
          coordinates: [
            Number(((start[0] + end[0]) / 2).toFixed(8)),
            Number(((start[1] + end[1]) / 2).toFixed(8)),
          ],
        },
        properties: { kind: 'label', label: formatMapDistance(place.distance_m) },
      },
    ],
  }
}

export function mapContextFeatureCollection(features: MapFeature[], kind: string) {
  return {
    type: 'FeatureCollection' as const,
    features: features
      .filter((feature) => feature.kind === kind)
      .map((feature) => ({
        type: 'Feature' as const,
        id: feature.id,
        geometry: feature.geometry,
        properties: { id: feature.id },
      })),
  }
}
