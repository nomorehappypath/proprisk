// The Settings landing screen: four links to the screens that read and
// change local app configuration. No data of its own, so no loading/empty/
// error states to show.
import { NavLink, Outlet, useLocation } from 'react-router-dom'

const ITEMS = [
  { to: '/settings/needs', label: 'What PropRisk needs', blurb: 'Local requirements and every key, at a glance.' },
  { to: '/settings/keys', label: 'Keys and secrets', blurb: 'Add, replace or remove a key.' },
  { to: '/settings/spending', label: 'AI spending', blurb: 'This month’s spend, the cap, and public-data usage.' },
  { to: '/settings/coverage', label: 'Where PropRisk works', blurb: 'The state, counties and cities PropRisk understands.' },
]

export default function SettingsMenu() {
  const location = useLocation()
  const atMenuRoot = location.pathname === '/settings'

  return (
    <main className="page">
      <h1>Settings</h1>
      {atMenuRoot ? (
        <div className="stack">
          {ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} className="card settings-menu-item">
              <strong>{item.label}</strong>
              <p className="muted-line">{item.blurb}</p>
            </NavLink>
          ))}
        </div>
      ) : (
        <>
          <nav className="settings-subnav">
            {ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to}>
                {item.label}
              </NavLink>
            ))}
          </nav>
          <Outlet />
        </>
      )}
    </main>
  )
}
