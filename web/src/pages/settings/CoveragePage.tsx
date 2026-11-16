// "Where PropRisk works" — the coverage table from GET /api/settings/coverage:
// the state, the counties PropRisk understands in depth, the cities with
// zoning data, and what works everywhere else.
import { useEffect, useState } from 'react'
import Busy from '../../components/Busy'
import { ApiError } from '../../lib/api'
import { getCoverage, type CoverageResponse } from '../../lib/settings'

export default function CoveragePage() {
  const [data, setData] = useState<CoverageResponse | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getCoverage()
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Something went wrong — please try again.'))
  }, [])

  if (error) return <p className="error">{error}</p>
  if (data === null) return <Busy label="Loading…" />

  return (
    <div className="stack">
      <section className="card">
        <h2>State</h2>
        <p>{data.state}</p>
        <p className="muted-line">Most sources assume this state; elsewhere, a lookup still runs with fewer results.</p>
      </section>

      <section className="card">
        <h2>Connected counties</h2>
        {data.counties.length === 0 ? (
          <p className="muted-line">Nothing to show yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="pd-table">
              <thead>
                <tr>
                  <th>County</th>
                  <th>Level</th>
                  <th>What is connected</th>
                </tr>
              </thead>
              <tbody>
                {data.counties.map((row) => (
                  <tr key={row.county}>
                    <td>{row.county}</td>
                    <td>{row.level}</td>
                    <td>{row.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Cities</h2>
        <p>
          <strong>Zoning data:</strong>{' '}
          {data.cities.zoning.length ? data.cities.zoning.join(', ') : 'None yet.'}
        </p>
        <p>
          <strong>Written zoning standards:</strong>{' '}
          {data.cities.standards.length ? data.cities.standards.join(', ') : 'None yet.'}
        </p>
        <p>
          <strong>Future land use:</strong>{' '}
          {data.cities.future_land_use.length ? data.cities.future_land_use.join(', ') : 'None yet.'}
        </p>
      </section>

      <section className="card">
        <h2>Statewide sources</h2>
        {data.statewide.length === 0 ? (
          <p className="muted-line">None yet.</p>
        ) : (
          <ul className="list plain">
            {data.statewide.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>Nationwide sources</h2>
        {data.nationwide.length === 0 ? (
          <p className="muted-line">None yet.</p>
        ) : (
          <ul className="list plain">
            {data.nationwide.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
      </section>
      <p className="muted-line">
        Everywhere else in {data.state}, a lookup still runs; county-specific cards say the county is
        not connected yet.
      </p>
    </div>
  )
}
