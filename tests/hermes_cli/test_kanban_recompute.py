"""Focused tests for canonical inactive-card reconciliation."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_recompute_materializes_contract_before_promoting_scratch_task(kanban_home):
    """Control-plane bookkeeping cannot deadlock a new scratch Todo."""
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Inspect retained evidence",
            body="Review the retained report and record the bounded verdict.",
            assignee="reviewer",
            workspace_kind="scratch",
            initial_status="blocked",
        )
        conn.execute(
            "UPDATE tasks SET status='todo',dispatch_eligible=1,block_kind=NULL WHERE id=?",
            (task_id,),
        )
        conn.commit()
        assert kb.get_task(conn, task_id).workspace_path is None

        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, task_id).status == "ready"
        contract_event, contract = contract_record(conn, task_id)
        assert contract_event is not None
        assert validate_contract(contract) == contract
        assert contract["materialized_by"] == "control_plane_pre_promotion"
