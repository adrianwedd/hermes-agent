"""Exact worker-session registration; never select a run from a title or cwd."""

import json
import os
import sqlite3


def stamp(conn, *, task_id, run_id, worker_pid, claim_lock, session_id, launch=None):
    """Stamp only the current live run owned by this worker PID and claim lock."""
    if (not all((task_id, session_id, claim_lock)) or type(run_id) is not int
            or type(worker_pid) is not int):
        return False
    row = conn.execute(
        "SELECT r.metadata FROM task_runs r JOIN tasks t ON t.id=r.task_id "
        "WHERE r.id=? AND r.task_id=? AND t.current_run_id=r.id "
        "AND t.status='running' AND r.ended_at IS NULL "
        "AND r.worker_pid=? AND t.worker_pid=? "
        "AND r.claim_lock=? AND t.claim_lock=?",
        (run_id, task_id, worker_pid, worker_pid, claim_lock, claim_lock),
    ).fetchone()
    if row is None:
        return False
    try:
        metadata = json.loads(row[0] or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(metadata, dict):
        return False
    existing = metadata.get("worker_session_id")
    if existing and existing != session_id:
        return False  # compression continuation has a separate additive receipt
    metadata["worker_session_id"] = session_id
    if "launch_route" not in metadata and isinstance(launch, dict):
        provider, model = launch.get("provider"), launch.get("model")
        if isinstance(provider, str) and provider and isinstance(model, str) and model:
            metadata["launch_route"] = {"provider": provider[:100], "model": model[:180]}
    encoded = json.dumps(metadata, separators=(",", ":"))
    changed = conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=? AND metadata IS ?",
        (encoded, run_id, row[0]),
    ).rowcount
    return changed == 1


def register_from_agent(agent):
    """Write a startup receipt for a dispatcher-owned worker after agent creation."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    raw_run = os.environ.get("HERMES_KANBAN_RUN_ID")
    claim = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    session_id = getattr(agent, "session_id", None)
    if not task_id or not raw_run or not claim or not isinstance(session_id, str) or not session_id:
        return False
    try:
        run_id = int(raw_run)
    except ValueError:
        return False
    from tools.kanban_tools import _board, _is_dispatcher_owned_worker
    if not _is_dispatcher_owned_worker():
        return False
    with _board(None, quiet_close=True) as (kb, conn):
        with kb.write_txn(conn):
            before = conn.execute(
                "SELECT metadata FROM task_runs WHERE id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
            if before is None:
                return False
            try:
                old = json.loads(before[0] or "{}")
            except (ValueError, TypeError):
                return False
            if not isinstance(old, dict):
                return False
            changed = stamp(
                conn, task_id=task_id, run_id=run_id, worker_pid=os.getpid(),
                claim_lock=claim, session_id=session_id,
                launch={"provider": getattr(agent, "provider", None),
                        "model": getattr(agent, "model", None)},
            )
            if not changed:
                return False
            if not old.get("worker_session_id"):
                registered = json.loads(conn.execute(
                    "SELECT metadata FROM task_runs WHERE id=?", (run_id,),
                ).fetchone()[0])
                # This immutable event survives terminal metadata replacement.
                kb._append_event(
                    conn, task_id, "worker_session_registered",
                    {"worker_session_id": session_id,
                     "launch_route": registered.get("launch_route")}, run_id=run_id,
                )
    return True


def registered_receipt(conn, task_id, run_id):
    """Earliest immutable startup receipt for this exact run."""
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind='worker_session_registered' ORDER BY id LIMIT 1",
            (task_id, run_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    try:
        value = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
