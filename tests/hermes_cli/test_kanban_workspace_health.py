"""Workspace-scoped infrastructure health for Kanban workers."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_decision import decide


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def _source_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "owned-hermes-source"
    (workspace / "hermes_cli").mkdir(parents=True)
    (workspace / "hermes_bootstrap.py").write_text("# fixture\n", encoding="utf-8")
    return workspace


def test_source_workspace_repair_releases_fault_without_host_generation_change(
    board, monkeypatch, tmp_path,
):
    import hermes_cli.kanban_decision as decision_module

    workspace = _source_workspace(tmp_path)
    task_id = kb.create_task(
        board, title="review repaired source workspace", body="Acceptance: review it.",
        assignee="reviewer", workspace_kind="dir", workspace_path=str(workspace),
        initial_status="running",
    )
    board.execute("UPDATE tasks SET status='review',dispatch_eligible=1 WHERE id=?", (task_id,))
    kb._append_event(board, task_id, "completion_requirements", {
        "version": 1, "kind": "review", "local_only": True,
        "require_review": False, "require_commands": False,
        "criteria": ["authoritative_scope_satisfied"], "qualified_for_dispatch": True,
        "scope": "review fixture", "scope_sha256": "fixture",
        "selected_next_action": {"phase": "REVIEW", "type": "worker_action", "action": "Review it"},
    })
    now = int(time.time())
    board.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,error,metadata) "
        "VALUES(?,?,'crashed',?,?, 'crashed',?,?)",
        (task_id, "reviewer", now - 2, now - 1,
         "no dependency environment is committed for this install",
         json.dumps({"infrastructure": True, "environment_fingerprint": "host-generation"})),
    )
    seen = []

    def fingerprint(root_override=None):
        seen.append(root_override)
        return {"identity": "workspace-repaired", "healthy": True}

    monkeypatch.setattr(decision_module, "environment_fingerprint", fingerprint)
    decision = decide(board, kb.get_task(board, task_id))
    assert seen == [workspace.resolve()]
    assert decision["execution_health"]["state"] == "HEALTHY"
    assert decision["workflow_stage"] == "REVIEW"
    assert decision["dispatchable"] is True


def test_dead_source_workspace_records_its_own_environment_fingerprint(tmp_path, monkeypatch):
    import hermes_cli.kanban_decision as decision_module

    workspace = _source_workspace(tmp_path)
    monkeypatch.setattr(
        kbd, "_classify_dead_worker_exit",
        lambda *args, **kwargs: kbd._DeadWorker(
            "nonzero_exit", 1, "no dependency environment is committed for this install",
            "crashed", {"pid": 7},
        ),
    )
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *args, **kwargs: "")
    seen = []

    def fingerprint(root_override=None):
        seen.append(root_override)
        return {"identity": "workspace-generation", "healthy": False}

    monkeypatch.setattr(decision_module, "environment_fingerprint", fingerprint)
    dead = kbd._classify_dead_worker(
        7, "host:claim", task_id="t_fixture", workspace_path=str(workspace),
    )
    assert seen == [workspace.resolve()]
    assert dead.infrastructure is True
    assert dead.event_payload["environment_fingerprint"] == "workspace-generation"
