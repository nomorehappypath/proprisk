// The map's own route. The map itself now lives INLINE in the report's first
// section (Parcel Info) — this page is the deep link / full-screen view, and
// it renders the very same panel so there is exactly one map implementation.
import { Link, useParams } from 'react-router-dom'

import PropertyMapPanel from './PropertyMapPanel'

export default function MapView() {
  const { id = '' } = useParams()
  return (
    <main className="page pd-report">
      <Link className="linklike" to={`/public-data/runs/${id}`}>
        ← Back to the report
      </Link>
      <PropertyMapPanel runId={id} initialFullscreen />
    </main>
  )
}
