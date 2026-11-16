// Map style construction for the Public Data property map.
//
// Ported from the reference app `publicIntelMapStyle.ts`. The colour tables, filters
// and paint expressions are held IDENTICAL to the source so a parcel, a CCN
// boundary or an AADT band reads the same in both products. Only the module
// name and the type imports differ.
import type {
  ExpressionSpecification,
  FilterSpecification,
  StyleSpecification,
} from 'maplibre-gl'

import type { MapBasemap, MapContext } from './publicDataMap'

// ── Nearby-place colours ────────────────────────────────────────────────

export const POI_CATEGORY_COLORS: Record<string, string> = {
  gas_station: '#DC2626',
  ev_charging: '#7C3AED',
  car_wash: '#0284C7',
  auto_dealer: '#475569',
  auto_parts: '#9F1239',
  restaurant: '#C2410C',
  fast_food: '#EA580C',
  cafe: '#92400E',
  bar: '#BE123C',
  hotel: '#6D28D9',
  hospital: '#B91C1C',
  clinic: '#E11D48',
  pharmacy: '#15803D',
  dentist: '#0F766E',
  veterinary: '#047857',
  supermarket: '#166534',
  convenience: '#4D7C0F',
  shopping_center: '#A21CAF',
  department: '#86198F',
  hardware: '#A16207',
  bank: '#4338CA',
  atm: '#4F46E5',
  school: '#1D4ED8',
  university: '#0369A1',
  daycare: '#0E7490',
  bus_stop: '#0E7490',
  train_station: '#075985',
  parking: '#334155',
  apartments: '#DB2777',
  park: '#16803C',
  gym: '#047857',
  sports_facility: '#0F766E',
  cinema: '#9333EA',
  theater: '#A21CAF',
  event_venue: '#C026D3',
  truck_stop: '#B45309',
  self_storage: '#64748B',
  other: '#0F766E',
}

export function poiCategoryColor(category: string): string {
  return POI_CATEGORY_COLORS[category] ?? POI_CATEGORY_COLORS.other
}

export const POI_CATEGORY_COLOR_EXPRESSION = [
  'match',
  ['get', 'category'],
  ...Object.entries(POI_CATEGORY_COLORS).flatMap(([category, color]) => [category, color]),
  POI_CATEGORY_COLORS.other,
] as unknown as ExpressionSpecification

// ── Traffic (AADT) bands ────────────────────────────────────────────────

export const TRAFFIC_AADT_COLOR_EXPRESSION = [
  'case',
  ['has', 'aadt'],
  [
    'step',
    ['to-number', ['get', 'aadt']],
    '#16A34A',
    5000, '#CA8A04',
    15000, '#EA580C',
    30000, '#DC2626',
    60000, '#7F1D1D',
  ],
  '#64748B',
] as unknown as ExpressionSpecification

export const TRAFFIC_AADT_WIDTH_EXPRESSION = [
  'interpolate',
  ['linear'],
  ['to-number', ['get', 'aadt'], 0],
  0, 2.75,
  5000, 3.25,
  15000, 4,
  30000, 5,
  60000, 6,
] as unknown as ExpressionSpecification

export const TRAFFIC_AADT_CASING_WIDTH_EXPRESSION = [
  'interpolate',
  ['linear'],
  ['to-number', ['get', 'aadt'], 0],
  0, 5.75,
  5000, 6.25,
  15000, 7,
  30000, 8,
  60000, 9,
] as unknown as ExpressionSpecification

// ── Road reference overlay (vector roads over the public aerial) ────────

export const ROAD_REFERENCE_LAYER_IDS = [
  'road-reference-major-casing',
  'road-reference-major',
  'road-reference-minor-casing',
  'road-reference-minor',
  'road-reference-label',
] as const

const ROAD_REFERENCE_SOURCE_ID = 'road-reference'

const MAJOR_ROAD_FILTER = [
  'match', ['get', 'class'],
  ['motorway', 'trunk', 'primary', 'secondary', 'tertiary'],
  true, false,
] as unknown as FilterSpecification

const MINOR_ROAD_FILTER = [
  'match', ['get', 'class'], ['minor', 'street', 'street_limited'],
  true, false,
] as unknown as FilterSpecification

export function roadReferenceVisibility(basemap: MapBasemap | undefined): 'visible' | 'none' {
  return basemap?.road_vector_url ? 'visible' : 'none'
}

