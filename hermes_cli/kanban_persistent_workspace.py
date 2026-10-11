"""Operator-owned directory policy shared by every Kanban entry point."""
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
    """Return a persistent task directory, or None when the policy is unset."""
    root = persistent_root()
    if root is None:
        return None
    if kind not in (None, "dir") or project_id or branch_name:
        raise ValueError("Kanban uses persistent directories; scratch, worktree, project and branch are disabled")
    target = Path(path).expanduser() if path else root
    if not target.is_absolute() or not target.resolve().is_relative_to(root):
        raise ValueError(f"Kanban workspace must be an ordinary directory under {root}")
    return str(target.resolve())


def creation_defaults(kind, path, project_id, source_task_id, branch_name, board):
    """Resolve local policy before considering legacy board project defaults."""
    from hermes_cli import kanban_db as kb

    persistent = creation_workspace(kind, path, project_id, branch_name)
    if persistent is not None:
        return "dir", persistent, "", None
    # Explicit scratch and an empty project id opt out of board inheritance.
    if project_id is None and kind != "scratch":
        board_project = kb._board_meta_for(board).get("project_id")
        project_id = (board_project.strip() or None) if isinstance(board_project, str) else None
    return "scratch" if kind is None else kind, path, project_id, source_task_id


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
