/**
 * The route status line must name the route that actually ran, and must say "not recorded"
 * when a route's sub-slots were never recorded — never the bare word "unknown", which is
 * indistinguishable from a data defect and conflates "no route recorded" with "unknown route".
 *
 * `card_facts.run_routes.last_observed` is the run's own recorded receipt (written by the
 * worker at the response it describes); `launch` is the run's launch route. A MoA route whose
 * aggregator/adviser slots are absent is unrecorded evidence about a call that DID happen;
 * a null route is "no route recorded at all"; these are three different statements.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { CardFacts, launchRouteLabel, NOT_RECORDED, observedRouteLabel } from './card-facts'
import { KANBAN_LOCALES } from './i18n'
import type { KanbanTask } from './types'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('renders the real aggregator and advisers when the run recorded a route', () => {
  const label = observedRouteLabel({
    advisers: [
      { model: 'grok-4.6', provider: 'xai' },
      { model: 'actor', provider: 'ollama-cloud' }
    ],
    aggregator: { model: 'gpt-6.1-sol', provider: 'openai-codex' },
    model: 'cloud',
    provider: 'moa'
  })

  expect(label).toBe('MoA · aggregator: openai-codex / gpt-6.1-sol · advisers: xai / grok-4.6, ollama-cloud / actor')
  expect(label).not.toMatch(/unknown/i)
})

it('renders an explicit "not recorded" for a MoA route with no recorded sub-slots', () => {
  const label = observedRouteLabel({ model: 'cloud', provider: 'moa' })

  expect(label).toBe(`MoA · aggregator: ${NOT_RECORDED} · advisers: ${NOT_RECORDED}`)
  // The conflation this bug was filed for: an absent recorded slot must not read "unknown".
  expect(label).not.toMatch(/unknown/i)
})

it('renders "no recorded route" for a null route, which is a different fact', () => {
  expect(observedRouteLabel(null)).toBe('Unknown · no recorded route')
  expect(observedRouteLabel(undefined)).toBe('Unknown · no recorded route')
  // A null route is not the same statement as an unrecorded sub-slot.
  expect(observedRouteLabel(null)).not.toBe(NOT_RECORDED)
})

it('renders partial slots honestly: a recorded aggregator with no advisers', () => {
  const label = observedRouteLabel({
    aggregator: { model: 'gpt-6.1-sol', provider: 'openai-codex' },
    model: 'cloud',
    provider: 'moa'
  })

  expect(label).toBe('MoA · aggregator: openai-codex / gpt-6.1-sol · advisers: not recorded')
})

it('names the launch preset without inventing unrecorded sub-slots', () => {
  // A launch route never HAS aggregator/adviser slots; saying "not recorded" there would
  // report a missing fact that was never asked for.
  expect(launchRouteLabel({ model: 'cloud', provider: 'moa' })).toBe('MoA · preset cloud')
  expect(launchRouteLabel({ model: 'gpt-6.1-sol', provider: 'openai-codex' })).toBe('openai-codex / gpt-6.1-sol')
  expect(launchRouteLabel(null)).toBe('Unknown · no recorded route')
})

it('shows a recorded route on the card face, not a configured guess', () => {
  registerPluginLocales('kanban', KANBAN_LOCALES)
  const task: KanbanTask = {
    card_facts: {
      blocker: null,
      model: { advisers: [], basis: 'Configured route; observed runtime may differ', primary: 'MoA · openai-codex / gpt-6.1-sol' },
      progress: null,
      run_routes: {
        historical_inference: false,
        history: [],
        planned_agent: 'implementer',
        selected: {
          agent: 'implementer',
          basis: 'Worker-recorded runtime route',
          launch: { model: 'cloud', provider: 'moa' },
          last_observed: {
            advisers: [{ model: 'actor', provider: 'ollama-cloud' }],
            aggregator: { model: 'gpt-6.1-sol', provider: 'openai-codex' },
            model: 'cloud',
            observed_at: 1791274013,
            provider: 'moa'
          },
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
  expect(screen.getByText(/openai-codex \/ gpt-6\.1-sol/)).toBeTruthy()
  expect(screen.getByText(/ollama-cloud \/ actor/)).toBeTruthy()
})
