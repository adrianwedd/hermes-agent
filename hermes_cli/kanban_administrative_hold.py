"""Operator-only receipt-administration hold; never inferred from worker claims.

A substantive-complete task can still need evidence binding and native closure.
That is operator work, not an invitation to rerun its audit or implementation.
The append-only hold survives status/contract edits and terminal closure.
"""
from __future__ import annotations

import os


def administrative_pending(conn, task_id: str) -> bool:
    """Return whether the latest administrative-hold decision is pending.

    Holds are historical events, not permanent task identity.  An explicit
    ``administrative_pending_released`` event supersedes an earlier pending
    event without deleting its audit trail.
    """
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? "
        "AND kind IN ('administrative_pending','administrative_pending_released') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None and row[0] == "administrative_pending"


def set_administrative_pending(
    conn, task_id: str, *, expected_status: str, expected_assignee: str | None,
    expected_contract_event: int | None, reason: str, board: str | None = None,
) -> int:
    """Atomically record explicit operator intent and suppress all worker claims.

    Caller must freshly bind status, assignee and operator contract identity.
    Active ownership is never cleared; artifact validation/native DONE stay on
    their existing completion path. There is deliberately no automatic release.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("Workers cannot declare substantive-complete administrative holds")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("An explicit operator receipt-administration reason is required")
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_completion_evidence import contract_record

    with write_txn(conn):
        row = conn.execute(
            "SELECT status,assignee,current_run_id,worker_pid,claim_lock FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        cid, _ = contract_record(conn, task_id)
        if row is None or tuple(row[:2]) != (expected_status, expected_assignee) or cid != expected_contract_event:
            raise RuntimeError("Administrative hold snapshot changed; reread task and contract")
        if row[0] == "running" or any(x is not None for x in row[2:]):
            raise RuntimeError("Administrative hold cannot replace an active worker owner")
        conn.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "administrative_pending", {
            "version": 1, "scope": "receipt_administration",
            "operator_confirmed_substantive_complete": True,
            "completion_requirements_event": cid, "reason": reason.strip(),
            "dispatch_eligible": False,
        })
        event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    kb.notify_task_updated(conn, task_id, ["dispatch_eligible", "administrative_pending"], board=board)
    return event_id


def command(args) -> int:
    """Native operator CLI; freshness mismatch never rewrites task ownership."""
    from hermes_cli import kanban as cli
    try:
        with cli.kbc.connect_closing() as conn:
            eid = set_administrative_pending(
                conn, args.task_id, expected_status=args.expect_status,
                expected_assignee=args.expect_assignee,
                expected_contract_event=args.expect_contract_event,
                reason=args.reason,
            )
    except (PermissionError, ValueError, RuntimeError) as exc:
        return cli._err(str(exc))
    payload = {"task_id": args.task_id, "event_id": eid,
               "administrative_pending": True, "dispatch_eligible": False}
    if not cli._json_out(args, payload):
        print(f"Administrative-only hold recorded for {args.task_id}; no further workers")
    return 0
