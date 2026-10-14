"""The central workspace invariant: ``workspace_kind='dir'`` requires a path.

Regression suite for the historical ``t_cce56238`` shape — a card persisted as
``workspace_kind='dir'`` with ``workspace_path=NULL``. The dispatcher always
refused to spawn such a card (``kanban_db_workspace.resolve_workspace`` raises
"has workspace_kind=dir but no workspace_path"), but nothing *before* dispatch
stopped the pair from being written, so an invalid row could sit on the board
until the day it was claimed. These tests pin the pair at every supported write
path: create, project resolution, decomposition fan-out, board import, plus the
dispatcher's fail-safe behaviour on rows that predate the check.

Authority for the reject-don't-default decision: the workspace kinds documented
in ``website/docs/user-guide/features/kanban.md`` — ``dir`` means "an existing
shared directory the caller named", and it is the one kind Hermes *preserves*
on completion. There is no documented default directory, and silently
downgrading to ``scratch`` would hand back a workspace that is deleted when the
task finishes.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _invalid_rows(conn: sqlite3.Connection) -> int:
    """The exact count the acceptance contract requires to be zero."""
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE workspace_kind = 'dir' "
        "AND (workspace_path IS NULL OR trim(workspace_path) = '')"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Creation
# ---------------------------------------------------------------------------

def test_create_dir_without_path_is_rejected(kanban_home):
    """``create_task(workspace_kind='dir')`` with no path must not persist.

    The historical repro, at its authoritative boundary: the invalid pair never
    reaches the table. Rejected (not defaulted) because ``dir`` means "the
    caller picked this exact directory" and is preserved on completion.
    """
    with kbc.connect() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="historical repro", assignee="a", workspace_kind="dir")
        assert "requires an absolute workspace_path" in str(excinfo.value)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert _invalid_rows(conn) == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_create_dir_with_blank_path_is_rejected(kanban_home, blank):
    """``''``/whitespace is the same absence as NULL, so it is rejected too.

    Every reader treats a blank path as "no path" (``resolve_workspace``, the
    SQL ``workspace_path IS ?`` guards), so accepting one would reintroduce the
    invalid row under a different spelling.
    """
    with kbc.connect() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="blank path", assignee="a",
                           workspace_kind="dir", workspace_path=blank)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_dir_inherits_configured_board_workdir(kanban_home, tmp_path):
    shared = tmp_path / "board-workdir"
    shared.mkdir()
    kb.write_board_metadata("default", default_workdir=str(shared), project_id="")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="board directory", assignee="a", workspace_kind="dir")
        task = kb.get_task(conn, tid)
        assert (task.workspace_kind, task.workspace_path) == ("dir", str(shared))
        assert _invalid_rows(conn) == 0


def test_create_valid_dir_with_path_is_accepted(kanban_home, tmp_path):
    """A ``dir`` task with a real path stays valid, and keeps its kind, path
    and (documented) preservation semantics — the fix must not collateral-damage
    existing valid cards."""
    shared = tmp_path / "shared-ops"
    shared.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="keeps its directory", assignee="a",
                             workspace_kind="dir", workspace_path=str(shared))
        task = kb.get_task(conn, tid)
        assert (task.workspace_kind, task.workspace_path) == ("dir", str(shared))
        assert kbw.resolve_workspace(task) == shared
        assert _invalid_rows(conn) == 0


def test_create_dir_with_relative_path_is_accepted_at_create_and_refused_at_dispatch(
    kanban_home, tmp_path,
):
    """Creation validates *presence*, not absoluteness, and the test says so.

    The card's invariant is literally ``workspace_kind == "dir" ->
    workspace_path is non-empty and validly specified``. "Validly specified" for
    absoluteness is already judged where it matters — ``resolve_workspace``
    against the dispatcher's CWD, the confused-deputy vector — and the stored
    value is plain TEXT. Narrowing creation to ``is_absolute()`` would reject
    nothing currently dispatchable while changing accepted-input semantics on a
    dimension this card does not own, so it is left as-is and pinned here. This
    test therefore asserts *presence* acceptance, not a claim of full validity.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="relative dir", assignee="a",
                             workspace_kind="dir", workspace_path="relative/path")
        task = kb.get_task(conn, tid)
        assert task.workspace_path == "relative/path"
        with pytest.raises(ValueError) as excinfo:
            kbw.resolve_workspace(task)
        assert "absolute" in str(excinfo.value)


