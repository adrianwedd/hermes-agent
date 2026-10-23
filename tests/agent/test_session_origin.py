"""Worker origin uses only dispatcher-pinned board identity."""

import json

from agent.session_origin import kanban_worker_origin_json


def test_worker_origin_requires_source_task_and_board(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")
    assert json.loads(kanban_worker_origin_json("kanban")) == {
        "kanban_board": "board-a", "kanban_task": "task-a"}
    assert kanban_worker_origin_json("cli") is None
    monkeypatch.delenv("HERMES_KANBAN_BOARD")
    assert kanban_worker_origin_json("kanban") is None
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "board-a")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert kanban_worker_origin_json("kanban") is None