/**
 * Build the initial style: the selected raster basemap plus, when the payload
 * offers one, the public vector road overlay so street names stay sharp over
 * enlarged aerial pixels. Unlike the reference app (which always seeds `basemaps[0]`)
 * this takes the chosen basemap so the first paint already matches the
 * selected background — the phone has no room for a wrong first frame.
 */
export function buildInitialMapStyle(data: MapContext, basemapId: string): StyleSpecification {
  const base = data.basemaps.find((item) => item.id === basemapId) ?? data.basemaps[0]
  const roadReference = data.basemaps.find((item) => item.road_vector_url && item.road_glyphs_url)
  const roadVisibility = roadReferenceVisibility(base)

  const sources: StyleSpecification['sources'] = {
    basemap: {
      type: 'raster',
      tiles: [base.tile_url],
      tileSize: base.tile_size,
      minzoom: base.min_zoom,
      maxzoom: base.max_zoom,
      attribution: base.attribution,
    },
  }
  if (roadReference?.road_vector_url) {
    sources[ROAD_REFERENCE_SOURCE_ID] = {
      type: 'vector',
      url: roadReference.road_vector_url,
      attribution: roadReference.attribution,
    }
  }

  const layers: StyleSpecification['layers'] = [
    { id: 'basemap', type: 'raster', source: 'basemap' },
  ]
  if (roadReference) {
    layers.push(
      {
        id: 'road-reference-major-casing',
        type: 'line',
        source: ROAD_REFERENCE_SOURCE_ID,
        'source-layer': 'transportation',
        minzoom: 8,
        filter: MAJOR_ROAD_FILTER,
        layout: { visibility: roadVisibility, 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#111827',
          'line-opacity': 0.78,
          'line-width': ['interpolate', ['linear'], ['zoom'], 8, 1.5, 14, 5, 18, 12],
        },
      },
      {
        id: 'road-reference-major',
        type: 'line',
        source: ROAD_REFERENCE_SOURCE_ID,
        'source-layer': 'transportation',
        minzoom: 8,
        filter: MAJOR_ROAD_FILTER,
        layout: { visibility: roadVisibility, 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#F8FAFC',
          'line-opacity': 0.94,
          'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.7, 14, 2.4, 18, 6],
        },
      },
      {
        id: 'road-reference-minor-casing',
        type: 'line',
        source: ROAD_REFERENCE_SOURCE_ID,
        'source-layer': 'transportation',
        minzoom: 13.5,
        filter: MINOR_ROAD_FILTER,
        layout: { visibility: roadVisibility, 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#111827',
          'line-opacity': 0.65,
          'line-width': ['interpolate', ['linear'], ['zoom'], 13.5, 1, 18, 8],
        },
      },
      {
        id: 'road-reference-minor',
        type: 'line',
        source: ROAD_REFERENCE_SOURCE_ID,
        'source-layer': 'transportation',
        minzoom: 13.5,
        filter: MINOR_ROAD_FILTER,
        layout: { visibility: roadVisibility, 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#FFFFFF',
          'line-opacity': 0.88,
          'line-width': ['interpolate', ['linear'], ['zoom'], 13.5, 0.5, 18, 4],
        },
      },
      {
        id: 'road-reference-label',
        type: 'symbol',
        source: ROAD_REFERENCE_SOURCE_ID,
        'source-layer': 'transportation_name',
        minzoom: 11,
        filter: [
          'match', ['get', 'class'],
          [
            'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
            'minor', 'street', 'street_limited',
          ],
          true, false,
        ] as unknown as FilterSpecification,
        layout: {
          visibility: roadVisibility,
          'symbol-placement': 'line',
          'symbol-spacing': 280,
          'text-field': ['coalesce', ['get', 'name_en'], ['get', 'name']],
          'text-font': ['Noto Sans Regular'],
          'text-rotation-alignment': 'map',
          'text-size': ['interpolate', ['linear'], ['zoom'], 11, 11, 16, 14],
        },
        paint: {
          'text-color': '#FFFFFF',
          'text-halo-color': '#111827',
          'text-halo-blur': 0.5,
          'text-halo-width': 2.25,
        },
      },
    )
  }

  return {
    version: 8,
    glyphs: roadReference?.road_glyphs_url ?? undefined,
    sources,
    layers,
  }
}
