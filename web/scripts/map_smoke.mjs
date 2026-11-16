// Executable evidence for the Public Data property map. Drives the REAL
// rendered app against the live local stack (backend :8765, vite :5173) at
// iPhone width and at desktop width.
//
// The map is INLINE in the report's first section (Parcel Info), so this
// exercises it where it lives: basemap swap → layer toggle → nearby-category
// chip → tap a marker → the details card → full screen and back. Fails on
// any console error other than the known upstream NWI wetlands outage.
//
//   SMOKE_RUN_ID=<an existing run id> node scripts/map_smoke.mjs
//
// Screenshots land in SHOT_DIR (default ./smoke-shots).

import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright-core'

const BASE = process.env.SMOKE_BASE_URL ?? 'http://127.0.0.1:5173'
const RUN_ID = process.env.SMOKE_RUN_ID ?? ''
const SHOTS = process.env.SHOT_DIR ?? 'smoke-shots'
if (!RUN_ID) {
  console.error('Set SMOKE_RUN_ID to an existing, completed lookup id.')
  process.exit(2)
}
mkdirSync(SHOTS, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const errors = []

// The NWI wetlands service moved and its replacement sends no CORS header —
// a known upstream outage, tracked separately. It must not mask a real error,
// so it is filtered by host rather than by ignoring console errors wholesale.
function isKnownUpstream(text) {
  return text.includes('fwspublicservices.wim.usgs.gov') || text.includes('net::ERR_FAILED')
}

async function drive(label, viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  page.on('pageerror', (e) => errors.push(`[${label}] ${e}`))
  page.on('console', (m) => {
    if (m.type() === 'error' && !isKnownUpstream(m.text())) errors.push(`[${label}] ${m.text()}`)
  })

  await page.goto(`${BASE}/public-data/runs/${RUN_ID}`, { waitUntil: 'domcontentloaded' })
  // Parcel Info opens by default and carries the map — no navigation needed.
  await page.waitForSelector('.pdmap-canvas canvas', { timeout: 30000 })
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.pdmap-canvas canvas')
    return !!canvas && canvas.width > 0
  })
  await page.waitForFunction(
    () => {
      const map = window.__pdmap
      if (!map?.isStyleLoaded() || !map.getLayer('subject-parcel-fill')) return false
      return map.querySourceFeatures('subject-parcel').length > 0
    },
    undefined,
    { timeout: 15000 },
  )
  await page.waitForTimeout(1500) // tiles + parcel outline settle
  await page.screenshot({ path: `${SHOTS}/${label}_1_report_map_inline.png` })

  // Terrain basemap, then back to aerial+roads.
  await page.click('.pdmap-basemaps button:nth-child(3)')
  await page.waitForTimeout(2500)
  await page.screenshot({ path: `${SHOTS}/${label}_2_basemap_terrain.png` })
  await page.click('.pdmap-basemaps button:nth-child(1)')
  await page.waitForTimeout(2000)

  // The layer grid is inline and always visible — turn on flood and traffic.
  await page.waitForSelector('.pdmap-layer-grid')
  for (const layerLabel of ['Flood', 'Traffic']) {
    const box = page.locator(`.pdmap-layer:has-text("${layerLabel}") input`).first()
    if ((await box.count()) && !(await box.isDisabled())) await box.check({ force: true })
  }
  await page.waitForTimeout(1500)
  const trafficState = await page.evaluate(() => {
    const map = window.__pdmap
    const layerIds = ['traffic-count-lines', 'traffic-station-points']
      .filter((id) => map?.getLayer(id))
    return {
      sourceFeatures: map?.getSource('traffic-counts')
        ? map.querySourceFeatures('traffic-counts').length
        : 0,
      visibleLayers: layerIds.filter(
        (id) => (map.getLayoutProperty(id, 'visibility') ?? 'visible') !== 'none',
      ).length,
      renderedFeatures: layerIds.length
        ? map.queryRenderedFeatures(undefined, { layers: layerIds }).length
        : 0,
    }
  })
  if (!trafficState.sourceFeatures || !trafficState.visibleLayers || !trafficState.renderedFeatures) {
    errors.push(
      `[${label}] Traffic enabled but did not draw ` +
        `(source=${trafficState.sourceFeatures}, visible=${trafficState.visibleLayers}, ` +
        `rendered=${trafficState.renderedFeatures})`,
    )
  }
  await page.locator('.pdmap-frame').scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${SHOTS}/${label}_3_layers_on.png` })

  // Each active layer contributes its legend + details card below the map.
  await page.waitForSelector('.pdmap-layer-card')
  await page.locator('.pdmap-layer-card').first().scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${SHOTS}/${label}_4_layer_legend.png` })

  // Nearby places: the chip row (the reference app's eight), one category on.
  await page.waitForSelector('.pdmap-chip')
  await page.locator('.pdmap-chip').first().click()
  await page.waitForTimeout(2500)
  await page.locator('.pdmap-frame').scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${SHOTS}/${label}_5_nearby_category.png` })

  // "All" opens the full 32-category menu.
  await page.click('.pdmap-chip-more')
  await page.waitForSelector('.pdmap-category-menu label')
  await page.screenshot({ path: `${SHOTS}/${label}_5b_all_categories.png` })
  await page.click('.pdmap-chip-more')

  // Move to one selected-category feature and tap the actual rendered marker.
  // This proves category controls feed the map worker and that its hit target
  // is wired to the nearby-place detail card.
  const activeCategoryLabel = await page.locator('.pdmap-chip.is-active span').last().textContent()
  const poiCoordinates = await page.evaluate(async ({ runId, categoryLabel }) => {
    const map = window.__pdmap
    const response = await fetch(`/api/site-check/runs/${runId}/map`)
    if (!response.ok) return null
    const payload = await response.json()
    const category = payload.categories?.find((item) => item.label === categoryLabel)
    const place = payload.places?.find((item) => item.category === category?.id)
    const coordinates = place ? [place.lng, place.lat] : null
    if (!Array.isArray(coordinates) || coordinates.length < 2) return null
    map.jumpTo({ center: coordinates, zoom: 16 })
    return coordinates
  }, { runId: RUN_ID, categoryLabel: activeCategoryLabel })
  if (!poiCoordinates) {
    errors.push(`[${label}] selected nearby category put no features in the map source`)
  } else {
    await page.waitForFunction(
      () => window.__pdmap.queryRenderedFeatures(undefined, { layers: ['poi-points'] }).length > 0,
      undefined,
      { timeout: 5000 },
    )
    await page.locator('.pdmap-canvas canvas').scrollIntoViewIfNeeded()
    const poiPoint = await page.evaluate((coordinates) => {
      const point = window.__pdmap.project(coordinates)
      return { x: point.x, y: point.y }
    }, poiCoordinates)
    const canvasBox = await page.locator('.pdmap-canvas canvas').boundingBox()
    if (!canvasBox) {
      errors.push(`[${label}] map canvas has no bounding box for nearby-place click`)
    } else {
      await page.mouse.click(canvasBox.x + poiPoint.x, canvasBox.y + poiPoint.y)
      await page.waitForTimeout(750)
      if (!(await page.locator('button[aria-label="Close place details"]').count())) {
        errors.push(`[${label}] tapping a nearby-place marker opened no detail card`)
      } else {
        await page.screenshot({ path: `${SHOTS}/${label}_5c_selected_place.png` })
      }
    }
  }

  await page.locator('.pdmap-tool', { hasText: 'Fit parcel' }).click()
  await page.waitForTimeout(750)
  await page.locator('.pdmap-canvas canvas').scrollIntoViewIfNeeded()

  // Tap an actual rendered parcel pixel, not the frame centre. The parcel is
  // not necessarily centred after context padding, and an inert canvas must
  // fail this gate rather than merely omit the evidence screenshot.
  const parcelPoint = await page.evaluate(() => {
    const map = window.__pdmap
    const canvas = map?.getCanvas()
    if (!map || !canvas) return null
    const bounds = canvas.getBoundingClientRect()
    for (let y = 0; y < bounds.height; y += 4) {
      for (let x = 0; x < bounds.width; x += 4) {
        if (map.queryRenderedFeatures([x, y], { layers: ['subject-parcel-fill'] }).length) {
          return { x, y }
        }
      }
    }
    return null
  })
  if (!parcelPoint) {
    errors.push(`[${label}] subject parcel has no rendered hit target`)
  } else {
    const canvasBox = await page.locator('.pdmap-canvas canvas').boundingBox()
    if (!canvasBox) {
      errors.push(`[${label}] map canvas has no bounding box`)
    } else {
      await page.mouse.click(canvasBox.x + parcelPoint.x, canvasBox.y + parcelPoint.y)
      await page.waitForTimeout(750)
      const selected = page.locator('.pdmap-selected')
      if (!(await page.locator('button[aria-label="Close parcel details"]').count())) {
        errors.push(`[${label}] tapping the subject parcel opened no detail card`)
      } else {
        await selected.first().scrollIntoViewIfNeeded()
        await page.screenshot({ path: `${SHOTS}/${label}_6_selected_details.png` })
      }
    }
  }

  // Full screen and back — the map instance must survive both.
  const canvasesBefore = await page.locator('.pdmap-canvas canvas').count()
  await page.locator('.pdmap-tool', { hasText: 'Full screen' }).click()
  await page.waitForTimeout(2500)
  await page.screenshot({ path: `${SHOTS}/${label}_7_fullscreen.png` })
  await page.locator('.pdmap-tool', { hasText: 'Close full screen' }).click()
  await page.waitForTimeout(1500)
  const canvasesAfter = await page.locator('.pdmap-canvas canvas').count()
  if (canvasesBefore !== 1 || canvasesAfter !== 1) {
    errors.push(`[${label}] map canvases ${canvasesBefore} → ${canvasesAfter} (expected 1 → 1)`)
  }
  await page.screenshot({ path: `${SHOTS}/${label}_8_back_inline.png` })

  // The page itself must never scroll sideways.
  const overflow = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    win: window.innerWidth,
  }))
  if (overflow.doc > overflow.win + 1) {
    errors.push(`[${label}] horizontal page scroll (${overflow.doc} > ${overflow.win})`)
  }

  await context.close()
}

try {
  await drive('phone', { width: 390, height: 844 })
  await drive('desktop', { width: 1440, height: 900 })
} finally {
  await browser.close()
}

if (errors.length) {
  console.error(`FAIL — ${errors.length} problem(s):`)
  for (const e of errors) console.error('  ' + e)
  process.exit(1)
}
console.log('Map smoke passed — screenshots in', SHOTS)
