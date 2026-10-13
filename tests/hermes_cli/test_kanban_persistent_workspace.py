"""Persistent local tasks must not inherit repository lifecycle behavior."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def local_board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "local-tasks"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(json.dumps({"kanban": {"persistent_workdir": str(root)}}))
    kb.init_db()
    kb.write_board_metadata("default", default_workdir=str(tmp_path / "old-repo"), project_id="")
    with kbc.connect() as conn:
        yield conn, root


def test_ordinary_card_executes_in_persistent_directory_and_keeps_files(local_board):
    conn, root = local_board
    tid = kb.create_task(conn, title="local task", assignee="default")
    task = kb.get_task(conn, tid)
    assert (task.workspace_kind, task.workspace_path, task.project_id) == ("dir", str(root), None)
    workspace = kbw.resolve_workspace(task)
    result = subprocess.run([sys.executable, "-c", "from pathlib import Path; Path('receipt.txt').write_text(str(Path.cwd()))"], cwd=workspace, check=True)
    assert result.returncode == 0
    kbw._cleanup_workspace(conn, tid)
    assert (root / "receipt.txt").read_text() == str(root)
    assert not kb.workspaces_root().exists()


def test_legacy_workspace_preserved_and_new_tasks_cannot_escape(local_board, tmp_path):
    conn, root = local_board
    tid = kb.create_task(conn, title="legacy", assignee="default")
    legacy = kb.workspaces_root() / tid
    legacy.mkdir(parents=True)
    (legacy / "existing.txt").write_text("active work")
    conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?", (str(legacy), tid))
    task = kb.get_task(conn, tid)
    assert kbw.resolve_workspace(task) == legacy
    kbw._cleanup_workspace(conn, tid)
    assert (legacy / "existing.txt").read_text() == "active work"
    for kwargs in ({"workspace_kind": "scratch"}, {"workspace_kind": "worktree"}, {"project_id": "p_old"}, {"workspace_kind": "dir", "workspace_path": str(tmp_path / "outside")}):
        with pytest.raises(ValueError):
            kb.create_task(conn, title="invalid", **kwargs)
    child = root / "child"
    child_tid = kb.create_task(conn, title="child", workspace_kind="dir", workspace_path=str(child))
    assert kbw.resolve_workspace(kb.get_task(conn, child_tid)) == child


def test_decomposition_places_legacy_nonproject_child_in_local_root(local_board, tmp_path):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    conn, root = local_board
    tid = kb.create_task(conn, title="split work", triage=True)
    legacy = tmp_path / "canonical" / ".worktrees" / tid
    legacy.mkdir(parents=True)
    conn.execute(
        "UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id=?",
        (str(legacy), tid),
    )
    children = decompose_triage_task(conn, tid, root_assignee="default", children=[{"title": "local child", "assignee": "default"}])
    assert children
    task = kb.get_task(conn, children[0])
    assert (task.workspace_kind, task.workspace_path, task.project_id) == ("dir", str(root), None)


def test_decomposition_rejects_outside_directory_override_atomically(local_board, tmp_path):
    from hermes_cli.kanban_db_graph import decompose_triage_task

    conn, _root = local_board
    outside = tmp_path / "outside"
    outside.mkdir()
    tid = kb.create_task(conn, title="split work", triage=True)

    with pytest.raises(ValueError, match="ordinary directory under"):
        decompose_triage_task(
            conn,
            tid,
            root_assignee="default",
            children=[
                {"title": "valid child"},
                {"title": "escaped child", "workspace_path": str(outside)},
            ],
        )

    assert kb.get_task(conn, tid).status == "triage"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decomposed'", (tid,),
    ).fetchone()[0] == 0


def test_decomposition_preserves_project_and_enforces_canonical_path(local_board, tmp_path):
    from hermes_cli import projects_db as pdb
    from hermes_cli.kanban_db_graph import decompose_triage_task

    conn, _root = local_board
    repo = tmp_path / "canonical-repo"
    repo.mkdir()
    outside = tmp_path / "other-repo"
    outside.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Decomposed", primary_path=str(repo))
    tid = kb.create_task(conn, title="project split", project_id=project_id, triage=True)

    with pytest.raises(ValueError, match="project's canonical repository"):
        decompose_triage_task(
            conn,
            tid,
            root_assignee="default",
            children=[{"title": "wrong checkout", "workspace_path": str(outside)}],
        )

    children = decompose_triage_task(
        conn,
        tid,
        root_assignee="default",
        children=[{"title": "canonical child"}],
    )
    assert children
    child = kb.get_task(conn, children[0])
    assert (child.workspace_kind, child.workspace_path, child.project_id) == (
        "dir", str(repo), project_id,
    )


def test_decomposition_moves_legacy_project_child_to_canonical_repo(
    local_board, tmp_path, monkeypatch,
):
    from hermes_cli import projects_db as pdb
    from hermes_cli.kanban_db_graph import decompose_triage_task

    conn, _root = local_board
    repo = tmp_path / "canonical-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Legacy project", primary_path=str(repo))
    tid = kb.create_task(conn, title="legacy project split", project_id=project_id, triage=True)
    legacy = repo / ".worktrees" / tid
    legacy.mkdir(parents=True)
    conn.execute(
        "UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id=?",
        (str(legacy), tid),
    )
    worker_home = tmp_path / "worker-profile"
    worker_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(worker_home))

    children = decompose_triage_task(
        conn,
        tid,
        root_assignee="default",
        children=[{"title": "canonical project child"}],
    )
    assert children
    child = kb.get_task(conn, children[0])
    assert (child.workspace_kind, child.workspace_path, child.project_id) == (
        "dir", str(repo), project_id,
    )


def test_project_task_uses_canonical_checkout_and_claims_serialize(local_board, tmp_path):
    from hermes_cli import projects_db as pdb

    conn, _root = local_board
    repo = tmp_path / "canonical-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Canonical", primary_path=str(repo))
    first = kb.create_task(conn, title="first", assignee="default", project_id=project_id)
    second = kb.create_task(conn, title="second", assignee="default", project_id=project_id)
    first_task = kb.get_task(conn, first)
    second_task = kb.get_task(conn, second)
    assert (first_task.workspace_kind, first_task.workspace_path) == ("dir", str(repo))
    assert (second_task.workspace_kind, second_task.workspace_path) == ("dir", str(repo))
    assert kb.claim_task(conn, first) is not None
    assert kb.claim_task(conn, second) is None


def test_dispatch_serializes_tasks_sharing_canonical_checkout(local_board, tmp_path):
    import os

    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import projects_db as pdb

    conn, _root = local_board
    repo = tmp_path / "shared-canonical-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Shared", primary_path=str(repo))
    first = kb.create_task(conn, title="first dispatch", assignee="default", project_id=project_id)
    second = kb.create_task(conn, title="second dispatch", assignee="default", project_id=project_id)

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: os.getpid(), max_spawn=2,
    )

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [first]
    assert kb.get_task(conn, second).status == "ready"


def test_dispatch_preview_reports_workspace_owner_instead_of_spawn(local_board, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import projects_db as pdb

    conn, _root = local_board
    repo = tmp_path / "preview-canonical-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Preview", primary_path=str(repo))
    first = kb.create_task(conn, title="claimed owner", assignee="default", project_id=project_id)
    second = kb.create_task(conn, title="waiting writer", assignee="default", project_id=project_id)
    assert kb.claim_task(conn, first) is not None

    result = kbd.dispatch_once(conn, dry_run=True, max_spawn=2)

    assert result.spawned == []
    assert (second, "workspace_owner_conflict") in result.respawn_guarded
    assert kb.get_task(conn, second).status == "ready"


def test_dispatch_preview_reserves_workspace_for_earlier_candidate(local_board, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import projects_db as pdb

    conn, _root = local_board
    repo = tmp_path / "preview-shared-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Preview shared", primary_path=str(repo))
    first = kb.create_task(conn, title="first candidate", assignee="default", project_id=project_id)
    second = kb.create_task(conn, title="second candidate", assignee="default", project_id=project_id)

    result = kbd.dispatch_once(conn, dry_run=True, max_spawn=2)

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [first]
    assert (second, "workspace_owner_conflict") in result.respawn_guarded
    assert kb.get_task(conn, first).status == kb.get_task(conn, second).status == "ready"


def test_cross_board_overlapping_workspace_is_owned_for_full_claim_lifetime(local_board):
    conn, root = local_board
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = kb.create_task(conn, title="default writer")
    kb.create_board("other")
    with kbc.connect(board="other") as other:
        second = kb.create_task(
            other, title="nested writer", workspace_kind="dir",
            workspace_path=str(nested), project_id="",
        )
        claimed = kb.claim_task(conn, first)
        assert claimed is not None
        assert kb.claim_task(other, second) is None
        assert kb.get_task(conn, first).status == "running"
        assert kb.get_task(other, second).status == "ready"


def test_workspace_ownership_releases_after_terminal_completion(local_board, tmp_path):
    from hermes_cli import projects_db as pdb

    conn, _root = local_board
    repo = tmp_path / "released-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Released", primary_path=str(repo))
    first = kb.create_task(conn, title="first owner", project_id=project_id)
    second = kb.create_task(conn, title="next owner", project_id=project_id)
    claimed = kb.claim_task(conn, first)
    assert claimed is not None
    assert kb.claim_task(conn, second) is None

    assert kb.complete_task(
        conn, first, summary="ownership released", expected_run_id=claimed.current_run_id,
    )
    assert kb.claim_task(conn, second) is not None


def test_dashboard_default_and_creation(local_board):
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    source = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_persistent_dashboard_test", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        boards = client.get("/boards").json()
        default = next(b for b in boards["boards"] if b["slug"] == "default")
        assert default["default_workspace_kind"] == "dir"
        assert default["default_workdir"] == str(local_board[1])
        assert default["project_id"] is None
        response = client.post("/tasks", json={"title": "dashboard local task", "assignee": "default"})
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert (task["workspace_kind"], task["workspace_path"], task["project_id"]) == ("dir", str(local_board[1]), None)


def test_dashboard_project_default_uses_canonical_checkout(local_board, tmp_path):
    import importlib.util

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "dashboard-canonical-repo"
    repo.mkdir()
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(pconn, name="Dashboard Canonical", primary_path=str(repo))
    kb.write_board_metadata("default", default_workdir=str(repo), project_id=project_id)
    source = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_canonical_dashboard_test", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)

    with TestClient(app) as client:
        default = next(
            board for board in client.get("/boards").json()["boards"]
            if board["slug"] == "default"
        )
        assert default["default_workspace_kind"] == "dir"
        assert default["default_workdir"] == str(repo)
        assert default["project_id"] == project_id
        response = client.post("/tasks", json={"title": "project task", "assignee": "default"})
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert (task["workspace_kind"], task["workspace_path"], task["project_id"]) == (
            "dir", str(repo), project_id,
        )


def test_agent_creation_ignores_creator_project(local_board, monkeypatch):
    from tools import kanban_tools  # noqa: F401 -- registers the production handlers
    from tools.registry import registry
    conn, root = local_board
    tid = kb.create_task(conn, title="legacy creator", assignee="default")
    conn.execute("UPDATE tasks SET project_id='p_old' WHERE id=?", (tid,))
    kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))
    result = json.loads(registry.dispatch("kanban_create", {"title": "agent local task", "assignee": "default"}))
    assert result["ok"], result
    child = kb.get_task(conn, result["task_id"])
    assert (child.workspace_kind, child.workspace_path, child.project_id) == ("dir", str(root), None)


def test_cli_card_dispatches_a_real_process_in_designated_folder(local_board):
    from hermes_cli.kanban import run_slash
    from hermes_cli import kanban_db_dispatch as kbd
    conn, root = local_board
    output = run_slash('create "CLI execution proof" --assignee default --body "Write a local receipt and report it."')
    task = conn.execute("SELECT id FROM tasks WHERE title='CLI execution proof'").fetchone()
    assert task is not None, output
    processes = []

    def spawn(task, workspace, board=None):
        process = subprocess.Popen([sys.executable, "-c", "from pathlib import Path; import time; Path('dispatch-receipt.txt').write_text(str(Path.cwd())); time.sleep(2)"], cwd=workspace)
        processes.append(process)
        return process.pid

    try:
        result = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert result.spawned, result
        assert result.spawned[0][2] == str(root)
        processes[0].wait(timeout=10)
        assert (root / "dispatch-receipt.txt").read_text() == str(root)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_policy_follows_board_owner_across_profiles_and_retains_gc_files(local_board, tmp_path, monkeypatch):
    from argparse import Namespace
    from hermes_cli.kanban_ops import _cmd_gc
    from hermes_cli.kanban_persistent_workspace import persistent_root
    conn, root = local_board
    profile = tmp_path / "worker-profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("kanban: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert persistent_root() == root
    tid = kb.create_task(conn, title="legacy archived work")
    legacy = kb.workspaces_root() / tid
    legacy.mkdir(parents=True)
    (legacy / "keep.txt").write_text("keep")
    conn.execute("UPDATE tasks SET status='archived', workspace_kind='scratch', workspace_path=? WHERE id=?", (str(legacy), tid))
    _cmd_gc(Namespace(event_retention_days=0, log_retention_days=0))
    assert (legacy / "keep.txt").read_text() == "keep"
    conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (tid,))
    task = kb.get_task(conn, tid)
    monkeypatch.setattr(kbw, "_git", lambda *a, **k: pytest.fail("persistent dispatch must not invoke Git"))
    assert kbw._resolve_worktree_workspace(task)[0] == legacy
    kbw._cleanup_worktree_workspace(tid, str(legacy))
    assert (legacy / "keep.txt").read_text() == "keep"
