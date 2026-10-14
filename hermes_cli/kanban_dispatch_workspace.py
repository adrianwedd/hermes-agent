"""Preview the workspace owner exclusion without claiming a task."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def preview_workspace_conflict(conn: sqlite3.Connection, task_id: str, planned_spawns) -> bool:
    """Account for live claims and earlier candidates in this dry-run tick."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_persistent_workspace import workspace_owned_by_other

    task = kb.get_task(conn, task_id)
    if not task or not task.workspace_path:
        return False
    if workspace_owned_by_other(conn, task_id, task.workspace_path):
        return True
    target = Path(task.workspace_path).expanduser().resolve()
    for planned_id, _assignee, _workspace in planned_spawns:
        planned = kb.get_task(conn, planned_id)
        if not planned or not planned.workspace_path:
            continue
        other = Path(planned.workspace_path).expanduser().resolve()
        if target == other or target in other.parents or other in target.parents:
            return True
    return False
