import { useKanban } from './i18n'
import type { KanbanTask } from './types'

export function DispatchFacts({ task }: { task: KanbanTask }) {
  const k = useKanban().dispatch
  const fact = task.card_facts?.dispatch
  const label = fact?.label ?? (task.dispatch_eligible === false ? k.held : task.dispatch_eligible === true ? k.enabled : k.unavailable)
  const reason = fact?.reason ?? (task.dispatch_eligible === false ? k.disabledReason : task.dispatch_eligible === true ? k.enabledReason : k.missingReason)
  return <div aria-label={k.heading} className="min-w-0 text-xs leading-snug [overflow-wrap:anywhere]">
    <div className={task.dispatch_eligible === false ? 'font-medium text-destructive' : 'font-medium'}>{label}</div>
    <div>{reason}</div>
    {fact?.owner && <div>{k.owner(fact.owner)}</div>}
    {fact?.next_action && <div>{k.next(fact.next_action)}</div>}
  </div>
}
