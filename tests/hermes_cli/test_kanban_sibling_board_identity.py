"""Board-identity semantics of the cross-board workspace ownership scan.

``kanban_db_workspace._sibling_board_db_files`` and
``kanban_preparation.workspace_busy`` are the two gates that decide whether a
workspace is exclusively owned. Both walk ``<root>/kanban/boards/*/kanban.db``
for *other* boards. ``kanban/boards/default/`` is the default board's metadata
directory (``board.json``, ``workspaces/``, ``logs/``); the default board's
store is ``<root>/kanban.db`` (or the ``HERMES_KANBAN_DB`` pin) and
``kanban_db_path("default")`` never resolves into it. Installs that once kept
the default board under ``boards/default/`` retain a schema-less
``kanban.db`` there — on this Studio install, measured, exactly a 0-byte file.

While a schema-less file is enumerated, every sibling read raises
``no such table: tasks`` and both gates translate any ``sqlite3.Error`` into
"ownership unknown": a preparation qualification is refused with
``workspace_owner_conflict`` and every scratch-workspace cleanup is deferred,
for a workspace nothing owns.

The fix is deliberately narrow — a *provably empty* (regular, zero-byte)
``boards/default/kanban.db`` stops being a sibling. Everything that could hold
ownership evidence keeps failing closed: a populated, corrupt or schema-less
nonempty store at that path (including under a custom ``HERMES_KANBAN_DB``
pin), an unreadable or symlinked one, and every named board.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kb_ws
from hermes_cli.kanban_db_workspace import (
    _defer_shared_workspace_cleanup,
    _sibling_board_db_files,
    _workspace_in_use_by_other,
)
from hermes_cli.kanban_preparation import workspace_busy

# Resolved at call time, not import time, so this file still collects against a
# tree that predates the predicate — the red/green proof needs the failing
# assertions to run, not a collection error that hides them.
def _provably_empty_default_metadata_db(path):
    predicate = getattr(kb_ws, "_provably_empty_default_metadata_db", None)
    assert predicate is not None, "kanban_db_workspace._provably_empty_default_metadata_db is missing"
    return predicate(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty board set."""
    root = tmp_path / "hermes_home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants

    hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    kb._INITIALIZED_PATHS.clear()
    return root


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _default_metadata_db(home_root: Path) -> Path:
    return home_root / "kanban" / "boards" / "default" / "kanban.db"


