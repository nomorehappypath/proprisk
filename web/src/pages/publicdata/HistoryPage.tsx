import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Busy from '../../components/Busy'
import { ApiError } from '../../lib/api'
import {
  deleteLookup,
  fmtCounty,
  fmtDayTime,
  FRESHNESS_LABEL,
  listLookups,
  type RunSummary,
} from '../../lib/publicData'

const GENERIC = 'Something went wrong — please try again.'

// Public Data — past reports (spec site_check.md §14). Newest first from the
// server; search + internal scroll per the house lists-scale rule; freshness
// badges come straight from the API's classification (§9.4) — the client
// never knows the day thresholds.
export default function HistoryPage() {
  const nav = useNavigate()
  const [rows, setRows] = useState<RunSummary[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const t = setTimeout(
      () => {
        listLookups(query, 1, 100)
          .then((b) => {
            setRows(b.runs)
            setTotal(b.total)
            setLoaded(true)
          })
          .catch((e) => {
            setError(e instanceof ApiError ? e.message : GENERIC)
            setLoaded(true)
          })
      },
      query ? 250 : 0, // debounce typing; first load is immediate
    )
    return () => clearTimeout(t)
  }, [query])

  async function remove(r: RunSummary) {
    if (!window.confirm('Remove this report? The public data itself is not affected.')) return
    setError('')
    try {
      await deleteLookup(r.id)
      setRows((cur) => cur.filter((x) => x.id !== r.id))
      setTotal((t) => Math.max(0, t - 1))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : GENERIC)
    }
  }

  return (
    <main className="page">
      <h1>Past reports</h1>
      <button onClick={() => nav('/public-data')}>+ New lookup</button>
      <input
        placeholder="Search addresses"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="pd-search"
      />
      {error && <p className="error">{error}</p>}
      {!loaded ? (
        <Busy label="Loading your reports…" />
      ) : rows.length === 0 ? (
        <p className="muted-line">
          {query ? 'No reports match your search.' : 'No reports yet — run your first lookup.'}
        </p>
      ) : (
        <>
          <p className="muted-line list-count">
            <span>
              Showing {rows.length} of {total}
            </span>
          </p>
          <div className="scroll-window">
            <ul className="list">
              {rows.map((r) => (
                <li
                  key={r.id}
                  className="card pd-row"
                  onClick={() => nav(`/public-data/runs/${r.id}`)}
                >
                  <div className="pd-row-top">
                    <strong>{r.address}</strong>
                    <StatusBadge r={r} />
                  </div>
                  {r.headline_takeaway && <p className="pd-takeaway">{r.headline_takeaway}</p>}
                  <p className="muted-line pd-tight">
                    {fmtDayTime(r.created_at)}
                    {r.county ? ` · ${fmtCounty(r.county)} County` : ''}
                    {r.parcel_id ? ` · ${r.parcel_id}` : ''}
                  </p>
                  {(r.status === 'completed' || r.status === 'failed' || r.status === 'cancelled') && (
                    <div className="row-actions">
                      <button
                        className="linklike danger"
                        onClick={(e) => {
                          e.stopPropagation()
                          remove(r)
                        }}
                      >
                        Remove
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </>
      )}
    </main>
  )
}

function StatusBadge({ r }: { r: RunSummary }) {
  if (r.status === 'completed' && r.freshness) {
    return <span className={`pd-badge pd-badge-${r.freshness}`}>{FRESHNESS_LABEL[r.freshness]}</span>
  }
  const label =
    r.status === 'queued'
      ? 'Waiting to start'
      : r.status === 'running'
        ? 'In progress'
        : r.status === 'cancelled'
          ? 'Cancelled'
          : r.status === 'failed'
            ? 'Stopped'
            : null
  if (!label) return null
  const cls = r.status === 'failed' ? 'pd-badge pd-badge-stopped' : 'pd-badge pd-badge-neutral'
  return <span className={cls}>{label}</span>
}
