import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import Busy from '../../components/Busy'
import EraGallery from './EraGallery'
import PropertyMapPanel from './PropertyMapPanel'
import { ApiError } from '../../lib/api'
import {
  brokerSafe,
  cancelLookup,
  createLookup,
  deleteLookup,
  failureMessage,
  fmtCounty,
  fmtDay,
  getLookup,
  retryLookupSources,
  type Card,
  type CardImage,
  type CardLink,
  type CardNarrative,
  type CardTable,
  type FactRow,
  type RetryRow,
  type RunDetail,
  type RunStatus,
  type SourceProvenance,
} from '../../lib/publicData'

const POLL_MS = 3000
// A source retry keeps the run `completed` (§8.4); we watch the retry job via
// the same detail poll. The authoritative signal is `retry_active`, which the
// server derives from the JOB ROW (§8.3) — we no longer infer liveness from
// `progress` JSON, which could stay running-looking after the job finished and
// kept this watcher spinning to its cap. Grace covers only the first poll
// after the click (the request may not have been admitted yet); the cap is a
// last-resort backstop if polling itself fails.
const RETRY_GRACE_MS = 6000
const RETRY_CAP_MS = 180000

const GENERIC = 'Something went wrong — please try again.'

export default function RunPage() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const [run, setRun] = useState<RunDetail | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [cancelling, setCancelling] = useState(false)
  const [starting, setStarting] = useState(false) // "Refresh all data" / "Run it again"
  const [retrying, setRetrying] = useState(false)
  const retryStartedAt = useRef(0)

  const load = useCallback(() => getLookup(id).then((r) => setRun(r)), [id])

  useEffect(() => {
    setRun(null)
    setError('')
    setNotice('')
    setStarting(false)
    setRetrying(false)
    load().catch((e) => setError(e instanceof ApiError ? e.message : GENERIC))
  }, [id, load])

  // Settle the retry watcher on the server's job-derived flag (§8.3).
  useEffect(() => {
    if (!retrying || !run) return
    const elapsed = Date.now() - retryStartedAt.current
    if (elapsed > RETRY_CAP_MS || (elapsed > RETRY_GRACE_MS && !run.retry_active)) {
      setRetrying(false)
    }
  }, [run, retrying])

  const active = run !== null && (run.status === 'queued' || run.status === 'running')
  useEffect(() => {
    if (!active && !retrying) return
    const t = setInterval(() => {
      load().catch(() => {}) // a blip never kills the poll; the next tick retries
    }, POLL_MS)
    return () => clearInterval(t)
  }, [active, retrying, load])

  async function onCancel() {
    setCancelling(true)
    setError('')
    try {
      await cancelLookup(id)
      await load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice(e.message) // e.g. it already finished — show the truth
        await load().catch(() => {})
      } else {
        setError(e instanceof ApiError ? e.message : GENERIC)
      }
    } finally {
      setCancelling(false)
    }
  }

  async function startLookup(mode: 'check' | 'fresh') {
    if (!run) return
    setStarting(true)
    setError('')
    try {
      const r = await createLookup(run.address, run.parcel_id, mode)
      nav(`/public-data/runs/${r.run_id}`)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : GENERIC)
      setStarting(false)
    }
  }

  async function onRetry(sourceIds: string[]) {
    setError('')
    setNotice('')
    try {
      await retryLookupSources(id, sourceIds)
      retryStartedAt.current = Date.now()
      setRetrying(true)
    } catch (e) {
      if (e instanceof ApiError) setNotice(e.message) // cooldown/409s carry plain wording
      else setError(GENERIC)
    }
  }

  if (!run) {
    return (
      <main className="page">
        <h1>Public Data</h1>
        {error ? <p className="error">{error}</p> : <Busy label="Loading the report…" />}
      </main>
    )
  }

  if (run.status === 'queued' || run.status === 'running') {
    return (
      <ProgressView run={run} cancelling={cancelling} onCancel={onCancel} error={error} notice={notice} />
    )
  }

  if (!run.cards) {
    // A failed run with nothing staged shows the plain failure only (§10.5).
    return (
      <main className="page">
        <h1>Public Data</h1>
        <p className="pd-address">
          {run.address}
          {run.parcel_id ? ` · ${run.parcel_id}` : ''}
        </p>
        <div className="card stack">
          <p className="error">{failureMessage(run)}</p>
          {starting ? (
            <Busy label="Starting the lookup…" />
          ) : (
            <>
              <button onClick={() => startLookup('check')}>Run it again</button>
              <button className="linklike danger" onClick={async () => {
                if (!window.confirm('Remove this report? The public data itself is not affected.')) return
                await deleteLookup(run.id)
                nav('/public-data')
              }}>Remove</button>
            </>
          )}
          {error && <p className="error">{error}</p>}
        </div>
        <Link to="/public-data/history" className="linklike">
          Past reports →
        </Link>
      </main>
    )
  }

  return (
    <ReportView
      run={run}
      retrying={retrying}
      starting={starting}
      error={error}
      notice={notice}
      onRetry={onRetry}
      onRefresh={() => startLookup('check')}
      onRefreshAll={() => startLookup('fresh')}
    />
  )
}

