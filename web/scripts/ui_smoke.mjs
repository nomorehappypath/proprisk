// Executable UI smoke — drives the real rendered app in headless Chrome at
// iPhone viewport. Run with both servers up (backend :8765, vite :5173):
//
//   npm run smoke:ui
//
// PropRisk has no sign-in, so this is the Public Data screens only: the
// fixture-driven report (every run state), the new-lookup → progress flow,
// and history. Fails on any console error. Screenshots land in
// ./smoke-shots/ (gitignored).

import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from 'playwright-core'

const BASE = process.env.SMOKE_BASE_URL ?? 'http://127.0.0.1:5173'

const SHOTS = 'smoke-shots'
mkdirSync(SHOTS, { recursive: true })

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})

try {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  for (const file of ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs']) {
    const response = await page.request.get(`${BASE}/assets/${file}`)
    const contentType = response.headers()['content-type'] ?? ''
    const bodyStart = (await response.text()).trimStart().slice(0, 16)
    if (!response.ok() || !contentType.includes('javascript') || bodyStart.startsWith('<')) {
      throw new Error(
        `${file} is not a JavaScript asset ` +
          `(status=${response.status()}, content-type=${contentType}, starts=${bodyStart})`,
      )
    }
  }
  console.log('MapLibre worker assets are executable JavaScript: OK')
  // No sign-in: "/" redirects straight to the New Lookup screen.
  await page.waitForSelector('text=Property address')
  await page.screenshot({ path: `${SHOTS}/1_new_lookup.png` })

  // ── Public Data: fixture-driven render assertions ────────────────────
  // The report/progress/history screens render SERVER-built payloads;
  // every run state is mounted here from a fixture with the API route
  // mocked, asserting the key copy renders.
  const pdFix = (name) =>
    JSON.parse(readFileSync(new URL(`./fixtures/public_data/${name}.json`, import.meta.url), 'utf8'))
  const mountPdRun = async (f) => {
    await page.route(`**/api/site-check/runs/${f.id}`, (route) => route.fulfill({ json: f }))
    // Parcel Info carries the map. These fixtures exercise report STRUCTURE —
    // the map has its own live gate (map_smoke.mjs) — so the map context is
    // stubbed as unavailable and the section shows its plain "not available"
    // line instead of pulling tiles.
    await page.route(`**/api/site-check/runs/${f.id}/map`, (route) => route.fulfill({ json: {} }))
    await page.goto(`${BASE}/public-data/runs/${f.id}`, { waitUntil: 'domcontentloaded' })
  }
  const unmountPdRun = async (f) => {
    await page.unroute(`**/api/site-check/runs/${f.id}/map`)
    await page.unroute(`**/api/site-check/runs/${f.id}`)
  }

  // Completed report: the flat accordion — action bar, ordered
  // collapsible sections with a compact summary and a confidence badge on
  // each collapsed row, Parcel Info open first, anchor note, status chips,
  // the manual section, composite provenance, cache-stale note.
  const openPdCard = async (title) => {
    const card = page.locator(`details:has(.pd-card-title:text-is("${title}"))`)
    if (!(await card.evaluate((el) => el.open))) await card.locator('summary').click()
  }
  const pdFull = pdFix('completed_full')
  await mountPdRun(pdFull)
  await page.waitForSelector('.pd-sections > details')
  // The verdict paragraph is NOT on the report — the reader asked no question.
  if (await page.$('.pd-bottomline')) throw new Error('report rendered a verdict block')
  if ((await page.locator('.pd-group h2').count()) !== 0) {
    throw new Error('report still renders group headings instead of one flat accordion')
  }
  // The action bar, in order.
  for (const action of ['Refresh', 'Refresh all sources', 'Change address']) {
    await page.waitForSelector(`.pd-actionbar .pd-action:text-is("${action}")`)
  }
  if (await page.$('.pd-actionbar .pd-action:text-is("Hide details")')) {
    throw new Error('removed Hide details action returned')
  }
  // Parcel Info is first and open; nothing else is.
  const firstTitle = await page.locator('.pd-sections > details').first()
    .locator('.pd-card-title').first().textContent()
  if (firstTitle.trim() !== 'Parcel Info') throw new Error(`first section is ${firstTitle}`)
  if ((await page.locator('.pd-sections > details[open]').count()) !== 1) {
    throw new Error('more than one section open by default')
  }
  // A collapsed row carries its compact summary and its confidence badge.
  await page.waitForSelector('.pd-summary')
  await page.waitForSelector('.pd-badge-conf:has-text("verified public fact")')
  await page.waitForSelector('text=Mapped from the address you typed') // anchor note
  await page.waitForSelector('.pd-chip:has-text("Not available here")')
  await page.waitForSelector('.pd-chip:has-text("Needs a human look")')
  await page.waitForSelector('.pd-card-title:text-is("Manual Verification Required")')
  await page.waitForSelector('text=Checked Jan 12, 2026') // run header = oldest source
  // Composite card: each contributing source shows its OWN checked-on date.
  await openPdCard('Traffic & Roads')
  await page.waitForSelector('text=Traffic counts checked Jul 28, 2026')
  await page.waitForSelector('text=Traffic history checked Jan 12, 2026')
  // Narrative block sits above the deterministic facts on the parcel card.
  // Parcel Info is already open (it is the first section), so this must be
  // idempotent — clicking would CLOSE it.
  await openPdCard('Parcel Info')
  await page.waitForSelector('text=Why it matters')
  await page.waitForSelector('text=A clean, buildable-size parcel')
  // Cache-stale provenance: "from an older saved copy"
  await openPdCard('Flood')
  await page.waitForSelector('text=from an older saved copy')
  // Issue card carries its plain failure copy + a retry action.
  await openPdCard('TxDOT Projects')
  await page.waitForSelector("text=We couldn't get a full answer from Road projects.")
  await page.waitForSelector('button:has-text("Try this source again")')
  await page.screenshot({ path: `${SHOTS}/2_publicdata_report.png` })

  // ── cards_schema_version 2 blocks ────────────────────────────────────
  // Facts render as a label/value grid, tables/images/links render at all,
  // and advisories are the muted "↳" continuation lines. Cards opened earlier
  // in this run must stay open, so opening is idempotent.
  await openPdCard('Parcel Info')
  await page.waitForSelector('.pd-fact-label:text-is("Size")')
  await page.waitForSelector('.pd-fact-value:text-is("11.9 acres")')

  // Table: title, header row, a body cell — inside its OWN scroll box.
  await openPdCard('Traffic & Roads')
  await page.waitForSelector('.pd-table-title:text-is("Traffic counts by year")')
  await page.waitForSelector('.pd-table thead th:text-is("Vehicles a day")')
  await page.waitForSelector('.pd-table tbody td:text-is("18,500")')
  const tableScroll = await page.$eval('.pd-table-scroll', (el) => ({
    overflowX: getComputedStyle(el).overflowX,
    stickyHeader: getComputedStyle(el.querySelector('thead th')).position,
    fitsInPage: el.clientWidth <= document.documentElement.clientWidth,
  }))
  if (tableScroll.overflowX !== 'auto') throw new Error('table is not in its own scroll container')
  if (tableScroll.stickyHeader !== 'sticky') throw new Error('table header is not sticky')
  if (!tableScroll.fitsInPage) throw new Error('table container is wider than the viewport')
  await page.screenshot({ path: `${SHOTS}/2b_publicdata_table.png` })

  // Image: rendered (naturalWidth > 0), responsive, with caption + attribution.
  await openPdCard('Aerial photo')
  await page.waitForSelector('.pd-image img')
  // The image is loading="lazy" (a real report carries several) — bring it on
  // screen the way a reader would before asserting it actually decoded.
  await page.locator('.pd-image img').scrollIntoViewIfNeeded()
  await page.waitForFunction(() => {
    const el = document.querySelector('.pd-image img')
    return el && el.complete && el.naturalWidth > 0
  })
  const fig = await page.$eval('.pd-image img', (el) => ({
    loaded: el.complete && el.naturalWidth > 0,
    width: el.getBoundingClientRect().width,
    parentWidth: el.parentElement.getBoundingClientRect().width,
  }))
  if (!fig.loaded) throw new Error('card image did not load')
  if (fig.width > fig.parentWidth + 1) throw new Error('card image overflows its card')
  await page.waitForSelector('text=Aerial view, 1995 — the oldest photo we found')
  await page.waitForSelector('text=Texas Natural Resources Information System')
  await page.waitForSelector('.pd-table-title:text-is("What the photos show")')
  // Advisory renders as a muted "↳" line, not a warning box.
  await page.waitForSelector('.pd-advisory:has-text("screening aid")')
  await page.screenshot({ path: `${SHOTS}/2c_publicdata_image.png` })

  // Links: labelled, and they open in a new tab.
  await openPdCard('Parcel Info')
  const parcelLinks = await page
    .locator('details:has(.pd-card-title:text-is("Parcel Info")) .pd-links a')
    .evaluateAll((els) => els.map((a) => [a.textContent.trim(), a.getAttribute('target')]))
  if (!parcelLinks.some(([t]) => t.startsWith('County appraisal record'))) {
    throw new Error(`card links missing: ${JSON.stringify(parcelLinks)}`)
  }
  if (parcelLinks.some(([, target]) => target !== '_blank')) {
    throw new Error('card links do not open in a new tab')
  }

  // The page itself must never scroll sideways on a 390px phone.
  const pdOverflow = await page.evaluate(() => {
    const d = document.documentElement
    return { scrollWidth: d.scrollWidth, clientWidth: d.clientWidth }
  })
  if (pdOverflow.scrollWidth > pdOverflow.clientWidth + 1) {
    throw new Error(`report page scrolls sideways: ${JSON.stringify(pdOverflow)}`)
  }
  await unmountPdRun(pdFull)
  console.log('Public Data v2 blocks (fact grid, table, image, links, advisory): OK')
  console.log('Public Data report (flat accordion, action bar, summaries, badges): OK')

  // Deterministic-only payload (no AI): cards, no narrative.
  const pdNoAi = pdFix('no_ai')
  await mountPdRun(pdNoAi)
  await page.waitForSelector('.pd-sections > details')
  if (await page.$('text=Why it matters')) throw new Error('no-AI payload rendered a narrative block')
  await unmountPdRun(pdNoAi)

  // Cancelled with staged waves: fetched cards render, the rest say Not checked.
  const pdCancelPart = pdFix('cancelled_partial')
  await mountPdRun(pdCancelPart)
  await page.waitForSelector('text=This lookup was cancelled before finishing')
  await page.waitForSelector('text=Partly inside a mapped flood zone')
  await page.waitForSelector('.pd-chip:has-text("Not checked")')
  await unmountPdRun(pdCancelPart)

  // Zero-staged cancelled: the full scaffold, every card Not checked.
  const pdCancelZero = pdFix('cancelled_zero')
  await mountPdRun(pdCancelZero)
  await page.waitForSelector('text=This lookup was cancelled before it finished.')
  await page.waitForSelector('text=the lookup was cancelled before this source')
  if ((await page.locator('.pd-chip:has-text("Not checked")').count()) < 2) {
    throw new Error('zero-staged cancelled scaffold missing Not checked cards')
  }
  await unmountPdRun(pdCancelZero)

  // Failed with nothing staged: the plain failure only — NO cards.
  const pdFailed = pdFix('failed_nocards')
  await mountPdRun(pdFailed)
  await page.waitForSelector("text=We couldn't find this address")
  await page.waitForSelector('button:has-text("Run it again")')
  if ((await page.locator('.pd-sections').count()) !== 0) throw new Error('failed run rendered cards')
  await unmountPdRun(pdFailed)

  // Failed WITH staged waves: partial results stay readable.
  const pdFailedPart = pdFix('failed_partial')
  await mountPdRun(pdFailedPart)
  await page.waitForSelector('text=This lookup stopped before finishing')
  await page.waitForSelector('text=Outside the mapped flood zones')
  await page.waitForSelector('text=the lookup stopped before this source')
  await unmountPdRun(pdFailedPart)

  // Queued: waiting copy with a live indicator.
  const pdQueued = pdFix('queued')
  await mountPdRun(pdQueued)
  await page.waitForSelector('text=Waiting for its turn')
  await page.waitForSelector('.busy .spinner')
  await unmountPdRun(pdQueued)

  // Cooldown retry row: disabled countdown that re-arms itself at expiry,
  // then the retry request goes out and the busy indicator shows.
  const pdCooldown = pdFix('cooldown')
  await mountPdRun(pdCooldown)
  // Idempotent: this card may already be the open first section.
  await openPdCard('Environmental Screening')
  await page.locator('text=asked us to slow down').first().waitFor()
  await page.waitForSelector('button:has-text("Try again in")')
  await page.waitForSelector('button:has-text("Try this source again"):not([disabled])', {
    timeout: 8000,
  })
  let pdRetryBody = null
  await page.route(`**/api/site-check/runs/${pdCooldown.id}/retry-sources`, (route) => {
    pdRetryBody = route.request().postDataJSON()
    return route.fulfill({ status: 202, json: { job_id: 'retry-job-1' } })
  })
  await page.click('button:has-text("Try this source again")')
  await page.waitForSelector('text=Trying Environmental records again')
  if (!pdRetryBody?.source_ids?.includes('environmental_screening')) {
    throw new Error('retry did not post the source id')
  }
  await page.unroute(`**/api/site-check/runs/${pdCooldown.id}/retry-sources`)
  await unmountPdRun(pdCooldown)
  console.log('Public Data run states (cancelled/failed/queued/cooldown-retry): OK')

  // New lookup → progress: one address field, optional id field, 202 create,
  // then the progress screen with the step line and continuing line.
  const pdRunning = pdFix('running')
  await page.route(`**/api/site-check/runs/${pdRunning.id}`, (route) =>
    route.fulfill({ json: pdRunning }))
  let pdCreateBody = null
  await page.route('**/api/site-check', (route) => {
    pdCreateBody = route.request().postDataJSON()
    return route.fulfill({ status: 202, json: { run_id: pdRunning.id, job_id: 'j1', status: 'queued' } })
  })
  await page.goto(`${BASE}/public-data`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=Parcel or property ID — if you have it')
  await page.fill('input[placeholder="1100 Congress Ave, Austin"]', '500 E Anderson Ln, Austin')
  await page.click('button:has-text("Look it up")')
  await page.waitForSelector('text=Checking flood maps — 12 of 35 sources done')
  await page.waitForSelector("text=1 source couldn't answer — continuing with the rest.")
  await page.waitForSelector('.busy .spinner')
  await page.waitForSelector('button:has-text("Stop this lookup")')
  if (pdCreateBody?.address !== '500 E Anderson Ln, Austin' || pdCreateBody?.mode !== 'check') {
    throw new Error(`create body wrong: ${JSON.stringify(pdCreateBody)}`)
  }
  await page.screenshot({ path: `${SHOTS}/3_publicdata_progress.png` })
  await page.unroute('**/api/site-check')
  await page.unroute(`**/api/site-check/runs/${pdRunning.id}`)
  console.log('Public Data new lookup → progress screen: OK')

  // History: newest first, freshness badges from the API, search box, delete
  // with the plain confirm.
  const pdHist = pdFix('history')
  let pdHistDeleted = false
  const pdListMatch = (url) => url.pathname.endsWith('/api/site-check/runs')
  await page.route(pdListMatch, (route) =>
    route.fulfill({
      json: pdHistDeleted
        ? { ...pdHist, runs: pdHist.runs.slice(1), total: pdHist.total - 1 }
        : pdHist,
    }),
  )
  await page.route('**/api/site-check/runs/pd-h1', (route) => {
    if (route.request().method() === 'DELETE') {
      pdHistDeleted = true
      return route.fulfill({ status: 204, body: '' })
    }
    return route.continue()
  })
  await page.goto(`${BASE}/public-data/history`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('input[placeholder="Search addresses"]')
  await page.waitForSelector('.pd-badge:has-text("Up to date")')
  await page.waitForSelector('.pd-badge:has-text("Out of date")')
  await page.waitForSelector('.pd-badge:has-text("Cancelled")')
  await page.waitForSelector('text=worth a closer look') // headline takeaway
  let pdDialogText = ''
  page.once('dialog', (d) => {
    pdDialogText = d.message()
    d.accept()
  })
  await page
    .locator('.pd-row', { hasText: '500 E Anderson Ln' })
    .locator('button:has-text("Remove")')
    .click()
  await page.waitForSelector('text=500 E Anderson Ln', { state: 'detached' })
  if (!pdDialogText.includes('The public data itself is not affected')) {
    throw new Error(`delete confirm wording wrong: "${pdDialogText}"`)
  }
  await page.screenshot({ path: `${SHOTS}/4_publicdata_history.png` })
  await page.unroute(pdListMatch)
  await page.unroute('**/api/site-check/runs/pd-h1')
  console.log('Public Data history (badges, search, delete confirm): OK')

  // ── Settings screens: loading, empty and error states ─────────────────
  // Each of the 4 screens is driven through all three states with the API
  // route mocked: a slow response (loading), an empty-but-successful
  // response (empty), and a 500 (error).
  const settingsScreens = [
    {
      path: '/settings/needs', route: '**/api/settings/needs',
      empty: { rows: [] }, loadingText: 'Loading…',
      emptyText: 'Nothing to show yet.', errorText: 'Something went wrong',
    },
    {
      path: '/settings/keys', route: '**/api/settings/keys',
      empty: [], loadingText: 'Loading…',
      emptyText: 'Nothing to show yet.', errorText: 'Something went wrong',
    },
    {
      path: '/settings/spending', route: '**/api/settings/spending',
      empty: {
        month: '2026-01', spend_cents: 0, budget_cents: 2000, warn: false, blocked: false,
        public_data: { lookups: 0, source_calls: 0, estimated_paid_cost_cents: 0, by_source: [] },
      },
      loadingText: 'Loading…',
      emptyText: 'No public-data activity yet this month.', errorText: 'Something went wrong',
    },
    {
      path: '/settings/coverage', route: '**/api/settings/coverage',
      empty: {
        state: 'Texas', counties: [],
        cities: { zoning: [], standards: [], future_land_use: [] },
        statewide: [], nationwide: [],
      },
      loadingText: 'Loading…',
      emptyText: 'None yet.', errorText: 'Something went wrong',
    },
  ]
  for (const screen of settingsScreens) {
    // Loading: delay the response and catch the busy indicator mid-flight.
    await page.route(screen.route, async (route) => {
      await new Promise((r) => setTimeout(r, 1500))
      // The page may already have moved on (the dev build mounts twice), so a
      // late reply to an abandoned request is ignored.
      return route.fulfill({ json: screen.empty }).catch(() => {})
    })
    const loadingNav = page.goto(`${BASE}${screen.path}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`text=${screen.loadingText}`, { timeout: 5000 })
    await loadingNav
    await page.unroute(screen.route)

    // Empty: an immediate, successful, empty-shaped response.
    await page.route(screen.route, (route) => route.fulfill({ json: screen.empty }))
    await page.goto(`${BASE}${screen.path}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`text=${screen.emptyText}`, { timeout: 5000 })
    await page.unroute(screen.route)

    // Error: a server failure. The browser itself logs a console error for
    // the mocked 500 response (standard DevTools network noise, not a bug
    // in the app) — this block's own induced errors are expected and
    // trimmed back out once the error state is confirmed rendered.
    const errorsBefore = errors.length
    await page.route(screen.route, (route) =>
      route.fulfill({ status: 500, json: { error: 'Something went wrong — please try again.' } }))
    await page.goto(`${BASE}${screen.path}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`text=${screen.errorText}`, { timeout: 5000 })
    await page.unroute(screen.route)
    errors.length = errorsBefore
    console.log(`Settings ${screen.path} loading/empty/error states: OK`)
  }

  // ── Disclaimer: reachable from the top bar, says the key things ───────
  await page.goto(`${BASE}/public-data`, { waitUntil: 'domcontentloaded' })
  await page.click('header.topbar >> text=Disclaimer')
  await page.waitForSelector('h1:has-text("Disclaimer")')
  for (const phrase of ['open-source project', 'NoMoreHappyPath', 'no responsibility for bugs', 'at your own risk']) {
    if (!(await page.locator(`text=${phrase}`).count())) throw new Error(`Disclaimer is missing "${phrase}"`)
  }
  await page.screenshot({ path: `${SHOTS}/disclaimer.png` })
  console.log('Disclaimer page from the top bar: OK')

  if (errors.length) throw new Error(`console errors: ${errors.join(' | ')}`)
  console.log('UI SMOKE PASS — screenshots in', SHOTS)
} catch (e) {
  await page.screenshot({ path: `${SHOTS}/failure.png` }).catch(() => {})
  console.error('UI SMOKE FAIL:', e.message); console.error((e.stack || '').split('\n').filter(l => l.includes('ui_smoke')).join('\n'))
  process.exitCode = 1
} finally {
  await browser.close()
}