def _zero_byte_default_duplicate(home_root: Path) -> Path:
    """The shape retained on this install: a 0-byte file, never a board store."""
    path = _default_metadata_db(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _nonempty_schemaless_default_duplicate(home_root: Path) -> Path:
    """A readable, nonempty DB whose only table is not the board schema."""
    path = _default_metadata_db(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE placeholder(x)")
        con.commit()
    finally:
        con.close()
    return path


def _populated_default_duplicate(home_root: Path, *, owner_workspace: Path) -> Path:
    """A *real* board store at the default metadata path, with a live owner task."""
    path = _default_metadata_db(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing(db_path=path) as conn:
        kb.create_task(conn, title="owner on the legacy default store",
                       workspace_kind="dir", workspace_path=str(owner_workspace))
    return path


def _corrupt_default_duplicate(home_root: Path) -> Path:
    path = _default_metadata_db(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database at all\n" * 8)
    return path


def _named_board(home_root: Path, slug: str, *, schema_less: bool = False) -> Path:
    path = home_root / "kanban" / "boards" / slug / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if schema_less:
        path.write_bytes(b"")
        return path
    with kbc.connect_closing(board=slug) as conn:
        kb.create_task(conn, title=f"task on {slug}")
    return path


def _task_on_local(home_root: Path, *, path: Path | None = None) -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="task under preparation", workspace_kind="dir",
                               workspace_path=str(path) if path else None)


def _hold_workspace(home_root: Path, *, board: str | None, path: Path) -> str:
    """A non-terminal task on *board* owning *path*; returns its task id."""
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, title="holds the workspace", workspace_kind="dir",
                               workspace_path=str(path), board=board)


# ---------------------------------------------------------------------------
# The fix: only a provably empty default metadata duplicate is dropped
# ---------------------------------------------------------------------------

def test_zero_byte_default_duplicate_is_not_a_sibling_board(home):
    _task_on_local(home)
    duplicate = _zero_byte_default_duplicate(home)
    named = _named_board(home, "undertow-constitutional")

    with kbc.connect_closing() as conn:
        siblings = _sibling_board_db_files(conn)

    assert duplicate.resolve() not in siblings
    assert named.resolve() in siblings
    assert [p.parent.name for p in siblings] == ["undertow-constitutional"]


def test_zero_byte_default_duplicate_is_left_on_disk_untouched(home):
    duplicate = _zero_byte_default_duplicate(home)
    _task_on_local(home)
    before = duplicate.stat()

    with kbc.connect_closing() as conn:
        _sibling_board_db_files(conn)

    after = duplicate.stat()
    assert after.st_size == 0
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_canonical_default_db_is_not_its_own_sibling(home):
    _task_on_local(home)
    with kbc.connect_closing() as conn:
        assert kb.kanban_db_path().resolve() not in _sibling_board_db_files(conn)


def test_provably_empty_predicate_rejects_everything_but_a_zero_byte_regular_file(home, tmp_path):
    empty = _zero_byte_default_duplicate(home)
    assert _provably_empty_default_metadata_db(empty) is True
    assert _provably_empty_default_metadata_db(_nonempty_schemaless_default_duplicate(home)) is False
    assert _provably_empty_default_metadata_db(_corrupt_default_duplicate(home)) is False
    assert _provably_empty_default_metadata_db(tmp_path / "absent.db") is False
    target = tmp_path / "real-store.db"
    target.write_bytes(b"")
    link = tmp_path / "link.db"
    link.symlink_to(target)
    assert _provably_empty_default_metadata_db(link) is False


# ---------------------------------------------------------------------------
# Sidecar evidence: a zero-byte main file is not an emptiness certificate
# ---------------------------------------------------------------------------

def _write_sidecar(duplicate: Path, suffix: str, data: bytes) -> Path:
    sidecar = duplicate.with_name(duplicate.name + suffix)
    sidecar.write_bytes(data)
    return sidecar


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_nonempty_sidecar_keeps_zero_byte_duplicate_fail_closed(home, tmp_path, suffix):
    """Retained sidecar evidence prevents exemption before any SQLite read."""
    free = tmp_path / "free-workspace"
    free.mkdir()
    duplicate = _zero_byte_default_duplicate(home)
    _write_sidecar(duplicate, suffix, b"x" * 512)
    task_id = _task_on_local(home)

    assert _provably_empty_default_metadata_db(duplicate) is False
    with kbc.connect_closing() as conn:
        assert duplicate.resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(free)) is True
        # SQLite may consume an invalid synthetic WAL while opening the DB.
        # Recreate it so the cleanup reader sees the same independent input.
        _write_sidecar(duplicate, suffix, b"x" * 512)
        assert _workspace_in_use_by_other(conn, task_id, str(free)) == "unknown"


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_zero_byte_sidecar_keeps_the_duplicate_enumerated(home, tmp_path, suffix):
    """Even an empty sidecar makes transaction/lock state uncertain."""
    free = tmp_path / "free-workspace"
    free.mkdir()
    duplicate = _zero_byte_default_duplicate(home)
    _write_sidecar(duplicate, suffix, b"")
    task_id = _task_on_local(home)

    assert _provably_empty_default_metadata_db(duplicate) is False
    with kbc.connect_closing() as conn:
        assert duplicate.resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(free)) is True


