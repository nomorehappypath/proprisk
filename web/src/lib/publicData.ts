// Public Data (internal codename: site-check, spec site_check.md) — typed
// client for the §8 API envelopes and the §10 CardsPayload DTO (v1).
// The server BUILDS every card and every string of substance; this layer
// types them and the pages render them — the UI never composes facts.
// All user-visible dates render in the office's local time.
import { api } from './api'

export type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
// Internal API values (§8.1) — never shown on screen.
export type RunMode = 'check' | 'fresh'
export type Freshness = 'current' | 'expiring' | 'expired'
export type CardStatus = 'ok' | 'issue' | 'not_covered' | 'manual' | 'not_checked'

// ── §10.1 DTO ───────────────────────────────────────────────────────────

export interface SourceProvenance {
  source_id: string
  source_name: string
  source_url?: string | null
  checked_at: string | null
  from_cache: boolean
}

export interface FactRow {
  label: string
  value: string
  hint?: string | null
  // Composite cards attribute each fact row to the source it came from.
  source_id?: string | null
}

// ── §10.1 enriched blocks (cards_schema_version 2) ──────────────────────
// A v1 payload simply omits these, so every field is optional and the
// renderer skips whatever is absent — an older saved run still renders.

export interface CardTable {
  title: string
  columns: string[]
  rows: string[][]
}

export interface CardImage {
  url: string
  caption: string
  attribution: string
}

// cards_schema_version 3 — an ORDERED gallery, distinct from the singular
// `image` (one hero picture). Today's only producer is the historical-aerials
// card: one frame per photo year, oldest first, `label` carrying the year.
export interface CardGalleryImage extends CardImage {
  label: string
}

export interface CardLink {
  label: string
  url: string
}

export interface CardNarrative {
  headline: string
  broker_read: string
  why_it_matters: string
  next_step: string
  caveat: string
}

export interface Card {
  card_key: string
  title: string
  status: CardStatus
  // v4 accordion identity — the collapsed row is glyph + title + summary +
  // badge. The server composes all four; the client only draws them.
  icon?: string | null
  order?: number | null
  summary?: string | null
  // The section's opening line (the panel's HeroLine) — only the sections
  // that have one emit it.
  hero?: string | null
  confidence?: string | null
  takeaway: string // deterministic, always present
  facts: FactRow[]
  // v2 blocks — absent on a v1 payload (see CardTable/CardImage/CardLink).
  tables?: CardTable[] | null
  image?: CardImage | null
  // v3 block — a v1/v2 payload omits it entirely.
  images?: CardGalleryImage[] | null
  links?: CardLink[] | null
  advisories: string[]
  sources: SourceProvenance[] // ≥1; each contributor with its OWN checked_at
  checked_at: string | null // rollup: oldest successful contributor
  narrative?: CardNarrative | null
  retryable: boolean
}

export interface CardGroup {
  key: string
  title: string
  cards: Card[]
}

export interface BottomLine {
  text: string
  kind: 'deterministic' | 'ai'
  checked_at: string | null
}

export interface ManualLookup {
  label: string
  reason: string
  link?: string | null
}

export interface CardsPayload {
  groups: CardGroup[]
  bottom_line: BottomLine
  manual_checks: ManualLookup[]
  anchor_note?: string | null
}

// ── §8 envelopes ────────────────────────────────────────────────────────

export interface Progress {
  stage: string
  status: string
  message: string | null
  completed_sources: number
  total_sources: number
  failed_sources_count: number
  elapsed_seconds: number
  updated_at: string
}

export interface RetryRow {
  source_id: string
  source_label: string
  cause: string
  error_detail: string
  last_failed_at: string | null
  retry_status?: 'cooldown' | null
  retry_disabled_until?: string | null
  retry_after_seconds?: number | null
  retry_message?: string | null
}

export interface RunSummary {
  id: string
  address: string
  parcel_id: string | null
  county: string | null
  status: RunStatus
  error_code: string | null
  created_at: string
  finished_at: string | null
  headline_takeaway: string | null
  oldest_checked_at: string | null
  freshness: Freshness | null
}

