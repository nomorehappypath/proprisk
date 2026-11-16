// Every maplibre source + layer the Public Data property map draws.
//
// Ported from the reference app `PublicIntelMap.tsx`'s `addMapData`. Layer ids, filters,
// colours, widths and ADD ORDER (which is the z-order) are held identical so
// the two products render the same property the same way. Kept out of the
// React component so the page file stays about layout and interaction.
import type { FeatureCollection } from 'geojson'
import type {
  ExpressionSpecification,
  FilterSpecification,
  Map as MapLibreMap,
} from 'maplibre-gl'

import {
  mapContextFeatureCollection,
  mapMeasurementFeatureCollection,
  mapPlacesFeatureCollection,
  type MapContext,
} from './publicDataMap'
import {
  POI_CATEGORY_COLOR_EXPRESSION,
  ROAD_REFERENCE_LAYER_IDS,
  TRAFFIC_AADT_CASING_WIDTH_EXPRESSION,
  TRAFFIC_AADT_COLOR_EXPRESSION,
  TRAFFIC_AADT_WIDTH_EXPRESSION,
} from './publicDataMapStyle'

export const SPECIAL_DISTRICT_FILL_COLOR = '#475569'
export const SPECIAL_DISTRICT_FILL_OPACITY = 0.05
export const SPECIAL_DISTRICT_BOUNDARY_WIDTH = 3

// Every layer a tap can open a details card for. The card comes from
// `context_features`, so a tap only opens one when the drawn feature's id
// matches a context feature the server published.
export const CLICKABLE_LAYER_IDS = [
  'neighbor-parcels-fill', 'public-project-points', 'public-project-label',
  'utility-territories-fill', 'utility-territories-line', 'utility-substations',
  'utility-area-labels', 'utility-line-labels',
  'traffic-count-casing', 'traffic-count-lines', 'traffic-station-points',
  'traffic-line-labels', 'traffic-station-labels',
  'access-roadway-casing', 'access-roadway-lines',
  'access-frontage-casing', 'access-frontage-lines',
  'access-driveway-casing', 'access-driveway-lines',
  'access-frontage-labels', 'access-points',
  'environmental-constraints-fill', 'environmental-constraints-line',
  'environmental-constraints-label',
  'population-density-fill', 'population-density-line', 'population-density-label',
  'regulatory-districts-fill', 'regulatory-districts-line', 'regulatory-districts-label',
  'buildable-envelope-fill', 'buildable-envelope-line',
]

const asExpression = (value: unknown) => value as ExpressionSpecification
const asFilter = (value: unknown) => value as FilterSpecification
const asGeoJson = (value: unknown) => value as unknown as FeatureCollection

