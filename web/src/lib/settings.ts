// Typed calls to /api/settings for the Settings menu (needs, keys, spending, coverage).
import { api } from './api'

export type NeedRow = {
  name: string
  label: string
  group: 'mac' | 'key'
  required: 'required' | 'optional' | 'recommended'
  without: string
  signup_url: string | null
  cost: string
  is_set: boolean
  last4: string | null
  updated_at: string | null
  install_hint: string | null
}

export type NeedsResponse = { rows: NeedRow[] }

export type KeyRow = {
  name: string
  label: string
  is_set: boolean
  last4: string | null
  updated_at: string | null
}

export type SpendingResponse = {
  month: string
  spend_cents: number
  budget_cents: number
  warn: boolean
  blocked: boolean
  public_data: {
    lookups: number
    source_calls: number
    estimated_paid_cost_cents: number
    by_source: Array<{ source: string; calls: number; estimated_cost_cents: number }>
  }
}

export type CoverageResponse = {
  state: string
  counties: Array<{ state: string; county: string; level: string; detail: string }>
  cities: {
    zoning: string[]
    standards: string[]
    future_land_use: string[]
  }
  statewide: string[]
  nationwide: string[]
}

export const getNeeds = () => api.get<NeedsResponse>('/settings/needs')
export const getKeys = () => api.get<KeyRow[]>('/settings/keys')
export const setKey = (name: string, value: string) => api.put<void>(`/settings/keys/${name}`, { value })
export const removeKey = (name: string) => api.del<void>(`/settings/keys/${name}`)
export const getSpending = () => api.get<SpendingResponse>('/settings/spending')
export const setSpendingCap = (budgetCents: number) =>
  api.put<void>('/settings/spending/cap', { budget_cents: budgetCents })
export const getCoverage = () => api.get<CoverageResponse>('/settings/coverage')
