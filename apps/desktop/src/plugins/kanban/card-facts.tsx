import { cardIsTerminal } from './dispatch-facts'
import type { KanbanTask, RecordedRoute } from './types'

/** One-line tile summary: a run summary is markdown, and a clamped line of raw
 *  `**bold**` / `## heading` markers reads as noise at 11px. Strip the inline
 *  emphasis and block markers, collapse whitespace, drop the rest of the line. */
export function tileSummary(text: string | null | undefined): string {
  if (!text) {
    return ''
  }

  return text
    .replace(/^\s*(#{1,6}|[-*+>])\s+/gm, '')
    .replace(/(\*\*|__|`)/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function observedRouteLabel(route: RecordedRoute | null | undefined) {
  if (!route) {
    return 'Unknown · no recorded route'
  }


  if (route.provider === 'moa') {return `MoA · aggregator: ${route.aggregator ? `${route.aggregator.provider} / ${route.aggregator.model}` : 'unknown'} · advisers: ${route.advisers?.length ? route.advisers.map(x => `${x.provider} / ${x.model}`).join(', ') : 'unknown'}`}

  return `${route.provider} / ${route.model}`
}

export function blockerFact(task: KanbanTask) {
  const recorded = task.card_facts?.blocker

  if (task.status !== 'blocked' || (recorded && recorded.label !== 'Blocker unknown')) {return recorded}

  const failure = task.last_failure_error?.replace(/https?:\/\/\S+/g, '[link]')
    .replace(/\b(token|api[_ -]?key|password|secret|authorization)\s*[:=]\s*\S+/gi, '$1=[redacted]')
    .replace(/\s+/g, ' ').trim().slice(0, 240)

  return failure ? { label: 'Last recorded failure', detail: failure }
    : { label: 'Blocker unknown', detail: 'No explicit blocker evidence recorded' }
}

/**
 * The board tile carries exactly one evidence line, and only when it changes what
 * the operator does with the card: an operator/held state the queue will not act on,
 * a blocker, or live progress. Route, tokens, estimate, delivery, dispatch copy and
 * the full blocker text all live in the drawer, which already renders them — repeating
 * them on the tile is what made a column unreadable.
 *
 * The lane alone cannot carry this: a card can sit in the Ready column while the
 * backend derives OPERATOR (receipt administration), and a tile that then reads
 * "ready" promises a dispatch that will never happen.
 */
export function CardAlert({ task }: { task: KanbanTask }) {
  if (cardIsTerminal(task)) {return null}

  const dispatch = task.card_facts?.dispatch

  if (dispatch && (dispatch.stage === 'OPERATOR' || dispatch.owner === 'operator')) {
    return <AlertLine label={dispatch.label} reason={dispatch.reason} task={task} tone="text-amber-500" />
  }

  if (task.status === 'blocked') {
    const blocker = blockerFact(task) ?? { label: 'Blocker unknown', detail: 'No explicit blocker evidence recorded' }

    return <AlertLine label={blocker.label} reason={blocker.detail} task={task} tone="text-destructive" />
  }

  if (dispatch?.eligible === false) {
    // Show the hold whether or not the backend explained it: a suppressed
    // "residency_denied" is exactly the card an operator needs to see.
    return <AlertLine label={dispatch.label} reason={dispatch.reason} task={task} tone="text-destructive" />
  }

  const progress = task.card_facts?.progress

  if (progress?.text) {
    return (
      <span
        className="line-clamp-1 break-words text-[0.6875rem] leading-snug text-(--ui-text-tertiary)"
        title={progress.basis ?? progress.text}
      >
        {progress.text}
      </span>
    )
  }

  return null
}

/** One clamped line: the state label, then its reason. The rest of the dispatch
 *  contract (owner, next action, after-gate continuation) stays in the tooltip and
 *  in the drawer, so nothing is lost and the tile does not grow a paragraph. */
function AlertLine({ label, reason, task, tone }: { label: string; reason: string; task: KanbanTask; tone: string }) {
  const dispatch = task.card_facts?.dispatch

  const tooltip = [
    label,
    reason,
    dispatch?.owner ? `Owner: ${dispatch.owner}` : '',
    dispatch?.next_action ? `Next: ${dispatch.next_action}` : ''
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <span className="line-clamp-1 break-words text-[0.6875rem] leading-snug" title={tooltip}>
      <span className={tone}>{label}</span>
      {reason ? <span className="text-(--ui-text-tertiary)"> · {reason}</span> : null}
    </span>
  )
}
