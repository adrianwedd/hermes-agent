"""One bounded resolution attempt on an existing card, using native admission.

Operator grants bind retained evidence and full original acceptance. They permit
only offline resolution, never final live execution, and are consumed once.
"""
import hashlib
import json
import os
import re
from pathlib import Path


def input_digest(conn, task_id, attachment_ids):
    row = conn.execute('SELECT body,assignee,workspace_path,project_id,provider_override,model_override FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown task')
    inputs = []
    for aid in sorted(set(attachment_ids)):
        a = conn.execute('SELECT stored_path FROM task_attachments WHERE task_id=? AND id=?', (task_id, aid)).fetchone()
        if a is None:
            raise ValueError('Reassessment input must be an existing attachment on this card')
        inputs.append((aid, hashlib.sha256(Path(a[0]).read_bytes()).hexdigest()))
    # Only an actual operator/user input, not worker chatter, authorises another attempt.
    manual = conn.execute("SELECT max(id) FROM task_comments WHERE task_id=? AND author IN ('user','desktop','operator')", (task_id,)).fetchone()[0]
    return hashlib.sha256(json.dumps({'scope':tuple(row),'inputs':inputs,'operator_input':manual},sort_keys=True).encode()).hexdigest()


def protected(conn, task_id):
    from hermes_cli.kanban_administrative_hold import administrative_pending
    ancestors = conn.execute('WITH RECURSIVE a(id) AS (SELECT ? UNION SELECT l.parent_id FROM task_links l JOIN a ON l.child_id=a.id) SELECT id FROM a', (task_id,)).fetchall()
    for r in ancestors:
        if administrative_pending(conn, r[0]):
            return 'explicit_administrative_no_rerun'
        t = conn.execute('SELECT title,dispatch_eligible FROM tasks WHERE id=?', (r[0],)).fetchone()
        if t and str(t[0]).startswith('HOLD reference'):
            return 'explicit_parked_reference'
        for c in conn.execute("SELECT body FROM task_comments WHERE task_id=? AND author IN ('user','desktop','operator')", (r[0],)):
            if re.search(r'^\s*STOP\s*$', c[0] or '', re.M):
                return 'explicit_user_stop'
    return ''


def apply_grant(conn, task_id, *, facts_loader=None):
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise PermissionError('Workers cannot qualify their own reassessment')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_provider_admission as admission
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    from hermes_cli.kanban_preparation import workspace_busy
    with admission.host_lock(kb.kanban_home()) as locked:
        if not locked:
            return {'prepared':False,'reason':'provider_admission_busy'}
        t = kb.get_task(conn,task_id)
        if not t or t.status not in ('todo','blocked') or t.current_run_id or t.worker_pid or t.claim_lock:
            return {'prepared':False,'reason':'stage_or_active_owner'}
        cid, contract = contract_record(conn,task_id)
        validate_contract(contract)
        grant = contract.get('reassessment',{})
        if not grant:
            return {'prepared':False,'reason':'explicit_resolution_grant_required'}
        if not isinstance(grant,dict) or not isinstance(grant.get('input_attachment_ids'),list) or any(type(x) is not int or x<=0 for x in grant.get('input_attachment_ids',[])) or not isinstance(grant.get('input_digest'),str) or not re.fullmatch(r'[0-9a-f]{64}',grant.get('input_digest','')) or not all(isinstance(grant.get(k),str) and grant[k].strip() for k in ('next_action','external_prerequisite')):
            return {'prepared':False,'reason':'malformed_resolution_grant'}
        if grant.get('mode')!='offline_resolution' or grant.get('state')!='pending' or not grant.get('next_action') or not grant.get('external_prerequisite'):
            return {'prepared':False,'reason':'explicit_resolution_grant_required'}
        why = protected(conn,task_id)
        if why:
            return {'prepared':False,'reason':why}
        if not t.dispatch_eligible or contract.get('qualified_for_dispatch') is not False:
            return {'prepared':False,'reason':'manual_dispatch_hold_or_already_qualified'}
        digest = input_digest(conn,task_id,grant.get('input_attachment_ids',[]))
        if digest!=grant.get('input_digest'):
            return {'prepared':False,'reason':'retained_inputs_or_scope_changed'}
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='reassessment_qualified' AND json_extract(payload,'$.input_digest')=?",(task_id,digest)).fetchone():
            return {'prepared':False,'reason':'unchanged_inputs_already_reassessed'}
        if not kb._parents_satisfied(conn,task_id):
            return {'prepared':False,'reason':'next_action_dependency_not_satisfied'}
        if facts_loader:
            lane, isolated = facts_loader(conn,t)
        else:
            path = Path(t.workspace_path or '').expanduser()
            installed = Path(__file__).resolve().parents[1]
            isolated = bool(t.workspace_path and path.is_dir() and path.resolve()!=installed and installed not in path.resolve().parents and path.resolve() not in installed.parents and not workspace_busy(conn,task_id,path))
            lane = admission.task_lane(t)
        if lane!='cloud' or not isolated:
            return {'prepared':False,'reason':'cloud_route_or_workspace_unqualified'}
        with kbc.write_txn(conn):
            now=kb.get_task(conn,task_id)
            if now!=t or contract_record(conn,task_id)[0]!=cid or input_digest(conn,task_id,grant.get('input_attachment_ids',[]))!=digest:
                return {'prepared':False,'reason':'snapshot_changed'}
            why=protected(conn,task_id)
            if why:
                return {'prepared':False,'reason':why}
            if not kb._parents_satisfied(conn,task_id):
                return {'prepared':False,'reason':'next_action_dependency_not_satisfied'}
            if facts_loader:
                final_lane,final_isolated=facts_loader(conn,now)
            else:
                path=Path(now.workspace_path or '').expanduser();installed=Path(__file__).resolve().parents[1]
                final_lane=admission.task_lane(now)
                final_isolated=bool(now.workspace_path and path.is_dir() and path.resolve()!=installed and installed not in path.resolve().parents and path.resolve() not in installed.parents and not workspace_busy(conn,task_id,path))
            if final_lane!='cloud' or not final_isolated:
                return {'prepared':False,'reason':'cloud_route_or_workspace_unqualified'}
            max_run_id=conn.execute('SELECT COALESCE(max(id),0) FROM task_runs WHERE task_id=?',(task_id,)).fetchone()[0]
            qualified=dict(contract,qualified_for_dispatch=True,reassessment=dict(grant,state='consumed',source_contract_event=cid,previous_max_run_id=max_run_id))
            kb._append_event(conn,task_id,'completion_requirements',qualified)
            # Preserve body, comments, links, project, route, owner and failure history.
            conn.execute("UPDATE tasks SET status='todo',block_kind=NULL,max_runtime_seconds=CASE WHEN max_runtime_seconds>0 THEN MIN(max_runtime_seconds,1800) ELSE 1800 END,max_retries=0,goal_mode=1,goal_max_turns=CASE WHEN goal_max_turns>0 THEN MIN(goal_max_turns,24) ELSE 24 END WHERE id=?",(task_id,))
            kb._append_event(conn,task_id,'reassessment_qualified',{'input_digest':digest,'next_action':grant['next_action'],'external_prerequisite':grant['external_prerequisite'],'source_contract_event':cid,'models_called':0,'new_cards':0,'max_turns':24,'max_runtime_seconds':1800})
        kb.recompute_ready(conn)
        kb.notify_task_updated(conn,task_id,['status','completion_requirements'])
        return {'prepared':True,'task_id':task_id,'status':kb.get_task(conn,task_id).status,'input_digest':digest}


