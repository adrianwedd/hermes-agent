"""Fail-closed compatibility for the retired generic Ready override.

A status gesture and a reason do not grant authority to revoke a STOP, waive a
prerequisite, qualify scope, reopen accepted work, or reclaim a worker. Callers
must use the specific evidence-bearing transition for the actual next action.
"""
from __future__ import annotations


def release(conn, task_id: str, *, reason: str) -> dict:
    """Return a truthful no-op only for an already runnable card."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import decide
    from hermes_cli.kanban_wait_transitions import require_operator

    require_operator(conn)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("A Ready override requires an explicit reason (--reason)")

    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    if task.current_run_id or task.worker_pid or task.claim_lock:
        raise PermissionError("A live worker owns this task; Ready cannot reclaim it")

    decision = decide(conn, task)
    if task.status != "ready" or not decision.get("dispatchable"):
        hold = requires_operator_release(conn, task_id) or decision.get("reason") or task.status
        raise PermissionError(
            f"Ready override cannot waive a STOP, wait, dependency, review, "
            f"terminal result or unqualified scope: {hold}. "
            "Satisfy the specific gate through its authorised transition"
        )

    return {
        "task_id": task_id, "released_gates": [], "waived_dependencies": [],
        "already_clear": True, "reopened_from": None, "reclaimed": None,
        "descendants_released": [], "descendants_held": [], "reason": reason,
        "applied": False, "ready": True, "workflow_stage": decision.get("workflow_stage"),
        "dispatchable": True, "status": task.status,
        "summary": "card already ready and dispatchable; no release performed",
    }


def requires_operator_release(conn, task_id: str) -> str | None:
    """Explain why a plain Ready gesture must be refused; never grant release."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import decide

    task = kb.get_task(conn, task_id)
    if task is None:
        return f"task {task_id} not found"
    if task.current_run_id or task.worker_pid or task.claim_lock:
        owner = []
        if task.worker_pid:
            owner.append(f"pid {task.worker_pid}")
        if task.claim_lock:
            owner.append(f"claim {task.claim_lock}")
        if task.current_run_id:
            owner.append(f"run {task.current_run_id}")
        return f"a live claim ({', '.join(owner)}) owns this card"
    if task.status == "review":
        return "pending review requires a review decision"

    decision = decide(conn, task)
    if decision.get("dispatchable"):
        return None
    if task.status == "blocked" and task.block_kind != "explicit_stop":
        from hermes_cli.kanban_wait_contract import facts
        from hermes_cli.kanban_decision import _dependency_state
        gates = (facts(conn, task_id) or {}).get("remaining_gates") or []
        dependencies = [item for item in _dependency_state(conn, task_id)
                        if not item["satisfied"] and not item.get("waived")]
        if not gates and not dependencies:
            # The native unblock_task transition handles an ungated block.
            return None
    stage = decision.get("workflow_stage")
    reason = decision.get("reason") or "no authorised worker action"
    return f"card is {task.status!r} ({stage!r}, dispatchable=False): {reason}"
