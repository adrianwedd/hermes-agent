"""Production locks cover explicit and implicit named-board creation."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_board_authorization import require_board_creation


@pytest.fixture
def locked_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    (home / "config.yaml").write_text("kanban:\n  board_creation_allowlist: []\n")
    return home


@pytest.mark.parametrize("action", [
    lambda: kb.create_board("unexpected"),
    lambda: kb.write_board_metadata("unexpected", name="Unexpected"),
    lambda: kbc.connect(board="unexpected"),
    lambda: kbc.init_db(board="unexpected"),
    lambda: kbc.connect(kb.boards_root() / "unexpected" / "kanban.db"),
])
def test_locked_creation_has_no_filesystem_side_effect(locked_home, action):
    with pytest.raises(ValueError, match="explicit operator authorization"):
        action()
    assert not kb.board_dir("unexpected").exists()


def test_grant_is_exact_and_worker_profile_cannot_override(locked_home, tmp_path, monkeypatch):
    (locked_home / "config.yaml").write_text(json.dumps({"kanban": {"board_creation_allowlist": ["approved"]}}))
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("kanban: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    require_board_creation("approved")
    with pytest.raises(ValueError, match="explicit operator authorization"):
        require_board_creation("approved-2")
    assert not kb.boards_root().exists()


def test_existing_default_remains_readable_and_writable(locked_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Existing board work")
        assert kb.get_task(conn, tid).title == "Existing board work"


def test_import_cannot_provision_a_new_board(locked_home, tmp_path):
    from hermes_cli.kanban_transfer import export_board, import_board
    kbc.init_db()
    archive = export_board("default", str(tmp_path / "export"))["archive"]
    with pytest.raises(ValueError, match="explicit operator authorization"):
        import_board(archive, "unexpected-import")
    assert not kb.board_dir("unexpected-import").exists()


def test_dashboard_cannot_provision_a_new_board(locked_home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router
    app = FastAPI()
    app.include_router(_load_plugin_router())
    with TestClient(app) as client:
        response = client.post("/boards", json={"slug": "unexpected-api"})
    assert response.status_code == 400, response.text
    assert "explicit operator authorization" in response.text
    assert not kb.board_dir("unexpected-api").exists()
