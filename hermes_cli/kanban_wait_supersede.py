"""Explicit operator correction of an erroneous gate, not satisfaction evidence."""
import copy


def supersede_gate(conn, task_id, *, contract_event_id, gate_id, revision,
                   declaration, reason, selected_next_action, assignee):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_evidence import validate_contract
    from hermes_cli.kanban_wait_contract import record, validate
    from hermes_cli.kanban_wait_transitions import require_operator
    require_operator(conn)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Supersession requires an explicit correction reason')
    if (not isinstance(selected_next_action, dict)
            or selected_next_action.get('phase') != 'READY'
            or selected_next_action.get('type') != 'worker_action'
            or not str(selected_next_action.get('action') or '').strip()
            or selected_next_action.get('required_authority') is not None
            or not isinstance(assignee, str) or not assignee.strip()):
        raise ValueError('Declare the current authorised worker action and owner')
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if (task is None or task.status in {'running', 'done', 'archived'}
                or task.current_run_id or task.worker_pid or task.claim_lock):
            raise RuntimeError('Supersession requires an inactive nonterminal task')
        if task.block_kind == 'explicit_stop':
            raise PermissionError('STOP requires explicit authorised revocation')
        cid, contract, wait = record(conn, task_id)
        if cid != contract_event_id:
            raise ValueError('Requirements changed; reread before superseding')
        if wait is None:
            raise ValueError('No current wait contract to supersede')
        validate(wait)
        gate = next((g for g in wait['gates'] if g['id'] == gate_id), None)
        if not gate or gate['revision'] != revision or gate['declaration'] != declaration:
            raise ValueError('Wrong gate identity or stale revision/declaration')
        if gate['evaluator'] not in {'attestation', 'unsupported'}:
            raise PermissionError('Protected STOP, decisions and automatic predicates cannot be superseded')
        latest_block = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'", (task_id,)).fetchone()[0]
        if latest_block != wait.get('owned_block_event'):
            raise ValueError('A newer blocker owns this task; reread')
        updated = copy.deepcopy(contract)
        updated['selected_next_action'] = copy.deepcopy(selected_next_action)
        # Preserve scope, criteria, accepted evidence, future conditions and other gates.
        kept = [g for g in wait['gates'] if g['id'] != gate_id]
        if kept:
            updated['wait_contract']['gates'] = kept
            updated['wait_contract']['continuation'] = {'state': 'todo', 'selected_next_action': copy.deepcopy(selected_next_action)}
            validate(updated['wait_contract'])
        else:
            updated.pop('wait_contract', None)
        validate_contract(updated)
        kb._append_event(conn, task_id, 'wait_gate_superseded', {
            'actor': 'operator', 'reason': reason, 'gate': gate,
            'source_contract_event': cid, 'previous_continuation': wait['continuation'],
            'selected_next_action': selected_next_action, 'assignee': assignee,
            'condition_satisfied': False, 'acceptance_changed': False,
        })
        kb._append_event(conn, task_id, 'completion_requirements', updated)
        conn.execute("UPDATE tasks SET status='todo', assignee=?, block_kind=NULL, dispatch_eligible=1 WHERE id=?", (assignee, task_id))
        kb._append_event(conn, task_id, 'unblocked', {'status': 'todo', 'reason': reason, 'transition': 'gate_supersession'})
    kb.recompute_ready(conn)
    kb.notify_task_updated(conn, task_id, ['wait_contract', 'status', 'assignee'])
    from hermes_cli.kanban_decision import decide
    return decide(conn, kb.get_task(conn, task_id))
