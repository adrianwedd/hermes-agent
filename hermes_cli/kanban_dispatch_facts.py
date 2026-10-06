"""Passive dispatch reasons from native task flags, gates and dependency rows."""
import json

def dispatch_facts(conn, task):
    from hermes_cli.kanban_completion_workflow import operator_contract_pending
    from hermes_cli.kanban_card_facts import text
    tid=task['id'];status=task['status'];eligible=task.get('dispatch_eligible')
    fact={'eligible':eligible,'label':'Dispatch state unavailable','reason':'Backend did not serialize dispatch eligibility','owner':None,'next_action':None,'basis':'Native task row'}
    if status in ('done','archived'):
        fact.update(label='Not queued',reason='Native status is '+status);return fact
    if task.get('current_run_id') or task.get('claim_lock') or task.get('worker_pid'):
        fact.update(label='Active owner',reason='Native run or claim is held',owner=task.get('assignee'),next_action='Wait for the current owner’s handoff');return fact
    event=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='administrative_pending' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
    if event:
        try:payload=json.loads(event[1] or '{}')
        except (TypeError,ValueError):payload={}
        payload = payload if isinstance(payload, dict) else {}
        fact.update(label='Stopped · receipt administration',reason=text(payload.get('reason')) or 'Native administrative_pending event prevents worker dispatch',owner='operator',next_action='Bind retained completion evidence and perform native acceptance/closure; do not rerun work',basis='administrative_pending event '+str(event[0]));return fact
    if status == 'blocked':
        block = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        try:
            payload = json.loads(block[0] or '{}') if block else {}
        except (TypeError, ValueError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        fact.update(label='Blocked', reason=text(payload.get('reason')) or 'Resolve the native recorded blocker',
                    owner=task.get('assignee'), next_action='Resolve this exact prerequisite before re-admission',
                    basis='Native blocked stage and event')
        return fact
    if status == 'review':
        fact.update(label='Awaiting acceptance', reason='Review existing work against the original acceptance and delivery requirements',
                    owner='reviewer/operator', next_action='Accept retained evidence or request specific changes')
        return fact
    if operator_contract_pending(conn,tid):
        fact.update(label='Completion declaration required',reason='Native completion gate requires an operator contract',owner='operator',next_action='Declare completion requirements against retained work; do not rerun the worker',basis='Native operator_contract_pending gate');return fact
    from hermes_cli.kanban_completion_evidence import contract_record
    contract_event,contract=contract_record(conn,tid)
    if isinstance(contract,dict) and contract.get('qualified_for_dispatch') is False:
        fact.update(label='Preparation required',reason='Original scope and acceptance await operator qualification',owner='operator',next_action='Qualify the original scope and acceptance before enabling dispatch',basis='completion_requirements event '+str(contract_event));return fact
    if eligible is False:
        fact.update(label='Unresolved dispatch restriction',reason='dispatch_eligible=false; no named hold reason recorded',next_action='Inspect the eligibility decision before authorising dispatch');return fact
    dependencies=conn.execute("SELECT t.id,t.assignee FROM task_links l JOIN tasks t ON t.id=l.parent_id WHERE l.child_id=? AND t.status NOT IN ('done','archived') ORDER BY t.id",(tid,)).fetchall()
    if dependencies:
        fact.update(label='Waiting · dependency',reason='Unfinished prerequisite '+', '.join(r[0] for r in dependencies),owner=', '.join(sorted({r[1] for r in dependencies if r[1]})) or None,next_action='Finish the named prerequisite before dispatch',basis='Native dependency graph');return fact
    if eligible is True:
        label,reason,action={
            'ready':('Eligible · queued','Ready and dispatch_eligible=true; no per-card capacity refusal receipt is recorded','Await the dispatcher; capacity is not asserted without a receipt'),
            'todo':('Eligible · preparation pending','Native status is Todo','Complete preparation before Ready'),
            'triage':('Eligible · scope refinement','Native status is Triage','Refine original scope and acceptance before Ready'),
            'scheduled':('Eligible · scheduled','Native status is Scheduled','Wait for the native schedule'),
            'review':('Review pending','Native status is Review','Accept existing evidence or request specific changes'),
            'blocked':('Blocked · not dispatchable','Native status is Blocked; see recorded blocker','Resolve the recorded blocker'),
        }.get(status,('Eligible','dispatch_eligible=true; status '+status,None))
        fact.update(label=label,reason=reason,next_action=action)
    return fact
