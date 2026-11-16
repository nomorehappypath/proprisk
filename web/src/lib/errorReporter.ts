// Catches uncaught browser errors and promise rejections and posts them to the
// server's error sink (POST /api/client-errors), so a PropRisk failure in the
// browser lands in the same local log as a server failure. Never throws itself.
const ENDPOINT = '/api/client-errors'
const MAX_MESSAGE = 4000

function post(payload: Record<string, unknown>) {
  try {
    const body = JSON.stringify(payload).slice(0, 16 * 1024)
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => {})
  } catch {
    // reporting must never throw back into the app
  }
}

export function installErrorReporter(): void {
  window.addEventListener('error', (event: ErrorEvent) => {
    post({
      kind: 'error',
      message: String(event.message ?? 'Unknown error').slice(0, MAX_MESSAGE),
      source: event.filename,
      line: event.lineno,
      column: event.colno,
      stack: event.error instanceof Error ? String(event.error.stack).slice(0, MAX_MESSAGE) : undefined,
      url: window.location.pathname,
    })
  })

  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    const reason = event.reason
    post({
      kind: 'unhandledrejection',
      message: (reason instanceof Error ? reason.message : String(reason)).slice(0, MAX_MESSAGE),
      stack: reason instanceof Error ? String(reason.stack).slice(0, MAX_MESSAGE) : undefined,
      url: window.location.pathname,
    })
  })
}
