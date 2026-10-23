"""Regressions for real review claims, repair routing and truthful holds."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_completion_evidence import contract_record
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_dispatch_facts import dispatch_facts


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(tmp_path / "kanban.db") as conn:
        yield conn


def implementation_review(conn):
    tid = kb.create_task(conn, title="Implement bounded export", body="Fix one defect and retain verification.",
                         assignee="builder", workspace_kind="scratch")
    kb.recompute_ready(conn)
    run = kb.claim_task(conn, tid, claimer="builder:test")
    assert run
    assert kb.request_review(conn, tid, summary="Verified implementation retained.", reviewer="reviewer",
                             expected_run_id=run.current_run_id)
    review = kb.claim_review_task(conn, tid, claimer="reviewer:test")
    assert review
    return tid, review


def test_claimed_independent_review_can_complete_local_implementation(board):
    tid, review = implementation_review(board)
    assert kb.complete_task(board, tid, summary="Approved independently.", expected_run_id=review.current_run_id)
    assert kb.get_task(board, tid).status == "done"
    assert any(event.kind == "completion_evidence_accepted" for event in kb.list_events(board, tid))


def test_changes_select_exact_repair_and_survive_recompute(board):
    tid, review = implementation_review(board)
    reason = "Correct only linked-worktree common-dir counting; retain existing controls."
    assert kb.request_changes(board, tid, reason=reason, expected_run_id=review.current_run_id) == (True, "builder")
    kb.recompute_ready(board)
    d = decide(board, kb.get_task(board, tid))
    assert d["workflow_stage"] == "READY" and d["dispatchable"]
    _, contract = contract_record(board, tid)
    assert reason in contract["selected_next_action"]["action"]
    assert contract["selected_next_action"]["review_run_id"] == review.current_run_id
    repair = kb.claim_task(board, tid, claimer="builder:repair")
    assert repair
    assert kb.request_review(board, tid, summary="Requested correction verified.", expected_run_id=repair.current_run_id)
    assert kb.get_task(board, tid).assignee == "reviewer"


def test_prepare_keeps_eligibility_until_workspace_supplied(board, tmp_path):
    workspace = tmp_path / "source"
    tid = kb.create_task(board, title="Inspect retained report", body="Read and classify its evidence.",
                         assignee="researcher", workspace_kind="dir", workspace_path=str(workspace))
    kb.recompute_ready(board)
    assert kb.get_task(board, tid).dispatch_eligible
    workspace.mkdir(exist_ok=True)
    kb.recompute_ready(board)
    assert decide(board, kb.get_task(board, tid))["dispatchable"]


def test_disabled_flag_without_hold_is_workflow_fault(board):
    tid = kb.create_task(board, title="Classify retained observations", body="Offline source labels only.",
                         assignee="researcher", workspace_kind="scratch")
    kb.recompute_ready(board)
    board.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (tid,))
    d = decide(board, kb.get_task(board, tid))
    assert d["workflow_stage"] == "WORKFLOW_FAULT"
    assert d["owner"] == "control plane"
    assert dispatch_facts(board, kb.get_task(board, tid))["column"] == "workflow"


def test_stop_and_actual_operator_decision_have_separate_columns(board):
    tid = kb.create_task(board, title="Do not activate experiment", assignee="builder", initial_status="blocked")
    board.execute("UPDATE tasks SET block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
    assert dispatch_facts(board, kb.get_task(board, tid))["column"] == "held"
    other = kb.create_task(board, title="Choose authorised source", assignee="builder", initial_status="blocked")
    kb._append_event(board, other, "operator_decision_pending", {"reason": "Choose source boundary", "resolver": "Adrian", "next_action": "Name authorised checkout"})
    assert dispatch_facts(board, kb.get_task(board, other))["column"] == "operator"


def test_archival_keeps_supersession_history_and_releases_child(board):
    original = kb.create_task(board, title="Original repair", initial_status="blocked")
    replacement = kb.create_task(board, title="Comprehensive replacement", initial_status="blocked")
    board.execute("UPDATE tasks SET status='done' WHERE id=?", (replacement,))
    child = kb.create_task(board, title="Review comprehensive retained repair", parents=[original], assignee="reviewer")
    kb.add_comment(board, original, "operator", "Superseded by " + replacement)
    assert kb.archive_task(board, original)
    assert kb.get_task(board, original).status == "archived"
    assert kb.list_comments(board, original)
    assert decide(board, kb.get_task(board, child))["dispatchable"]


def test_untyped_column_placement_is_a_workflow_fault_not_a_blocker(board):
    tid = kb.create_task(board, title="Retained bounded task", assignee="worker", initial_status="blocked")
    d = decide(board, kb.get_task(board, tid))
    assert d["workflow_stage"] == "WORKFLOW_FAULT"
    assert d["owner"] == "control plane" and not d["dispatchable"]


def test_waiting_prerequisite_completion_preserves_authorised_eligibility(board):
    parent = kb.create_task(board, title="Retain prerequisite", initial_status="blocked")
    child = kb.create_task(board, title="Audit retained prerequisite", body="Read the retained evidence only.",
                           assignee="reviewer", parents=[parent], workspace_kind="scratch")
    kb.recompute_ready(board)
    assert decide(board, kb.get_task(board, child))["workflow_stage"] == "WAITING"
    assert kb.get_task(board, child).dispatch_eligible
    board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    kb.recompute_ready(board)
    assert decide(board, kb.get_task(board, child))["dispatchable"]


def test_implementation_run_cannot_launder_itself_as_review_approval(board):
    from hermes_cli.kanban_completion_evidence import CompletionEvidenceError
    tid = kb.create_task(board, title="Implement bounded fix", body="Retain executable verification and independent review.",
                         assignee="builder", workspace_kind="scratch")
    kb.recompute_ready(board)
    run = kb.claim_task(board, tid, claimer="builder:test")
    assert run
    with pytest.raises(CompletionEvidenceError):
        kb.complete_task(board, tid, summary="I approve myself.", expected_run_id=run.current_run_id)
    assert kb.get_task(board, tid).status == "running"


def test_new_missing_worktree_is_materialised_before_claim(board, tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
    destination = repo / ".worktrees" / "research"
    tid = kb.create_task(board, title="Research bounded retained source", body="Read source and retain provenance.",
                         assignee="researcher", workspace_kind="worktree", workspace_path=str(destination))
    kb.recompute_ready(board)
    kb.recompute_ready(board)
    assert destination.is_dir()
    assert decide(board, kb.get_task(board, tid))["dispatchable"]


def test_review_approval_cannot_erase_a_later_original_scope_condition(board):
    from hermes_cli.kanban_completion_evidence import CompletionEvidenceError
    tid, review = implementation_review(board)
    _, contract = contract_record(board, tid)
    updated = dict(contract, completion_conditions=[{"condition_id": "live-proof", "requirement": "Retained original scope requires approved Stage B evidence", "resolver": "Adrian and runtime owner", "satisfied": False}])
    kb._append_event(board, tid, "completion_requirements", updated)
    with pytest.raises(CompletionEvidenceError, match="Original-scope completion condition"):
        kb.complete_task(board, tid, summary="Only Stage A approved.", expected_run_id=review.current_run_id)
    assert kb.get_task(board, tid).status == "running"


def test_old_administrative_handoff_does_not_disable_new_revision_review(board):
    tid, review = implementation_review(board)
    kb._append_event(board, tid, "operator_review_handoff", {"run_id": review.current_run_id})
    assert kb.request_changes(board, tid, reason="Correct retained source identity.", expected_run_id=review.current_run_id)[0]
    kb.recompute_ready(board)
    repair = kb.claim_task(board, tid, claimer="builder:repair")
    assert repair
    assert kb.request_review(board, tid, summary="New revision corrected.", expected_run_id=repair.current_run_id)
    decision = decide(board, kb.get_task(board, tid))
    assert decision["workflow_stage"] == "REVIEW" and decision["dispatchable"]


def test_review_correction_preserves_future_original_scope_obligations(board):
    from hermes_cli.kanban_review_transition import select_review_correction
    tid, review = implementation_review(board)
    _, before = contract_record(board, tid)
    original = dict(before)
    original["remaining_required_actions"] = ["Stage B: retain independently reviewed live qualification."]
    kb._append_event(board, tid, "completion_requirements", original)
    select_review_correction(board, tid, "Correct only the offline harness.", review.current_run_id)
    _, after = contract_record(board, tid)
    assert after["remaining_required_actions"][0] == original["remaining_required_actions"][0]
    assert after["criteria"] == original["criteria"]
    assert after["selected_next_action"]["type"] == "review_correction"


@pytest.mark.parametrize("stage,column", [("HELD", "held"), ("OPERATOR", "operator"), ("WORKFLOW_FAULT", "workflow"), ("DONE", "done")])
def test_historical_execution_fault_does_not_mask_canonical_gate(board, monkeypatch, stage, column):
    from hermes_cli import kanban_dispatch_facts as module
    tid = kb.create_task(board, title="Protected scope", assignee="worker", initial_status="blocked")
    task = kb.get_task(board, tid)
    canonical = decide(board, task)
    canonical["workflow_stage"] = stage
    canonical["execution_health"] = {"state": "INFRASTRUCTURE_FAULT"}
    monkeypatch.setattr(module, "decide", lambda conn, task: canonical)
    assert module.dispatch_facts(board, task)["column"] == column


def test_scheduled_authority_hold_does_not_claim_time_will_release_it(board):
    from hermes_cli.kanban_wait_contract import declaration
    from hermes_cli.kanban_wait_transitions import declare_wait
    import json
    tid = kb.create_task(board, title="Run measured trial", assignee="bench", workspace_kind="scratch")
    gate = declaration("measurement-window", "attestation", resolver="Adrian and runtime controller",
                       parameters={"required_assertions": ["explicit_window_granted", "six_preflight_checks_recorded"],
                                   "condition": "Applicable window grant and all six preflight checks recorded."},
                       reason="Explicit exclusive-window approval remains absent.",
                       action="Record the applicable grant and all six preflight checks", stage="WAITING")
    declare_wait(board, tid, [gate], {"state": "todo", "selected_next_action": {
        "phase": "READY", "type": "worker_action", "action": "Run the authorised measured trial"}},
        provenance={"fixture": "qualified authority condition, not legacy scheduling prose"})
    d = decide(board, kb.get_task(board, tid))
    assert d["workflow_stage"] == "WAITING" and not d["dispatchable"]
    assert d["owner"] == "Adrian and runtime controller"
    assert d["resume_condition"]["evaluator"] == "attestation"
    condition = json.dumps(d["resume_condition"])
    assert "scheduled time" not in condition
    assert "six preflight" in condition
    assert kb.claim_task(board, tid) is None


def test_unbound_scheduled_prose_stays_a_workflow_fault(board):
    tid = kb.create_task(board, title="Run measured trial", assignee="bench", workspace_kind="scratch")
    assert kb.schedule_task(board, tid, reason="Explicit exclusive-window approval remains absent.")
    kb._append_event(board, tid, "scheduled", {"reason": "Explicit exclusive-window approval remains absent.",
                    "resolver": "Adrian and runtime controller", "resume_condition": "A grant is still required."})
    d = decide(board, kb.get_task(board, tid))
    assert d["workflow_stage"] == "WORKFLOW_FAULT" and not d["dispatchable"]
    assert kb.claim_task(board, tid) is None


def test_bare_scheduled_placement_is_a_workflow_fault(board):
    tid = kb.create_task(board, title="Unspecified wait", assignee="worker")
    board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    assert decide(board, kb.get_task(board, tid))["workflow_stage"] == "WORKFLOW_FAULT"


def declared_phase(conn, tid):
    _, contract = contract_record(conn, tid)
    contract["completion_conditions"] = [{"id": "measurement", "requirement": "Authorised measured results retained", "satisfied": False}]
    contract["review_scope"] = {"type": "bounded_phase", "action": "Prepare offline protocol", "next_action": {"phase": "WAITING_FOR_AUTHORITY", "type": "authority_grant", "action": "Grant explicit exclusive measurement window", "resolver": "Adrian and runtime controller", "resume_condition": "Applicable grant and all preflight checks recorded"}}
    kb._append_event(conn, tid, "completion_requirements", contract)


def test_native_phase_review_acceptance_waits_without_completing_goal(board):
    from hermes_cli.kanban_review_transition import accept_bounded_review
    tid, review = implementation_review(board)
    declared_phase(board, tid)
    assert accept_bounded_review(board, tid, "Offline protocol independently approved.", expected_run_id=review.current_run_id)
    task = kb.get_task(board, tid)
    assert task.status == "todo" and task.current_run_id is None
    decision = decide(board, task)
    assert decision["workflow_stage"] == "WAITING" and not decision["dispatchable"]
    assert kb.claim_task(board, tid) is None
    _, contract = contract_record(board, tid)
    assert contract["completion_conditions"][0]["satisfied"] is False
    assert not any(e.kind == "completed" for e in kb.list_events(board, tid))


def test_implementation_cannot_use_phase_acceptance_to_self_review(board):
    from hermes_cli.kanban_review_transition import accept_bounded_review
    tid = kb.create_task(board, title="Prepare experiment", assignee="builder", workspace_kind="scratch")
    kb.recompute_ready(board)
    run = kb.claim_task(board, tid, claimer="builder:test")
    declared_phase(board, tid)
    assert accept_bounded_review(board, tid, "Self approval", expected_run_id=run.current_run_id) is False
    assert kb.get_task(board, tid).status == "running"


def test_bounded_review_judge_uses_phase_while_completion_judges_original_goal(board, monkeypatch):
    from tools import kanban_tools as kt
    from hermes_cli.kanban_review_transition import bounded_review_goal
    tid, _ = implementation_review(board)
    declared_phase(board, tid)
    goals = []
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(kt, "judge_goal", lambda **kw: (goals.append(kw["goal"]) or ("done", "ok", None, None, False)))
    task = kb.get_task(board, tid)
    from dataclasses import replace
    task = replace(task, goal_mode=True)
    kt._goal_gate("kanban_request_review", task, tid, "protocol ready", goal_scope=bounded_review_goal(board, tid))
    kt._goal_gate("kanban_complete", task, tid, "whole acceptance")
    assert "Prepare offline protocol" in goals[0] and "whole goal remains nonterminal" in goals[0]
    assert task.title in goals[1] and task.body in goals[1]


def test_qualified_selected_protocol_review_preserves_unmet_future_work(board):
    from hermes_cli.kanban_review_transition import bounded_review_goal, accept_bounded_review
    tid, review = implementation_review(board)
    _, contract = contract_record(board, tid)
    contract["selected_next_action"] = {"phase": "READY", "type": "offline_experiment_protocol", "action": "Prepare original offline protocol"}
    contract["completion_conditions"] = [{"id": "future-results", "requirement": "Retain original authorised empirical results", "resolver": "runtime owner and Adrian", "satisfied": False}]
    kb._append_event(board, tid, "completion_requirements", contract)
    assert "Prepare original offline protocol" in bounded_review_goal(board, tid)
    assert accept_bounded_review(board, tid, "Protocol approved only", expected_run_id=review.current_run_id)
    assert kb.get_task(board, tid).status == "todo"
    decision = decide(board, kb.get_task(board, tid))
    assert decision["workflow_stage"] == "WAITING" and not decision["dispatchable"]
    assert kb.claim_task(board, tid) is None
    _, after = contract_record(board, tid)
    assert after["criteria"] == contract["criteria"]
    assert after["completion_conditions"] == contract["completion_conditions"]
    assert not any(e.kind == "completed" for e in kb.list_events(board, tid))
