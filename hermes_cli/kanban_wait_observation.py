"""Allow-listed native observation receipts; no commands are read from card prose."""
import hashlib
import json

PROBE_NAMES = {'notebooklm_auth'}


def observation_gate(identity, selected, source_event_id):
    from hermes_cli.kanban_wait_contract import declaration
    name = selected.get('condition_probe')
    if name not in PROBE_NAMES:
        raise ValueError('Unregistered observation evaluator')
    return declaration(identity, 'observation', resolver='native observer:'+name,
                       parameters={'probe_name': name, 'condition_id': selected.get('condition_id'),
                                   'source_event_id': source_event_id, 'allow_invalidation': True, 'required_assertions': ['probe_succeeded']},
                       action='Observe '+name+' and retain its evidence', reason='Registered observation has not succeeded')


def _record_observation(conn, task_id, gate, observation):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_contract import gate_event
    from hermes_cli.kanban_wait_transitions import require_operator
    require_operator(conn)
    name = gate['parameters']['probe_name']
    if name not in PROBE_NAMES or observation.get('probe') != name or not isinstance(observation.get('exit_code'), int):
        raise ValueError('Observation does not satisfy its registered evaluator')
    kind = 'wait_gate_satisfied' if observation['exit_code'] == 0 else 'wait_gate_invalidated'
    previous = gate_event(conn, task_id, gate)
    if (previous and previous['kind'] == kind) or (kind == 'wait_gate_invalidated' and (not previous or not gate['parameters'].get('allow_invalidation'))):
        return False
    encoded = json.dumps(observation, sort_keys=True)
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        from hermes_cli.kanban_wait_contract import record
        _, _, current = record(conn, task_id)
        native_gate = next((g for g in (current or {}).get('gates', []) if g['id'] == gate['id']), None)
        if not native_gate or native_gate['revision'] != gate['revision'] or native_gate['declaration'] != gate['declaration']:
            raise RuntimeError('Observation gate declaration changed')
        if not task or task.status in {'done', 'archived', 'running'} or task.current_run_id or task.worker_pid or task.claim_lock:
            raise RuntimeError('Observation ownership changed')
        payload = {
            'gate_id': gate['id'], 'revision': gate['revision'], 'declaration': gate['declaration'],
            'authorised': True, 'actor': 'observer:'+name, 'evidence_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'evidence': observation, 'observation_mode': 'registered_probe'}
        if kind == 'wait_gate_invalidated':
            _, contract, _ = record(conn, task_id)
            payload['resume_snapshot'] = {'state': 'review' if task.status == 'review' else 'todo', 'selected_next_action': contract.get('selected_next_action')}
        kb._append_event(conn, task_id, kind, payload)
    kb.recompute_ready(conn)
    kb.notify_task_updated(conn, task_id, ['wait_contract', 'status', 'dispatch_eligible'])
    return True


def observe(conn, *, probes=None, board=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_resume import _PROBES
    from hermes_cli.kanban_wait_contract import facts
    from hermes_cli.kanban_wait_migration import migrate
    migrate(conn)
    registry = _PROBES if probes is None else probes
    observations = {};changed = []
    for row in conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived','running') AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL").fetchall():
        wait = facts(conn, row[0])
        for gate in (wait or {}).get('gates', []):
            name = gate['parameters'].get('probe_name')
            if gate['evaluator'] != 'observation' or name not in PROBE_NAMES or name not in registry:
                continue
            if name not in observations:
                observations[name] = registry[name]()
            evidence = observations[name]
            if isinstance(evidence, dict) and isinstance(evidence.get('exit_code'), int):
                try:
                    if _record_observation(conn, row[0], gate, evidence):
                        changed.append(row[0])
                except (RuntimeError, ValueError):
                    continue
    return changed
