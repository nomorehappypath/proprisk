// PropRisk has no sign-in: every route renders straight away, behind one
// top bar (PropRisk / History / Settings).
import { useEffect } from 'react'
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import NewLookupPage from './pages/publicdata/NewLookupPage'
import RunPage from './pages/publicdata/RunPage'
import MapView from './pages/publicdata/MapView'
import HistoryPage from './pages/publicdata/HistoryPage'
import SettingsMenu from './pages/settings/SettingsMenu'
import NeedsPage from './pages/settings/NeedsPage'
import KeysPage from './pages/settings/KeysPage'
import SpendingPage from './pages/settings/SpendingPage'
import CoveragePage from './pages/settings/CoveragePage'

function TopBar() {
  const location = useLocation()
  return (
    <header className="topbar">
      {location.pathname === '/public-data' ? (
        <span className="brand-small">PropRisk</span>
      ) : (
        <Link to="/public-data">PropRisk</Link>
      )}
      <nav className="topbar-links">
        <Link to="/public-data/history">History</Link>
        <Link to="/settings">Settings</Link>
      </nav>
    </header>
  )
}

function Layout() {
  return (
    <>
      <TopBar />
      <Outlet />
    </>
  )
}

export default function App() {
  useEffect(() => {
    const reloadOnStaleChunk = (event: ErrorEvent) => {
      const message = event.message || ''
      if (!/chunk|importing|dynamically imported module/i.test(message)) return
      if (sessionStorage.getItem('proprisk_chunk_reload')) return
      sessionStorage.setItem('proprisk_chunk_reload', '1')
      window.setTimeout(() => sessionStorage.removeItem('proprisk_chunk_reload'), 10000)
      window.location.reload()
    }
    window.addEventListener('error', reloadOnStaleChunk)
    return () => window.removeEventListener('error', reloadOnStaleChunk)
  }, [])

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/public-data" replace />} />
          <Route path="/public-data" element={<NewLookupPage />} />
          <Route path="/public-data/history" element={<HistoryPage />} />
          <Route path="/public-data/runs/:id" element={<RunPage />} />
          <Route path="/public-data/runs/:id/map" element={<MapView />} />
          <Route path="/settings" element={<SettingsMenu />}>
            <Route path="needs" element={<NeedsPage />} />
            <Route path="keys" element={<KeysPage />} />
            <Route path="spending" element={<SpendingPage />} />
            <Route path="coverage" element={<CoveragePage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/public-data" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