function ProgressView({
  run,
  cancelling,
  onCancel,
  error,
  notice,
}: {
  run: RunDetail
  cancelling: boolean
  onCancel: () => void
  error: string
  notice: string
}) {
  const p = run.progress
  const pct = p && p.total_sources > 0 ? Math.round((p.completed_sources / p.total_sources) * 100) : 0
  // §14: the elapsed counter shows the run's TRUE elapsed time — seeded once
  // from the server's elapsed_seconds so a reloaded page doesn't restart at 0.
  const sinceRef = useRef<number | null>(null)
  if (sinceRef.current === null) {
    sinceRef.current = Date.now() - (p?.elapsed_seconds ?? 0) * 1000
  }
  return (
    <main className="page">
      <h1>Public Data</h1>
      <p className="pd-address">
        {run.address}
        {run.parcel_id ? ` · ${run.parcel_id}` : ''}
      </p>
      <div className="card stack">
        <Busy
          since={sinceRef.current}
          label={
            run.status === 'queued'
              ? 'Waiting for its turn — the lookup starts in a moment…'
              : 'Looking through public records…'
          }
        />
        {p && p.total_sources > 0 && (
          <>
            <div className="meter">
              <div className="meter-fill" style={{ width: `${pct}%` }} />
            </div>
            <p className="muted-line">
              {p.completed_sources} of {p.total_sources} sources done
            </p>
          </>
        )}
        {p?.message && <p>{p.message}</p>}
        {p && p.failed_sources_count > 0 && (
          <p className="muted-line">
            {p.failed_sources_count} {p.failed_sources_count === 1 ? 'source' : 'sources'} couldn't
            answer — continuing with the rest.
          </p>
        )}
        <p className="muted-line">
          This usually takes a few minutes. You can leave this screen — the lookup keeps going and
          lands in your past reports.
        </p>
        {cancelling ? (
          <Busy label="Stopping…" />
        ) : (
          <button className="secondary" onClick={onCancel}>
            Stop this lookup
          </button>
        )}
      </div>
      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}
    </main>
  )
}

