/**
 * The board tile carries ONE evidence line. These tests pin what may appear there
 * and — just as importantly — what may not: route, tokens, estimate, delivery and
 * the dispatch contract belong to the drawer, and a tile that repeats them is the
 * wall of text this module exists to prevent.
 *
 * The line is not optional decoration. A card can sit in the Ready column while the
 * backend derives OPERATOR (receipt administration); a tile that then reads "ready"
 * promises a dispatch that will never happen. That state must still reach the tile.
 *
 * `observedRouteLabel` is a pure function shared with the drawer's run-routes section.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { CardAlert, observedRouteLabel, tileSummary } from './card-facts'
import type { KanbanTask } from './types'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('reports an unknown route as unknown rather than guessing the configured one', () => {
  expect(observedRouteLabel(null)).toBe('Unknown · no recorded route')
  expect(observedRouteLabel(undefined)).toBe('Unknown · no recorded route')
})

it('names the observed stages of a multi-stage run', () => {
  const label = observedRouteLabel({
    advisers: [{ model: 'grok-4.6', provider: 'xai' }],
    aggregator: { model: 'gpt-6.1-sol', provider: 'openai-codex' },
    model: 'moa',
    provider: 'moa'
  })

  expect(label).toContain('aggregator: openai-codex / gpt-6.1-sol')
  expect(label).toContain('advisers: xai / grok-4.6')
})

it('strips markdown out of the one-line tile summary', () => {
  // Run summaries are markdown; a clamped line of raw markers is noise at 11px.
  expect(tileSummary('## Round 2 review — verdict: **APPROVE**')).toBe('Round 2 review — verdict: APPROVE')
  expect(tileSummary('  - fixed the   throughput stats\n- recomputed from nine records')).toBe(
    'fixed the throughput stats recomputed from nine records'
  )
  expect(tileSummary(null)).toBe('')
  expect(tileSummary(undefined)).toBe('')
})

it('says nothing on an ordinary queued card', () => {
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'moa / BeastMode' },
      progress: null,
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: false,
    id: 't_planned',
    status: 'ready',
    title: 'queued work'
  }

  const { container } = render(<CardAlert task={task} />)

  // Neither the configured route nor the planned agent is tile material.
  expect(container.textContent).toBe('')
  expect(container.querySelector('[title]')).toBeNull()
  expect(screen.queryByText(/Planned model/)).toBeNull()
  expect(screen.queryByText(/Last observed model/)).toBeNull()
})

it('keeps the derived OPERATOR state on the tile, not a queue promise', () => {
  // The card the operator must not be misled by: native status `ready`, but the
  // dispatcher refuses it on both lanes under a receipt-administration hold. The tile
  // must say OPERATOR with the closure it needs — never the Ready lane's copy.
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      dispatch: {
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
      },
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'openai-codex / gpt-6.1-sol' },
      progress: null,
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: false,
    id: 't_operator',
    status: 'ready',
    title: 'substantive-complete, receipts pending'
  }

  const { container } = render(<CardAlert task={task} />)

  expect(screen.getByText('OPERATOR · receipt administration')).toBeTruthy()
  expect(screen.getByText(/bind retained completion evidence/)).toBeTruthy()
  // Owner and next action are retained in the tooltip rather than in the tile's text.
  const tooltip = container.querySelector('[title]')?.getAttribute('title') ?? ''
  expect(tooltip).toContain('Owner: operator')
  expect(tooltip).toContain('do not rerun the work')
})

it('names the blocker on a blocked card', () => {
  const task: KanbanTask = {
    card_facts: {
      blocker: { label: 'Needs input', detail: 'Approve an exclusive window before the ladder runs' },
      model: { advisers: [], basis: '', primary: 'moa' },
      progress: null,
      run_routes: { historical_inference: false, history: [], planned_agent: 'default', selected: null }
    },
    dispatch_eligible: false,
    id: 't_blocked',
    status: 'blocked',
    title: 'window request'
  }

  render(<CardAlert task={task} />)

  expect(screen.getByText('Needs input')).toBeTruthy()
  expect(screen.getByText(/Approve an exclusive window/)).toBeTruthy()
})

it('shows live progress while the work is running', () => {
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      model: { advisers: [], basis: '', primary: 'moa' },
      progress: { text: 'halfway through the extraction', at: 1791274013, basis: 'Worker comment during current run' },
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: true,
    id: 't_live',
    status: 'running',
    title: 'in flight'
  }

  render(<CardAlert task={task} />)

  expect(screen.getByText(/halfway through the extraction/)).toBeTruthy()
})

it('shows no stale progress on a completed card even if a payload still carries one', () => {
  // The backend withholds `card_facts.progress` for terminal statuses; this pins the
  // render path too, so an older backend (or a cached payload) cannot put
  // "Reviewed and approved …" under a finished card.
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      progress: { text: 'Reviewed and approved the corrected reconciliation', at: 1791274013, basis: 'Latest retained run handoff' },
      model: { advisers: [], basis: '', primary: 'moa' },
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: false,
    id: 't_finished',
    status: 'done',
    title: 'finished work'
  }

  const { container } = render(<CardAlert task={task} />)

  expect(container.textContent).toBe('')
  expect(screen.queryByText(/Reviewed and approved/)).toBeNull()
})
