"""Explicit decomposition authority and durable, idempotent refusal receipts."""
import json


def boundary_refusal(requirement):
    """Authority, gates and explicit negatives veto either declaration source."""
    if requirement.get('required_authority') or requirement.get('type') in {'authority_grant', 'operator_decision'}:
        return 'decomposition_authority_required'
    if requirement.get('gate_id') or requirement.get('type') in {'gate', 'external_condition', 'wait'}:
        return 'decomposition_gate_required'
    if requirement.get('decomposable') is False or requirement.get('worker_executable') is False:
        return 'decomposition_explicitly_disallowed'
    return None


def requirement_refusal(requirement):
    """A positive worker declaration never overrides a gate or authority boundary."""
    boundary = boundary_refusal(requirement)
    if boundary:
        return boundary
    if requirement.get('decomposable') is not True and requirement.get('worker_executable') is not True:
        return 'decomposition_safety_not_declared'
    return None


def decision_boundary_refusal(decision):
    """Check both the retained selection and authority added by decision projection."""
    return boundary_refusal(decision.get('selected_next_action') or {}) or boundary_refusal({
        'required_authority': decision.get('required_authority'),
        'type': decision.get('current_next_action_type'),
    })


def check_and_record(conn, task_id):
    """Caller holds the write transaction: fresh decision and receipt are atomic.

    Also used at both graph/single persistence boundaries after slow inference.
    Unchanged polling compares structured facts, never a timestamp or prose title.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import auto_decompose_allowed
    task = kb.get_task(conn, task_id)
    if task is None:
        return False, 'task_missing'
    if task.status != 'triage' or task.current_run_id or task.worker_pid or task.claim_lock:
        return False, 'task_not_inactive_triage'
    allowed, decision = auto_decompose_allowed(conn, task)
    if allowed:
        return True, ''
    remaining = decision.get('remaining_required_actions') or []
    requirement = remaining[0] if remaining and isinstance(remaining[0], dict) else {}
    selected = decision.get('selected_next_action') or {}
    boundary = decision_boundary_refusal(decision)
    reason = boundary or requirement_refusal(requirement) or 'canonical_decision_forbids_decomposition'
    gates = decision.get('remaining_gates') or []
    if gates:
        reason = 'decomposition_gate_required'
    elif not boundary and decision['workflow_stage'] != 'TRIAGE':
        reason = 'canonical_decision_forbids_decomposition'
    payload = {
        'reason': reason,
        'requirement': remaining[0] if remaining else None,
        'selected_next_action': selected,
        'requirement_id': requirement.get('requirement_id') or requirement.get('evidence_identity') or task_id + ':scope',
        'gate_id': gates[0]['id'] if gates else selected.get('gate_id') or requirement.get('gate_id'),
        'gates': gates,
        'required_authority': decision.get('required_authority') or selected.get('required_authority') or requirement.get('required_authority'),
        'workflow_stage': decision['workflow_stage'],
        'decision_reason': decision['reason'],
        'resolution': {'owner': 'operator/specifier', 'command': 'hermes kanban specify ' + task_id,
                       'instruction': 'Clarify the bounded requirement and record authority; specifying does not waive a gate.'},
    }
    previous = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='auto_decompose_skipped' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if previous is None or json.loads(previous[0]) != payload:
        kb._append_event(conn, task_id, 'auto_decompose_skipped', payload)
    return False, reason
