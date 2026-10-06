/**
 * Dispatch facts on the card.
 *
 * The board was painting every `dispatch_eligible === false` card with the
 * destructive colour — including DONE cards, where the flag simply means "the
 * queue no longer applies". That red "not queued" warning on completed work is
 * the exact stale signal this guards against, so the tests below pin the tone to
 * the FACT the backend supplied, not to a bare flag.
 *
 * The second half pins the corrected acceptance: a card the dispatcher refuses must
 * not be filed in, nor claimable from, the lane its raw native status names, and the
 * generic "Held · dispatch disabled" copy may not stand in for a state the card
 * itself still reports.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { boardColumnFor, DispatchFacts, dispatchActionable, dispatchTone } from './dispatch-facts'
import { KANBAN_LOCALES } from './i18n'
import type { KanbanTask } from './types'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const terminal = {
  basis: 'Native terminal status done',
  column: 'done',
  dispatchable: false,
  eligible: false,
  label: 'Completed',
  next_action: null,
  owner: null,
  reason: 'Final resolution: shipped Retained as history; no further dispatch',
  stage: 'DONE',
  terminal: true
}

const live = {
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

// A `ready` card under an administrative hold: dispatch_eligible is false and the
// native claim guard refuses it on BOTH lanes, yet the native status stays `ready`.
const operatorHeld = {
  basis: 'administrative_pending event 11657',
  column: 'blocked',
  dispatchable: false,
  eligible: false,
  label: 'OPERATOR · receipt administration',
  next_action: 'Perform the named native closure for this card; do not rerun the work',
  owner: 'operator',
  reason: 'bind retained completion evidence and close natively',
  stage: 'OPERATOR',
  terminal: false
}

function liveTask(overrides: Partial<KanbanTask> = {}): KanbanTask {
  return {
    dispatch_eligible: false,
    id: 't_live',
    status: 'ready',
    title: 'a card',
    ...overrides
  }
}

it('does not colour a terminal card as a fault', () => {
  expect(dispatchTone(terminal)).not.toContain('text-destructive')
  // The old expression keyed on eligible === false alone, which is exactly why a
  // Done card came out red.
  expect(terminal.eligible).toBe(false)
  expect(dispatchTone(terminal)).toContain('text-(--ui-text-secondary)')
})

it('still flags a live card the backend is holding back with no stated reason', () => {
  expect(dispatchTone({ ...live, eligible: false, label: 'Held', reason: '', stage: 'HELD' })).toContain(
    'text-destructive'
  )
})

it('does not flag a held card that DOES explain itself', () => {
  const explained = {
    ...live,
    column: 'todo',
    dispatchable: false,
    eligible: false,
    label: 'WAITING · dependency',
    reason: 'unfinished t_1',
    stage: 'WAITING'
  }

  expect(dispatchTone(explained)).not.toContain('text-destructive')
})

it('marks an operator-owned hold in the attention colour, not as an error', () => {
  expect(dispatchTone(operatorHeld)).toContain('text-amber-500')
  expect(dispatchTone(operatorHeld)).not.toContain('text-destructive')
})

it('marks a genuine external blocker as a fault', () => {
  const blocked = {
    ...live,
    column: 'blocked',
    dispatchable: false,
    eligible: false,
    label: 'BLOCKED · capability',
    reason: 'no access to the vendor console',
    stage: 'BLOCKED' as const
  }

  expect(dispatchTone(blocked)).toContain('text-destructive')
})

it('an operator-only card is neither filed in nor actionable from the ready lane', () => {
  const held = liveTask({ card_facts: { dispatch: operatorHeld } as KanbanTask['card_facts'] })

  // Lane, verdict and label all agree, because all three read the one derived stage.
  expect(boardColumnFor(held)).toBe('blocked')
  expect(dispatchActionable(held)).toBe(false)
  // The same card WITHOUT the fact would read as dispatchable from `ready` alone —
  // which is exactly why the derived fact, not the raw status, has to win.
  expect(dispatchActionable(liveTask({ dispatch_eligible: true, status: 'ready' }))).toBe(true)
})

it('a done card is never actionable', () => {
  expect(dispatchActionable(liveTask({ dispatch_eligible: false, status: 'done' }))).toBe(false)
  expect(
    dispatchActionable(liveTask({ card_facts: { dispatch: terminal } as KanbanTask['card_facts'] }))
  ).toBe(false)
})

it('renders the backend fact rather than falling back to the flag', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const task: KanbanTask = {
    card_facts: { dispatch: terminal } as KanbanTask['card_facts'],
    dispatch_eligible: false,
    id: 't_done',
    status: 'done',
    title: 'finished'
  }

  render(<DispatchFacts task={task} />)

  expect(screen.getByText('Completed')).toBeTruthy()
  expect(screen.getByText(/Final resolution/)).toBeTruthy()
  // No owner / next-action rows on a finished card.
  expect(screen.queryByText(/^Owner:/)).toBeNull()
  expect(screen.queryByText(/^Next:/)).toBeNull()
  expect(screen.getByText('Completed').className).not.toContain('text-destructive')
})

it('a recorded block outranks the generic held flag when the fact is absent', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  // A cached/older payload with no card_facts: the card's own structured native state
  // (here a recorded block kind) decides, not the bare `dispatch_eligible=false`.
  const blocked: KanbanTask = {
    block_kind: 'needs_input',
    dispatch_eligible: false,
    id: 't_blocked',
    status: 'blocked',
    title: 'waiting on a human'
  }

  render(<DispatchFacts task={blocked} />)

  expect(screen.queryByText('Held · dispatch disabled')).toBeNull()
  expect(screen.getByText('Blocked')).toBeTruthy()
})

it('does not fall back to the held copy for a completed card that lost its facts', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const done: KanbanTask = {
    dispatch_eligible: false,
    id: 't_done_nofacts',
    status: 'done',
    title: 'finished, cached payload'
  }

  render(<DispatchFacts task={done} />)

  expect(screen.queryByText('Held · dispatch disabled')).toBeNull()
  expect(screen.getByText('Done')).toBeTruthy()
})