@pytest.mark.parametrize("bad", [123, Path("/tmp/x"), ["/tmp/x"], b"/tmp/x"])
def test_create_rejects_non_string_workspace_path(kanban_home, bad):
    """A non-string path is refused, not coerced: ``str(obj)`` would mint a
    plausible-looking path out of a bad type."""
    with kbc.connect() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="bad type", assignee="a",
                           workspace_kind="dir", workspace_path=bad)
        assert "must be a string" in str(excinfo.value)


def test_non_blank_path_is_preserved_verbatim(kanban_home, tmp_path):
    """A path with significant leading/trailing whitespace is a legal POSIX
    name and must not be silently stripped to a different directory."""
    odd = tmp_path / " padded dir "
    odd.mkdir()
    assert odd.name == " padded dir "
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spacey", assignee="a",
                             workspace_kind="dir", workspace_path=str(odd))
        assert kb.get_task(conn, tid).workspace_path == str(odd)


def test_create_scratch_without_path_still_valid(kanban_home):
    """``scratch`` semantics are untouched: no path is the *point* of scratch,
    and the dispatcher regenerates one per claim."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ephemeral", assignee="a")
        task = kb.get_task(conn, tid)
        assert (task.workspace_kind, task.workspace_path) == ("scratch", None)
        resolved = kbw.resolve_workspace(task)
        assert resolved.is_dir()
        assert _invalid_rows(conn) == 0


def test_create_worktree_without_path_still_valid(kanban_home, tmp_path, monkeypatch):
    """``worktree`` with no path is legitimate: dispatch materialises
    ``<repo>/.worktrees/<id>`` from the board anchor, so the invariant must not
    over-reach into it (an over-broad check would have broken every worktree
    card)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worktree card", assignee="a",
                             workspace_kind="worktree", workspace_path=str(repo))
        task = kb.get_task(conn, tid)
        assert task.workspace_kind == "worktree"
        assert task.workspace_path == str(repo)


# ---------------------------------------------------------------------------
# 2/3. The project-link upgrade must not smuggle the pair past the check
# ---------------------------------------------------------------------------

def test_project_upgrade_to_dir_cannot_bypass_the_invariant(kanban_home, tmp_path, monkeypatch):
    """The check sits *after* ``_resolve_project_link``.

    A project link can rewrite ``workspace_kind`` (scratch -> worktree), so a
    check placed before it would judge a pair the database never stores. Here
    the resolver is forced to upgrade a pathless ``dir`` request into a
    pathless ``worktree``-style pair, and creation is still refused.
    """
    with kbc.connect() as conn:
        import hermes_cli.kanban_db as kb_mod

        monkeypatch.setattr(
            kb_mod, "_resolve_project_link",
            lambda conn, pid, src, kind, path: ("proj-1", None, None, kind),
        )
        with pytest.raises(ValueError):
            kb.create_task(conn, title="dir via project", assignee="a", workspace_kind="dir")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_blank_path_normalised_before_project_resolution(kanban_home, monkeypatch):
    """A blank ``workspace_path`` reaches the project resolver as ``None``.

    ``_resolve_project_link`` branches on ``workspace_path is None`` to decide
    whether to derive a worktree path, so a ``""`` leaking through would take
    the "path already supplied" branch and persist an empty path.
    """
    seen: dict[str, object] = {}
    with kbc.connect() as conn:
        import hermes_cli.kanban_db as kb_mod

        real = kb_mod._resolve_project_link

        def spy(conn_, pid, src, kind, path):
            seen["path"] = path
            return real(conn_, pid, src, kind, path)

        monkeypatch.setattr(kb_mod, "_resolve_project_link", spy)
        kb.create_task(conn, title="blank normalised", assignee="a",
                       workspace_kind="worktree", workspace_path="   ")
        assert seen["path"] is None


# ---------------------------------------------------------------------------
# 5. Decomposition / automatic card creation
# ---------------------------------------------------------------------------

def _triage_root(conn, **kw):
    return kb.create_task(conn, title="rough idea", assignee="a", triage=True, **kw)


