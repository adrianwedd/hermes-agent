"""Literal display of a retained native condition, without changing its authority."""


def gate_explanation(gate):
    """Project known attestation text; keep the persisted gate and predicate intact."""
    reason, action = gate['reason'], gate['current_unblock_action']
    if gate.get('evaluator') != 'attestation':
        return reason, action
    native = (gate.get('parameters') or {}).get('native_condition')
    if not isinstance(native, dict):
        return reason, action
    selected = native.get('selected_next_action') or {}
    scheduled = native.get('scheduled_payload') or {}
    clauses = []
    for record, keys in ((native, ('reason', 'requirement', 'resume_condition')),
                         (selected, ('resume_condition',)), (scheduled, ('reason', 'resume_condition'))):
        if not isinstance(record, dict):
            continue
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip() and value not in clauses:
                clauses.append(value)
    if not clauses and isinstance(selected, dict):
        value = selected.get('action')
        if isinstance(value, str) and value.strip():
            clauses.append(value)
    if not clauses:
        return reason, action
    clauses.extend('Recorded condition resolver: ' + value
                   for value in _recorded_resolvers((native, selected, scheduled), gate.get('resolver')))
    condition = '\n'.join(clauses)
    return condition, 'Satisfy the recorded condition: ' + condition + '\n' + action


def _recorded_resolvers(records, declared_resolver):
    resolvers = []
    for record in records:
        value = record.get('resolver') if isinstance(record, dict) else None
        if isinstance(value, str) and value.strip() and value != declared_resolver and value not in resolvers:
            resolvers.append(value)
    return resolvers


def native_unblock_fields(conn, task_id, decision, wait):
    """Report the existing operator transition for a proven block representation.

    Migration conservatively represents ordinary blocks as attestations, but
    that must not invent a receipt requirement when native unblock can retire
    the representation. Use the mutation boundary's origin/ownership/shape
    proof; neither a matching gate name nor wait-level provenance is enough.
    """
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if (task is None or task.status != "blocked" or task.block_kind == "explicit_stop"
            or decision.get("workflow_stage") in {"HELD", "OPERATOR"}
            or decision.get("administrative_pending")):
        return
    gate_ids = kb._releasable_block_representation(conn, task_id, wait)
    if not gate_ids:
        return
    action = (
        f"Operator: run hermes kanban --board <owning-board> unblock {task_id} "
        "to release the native block; dependencies and other conditions are rechecked. "
        "No new attestation is required for this migration-owned representation."
    )
    reason = "Native block awaits operator release through the supported unblock transition"
    decision.update(
        reason=reason, why_not_executable=reason, next_action=action,
        current_next_action=action, current_unblock_action=action,
        current_next_action_type="native_unblock", evaluation_mode="manual_transition",
        resume_condition={"transition": "unblock", "gate_ids": gate_ids},
        predicate={"evaluator": "native_unblock", "gate_ids": gate_ids},
        owner="operator", resolver="operator",
    )
