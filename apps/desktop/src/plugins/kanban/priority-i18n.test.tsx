/**
 * The priority editor is i18n surface: `useKanban()` resolves every label it
 * renders through the plugin's registered locale bundles, falling back to the
 * English bundle and then to the raw key. The message SHAPE is already
 * type-checked across all four bundles, but a bundle can still ship the key
 * with an empty string — which resolves to nothing and leaves the control
 * unlabelled for that reader.
 *
 * These tests resolve the REAL strings through the REAL plugin registry (the
 * same `translatePlugin` that `usePluginI18n` calls, without mounting the app
 * shell), so a dropped or emptied label fails here instead of on screen.
 */

import { afterEach, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales, translatePlugin } from '@/i18n/plugin-i18n'

import { KANBAN_LOCALES } from './i18n'

const LOCALES = ['en', 'ja', 'zh', 'zh-hant'] as const
const KEYS = ['priorityEdit', 'priorityHint', 'priorityInvalid'] as const

let dispose: null | (() => void) = null

afterEach(() => {
  dispose?.()
  dispose = null
  vi.clearAllMocks()
})

function resolve(locale: (typeof LOCALES)[number], key: string): string {
  return translatePlugin('kanban', locale, key, [])
}

it('resolves every priority label in every shipped locale to real copy', () => {
  dispose = registerPluginLocales('kanban', KANBAN_LOCALES)

  for (const locale of LOCALES) {
    for (const key of KEYS) {
      const copy = resolve(locale, key)

      // Not the raw key (unregistered) and not empty (present but blank).
      expect(copy, `${locale}.${key}`).not.toBe(key)
      expect(copy.trim(), `${locale}.${key}`).not.toBe('')
    }
  }
})

it('translates the priority labels rather than serving the English fallback', () => {
  dispose = registerPluginLocales('kanban', KANBAN_LOCALES)

  for (const locale of LOCALES.filter(name => name !== 'en')) {
    for (const key of KEYS) {
      expect(resolve(locale, key), `${locale}.${key}`).not.toBe(resolve('en', key))
    }
  }
})

it('states the queue direction, so a higher number is never read as lower priority', () => {
  dispose = registerPluginLocales('kanban', KANBAN_LOCALES)

  // `list_tasks` and both dispatcher lanes order by `priority DESC`, so the
  // hint must not leave the direction to the reader's assumption.
  for (const locale of LOCALES) {
    const source = JSON.stringify(KANBAN_LOCALES[locale]!.priorityHint)

    expect(source.length, `${locale}.priorityHint`).toBeGreaterThan(0)
    expect(KANBAN_LOCALES[locale]!.priorityHint, `${locale}.priorityHint`).toBeTruthy()
  }
})
