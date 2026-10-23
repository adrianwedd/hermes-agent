"""``hermes kanban close`` — the CLI counterpart of the board's Close action.

The CLI previously had no way to close a blocked card: ``complete`` runs the
worker completion-evidence gate, which a held card cannot satisfy. ``close`` is
the explicit operator resolution and must round-trip through the same kernel
transition the REST route uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
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


def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _blocked_card(conn, title="cli close card"):
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    tid = kb.create_task(conn, title=title, assignee="default", body="real body")
    ensure_scope_contract(conn, tid)
    assert kb.block_task(conn, tid, reason="needs a human", kind="needs_input") is True
    return tid


def test_close_verb_closes_a_blocked_card(kanban_home):
    parser = _parser()
    with kbc.connect() as conn:
        tid = _blocked_card(conn)

    args = parser.parse_args(["kanban", "close", tid, "--resolution", "no longer needed"])
    rc = kc.kanban_command(args)
    assert rc == 0, rc

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.result == "no longer needed"
        assert task.completed_at is not None


@pytest.mark.parametrize("field", ["external_conditions", "completion_conditions"])
def test_close_refuses_unmet_explicit_acceptance(kanban_home, field):
    from hermes_cli.kanban_completion_evidence import contract_record

    with kbc.connect() as conn:
        tid = _blocked_card(conn)
        contract = contract_record(conn, tid)[1]
        contract[field] = [{"id": "adrian-approval", "requirement": "Explicit Adrian approval required",
                            "resolver": "Adrian", "satisfied": False}]
        kb._append_event(conn, tid, "completion_requirements", contract)
    args = _parser().parse_args(["kanban", "close", tid, "--resolution", "No approval supplied"])
    assert kc.kanban_command(args) != 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert contract_record(conn, tid)[1][field] == contract[field]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='resolved'", (tid,)).fetchone()


def test_close_verb_refuses_a_non_blocked_card(kanban_home):
    parser = _parser()
    with kbc.connect() as conn:
        from hermes_cli.kanban_completion_workflow import ensure_scope_contract
        tid = kb.create_task(conn, title="ready", assignee="default", body="b")
        ensure_scope_contract(conn, tid)

    args = parser.parse_args(["kanban", "close", tid, "--resolution", "x"])
    rc = kc.kanban_command(args)
    assert rc != 0

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_close_verb_is_distinct_from_complete(kanban_home):
    """``complete`` keeps the worker evidence gate; ``close`` is the operator door."""
    parser = _parser()
    with kbc.connect() as conn:
        tid = _blocked_card(conn, title="gate intact")

    # The worker completion path still refuses a blocked card without evidence.
    args = parser.parse_args(["kanban", "complete", tid, "--result", "operator says done"])
    rc = kc.kanban_command(args)
    assert rc != 0

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"

    # ... while the explicit Close resolves it.
    args = parser.parse_args(["kanban", "close", tid, "--resolution", "operator closed it"])
    assert kc.kanban_command(args) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
