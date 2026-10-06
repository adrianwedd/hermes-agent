"""Behavior contracts for the single Kanban next-action decision."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_decision import (
    auto_decompose_allowed,
    claim_allowed,
    decide,
    record_decision,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def _task(conn, title, *, status="ready", eligible=True, assignee="worker"):
    tid = kb.create_task(
        conn, title=title, body="Acceptance: one bounded action.", assignee=assignee,
        workspace_kind="scratch", initial_status="running",
    )
    conn.execute("UPDATE tasks SET status=?,dispatch_eligible=? WHERE id=?",
                 (status, int(eligible), tid))
    return tid


def _contract(conn, tid, **extra):
    payload = {
        "version": 1, "kind": "implementation", "local_only": True,
        "require_review": True, "require_commands": True,
        "criteria": ["authoritative_scope_satisfied"],
        "qualified_for_dispatch": True,
        "scope": "bounded fixture", "scope_sha256": "fixture",
        **extra,
    }
    kb._append_event(conn, tid, "completion_requirements", payload)
    return payload


def test_t_6a127187_waiting_authority_is_never_claimed_or_decomposed(board):
    tid = _task(board, "pruning live A/B", status="ready")
    _contract(board, tid, selected_next_action={
        "phase": "WAITING_FOR_AUTHORITY", "type": "live_experiment",
        "action": "Grant the live pruning A/B window", "resolver": "Adrian",
        "resume_condition": "operator grant is recorded",
    })
    task = kb.get_task(board, tid)
    decision = decide(board, task)
    assert decision["workflow_stage"] == "WAITING"
    assert decision["dispatchable"] is False
    assert decision["auto_decompose_allowed"] is False
    assert claim_allowed(board, task, "ready")[0] is False


def test_t_e5d24de5_review_flag_and_claim_guard_cannot_disagree(board):
    tid = _task(board, "dashboard review", status="review", eligible=False)
    _contract(board, tid)
    task = kb.get_task(board, tid)
    decision = decide(board, task)
    assert decision["workflow_stage"] == "HELD"
    assert decision["dispatchable"] is False
    assert any("claim rejects" in item for item in decision["invariant_violations"])
    assert claim_allowed(board, task, "review")[0] is False


def test_t_ebcf430c_infrastructure_fault_does_not_change_review_semantics(board):
    tid = _task(board, "independent MoA review", status="review")
    _contract(board, tid, kind="review", require_review=False, require_commands=False)
    now = int(time.time())
    board.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,error) "
        "VALUES(?,?,'crashed',?,?, 'spawn_failed',?)",
        (tid, "reviewer", now - 2, now - 1,
         "no dependency environment is committed for this install"),
    )
    decision = decide(board, kb.get_task(board, tid))
    assert decision["workflow_stage"] == "REVIEW"
    assert decision["execution_health"]["state"] == "INFRASTRUCTURE_FAULT"
    assert decision["dispatchable"] is False
    assert decision["resume_condition"] == "execution environment fingerprint changes"


def test_specific_dependency_evidence_releases_child_while_parent_remains_open(board):
    parent = _task(board, "produce shared policy", status="ready")
    child = _task(board, "run experiment", status="todo")
    _contract(board, parent)
    _contract(board, child)
    kb.link_tasks(board, parent, child)
    kb._append_event(board, child, "dependency_requirement", {
        "parent_id": parent, "requirement_id": "policy-v1",
        "requirement": "accepted shared policy v1", "evidence_identity": "sha256:abc",
    })
    waiting = decide(board, kb.get_task(board, child), status_override="ready")
    assert waiting["workflow_stage"] == "WAITING"
    kb._append_event(board, child, "dependency_requirement_satisfied", {
        "requirement_id": "policy-v1", "evidence_identity": "sha256:abc",
    })
    released = decide(board, kb.get_task(board, child), status_override="ready")
    assert released["workflow_stage"] == "READY"
    assert released["dispatchable"] is True
    assert kb.get_task(board, parent).status == "ready"


def test_unchanged_no_work_assessment_is_idempotent(board):
    tid = _task(board, "known future wait", status="triage")
    _contract(board, tid, remaining_required_actions=[])
    task = kb.get_task(board, tid)
    allowed, decision = auto_decompose_allowed(board, task)
    assert allowed is False
    with kbc.write_txn(board):
        assert record_decision(board, tid, decision, "no_worker_executable_action") is True
    allowed_again, same = auto_decompose_allowed(board, kb.get_task(board, tid))
    assert allowed_again is False
    assert same["decision_fingerprint"] == decision["decision_fingerprint"]
    with kbc.write_txn(board):
        assert record_decision(board, tid, same, "no_worker_executable_action") is False
    count = board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='control_decision_recorded'",
        (tid,),
    ).fetchone()[0]
    assert count == 1


def test_done_with_remaining_actions_is_an_impossible_state(board):
    tid = _task(board, "bad terminal", status="done")
    _contract(board, tid, remaining_required_actions=["still do work"])
    decision = decide(board, kb.get_task(board, tid))
    assert decision["workflow_stage"] == "DONE"
    assert "DONE && remaining_required_actions" in decision["invariant_violations"]


def test_waiting_resume_condition_satisfaction_atomically_enables_existing_action(board):
    """NotebookLM regression: successful auth cannot leave Ready + disabled."""
    from hermes_cli.kanban_resume import satisfy_resume_condition

    tid = _task(board, "retrieve NotebookLM sources", status="blocked", eligible=False)
    _contract(board, tid, kind="research", require_review=False, require_commands=True,
              selected_next_action={
                  "phase": "WAITING_FOR_AUTHORITY", "type": "external_authentication",
                  "action": "Authenticate Google profile default", "resolver": "Adrian",
                  "resume_condition": "nlm login --check succeeds",
              })
    before = decide(board, kb.get_task(board, tid))
    assert before["workflow_stage"] == "WAITING"

    after = satisfy_resume_condition(
        board, tid, condition="nlm login --check succeeds",
        evidence={"exit_code": 0, "profile": "default", "notebooks_visible": 479},
        next_action="retrieve/export supplied NotebookLM sources",
        expected_status="blocked", expected_decision_fingerprint=before["decision_fingerprint"],
    )
    task = kb.get_task(board, tid)
    assert (task.status, task.dispatch_eligible, task.block_kind) == ("ready", True, None)
    assert after["workflow_stage"] == "READY"
    assert after["dispatchable"] is True
    assert after["next_action"] == "retrieve/export supplied NotebookLM sources"


def test_resume_rolls_back_if_no_worker_action_can_become_ready(board):
    from hermes_cli.kanban_resume import satisfy_resume_condition

    parent = _task(board, "still required", status="ready")
    child = _task(board, "waiting child", status="blocked", eligible=False)
    _contract(board, parent)
    _contract(board, child, kind="research", require_review=False, require_commands=False)
    kb.link_tasks(board, parent, child)
    with pytest.raises(RuntimeError, match="did not produce"):
        satisfy_resume_condition(
            board, child, condition="authentication valid", evidence={"exit_code": 0},
            next_action="retrieve sources", expected_status="blocked",
        )
    task = kb.get_task(board, child)
    assert (task.status, task.dispatch_eligible) == ("blocked", False)
