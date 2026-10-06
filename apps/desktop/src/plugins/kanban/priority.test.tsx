/**
 * After-creation priority editing (native scheduling input).
 *
 * These tests pin BEHAVIOUR, not the presence of a component: a priority is a
 * native queue key, so the contract that matters is (a) what number gets
 * persisted, (b) that an invalid draft persists NOTHING, and (c) that the
 * editor closes on the server's stored value rather than on our own guess.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { en, KANBAN_LOCALES } from './i18n'
import { makePriorityWriter, PriorityEditor } from './priority-editor'
import { parsePriority, priorityPatch } from './priority'

let disposeLocales: () => void

beforeEach(() => {
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})

afterEach(() => {
  cleanup()
  disposeLocales()
  vi.clearAllMocks()
})

describe('parsePriority', () => {
  it('accepts whole numbers, including zero and negatives', () => {
    expect(parsePriority('3')).toBe(3)
    expect(parsePriority(' 12 ')).toBe(12)
    expect(parsePriority('0')).toBe(0)
    expect(parsePriority('-2')).toBe(-2)
    expect(parsePriority('+4')).toBe(4)
  })

  it('refuses anything that is not a whole number instead of coercing to 0', () => {
    // Silent coercion would DEMOTE a card the operator meant to raise — the
    // reason this is a refusal and not a fallback.
    for (const bad of ['', '   ', 'abc', '1.5', '1e3', '0x10', '1,000', '--1']) {
      expect(parsePriority(bad), bad).toBeNull()
    }
  })

  it('refuses integers the backend cannot store safely', () => {
    expect(parsePriority('9007199254740993')).toBeNull()
  })
})

describe('priorityPatch', () => {
  it('names the native REST field', () => {
    expect(priorityPatch(7)).toEqual({ priority: 7 })
  })
})

const PRIORITY_TRIGGER = en.priorityEdit
const PRIORITY_FIELD = en.metaPriority

function openEditor(onSave = vi.fn(async () => 5), priority = 5) {
  render(<PriorityEditor onSave={onSave} priority={priority} taskId="t_priority" />)
  fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))

  return onSave
}

describe('PriorityEditor', () => {
  it('is reachable by keyboard and announces itself', () => {
    render(<PriorityEditor onSave={vi.fn()} priority={4} taskId="t_priority" />)

    const trigger = screen.getByRole('button', { name: PRIORITY_TRIGGER })
    expect((trigger as HTMLButtonElement).type).toBe('button')
    expect(trigger.getAttribute('aria-label')).toBe(PRIORITY_TRIGGER)

    fireEvent.click(trigger)
    expect(screen.getByRole('spinbutton', { name: PRIORITY_FIELD })).toBeTruthy()
  })

  it('closes the editor when Escape is pressed, without writing', () => {
    const onSave = openEditor()
    fireEvent.change(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { target: { value: '77' } })
    fireEvent.keyDown(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { key: 'Escape' })

    expect(onSave).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: PRIORITY_TRIGGER })).toBeTruthy()
  })

  it('saves the entered number on Enter and closes only after the write resolves', async () => {
    let release!: (value: number) => void
    const onSave = vi.fn(
      () =>
        new Promise<number>(resolve => {
          release = resolve
        })
    )

    render(<PriorityEditor onSave={onSave} priority={1} taskId="t_priority" />)
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))
    fireEvent.change(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { target: { value: '9' } })
    fireEvent.keyDown(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { key: 'Enter' })

    expect(onSave).toHaveBeenCalledWith('t_priority', 9)
    // Still open: the write has not landed yet, so the readback has not either.
    expect(screen.getByRole('spinbutton', { name: PRIORITY_FIELD })).toBeTruthy()

    release(9)
    await waitFor(() => expect(screen.queryByRole('spinbutton', { name: PRIORITY_FIELD })).toBeNull())
    expect(screen.getByRole('button', { name: PRIORITY_TRIGGER })).toBeTruthy()
  })

  it('saves through the Save button too', async () => {
    const onSave = vi.fn(async () => 2)

    render(<PriorityEditor onSave={onSave} priority={1} taskId="t_priority" />)
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))
    fireEvent.change(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: en.save }))

    await waitFor(() => expect(onSave).toHaveBeenCalledWith('t_priority', 2))
  })

  it('persists nothing for a fractional draft and says why', () => {
    const onSave = openEditor()
    const field = screen.getByRole('spinbutton', { name: PRIORITY_FIELD })
    fireEvent.change(field, { target: { value: '2.5' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onSave).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toBe(en.priorityInvalid)
    // The editor stays open with the operator's draft intact.
    expect((field as HTMLInputElement).value).toBe('2.5')
  })

  it('fires one write when Enter is pressed twice before the write settles', () => {
    const onSave = vi.fn(() => new Promise<number>(() => {}))

    render(<PriorityEditor onSave={onSave} priority={1} taskId="t_priority" />)
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))
    const field = screen.getByRole('spinbutton', { name: PRIORITY_FIELD })
    fireEvent.change(field, { target: { value: '6' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.keyDown(field, { key: 'Enter' })

    expect(onSave).toHaveBeenCalledTimes(1)
  })

  it('keeps the draft when the write is rejected, so a failure is correctable', async () => {
    const onSave = vi.fn(async () => {
      throw new Error('refused')
    })

    render(<PriorityEditor onSave={onSave} priority={1} taskId="t_priority" />)
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))
    const field = screen.getByRole('spinbutton', { name: PRIORITY_FIELD })
    fireEvent.change(field, { target: { value: '8' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    const stillOpen = screen.getByRole('spinbutton', { name: PRIORITY_FIELD }) as HTMLInputElement

    expect(stillOpen.value).toBe('8')
    // A failed write must not wedge the form: Save is usable again.
    await waitFor(() =>
      expect((screen.getByRole('button', { name: en.save }) as HTMLButtonElement).disabled).toBe(false)
    )
  })

  it('cancels without writing and restores the server value', () => {
    const onSave = openEditor(vi.fn(), 5)
    fireEvent.change(screen.getByRole('spinbutton', { name: PRIORITY_FIELD }), { target: { value: '99' } })
    fireEvent.click(screen.getByRole('button', { name: en.cancel }))

    expect(onSave).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))
    expect((screen.getByRole('spinbutton', { name: PRIORITY_FIELD }) as HTMLInputElement).value).toBe('5')
  })

  it('re-seeds a closed editor when the server value changes underneath it', () => {
    const onSave = vi.fn()
    const { rerender } = render(<PriorityEditor onSave={onSave} priority={1} taskId="t_priority" />)

    rerender(<PriorityEditor onSave={onSave} priority={42} taskId="t_priority" />)
    fireEvent.click(screen.getByRole('button', { name: PRIORITY_TRIGGER }))

    expect((screen.getByRole('spinbutton', { name: PRIORITY_FIELD }) as HTMLInputElement).value).toBe('42')
  })
})

describe('makePriorityWriter', () => {
  it('PATCHes the priority, then reads the stored value back and invalidates', async () => {
    const patch = vi.fn(async () => ({ task: {} }))
    const invalidate = vi.fn()
    const fetch = vi.fn(async () => ({ task: { priority: 7 } }))
    const write = makePriorityWriter({ fetch, invalidate, patch })

    await expect(write('t_priority', 7)).resolves.toBe(7)
    expect(patch).toHaveBeenCalledWith({ priority: 7 })
    expect(fetch).toHaveBeenCalled()
    expect(invalidate).toHaveBeenCalled()
    // The readback happens AFTER the write, never instead of it.
    expect(patch.mock.invocationCallOrder[0]).toBeLessThan(fetch.mock.invocationCallOrder[0]!)
  })

  it('reports an unknown persisted value rather than echoing the request', async () => {
    // An older backend that omits `priority` must not be reported as success
    // for a number we only assumed.
    const write = makePriorityWriter({
      fetch: async () => ({ task: {} }),
      invalidate: vi.fn(),
      patch: async () => ({})
    })

    await expect(write('t_priority', 7)).resolves.toBeNull()
  })

  it('rethrows a rejected write so the caller can report it', async () => {
    const write = makePriorityWriter({
      fetch: vi.fn(async () => ({ task: { priority: 1 } })),
      invalidate: vi.fn(),
      patch: async () => {
        throw new Error('409 refused')
      }
    })

    await expect(write('t_priority', 7)).rejects.toThrow('409 refused')
  })
})
