"""Housekeeping and completion must preserve native workflow meaning."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_administrative_hold import administrative_pending
from hermes_cli.kanban_completion_evidence import (
    CompletionEvidenceError, _children_missing, contract_record, validate_contract,
)
from hermes_cli.kanban_decision import dependencies_satisfied


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with kbc.connect_closing() as conn:
        yield conn


def _contract(conn, tid, **fields):
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    ensure_scope_contract(conn, tid)
    value = dict(contract_record(conn, tid)[1] or {})
    value.update(version=1, kind="implementation", local_only=True, criteria=["verified"])
    value.update(fields)
    validate_contract(value)
    kb._append_event(conn, tid, "completion_requirements", value)
    return contract_record(conn, tid)


def _review_and_complete(conn, tid):
    assert kb.request_review(conn, tid, summary="Implementation retained", reviewer="reviewer")
    assert kb.complete_task(conn, tid, summary="Independent verification passed")


def test_gc_preserves_contract_acceptance_and_released_hold(board):
    from hermes_cli.kanban_administrative_hold import (
        release_administrative_pending, set_administrative_pending,
    )
    from hermes_cli.kanban_dependency_output import accept, certify_output, declare

    child = kb.create_task(board, title="Implementation", assignee="builder")
    cid, contract = _contract(board, child)
    set_administrative_pending(
        board, child, expected_status=kb.get_task(board, child).status,
        expected_assignee="builder", expected_contract_event=cid, reason="Bind retained evidence",
    )
    assert release_administrative_pending(board, child, reason="Evidence now bound")
    aid = kb.store_attachment_bytes(board, child, "result.txt", b"verified implementation")
    receipt = certify_output(
        board, child, contract_event_id=cid, artifact_attachment_id=aid, criterion_id="verified",
        evidence={"reference": "fixture://verification", "assertions": {
            "output_verified": True, "applicability_verified": True, "authority_verified": True,
        }},
    )
    downstream = kb.create_task(board, title="Consume accepted output", parents=[child])
    declare(board, downstream, child, "output", evidence_identity=receipt["evidence_identity"],
            requirement="Use verified implementation")
    declaration_id = board.execute(
        "SELECT max(id) FROM task_events WHERE task_id=? AND kind='dependency_requirement'",
        (downstream,),
    ).fetchone()[0]
    accept(board, downstream, "output", declaration_event_id=declaration_id,
           producer_evidence_event_id=receipt["producer_evidence_event_id"])
    _review_and_complete(board, child)
    assert dependencies_satisfied(board, downstream)
    assert not administrative_pending(board, child)
    kb._append_event(board, child, "heartbeat", None)
    kb._append_event(board, child, "heartbeat", {"note": "Retained implementation progress"})
    # Unknown future/plugin event kinds must not become disposable by default.
    kb._append_event(board, child, "plugin_acceptance", {"reference": "retained"})
    board.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (child,))

    assert kb.gc_events(board, older_than_seconds=1) > 0
    assert contract_record(board, child) == (cid, contract)
    assert dependencies_satisfied(board, downstream)
    assert not administrative_pending(board, child)
    assert kb.get_task(board, child).status == "done"
    assert [event.payload for event in kb.list_events(board, child) if event.kind == "heartbeat"] == [
        {"note": "Retained implementation progress"},
    ]
    assert board.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='plugin_acceptance'", (child,),
    ).fetchone()


@pytest.mark.parametrize("path", ["complete", "close"])
@pytest.mark.parametrize("state", ["satisfied", "unsatisfied", "stale"])
def test_completion_conditions_agree_and_reject_stale_receipts(board, path, state):
    from hermes_cli.kanban_wait_contract import condition_digest, declaration
    from hermes_cli.kanban_wait_transitions import declare_wait, resolve_gate

    tid = kb.create_task(board, title="Verify retained result", assignee="builder")
    condition = {"condition_id": "approval", "requirement": "Approve retained result"}
    _contract(board, tid, completion_conditions=[condition])
    gate = declaration(
        "approval", "attestation", resolver="operator", action="Verify retained result",
        reason="Independent acceptance required", parameters={
            "condition_id": "approval", "native_condition_sha256": condition_digest(condition),
            "required_assertions": ["verified"],
        },
    )
    declare_wait(board, tid, [gate], {"state": "todo"}, provenance={"source": "test"})
    if state != "unsatisfied":
        resolve_gate(board, tid, gate["id"], revision=gate["revision"],
                     declaration=gate["declaration"], evidence={
                         "reference": "fixture://acceptance", "sha256": "a" * 64,
                         "assertions": {"verified": True},
                     })
    if state == "stale":
        _contract(board, tid, completion_conditions=[{**condition, "requirement": "Approve revised result"}])
    # Enter the public completion states without replacing the condition contract.
    if path == "complete":
        assert kb.request_review(board, tid, summary="Ready for acceptance", reviewer="reviewer")
        finish = kb.complete_task
        error = CompletionEvidenceError
    else:
        assert kb.block_task(board, tid, reason="Operator close required", kind="needs_input")
        finish = kb.close_task
        error = kb.OperatorCloseRefused
    if state == "satisfied":
        assert finish(board, tid, summary="Accepted retained result")
        assert kb.get_task(board, tid).status == "done"
    else:
        with pytest.raises(error, match="acceptance|condition"):
            finish(board, tid, summary="Cannot waive the outstanding condition")
        assert kb.get_task(board, tid).status != "done"


def test_decomposed_children_run_and_satisfy_program_even_after_gc(board):
    from hermes_cli.kanban_db_graph import decompose_triage_task

    root = kb.create_task(board, title="Deliver programme", triage=True)
    children = decompose_triage_task(board, root, root_assignee="builder", children=[
        {"title": "Implement first part", "assignee": "builder"},
        {"title": "Implement second part", "assignee": "builder", "parents": [0]},
    ])
    assert len(children) == 2
    _, program = _contract(board, root, kind="program", required_children=children,
                           legacy_completion_repair=False)
    for child in children:
        _contract(board, child, legacy_completion_repair=False)
    for child in children:
        assert _children_missing(board, root, program)
        assert not kb.complete_task(board, root, summary="Programme unfinished")
        kb.recompute_ready(board)
        run = kb.claim_task(board, child, claimer="builder:test")
        assert run is not None
        assert kb.request_review(board, child, summary="Part implemented", reviewer="reviewer",
                                 expected_run_id=run.current_run_id)
        assert kb.complete_task(board, child, summary="Part independently accepted")
    assert not _children_missing(board, root, program)
    for child in children:
        kb._append_event(board, child, "heartbeat", None)
        board.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (child,))
    assert kb.gc_events(board, older_than_seconds=1) > 0
    assert not _children_missing(board, root, program)
    _review_and_complete(board, root)
    assert kb.get_task(board, root).status == "done"
