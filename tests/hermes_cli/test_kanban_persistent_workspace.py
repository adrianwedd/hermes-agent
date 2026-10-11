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
    kb.write_board_metadata("default", default_workdir=str(tmp_path / "old-repo"), project_id="p_old")
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


def test_decomposition_does_not_inherit_legacy_worktree(local_board):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    conn, root = local_board
    tid = kb.create_task(conn, title="split work", triage=True)
    conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path='/old/repo' WHERE id=?", (tid,))
    children = decompose_triage_task(conn, tid, root_assignee="default", children=[{"title": "local child", "assignee": "default"}])
    assert children
    task = kb.get_task(conn, children[0])
    assert (task.workspace_kind, task.workspace_path, task.project_id) == ("dir", str(root), None)


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
