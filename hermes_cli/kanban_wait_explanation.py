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
                         (selected, ('resume_condition',)), (scheduled, ('reason',))):
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
    condition = '\n'.join(clauses)
    return condition, 'Satisfy the recorded condition: ' + condition + '\n' + action
