"""Atomic resume-condition transitions for known WAITING/BLOCKED actions."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any


def satisfy_resume_condition(
    conn, task_id: str, *, condition: str, evidence: dict[str, Any],
    next_action: str, expected_status: str | None = None,
    expected_decision_fingerprint: str | None = None, board: str | None = None,
    condition_id: str | None = None,
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
        if condition_id:
            kb._append_event(conn, task_id, "external_condition_satisfied", {
                "condition_id": condition_id, "evidence": evidence,
                "evidence_identity": evidence.get("evidence_identity"),
                "source_contract_event": contract_id,
                "ready_contract_event": new_contract_id,
            })
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


def _notebooklm_auth_probe() -> dict[str, Any] | None:
    """Return bounded non-secret evidence when the configured NLM login is valid."""
    binary = shutil.which("nlm")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "login", "--check"], capture_output=True, text=True,
            timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout + "\n" + result.stderr)[-4000:]
    count = re.search(r"\b(\d+)\s+notebooks?\b", output, re.IGNORECASE)
    profile = re.search(r"\bprofile\s*[:=]\s*([^\s,]+)", output, re.IGNORECASE)
    return {
        "probe": "notebooklm_auth", "exit_code": 0,
        "profile": profile.group(1) if profile else "default",
        "notebooks_visible": int(count.group(1)) if count else None,
    }


_PROBES = {"notebooklm_auth": _notebooklm_auth_probe}


def observe_resume_conditions(
    conn, *, board: str | None = None,
    probes: dict[str, Any] | None = None,
) -> list[str]:
    """Observe supported deterministic conditions and atomically wake existing cards.

    This is a control-plane probe, not a model reassessment.  Only explicitly
    typed, allow-listed probes run; arbitrary commands in card data are never
    executed.  Snapshot/fingerprint checks make a concurrent edit a safe no-op.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_evidence import contract_record
    from hermes_cli.kanban_decision import decide

    registry = probes or _PROBES
    resumed: list[str] = []
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status NOT IN ('done','archived','running') "
        "AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL"
    ).fetchall()
    for row in rows:
        task = kb.get_task(conn, row[0])
        if task is None:
            continue
        decision = decide(conn, task)
        if decision["workflow_stage"] != "WAITING":
            continue
        _, contract = contract_record(conn, task.id)
        selected = contract.get("selected_next_action") if isinstance(contract, dict) else None
        if not isinstance(selected, dict):
            continue
        probe_name = selected.get("condition_probe")
        next_action = selected.get("resume_next_action")
        condition_id = selected.get("condition_id")
        if not isinstance(probe_name, str) or probe_name not in registry or not next_action:
            continue
        evidence = registry[probe_name]()
        if not isinstance(evidence, dict):
            continue
        try:
            satisfy_resume_condition(
                conn, task.id,
                condition=str(selected.get("resume_condition") or probe_name),
                evidence=evidence, next_action=str(next_action),
                expected_status=task.status,
                expected_decision_fingerprint=decision["decision_fingerprint"],
                board=board,
                condition_id=str(condition_id) if condition_id else None,
            )
        except RuntimeError:
            continue
        resumed.append(task.id)
    return resumed
