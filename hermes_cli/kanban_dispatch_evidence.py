"""Bounded board-local dispatch receipts, not per-card tick events."""
from __future__ import annotations

import json
import time

# Retain transitions, not ticks. A long unchanged stall consumes one row;
# recovery cannot erase it on the next tick. Each board has its own database.
MAX_EPISODES = 128


def count_ready(conn):
    return int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0])


def add_reason(result, reason, count=1):
    result.suppression_reasons[reason] = result.suppression_reasons.get(reason, 0) + count


def admission_reason(decision):
    """Use bounded semantic codes, never operator prose as metric keys."""
    if not decision:
        return "task_disappeared"
    if decision.get("execution_health", {}).get("state") == "INFRASTRUCTURE_FAULT":
        return "admission:infrastructure_fault"
    if any(not d.get("satisfied") for d in decision.get("dependency_requirements", [])):
        return "admission:dependency"
    if any(not d.get("satisfied") for d in decision.get("external_conditions", [])):
        return "admission:external_condition"
    stage = str(decision.get("workflow_stage", "unknown")).lower()
    if stage not in {"done", "superseded", "running", "held", "blocked", "operator", "waiting",
                     "prepare", "triage", "review", "ready"}:
        stage = "unknown"
    return f"admission:{stage}"


def read_evidence(conn):
    """Newest first, including previous suppression after recovery."""
    rows = conn.execute("SELECT * FROM dispatch_evidence ORDER BY id DESC LIMIT ?", (MAX_EPISODES,))
    return [{**dict(row), "reasons": json.loads(row["reasons"])} for row in rows]


def reason_counts(result):
    """Include legacy buckets for callers constructing DispatchResult directly."""
    counts = dict(result.suppression_reasons)
    legacy = {}
    for _, reason in result.respawn_guarded:
        legacy[reason] = legacy.get(reason, 0) + 1
    for key in ("rate_limited", "skipped_unassigned", "skipped_nonspawnable",
                "skipped_per_profile_capped"):
        if getattr(result, key):
            legacy[key] = len(getattr(result, key))
    if result.skipped_locked:
        legacy["skipped_locked"] = 1
    if result.memory_pressure:
        legacy[f"memory_pressure:{result.memory_pressure}"] = 1
    for key, count in legacy.items():
        counts[key] = max(counts.get(key, 0), count)
    if result.ready_total and not result.spawned and not counts:
        counts["unknown"] = 1
    return counts


def finish_tick(conn, result, *, persist=True):
    """Finalize even early returns; evidence failure must not alter dispatch.

    Never persist dry runs or losing lock contenders. A failed/unavailable DB
    cannot store its own failure: that remains visible in the returned result.
    """
    try:
        result.ready_total = max(result.ready_total, count_ready(conn))
    except Exception:
        add_reason(result, "ready_read_failed")
    result.suppression_reasons = reason_counts(result)
    if not persist or result.skipped_locked:
        return result
    try:
        _record(conn, result)
    except Exception:
        add_reason(result, "evidence_write_failed")
    return result


def _record(conn, result):
    from hermes_cli import kanban_db as kb
    state = "spawned" if result.spawned else "suppressed" if result.suppression_reasons else "idle"
    reasons = json.dumps(result.suppression_reasons, sort_keys=True, separators=(",", ":"))
    # Counts may vary while the same gate holds. Preserve the latest counts,
    # coalescing by reason keys so a changing queue does not churn the history.
    signature = json.dumps(sorted(result.suppression_reasons)) if state == "suppressed" else "healthy"
    now = int(time.time())
    with kb.write_txn(conn):
        last = conn.execute("SELECT id, state, signature FROM dispatch_evidence ORDER BY id DESC LIMIT 1").fetchone()
        if last and last["signature"] == signature:
            conn.execute(
                "UPDATE dispatch_evidence SET state=?, last_seen=?, ticks=ticks+1, ready_total=?, "
                "spawned=?, reasons=? WHERE id=?",
                (state, now, result.ready_total, len(result.spawned), reasons, last["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO dispatch_evidence(state,signature,first_seen,last_seen,ticks,ready_total,spawned,reasons) "
                "VALUES(?,?,?,?,1,?,?,?)",
                (state, signature, now, now, result.ready_total, len(result.spawned), reasons),
            )
            conn.execute(
                "DELETE FROM dispatch_evidence WHERE id NOT IN "
                "(SELECT id FROM dispatch_evidence ORDER BY id DESC LIMIT ?)", (MAX_EPISODES,),
            )


def record_guard(conn, task_id, reason):
    """One task event per hold episode/reason/hour, alongside the board receipt."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind,payload,created_at FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if (last and last["kind"] == "respawn_guarded"
                and kb._json_dict(last["payload"]).get("reason") == reason
                and int(time.time()) - last["created_at"] < 3600):
            return
        kb._append_event(conn, task_id, "respawn_guarded", {"reason": reason})
