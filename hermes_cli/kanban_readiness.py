"""Native prerequisites for execution; stages own waiting, not opaque flags."""
from pathlib import Path


def preparation_reason(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    row = conn.execute('SELECT workspace_path,status,idempotency_key FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return 'Unknown task'
    _, contract = contract_record(conn, task_id)
    guarded = (str(row[2] or '').startswith(('codex-goalpack-', 'workflow-programme-'))
               or isinstance(contract, dict) and 'qualified_for_dispatch' in contract
               or conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='blocked' AND json_valid(payload) AND json_extract(payload,'$.migration')='stage_owns_readiness' LIMIT 1", (task_id,)).fetchone() is not None)
    if not guarded:
        return ''  # Preserve existing intake until it emits explicit preparation contracts.
    try:
        validate_contract(contract)
    except ValueError:
        return 'Preparation required: declare the original-scope completion contract'
    if contract.get('qualified_for_dispatch') is not True:
        return 'Preparation required: operator qualification of original scope and acceptance'
    path = row[0]
    if not path or not Path(path).is_dir():
        return 'Preparation required: resolve the owned source or evidence workspace'
    target = Path(path).resolve()
    installed = Path(__file__).resolve().parents[1]
    if target == installed or installed in target.parents or target in installed.parents:
        return 'Preparation required: use an isolated workspace outside the live runtime'
    return ''


def migrate_disabled(conn, task_id, *, expected_status, expected_assignee, reason):
    """Replace an obsolete flag with a durable explicit blocked stage, atomically."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_administrative_hold import administrative_pending
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Exact blocker reason required')
    with kbc.write_txn(conn):
        t = kb.get_task(conn, task_id)
        if (not t or t.status != expected_status or t.assignee != expected_assignee
                or t.dispatch_eligible or t.current_run_id or t.worker_pid or t.claim_lock
                or t.status not in ('todo', 'ready', 'review', 'blocked') or administrative_pending(conn, task_id)):
            return False
        resume_review = t.status == 'review' or (t.status == 'blocked' and kb._resume_status_from_events(conn, task_id) == 'review')
        conn.execute("UPDATE tasks SET status='blocked',dispatch_eligible=1,block_kind='needs_input',block_recurrences=1 WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, 'blocked', {
            'kind': 'needs_input', 'reason': reason, 'source_status': t.status,
            'resume_status': 'review' if resume_review else 'todo',
            'migration': 'stage_owns_readiness', 'automatic_retry': False,
        })
        kb._append_event(conn, task_id, 'edited', {'fields': ['dispatch_eligible'], 'dispatch_eligible': True})
    kb.notify_task_updated(conn, task_id, ['status', 'dispatch_eligible', 'block_kind'])
    return True


def return_to_preparation(conn, task_id, *, expected_assignee, next_action):
    """Correct only this migration's routine preparation stages; preserve history."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_administrative_hold import administrative_pending
    import json
    with kbc.write_txn(conn):
        t=kb.get_task(conn,task_id)
        if (not t or t.status!='blocked' or t.assignee!=expected_assignee or t.current_run_id
                or t.worker_pid or t.claim_lock or administrative_pending(conn,task_id)):
            return False
        row=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
        try:payload=json.loads(row[0]) if row else {}
        except (TypeError,ValueError):return False
        if not isinstance(payload,dict) or payload.get('migration')!='stage_owns_readiness' or payload.get('source_status')!='todo':
            return False
        conn.execute("UPDATE tasks SET status='todo',block_kind=NULL,block_recurrences=0 WHERE id=?",(task_id,))
        kb._append_event(conn,task_id,'preparation_requested',{'owner':'bounded_preparation_controller','next_action':next_action,'source_status':'blocked','migration_correction':'routine_preparation_is_todo','source_blocker':payload.get('reason'),'automatic_worker_retry':False})
    kb.notify_task_updated(conn,task_id,['status','block_kind'])
    return True
