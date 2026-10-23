"""Recovery decisions must describe the transition the operator can actually use.

Studio t_cfffeef6 retained an approved preparation review, but its empty native
block was presented as requiring a new attestation. The ordinary unblock path
already distinguishes migration-owned representations from real predicates.
"""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_wait_contract import facts
from hermes_cli.kanban_wait_transitions import declare_wait


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with connect(tmp_path / "board.db") as conn:
        yield conn


def blocked_review(conn):
    tid = kb.create_task(conn, title="Verify retained offline preparation", assignee="builder")
    kb.recompute_ready(conn)
    assert kb.request_review(conn, tid, summary="Offline preparation delivered", reviewer="reviewer")
    assert kb.claim_review_task(conn, tid, claimer="review-worker")
    assert kb.block_task(conn, tid)
    kb.recompute_ready(conn)
    return tid


def test_recovery_decision_names_native_unblock_and_restores_review(board):
    tid = blocked_review(board)
    before = facts(board, tid)
    decision = decide(board, kb.get_task(board, tid))
    assert f"unblock {tid}" in decision["current_unblock_action"]
    assert decision["current_next_action_type"] == "native_unblock"
    assert not decision["dispatchable"]
    assert facts(board, tid) == before  # Explaining recovery grants nothing.

    assert kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == "review"
    assert kb.complete_task(board, tid, summary="Retained preparation independently verified")
    assert kb.get_task(board, tid).status == "done"
    assert board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='wait_gate_satisfied'",
        (tid,),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("protected", ["foreign_declaration", "explicit_stop"])
def test_recovery_decision_does_not_offer_unblock_for_protected_gate(board, protected):
    tid = blocked_review(board)
    if protected == "foreign_declaration":
        wait = facts(board, tid)
        declare_wait(board, tid, wait["gates"], wait["continuation"],
                     provenance={"source": "operator-declared predicate"})
    else:
        board.execute("UPDATE tasks SET block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
        kb._append_event(board, tid, "blocked", {"kind": "explicit_stop", "reason": "No live experiment authorised"})
        kb.recompute_ready(board)
    decision = decide(board, kb.get_task(board, tid))
    assert decision["current_next_action_type"] != "native_unblock"
    with pytest.raises(PermissionError):
        kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
