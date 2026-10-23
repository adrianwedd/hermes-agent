"""Named-board provisioning policy, owned by the shared Kanban home."""
from pathlib import Path


def require_board_creation(slug: str) -> None:
    """An empty allowlist locks provisioning; entries authorize exact new slugs."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb

    if slug == kb.DEFAULT_BOARD:
        return
    root = kb.board_dir(slug)
    if (root / "board.json").is_file() or (root / "kanban.db").is_file():
        return
    config = load_user_config_effective(kb.kanban_home() / "config.yaml", fail_closed=True)
    allowed = config.get("kanban", {}).get("board_creation_allowlist")
    if allowed is None:
        return
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        raise ValueError("kanban.board_creation_allowlist must be a list of exact board slugs")
    if slug not in allowed:
        raise ValueError(
            f"Board creation is locked: {slug!r} requires explicit operator authorization "
            "in kanban.board_creation_allowlist. Use an existing board; "
            "run tests through scripts/run_kanban_probe.py."
        )


def require_board_database(path: Path) -> None:
    """Guard implicit connect/init as well as explicit create and import."""
    from hermes_cli import kanban_db as kb

    candidate = path.expanduser().resolve()
    root = kb.boards_root().resolve()
    if candidate.name == "kanban.db" and candidate.parent.parent == root:
        require_board_creation(candidate.parent.name)