def append_context(lines,conn,task_id):
    from hermes_cli.kanban_completion_evidence import contract_record
    _,c=contract_record(conn,task_id)
    g=c.get('reassessment',{}) if isinstance(c,dict) else {}
    if not isinstance(g,dict) or g.get('state')!='consumed':
        return
    lines.extend(['','## Bounded offline blocker resolution','Consume only the retained inputs for this existing card. Do not redo accepted work, create follow-up cards, run local inference/experiments, publish, change live services/config/credentials, or waive original final acceptance.', 'Next action: '+str(g['next_action']), 'Protected final prerequisite: '+str(g['external_prerequisite']), 'Return a concrete evidence-backed resolution/handoff, or one exact needs_input blocker with evidence, owner and required input. No unchanged review loops. Original completion criteria still govern DONE.'])


def prepare_tick(conn, *, facts_loader=None):
    """Consume at most one existing resolution grant, with deduplicated refusals."""
    from hermes_cli import kanban_db as kb,kanban_db_connect as kbc
    rows=conn.execute("SELECT t.id FROM tasks t JOIN task_events e ON e.id=(SELECT max(id) FROM task_events WHERE task_id=t.id AND kind='completion_requirements') WHERE t.status IN ('todo','blocked') AND json_valid(e.payload) AND json_extract(e.payload,'$.reassessment.state')='pending' ORDER BY COALESCE((SELECT max(id) FROM task_events WHERE task_id=t.id AND kind='reassessment_checked'),0),t.priority DESC,t.created_at,t.id LIMIT 64").fetchall()
    for r in rows:
        try:
            result=apply_grant(conn,r[0],facts_loader=facts_loader)
        except (ValueError,OSError) as exc:
            result={'prepared':False,'reason':str(exc)}
        if result['prepared']:
            return 1
        payload=json.dumps({'reason':result['reason'],'contract_event':conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='completion_requirements'",(r[0],)).fetchone()[0]},sort_keys=True)
        with kbc.write_txn(conn):
            kb._append_event(conn,r[0],'reassessment_checked',{'reason':result['reason']})
            last=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='reassessment_refused' ORDER BY id DESC LIMIT 1",(r[0],)).fetchone()
            if not last or last[0]!=payload:
                kb._append_event(conn,r[0],'reassessment_refused',json.loads(payload))
    return 0
