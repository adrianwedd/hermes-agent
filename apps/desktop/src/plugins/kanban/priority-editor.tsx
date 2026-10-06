/**
 * After-creation priority editor (task modal).
 *
 * The value is native scheduling input (higher integer = claimed first) and the
 * write is the board's existing `PATCH /tasks/{id} {priority}`.
 *
 * Three behaviours differ from the neighbouring inline editors on purpose:
 *
 * 1. Keyboard-first. The trigger is a real `<button>`, so it is reachable and
 *    announced; the field is labelled, Enter saves, Escape cancels.
 * 2. Strict input. `parsePriority` rejects blank/fractional/unsafe values, and
 *    an invalid draft produces NO PATCH — silent coercion to 0 would demote a
 *    card the operator meant to raise.
 * 3. Readback before closing. The save awaits the persisted GET, so the editor
 *    closes on SERVER truth, not on our optimistic guess, and a rejected write
 *    keeps the draft (the parent's error toast reports it).
 */

import { Button, Codicon, Input, Tip } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { parsePriority, priorityPatch } from './priority'
import { PriorityGlyph, useKanban } from './ui'

/** Persist a priority and resolve with the server's stored value. Rejects on
 *  failure (the caller surfaces it) — deliberately NOT the fire-and-forget
 *  `mutate()` helper, which swallows rejections and would close the editor on
 *  a failed write. */
export type PriorityWriter = (taskId: string, priority: number) => Promise<null | number>

export function PriorityEditor({
  onSave,
  priority,
  taskId
}: {
  onSave: PriorityWriter
  priority: number
  taskId: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(String(priority))
  const [error, setError] = useState<null | string>(null)
  const [saving, setSaving] = useState(false)
  const k = useKanban()
  // `saving` is async state; a second Enter in the same tick would otherwise
  // fire a duplicate PATCH (React state has not flushed yet).
  const inFlight = useRef(false)

  // Re-seed from the server value whenever the card changes or a readback
  // lands, but never while a save is in flight (the draft is the operator's).
  useEffect(() => {
    if (!editing) {
      setDraft(String(priority))
    }
  }, [editing, priority])

  // The card can change under an open editor (switch tasks, then back).
  useEffect(() => {
    setEditing(false)
    setError(null)
    inFlight.current = false
  }, [taskId])

  const save = () => {
    if (inFlight.current) {
      return
    }

    const parsed = parsePriority(draft)

    if (parsed === null) {
      setError(k.priorityInvalid)

      return
    }

    setError(null)
    inFlight.current = true
    setSaving(true)
    onSave(taskId, parsed).then(
      () => {
        inFlight.current = false
        setSaving(false)
        setEditing(false)
      },
      () => {
        // Keep the editor open with the draft and let the caller report why.
        inFlight.current = false
        setSaving(false)
      }
    )
  }

  const cancel = () => {
    setEditing(false)
    setError(null)
    setDraft(String(priority))
  }

  if (!editing) {
    return (
      <span className="inline-flex items-center gap-1">
        <PriorityGlyph priority={priority} />
        <Tip label={k.priorityHint}>
          <button
            aria-label={k.priorityEdit}
            className="rounded p-0.5 text-(--ui-text-quaternary) transition-colors hover:text-foreground"
            onClick={() => setEditing(true)}
            type="button"
          >
            <Codicon name="edit" size="0.7rem" />
          </button>
        </Tip>
      </span>
    )
  }

  return (
    <span className="flex flex-col gap-1">
      <span className="flex items-center gap-1">
        <Input
          aria-label={k.metaPriority}
          className="h-7 w-20 text-[0.75rem]"
          disabled={saving}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              save()
            }

            if (event.key === 'Escape') {
              event.preventDefault()
              event.stopPropagation()
              cancel()
            }
          }}
          step={1}
          type="number"
          value={draft}
        />
        <Button disabled={saving} onClick={save} size="sm" variant="outline">
          {k.save}
        </Button>
        <Button disabled={saving} onClick={cancel} size="sm" variant="ghost">
          {k.cancel}
        </Button>
      </span>
      {error && (
        <span className="text-[0.65rem] text-destructive" role="alert">
          {error}
        </span>
      )}
    </span>
  )
}

/** The drawer's writer: PATCH, then read the server's stored value back.
 *  Resolves with the persisted priority (or `null` when an older backend omits
 *  it) and rethrows a rejected write so the editor keeps the draft. */
export function makePriorityWriter({
  fetch,
  invalidate,
  patch
}: {
  fetch: () => Promise<{ task?: { priority?: number } }>
  invalidate: () => void
  patch: (body: { priority: number }) => Promise<unknown>
}): PriorityWriter {
  return async (_taskId, priority) => {
    await patch(priorityPatch(priority))
    const detail = await fetch()
    invalidate()

    return typeof detail.task?.priority === 'number' ? detail.task.priority : null
  }
}
