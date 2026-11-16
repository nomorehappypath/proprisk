// Era filmstrip for the "What this land was before" card (cards_schema_version
// 3, `Card.images`). One frame per photo year, oldest on the left, scrolled
// sideways on a phone and laid out in full on a desktop. Tapping a frame
// opens it large so the reader can actually see what is on the ground.
//
// Deliberately self-contained: all styling is inline (reading the app's own
// CSS custom properties for colour), so the component adds no rules to
// index.css and can be dropped into any card without a stylesheet change.
import { useCallback, useEffect, useState } from 'react'
import type { CardGalleryImage } from '../../lib/publicData'

const FRAME = 148 // px — two frames plus a peek of the third at 390px wide

const styles: Record<string, React.CSSProperties> = {
  wrap: { margin: '0.25rem 0 0.5rem' },
  strip: {
    display: 'flex',
    gap: '0.5rem',
    overflowX: 'auto',
    // Keeps the sideways scroll INSIDE the strip — the page itself must
    // never scroll horizontally on a phone.
    maxWidth: '100%',
    padding: '0.125rem 0 0.5rem',
    scrollSnapType: 'x proximity',
    WebkitOverflowScrolling: 'touch',
  },
  frame: {
    flex: '0 0 auto',
    width: FRAME,
    margin: 0,
    padding: 0,
    border: 'none',
    background: 'transparent',
    cursor: 'zoom-in',
    scrollSnapAlign: 'start',
    textAlign: 'center',
    font: 'inherit',
    color: 'inherit',
  },
  thumb: {
    width: FRAME,
    height: FRAME,
    objectFit: 'cover',
    display: 'block',
    borderRadius: 6,
    border: '1px solid rgba(0,0,0,0.18)',
    background: 'rgba(0,0,0,0.04)',
  },
  year: {
    marginTop: '0.25rem',
    fontWeight: 700,
    fontSize: '0.9rem',
    color: 'var(--text)',
  },
  attribution: {
    margin: 0,
    fontSize: '0.75rem',
    color: 'var(--muted)',
  },
  hint: {
    margin: '0 0 0.25rem',
    fontSize: '0.8rem',
    color: 'var(--muted)',
  },
  overlay: {
    position: 'fixed',
    inset: 0,
    zIndex: 1000,
    background: 'rgba(0,0,0,0.82)',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '0.75rem',
    padding: '1rem',
    cursor: 'zoom-out',
  },
  large: {
    maxWidth: 'min(92vw, 900px)',
    maxHeight: '72vh',
    objectFit: 'contain',
    borderRadius: 6,
    background: '#000',
  },
  overlayYear: {
    color: '#fff',
    fontWeight: 700,
    fontSize: '1.25rem',
    margin: 0,
  },
  overlayCaption: {
    color: '#fff',
    fontSize: '0.9rem',
    margin: 0,
    textAlign: 'center',
    maxWidth: 'min(92vw, 900px)',
  },
  close: {
    marginTop: '0.25rem',
    padding: '0.5rem 1.25rem',
    borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.6)',
    background: 'transparent',
    color: '#fff',
    font: 'inherit',
    cursor: 'pointer',
  },
}

export default function EraGallery({ images }: { images: CardGalleryImage[] }) {
  // Defensive: the server owns this list, but a malformed row must never
  // blank the card it sits on.
  const frames = (images ?? []).filter((img) => img && img.url)
  const [openAt, setOpenAt] = useState<number | null>(null)
  const close = useCallback(() => setOpenAt(null), [])

  // Escape closes the enlarged view — a full-screen overlay with no keyboard
  // way out is a trap on desktop.
  useEffect(() => {
    if (openAt === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openAt, close])

  if (frames.length === 0) return null

  const open = openAt !== null ? frames[openAt] : null
  const attribution = frames[0].attribution

  return (
    <div style={styles.wrap}>
      <p style={styles.hint}>
        {frames.length === 1
          ? 'One photo of this land. Tap it to see it larger.'
          : `${frames.length} photos of this land over the years — oldest first. Tap one to see it larger.`}
      </p>
      <div style={styles.strip}>
        {frames.map((img, i) => (
          <button
            key={`${img.url}-${i}`}
            type="button"
            style={styles.frame}
            onClick={() => setOpenAt(i)}
            aria-label={`See the ${img.label || 'aerial'} photo larger`}
          >
            {/* Deliberately NOT loading="lazy": the frames sit off-screen in
                a horizontal strip, so lazy loading leaves every frame past
                the first blank until the reader swipes to it. They are six
                ~30 KB chips inside a collapsed card that fetches nothing
                until it is opened. */}
            <img src={img.url} alt={img.caption || ''} style={styles.thumb} />
            {img.label && <span style={styles.year}>{img.label}</span>}
          </button>
        ))}
      </div>
      {attribution && <p style={styles.attribution}>{attribution}</p>}

      {open && (
        <div
          style={styles.overlay}
          role="dialog"
          aria-modal="true"
          aria-label={open.caption || 'Aerial photo'}
          onClick={close}
        >
          {open.label && <p style={styles.overlayYear}>{open.label}</p>}
          <img src={open.url} alt={open.caption || ''} style={styles.large} />
          {open.caption && <p style={styles.overlayCaption}>{open.caption}</p>}
          <button type="button" style={styles.close} onClick={close}>
            Close
          </button>
        </div>
      )}
    </div>
  )
}
