/**
 * A completed card's TILE carries no red diagnostics badge, even on a cached payload.
 *
 * The backend withholds the diagnostics rollup for terminal statuses (a block→unblock
 * streak is frozen history once the card can no longer be blocked), and `CardFooter`
 * gates the badge on the same derived fact, so a payload that still carries the summary
 * cannot paint the destructive badge under finished work — the operator's "completed
 * cards must not show stale progress or red warnings" contract.
 *
 * The live-card arm is the control: the badge must survive for a card with work still
 * to do, so the assertion above is meaningful rather than vacuous.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as KanbanApi from './api'
import { $boardSlug } from './api'
import { KanbanBoardPage } from './board'
import { KANBAN_LOCALES } from './i18n'
import type { Diagnostic, KanbanTask } from './types'

const cyclingWarning = {
  count: 3,
  kinds: { block_unblock_cycling: 3 },
  latest_at: 1791248759,
  highest_severity: 'warning' as const
}

const cyclingDiagnostic: Diagnostic = {
  actions: [],
  count: 3,
  data: { cycles: 3, window_seconds: 86400 },
  detail: 'this task bounced block → unblock',
  kind: 'block_unblock_cycling',
  last_seen_at: 1791248759,
  severity: 'warning',
  title: 'Task block→unblock cycled 3x in 24h'
}

// Exactly what the reviewer reproduced on the served payload of a live done card.
const doneCard: KanbanTask = {
  card_facts: {
    dispatch: {
      basis: 'Native terminal status done',
      column: 'done',
      dispatchable: false,
      eligible: false,
      label: 'Completed',
      next_action: null,
      owner: null,
      reason: 'Final resolution: shipped. Retained as history; no further dispatch',
      stage: 'DONE',
      terminal: true
    }
  } as KanbanTask['card_facts'],
  diagnostics: [cyclingDiagnostic],
  id: 't_done_cycling',
  status: 'done',
  title: 'a finished card with a retained cycling warning',
  warnings: cyclingWarning
}

const liveCard: KanbanTask = {
  card_facts: {
    dispatch: {
      basis: 'Native task row',
      column: 'ready',
      dispatchable: true,
      eligible: true,
      label: 'READY · queued',
      next_action: 'Await dispatch',
      owner: 'implementer',
      reason: 'Ready and dispatch_eligible=true',
      stage: 'READY',
      terminal: false
    }
  } as KanbanTask['card_facts'],
  diagnostics: [cyclingDiagnostic],
  id: 't_live_cycling',
  status: 'ready',
  title: 'a live card with the same history',
  warnings: cyclingWarning
}

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  fetchBoards: vi.fn(async () => ({ boards: [{ name: 'Default', project_id: null, slug: 'default', total: 2 }], current: 'default' })),
  fetchBoard: vi.fn(async () => ({
    assignees: [],
    columns: [
      { name: 'ready', tasks: [liveCard] },
      { name: 'done', tasks: [doneCard] }
    ],
    latest_event_id: 1,
    now: 1791248800,
    tenants: []
  })),
  fetchOrchestration: vi.fn(async () => ({ default_assignee: '' })),
  fetchProfiles: vi.fn(async () => ({ profiles: [] }))
}))

let disposeLocales: () => void = () => undefined
let client = new QueryClient()

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})

afterEach(() => {
  cleanup()
  disposeLocales()
  $boardSlug.set('')
  vi.clearAllMocks()
})

const withQuery = (ui: ReactNode) => <QueryClientProvider client={client}>{ui}</QueryClientProvider>

describe('the diagnostics badge on a card tile', () => {
  it('is absent on a completed card and present on a live one', async () => {
    const { container } = render(withQuery(<KanbanBoardPage />))

    await waitFor(() => expect(screen.getByText(doneCard.title)).toBeTruthy())

    // The tile's destructive span IS the warnings badge: the fixture has no blocked
    // cards and the live card's dispatch fact carries a reason, so nothing else in a
    // tile takes the destructive colour. (Base and patched both carry the class, so the
    // control arm below is comparable across the two trees.)
    const BADGE = 'span.text-destructive'
    const badges = container.querySelectorAll(BADGE)

    // The control: the live card's badge is real, so the negative below is meaningful.
    expect(badges).toHaveLength(1)

    const doneTile = screen.getByText(doneCard.title).closest('.group')
    expect(doneTile).toBeTruthy()
    expect(doneTile!.querySelectorAll(BADGE)).toHaveLength(0)
  })
})