export function addMapData(
  map: MapLibreMap,
  data: MapContext,
  selectedCategories: Set<string>,
  activeLayers: Set<string>,
): void {
  const contextFeatures = data.context_features ?? []
  const vis = (id: string): 'visible' | 'none' => (activeLayers.has(id) ? 'visible' : 'none')

  // ── Public raster screens (flood, wetlands, elevation, …) ────────────
  for (const layer of data.layers) {
    if (!layer.available || layer.kind !== 'raster' || !layer.tile_url) continue
    map.addSource(`overlay-${layer.id}`, {
      type: 'raster',
      tiles: [layer.tile_url],
      tileSize: layer.tile_size,
      attribution: layer.attribution,
    })
    map.addLayer({
      id: `overlay-${layer.id}`,
      type: 'raster',
      source: `overlay-${layer.id}`,
      layout: { visibility: vis(layer.id) },
      paint: { 'raster-opacity': layer.opacity },
    })
  }

  // ── Zoning / jurisdiction fill ───────────────────────────────────────
  const zoning = data.layers.find((layer) => layer.id === 'zoning')
  if (zoning?.available && zoning.geojson) {
    map.addSource('subject-zoning', { type: 'geojson', data: asGeoJson(zoning.geojson) })
    map.addLayer({
      id: 'subject-zoning-fill',
      type: 'fill',
      source: 'subject-zoning',
      layout: { visibility: vis('zoning') },
      paint: {
        'fill-color': asExpression(['match', ['get', 'jurisdiction_kind'], 'etj', '#0891B2', '#7C3AED']),
        'fill-opacity': asExpression(['match', ['get', 'jurisdiction_kind'], 'etj', 0.08, 0.18]),
      },
    })
  }

  // ── Utilities ────────────────────────────────────────────────────────
  const utilities = data.layers.find((layer) => layer.id === 'utilities')
  if (utilities?.available && utilities.geojson) {
    const utilityColor = asExpression([
      'match', ['get', 'kind'],
      'utility_ccn_water', '#0284C7',
      'utility_ccn_wastewater', '#059669',
      'utility_electric_transmission', '#A21CAF',
      'utility_electric_substation', '#EA580C',
      'utility_main_water', '#0F766E',
      'utility_main_wastewater', '#0F766E',
      'utility_main_gas', '#92400E',
      'utility_lift_station', '#0F766E',
      'utility_facility_pumping_station', '#0F766E',
      'utility_facility_water_works', '#0284C7',
      'utility_facility_wastewater_plant', '#059669',
      'utility_gas_pipeline', '#92400E',
      '#64748B',
    ])
    map.addSource('utility-territories', { type: 'geojson', data: asGeoJson(utilities.geojson) })
    map.addLayer({
      id: 'utility-territories-fill',
      type: 'fill',
      source: 'utility-territories',
      filter: asFilter(['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]]),
      layout: { visibility: vis('utilities') },
      paint: { 'fill-color': utilityColor, 'fill-opacity': 0.16 },
    })
    map.addLayer({
      id: 'utility-territories-line',
      type: 'line',
      source: 'utility-territories',
      layout: { visibility: vis('utilities') },
      paint: {
        'line-color': utilityColor,
        'line-width': asExpression([
          'match', ['get', 'kind'],
          'utility_electric_transmission', 3.5,
          'utility_gas_pipeline', 3,
          2.5,
        ]),
        'line-opacity': 0.9,
      },
    })
    map.addLayer({
      id: 'utility-substations',
      type: 'circle',
      source: 'utility-territories',
      filter: asFilter(['==', ['geometry-type'], 'Point']),
      layout: { visibility: vis('utilities') },
      paint: {
        'circle-color': utilityColor,
        'circle-radius': 7,
        'circle-stroke-color': '#FFFFFF',
        'circle-stroke-width': 2,
      },
    })
    map.addLayer({
      id: 'utility-area-labels',
      type: 'symbol',
      source: 'utility-territories',
      filter: asFilter(['==', ['geometry-type'], 'Polygon']),
      layout: {
        visibility: vis('utilities'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: { 'text-color': '#0F172A', 'text-halo-color': '#FFFFFF', 'text-halo-width': 2 },
    })
    map.addLayer({
      id: 'utility-line-labels',
      type: 'symbol',
      source: 'utility-territories',
      filter: asFilter(['==', ['geometry-type'], 'LineString']),
      layout: {
        visibility: vis('utilities'),
        'symbol-placement': 'line',
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
        'text-keep-upright': true,
      },
      paint: {
        'text-color': utilityColor,
        'text-halo-color': '#FFFFFF',
        'text-halo-width': 2,
      },
    })
  }

  // ── Certificated service areas, each on its own switch ───────────────
  for (const ccn of [
    { id: 'sewer_ccn', source: 'sewer-ccn', color: '#059669' },
    { id: 'water_ccn', source: 'water-ccn', color: '#0284C7' },
    { id: 'water_districts', source: 'water-districts', color: '#7C3AED' },
  ] as const) {
    const layerData = data.layers.find((layer) => layer.id === ccn.id)
    if (!layerData?.available || !layerData.geojson) continue
    map.addSource(ccn.source, { type: 'geojson', data: asGeoJson(layerData.geojson) })
    const visibility = vis(ccn.id)
    map.addLayer({
      id: `${ccn.source}-fill`,
      type: 'fill',
      source: ccn.source,
      filter: asFilter(['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]]),
      layout: { visibility },
      paint: { 'fill-color': ccn.color, 'fill-opacity': 0.14 },
    })
    map.addLayer({
      id: `${ccn.source}-line`,
      type: 'line',
      source: ccn.source,
      layout: { visibility },
      paint: { 'line-color': ccn.color, 'line-width': 2.5, 'line-opacity': 0.9 },
    })
    map.addLayer({
      id: `${ccn.source}-label`,
      type: 'symbol',
      source: ccn.source,
      filter: asFilter(['==', ['geometry-type'], 'Polygon']),
      layout: {
        visibility,
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: { 'text-color': '#0F172A', 'text-halo-color': '#FFFFFF', 'text-halo-width': 2 },
    })
  }

  // ── Traffic counts ───────────────────────────────────────────────────
  const traffic = data.layers.find((layer) => layer.id === 'traffic')
  if (traffic?.available && traffic.geojson) {
    map.addSource('traffic-counts', { type: 'geojson', data: asGeoJson(traffic.geojson) })
    map.addLayer({
      id: 'traffic-count-casing',
      type: 'line',
      source: 'traffic-counts',
      filter: asFilter(['==', ['geometry-type'], 'LineString']),
      layout: { visibility: vis('traffic'), 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#FFFFFF',
        'line-width': TRAFFIC_AADT_CASING_WIDTH_EXPRESSION,
        'line-opacity': 0.88,
      },
    })
    map.addLayer({
      id: 'traffic-count-lines',
      type: 'line',
      source: 'traffic-counts',
      filter: asFilter(['==', ['geometry-type'], 'LineString']),
      layout: { visibility: vis('traffic'), 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': TRAFFIC_AADT_COLOR_EXPRESSION,
        'line-width': TRAFFIC_AADT_WIDTH_EXPRESSION,
      },
    })
    map.addLayer({
      id: 'traffic-station-points',
      type: 'circle',
      source: 'traffic-counts',
      filter: asFilter(['==', ['geometry-type'], 'Point']),
      layout: { visibility: vis('traffic') },
      paint: {
        'circle-color': TRAFFIC_AADT_COLOR_EXPRESSION,
        'circle-radius': 8,
        'circle-stroke-color': '#FFFFFF',
        'circle-stroke-width': 2.5,
      },
    })
    map.addLayer({
      id: 'traffic-line-labels',
      type: 'symbol',
      source: 'traffic-counts',
      filter: asFilter(['==', ['geometry-type'], 'LineString']),
      layout: {
        visibility: vis('traffic'),
        'symbol-placement': 'line',
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
      },
      paint: { 'text-color': '#0F172A', 'text-halo-color': '#FFFFFF', 'text-halo-width': 2.5 },
    })
    map.addLayer({
      id: 'traffic-station-labels',
      type: 'symbol',
      source: 'traffic-counts',
      filter: asFilter(['==', ['geometry-type'], 'Point']),
      layout: {
        visibility: vis('traffic'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
        'text-offset': [0, 1.35],
      },
      paint: { 'text-color': '#0F172A', 'text-halo-color': '#FFFFFF', 'text-halo-width': 2.5 },
    })
  }

  // ── Access: roadways, frontage, driveways, signals ───────────────────
  const access = data.layers.find((layer) => layer.id === 'access')
  if (access?.available && access.geojson) {
    const drivewayKinds = asFilter([
      'in', ['get', 'kind'],
      ['literal', [
        'access_subject_driveway', 'access_on_site_driveway', 'access_nearby_driveway',
      ]],
    ])
    map.addSource('access-roadways', { type: 'geojson', data: asGeoJson(access.geojson) })
    map.addLayer({
      id: 'access-roadway-casing',
      type: 'line',
      source: 'access-roadways',
      filter: asFilter(['==', ['get', 'kind'], 'access_roadway']),
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#FFFFFF', 'line-width': 7, 'line-opacity': 0.9 },
    })
    map.addLayer({
      id: 'access-roadway-lines',
      type: 'line',
      source: 'access-roadways',
      filter: asFilter(['==', ['get', 'kind'], 'access_roadway']),
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#2563EB', 'line-width': 4, 'line-opacity': 0.88 },
    })
    map.addLayer({
      id: 'access-frontage-casing',
      type: 'line',
      source: 'access-roadways',
      filter: asFilter(['==', ['get', 'kind'], 'access_subject_frontage']),
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#FFFFFF', 'line-width': 10, 'line-opacity': 0.95 },
    })
    map.addLayer({
      id: 'access-frontage-lines',
      type: 'line',
      source: 'access-roadways',
      filter: asFilter(['==', ['get', 'kind'], 'access_subject_frontage']),
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#059669', 'line-width': 6, 'line-opacity': 1 },
    })
    map.addLayer({
      id: 'access-driveway-casing',
      type: 'line',
      source: 'access-roadways',
      filter: drivewayKinds,
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#FFFFFF', 'line-width': 7, 'line-opacity': 0.9 },
    })
    map.addLayer({
      id: 'access-driveway-lines',
      type: 'line',
      source: 'access-roadways',
      filter: drivewayKinds,
      layout: { visibility: vis('access'), 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': asExpression([
          'match', ['get', 'kind'],
          'access_subject_driveway', '#BE185D',
          'access_on_site_driveway', '#D97706',
          '#7C3AED',
        ]),
        'line-width': asExpression([
          'match', ['get', 'kind'],
          'access_subject_driveway', 5,
          'access_on_site_driveway', 4,
          3,
        ]),
        'line-opacity': 0.95,
      },
    })
    map.addLayer({
      id: 'access-frontage-labels',
      type: 'symbol',
      source: 'access-roadways',
      filter: asFilter(['==', ['get', 'kind'], 'access_subject_frontage']),
      layout: {
        visibility: vis('access'),
        'symbol-placement': 'line-center',
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: { 'text-color': '#065F46', 'text-halo-color': '#FFFFFF', 'text-halo-width': 3 },
    })
    map.addLayer({
      id: 'access-points',
      type: 'circle',
      source: 'access-roadways',
      filter: asFilter(['==', ['geometry-type'], 'Point']),
      layout: { visibility: vis('access') },
      paint: {
        'circle-color': asExpression([
          'match', ['get', 'kind'],
          'access_signal', '#DC2626',
          'access_subject_driveway', '#BE185D',
          'access_on_site_driveway', '#D97706',
          'access_nearby_driveway', '#7C3AED',
          '#475569',
        ]),
        'circle-radius': 7,
        'circle-stroke-color': '#FFFFFF',
        'circle-stroke-width': 2,
      },
    })
  }

  // ── Drainage ─────────────────────────────────────────────────────────
  const drainage = data.layers.find((layer) => layer.id === 'drainage')
  if (drainage?.available && drainage.geojson) {
    map.addSource('drainage', { type: 'geojson', data: asGeoJson(drainage.geojson) })
    const drainageAreaColor = asExpression([
      'match', ['get', 'kind'],
      'drainage_depression', '#3B82F6',
      'drainage_detention', '#10B981',
      '#3B82F6',
    ])
    const areaFilter = asFilter([
      'in', ['get', 'kind'], ['literal', ['drainage_depression', 'drainage_detention']],
    ])
    map.addLayer({
      id: 'drainage-areas-fill',
      type: 'fill',
      source: 'drainage',
      filter: areaFilter,
      layout: { visibility: vis('drainage') },
      paint: { 'fill-color': drainageAreaColor, 'fill-opacity': 0.3 },
    })
    map.addLayer({
      id: 'drainage-areas-line',
      type: 'line',
      source: 'drainage',
      filter: areaFilter,
      layout: { visibility: vis('drainage') },
      paint: { 'line-color': drainageAreaColor, 'line-width': 2 },
    })
    map.addLayer({
      id: 'drainage-flow-lines',
      type: 'line',
      source: 'drainage',
      filter: asFilter(['==', ['get', 'kind'], 'drainage_flow_path']),
      layout: { visibility: vis('drainage'), 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#0EA5E9',
        'line-width': 3,
        'line-opacity': 0.9,
        'line-dasharray': [2, 1],
      },
    })
    map.addLayer({
      id: 'drainage-runon-points',
      type: 'circle',
      source: 'drainage',
      filter: asFilter(['==', ['get', 'kind'], 'drainage_runon']),
      layout: { visibility: vis('drainage') },
      paint: {
        'circle-color': '#F59E0B',
        'circle-radius': 8,
        'circle-stroke-color': '#FFFFFF',
        'circle-stroke-width': 2,
        'circle-opacity': 0.95,
      },
    })
  }

  // ── Environmental constraints ────────────────────────────────────────
  const environmental = data.layers.find((layer) => layer.id === 'environmental')
  if (environmental?.available && environmental.geojson) {
    const environmentalColor = asExpression([
      'match', ['get', 'kind'],
      'environment_edwards_aquifer', '#0891B2',
      'environment_critical_habitat', '#16A34A',
      '#65A30D',
    ])
    map.addSource('environmental-constraints', {
      type: 'geojson',
      data: asGeoJson(environmental.geojson),
    })
    map.addLayer({
      id: 'environmental-constraints-fill',
      type: 'fill',
      source: 'environmental-constraints',
      layout: { visibility: vis('environmental') },
      paint: { 'fill-color': environmentalColor, 'fill-opacity': 0.2 },
    })
    map.addLayer({
      id: 'environmental-constraints-line',
      type: 'line',
      source: 'environmental-constraints',
      layout: { visibility: vis('environmental') },
      paint: { 'line-color': environmentalColor, 'line-width': 2.5 },
    })
    map.addLayer({
      id: 'environmental-constraints-label',
      type: 'symbol',
      source: 'environmental-constraints',
      layout: {
        visibility: vis('environmental'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: {
        'text-color': environmentalColor,
        'text-halo-color': '#FFFFFF',
        'text-halo-width': 2,
      },
    })
  }

  // ── Population density bands ─────────────────────────────────────────
  const populationDensity = data.layers.find((layer) => layer.id === 'population_density')
  if (populationDensity?.available && populationDensity.geojson) {
    const densityColor = asExpression(['coalesce', ['get', 'density_color'], '#2563EB'])
    map.addSource('population-density', {
      type: 'geojson',
      data: asGeoJson(populationDensity.geojson),
    })
    map.addLayer({
      id: 'population-density-fill',
      type: 'fill',
      source: 'population-density',
      layout: { visibility: vis('population_density') },
      paint: { 'fill-color': densityColor, 'fill-opacity': 0.18 },
    })
    map.addLayer({
      id: 'population-density-line',
      type: 'line',
      source: 'population-density',
      layout: { visibility: vis('population_density') },
      paint: { 'line-color': densityColor, 'line-width': 2.5, 'line-opacity': 0.95 },
    })
    map.addLayer({
      id: 'population-density-label',
      type: 'symbol',
      source: 'population-density',
      layout: {
        visibility: vis('population_density'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: { 'text-color': '#0F172A', 'text-halo-color': '#FFFFFF', 'text-halo-width': 2.5 },
    })
  }

  // ── Special districts (MUD / WCID / PID / TIRZ / ESD) ────────────────
  const regulatory = data.layers.find((layer) => layer.id === 'regulatory')
  if (regulatory?.available && regulatory.geojson) {
    const regulatoryColor = asExpression([
      'match', ['get', 'district_kind'],
      'mud', '#7C3AED',
      'wcid', '#7C3AED',
      'pid', '#BE185D',
      'tirz', '#BE185D',
      '#475569',
    ])
    map.addSource('regulatory-districts', {
      type: 'geojson',
      data: asGeoJson(regulatory.geojson),
    })
    map.addLayer({
      id: 'regulatory-districts-fill',
      type: 'fill',
      source: 'regulatory-districts',
      layout: { visibility: vis('regulatory') },
      paint: {
        'fill-color': SPECIAL_DISTRICT_FILL_COLOR,
        'fill-opacity': SPECIAL_DISTRICT_FILL_OPACITY,
      },
    })
    map.addLayer({
      id: 'regulatory-districts-line',
      type: 'line',
      source: 'regulatory-districts',
      layout: { visibility: vis('regulatory') },
      paint: {
        'line-color': regulatoryColor,
        'line-opacity': 0.95,
        'line-width': SPECIAL_DISTRICT_BOUNDARY_WIDTH,
      },
    })
    map.addLayer({
      id: 'regulatory-districts-label',
      type: 'symbol',
      source: 'regulatory-districts',
      layout: {
        visibility: vis('regulatory'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: {
        'text-color': regulatoryColor,
        'text-halo-color': '#FFFFFF',
        'text-halo-width': 2,
      },
    })
  }

  // ── Buildable envelope ───────────────────────────────────────────────
  const buildable = data.layers.find((layer) => layer.id === 'buildable_envelope')
  if (buildable?.available && buildable.geojson) {
    map.addSource('buildable-envelope', { type: 'geojson', data: asGeoJson(buildable.geojson) })
    map.addLayer({
      id: 'buildable-envelope-fill',
      type: 'fill',
      source: 'buildable-envelope',
      layout: { visibility: vis('buildable_envelope') },
      paint: { 'fill-color': '#D97706', 'fill-opacity': 0.24 },
    })
    map.addLayer({
      id: 'buildable-envelope-line',
      type: 'line',
      source: 'buildable-envelope',
      layout: { visibility: vis('buildable_envelope') },
      paint: { 'line-color': '#92400E', 'line-width': 3 },
    })
  }

  // ── Neighbouring parcels ─────────────────────────────────────────────
  if (contextFeatures.some((feature) => feature.kind === 'neighbor_parcel')) {
    map.addSource('neighbor-parcels', {
      type: 'geojson',
      data: asGeoJson(mapContextFeatureCollection(contextFeatures, 'neighbor_parcel')),
    })
    map.addLayer({
      id: 'neighbor-parcels-fill',
      type: 'fill',
      source: 'neighbor-parcels',
      layout: { visibility: vis('neighbors') },
      paint: { 'fill-color': '#64748B', 'fill-opacity': 0.12 },
    })
    map.addLayer({
      id: 'neighbor-parcels-line',
      type: 'line',
      source: 'neighbor-parcels',
      layout: { visibility: vis('neighbors') },
      paint: { 'line-color': '#475569', 'line-width': 2 },
    })
  }

  // ── Planned public projects ──────────────────────────────────────────
  if (contextFeatures.some((feature) => feature.kind === 'public_project')) {
    map.addSource('public-projects', {
      type: 'geojson',
      data: asGeoJson(mapContextFeatureCollection(contextFeatures, 'public_project')),
    })
    map.addLayer({
      id: 'public-project-points',
      type: 'circle',
      source: 'public-projects',
      layout: { visibility: vis('projects') },
      paint: {
        'circle-color': '#172033',
        'circle-radius': 10,
        'circle-stroke-color': '#EA580C',
        'circle-stroke-width': 3,
      },
    })
    map.addLayer({
      id: 'public-project-label',
      type: 'symbol',
      source: 'public-projects',
      layout: {
        visibility: vis('projects'),
        'text-field': 'P',
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
        'text-allow-overlap': true,
      },
      paint: { 'text-color': '#FFFFFF' },
    })
  }

  // ── The property itself ──────────────────────────────────────────────
  const parcel = data.layers.find((layer) => layer.id === 'parcel')
  if (parcel?.available && parcel.geojson) {
    map.addSource('subject-parcel', { type: 'geojson', data: asGeoJson(parcel.geojson) })
    map.addLayer({
      id: 'subject-parcel-fill',
      type: 'fill',
      source: 'subject-parcel',
      layout: { visibility: vis('parcel') },
      paint: { 'fill-color': '#F2C94C', 'fill-opacity': 0.18 },
    })
    map.addLayer({
      id: 'subject-parcel-line',
      type: 'line',
      source: 'subject-parcel',
      layout: { visibility: vis('parcel') },
      paint: { 'line-color': '#B8962E', 'line-width': 4 },
    })
  } else {
    map.addSource('subject-pin', {
      type: 'geojson',
      data: {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [data.center.lng, data.center.lat] },
        properties: {},
      },
    })
    map.addLayer({
      id: 'subject-pin',
      type: 'circle',
      source: 'subject-pin',
      paint: {
        'circle-radius': 9,
        'circle-color': '#F2C94C',
        'circle-stroke-color': '#0F172A',
        'circle-stroke-width': 3,
      },
    })
  }

  // ── Zoning outline + label, above the property fill ──────────────────
  if (zoning?.available && zoning.geojson) {
    const zoningColor = asExpression([
      'match', ['get', 'jurisdiction_kind'], 'etj', '#0891B2', '#7C3AED',
    ])
    map.addLayer({
      id: 'subject-zoning-line',
      type: 'line',
      source: 'subject-zoning',
      layout: { visibility: vis('zoning') },
      paint: { 'line-color': zoningColor, 'line-width': 4, 'line-dasharray': [2, 1.5] },
    })
    map.addLayer({
      id: 'subject-zoning-label',
      type: 'symbol',
      source: 'subject-zoning',
      layout: {
        visibility: vis('zoning'),
        'text-field': ['get', 'display_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 13,
      },
      paint: {
        'text-color': zoningColor,
        'text-halo-color': '#FFFFFF',
        'text-halo-width': 2.5,
      },
    })
  }

  // ── Nearby places (clustered) ────────────────────────────────────────
  map.addSource('nearby-places', {
    type: 'geojson',
    data: asGeoJson(mapPlacesFeatureCollection(data.places, selectedCategories)),
    cluster: true,
    clusterMaxZoom: 14,
    clusterRadius: 44,
  })
  map.addLayer({
    id: 'poi-clusters',
    type: 'circle',
    source: 'nearby-places',
    filter: asFilter(['has', 'point_count']),
    paint: {
      'circle-color': '#172033',
      'circle-radius': asExpression(['step', ['get', 'point_count'], 15, 10, 20, 30, 25]),
      'circle-stroke-color': '#FFFFFF',
      'circle-stroke-width': 2,
    },
  })
  map.addLayer({
    id: 'poi-cluster-count',
    type: 'symbol',
    source: 'nearby-places',
    filter: asFilter(['has', 'point_count']),
    layout: {
      'text-field': ['get', 'point_count_abbreviated'],
      'text-font': ['Noto Sans Regular'],
      'text-size': 12,
    },
    paint: { 'text-color': '#FFFFFF' },
  })
  map.addLayer({
    id: 'poi-points',
    type: 'circle',
    source: 'nearby-places',
    filter: asFilter(['!', ['has', 'point_count']]),
    paint: {
      'circle-color': POI_CATEGORY_COLOR_EXPRESSION,
      'circle-radius': 7,
      'circle-stroke-color': '#FFFFFF',
      'circle-stroke-width': 2,
    },
  })
  if (map.getLayer('public-project-points')) {
    map.moveLayer('public-project-points', 'poi-clusters')
    map.moveLayer('public-project-label', 'poi-clusters')
  }

  // ── Distance measurement (drawn when a place is selected) ────────────
  map.addSource('measurement', {
    type: 'geojson',
    data: asGeoJson(mapMeasurementFeatureCollection(null)),
  })
  map.addLayer({
    id: 'measurement-line',
    type: 'line',
    source: 'measurement',
    filter: asFilter(['==', ['get', 'kind'], 'line']),
    paint: { 'line-color': '#DC2626', 'line-width': 3, 'line-dasharray': [2, 1.5] },
  })
  map.addLayer({
    id: 'measurement-label',
    type: 'symbol',
    source: 'measurement',
    filter: asFilter(['==', ['get', 'kind'], 'label']),
    layout: {
      'text-field': ['get', 'label'],
      'text-font': ['Noto Sans Regular'],
      'text-size': 13,
      'text-allow-overlap': true,
    },
    paint: {
      'text-color': '#991B1B',
      'text-halo-color': '#FFFFFF',
      'text-halo-width': 3,
    },
  })
}

/**
 * Street names must stay readable over the public raster screens (flood,
 * wetlands, elevation) but below the property outline and the place markers.
 */
export function raiseRoadReferencesAboveRasterOverlays(map: MapLibreMap): void {
  const beforeId = ['neighbor-parcels-fill', 'subject-parcel-fill', 'subject-pin', 'poi-clusters']
    .find((layerId) => map.getLayer(layerId))
  for (const layerId of ROAD_REFERENCE_LAYER_IDS) {
    if (map.getLayer(layerId)) map.moveLayer(layerId, beforeId)
  }
}
