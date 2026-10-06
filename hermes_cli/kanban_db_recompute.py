"""Canonical inactive-card reconciliation kept out of the legacy DB facade."""
from __future__ import annotations

import sqlite3
import time


def recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int:
    """Align inactive native rows with the canonical next-action decision."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract
    from hermes_cli.kanban_decision import decide

    if failure_limit is None:
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    changed = 0
    with kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id,status,consecutive_failures,max_retries FROM tasks "
            "WHERE status NOT IN ('done','archived','running') "
            "AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL"
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            ensure_scope_contract(conn, task_id, authority="control_plane_pre_promotion")
            current = row["status"]
            if current == "blocked" and kb._has_sticky_block(conn, task_id):
                continue
            task = kb.get_task(conn, task_id)
            override = kb._resume_status_from_events(conn, task_id) if current in {"todo", "blocked"} else None
            decision = decide(conn, task, status_override=override) if task else None
            if not decision:
                continue
            stage = decision["workflow_stage"]
            if stage == "DONE":
                result = task.result or "Accepted bounded contract satisfied"
                updated = conn.execute(
                    "UPDATE tasks SET status='done',result=?,completed_at=?,dispatch_eligible=0,"
                    "block_kind=NULL,block_recurrences=0 WHERE id=? AND status=? "
                    "AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL",
                    (result, int(time.time()), task_id, current),
                )
                if updated.rowcount:
                    kb._append_event(conn, task_id, "mechanically_completed", {
                        "decision_fingerprint": decision["decision_fingerprint"],
                        "accepted_completed_actions": decision["accepted_completed_actions"],
                        "remaining_required_actions": [],
                    })
                    changed += 1
                continue
            targets = {
                "TRIAGE": "triage", "PREPARE": "todo", "READY": "ready",
                "REVIEW": "review", "WAITING": "scheduled" if current == "scheduled" else "todo",
                "BLOCKED": "blocked",
            }
            target = targets.get(stage)
            desired_eligible = int(decision["dispatchable"] or stage == "WAITING")
            if target is None:
                continue
            if target == current:
                # Same-status eligibility repair is only safe for canonical
                # WAITING. READY/REVIEW may be temporarily non-dispatchable
                # because execution health is bad; persisting that overlay as
                # dispatch_eligible=0 would turn recovery into a stale hold.
                if stage != "WAITING":
                    continue
                if int(task.dispatch_eligible) == desired_eligible:
                    continue
                updated = conn.execute(
                    "UPDATE tasks SET dispatch_eligible=? WHERE id=? AND status=?",
                    (desired_eligible, task_id, current),
                )
                if updated.rowcount:
                    kb._append_event(conn, task_id, "decision_reconciled", {
                        "previous_status": current, "status": current,
                        "workflow_stage": stage,
                        "decision_fingerprint": decision["decision_fingerprint"],
                        "next_action": decision["next_action"],
                        "dispatch_eligible": bool(desired_eligible),
                    })
                    changed += 1
                continue
            if stage in {"READY", "REVIEW"} and not decision["dispatchable"]:
                continue
            if current == "blocked" and stage in {"READY", "REVIEW"}:
                failures = int(row["consecutive_failures"] or 0)
                task_limit = row["max_retries"]
                effective_limit = int(task_limit) if task_limit is not None else int(failure_limit)
                if failures >= effective_limit:
                    continue
            updated = conn.execute(
                "UPDATE tasks SET status=?,dispatch_eligible=? WHERE id=? AND status=?",
                # A WAITING decision is the active gate. Keep latent native
                # eligibility so satisfying that declared condition can
                # recompute directly to READY instead of sealing itself in a
                # stale Todo+disabled hold. The claim boundary still rejects
                # WAITING because it consumes this same canonical decision.
                (target, desired_eligible, task_id, current),
            )
            if updated.rowcount != 1:
                continue
            kb._append_event(conn, task_id, "decision_reconciled", {
                "previous_status": current,
                "status": target,
                "workflow_stage": stage,
                "decision_fingerprint": decision["decision_fingerprint"],
                "next_action": decision["next_action"],
            })
            changed += 1
    return changed
