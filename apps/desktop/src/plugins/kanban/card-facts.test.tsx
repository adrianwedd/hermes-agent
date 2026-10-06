/**
 * Card facts must not present a CONFIGURED route as observed execution.
 *
 * `card_facts.model` is the resolved configuration (profile default, or the
 * task's own override) and `card_facts.run_routes.last_observed` is a retained
 * run receipt. The two are different claims, and the board is only allowed to
 * attribute a model to work that actually ran when the receipt exists — an
 * absent receipt is `UNKNOWN`, never a substitution of the configured default.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { CardFacts, observedRouteLabel } from './card-facts'
import { KANBAN_LOCALES } from './i18n'
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

it('labels a configured route as planned and does not claim it ran', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      // No run receipts at all: nothing has been observed for this card.
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'moa / BeastMode' },
      progress: null,
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: false,
    id: 't_planned',
    status: 'ready',
    title: 'queued work'
  }

  render(<CardFacts task={task} />)

  expect(screen.getByText(/Planned model/)).toBeTruthy()
  // The configured model appears ONLY under the planned row, and no observed
  // model row is rendered for a card with no retained run evidence.
  expect(screen.queryByText(/Last observed model/)).toBeNull()
})

it('shows the observed route once a retained receipt exists', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'openai-codex / gpt-6.1-sol' },
      progress: null,
      run_routes: {
        historical_inference: false,
        history: [],
        planned_agent: 'implementer',
        selected: {
          agent: 'implementer',
          basis: 'Worker-recorded runtime route',
          last_observed: { model: 'grok-4.6', observed_at: 1791274013, provider: 'xai' },
          launch: null,
          run_id: 7,
          started_at: 1791274013
        }
      }
    },
    dispatch_eligible: true,
    id: 't_observed',
    status: 'running',
    title: 'in flight'
  }

  render(<CardFacts task={task} />)

  expect(screen.getByText(/Last observed model/)).toBeTruthy()
  expect(screen.getByText(/xai \/ grok-4\.6/)).toBeTruthy()
})

it('shows no Progress row on a completed card even if a payload still carries one', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  // The backend now withholds `card_facts.progress` for terminal statuses; this
  // pins the RENDER path too, so an older backend (or a cached payload) cannot
  // put "Progress: Reviewed and approved …" under a finished card.
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      progress: { text: 'Reviewed and approved the corrected reconciliation', at: 1791274013, basis: 'Latest retained run handoff' },
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'openai-codex / gpt-6.1-sol' },
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: false,
    id: 't_finished',
    status: 'done',
    title: 'finished work'
  }

  render(<CardFacts task={task} />)

  expect(screen.queryByText(/Progress:/)).toBeNull()
  expect(screen.queryByText(/Reviewed and approved/)).toBeNull()
})

it('still shows Progress while the work is live', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      progress: { text: 'halfway through the extraction', at: 1791274013, basis: 'Worker comment during current run' },
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'openai-codex / gpt-6.1-sol' },
      run_routes: { historical_inference: false, history: [], planned_agent: 'implementer', selected: null }
    },
    dispatch_eligible: true,
    id: 't_live',
    status: 'running',
    title: 'in flight'
  }

  render(<CardFacts task={task} />)

  expect(screen.getByText(/Progress:/)).toBeTruthy()
  expect(screen.getByText(/halfway through the extraction/)).toBeTruthy()
})

it('renders the derived OPERATOR state on the tile, not a queue promise', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
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

  render(<CardFacts task={task} />)

  expect(screen.getByText('OPERATOR · receipt administration')).toBeTruthy()
  expect(screen.getByText(/bind retained completion evidence/)).toBeTruthy()
  expect(screen.getByText(/Owner: operator/)).toBeTruthy()
  expect(screen.getByText(/do not rerun the work/)).toBeTruthy()
})
