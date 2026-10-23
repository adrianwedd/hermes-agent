"""A generic Ready override cannot create authority to run held work."""
from __future__ import annotations

import argparse
import json
from contextlib import nullcontext

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kanban_cli
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_dispatch_release import release, requires_operator_release
from hermes_cli.kanban_wait_contract import declaration, facts
from hermes_cli.kanban_wait_transitions import declare_wait

FENCE_KEYS = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
    "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_PARENT_NONCE",
    "HERMES_SESSION_SOURCE",
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for key in FENCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    with connect(tmp_path / "board.db") as conn:
        yield conn


def _task(conn, title="task", **kwargs):
    return kb.create_task(
        conn, title=title, body="Preserve the original request.",
        assignee="worker", workspace_kind="scratch", **kwargs,
    )


def _snapshot(conn, task_id):
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    events = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,),
    ).fetchall()
    runs = conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,),
    ).fetchall()
    return dict(task), [dict(row) for row in events], [dict(row) for row in runs]


def _explicit_stop(conn):
    task_id = _task(conn, "held by explicit STOP")
    kb.block_task(conn, task_id, reason="operator STOP", kind="needs_input")
    conn.execute(
        "UPDATE tasks SET status='blocked', block_kind='explicit_stop' WHERE id=?",
        (task_id,),
    )
    event_id = conn.execute(
        "SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'",
        (task_id,),
    ).fetchone()[0]
    gate = declaration(
        f"stop:{event_id}", "explicit_stop", resolver="operator",
        parameters={"stop_event_id": event_id,
                    "required_assertions": ["explicit_stop_revoked"]},
        action="Record an authorised STOP revocation",
        reason="Explicit STOP remains in force",
    )
    declare_wait(conn, task_id, [gate], {"state": "todo"},
                 provenance={"test": "explicit-stop"})
    kb.recompute_ready(conn)
    return task_id, gate


def test_ready_override_cannot_revoke_explicit_stop(board):
    task_id, gate = _explicit_stop(board)
    before = _snapshot(board, task_id)

    with pytest.raises(PermissionError, match="STOP"):
        release(board, task_id, reason="I want it to run")

    assert _snapshot(board, task_id) == before
    assert any(item["id"] == gate["id"] and not item["satisfied"]
               for item in facts(board, task_id)["gates"])
    assert kb.claim_task(board, task_id, claimer="unauthorised") is None


def test_ready_override_cannot_waive_unfinished_dependency(board):
    parent = _task(board, "unfinished parent")
    child = _task(board, "dependent child", parents=[parent])
    before = _snapshot(board, child)

    with pytest.raises(PermissionError, match="dependency"):
        release(board, child, reason="run child first")

    assert _snapshot(board, child) == before
    assert board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? "
        "AND kind='dependency_requirement_waived'", (child,),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_ready_override_cannot_reopen_terminal_result(board, terminal):
    task_id = _task(board, "accepted result")
    if terminal == "done":
        # A terminal fixture tests the status boundary without fabricating a
        # production acceptance receipt or invoking the evidence gate.
        board.execute(
            "UPDATE tasks SET status='done', result='accepted', completed_at=123 "
            "WHERE id=?", (task_id,),
        )
    else:
        assert kb.archive_task(board, task_id)
    before = _snapshot(board, task_id)

    with pytest.raises(PermissionError, match="terminal"):
        release(board, task_id, reason="do it again")

    assert _snapshot(board, task_id) == before


def test_ready_override_cannot_reclaim_live_worker(board):
    task_id = _task(board, "claimed task")
    assert kb.claim_task(board, task_id, claimer="worker-owner") is not None
    before = _snapshot(board, task_id)

    with pytest.raises(PermissionError, match="live worker"):
        release(board, task_id, reason="take it back")

    assert _snapshot(board, task_id) == before


def test_ready_override_cannot_qualify_triage_or_reopen_review(board):
    triage_id = _task(board, "unqualified scope", triage=True)
    review_id = _task(board, "awaiting review")
    assert kb.request_review(board, review_id, reviewer="reviewer")
    for task_id in (triage_id, review_id):
        before = _snapshot(board, task_id)
        with pytest.raises(PermissionError):
            release(board, task_id, reason="start anyway")
        assert _snapshot(board, task_id) == before


def test_ready_override_cannot_be_run_from_worker_context(board, monkeypatch):
    task_id = _task(board, "worker context")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    with pytest.raises(PermissionError):
        release(board, task_id, reason="worker asks")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


def test_already_ready_release_is_a_truthful_no_op(board):
    task_id = _task(board, "already ready")
    task = kb.get_task(board, task_id)
    assert task.status == "ready"
    assert decide(board, task)["dispatchable"]
    before = _snapshot(board, task_id)

    result = release(board, task_id, reason="check Ready")

    assert result["ready"] is True
    assert result["applied"] is False
    assert result["released_gates"] == []
    assert result["waived_dependencies"] == []
    assert _snapshot(board, task_id) == before


def test_cli_force_cannot_revoke_stop(board, capsys, monkeypatch):
    task_id, _ = _explicit_stop(board)
    before = _snapshot(board, task_id)
    monkeypatch.setattr(kanban_cli.kbc, "connect_closing", lambda: nullcontext(board))
    args = argparse.Namespace(task_id=task_id, ids=None, reason=["generic override"],
                              force=True, dry_run=False, json=True)

    assert kanban_cli._cmd_promote(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is False
    assert "PermissionError" in output["error"]
    assert _snapshot(board, task_id) == before


def test_plain_ready_uses_native_unblock_not_generic_release(board):
    task_id, _ = _explicit_stop(board)
    assert requires_operator_release(board, task_id)
    with pytest.raises(PermissionError, match="STOP"):
        kb.unblock_task(board, task_id)
    assert kb.get_task(board, task_id).status == "blocked"
