"""Small worker-process environment helpers for the Kanban dispatcher."""
from __future__ import annotations

import contextlib
from pathlib import Path


def propagate_module_import_root(cmd: list[str], env: dict[str, str], source: Path) -> None:
    """Put the running install's package root on a module-form worker's path."""
    if cmd[1:3] != ["-m", "hermes_cli.main"]:
        return
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath

    pin_hermes_tree_on_pythonpath(env, source.resolve().parents[1])


def worker_terminal_timeout(
    max_runtime_seconds: int | None,
    current_timeout: str | None,
    grace_seconds: int,
) -> str | None:
    """Raise only a child's terminal default to fit its task runtime ceiling."""
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None
    desired = max(1, runtime - grace_seconds)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    return None if existing >= desired else str(desired)


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def rotate_worker_log(
    log_path: Path, max_bytes: int, backup_count: int, *, default_backups: int,
) -> None:
    """Rotate a bounded worker log without making logging failure task-fatal."""
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        try:
            backups = int(backup_count)
        except (TypeError, ValueError):
            backups = default_backups
        backups = backups if backups >= 0 else default_backups
        if backups == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backups)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backups - 1, 0, -1):
            source = _rotated_log_path(log_path, generation)
            if source.exists():
                with contextlib.suppress(OSError):
                    source.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        return
