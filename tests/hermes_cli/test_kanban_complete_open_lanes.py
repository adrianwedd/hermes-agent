"""Any open lane may be closed; only a terminal card is refused, and it says so.

The board audit of 2026-10-30 found a shipped deliverable (clutterbusters-hub PR
#48 merged, issue #47 closed) that could never be closed: `prepare_gate`
accepted only running/ready/blocked/review, so a card in `todo` was refused and
the CLI rendered that as "completion evidence snapshot changed; retry handoff"
— a false diagnosis that reads as a transient race, so every retry repeated it.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_completion_evidence import (
    OPEN_COMPLETION_STATUSES,
    CompletionEvidenceError,
    prepare_gate,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(tmp_path / "kanban.db") as conn:
        yield conn


@pytest.mark.parametrize("status", sorted(OPEN_COMPLETION_STATUSES))
def test_the_gate_accepts_every_open_lane(board, status):
    """`todo` is the lane that broke: a child whose parent has just finished, an
    operator-parked card. Refusing it is an unbreakable retry loop."""
    tid = kb.create_task(board, title=f"Card in {status}", workspace_kind="scratch")
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))

    assert prepare_gate(board, tid, {}) is not False


def test_a_terminal_card_is_refused_with_the_real_reason(board):
    tid = kb.create_task(board, title="Already finished", workspace_kind="scratch")
    kb.recompute_ready(board)
    run = kb.claim_task(board, tid, claimer="builder:test")
    assert run
    assert kb.complete_task(board, tid, result="done", expected_run_id=run.current_run_id)

    with pytest.raises(CompletionEvidenceError) as excinfo:
        prepare_gate(board, tid, {})

    message = str(excinfo.value)
    assert "already done" in message
    # Not the misleading race diagnosis the CLI used to print for every refusal.
    assert "snapshot changed" not in message.lower()


def test_a_stale_run_id_is_still_a_snapshot_race(board):
    """The one remaining `False` is a genuine ownership race, unchanged."""
    tid = kb.create_task(board, title="Running card", workspace_kind="scratch")
    kb.recompute_ready(board)
    run = kb.claim_task(board, tid, claimer="builder:test")
    assert run

    assert prepare_gate(board, tid, {}, expected_run_id=run.current_run_id + 999) is False


def test_a_todo_card_closes_end_to_end_once_its_parents_are_done(board):
    """The real shape: the parent finished, the child is still sitting in `todo`
    because promotion has not run yet. It must be closable, not loop forever."""
    parent = kb.create_task(board, title="Parent", workspace_kind="scratch")
    child = kb.create_task(board, title="Child", workspace_kind="scratch")
    kb.link_tasks(board, parent, child)
    kb.recompute_ready(board)
    assert kb.get_task(board, parent) is not None
    run = kb.claim_task(board, parent, claimer="builder:test")
    assert run

    # Close the parent, then force the child back to `todo` to model the promotion lag.
    assert kb.complete_task(board, parent, result="shipped", expected_run_id=run.current_run_id)
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))

    gate = prepare_gate(board, child, {})
    assert gate is not False
    assert kb.complete_task(board, child, result="shipped too", _prepared_evidence=gate)
    task = kb.get_task(board, child)
    assert task is not None and task.status == "done"


def test_a_promotion_between_gate_and_write_does_not_refuse_the_completion(board):
    """The race: the card is promoted `todo -> ready` after the gate snapshot.

    Nothing the gate depends on changed — same run, same assignee, same contract —
    so the completion must stand. Before this, the raw snapshot comparison returned
    a bare False that the CLI rendered as "unknown id or terminal state".
    """
    parent = kb.create_task(board, title="Parent", workspace_kind="scratch")
    child = kb.create_task(board, title="Child", workspace_kind="scratch")
    kb.link_tasks(board, parent, child)
    kb.recompute_ready(board)
    run = kb.claim_task(board, parent, claimer="builder:test")
    assert run and kb.complete_task(board, parent, result="shipped", expected_run_id=run.current_run_id)
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))

    gate = prepare_gate(board, child, {})

    # the routine promotion that every tick performs
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))

    assert kb.complete_task(board, child, result="shipped as well", _prepared_evidence=gate) is True
    assert kb.get_task(board, child).status == "done"
