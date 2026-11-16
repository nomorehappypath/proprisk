// "Keys and secrets" — one setup panel per key from GET /api/settings/keys.
// A saved value is never shown again in full: only its last 4 characters.
import { useEffect, useState } from 'react'
import Busy from '../../components/Busy'
import { ApiError } from '../../lib/api'
import { getKeys, removeKey, setKey, type KeyRow } from '../../lib/settings'

function Panel({ row, onChanged }: { row: KeyRow; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmingRemove, setConfirmingRemove] = useState(false)

  async function save() {
    setBusy(true)
    setError('')
    try {
      await setKey(row.name, value)
      setValue('')
      setEditing(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.')
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    setError('')
    try {
      await removeKey(row.name)
      setConfirmingRemove(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.')
      setBusy(false)
    }
  }

  return (
    <li className="card">
      <div className="row-actions">
        <strong>{row.label}</strong>
        <span>{row.is_set ? `Set …${row.last4} (saved ${row.updated_at?.slice(0, 10)})` : 'Not set'}</span>
      </div>
      {editing ? (
        <div className="stack">
          <input
            type={row.name === 'contact_email' ? 'email' : 'password'}
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            maxLength={row.name === 'contact_email' ? 254 : 512}
          />
          {error && <p className="error">{error}</p>}
          {busy ? (
            <Busy label="Saving…" />
          ) : (
            <div className="row-actions">
              <button type="button" onClick={save} disabled={!value.trim()}>
                Save
              </button>
              <button
                type="button"
                className="linklike"
                onClick={() => {
                  setEditing(false)
                  setValue('')
                  setError('')
                }}
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="row-actions">
          <button type="button" onClick={() => setEditing(true)}>
            {row.is_set ? 'Replace' : 'Add'}
          </button>
          {row.is_set && !confirmingRemove && (
            <button type="button" className="linklike danger" onClick={() => setConfirmingRemove(true)}>
              Remove
            </button>
          )}
          {confirmingRemove && (
            <span className="row-actions">
              Remove this key?
              <button type="button" className="linklike danger" onClick={remove} disabled={busy}>
                Yes, remove it
              </button>
              <button type="button" className="linklike" onClick={() => setConfirmingRemove(false)}>
                Cancel
              </button>
            </span>
          )}
        </div>
      )}
    </li>
  )
}

export default function KeysPage() {
  const [rows, setRows] = useState<KeyRow[] | null>(null)
  const [error, setError] = useState('')

  function load() {
    getKeys()
      .then((body) => setRows(body))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.'))
  }

  useEffect(load, [])

  if (error) return <p className="error">{error}</p>
  if (rows === null) return <Busy label="Loading…" />
  if (rows.length === 0) return <p className="muted-line">Nothing to show yet.</p>

  return (
    <ul className="list">
      {rows.map((row) => (
        <Panel key={row.name} row={row} onChanged={load} />
      ))}
    </ul>
  )
}
