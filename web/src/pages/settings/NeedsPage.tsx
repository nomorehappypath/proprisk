// "What PropRisk needs" — the checklist from GET /api/settings/needs: local
// prerequisites (for reference) and every key, what it is for, and its
// current state, with a link to set it up.
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Busy from '../../components/Busy'
import { ApiError } from '../../lib/api'
import { getNeeds, type NeedRow } from '../../lib/settings'

const REQUIRED_LABEL: Record<NeedRow['required'], string> = {
  required: 'Required',
  recommended: 'Recommended',
  optional: 'Optional',
}

export default function NeedsPage() {
  const [rows, setRows] = useState<NeedRow[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getNeeds()
      .then((body) => setRows(body.rows))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.'))
  }, [])

  if (error) return <p className="error">{error}</p>
  if (rows === null) return <Busy label="Loading…" />
  if (rows.length === 0) return <p className="muted-line">Nothing to show yet.</p>

  const macRows = rows.filter((row) => row.group === 'mac')
  const keyRows = rows.filter((row) => row.group === 'key')

  return (
    <div className="stack">
      <section className="card">
        <h2>On this Mac</h2>
        <p className="muted-line">Checked automatically by the launch script.</p>
        <ul className="list plain">
          {macRows.map((row) => (
            <li key={row.name}>
              <strong>{row.label}</strong>
              {row.install_hint && <span className="muted-line"> — {row.install_hint}</span>}
            </li>
          ))}
        </ul>
      </section>
      <section className="card">
        <h2>Keys and settings</h2>
        <ul className="list">
          {keyRows.map((row) => (
            <li key={row.name}>
              <div className="row-actions">
                <div>
                  <strong>{row.label}</strong>
                  <span className="muted-line"> · {REQUIRED_LABEL[row.required]}</span>
                </div>
                <span>{row.is_set ? `Set …${row.last4}` : 'Not set'}</span>
              </div>
              <p className="muted-line">Without it: {row.without}</p>
              <p className="muted-line">
                {row.cost}
                {row.signup_url && (
                  <>
                    {' '}
                    ·{' '}
                    <a href={row.signup_url} target="_blank" rel="noreferrer">
                      Sign up
                    </a>
                  </>
                )}
              </p>
              <Link to="/settings/keys" className="linklike">
                Set up
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
