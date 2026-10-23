"""Regression: only the migration's *own* block representation is releasable.

The wait lane's conservative migration (``kanban_wait_migration``) wraps every
native block in an operator-attestation gate ``blocked:<event_id>`` so the block
is *represented* to the decision layer. ``unblock_task`` may retire exactly that
representation — it is a record of the card's own block, not a predicate anyone
declared. It must never retire an independently declared predicate that merely
*looks* like one.

Reproduces the reviewer's counterexample for t_22000147: an operator declaration
that reuses the migration's id shape, its block-bound ``source_event_id`` and a
copy of the block payload, but declares a different obligation, was silently
retired by an earlier revision of the guard. These tests pin the boundary:

* a plain migration-owned representation is releasable by an ordinary unblock;
* a declared lookalike keeps its predicate and ``unblock`` raises;
* a verbatim copy of the representation declared under a foreign provenance also
  keeps its veto (the representation is proven by origin *and* shape, not shape
  alone);
* and origin survives a *later* ordinary migration — the wait's global provenance
  tag is re-stamped ``native-marker-only-v1`` when any unrelated gate is appended,
  so it cannot launder a foreign gate into a releasable one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_wait_contract import declaration, facts, record
from hermes_cli.kanban_wait_transitions import declare_wait, resolve_gate


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    with connect(tmp_path / "board.db") as conn:
        yield conn


def _blocked_task(conn, *, title: str, reason: str, kind: str):
    """A card driven to ``running`` then blocked, returning (task_id, block_event_id, payload)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    kb.recompute_ready(conn)
    run = kb.claim_task(conn, tid, claimer="worker")
    assert run is not None
    assert kb.block_task(conn, tid, reason=reason, kind=kind, expected_run_id=run.current_run_id)
    event = conn.execute(
        "SELECT id,payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    return tid, event[0], json.loads(event[1])


def test_plain_migration_representation_is_releasable(board):
    """A bare worker block that names no resolver is released by an ordinary unblock."""
    tid, block_id, _ = _blocked_task(board, title="plain", reason="waiting", kind="needs_input")
    kb.recompute_ready(board)
    wait = facts(board, tid)
    assert wait["provenance"]["migration"] == "native-marker-only-v1"
    assert [g["id"] for g in wait["remaining_gates"]] == ["blocked:" + str(block_id)]
    assert kb.unblock_task(board, tid) is True
    assert kb.get_task(board, tid).status != "blocked"
    assert not (facts(board, tid) or {}).get("remaining_gates")


def test_declared_lookalike_keeps_its_predicate_and_unblock_raises(board):
    """An explicitly declared block-bound attestation is not the migration's representation."""
    tid, block_id, payload = _blocked_task(
        board, title="lookalike", reason="External verification needed", kind="needs_input")
    gate = declaration(
        "blocked:" + str(block_id), "attestation", resolver="operator",
        parameters={
            "source_event_id": block_id,
            "native_condition": payload,
            "required_assertions": ["external_verification_complete"],
        },
        action="Attest external verification before release",
        reason="Explicit operator-declared predicate",
        stage="BLOCKED",
    )
    declare_wait(board, tid, [gate], {"state": "todo"},
                 provenance={"source": "explicit_operator_declaration_not_migration"})
    before = facts(board, tid)
    assert [g["id"] for g in before["remaining_gates"]] == ["blocked:" + str(block_id)]
    with pytest.raises(PermissionError):
        kb.unblock_task(board, tid)
    after = facts(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
    assert [g["id"] for g in after["remaining_gates"]] == ["blocked:" + str(block_id)]
    assert conn_receipts(board, tid) == 0


def test_verbatim_copy_under_foreign_provenance_keeps_its_veto(board):
    """A shape-identical copy declared by a foreign authority is not proven to be the migration's."""
    tid, block_id, _ = _blocked_task(board, title="verbatim-copy", reason="waiting", kind="needs_input")
    kb.recompute_ready(board)
    verbatim = dict(facts(board, tid)["gates"][0])
    declare_wait(board, tid, [verbatim], {"state": "todo"},
                 provenance={"source": "explicit_operator_declaration_not_migration"})
    assert [g["id"] for g in facts(board, tid)["remaining_gates"]] == ["blocked:" + str(block_id)]
    with pytest.raises(PermissionError):
        kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
    assert [g["id"] for g in facts(board, tid)["remaining_gates"]] == ["blocked:" + str(block_id)]


def conn_receipts(conn, task_id: str, gate_id: str | None = None) -> int:
    sql = "SELECT count(*) FROM task_events WHERE task_id=? AND kind='wait_gate_satisfied'"
    params: list = [task_id]
    if gate_id is not None:
        sql += " AND json_extract(payload,'$.gate_id')=?"
        params.append(gate_id)
    return conn.execute(sql, params).fetchone()[0]


def test_foreign_gate_origin_survives_a_later_normal_migration(board):
    """Provenance laundering: the wait's *global* tag does not prove a gate's origin.

    A foreign-provenance verbatim copy vetoes its wait (previous test) only until
    the next ordinary migration touches the card: ``kanban_wait_migration``
    deep-copies existing gates and then re-stamps the whole wait
    ``native-marker-only-v1`` when it appends an unrelated gate. Trusting the
    wait-level tag therefore retired the foreign predicate the moment a second,
    genuinely unrelated condition was declared and satisfied — with zero
    satisfaction receipts for the block gate. Origin must be read per gate from
    the immutable ``wait_contract_declared`` log instead.
    """
    tid, block_id, _ = _blocked_task(board, title="laundered", reason="waiting", kind="needs_input")
    kb.recompute_ready(board)
    foreign = {"source": "explicit_operator_declaration_not_migration"}
    verbatim = dict(facts(board, tid)["gates"][0])
    declare_wait(board, tid, [verbatim], {"state": "todo"}, provenance=foreign)
    with pytest.raises(PermissionError):
        kb.unblock_task(board, tid)

    # The next ordinary migration appends an unrelated external condition and
    # re-stamps the wait provenance, preserving the foreign gate verbatim.
    _, contract, _ = record(board, tid)
    contract = dict(contract)
    contract["external_conditions"] = [
        {"condition_id": "extra-check", "resolver": "operator",
         "requirement": "Verify the unrelated external condition"}
    ]
    with kb.write_txn(board):
        kb._append_event(board, tid, "completion_requirements", contract)
    kb.recompute_ready(board)
    wait = facts(board, tid)
    assert wait["provenance"]["migration"] == "native-marker-only-v1"
    assert [g["id"] for g in wait["remaining_gates"]] == ["blocked:" + str(block_id), "external:extra-check"]
    extra = next(g for g in wait["gates"] if g["id"] == "external:extra-check")
    resolve_gate(
        board, tid, extra["id"], revision=extra["revision"], declaration=extra["declaration"],
        evidence={"sha256": "a" * 64, "reference": "isolated-regression-fixture",
                  "assertions": {key: True for key in extra["parameters"]["required_assertions"]}},
    )

    # Only the unrelated condition is satisfied; the block gate has NO receipt.
    block_gate = "blocked:" + str(block_id)
    assert conn_receipts(board, tid, block_gate) == 0
    assert [g["id"] for g in facts(board, tid)["remaining_gates"]] == [block_gate]
    with pytest.raises(PermissionError):
        kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
    assert conn_receipts(board, tid, block_gate) == 0
    assert [g["id"] for g in facts(board, tid)["remaining_gates"]] == [block_gate]
