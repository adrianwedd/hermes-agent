"""The workspace invariant across the dashboard/API surface.

``POST /api/plugins/kanban/tasks`` is a supported write path (the desktop
create dialog uses it), and ``CreateTaskBody`` carries ``workspace_kind`` and
``workspace_path`` straight through to ``kanban_db.create_task``. The
domain-layer rejection therefore has to surface as a clean HTTP 400 rather than
a 500 — and must persist nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_ws_invariant_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_create_dir_without_path_is_400_and_persists_nothing(client):
    r = client.post("/api/plugins/kanban/tasks", json={"title": "bad", "workspace_kind": "dir"})
    assert r.status_code == 400, r.text
    assert "requires an absolute workspace_path" in r.text

    with kbc.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workspace_kind = 'dir' "
            "AND (workspace_path IS NULL OR trim(workspace_path) = '')"
        ).fetchone()[0] == 0


def test_dashboard_create_dir_with_blank_path_is_400(client):
    for blank in ("", "   "):
        r = client.post(
            "/api/plugins/kanban/tasks",
            json={"title": "bad", "workspace_kind": "dir", "workspace_path": blank},
        )
        assert r.status_code == 400, r.text


def test_dashboard_create_valid_dir_still_accepted(client, tmp_path):
    shared = tmp_path / "shared-dir"
    shared.mkdir()
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "ok", "workspace_kind": "dir", "workspace_path": str(shared)},
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert (task["workspace_kind"], task["workspace_path"]) == ("dir", str(shared))
