"""Operator-owned workspace policy shared by every Kanban entry point."""
import contextlib
import sqlite3
from pathlib import Path


def persistent_root() -> Path | None:
    """Read the board owner's policy, including in another profile's worker."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.kanban_db import kanban_home

    config = load_user_config_effective(kanban_home() / "config.yaml", fail_closed=True)
    value = config.get("kanban", {}).get("persistent_workdir")
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("kanban.persistent_workdir must be an absolute directory")
    return root.resolve()


def creation_workspace(kind, path, project_id, branch_name):
    """Return the configured local-task directory, or None when policy is unset."""
    root = persistent_root()
    if root is None:
        return None
    if kind not in (None, "dir") or branch_name:
        raise ValueError("Kanban uses canonical repositories or persistent directories; scratch, worktree and branch are disabled")
    if project_id:
        return None
    target = Path(path).expanduser() if path else root
    if not target.is_absolute() or not target.resolve().is_relative_to(root):
        raise ValueError(f"Kanban workspace must be an ordinary directory under {root}")
    return str(target.resolve())


def creation_defaults(kind, path, project_id, source_task_id, branch_name, board):
    """Choose a canonical repository or the configured local-task directory."""
    from hermes_cli import kanban_db as kb

    root = persistent_root()
    if root is None:
        # Legacy behaviour when the operator has not enabled this policy.
        if project_id is None and kind != "scratch":
            board_project = kb._board_meta_for(board).get("project_id")
            project_id = (board_project.strip() or None) if isinstance(board_project, str) else None
        return "scratch" if kind is None else kind, path, project_id, source_task_id

    if kind not in (None, "dir") or branch_name:
        raise ValueError(
            "Kanban uses canonical repositories or persistent directories; "
            "scratch, worktree and branch are disabled"
        )
    # None inherits an explicitly configured board project. Empty string is the
    # supported opt-out for a local-only task.
    if project_id is None:
        board_project = kb._board_meta_for(board).get("project_id")
        project_id = (board_project.strip() or None) if isinstance(board_project, str) else None
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if project_id:
        return "dir", path, project_id, source_task_id
    persistent = creation_workspace(kind, path, None, branch_name)
    return "dir", persistent, "", None


def resolve_new_task_workspace(
    conn, workspace_kind, workspace_path, project_id, project_source_task_id,
    branch_name, board,
):
    """Resolve and validate the workspace persisted for a newly inserted task.

    This performs no writes and opens no Kanban transaction, so atomic graph
    builders can use the same canonical-project and local-directory policy as
    ordinary creation inside their one outer transaction.
    """
    from hermes_cli import kanban_db as kb

    workspace_kind, workspace_path, project_id, project_source_task_id = creation_defaults(
        workspace_kind, workspace_path, project_id, project_source_task_id, branch_name, board,
    )
    if workspace_kind not in kb.VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(kb.VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    workspace_path = kb._normalize_workspace_path(workspace_path)
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    project_id, project_obj, project_repo, workspace_kind = kb._resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path,
    )
    if workspace_kind == "dir" and project_repo is not None:
        workspace_path = project_repo
    # Apply the legacy board directory before checking the pair. Persistent
    # local tasks already have their configured root, and project tasks have
    # their canonical repository at this point.
    if workspace_path is None and project_repo is None and workspace_kind in {"dir", "worktree"}:
        board_default = kb._board_meta_for(board).get("default_workdir")
        if board_default:
            workspace_path = str(board_default)
    kb._validate_workspace_pair(workspace_kind, workspace_path)
    return workspace_kind, workspace_path, branch_name, project_id, project_obj, project_repo


def execution_workspace(task) -> Path | None:
    """Preserve legacy work in place without creating scratch dirs or worktrees."""
    root = persistent_root()
    if root is None:
        return None
    if task.workspace_path:
        path = Path(task.workspace_path).expanduser()
        if not path.is_absolute():
            raise ValueError("Kanban workspace must be absolute")
        if not path.is_dir():
            # A missing legacy path must not be recreated as an empty replacement
            # for existing work. New directory tasks may create children of root.
            if task.workspace_kind != "dir" or not path.resolve().is_relative_to(root):
                raise ValueError(f"Existing Kanban workspace is unavailable: {path}")
    else:
        path = root
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_owned_by_other(conn, task_id: str, path: str | Path) -> bool:
    """Whether another live claim owns an overlapping workspace, fail closed."""
    from hermes_cli.kanban_db_dispatch import _worker_alive
    from hermes_cli.kanban_db_workspace import _sibling_board_db_files

    target = Path(path).expanduser().resolve()

    def overlaps(value) -> bool:
        if not value:
            return False
        other = Path(value).expanduser().resolve()
        return target == other or target in other.parents or other in target.parents

    def occupied(database, *, current: bool) -> bool:
        rows = database.execute(
            "SELECT id, workspace_path FROM tasks WHERE workspace_path IS NOT NULL "
            "AND (status = 'running' OR current_run_id IS NOT NULL "
            "OR worker_pid IS NOT NULL OR claim_lock IS NOT NULL)"
        )
        for row in rows:
            if (not current or row[0] != task_id) and overlaps(row[1]):
                return True
        runs = database.execute(
            "SELECT t.id, t.workspace_path, r.worker_pid, r.worker_started_at "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.worker_pid IS NOT NULL"
        )
        for row in runs:
            if (not current or row[0] != task_id) and overlaps(row[1]) and _worker_alive(row[2], row[3]):
                return True
        return False

    try:
        if occupied(conn, current=True):
            return True
        for db_file in _sibling_board_db_files(conn):
            with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, timeout=1) as other:
                if occupied(other, current=False):
                    return True
    except (OSError, sqlite3.Error):
        return True
    return False


@contextlib.contextmanager
def _workspace_claim_lock(home: Path):
    """Serialize the cross-board ownership check with the claim commit."""
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    path = home / "kanban" / ".workspace-ownership.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if not _try_acquire_file_lock(handle):
            yield False
            return
        try:
            yield True
        finally:
            _release_file_lock(handle)


def workspace_claim_guard(fn):
    """Exclude overlapping directory writers until their durable claim ends."""
    from functools import wraps

    @wraps(fn)
    def guarded(conn, task_id, **kwargs):
        from hermes_cli import kanban_db as kb

        task = kb.get_task(conn, task_id)
        if task is None:
            return None
        with _workspace_claim_lock(kb.kanban_home()) as acquired:
            if not acquired:
                return None
            task = kb.get_task(conn, task_id)
            if task is None:
                return None
            if task.workspace_path and workspace_owned_by_other(
                conn, task_id, task.workspace_path,
            ):
                return None
            return fn(conn, task_id, **kwargs)

    return guarded
