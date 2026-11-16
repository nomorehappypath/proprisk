// Typed fetch wrapper for the single local server. No sign-in: PropRisk sends no
// Authorization header and there is no token to clear on a 401.
export class ApiError extends Error {
  status: number
  data: Record<string, unknown>
  constructor(status: number, message: string, data: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.data = data
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  let payload: BodyInit | undefined
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const resp = await fetch(`/api${path}`, { method, headers, body: payload })
  if (resp.status === 204) return undefined as T
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const body = data as Record<string, unknown>
    throw new ApiError(
      resp.status,
      typeof body.error === 'string' ? body.error : 'Something went wrong — please try again.',
      body,
    )
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}

export function dollars(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return ''
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}
