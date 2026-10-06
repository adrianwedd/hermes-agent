import { columnHelp, columnLabel, useKanban } from './i18n'
import type { KanbanTask } from './types'

type DispatchFact = NonNullable<KanbanTask['card_facts']>['dispatch']
type KanbanText = ReturnType<typeof useKanban>

/** The lane a card must be shown in: the backend's derived stage column when it
 *  supplied one, else the raw native status. The board buckets by the same value, so
 *  a card's lane, its tone and its own dispatch fact can never disagree. */
export function boardColumnFor(task: KanbanTask): string {
  return task.card_facts?.dispatch?.column ?? task.status
}

/** True when the dispatcher will act on this card at all. Capacity is a separate,
 *  receipt-less question and is never asserted here. */
export function dispatchActionable(task: KanbanTask): boolean {
  const fact = task.card_facts?.dispatch

  if (fact?.stage) {
    return Boolean(fact.dispatchable)
  }

  if (fact?.terminal) {
    return false
  }

  return (
    task.status === 'running' ||
    task.status === 'review' ||
    (task.status === 'ready' && task.dispatch_eligible !== false)
  )
}

/** Styling follows the FACT, not a bare flag.
 *
 *  `dispatch_eligible === false` is normal on a terminal card — it means "the
 *  queue no longer applies", not "something is wrong". The old expression
 *  painted every such card red, which is the stale "not queued" warning the
 *  board must never show on completed work. Only a card the backend is actually
 *  holding back (recorded, non-terminal, no supplied reason) is a fault worth
 *  the error colour. */
export function dispatchTone(fact: DispatchFact | undefined): string {
  if (fact?.terminal || fact?.stage === 'DONE' || fact?.stage === 'SUPERSEDED') {
    return 'font-medium text-(--ui-text-secondary)'
  }

  // A genuine external blocker or an unexplained hold is a fault the operator must
  // clear; an OPERATOR card is a decision, which is attention rather than error.
  if (fact?.stage === 'BLOCKED' || (fact?.eligible === false && !fact.reason)) {
    return 'font-medium text-destructive'
  }

  if (fact?.stage === 'OPERATOR' || fact?.owner === 'operator') {
    return 'font-medium text-amber-500'
  }

  return 'font-medium'
}

/** The card's own native state, read when `card_facts.dispatch` is absent.
 *
 *  `dispatch_eligible === false` says only "the queue will not claim this", and it is
 *  equally false on a finished card, under an operator decision and while a review is
 *  open — states whose next actions differ entirely. So the structured state the card
 *  still carries is read FIRST (terminal, blocked, review, a granted flag) and the
 *  generic "Held · dispatch disabled" copy is the LAST resort, for a card that has no
 *  other fact to report. The label is the canonical lane name and the reason is that
 *  lane's own help text, so no locale has to invent a synonym for a state it already
 *  names. */
function nativeState(task: KanbanTask, k: KanbanText): { label: string; reason: string } {
  const label = columnLabel(k, task.status)
  const help = columnHelp(k, task.status)

  if (task.status === 'done' || task.status === 'archived') {
    return { label, reason: help }
  }

  if (task.status === 'blocked') {
    return { label, reason: task.block_kind ? k.blockKindTip(task.block_kind) : help }
  }

  if (task.status === 'review') {
    return { label, reason: k.reviewChecking || help }
  }

  if (task.dispatch_eligible === true) {
    return { label, reason: k.dispatch.enabledReason }
  }

  if (task.dispatch_eligible === false) {
    return { label: k.dispatch.held, reason: k.dispatch.disabledReason }
  }

  return { label, reason: help || k.dispatch.missingReason }
}

export function DispatchFacts({ task }: { task: KanbanTask }) {
  const k = useKanban().dispatch
  const kanban = useKanban()
  const fact = task.card_facts?.dispatch
  const structured = fact ?? nativeState(task, kanban)
  const label = fact?.label ?? structured.label
  const reason = fact?.reason ?? structured.reason

  return (
    <div aria-label={k.heading} className="min-w-0 text-xs leading-snug [overflow-wrap:anywhere]">
      <div className={dispatchTone(fact)}>{label}</div>
      <div>{reason}</div>
      {fact?.owner && <div>{k.owner(fact.owner)}</div>}
      {fact?.next_action && <div>{k.next(fact.next_action)}</div>}
    </div>
  )
}