function ReportView({
  run,
  retrying,
  starting,
  error,
  notice,
  onRetry,
  onRefresh,
  onRefreshAll,
}: {
  run: RunDetail
  retrying: boolean
  starting: boolean
  error: string
  notice: string
  onRetry: (sourceIds: string[]) => void
  onRefresh: () => void
  onRefreshAll: () => void
}) {
  const nav = useNavigate()
  const cards = run.cards!
  const anyStaged = cards.groups.some((g) => g.cards.some((c) => c.status !== 'not_checked'))
  const retryRows = new Map(run.retryable_failed_sources.map((r) => [r.source_id, r]))
  const oldest = cards.bottom_line.checked_at
  // ONE flat, ordered accordion — the pinned the reference app report screen. The
  // server sets each section's place (`order`); groups survive in the payload
  // for other consumers but are not a heading on this screen.
  const sections = cards.groups
    .flatMap((g) => g.cards)
    .sort((a, b) => (a.order ?? 999) - (b.order ?? 999))

  // §10.5 headers: cancelled/stopped runs say so plainly, up top.
  const stateHeader =
    run.status === 'cancelled'
      ? anyStaged
        ? 'This lookup was cancelled before finishing — here is what came back first.'
        : 'This lookup was cancelled before it finished.'
      : run.status === 'failed'
        ? 'This lookup stopped before finishing — here is what came back first.'
        : null

  return (
    // pd-report only widens the column on a real desktop window (see index.css)
    // — the phone layout is untouched.
    <main className="page pd-report">
      <h1>Public Data</h1>
      <p className="pd-address">
        {run.address}
        {run.parcel_id ? ` · ${run.parcel_id}` : ''}
      </p>
      {run.county && <p className="muted-line">{fmtCounty(run.county)} County</p>}
      {oldest && (
        <p className="muted-line">
          Checked {fmtDay(oldest)}
          {run.refresh_after ? ` · facts refresh after ${fmtDay(run.refresh_after)}` : ''}
        </p>
      )}
      {run.freshness === 'expired' && (
        <p className="notice">
          {run.freshness_days
            ? `These facts are older than ${run.freshness_days} days — refresh for current data.`
            : 'These facts are past their refresh date — refresh for current data.'}
        </p>
      )}
      {stateHeader && <p className="notice">{stateHeader}</p>}
      {run.status === 'failed' && <p className="error">{failureMessage(run)}</p>}
      {cards.anchor_note && <p className="notice">{cards.anchor_note}</p>}
      {/* No verdict paragraph here. The report answers questions the reader
          asked of the public record; `bottom_line` is a one-line headline for
          the history list, not an opinion the report opens with. The server
          still emits it — this screen simply does not render it. */}
      {/* Action bar — the report's own controls, in one row above the
          sections (ported from the the reference app report header). */}
      <div className="pd-actionbar">
        <Link className="pd-action" to="/public-data#properties">
          Back to properties
        </Link>
        <button
          type="button"
          className="pd-action danger"
          onClick={async () => {
            if (!window.confirm('Remove this report? The public data itself is not affected.')) return
            await deleteLookup(run.id)
            nav('/public-data')
          }}
        >
          Remove
        </button>
        {starting ? (
          <Busy label="Starting the lookup…" />
        ) : (
          <>
            <button type="button" className="pd-action" onClick={onRefresh}>
              Refresh
            </button>
            <button type="button" className="pd-action" onClick={onRefreshAll}>
              Refresh all sources
            </button>
            <Link className="pd-action" to="/public-data">
              Change address
            </Link>
          </>
        )}
      </div>
      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}
      <div className="pd-sections">
          {sections.map((c, index) => (
            <ReportCard
              key={c.card_key}
              card={c}
              runId={run.id}
              // Parcel Info is first and opens by default — it carries the map.
              defaultOpen={index === 0}
              withMap={c.card_key === 'parcel' && anyStaged}
              runStatus={run.status}
              retryRows={retryRows}
              retrying={retrying}
              onRetry={onRetry}
            />
          ))}
          {cards.manual_checks.length > 0 && (
            <details className="card pd-card">
              <summary>
                <div className="pd-card-head">
                  <span className="pd-card-icon" aria-hidden>
                    📋
                  </span>
                  <span className="pd-card-title">Manual Verification Required</span>
                  <span className="pd-summary">
                    {cards.manual_checks.length}{' '}
                    {cards.manual_checks.length === 1 ? 'action' : 'actions'}
                  </span>
                  <span className="pd-badge-conf">manual verification required</span>
                </div>
              </summary>
              <div className="stack pd-card-body">
                {cards.manual_checks.map((m, i) => (
                  <div key={i}>
                    <strong>{m.label}</strong>
                    <p className="muted-line pd-tight">{m.reason}</p>
                    {m.link && (
                      <a className="linklike" href={m.link} target="_blank" rel="noreferrer">
                        Where to look →
                      </a>
                    )}
                  </div>
                ))}
              </div>
            </details>
          )}
      </div>
      <div className="stack pd-actions">
        <Link to="/public-data/history" className="linklike">
          Past reports →
        </Link>
      </div>
    </main>
  )
}

