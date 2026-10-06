import { DispatchFacts } from './dispatch-facts'
import { DeliveryBadges } from './delivery-receipt'
import type { KanbanTask, RecordedRoute } from './types'

function age(at: number | null | undefined, now = Date.now() / 1000) {
  if (!at) return 'time unknown'
  const minutes = Math.max(0, Math.floor((now - at) / 60))
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes}m ago` : `${Math.floor(minutes / 60)}h ago`
}

export function observedRouteLabel(route: RecordedRoute | null | undefined) {
  if (!route) return 'Unknown · no recorded route'
  if (route.provider === 'moa') return `MoA · aggregator: ${route.aggregator ? `${route.aggregator.provider} / ${route.aggregator.model}` : 'unknown'} · advisers: ${route.advisers?.length ? route.advisers.map(x => `${x.provider} / ${x.model}`).join(', ') : 'unknown'}`
  return `${route.provider} / ${route.model}`
}

export function blockerFact(task: KanbanTask) {
  const recorded = task.card_facts?.blocker
  if (task.status !== 'blocked' || (recorded && recorded.label !== 'Blocker unknown')) return recorded
  const failure = task.last_failure_error?.replace(/https?:\/\/\S+/g, '[link]')
    .replace(/\b(token|api[_ -]?key|password|secret|authorization)\s*[:=]\s*\S+/gi, '$1=[redacted]')
    .replace(/\s+/g, ' ').trim().slice(0, 240)
  return failure ? { label: 'Last recorded failure', detail: failure }
    : { label: 'Blocker unknown', detail: 'No explicit blocker evidence recorded' }
}

function MissingCardFacts({ task }: { task: KanbanTask }) {
  const blocker = blockerFact(task)
  return <dl aria-label="Task evidence" className="min-w-0 border-t border-(--ui-stroke-tertiary) pt-1.5 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">
    <DispatchFacts task={task} />
    <dt className="inline font-medium">Evidence: </dt><dd className="inline [overflow-wrap:anywhere]">Awaiting backend update; recorded facts unavailable.</dd>
    {blocker && <div>{blocker.label}: {blocker.detail}</div>}
    {task.status === 'running' && <div>Heartbeat: {age(task.last_heartbeat_at)} · liveness only</div>}
  </dl>
}

function ProgressFacts({ task }: { task: KanbanTask }) {
  const facts = task.card_facts!
  const progress = facts.progress
  const terminal = facts.dispatch?.terminal ?? ['done', 'archived'].includes(task.status)
  return <>
    {!terminal && <div title={progress?.basis}><dt className="inline font-medium">Progress: </dt><dd className="inline [overflow-wrap:anywhere]">{progress ? `${progress.text} · ${age(progress.at)}` : 'No worker update recorded'}</dd></div>}
    {task.status === 'running' && <div><dt className="inline font-medium">Heartbeat: </dt><dd className="inline">{age(facts.heartbeat_at ?? task.last_heartbeat_at)} · liveness only</dd></div>}
  </>
}

function TokenFacts({ task }: { task: KanbanTask }) {
  const tokens = task.card_facts!.tokens
  const current = tokens?.current
  const cumulative = tokens?.cumulative
  const currentLabel = current
    ? `${current.counters_complete ? '' : '≥'}${current.total.toLocaleString()} current run · ${current.input.toLocaleString()} in / ${current.output?.toLocaleString() ?? '?'} out · partial`
    : task.status === 'running' ? 'Current run unknown' : 'No active run'
  const cumulativeLabel = cumulative
    ? ` · ${cumulative.counters_complete ? '' : '≥'}${cumulative.total.toLocaleString()} cumulative recorded · partial`
    : ' · cumulative unknown'
  return <div title={tokens?.coverage}><dt className="inline font-medium">Recorded tokens: </dt><dd className="inline [overflow-wrap:anywhere]">{currentLabel}{cumulativeLabel}</dd></div>
}

function RouteFacts({ task }: { task: KanbanTask }) {
  const facts = task.card_facts!
  const run = facts.run_routes?.selected
  const queued = ['ready', 'todo', 'scheduled', 'triage'].includes(task.status)
  const plannedAgent = facts.run_routes?.planned_agent || task.assignee || 'default'
  return <>
    <div><dt className="inline font-medium">Agent: </dt><dd className="inline [overflow-wrap:anywhere]">{run ? `${run.agent} · run ${run.run_id}` : `${plannedAgent} · planned`}</dd></div>
    {run && <div title={run.basis}><dt className="inline font-medium">Last observed model: </dt><dd className="inline [overflow-wrap:anywhere]">{observedRouteLabel(run.last_observed)}{run.last_observed?.observed_at ? ` · ${age(run.last_observed.observed_at)}` : ''}</dd></div>}
    {queued && <div title={facts.model?.basis}><dt className="inline font-medium">Planned model: </dt><dd className="inline [overflow-wrap:anywhere]">{facts.model?.primary || 'Unknown'}{facts.model?.advisers?.length ? ` · advisers: ${facts.model.advisers.join(', ')}` : ''}</dd></div>}
  </>
}

function EstimateFact({ task }: { task: KanbanTask }) {
  const estimate = task.task_estimate
  if (!estimate?.ok) return null
  return <div title={estimate.rationale || undefined}><dt className="inline font-medium">Estimate: </dt><dd className="inline [overflow-wrap:anywhere]">~{estimate.est_tokens?.toLocaleString()} tokens{estimate.complexity ? ` · ${estimate.complexity}` : ''}{estimate.stale_scope ? ' · scope changed' : ''}</dd></div>
}

export function CardFacts({ task }: { task: KanbanTask }) {
  const facts = task.card_facts
  if (!facts) return <MissingCardFacts task={task} />
  const blocker = blockerFact(task) ?? (task.status === 'blocked'
    ? { label: 'Blocker unknown', detail: 'No explicit blocker evidence recorded' }
    : ['ready', 'todo', 'scheduled', 'triage'].includes(task.status)
      ? { label: 'Waiting', detail: 'Awaiting dispatch or preparation' } : null)
  // A finished card has a resolution, not progress. The backend withholds the
  // value for terminal statuses (plugin_api.get_board, kanban_card_facts) and the
  // row is skipped here too, so an older payload cannot re-introduce
  // "Progress: Reviewed and approved …" under a completed card.
  return (
    <dl aria-label="Task evidence" className="flex min-w-0 flex-col gap-1 border-t border-(--ui-stroke-tertiary) pt-1.5 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">
      <DispatchFacts task={task} />
      {blocker && <div><dt className="inline font-medium">{blocker.label}: </dt><dd className="inline [overflow-wrap:anywhere]">{blocker.detail}</dd></div>}
      <ProgressFacts task={task} />
      <TokenFacts task={task} />
      <EstimateFact task={task} />
      <RouteFacts task={task} />
      <div><DeliveryBadges receipt={facts.delivery} /></div>
    </dl>
  )
}
