"""Operator Close: a BLOCKED card becomes ``done`` without the worker manifest.

Regression for the reported failure: on the Kanban board a blocked card offered
only Archive, because every completion door (``PATCH {"status":"done"}``, bulk,
``hermes kanban complete``) runs the worker completion-evidence gate, which
demands ``completion-evidence.json`` plus an accepted command run — impossible
for a held card, and ``blocked`` cannot even reach REVIEW to be approved.

These assert the operator override closes the card, persists a durable
resolution, keeps the evidence gate intact for every other path, and still
refuses the transitions an operator close must not bypass. Archiving remains
available and unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_close_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


API = "/api/plugins/kanban"


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _declared_blocked(conn, client, title="blocked card", **create):
    """A blocked card carrying the completion contract the control plane writes."""
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    tid = kb.create_task(conn, title=title, assignee="default", body="real body", **create)
    ensure_scope_contract(conn, tid)
    r = client.patch(f"{API}/tasks/{tid}", json={"status": "blocked", "block_reason": "needs a human"})
    assert r.status_code == 200, r.text
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def test_close_blocked_card_succeeds(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)

    r = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "not doing this after all"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "done"

    stored = kb.get_task(conn, tid)
    assert stored.status == "done"
    assert stored.completed_at is not None
    assert stored.result == "not doing this after all"
    # The blocked provenance is cleared so the card no longer renders as held.
    assert stored.block_kind is None


def test_close_records_durable_resolution_event(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)

    client.post(f"{API}/tasks/{tid}/close", json={"resolution": "obsolete, superseded"})

    events = [dict(e) for e in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    resolved = [e for e in events if e["kind"] == "resolved"]
    assert resolved, f"no resolved event: {[e['kind'] for e in events]}"
    payload = json.loads(resolved[-1]["payload"])
    assert payload["resolution"] == "obsolete, superseded"
    assert payload["by"] == "operator_close"
    assert payload["prior_status"] == "blocked"


def test_close_without_resolution_records_a_default_reason(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)

    r = client.post(f"{API}/tasks/{tid}/close", json={})
    assert r.status_code == 200, r.text
    # The card must never close with empty evidence: the default names the actor.
    assert (kb.get_task(conn, tid).result or "").strip()


def test_close_persists_across_a_fresh_connection(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)
    client.post(f"{API}/tasks/{tid}/close", json={"resolution": "done with it"})
    conn.close()

    fresh = _conn()
    assert kb.get_task(fresh, tid).status == "done"
    board = client.get(f"{API}/board").json()
    done_ids = {t["id"] for col in board["columns"] if col["name"] == "done" for t in col["tasks"]}
    assert tid in done_ids


def test_completion_evidence_gate_still_refuses_a_bare_done(client):
    """The Close route is the only new door; the worker gate is unchanged."""
    conn = _conn()
    tid = _declared_blocked(conn, client)

    r = client.patch(f"{API}/tasks/{tid}", json={"status": "done", "summary": "operator closed"})
    assert r.status_code == 400
    assert "completion" in r.json()["detail"].lower()
    assert kb.get_task(conn, tid).status == "blocked"


def test_close_refuses_a_non_blocked_card(client):
    conn = _conn()
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    tid = kb.create_task(conn, title="ready card", assignee="default", body="b")
    ensure_scope_contract(conn, tid)

    r = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "x"})
    assert r.status_code == 409
    assert kb.get_task(conn, tid).status != "done"


def test_close_refuses_unsatisfied_parents(client):
    conn = _conn()
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    parent = kb.create_task(conn, title="parent", assignee="default", body="p")
    ensure_scope_contract(conn, parent)
    child = kb.create_task(conn, title="child", assignee="default", body="c", parents=[parent])
    ensure_scope_contract(conn, child)
    # A dependent card rests in todo and no normal path moves it to blocked, so
    # park it directly: the guard under test is close_task's parent check, not
    # how the card got held.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' WHERE id = ?", (child,))
    assert kb.get_task(conn, child).status == "blocked"

    r = client.post(f"{API}/tasks/{child}/close", json={"resolution": "x"})
    assert r.status_code == 409
    assert kb.get_task(conn, child).status == "blocked"


def test_close_refuses_a_remote_acceptance_contract(client):
    """A PR/issue boundary is the operator's own requirement, not ceremony."""
    conn = _conn()
    from hermes_cli.kanban_completion_evidence import set_requirements

    tid = kb.create_task(conn, title="remote contract", assignee="default", body="b")
    task = kb.get_task(conn, tid)
    set_requirements(
        conn, tid,
        {
            "version": 1, "kind": "implementation", "repository": "acme/widget",
            "criteria": ["merged"], "require_pr": True,
        },
        expected_status=task.status, expected_assignee=task.assignee,
    )
    client.patch(f"{API}/tasks/{tid}", json={"status": "blocked", "block_reason": "waiting on PR"})

    r = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "x"})
    assert r.status_code == 409
    assert "PR" in r.json()["detail"] or "issue" in r.json()["detail"]
    assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("field", ["external_conditions", "completion_conditions"])