// §10.4 status chips — plain English, no engineer-speak.
const CHIP: Record<Card['status'], string | null> = {
  ok: null,
  issue: 'Partial answer',
  not_covered: 'Not available here',
  manual: 'Needs a human look',
  not_checked: 'Not checked',
}

function notCheckedLine(runStatus: RunStatus): string {
  return runStatus === 'failed'
    ? 'Not checked — the lookup stopped before this source.'
    : 'Not checked — the lookup was cancelled before this source.'
}

function ReportCard({
  card,
  runId,
  defaultOpen = false,
  withMap = false,
  runStatus,
  retryRows,
  retrying,
  onRetry,
}: {
  card: Card
  runId: string
  defaultOpen?: boolean
  withMap?: boolean
  runStatus: RunStatus
  retryRows: Map<string, RetryRow>
  retrying: boolean
  onRetry: (sourceIds: string[]) => void
}) {
  const chip = CHIP[card.status]
  // Retry rides on the card it belongs to (§10.4): match this card's sources
  // against the run's retryable rows.
  const cardRetryRows = card.sources
    .map((s) => retryRows.get(s.source_id))
    .filter((r): r is RetryRow => r !== undefined)
  // §10.1: the UI renders, never composes. The server takeaway already names
  // the failing source on issue cards — no client-built issue sentence.
  const statusLine = card.status === 'not_checked' ? notCheckedLine(runStatus) : null
  // Text already on the card (takeaway/advisories) must not repeat inside the
  // retry row below it.
  const shownTexts = [card.takeaway, ...card.advisories].map((t) => t.trim())
  // §10.1 v2 blocks. Everything is optional and defensively filtered — a v1
  // payload has none of it, and a malformed block never breaks the card.
  const tables = (card.tables ?? []).filter(
    (t) => t && t.columns?.length > 0 && t.rows?.length > 0,
  )
  const image = card.image?.url ? card.image : null
  const links = (card.links ?? []).filter((l) => l && l.url && l.label)
  // Composite cards attribute each fact row to a source. Show that name only
  // when the FACTS themselves span more than one source — on a card whose rows
  // all came from the same place it would just repeat the provenance line.
  const factSourceIds = new Set(
    card.facts.map((f) => f.source_id).filter((s): s is string => !!s),
  )
  const sourceNames =
    factSourceIds.size > 1 ? new Map(card.sources.map((s) => [s.source_id, s.source_name])) : null

  // The collapsed row IS the summary — glyph, name, one compact line, badge.
  // Everything below only appears once the reader opens the section.
  return (
    <details className="card pd-card" open={defaultOpen}>
      <summary>
        <div className="pd-card-head">
          <span className="pd-card-icon" aria-hidden>
            {card.icon || '•'}
          </span>
          <span className="pd-card-title">{card.title}</span>
          {card.summary && <span className="pd-summary">{card.summary}</span>}
          {chip && <span className={`pd-chip pd-chip-${card.status}`}>{chip}</span>}
          {card.confidence && <span className="pd-badge-conf">{card.confidence}</span>}
        </div>
      </summary>
      <div className="stack pd-card-body">
        {card.hero && <p className="pd-hero">{card.hero}</p>}
        <p className="pd-takeaway">{card.takeaway}</p>
        {/* The parcel section carries the map, exactly as the reference app does — the
            picture the whole report was screened against, not a side trip. */}
        {withMap && <PropertyMapPanel runId={runId} />}
        {/* The manual takeaway already opens with "This one needs a human
            look" — repeating it here showed the phrase twice per card. */}
        {statusLine && statusLine !== card.takeaway && <p className="muted-line">{statusLine}</p>}
        {card.narrative && <Narrative n={card.narrative} />}
        {image && <CardFigure img={image} />}
        {(card.images?.length ?? 0) > 0 && <EraGallery images={card.images!} />}
        {card.facts.length > 0 && <FactGrid facts={card.facts} sourceNames={sourceNames} />}
        {tables.map((t, i) => (
          <FactTable key={i} table={t} />
        ))}
        {links.length > 0 && <LinkList links={links} />}
        {card.advisories.map((a, i) => (
          <p key={i} className="pd-advisory">
            {a}
          </p>
        ))}
        <Provenance sources={card.sources} />
        {cardRetryRows.map((row) => (
          <RetryControl
            key={row.source_id}
            row={row}
            retrying={retrying}
            onRetry={onRetry}
            alreadyShown={shownTexts}
          />
        ))}
      </div>
    </details>
  )
}

