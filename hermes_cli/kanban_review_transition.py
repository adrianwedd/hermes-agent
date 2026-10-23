"""Native review provenance and bounded correction metadata."""
import json


def active_independent_review(conn, task_id, run_id):
    """A live claim from Review remains a review after its status becomes Running."""
    if run_id is None:
        return False
    row = conn.execute(
        "SELECT r.profile,t.assignee,r.status,t.current_run_id FROM task_runs r "
        "JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND r.task_id=?",
        (run_id, task_id),
    ).fetchone()
    if row is None or row[2] != "running" or row[3] != run_id or row[0] != row[1]:
        return False
    claim = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    handoff = conn.execute(
        "SELECT payload,run_id FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if claim is None or handoff is None:
        return False
    claimed = json.loads(claim[0] or "{}")
    requested = json.loads(handoff[0] or "{}")
    return claimed.get("source_status") == "review" and requested.get("implementer") is not None and handoff[1] != run_id


def select_review_correction(conn, task_id, reason, run_id):
    """Preserve acceptance while selecting the reviewer's finite correction."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    cid, contract = contract_record(conn, task_id)
    validate_contract(contract)
    correction = dict(contract)
    action = "Apply only the reviewer-requested correction: " + reason
    remaining = list(contract.get("remaining_required_actions") or [])
    if action not in remaining:
        remaining.append(action)
    correction.update(
        remaining_required_actions=remaining,
        selected_next_action={"phase": "READY", "type": "review_correction", "action": action,
                              "review_run_id": run_id, "required_authority": None},
    )
    kb._append_event(conn, task_id, "completion_requirements", correction)
    kb._append_event(conn, task_id, "review_correction_selected", {
        "previous_contract_event": cid, "review_run_id": run_id, "action": action,
        "acceptance_changed": False,
    })
    conn.execute("UPDATE tasks SET dispatch_eligible=1 WHERE id=?", (task_id,))


def _phase_scope(contract):
    """Read a declared phase or the existing qualified bounded-action contract."""
    if not contract:
        return {}
    explicit = contract.get("review_scope") or {}
    if explicit:
        return explicit
    selected = contract.get("selected_next_action") or {}
    conditions = [c for c in contract.get("completion_conditions") or [] if not c.get("satisfied")]
    if (contract.get("qualified_for_dispatch") is not True or selected.get("phase") != "READY"
            or selected.get("type") not in {"offline_experiment_protocol", "review_correction"}
            or not selected.get("action") or not conditions
            or not all(c.get("requirement") and c.get("resolver") for c in conditions)):
        return {}
    requirements = "; ".join(c["requirement"] for c in conditions)
    resolvers = "; ".join(dict.fromkeys(c["resolver"] for c in conditions))
    return {"type": "bounded_phase", "action": selected["action"], "next_action": {
        "phase": "WAITING_FOR_AUTHORITY", "type": "remaining_conditions",
        "action": "Satisfy retained future conditions: " + requirements,
        "resolver": resolvers, "resume_condition": requirements}}


def bounded_review_goal(conn, task_id):
    """Only control-plane-declared phase review changes the review judge's target."""
    from hermes_cli.kanban_completion_evidence import contract_record
    _, contract = contract_record(conn, task_id)
    scope = _phase_scope(contract)
    if scope.get("type") == "bounded_phase" and scope.get("action") and contract.get("qualified_for_dispatch") is True:
        return "Review readiness of this authorised bounded phase only (whole goal remains nonterminal):\n" + scope["action"]
    return None


def accept_bounded_review(conn, task_id, summary, *, expected_run_id=None):
    """Accept an explicitly declared phase and wait; never mark the whole goal Done."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    from hermes_cli.kanban_decision import _operator_decision_pending
    cid, contract = contract_record(conn, task_id)
    scope = _phase_scope(contract)
    if scope.get("type") != "bounded_phase":
        return None
    task = kb.get_task(conn, task_id)
    review = task and (task.status == "review" or active_independent_review(conn, task_id, expected_run_id))
    next_action = scope.get("next_action") or {}
    if not review or not summary or not kb._parents_satisfied(conn, task_id) or _operator_decision_pending(conn, task_id):
        return False
    if next_action.get("phase") != "WAITING_FOR_AUTHORITY" or not all(next_action.get(k) for k in ["action", "resolver", "resume_condition"]):
        return False
    if expected_run_id is not None and task.current_run_id != expected_run_id:
        return False
    remaining = dict(contract)
    remaining["accepted_completed_actions"] = list(contract.get("accepted_completed_actions") or []) + ["Independent bounded phase accepted: " + scope["action"]]
    remaining["remaining_required_actions"] = [a for a in contract.get("remaining_required_actions") or [] if a != scope["action"]]
    remaining["selected_next_action"] = next_action
    remaining.pop("review_scope", None)
    validate_contract(remaining)
    with kb.write_txn(conn):
        current_cid, _ = contract_record(conn, task_id)
        if current_cid != cid or not kb._parents_satisfied(conn, task_id) or _operator_decision_pending(conn, task_id):
            return False
        changed = conn.execute("UPDATE tasks SET status='scheduled',dispatch_eligible=1,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,block_kind=NULL WHERE id=? AND status=? AND current_run_id IS ?", (task_id, task.status, task.current_run_id))
        if changed.rowcount != 1:
            return False
        kb._append_event(conn, task_id, "completion_requirements", remaining)
        run_id = kb._end_run(conn, task_id, outcome="completed", status="completed", summary="Bounded phase approved; whole goal remains waiting. " + summary)
        kb._append_event(conn, task_id, "phase_review_accepted", {"scope": scope["action"], "summary": summary, "whole_goal_completed": False, "future_conditions": contract.get("completion_conditions") or [], "next_action": next_action}, run_id=run_id)
    kb.recompute_ready(conn)
    return True
