"""Preview the workspace owner exclusion without claiming a task."""

from __future__ import annotations

import sqlite3


def preview_workspace_conflict(conn: sqlite3.Connection, task_id: str) -> bool:
    """Do not promise a claim the durable workspace guard would refuse."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_persistent_workspace import workspace_owned_by_other

    task = kb.get_task(conn, task_id)
    return bool(
        task and task.workspace_path and
        workspace_owned_by_other(conn, task_id, task.workspace_path)
    )