// Fact grid (§10.1) — one boxed label/value cell per row. Single column on a
// phone, two columns once there is room; the hint is a muted sub-line under
// the value, never a second column that would squeeze the value.
function FactGrid({
  facts,
  sourceNames,
}: {
  facts: FactRow[]
  sourceNames: Map<string, string> | null
}) {
  return (
    <dl className="pd-facts">
      {facts.map((f, i) => {
        const from = f.source_id ? sourceNames?.get(f.source_id) : undefined
        return (
          <div key={i} className="pd-fact">
            <dt className="pd-fact-label">{f.label}</dt>
            <dd className="pd-fact-value">{f.value}</dd>
            {f.hint && <dd className="pd-fact-hint">{f.hint}</dd>}
            {from && <dd className="pd-fact-hint">From {from}</dd>}
          </div>
        )
      })}
    </dl>
  )
}

// Table block (§10.1) — titled, sticky header, and its OWN horizontal scroll
// box so a wide table never makes the page itself scroll sideways.
function FactTable({ table }: { table: CardTable }) {
  const cols = table.columns.length
  return (
    <div className="pd-table-block">
      {table.title && <p className="pd-table-title">{table.title}</p>}
      <div className="pd-table-scroll">
        <table className="pd-table">
          <thead>
            <tr>
              {table.columns.map((c, i) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, ri) => (
              <tr key={ri}>
                {/* Pad a short row so the columns stay aligned. */}
                {Array.from({ length: cols }, (_, ci) => (
                  <td key={ci}>{row[ci] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Image block (§10.1) — responsive, with the caption and the attribution the
// source requires. Never crops: a map tile or aerial must stay readable.
function CardFigure({ img }: { img: CardImage }) {
  return (
    <figure className="pd-image">
      <img src={img.url} alt={img.caption || ''} loading="lazy" />
      {img.caption && <figcaption className="pd-image-caption">{img.caption}</figcaption>}
      {img.attribution && <p className="pd-image-attr">{img.attribution}</p>}
    </figure>
  )
}

// Source links (§10.1) — labelled, open in a new tab so the report stays put.
function LinkList({ links }: { links: CardLink[] }) {
  return (
    <ul className="pd-links">
      {links.map((l, i) => (
        <li key={i}>
          <a href={l.url} target="_blank" rel="noreferrer">
            {l.label} →
          </a>
        </li>
      ))}
    </ul>
  )
}

// Narrative overlay (§10.6) — visually distinct, ABOVE the deterministic facts.
function Narrative({ n }: { n: CardNarrative }) {
  return (
    <div className="pd-narrative">
      {n.headline && <p className="pd-narrative-headline">{n.headline}</p>}
      {n.broker_read && <p>{n.broker_read}</p>}
      {n.why_it_matters && (
        <p>
          <strong>Why it matters:</strong> {n.why_it_matters}
        </p>
      )}
      {n.next_step && (
        <p>
          <strong>Next step:</strong> {n.next_step}
        </p>
      )}
      {n.caveat && <p className="muted-line pd-tight">{n.caveat}</p>}
    </div>
  )
}

// Provenance (§10.1): one date line when every contributor agrees, a line per
// source when they differ — each fact always shows when it was checked.
function Provenance({ sources }: { sources: SourceProvenance[] }) {
  const dated = sources.filter((s) => s.checked_at)
  if (dated.length === 0) return null
  const days = new Set(dated.map((s) => fmtDay(s.checked_at)))
  const sameCache = new Set(dated.map((s) => s.from_cache)).size === 1
  if (days.size === 1 && sameCache) {
    return (
      <p className="muted-line pd-provenance">
        Checked {fmtDay(dated[0].checked_at)}
        {dated[0].from_cache ? ' · from an older saved copy' : ''} ·{' '}
        {dated.map((s, i) => (
          <span key={s.source_id}>
            {i > 0 && ', '}
            <SourceName s={s} />
          </span>
        ))}
      </p>
    )
  }
  return (
    <div className="pd-provenance">
      {dated.map((s) => (
        <p key={s.source_id} className="muted-line pd-tight">
          <SourceName s={s} /> checked {fmtDay(s.checked_at)}
          {s.from_cache ? ' · from an older saved copy' : ''}
        </p>
      ))}
    </div>
  )
}

function SourceName({ s }: { s: SourceProvenance }) {
  if (!s.source_url) return <>{s.source_name}</>
  return (
    <a href={s.source_url} target="_blank" rel="noreferrer">
      {s.source_name}
    </a>
  )
}

// Per-source retry with the ported cooldown behavior (§8.4): while cooling the
// button is disabled with a countdown and re-arms itself the moment the
// window passes — no reload needed.
function RetryControl({
  row,
  retrying,
  onRetry,
  alreadyShown = [],
}: {
  row: RetryRow
  retrying: boolean
  onRetry: (sourceIds: string[]) => void
  alreadyShown?: string[]
}) {
  const deadline = useMemo(() => {
    const until = row.retry_disabled_until ? Date.parse(row.retry_disabled_until) : NaN
    if (!Number.isNaN(until) && until > Date.now()) return until
    if (row.retry_status === 'cooldown' && row.retry_after_seconds && row.retry_after_seconds > 0) {
      return Date.now() + row.retry_after_seconds * 1000
    }
    return 0
    // Re-arm only when the row itself changes, not on every poll re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.source_id, row.last_failed_at, row.retry_disabled_until, row.retry_status])
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!deadline || deadline <= Date.now()) return
    const t = setInterval(() => {
      setNow(Date.now())
      if (Date.now() >= deadline) clearInterval(t)
    }, 1000)
    return () => clearInterval(t)
  }, [deadline])

  const cooling = deadline > now
  const secondsLeft = Math.max(0, Math.ceil((deadline - now) / 1000))
  const rawDetail = brokerSafe(row.error_detail || row.cause)
  const detail = alreadyShown.includes(rawDetail.trim()) ? '' : rawDetail

  return (
    <div className="pd-retry stack">
      {detail && <p className="muted-line pd-tight">{detail}</p>}
      {row.retry_message && <p className="muted-line pd-tight">{brokerSafe(row.retry_message)}</p>}
      {retrying ? (
        <Busy label={`Trying ${row.source_label} again…`} />
      ) : cooling ? (
        <button className="secondary" disabled>
          Try again in {fmtCountdown(secondsLeft)}
        </button>
      ) : (
        <button className="secondary" onClick={() => onRetry([row.source_id])}>
          Try this source again
        </button>
      )}
    </div>
  )
}

function fmtCountdown(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}