def test_symlinked_sidecar_stays_fail_closed(home):
    duplicate = _zero_byte_default_duplicate(home)
    real = duplicate.parent / "elsewhere.db"
    real.write_bytes(b"frames" * 64)
    duplicate.with_name(duplicate.name + "-wal").symlink_to(real)
    _task_on_local(home)

    assert _provably_empty_default_metadata_db(duplicate) is False
    with kbc.connect_closing() as conn:
        assert duplicate.resolve() in _sibling_board_db_files(conn)


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_sidecar_stat_failure_stays_fail_closed(home, monkeypatch, suffix):
    duplicate = _zero_byte_default_duplicate(home)
    wal = _write_sidecar(duplicate, suffix, b"")
    _task_on_local(home)
    real_stat = Path.stat

    def _boom(self, **kwargs):
        if self == wal:
            raise OSError("simulated stat failure")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _boom, raising=True)
    assert _provably_empty_default_metadata_db(duplicate) is False
    with kbc.connect_closing() as conn:
        assert duplicate.resolve() in _sibling_board_db_files(conn)


# ---------------------------------------------------------------------------
# RED on base / GREEN on fix: a free workspace must not read as owned
# ---------------------------------------------------------------------------

def test_zero_byte_duplicate_does_not_report_a_free_workspace_as_busy(home, tmp_path):
    free = tmp_path / "free-workspace"
    free.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _zero_byte_default_duplicate(home)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(free)) is False


def test_zero_byte_duplicate_does_not_defer_workspace_cleanup(home, tmp_path):
    free = tmp_path / "free-workspace"
    free.mkdir()
    task_id = _task_on_local(home)
    _zero_byte_default_duplicate(home)

    with kbc.connect_closing() as conn:
        # None == free; "unknown" is what the duplicate used to force.
        assert _workspace_in_use_by_other(conn, task_id, str(free)) is None
        assert _defer_shared_workspace_cleanup(conn, task_id, str(free)) is False


# ---------------------------------------------------------------------------
# Fail-closed parity: everything that could own the path still blocks
# ---------------------------------------------------------------------------

def test_nonempty_schemaless_default_duplicate_still_fails_closed(home, tmp_path):
    free = tmp_path / "free-workspace"
    free.mkdir()
    task_id = _task_on_local(home)
    _nonempty_schemaless_default_duplicate(home)

    with kbc.connect_closing() as conn:
        assert _default_metadata_db(home).resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(free)) is True
        assert _workspace_in_use_by_other(conn, task_id, str(free)) == "unknown"
        assert _defer_shared_workspace_cleanup(conn, task_id, str(free)) is True


def test_corrupt_default_duplicate_still_fails_closed(home, tmp_path):
    free = tmp_path / "free-workspace"
    free.mkdir()
    task_id = _task_on_local(home)
    _corrupt_default_duplicate(home)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(free)) is True
        assert _workspace_in_use_by_other(conn, task_id, str(free)) == "unknown"


def test_populated_default_duplicate_ownership_still_blocks(home, tmp_path):
    """A real store at the default metadata path must not be silenced by the fix."""
    shared = tmp_path / "shared-workspace"
    shared.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _populated_default_duplicate(home, owner_workspace=shared)

    with kbc.connect_closing() as conn:
        assert _default_metadata_db(home).resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(shared)) is True
        assert _workspace_in_use_by_other(conn, task_id, str(shared)) == "shared"
        assert _defer_shared_workspace_cleanup(conn, task_id, str(shared)) is True


def test_populated_default_duplicate_under_custom_kb_db_pin_still_blocks(home, tmp_path, monkeypatch):
    """A custom ``HERMES_KANBAN_DB`` pin must not narrow the scan into silence."""
    shared = tmp_path / "shared-workspace"
    shared.mkdir()
    custom = tmp_path / "custom-board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(custom))
    with kbc.connect_closing(db_path=custom) as conn:
        kb.init_db(db_path=custom)
        task_id = kb.create_task(conn, title="task under preparation", workspace_kind="dir",
                                 workspace_path=str(tmp_path / "elsewhere"))
    _populated_default_duplicate(home, owner_workspace=shared)

    with kbc.connect_closing() as conn:
        assert _default_metadata_db(home).resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(shared)) is True


