"""Receipt administration is an explicit operator handoff, never repeat worker work."""
import json

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_administrative_hold import administrative_pending, set_administrative_pending


@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    conn = kbc.connect(db_path=tmp_path / "kanban.db")
    tid = kb.create_task(conn, title="Substantive-complete audit fixture", workspace_kind="scratch")
    yield conn, tid
    conn.close()


def hold(conn, tid):
    task = kb.get_task(conn, tid)
    return set_administrative_pending(conn, tid, expected_status=task.status,
        expected_assignee=task.assignee, expected_contract_event=None,
        reason="Operator confirms completed audit; only receipt binding remains")


@pytest.mark.parametrize("status", ["todo", "ready", "review", "done"])
def test_operator_hold_survives_status_changes_and_forced_eligibility(card, status):
    conn, tid = card
    hold(conn, tid)
    with pytest.raises(ValueError, match="Administrative receipt hold"):
        kb.edit_task(conn, tid, dispatch_eligible=True)
    # Simulate a stale consumer restoring eligibility/status. Claim/recompute are backstops.
    conn.execute("UPDATE tasks SET status=?,dispatch_eligible=1 WHERE id=?", (status, tid))
    conn.commit()
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == status
    assert kb.claim_task(conn, tid) is None
    assert kb.claim_review_task(conn, tid) is None
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


def test_worker_or_stale_snapshot_cannot_declare_hold(card, monkeypatch):
    conn, tid = card
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(PermissionError):
        hold(conn, tid)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        set_administrative_pending(conn, tid, expected_status="done",
            expected_assignee=None, expected_contract_event=None, reason="Receipt only")
    assert not administrative_pending(conn, tid)
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, tid) is not None


def test_active_owner_is_preserved_and_no_worker_claim_implies_completion(card):
    conn, tid = card
    kb.recompute_ready(conn)
    run = kb.claim_task(conn, tid)
    assert run is not None
    task = kb.get_task(conn, tid)
    assert not administrative_pending(conn, tid)
    with pytest.raises(RuntimeError, match="active worker"):
        hold(conn, tid)
    assert kb.get_task(conn, tid).current_run_id == task.current_run_id
    assert kb.get_task(conn, tid).claim_lock == task.claim_lock


def test_corrupt_admin_event_remains_fail_closed(card):
    conn, tid = card
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                 (tid, "administrative_pending", json.dumps({"scope": "damaged"}), 1))
    conn.commit()
    assert administrative_pending(conn, tid)
    with pytest.raises(ValueError):
        kb.edit_task(conn, tid, dispatch_eligible=True)


def test_administrative_receipt_cannot_reenter_automatic_specification(card, monkeypatch):
    from contextlib import nullcontext
    from hermes_cli import kanban_specify
    conn, tid = card
    hold(conn, tid)
    conn.execute("UPDATE tasks SET status='triage',dispatch_eligible=1 WHERE id=?", (tid,))
    conn.commit()
    monkeypatch.setattr(kanban_specify.kbc, "connect_closing", lambda: nullcontext(conn))
    task, reason = kanban_specify._load_triage_task(tid)
    assert task is None
    assert "administrative receipt hold" in reason


def test_terminal_event_retention_does_not_erase_admin_stop(card):
    conn, tid = card
    hold(conn, tid)
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=?", (tid,))
    conn.commit()
    kb.gc_events(conn, older_than_seconds=0)
    assert administrative_pending(conn, tid)


def test_missing_operator_contract_parks_without_claiming_substantive_completion(card):
    from hermes_cli.kanban_completion_evidence import CompletionEvidenceError, set_requirements
    from hermes_cli.kanban_completion_workflow import require_declared_contract, operator_contract_pending
    conn, tid = card
    with pytest.raises(CompletionEvidenceError):
        require_declared_contract(conn, tid)
    assert not kb.get_task(conn, tid).dispatch_eligible
    assert operator_contract_pending(conn, tid)
    assert not administrative_pending(conn, tid)  # No inferred substantive-complete assertion.
    conn.execute("UPDATE tasks SET status='ready',dispatch_eligible=1 WHERE id=?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid) is None
    assert kb.claim_review_task(conn, tid) is None
    # An ordinary operator declaration resolves the structural hold, without fake completion.
    assert set_requirements(conn, tid, {"version": 1, "kind": "research", "criteria": ["existing_evidence"]},
        expected_status="ready", expected_assignee=None)
    assert not operator_contract_pending(conn, tid)
    assert kb.claim_task(conn, tid) is not None


def test_later_rejection_cannot_erase_missing_operator_declaration(card):
    from hermes_cli.kanban_completion_workflow import require_declared_contract, operator_contract_pending
    from hermes_cli.kanban_completion_evidence import CompletionEvidenceError
    conn, tid = card
    with pytest.raises(CompletionEvidenceError):
        require_declared_contract(conn, tid)
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
        (tid, "completion_evidence_rejected", '{broken', 2))
    conn.execute("UPDATE tasks SET status='ready',dispatch_eligible=1 WHERE id=?", (tid,))
    conn.commit()
    assert operator_contract_pending(conn, tid)
    assert kb.claim_task(conn, tid) is None


def test_stale_missing_contract_preflight_cannot_park_new_owner(card):
    from hermes_cli.kanban_completion_evidence import _snapshot, _refuse, CompletionEvidenceError
    from hermes_cli.kanban_completion_workflow import operator_contract_pending
    conn, tid = card
    old = _snapshot(conn, tid)
    conn.execute("UPDATE tasks SET assignee='new-owner' WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(CompletionEvidenceError, match="snapshot changed"):
        _refuse(conn, tid, ["Operator must declare requirements"], expected_snapshot=old)
    assert kb.get_task(conn, tid).dispatch_eligible
    assert not operator_contract_pending(conn, tid)
