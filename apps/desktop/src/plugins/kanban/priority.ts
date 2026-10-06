/**
 * After-creation priority editing — the pure half.
 *
 * Priority is native scheduling input, not presentation: `kanban_db.list_tasks`
 * and both dispatcher lanes order by `priority DESC, created_at ASC`, so a
 * higher integer is claimed first and a change here is what makes a card jump
 * the queue. The REST surface is the existing `PATCH /tasks/{id} {priority}`.
 *
 * The parser is strict on purpose. A blank field, `"1.5"` or `"1e3"` is NOT a
 * priority; coercing it to 0 (the old dashboard behaviour) silently demotes a
 * card the operator meant to raise. Invalid input returns `null` so the caller
 * refuses the write instead of guessing.
 */

const INTEGER = /^[+-]?\d+$/

/** A whole number the backend can store, or `null` when the input is not one.
 *  Zero and negative integers are valid: 0 is the default and negatives are a
 *  legitimate way to hold a card back. */
export function parsePriority(input: string): null | number {
  const raw = input.trim()

  if (!INTEGER.test(raw)) {
    return null
  }

  const value = Number(raw)

  return Number.isSafeInteger(value) ? value : null
}

/** The exact PATCH body for a priority change. Kept as a function so the REST
 *  field name lives in one place and tests can assert it without a render. */
export const priorityPatch = (priority: number): { priority: number } => ({ priority })