def test_symlinked_default_duplicate_is_never_treated_as_empty(home, tmp_path):
    """A link may point at a real custom store; it stays enumerated."""
    real = tmp_path / "custom-store.db"
    with kbc.connect_closing(db_path=real) as conn:
        kb.create_task(conn, title="real store")
    link = _default_metadata_db(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    _task_on_local(home)

    assert _provably_empty_default_metadata_db(link) is False
    with kbc.connect_closing() as conn:
        assert link.resolve() in _sibling_board_db_files(conn)


def test_unreadable_default_duplicate_stays_enumerated(home, monkeypatch):
    """A stat failure is uncertainty, never grounds to drop the candidate."""
    path = _zero_byte_default_duplicate(home)
    _task_on_local(home)
    real_stat = Path.stat

    def _boom(self, **kwargs):
        if self == path:
            raise OSError("simulated stat failure")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _boom, raising=True)
    assert _provably_empty_default_metadata_db(path) is False
    with kbc.connect_closing() as conn:
        assert path.resolve() in _sibling_board_db_files(conn)


def test_named_schemeless_board_still_fails_closed(home, tmp_path):
    free = tmp_path / "free-workspace"
    free.mkdir()
    task_id = _task_on_local(home)
    _named_board(home, "undertow-constitutional", schema_less=True)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(free)) is True
        assert _workspace_in_use_by_other(conn, task_id, str(free)) == "unknown"


def test_named_board_ownership_still_blocks_preparation(home, tmp_path):
    shared = tmp_path / "shared-workspace"
    shared.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _named_board(home, "undertow-constitutional")
    _hold_workspace(home, board="undertow-constitutional", path=shared)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(shared)) is True
        assert _workspace_in_use_by_other(conn, task_id, str(shared)) == "shared"


def test_local_board_ownership_still_blocks_preparation(home, tmp_path):
    shared = tmp_path / "shared-workspace"
    shared.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _hold_workspace(home, board=None, path=shared)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(shared)) is True


def test_ancestor_and_descendant_paths_still_overlap(home, tmp_path):
    """``workspace_busy`` treats nesting as a conflict, not just equality."""
    task_id = _task_on_local(home)
    parent = tmp_path / "repo"
    (parent / "sub").mkdir(parents=True)
    _hold_workspace(home, board=None, path=parent)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(parent / "sub")) is True


# ---------------------------------------------------------------------------
# Retained worker-PID evidence is still consulted
# ---------------------------------------------------------------------------

def _record_live_pid_run(conn, task_id: str, pid: int, started_at) -> None:
    with kbc.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, worker_pid, worker_started_at, started_at) "
            "VALUES (?, 'default', 'running', ?, ?, 1)", (task_id, pid, started_at))


def test_retained_live_pid_run_blocks_on_a_named_sibling_board(home, tmp_path):
    """The PID branch ignores task status, so a retained live worker still owns its workspace.

    Task and run are written on ONE named connection: passing ``board=`` to
    ``create_task`` on the canonical connection would insert the row there and
    make this test pass for the wrong reason.
    """
    target = tmp_path / "target-workspace"
    target.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _named_board(home, "undertow-constitutional")

    with kbc.connect_closing(board="undertow-constitutional") as named_conn:
        other = kb.create_task(named_conn, title="worker holding target",
                               workspace_path=str(target))
        with kbc.write_txn(named_conn):
            # Terminal before the PID row, so only the retained-PID branch can own it.
            named_conn.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
        _record_live_pid_run(named_conn, other, os.getpid(), None)

    with kbc.connect_closing() as conn:
        local_owners = [
            r["workspace_path"] for r in conn.execute(
                "SELECT workspace_path FROM tasks WHERE workspace_path IS NOT NULL "
                "AND status NOT IN ('done','archived')")
        ]
        assert all(Path(p).expanduser().resolve() != target.resolve() for p in local_owners)
        assert workspace_busy(conn, task_id, str(target)) is True