export interface RunListPage {
  runs: RunSummary[]
  total: number
  page: number
  page_size: number
}

export interface RunDetail {
  id: string
  address: string
  parcel_id: string | null
  county: string | null
  lat: number | null
  lng: number | null
  anchor_kind: 'geocode' | 'parcel' | null
  anchor_quality: 'provisional' | 'verified' | null
  status: RunStatus
  error_code: string | null
  failure_reason: string | null
  mode: RunMode
  created_at: string
  started_at: string | null
  finished_at: string | null
  progress: Progress | null
  // True only while a retry job for this run is non-terminal. Derived
  // server-side FROM THE JOB ROW, never from `progress` JSON — a
  // terminal job used to leave stale running-looking progress behind,
  // which kept this client polling a finished refresh (spec §8.3).
  retry_active: boolean
  cards: CardsPayload | null
  retryable_failed_sources: RetryRow[]
  cards_schema_version: number
  // §9.4 header/notice values. Thresholds live server-side
  // (site_check_freshness_days) — the client NEVER hard-codes a day count,
  // so these render only when the API provides them.
  freshness?: Freshness | null
  refresh_after?: string | null
  freshness_days?: number | null
}

// ── Calls (§8.1–§8.5) ───────────────────────────────────────────────────

export function createLookup(address: string, parcelId: string | null, mode: RunMode = 'check') {
  return api.post<{ run_id: string; job_id: string; status: RunStatus }>('/site-check', {
    address,
    ...(parcelId ? { parcel_id: parcelId } : {}),
    mode,
  })
}

export function listLookups(query: string, page = 1, pageSize = 100) {
  const q = new URLSearchParams({ query, page: String(page), page_size: String(pageSize) })
  return api.get<RunListPage>(`/site-check/runs?${q.toString()}`)
}

export const getLookup = (id: string) => api.get<RunDetail>(`/site-check/runs/${id}`)

export const cancelLookup = (id: string) => api.post<unknown>(`/site-check/runs/${id}/cancel`)

export function retryLookupSources(id: string, sourceIds?: string[]) {
  return api.post<{ job_id: string }>(
    `/site-check/runs/${id}/retry-sources`,
    sourceIds && sourceIds.length ? { source_ids: sourceIds } : {},
  )
}

export const deleteLookup = (id: string) => api.del<void>(`/site-check/runs/${id}`)

// ── Rendering helpers ───────────────────────────────────────────────────

export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export function fmtDayTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

// The API's county is a lowercase registry key ("travis", "hays"); render it
// title-cased so the screen says "Travis County", never "travis County".
export function fmtCounty(county: string | null | undefined): string {
  if (!county) return ''
  return county
    .split(/[\s_]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

// Badge labels for the API's freshness classification (§9.4). The API
// decides which band a report is in — nothing here knows the day counts.
export const FRESHNESS_LABEL: Record<Freshness, string> = {
  current: 'Up to date',
  expiring: 'Refresh soon',
  expired: 'Out of date',
}

// Plain failure line for a run that stopped (§8.1/§7.4 wording). The
// server's failure_reason is authoritative; the map only backstops it.
const FAILURE_FALLBACK: Record<string, string> = {
  geocode_failed: "We couldn't find this address — check the spelling or add the city.",
  worker_lost: 'The lookup stopped unexpectedly. Run it again.',
  inconsistent_worker_state: 'The lookup stopped unexpectedly. Run it again.',
  superseded_by_new_check: 'A newer lookup for this address replaced this one.',
}

export function failureMessage(run: { error_code: string | null; failure_reason: string | null }): string {
  if (run.failure_reason) return run.failure_reason
  if (run.error_code && FAILURE_FALLBACK[run.error_code]) return FAILURE_FALLBACK[run.error_code]
  return 'This lookup stopped with a problem. Run it again.'
}

// Client-side re-suppression (§8.3): the server already rewrites failure
// text broker-safe, but a raw provider URL or stack line must never render
// even if one slips through.
export function brokerSafe(text: string | null | undefined): string {
  if (!text) return ''
  return text
    .replace(/https?:\/\/\S+/gi, '')
    .replace(/traceback[\s\S]*/i, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}