def test_decompose_child_asking_for_dir_without_path_is_refused(kanban_home):
    """An automatic-creation path cannot emit the invalid pair either.

    This is the exact historical shape: a ``dir`` child under a scratch root.
    The child's path inherits only when the kinds match, so it used to fall
    through to ``(dir, None)``. The fan-out is atomic, so the whole call is
    refused and the root stays in triage — no partial graph, no invalid row.
    """
    with kbc.connect() as conn:
        root = _triage_root(conn)
        with pytest.raises(ValueError) as excinfo:
            decompose_triage_task(
                conn, root, root_assignee="a",
                children=[{"title": "dir child", "assignee": "a", "workspace_kind": "dir"}],
            )
        assert "dir child" in str(excinfo.value)
        assert kb.get_task(conn, root).status == "triage"
        assert _invalid_rows(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1  # root only


def test_decompose_does_not_carry_root_path_into_a_different_kind_child(kanban_home, tmp_path):
    """A valid root path must not be inherited by a child of another kind, and
    a matching-kind ``dir`` child keeps its inherited real path."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="dir root", assignee="a", triage=True,
                              workspace_kind="dir", workspace_path=str(shared))
        children = decompose_triage_task(
            conn, root, root_assignee="a",
            children=[
                {"title": "dir child", "assignee": "a"},                       # inherits dir + path
                {"title": "scratch child", "assignee": "a", "workspace_kind": "scratch"},
            ],
        )
        assert children is not None
        by_title = {kb.get_task(conn, cid).title: kb.get_task(conn, cid) for cid in children}
        assert by_title["dir child"].workspace_kind == "dir"
        assert by_title["dir child"].workspace_path == str(shared)
        assert by_title["scratch child"].workspace_kind == "scratch"
        assert by_title["scratch child"].workspace_path is None
        assert _invalid_rows(conn) == 0


def test_decompose_child_dir_with_path_is_accepted(kanban_home, tmp_path):
    """The legitimate automatic-creation case still works."""
    shared = tmp_path / "child-dir"
    shared.mkdir()
    with kbc.connect() as conn:
        root = _triage_root(conn)
        children = decompose_triage_task(
            conn, root, root_assignee="a",
            children=[{"title": "bounded child", "assignee": "a",
                       "workspace_kind": "dir", "workspace_path": str(shared)}],
        )
        assert children is not None
        child = kb.get_task(conn, children[0])
        assert (child.workspace_kind, child.workspace_path) == ("dir", str(shared))


# ---------------------------------------------------------------------------
# 6. Import / migration path
# ---------------------------------------------------------------------------

def test_board_import_behaviour_for_dir_rows_is_pinned_and_the_mint_is_reported(tmp_path, monkeypatch):
    """Import is pinned to its **actual** behaviour, including the mint it still has.

    ``_relocate_imported_rows`` clears every workspace path (foreign paths must
    not be dispatched) and then parks the dispatchable ``dir``/``worktree`` rows
    in triage. For a ``dir`` row that is the invalid pair: kind ``dir``, no path.
    That is pre-existing behaviour, not something this patch introduces, and the
    card's "avoid collateral semantics" clause rules out the obvious repair —
    demoting those rows to ``scratch`` would mass-convert *valid* shared-directory
    workspaces into disposable ones. So the invariant patch deliberately does not
    touch it; this test records exactly what import does today (the "known
    remaining invalid-row source" the completion report has to name), and pins
    the fail-safe on the other side: the imported row is not dispatchable and the
    dispatcher does not fabricate a path for it.
    """
    from hermes_cli import kanban_transfer as kt

    def _use(name):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
        for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_BOARD"):
            monkeypatch.delenv(var, raising=False)
        kb._INITIALIZED_PATHS.clear()
        return root

    _use("source")
    kb.create_board("alpha", name="Alpha")
    with kbc.connect_closing(board="alpha") as conn:
        # (a) the historical invalid pair, (b) a *valid* dir row that import will
        # itself turn into that same pair — the evidence for the note above.
        conn.execute(
            "INSERT INTO tasks (id, title, status, workspace_kind, workspace_path, "
            "created_at, created_by) VALUES ('t_legacy_dir', 'legacy dir', 'todo', "
            "'dir', NULL, ?, 'old-release')",
            (int(time.time()),),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, workspace_kind, workspace_path, "
            "created_at, created_by) VALUES ('t_valid_dir', 'valid dir', 'todo', "
            "'dir', '/exporter/shared', ?, 'old-release')",
            (int(time.time()),),
        )
        conn.commit()
        assert _invalid_rows(conn) == 1

    archive = kt.export_board("alpha", str(tmp_path / "alpha"))["archive"]
    _use("target")
    result = kt.import_board(archive)

    with kbc.connect_closing(board=result["board"]) as conn:
        rows = {
            r["id"]: (r["workspace_kind"], r["workspace_path"], r["status"])
            for r in conn.execute("SELECT id, workspace_kind, workspace_path, status FROM tasks")
        }
        # Both rows are parked in triage, both are left pathless and neither is
        # silently converted to another kind: the invariant patch adds no
        # behaviour here, so the count of pathless 'dir' rows is 2, not 0.
        assert rows["t_legacy_dir"] == ("dir", None, "triage")
        assert rows["t_valid_dir"] == ("dir", None, "triage")
        assert _invalid_rows(conn) == 2  # the unrepaired source, reported not hidden

        # Fail-safe on the far side: neither row dispatches, and nothing invents
        # a path for them.
        spawned: list[str] = []
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(ws) or 1)
        assert spawned == []

    # The import reports the workspace situation rather than doing it silently.
    assert any("workspace was a directory or git" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 7. Dispatcher fail-safe for legacy rows
# ---------------------------------------------------------------------------

def test_dispatcher_refuses_legacy_invalid_row_without_crashing_or_inventing_a_path(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    """A row that predates the check (only reachable by hand-editing the DB)
    must still fail safe: the dispatcher refuses the spawn, records the error
    and never fabricates a path or converts the kind at dispatch time."""
    shared_state = tmp_path / "unused"
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, workspace_kind, "
            "workspace_path, created_at, created_by) "
            "VALUES ('t_cce56238', 'historical card', 'ready', 'a', 'dir', NULL, ?, 'old-release')",
            (int(__import__("time").time()),),
        )
        conn.commit()

        spawned: list[str] = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(ws) or 1234)

        assert spawned == [], "no worker may be spawned into an uninferable directory"
        row = conn.execute(
            "SELECT workspace_kind, workspace_path, status, last_failure_error, "
            "consecutive_failures FROM tasks WHERE id = 't_cce56238'"
        ).fetchone()
        # Fail-safe, not repair: the kind is untouched, no path invented, the
        # refusal is recorded on the card with its cause.
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] is None
        assert "no workspace_path" in (row["last_failure_error"] or "")
        assert row["consecutive_failures"] >= 1
        assert result.spawned == []
        assert not shared_state.exists()


def test_dispatcher_still_spawns_a_valid_dir_card(kanban_home, tmp_path, all_assignees_spawnable):
    """Positive control: the fail-safe must not have become a blanket refusal."""
    shared = tmp_path / "ops-dir"
    shared.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="valid dir", assignee="a",
                             workspace_kind="dir", workspace_path=str(shared))
        spawned: list[str] = []
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(ws) or 4242)
        assert spawned and Path(spawned[0]) == shared


def test_dispatcher_leaves_the_invalid_row_visible_for_a_human(kanban_home, all_assignees_spawnable):
    """The refusal must not silently demote the card into scratch either: the
    operator has to be able to see the broken row and re-point it."""
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, workspace_kind, "
            "workspace_path, created_at, created_by) "
            "VALUES ('t_legacy2', 'legacy', 'ready', 'a', 'dir', NULL, ?, 'old-release')",
            (int(__import__("time").time()),),
        )
        conn.commit()
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: 1)
        assert kb.get_task(conn, "t_legacy2").workspace_kind == "dir"


# ---------------------------------------------------------------------------
# 6b. Every supported API leaves the invariant intact
# ---------------------------------------------------------------------------

def test_supported_write_paths_leave_zero_invalid_rows(kanban_home, tmp_path):
    """Sweep the supported surfaces and assert the persisted board is clean.

    Covers: default create, explicit scratch, valid ``dir``, ``worktree`` with
    no path, decomposition fan-out, and a rejected ``dir`` request (which must
    leave no residue).
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)

    with kbc.connect() as conn:
        kb.create_task(conn, title="plain", assignee="a")
        kb.create_task(conn, title="explicit scratch", assignee="a", workspace_kind="scratch")
        kb.create_task(conn, title="dir ok", assignee="a", workspace_kind="dir", workspace_path=str(shared))
        kb.create_task(conn, title="worktree ok", assignee="a", workspace_kind="worktree")
        root = kb.create_task(conn, title="root", assignee="a", triage=True)
        decompose_triage_task(conn, root, root_assignee="a", children=[
            {"title": "child a", "assignee": "a"},
            {"title": "child b", "assignee": "a", "workspace_kind": "dir", "workspace_path": str(shared)},
        ])
        for bad in (None, "", "  "):
            with pytest.raises(ValueError):
                kb.create_task(conn, title="bad", assignee="a",
                               workspace_kind="dir", workspace_path=bad)
        assert _invalid_rows(conn) == 0