def test_populated_default_path_store_with_retained_live_pid_blocks(home, tmp_path):
    """A real store at the metadata path is never silenced: terminal task, live PID owns it."""
    target = tmp_path / "target-workspace"
    target.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    legacy = _populated_default_duplicate(home, owner_workspace=target)

    with kbc.connect_closing(db_path=legacy) as legacy_conn:
        owner = legacy_conn.execute(
            "SELECT id FROM tasks WHERE workspace_path=?", (str(target),)).fetchone()[0]
        with kbc.write_txn(legacy_conn):
            legacy_conn.execute("UPDATE tasks SET status='done' WHERE id=?", (owner,))
        _record_live_pid_run(legacy_conn, owner, os.getpid(), None)

    with kbc.connect_closing() as conn:
        assert _default_metadata_db(home).resolve() in _sibling_board_db_files(conn)
        assert workspace_busy(conn, task_id, str(target)) is True


def test_custom_pinned_store_with_retained_live_pid_blocks(home, tmp_path, monkeypatch):
    """A custom ``HERMES_KANBAN_DB`` store is a sibling too; its retained PID still owns.

    The sibling scan is layout-based, so the pinned store is created where that
    layout enumerates it (the default metadata path) to model the real hazard:
    a supported custom pin writing a genuine store there is never silenced.
    """
    target = tmp_path / "target-workspace"
    target.mkdir()
    custom = _default_metadata_db(home)
    custom.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(custom))

    with kbc.connect_closing(db_path=custom) as custom_conn:
        task_id = kb.create_task(custom_conn, title="task under preparation", workspace_kind="dir",
                                 workspace_path=str(tmp_path / "elsewhere"))
        other = kb.create_task(custom_conn, title="worker holding target", workspace_path=str(target))
        with kbc.write_txn(custom_conn):
            custom_conn.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
        _record_live_pid_run(custom_conn, other, os.getpid(), None)

    # A separate connection on the *default* DB sees the pinned store as a sibling.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with kbc.connect_closing() as conn:
        assert custom.resolve() in _sibling_board_db_files(conn)
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=NULL WHERE id=?", (task_id,))
        assert workspace_busy(conn, task_id, str(target)) is True


def test_current_custom_store_checks_canonical_sibling_retained_pid(home, tmp_path, monkeypatch):
    target = tmp_path / "target-workspace"
    target.mkdir()
    with kbc.connect_closing() as canonical:
        owner = kb.create_task(canonical, title="retained canonical worker", workspace_path=str(target))
        with kbc.write_txn(canonical):
            canonical.execute("UPDATE tasks SET status='done' WHERE id=?", (owner,))
        _record_live_pid_run(canonical, owner, os.getpid(), None)
        canonical_path = Path(canonical.execute("PRAGMA database_list").fetchone()[2]).resolve()

    custom = tmp_path / "custom-board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(custom))
    with kbc.connect_closing(db_path=custom) as conn:
        task_id = kb.create_task(conn, title="custom task", workspace_path=str(tmp_path / "elsewhere"))
        assert canonical_path in _sibling_board_db_files(conn)
        assert conn.execute("SELECT 1 FROM tasks WHERE workspace_path=?", (str(target),)).fetchone() is None
        assert workspace_busy(conn, task_id, str(target)) is True


def test_dead_pid_run_does_not_block(home, tmp_path, monkeypatch):
    target = tmp_path / "target-workspace"
    target.mkdir()
    task_id = _task_on_local(home, path=tmp_path / "elsewhere")
    _named_board(home, "undertow-constitutional")
    with kbc.connect_closing(board="undertow-constitutional") as conn:
        other = kb.create_task(conn, title="crashed worker", board="undertow-constitutional",
                               workspace_path=str(target))
        with kbc.write_txn(conn):
            # Terminal task: only the retained-PID branch could still claim the path.
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
        _record_live_pid_run(conn, other, 999_999, None)
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch._worker_alive", lambda pid, started: False)

    with kbc.connect_closing() as conn:
        assert workspace_busy(conn, task_id, str(target)) is False
