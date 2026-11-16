import { useEffect, useState } from 'react'

// Every wait over ~500ms shows an animated indicator with elapsed seconds —
// plain static text reads as frozen on a phone. `since` (epoch ms) lets a
// caller seed the clock from the server's true start, so a reloaded page
// shows the real elapsed time instead of restarting at zero.
export default function Busy({ label, since }: { label: string; since?: number }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const started = since ?? Date.now()
    const tick = () => setElapsed(Math.max(0, Math.round((Date.now() - started) / 1000)))
    tick()
    const timer = setInterval(tick, 500)
    return () => clearInterval(timer)
  }, [since])
  return (
    <div className="busy">
      <div className="spinner" aria-hidden="true" />
      <div>
        {label}
        {elapsed > 0 && <span className="busy-elapsed"> · {elapsed}s</span>}
      </div>
    </div>
  )
}
