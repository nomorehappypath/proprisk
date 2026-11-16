import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import Busy from '../../components/Busy'
import { ApiError } from '../../lib/api'
import { createLookup, fmtCounty, listLookups, type RunSummary } from '../../lib/publicData'
import { getKeys } from '../../lib/settings'

function propertyKey(run: RunSummary): string {
  const county = (run.county ?? '').trim().toLowerCase()
  const parcel = (run.parcel_id ?? '').trim().toUpperCase()
  if (parcel) return `${county}|parcel:${parcel}`
  return `${county}|address:${run.address.trim().toLowerCase().replace(/\s+/g, ' ')}`
}

function newestPropertyRuns(runs: RunSummary[]): RunSummary[] {
  const groups = new Map<string, RunSummary[]>()
  for (const run of runs) {
    const key = propertyKey(run)
    groups.set(key, [...(groups.get(key) ?? []), run])
  }
  return [...groups.values()].map((group) => {
    const newestFirst = [...group].sort((a, b) => b.created_at.localeCompare(a.created_at))
    return newestFirst.find((run) => run.status === 'queued' || run.status === 'running')
      ?? newestFirst.find((run) => run.status === 'completed')
      ?? newestFirst[0]
  })
}

// Public Data — new lookup (spec site_check.md §14). One address field, an
// optional parcel/property id, one button. Validation is STRUCTURAL only
// (presence/length, §8.1) — the server geocodes in the background, so an
// unfindable address becomes a plainly-worded failed run, never a form error.
export default function NewLookupPage() {
  const nav = useNavigate()
  const [address, setAddress] = useState('')
  const [parcel, setParcel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [properties, setProperties] = useState<RunSummary[]>([])
  const [missingKeyNotice, setMissingKeyNotice] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const addr = address.trim()
    if (!addr) return
    setBusy(true)
    setError('')
    try {
      const r = await createLookup(addr, parcel.trim() || null)
      nav(`/public-data/runs/${r.run_id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.')
      setBusy(false)
    }
  }

  useEffect(() => {
    listLookups('', 1, 100).then((b) => setProperties(newestPropertyRuns(b.runs))).catch(() => {})
    getKeys()
      .then((rows) => {
        const isSet = (name: string) => rows.some((row) => row.name === name && row.is_set)
        setMissingKeyNotice(!isSet('openai') || !isSet('census'))
      })
      .catch(() => {})
  }, [])

  return (
    <main className="page">
      <h1>Public Data</h1>
      <p className="muted-line">
        Type an address and get a plain-English picture from public records — flood zones, zoning,
        roads, utilities, schools, and more. Enough to decide if a site is worth more of your time.
      </p>
      {missingKeyNotice && (
        <p className="notice">
          Some results need a key — see <Link to="/settings/needs">What PropRisk needs</Link>.
        </p>
      )}
      <form className="card stack" onSubmit={submit}>
        <label>
          Property address
          <input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            required
            maxLength={512}
            placeholder="1100 Congress Ave, Austin"
          />
        </label>
        <label>
          Parcel or property ID — if you have it
          <input value={parcel} onChange={(e) => setParcel(e.target.value)} maxLength={64} />
        </label>
        {busy ? (
          <Busy label="Starting the lookup…" />
        ) : (
          <button type="submit" disabled={!address.trim()}>
            Look it up
          </button>
        )}
        {error && <p className="error">{error}</p>}
      </form>
      <p className="muted-line">
        Lookups run in the background — you can keep working and come back to the report.
      </p>
      <section id="properties" className="pd-properties" aria-label="Your properties">
        <div className="pd-properties-heading">
          <h2>Your properties</h2>
        </div>
        {properties.length === 0 ? (
          <p className="muted-line">Your saved properties will appear here.</p>
        ) : (
          <div className="scroll-window pd-property-window">
            <ul className="list">
              {properties.map((property) => (
                <li key={property.id} className="card pd-property-row" onClick={() => nav(`/public-data/runs/${property.id}`)}>
                  <strong>{property.address}</strong>
                  <span className="muted-line pd-tight">
                    {property.parcel_id ? `Property ID ${property.parcel_id}` : 'Property ID not recorded'}
                    {property.county ? ` · ${fmtCounty(property.county)} County` : ''}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </main>
  )
}