def test_close_refuses_unmet_explicit_acceptance(client, field):
    from hermes_cli.kanban_completion_evidence import contract_record

    with _conn() as conn:
        tid = _declared_blocked(conn, client)
        contract = contract_record(conn, tid)[1]
        contract[field] = [{"id": "adrian-approval", "requirement": "Explicit Adrian approval required",
                            "resolver": "Adrian", "satisfied": False}]
        kb._append_event(conn, tid, "completion_requirements", contract)
        response = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "No approval supplied"})
        assert response.status_code == 409, response.text
        assert "Explicit Adrian approval required" in response.json()["detail"]
        assert kb.get_task(conn, tid).status == "blocked"
        assert contract_record(conn, tid)[1][field] == contract[field]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='resolved'", (tid,)).fetchone()


@pytest.mark.parametrize("field", ["external_conditions", "completion_conditions"])
@pytest.mark.parametrize("route", ["cli", "rest"])
def test_close_accepts_native_acceptance_but_not_stale_receipts(client, field, route):
    from hermes_cli import kanban as kc
    from hermes_cli.kanban_completion_evidence import contract_record
    from hermes_cli.kanban_wait_migration import manual_condition
    from hermes_cli.kanban_wait_transitions import declare_wait, resolve_gate
    import argparse

    def close(tid):
        if route == "rest":
            return client.post(f"{API}/tasks/{tid}/close", json={"resolution": "Explicit operator closure"}).status_code == 200
        parser = argparse.ArgumentParser()
        kc.build_parser(parser.add_subparsers(dest="command"))
        return kc.kanban_command(parser.parse_args(["kanban", "close", tid, "--resolution", "Explicit operator closure"])) == 0

    with _conn() as conn:
        tid = _declared_blocked(conn, client)
        contract = contract_record(conn, tid)[1]
        condition = {"condition_id": "adrian-approval", "requirement": "Explicit Adrian approval required", "resolver": "Adrian"}
        contract[field] = [condition]
        kb._append_event(conn, tid, "completion_requirements", contract)
        gate = manual_condition("external:adrian-approval", condition, contract_record(conn, tid)[0], stage="WAITING")
        declare_wait(conn, tid, [gate], {"state": "todo", "selected_next_action": {
            "phase": "READY", "type": "worker_action", "action": "Finish bounded local work"}},
            provenance={"test": "native explicit acceptance"})
        assert not close(tid)
        resolve_gate(conn, tid, gate["id"], revision=gate["revision"], declaration=gate["declaration"],
                     evidence={"reference": "fixture:operator-approval", "sha256": "a" * 64,
                               "assertions": {key: True for key in gate["parameters"]["required_assertions"]}})
        assert kb.block_task(conn, tid, reason="operator closure requested", kind="needs_input")
        accepted = contract_record(conn, tid)[1]
        revised = {**accepted, field: [{**condition, "requirement": "Additional explicit Adrian approval required"}]}
        kb._append_event(conn, tid, "completion_requirements", revised)
        assert not close(tid), "old acceptance cannot satisfy changed requirements"
        assert kb.get_task(conn, tid).status == "blocked"
        kb._append_event(conn, tid, "completion_requirements", accepted)
        assert close(tid), "native acceptance needs no worker manifest"
        assert kb.get_task(conn, tid).status == "done"


def test_close_refuses_live_claim_even_on_blocked_card(client, monkeypatch):
    with _conn() as conn:
        tid = _declared_blocked(conn, client)
        monkeypatch.setattr(kb, "_claim_is_live", lambda row: True)
        response = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "No orphaned worker"})
        assert response.status_code == 409, response.text
        assert kb.get_task(conn, tid).status == "blocked"


def test_double_close_is_refused_the_second_time(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)
    assert client.post(f"{API}/tasks/{tid}/close", json={"resolution": "x"}).status_code == 200
    r = client.post(f"{API}/tasks/{tid}/close", json={"resolution": "again"})
    assert r.status_code == 409


def test_archive_still_works_on_a_blocked_card(client):
    """The archive action is untouched by the Close addition."""
    conn = _conn()
    tid = _declared_blocked(conn, client)

    r = client.patch(f"{API}/tasks/{tid}", json={"status": "archived"})
    assert r.status_code == 200, r.text
    assert kb.get_task(conn, tid).status == "archived"


def test_bulk_archive_still_works_on_a_blocked_card(client):
    conn = _conn()
    tid = _declared_blocked(conn, client)

    r = client.post(f"{API}/tasks/bulk", json={"ids": [tid], "archive": True})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is True
    assert kb.get_task(conn, tid).status == "archived"


def test_close_releases_children_into_ready(client):
    """A closed parent no longer gates its child (same contract as completion)."""
    conn = _conn()
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract

    parent = _declared_blocked(conn, client, title="parent")
    child = kb.create_task(conn, title="child", assignee="default", body="c", parents=[parent])
    ensure_scope_contract(conn, child)
    assert kb.get_task(conn, child).status == "todo"

    assert client.post(f"{API}/tasks/{parent}/close", json={"resolution": "x"}).status_code == 200
    assert kb.get_task(conn, child).status == "ready"
