"""Atomic resume-condition transitions for known WAITING/BLOCKED actions."""
from __future__ import annotations

import os
from typing import Any


def satisfy_resume_condition(
    conn, task_id: str, *, condition: str, evidence: dict[str, Any],
    next_action: str, expected_status: str | None = None,
    expected_decision_fingerprint: str | None = None, board: str | None = None,
) -> dict[str, Any]:
    """Clear a satisfied wait and enable its existing action in one transaction.

    The caller supplies current deterministic evidence (for example a successful
    authenticated CLI check).  The transition rolls back unless the resulting
    canonical decision is READY and claimable; status and dispatch eligibility
    therefore cannot split into another impossible hybrid.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("Workers cannot self-certify external resume conditions")
    if not condition.strip() or not next_action.strip() or not isinstance(evidence, dict):
        raise ValueError("condition, evidence, and next_action are required")

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_decision import decide
    from hermes_cli.kanban_completion_evidence import contract_record

    with write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError("unknown task")
        if expected_status is not None and task.status != expected_status:
            raise RuntimeError("resume snapshot changed; reread task")
        if task.status in {"done", "archived", "running"} or any(
            value is not None for value in (task.current_run_id, task.worker_pid, task.claim_lock)
        ):
            raise RuntimeError("resume requires an inactive nonterminal task")
        before = decide(conn, task)
        if (expected_decision_fingerprint is not None and
                before["decision_fingerprint"] != expected_decision_fingerprint):
            raise RuntimeError("resume decision fingerprint changed; reread task")

        contract_id, contract = contract_record(conn, task_id)
        if isinstance(contract, dict):
            updated = dict(contract)
            updated["selected_next_action"] = {
                "phase": "READY", "type": "worker_action", "action": next_action.strip(),
                "required_authority": None,
            }
            kb._append_event(conn, task_id, "completion_requirements", updated)
            new_contract_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            new_contract_id = contract_id

        cur = conn.execute(
            "UPDATE tasks SET status='ready',dispatch_eligible=1,block_kind=NULL,"
            "block_recurrences=0,consecutive_failures=0,last_failure_error=NULL "
            "WHERE id=? AND status=? AND current_run_id IS NULL AND worker_pid IS NULL "
            "AND claim_lock IS NULL",
            (task_id, task.status),
        )
        if cur.rowcount != 1:
            raise RuntimeError("resume snapshot changed during transition")
        kb._append_event(conn, task_id, "resume_condition_satisfied", {
            "condition": condition.strip(), "evidence": evidence,
            "previous_stage": before["workflow_stage"], "new_stage": "READY",
            "next_action": next_action.strip(), "worker_executable_now": True,
            "dispatch_eligible": True, "source_contract_event": contract_id,
            "ready_contract_event": new_contract_id,
        })
        after = decide(conn, kb.get_task(conn, task_id))
        if not (after["workflow_stage"] == "READY" and after["dispatchable"] and
                after["worker_executable_now"]):
            raise RuntimeError(
                "resume evidence did not produce an authorised worker-executable READY action: "
                + after["reason"]
            )
    kb.notify_task_updated(
        conn, task_id,
        ["status", "dispatch_eligible", "block_kind", "resume_condition", "completion_requirements"],
        board=board,
    )
    return after

