"""Accepted terminal scope is history, not outstanding work (Studio t_cfffeef6)."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_completion_evidence import contract_record
from hermes_cli.kanban_decision import claim_allowed, decide


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with connect(tmp_path / "board.db") as conn:
        yield conn


def accepted_task(conn, path="complete"):
    tid = kb.create_task(conn, title="Retain verified offline output", assignee="builder")
    kb.recompute_ready(conn)
    if path == "complete":
        assert kb.request_review(conn, tid, summary="Output retained", reviewer="reviewer")
        assert kb.complete_task(conn, tid, summary="Independent verification accepted")
    else:
        assert kb.block_task(conn, tid, reason="Operator acceptance needed")
        assert kb.close_task(conn, tid, summary="Operator accepted retained output")
    return tid


@pytest.mark.parametrize("path", ["complete", "close"])
def test_accepted_terminal_scope_has_no_outstanding_action_without_rewriting_history(board, path):
    tid = accepted_task(board, path)
    original = contract_record(board, tid)
    assert original[1]["remaining_required_actions"]
    events = list(board.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,)))
    task = kb.get_task(board, tid)
    decision = decide(board, task)
    assert decision["remaining_required_actions"] == []
    assert decision["selected_next_action"] == {}
    assert decision["current_next_action_type"] is None
    assert decision["current_next_action"] is None
    assert not decision["dispatchable"]
    assert decision["invariant_violations"] == []
    assert not claim_allowed(board, task, "ready")[0]
    assert contract_record(board, tid) == original
    assert list(board.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,))) == events
    assert kb.get_task(board, tid).status == "done"


@pytest.mark.parametrize("path", ["complete", "close"])
def test_acceptance_of_an_earlier_contract_does_not_hide_new_requirements(board, path):
    tid = accepted_task(board, path)
    _, original = contract_record(board, tid)
    revised = {**original, "remaining_required_actions": ["Verify additional scope"]}
    kb._append_event(board, tid, "completion_requirements", revised)
    decision = decide(board, kb.get_task(board, tid))
    assert decision["remaining_required_actions"] == ["Verify additional scope"]
    assert "DONE && remaining_required_actions" in decision["invariant_violations"]
    assert not decision["dispatchable"]
