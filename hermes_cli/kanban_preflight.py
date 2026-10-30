"""Live conditions a Kanban worker should read before it acts.

An old card is a request for work, not a description of the present. A worker
that knows the machine is loaded, that a sibling is already in the same
repository, or that its own brief predates the last restart can adapt — wait,
coordinate, re-scope — instead of colliding. Nothing here can refuse a task:
this is awareness, and awareness should change behaviour, not prevent it.
"""
from __future__ import annotations


def preflight(conn, task) -> str:
    """What is happening around this card right now.

    An old card is a request for work, not a description of the present. A worker
    that knows the machine is loaded, that a sibling is already in the same
    repository, or that its own brief predates the last three restarts can adapt
    (wait, coordinate, re-scope) instead of colliding. This is awareness, not a
    gate: nothing here can refuse the task.
    """
    import time as _time

    from hermes_cli.local_runtime import residency_gate

    lines = ["## Live conditions (read before acting; adapt rather than collide)"]
    facts = residency_gate.snapshot()
    if "error" in facts:
        lines.append(f"- Memory: unmeasurable ({facts['error']}). Do not start a large local model.")
    else:
        lines.append(
            f"- Memory: {facts['available_gib']:.0f} GiB available of {facts['total_gib']:.0f} GiB, "
            f"{facts['reserved_gib']:.0f} GiB already reserved to other work."
        )
        for holder, lease in (facts.get("leases") or {}).items():
            lines.append(f"  - reserved by {holder}: {lease.get('peak_gib')} GiB until {lease.get('expires_at')}")

    rows = conn.execute(
        "SELECT id, assignee, workspace_path FROM tasks WHERE status IN ('running','ready') AND id != ?",
        (task.id,),
    ).fetchall()
    if rows:
        lines.append("- Other work in flight:")
        for row in rows:
            lines.append(f"  - {row['id']} ({row['assignee'] or 'unassigned'}) in {row['workspace_path'] or 'no workspace'}")
        shared = [row["id"] for row in rows if row["workspace_path"] and row["workspace_path"] == task.workspace_path]
        if shared:
            lines.append(f"- **{', '.join(shared)} shares this workspace.** Serialise edits; do not stage or commit over another worker's changes.")

    last = conn.execute(
        "SELECT status, started_at, ended_at FROM task_runs WHERE task_id=? "
        "ORDER BY coalesce(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    if last is not None:
        age_h = int((_time.time() - (last["started_at"] or _time.time())) / 3600)
        lines.append(f"- This card's previous attempt: {last['status']}, started {age_h}h ago.")
    if task.created_at:
        card_age_days = int((_time.time() - task.created_at) / 86400)
        if card_age_days >= 2:
            lines.append(
                f"- This brief is {card_age_days} days old. Re-check that its instructions still describe "
                "reality (paths, model identities, branches, other cards) before executing them literally."
            )
    return "\n".join(lines)


