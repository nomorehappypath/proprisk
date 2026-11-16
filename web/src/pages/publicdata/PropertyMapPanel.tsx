// The property map, as the reference app shows it: INLINE inside the first report
// section, not behind a link. Ported widget-for-widget from the pinned panel's
// PublicIntelMap — a basemap toggle, a fit-parcel button, the always-visible
// layer checkbox grid, the nearby-category chip row, the map itself with its
// attribution strip, and the tapped feature's card BELOW the map.
//
// One component serves both placements: `inline` (the report section) and
// `fullscreen` (the phone's tap-to-expand, and the /map deep link). The server
// decides every layer, legend swatch and distance — this only draws them.
import 'maplibre-gl/dist/maplibre-gl.css'

import {
  Map as MapLibreMap,
  NavigationControl,
  ScaleControl,
  setWorkerUrl,
  type GeoJSONSource,
  type MapMouseEvent,
  type MapLayerMouseEvent,
  type RasterTileSource,
} from 'maplibre-gl'
import type { Point as GeoJSONPoint } from 'geojson'
import { memo, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'

import { ApiError } from '../../lib/api'
import {
  applyMapLayerVisibility,
  confidenceLabel,
  distanceBasisLabel,
  formatMapDistance,
  getRunMap,
  hasDrawableLayer,
  hasMapRenderer,
  isImageryDetailLimitReached,
  mapMeasurementFeatureCollection,
  mapPlacesFeatureCollection,
  subjectContextBounds,
  toggleMapLayerVisibility,
  type MapCategory,
  type MapContext,
  type MapFeature,
  type MapLayer,
  type MapPlace,
} from '../../lib/publicDataMap'
import {
  addMapData,
  CLICKABLE_LAYER_IDS,
  raiseRoadReferencesAboveRasterOverlays,
} from '../../lib/publicDataMapLayers'
import { buildInitialMapStyle, poiCategoryColor } from '../../lib/publicDataMapStyle'
import CategoryIcon from './CategoryIcon'

setWorkerUrl('/assets/maplibre-gl-worker.mjs')

// the reference app's own chip order — the eight a broker actually scans for. Every
// other category stays one click away behind "All".
const CHIP_CATEGORY_PRIORITY = [
  'gas_station', 'school', 'apartments', 'restaurant',
  'hotel', 'parking', 'bus_stop', 'pharmacy',
]

function visibleChipCategories(categories: MapCategory[]): MapCategory[] {
  const rank = new Map(CHIP_CATEGORY_PRIORITY.map((id, index) => [id, index]))
  return [...categories]
    .sort(
      (left, right) =>
        (rank.get(left.id) ?? Number.MAX_SAFE_INTEGER)
          - (rank.get(right.id) ?? Number.MAX_SAFE_INTEGER)
        || left.label.localeCompare(right.label),
    )
    .slice(0, 8)
}

const GENERIC = 'Something went wrong — please try again.'

const ATTRIBUTION =
  'Map services: USGS National Map; OpenFreeMap / OpenMapTiles, data from OpenStreetMap. ' +
  'Nearby places: OpenStreetMap contributors and the listed public agencies.'

function fitSubject(map: MapLibreMap, data: MapContext): void {
  map.fitBounds(subjectContextBounds(data), { padding: 42, duration: 500, maxZoom: 18 })
}

// The map lives inside a report that polls itself. Memoised on its two
// scalar props, so a poll tick that re-renders the report never re-renders —
// let alone remounts — the map: the maplibre instance is created exactly once
// per run and survives every refresh of the surrounding page.
export default memo(PropertyMapPanel)

function PropertyMapPanel({
  runId,
  initialFullscreen = false,
}: {
  runId: string
  initialFullscreen?: boolean
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const selectedCategoriesRef = useRef<Set<string>>(new Set())
  const activeLayersRef = useRef<Set<string>>(new Set(['parcel']))

  const [data, setData] = useState<MapContext | null>(null)
  const [error, setError] = useState('')
  const [selectedCategories, setSelectedCategories] = useState<Set<string>>(new Set())
  const [activeLayers, setActiveLayers] = useState<Set<string>>(new Set(['parcel']))
  const [basemapId, setBasemapId] = useState('')
  const [mapZoom, setMapZoom] = useState(14)
  const [selectedPlaceId, setSelectedPlaceId] = useState<string | null>(null)
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null)
  const [parcelSelected, setParcelSelected] = useState(false)
  const [layersOpen, setLayersOpen] = useState(true)
  const [categoryMenuOpen, setCategoryMenuOpen] = useState(false)
  const [fullscreen, setFullscreen] = useState(initialFullscreen)
  const [mapWarning, setMapWarning] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getRunMap(runId)
      .then((payload) => {
        if (cancelled) return
        // A payload without a basemap or a layer list cannot be drawn. Say so
        // plainly instead of half-building a map that will never paint.
        if (!Array.isArray(payload?.layers) || !Array.isArray(payload?.basemaps)) {
          setError('The property map is not available for this lookup.')
          return
        }
        setData(payload)
        setBasemapId(payload.basemaps[0]?.id ?? '')
        const defaultLayers = new Set<string>()
        if (payload.layers.some((layer) => layer.id === 'parcel' && hasDrawableLayer(layer))) {
          defaultLayers.add('parcel')
        }
        activeLayersRef.current = defaultLayers
        setActiveLayers(defaultLayers)
        selectedCategoriesRef.current = new Set()
        setSelectedCategories(new Set())
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : GENERIC)
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  const selectedPlace = useMemo(
    () => data?.places.find((place) => place.id === selectedPlaceId) ?? null,
    [data, selectedPlaceId],
  )
  const selectedFeature = useMemo(
    () => (data?.context_features ?? []).find((feature) => feature.id === selectedFeatureId) ?? null,
    [data, selectedFeatureId],
  )

  const clearPlaceSelection = useCallback(() => {
    setSelectedPlaceId(null)
    const source = mapRef.current?.getSource('measurement') as GeoJSONSource | undefined
    source?.setData(mapMeasurementFeatureCollection(null))
  }, [])

  // ── Build the map once the payload is in hand ──────────────────────────
  useEffect(() => {
    if (!data || !containerRef.current || mapRef.current) return
    if (!('WebGLRenderingContext' in window)) {
      setMapWarning('This browser cannot show the interactive map.')
      return
    }
    const map = new MapLibreMap({
      container: containerRef.current,
      style: buildInitialMapStyle(data, data.basemaps[0]?.id ?? ''),
      center: [data.center.lng, data.center.lat],
      zoom: 14,
      minZoom: 5,
      maxZoom: 22,
      attributionControl: { compact: true },
      cooperativeGestures: true, // two-finger pan, so the report still scrolls
    })
    mapRef.current = map
    map.addControl(
      // the reference app's own control set, ported verbatim: the compass IS the
      // north indicator (it rotates with bearing and snaps back to north when
      // clicked) and it carries the zoom in/out buttons.
      new NavigationControl({ showCompass: true, showZoom: true, visualizePitch: true }),
      'top-right',
    )
    map.addControl(new ScaleControl({ unit: 'imperial' }), 'bottom-left')

    let initialized = false
    const initializeMap = () => {
      if (initialized) return
      try {
        addMapData(map, data, selectedCategoriesRef.current, activeLayersRef.current)
        raiseRoadReferencesAboveRasterOverlays(map)
        fitSubject(map, data)
        initialized = true
      } catch (error) {
        console.error('Property map initialization failed', error)
        setMapWarning('The interactive map layers could not be initialized.')
        return
      }

      map.on('click', 'poi-points', (event: MapLayerMouseEvent) => {
        const placeId = event.features?.[0]?.properties?.id
        if (typeof placeId === 'string') {
          setSelectedFeatureId(null)
          setSelectedPlaceId(placeId)
        }
      })
      map.on('click', 'poi-clusters', (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0]
        const clusterId = Number(feature?.properties?.cluster_id)
        const coordinates = (feature?.geometry as GeoJSONPoint | undefined)?.coordinates
        const source = map.getSource('nearby-places') as GeoJSONSource | undefined
        if (!source || !coordinates || !Number.isFinite(clusterId)) return
        source
          .getClusterExpansionZoom(clusterId)
          .then((zoom) => map.easeTo({ center: coordinates as [number, number], zoom }))
          .catch(() => setMapWarning('That group of markers could not be opened.'))
      })
      for (const layerId of ['poi-points', 'poi-clusters']) {
        map.on('mouseenter', layerId, () => {
          map.getCanvas().style.cursor = 'pointer'
        })
        map.on('mouseleave', layerId, () => {
          map.getCanvas().style.cursor = ''
        })
      }
      for (const layerId of CLICKABLE_LAYER_IDS) {
        if (!map.getLayer(layerId)) continue
        map.on('click', layerId, (event: MapLayerMouseEvent) => {
          const featureId = event.features?.[0]?.properties?.id
          if (typeof featureId === 'string') {
            clearPlaceSelection()
            setParcelSelected(false)
            setSelectedFeatureId(featureId)
          }
        })
        map.on('mouseenter', layerId, () => {
          map.getCanvas().style.cursor = 'pointer'
        })
        map.on('mouseleave', layerId, () => {
          map.getCanvas().style.cursor = ''
        })
      }
      if (map.getLayer('subject-parcel-fill')) {
        map.on('click', 'subject-parcel-fill', () => {
          clearPlaceSelection()
          setSelectedFeatureId(null)
          setParcelSelected(true)
        })
        map.on('mouseenter', 'subject-parcel-fill', () => {
          map.getCanvas().style.cursor = 'pointer'
        })
        map.on('mouseleave', 'subject-parcel-fill', () => {
          map.getCanvas().style.cursor = ''
        })
      }
      // A single map-level fallback makes hit-testing reliable when a style
      // layer is covered by another rendered layer or a provider tile updates
      // after the layer-specific listener was registered.
      map.on('click', (event: MapMouseEvent) => {
        const rendered = map.queryRenderedFeatures(event.point)
        const parcelHit = rendered.some((feature) => feature.layer?.id === 'subject-parcel-fill')
        if (parcelHit) {
          clearPlaceSelection()
          setSelectedFeatureId(null)
          setParcelSelected(true)
          return
        }
        const id = rendered
          .map((feature) => feature.properties?.id)
          .find((value): value is string => typeof value === 'string')
        if (!id) return
        const place = data.places.find((item) => item.id === id)
        if (place) {
          setSelectedFeatureId(null)
          setSelectedPlaceId(id)
          return
        }
        if ((data.context_features ?? []).some((feature) => feature.id === id)) {
          clearPlaceSelection()
          setParcelSelected(false)
          setSelectedFeatureId(id)
        }
      })
    }
    if (map.isStyleLoaded()) initializeMap()
    else map.once('style.load', initializeMap)
    map.once('load', initializeMap)
    const initializationPoll = window.setInterval(() => {
      if (initialized) window.clearInterval(initializationPoll)
      else if (map.isStyleLoaded()) initializeMap()
    }, 50)
    // Dev-only handle. The map is a canvas: without it, a browser test can
    // click the zoom and compass controls but cannot assert what they did.
    // Gated on DEV so it is absent from every production bundle.
    if (import.meta.env.DEV) {
      ;(window as unknown as Record<string, unknown>).__pdmap = map
    }
    map.on('zoomend', () => setMapZoom(map.getZoom()))
    map.on('error', () => setMapWarning('One map source is temporarily unavailable.'))

    return () => {
      window.clearInterval(initializationPoll)
      mapRef.current = null
      map.remove()
    }
  }, [data, clearPlaceSelection])

  useEffect(() => {
    const source = mapRef.current?.getSource('nearby-places') as GeoJSONSource | undefined
    if (source && data) source.setData(mapPlacesFeatureCollection(data.places, selectedCategories))
  }, [data, selectedCategories])

  useEffect(() => {
    const map = mapRef.current
    if (!map?.isStyleLoaded()) return
    const source = map.getSource('measurement') as GeoJSONSource | undefined
    source?.setData(mapMeasurementFeatureCollection(selectedPlace))
  }, [selectedPlace])

  useEffect(() => {
    const map = mapRef.current
    if (map) applyMapLayerVisibility(map, activeLayers)
  }, [activeLayers])

  // Changing the background swaps raster tiles in place — every overlay,
  // outline and marker stays exactly where it is.
  useEffect(() => {
    const map = mapRef.current
    if (!map?.isStyleLoaded() || !data || !basemapId) return
    const selected = data.basemaps.find((item) => item.id === basemapId)
    const source = map.getSource('basemap') as RasterTileSource | undefined
    if (!selected || !source) return
    source.setTiles([selected.tile_url])
    const visibility = selected.road_vector_url ? 'visible' : 'none'
    for (const layerId of [
      'road-reference-major-casing', 'road-reference-major',
      'road-reference-minor-casing', 'road-reference-minor', 'road-reference-label',
    ]) {
      if (map.getLayer(layerId)) map.setLayoutProperty(layerId, 'visibility', visibility)
    }
  }, [basemapId, data])

  // Expanding, collapsing the layer grid or opening a feature card all change
  // how much canvas is visible; maplibre only re-reads its container when told.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const timer = window.setTimeout(() => map.resize(), 260)
    return () => window.clearTimeout(timer)
  }, [fullscreen, layersOpen, selectedPlaceId, selectedFeatureId])

  const toggleCategory = (categoryId: string) => {
    if (!data) return
    setSelectedCategories((current) => {
      const next = new Set(current)
      if (next.has(categoryId)) next.delete(categoryId)
      else next.add(categoryId)
      selectedCategoriesRef.current = next
      const source = mapRef.current?.getSource('nearby-places') as GeoJSONSource | undefined
      source?.setData(mapPlacesFeatureCollection(data.places, next))
      return next
    })
    clearPlaceSelection()
    setParcelSelected(false)
  }

  const toggleLayer = (layerId: string) => {
    setActiveLayers((current) => {
      const next = toggleMapLayerVisibility(mapRef.current, current, layerId)
      activeLayersRef.current = next
      // Switching a layer on NEVER moves the view. the reference app does not re-fit,
      // and an automatic zoom-out takes the map out from under the reader —
      // it measured as 14.5 → 11.9 on a single checkbox. The rings and other
      // wide layers draw at the parcel's own zoom; the reader zooms out if
      // and when they want to.
      return next
    })
  }

  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="muted-line">Opening the property map…</p>

  const mapLayers = data.layers.filter(hasMapRenderer)
  const activeLayerCards = data.layers.filter(
    (layer) => activeLayers.has(layer.id) && hasDrawableLayer(layer),
  )
  const selectedBasemap = data.basemaps.find((item) => item.id === basemapId) ?? data.basemaps[0]
  const detailLimit = isImageryDetailLimitReached(selectedBasemap, mapZoom)
  const chipCategories = visibleChipCategories(data.categories)

  return (
    <div className={`pdmap-panel${fullscreen ? ' is-fullscreen' : ''}`} aria-label="Property map">
      {/* Map control row — background, fit, layers (the reference app's own order). */}
      <div className="pdmap-toolbar">
        <div className="pdmap-basemaps" role="group" aria-label="Map background">
          {data.basemaps.map((basemap) => (
            <button
              key={basemap.id}
              type="button"
              aria-pressed={basemapId === basemap.id}
              className={basemapId === basemap.id ? 'is-active' : ''}
              onClick={() => setBasemapId(basemap.id)}
            >
              {basemap.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="pdmap-tool"
          onClick={() => mapRef.current && fitSubject(mapRef.current, data)}
        >
          ⛶ Fit parcel
        </button>
        <button
          type="button"
          className={`pdmap-tool${layersOpen ? ' is-active' : ''}`}
          aria-expanded={layersOpen}
          onClick={() => setLayersOpen((open) => !open)}
        >
          ☰ Layers
        </button>
        <button
          type="button"
          className="pdmap-tool"
          aria-pressed={fullscreen}
          onClick={() => setFullscreen((open) => !open)}
        >
          {fullscreen ? '✕ Close full screen' : '⤢ Full screen'}
        </button>
      </div>

      {/* Layer checkbox grid — inline and visible, never hidden in a drawer. */}
      {layersOpen && (
        <div className="pdmap-layer-grid" role="group" aria-label="Map layers">
          {mapLayers.map((layer) => (
            <label
              key={layer.id}
              className={`pdmap-layer${layer.available ? '' : ' is-unavailable'}`}
            >
              <input
                type="checkbox"
                checked={activeLayers.has(layer.id)}
                disabled={!layer.available}
                onChange={() => toggleLayer(layer.id)}
              />
              <span>{layer.label}</span>
              {!layer.available && <small>Nothing mapped here</small>}
            </label>
          ))}
        </div>
      )}

      {/* Nearby-category chips. the reference app shows the EIGHT categories a broker
          reaches for, in its own priority order, and puts the rest behind
          "All" — 32 chips at once is a wall, not a control. */}
      <div className="pdmap-chiprow" role="group" aria-label="Nearby places">
        {chipCategories.map((category) => (
          <button
            key={category.id}
            type="button"
            aria-pressed={selectedCategories.has(category.id)}
            className={`pdmap-chip${selectedCategories.has(category.id) ? ' is-active' : ''}`}
            style={{ '--poi-color': poiCategoryColor(category.id) } as CSSProperties}
            onClick={() => toggleCategory(category.id)}
          >
            <i className="pdmap-chip-swatch" aria-hidden />
            <CategoryIcon category={category.id} />
            <span>{category.label}</span>
            <strong>{category.count}</strong>
          </button>
        ))}
        <button
          type="button"
          className="pdmap-chip-more"
          aria-expanded={categoryMenuOpen}
          onClick={() => setCategoryMenuOpen((open) => !open)}
        >
          All
        </button>
      </div>

      {categoryMenuOpen && (
        <div className="pdmap-category-menu" aria-label="All nearby categories">
          {data.categories.map((category) => (
            <label
              key={category.id}
              style={{ '--poi-color': poiCategoryColor(category.id) } as CSSProperties}
            >
              <input
                type="checkbox"
                checked={selectedCategories.has(category.id)}
                onChange={() => toggleCategory(category.id)}
              />
              <CategoryIcon category={category.id} />
              <span>{category.label}</span>
              <small>{category.count}</small>
            </label>
          ))}
        </div>
      )}

      {/* The frame's geometry is stated INLINE, not left to a stylesheet.
          maplibre runs its own ResizeObserver on the container: if the
          container's height is ever derived from its content, the canvas it
          draws feeds that height back and the two grow without bound (the
          page scrolls forever and the canvas goes blurry). An explicit height
          plus an absolutely-positioned canvas breaks that loop, and stating
          it here means a missing or late-loading CSS rule can never
          reintroduce it. `--pdmap-h` is what the stylesheet varies per
          breakpoint; the literal is the floor if it never arrives. */}
      <div
        className="pdmap-frame"
        style={{
          position: 'relative',
          height: 'var(--pdmap-h, 340px)',
          overflow: 'hidden',
          contain: 'strict',
        }}
      >
        <div
          ref={containerRef}
          className="pdmap-canvas"
          style={{ position: 'absolute', inset: 0 }}
        />
      </div>
      <p className="pdmap-attribution">{ATTRIBUTION}</p>

      {mapWarning && (
        <div className="pdmap-warning" role="status">
          <span>{mapWarning}</span>
          <button type="button" onClick={() => setMapWarning(null)} aria-label="Dismiss">
            ✕
          </button>
        </div>
      )}

      {/* What is selected on the map, directly BELOW it. */}
      <div className="pdmap-below">
        {selectedPlace && <SelectedPlaceCard place={selectedPlace} onClose={clearPlaceSelection} />}
        {selectedFeature && (
          <SelectedFeatureCard
            feature={selectedFeature}
            onClose={() => setSelectedFeatureId(null)}
          />
        )}
        {parcelSelected && (
          <SelectedParcelCard
            layer={data.layers.find((layer) => layer.id === 'parcel')}
            onClose={() => setParcelSelected(false)}
          />
        )}
        {activeLayerCards.map((layer) => (
          <LayerCard key={layer.id} layer={layer} />
        ))}
        {!selectedPlace && !selectedFeature && activeLayerCards.length === 0 && (
          <p className="muted-line">
            Tap anything on the map — a marker, a boundary, a road — and what the public record says
            about it appears here.
          </p>
        )}
        {detailLimit && (
          <div className="pdmap-resolution" role="status">
            <strong>Closest public imagery reached.</strong> {selectedBasemap.detail_advisory}
            <span>
              {selectedBasemap.provider_label}
              {selectedBasemap.resolution_label ? ` · ${selectedBasemap.resolution_label}` : ''}
            </span>
          </div>
        )}
        {data.advisories.length > 0 && (
          <div className="pdmap-advisories" role="status">
            {data.advisories.map((advisory) => (
              <p key={advisory}>{advisory}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Sub-components ──────────────────────────────────────────────────────

const LONG_TEXT_THRESHOLD = 240
const LONG_TEXT_PREVIEW = 180

function compactText(value: string): string {
  const candidate = value.slice(0, LONG_TEXT_PREVIEW + 1)
  const lastWhitespace = candidate.lastIndexOf(' ')
  const cutAt =
    lastWhitespace >= Math.floor(LONG_TEXT_PREVIEW * 0.6) ? lastWhitespace : LONG_TEXT_PREVIEW
  return `${value.slice(0, cutAt).trimEnd()}…`
}

function ExpandableText({ value, label }: { value: string; label: string }) {
  const [expanded, setExpanded] = useState(false)
  if (value.length <= LONG_TEXT_THRESHOLD) return <>{value}</>
  return (
    <span className="pdmap-long-text">
      <span>{expanded ? value : compactText(value)}</span>
      <button
        type="button"
        className="linklike"
        aria-expanded={expanded}
        aria-label={`${expanded ? 'Show less' : 'Show all'} for ${label}`}
        onClick={() => setExpanded((current) => !current)}
      >
        {expanded ? 'Show less' : 'Show all'}
      </button>
    </span>
  )
}

function LayerCard({ layer }: { layer: MapLayer }) {
  const accent = layer.legend[0]?.color ?? '#B8962E'
  return (
    <section
      className="pdmap-layer-card"
      aria-label={`${layer.label} details`}
      style={{ '--layer-color': accent } as CSSProperties}
    >
      <div className="pdmap-layer-card-head">
        <h4>{layer.label}</h4>
        <span>ON MAP</span>
      </div>
      {layer.legend.length > 0 && (
        <div className="pdmap-legend" aria-label={`${layer.label} key`}>
          {/* Two legend rows can carry the same label (a district type mapped
              from more than one source), so the index is the only safe key. */}
          {layer.legend.map((item, i) => (
            <span key={`${layer.id}-${i}`}>
              <i style={{ background: item.color ?? accent }} aria-hidden /> {item.label}
            </span>
          ))}
        </div>
      )}
      {layer.details.length > 0 && (
        <dl>
          {/* A layer can list the same label twice — two water control &
              improvement districts both cover this site — so the index is the
              only safe key here too. */}
          {layer.details.map((detail, i) => (
            <div key={`${layer.id}-detail-${i}`}>
              <dt>{detail.label}</dt>
              <dd>
                <ExpandableText value={detail.value} label={detail.label} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {layer.advisory && (
        <p className="pdmap-layer-advisory">
          <ExpandableText value={layer.advisory} label={`${layer.label} caution`} />
        </p>
      )}
      <p className="pdmap-source">Source: {layer.attribution}</p>
    </section>
  )
}

function SelectedPlaceCard({ place, onClose }: { place: MapPlace; onClose: () => void }) {
  return (
    <section className="pdmap-selected" aria-live="polite">
      <div>
        <p className="pdmap-selected-name">{place.name}</p>
        <p>
          {place.category.replaceAll('_', ' ')} · {formatMapDistance(place.distance_m)}{' '}
          {distanceBasisLabel(place.distance_basis)}
        </p>
        {place.address && <p>{place.address}</p>}
        <p className="pdmap-source">
          Straight-line distance · {place.source} · {confidenceLabel(place.confidence)}
        </p>
      </div>
      <button type="button" className="linklike" onClick={onClose} aria-label="Close place details">
        ✕
      </button>
    </section>
  )
}

function SelectedParcelCard({
  layer,
  onClose,
}: {
  layer?: MapLayer
  onClose: () => void
}) {
  if (!layer) return null
  return (
    <section className="pdmap-selected is-feature" aria-live="polite">
      <div>
        <div className="pdmap-selected-head">
          <p className="pdmap-selected-name">{layer.label}</p>
          <span className="pdmap-onmap">ON MAP</span>
        </div>
        {layer.details.map((detail, i) => (
          <p key={`${detail.label}-${i}`}><strong>{detail.label}:</strong> {detail.value}</p>
        ))}
        <p className="pdmap-source">Source: {layer.attribution}</p>
      </div>
      <button type="button" className="linklike" onClick={onClose} aria-label="Close parcel details">✕</button>
    </section>
  )
}

function SelectedFeatureCard({
  feature,
  onClose,
}: {
  feature: MapFeature
  onClose: () => void
}) {
  return (
    <section className="pdmap-selected is-feature" aria-live="polite">
      <div>
        <div className="pdmap-selected-head">
          <p className="pdmap-selected-name">{feature.title}</p>
          <span className="pdmap-onmap">ON MAP</span>
        </div>
        {feature.subtitle && <p>{feature.subtitle}</p>}
        {feature.details.length > 0 && (
          <dl>
            {feature.details.map((detail) => (
              <div key={detail.label}>
                <dt>{detail.label}</dt>
                <dd>
                  {detail.url ? (
                    <a href={detail.url} target="_blank" rel="noreferrer">
                      {detail.value}
                    </a>
                  ) : (
                    <ExpandableText value={detail.value} label={detail.label} />
                  )}
                </dd>
              </div>
            ))}
          </dl>
        )}
        <p className="pdmap-source">
          {feature.source} · {confidenceLabel(feature.confidence)}
        </p>
      </div>
      <button
        type="button"
        className="linklike"
        onClick={onClose}
        aria-label="Close map feature details"
      >
        ✕
      </button>
    </section>
  )
}
