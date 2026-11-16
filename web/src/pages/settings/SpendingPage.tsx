// "AI spending" — this month's spend vs. the cap (GET /api/settings/spending),
// an editable cap, and this month's public-data source usage.
import { useEffect, useState } from 'react'
import Busy from '../../components/Busy'
import { ApiError, dollars } from '../../lib/api'
import { getSpending, setSpendingCap, type SpendingResponse } from '../../lib/settings'

export default function SpendingPage() {
  const [data, setData] = useState<SpendingResponse | null>(null)
  const [error, setError] = useState('')
  const [capInput, setCapInput] = useState('')
  const [savingCap, setSavingCap] = useState(false)
  const [capError, setCapError] = useState('')

  function load() {
    getSpending()
      .then((body) => {
        setData(body)
        setCapInput((body.budget_cents / 100).toFixed(2))
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.'))
  }

  useEffect(load, [])

  async function saveCap(e: React.FormEvent) {
    e.preventDefault()
    const dollarsValue = Number(capInput)
    if (!Number.isFinite(dollarsValue) || dollarsValue < 0 || dollarsValue > 10_000) {
      setCapError('Please enter an amount from $0.00 to $10,000.00.')
      return
    }
    setSavingCap(true)
    setCapError('')
    try {
      await setSpendingCap(Math.round(dollarsValue * 100))
      load()
    } catch (err) {
      setCapError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.')
    } finally {
      setSavingCap(false)
    }
  }

  if (error) return <p className="error">{error}</p>
  if (data === null) return <Busy label="Loading…" />

  return (
    <div className="stack">
      <section className="card">
        <h2>This month</h2>
        <p className="big-number">{dollars(data.spend_cents)}</p>
        <p className="muted-line">of a {dollars(data.budget_cents)} monthly cap</p>
        <div className="meter">
          <div
            className={`meter-fill${data.blocked ? ' meter-full' : data.warn ? ' meter-warn' : ''}`}
            style={{ width: `${Math.min(100, (data.spend_cents / Math.max(1, data.budget_cents)) * 100)}%` }}
          />
        </div>
        {data.blocked && <p className="notice">AI is paused until next month.</p>}
        {!data.blocked && data.warn && <p className="notice">Getting close to this month's cap.</p>}
      </section>

      <form className="card stack" onSubmit={saveCap}>
        <h2>Change the cap</h2>
        <label>
          Monthly cap (dollars)
          <input
            type="number"
            min={0}
            max={10000}
            step="0.01"
            value={capInput}
            onChange={(e) => setCapInput(e.target.value)}
          />
        </label>
        {capError && <p className="error">{capError}</p>}
        {savingCap ? <Busy label="Saving…" /> : <button type="submit">Save</button>}
      </form>

      <section className="card">
        <h2>Public-data usage this month</h2>
        <p className="muted-line">
          {data.public_data.lookups} lookup{data.public_data.lookups === 1 ? '' : 's'} ·{' '}
          {data.public_data.source_calls} source call{data.public_data.source_calls === 1 ? '' : 's'} ·{' '}
          est. {dollars(data.public_data.estimated_paid_cost_cents)} in paid sources
        </p>
        {data.public_data.by_source.length === 0 ? (
          <p className="muted-line">No public-data activity yet this month.</p>
        ) : (
          <ul className="list plain">
            {data.public_data.by_source.map((row) => (
              <li key={row.source}>
                {row.source}: {row.calls} call{row.calls === 1 ? '' : 's'}, est.{' '}
                {dollars(row.estimated_cost_cents)}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
