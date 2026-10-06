"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home

@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)

# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------

def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == (kb.VALID_STATUSES - {"archived"}) | {"execution"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0

# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------

def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]

def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )

def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])

def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1

# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------

def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1

# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------

def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kbc.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kbc.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )

def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done", "result": "done", "summary": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"

def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id, result="done")
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done", "result": "done", "summary": "done"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"

def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id, result="done")
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id, result="done")
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done", "result": "done", "summary": "done"},
    )
    assert response.status_code == 200, response.text
    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"

def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kbc.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None

# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404

# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------

def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"

# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------

def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)

# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------

def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server_chat._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server_chat with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", stub)
    monkeypatch.setattr(hermes_cli, "web_server_chat", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded

    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.

# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert all(item["ok"] for item in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {task["id"] for task in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)

def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kbc.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"

def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kbc.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()

def _gated_child(client):
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "parent"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks", json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    return parent["id"], child["id"]

def test_patch_done_or_review_refused_by_open_parent_names_it(client):
    """A completion refused by the dependency gate must say which parent is open,
    not the generic 'not valid from current state'."""
    parent_id, child_id = _gated_child(client)
    for status in ("done", "review"):
        r = client.patch(f"/api/plugins/kanban/tasks/{child_id}", json={"status": status})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert f"{parent_id} (ready)" in detail, detail
        assert "unsatisfied parent" in detail, detail

def test_bulk_done_refused_by_open_parent_names_it(client):
    parent_id, child_id = _gated_child(client)
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [child_id], "status": "done"})
    assert r.status_code == 200
    entry = r.json()["results"][0]
    assert entry["ok"] is False
    assert f"{parent_id} (ready)" in entry["error"], entry
    assert "unsatisfied parent" in entry["error"], entry

def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"

def test_dashboard_confirm_dispatches_expected_patch_body(client):
    """Behavioral: the PATCH body shape the bundle produces on confirm
    (status + result + summary) must be accepted by the backend without
    rejection. The backend stores ``result`` as the human-readable
    completion summary (the bundle comments confirm ``summary`` is sent
    duplicatively so the backend can store the value under its preferred
    key while the wire format remains explicit).
    This is the contract the bundle's performMoveTask relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Bundle's performMoveTask on confirm with a summary produces:
    #   { status, result: summary, summary: summary }
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped", "summary": "shipped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "done"
    assert body.get("result") == "shipped"

def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "archive": True})
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids

def test_bulk_reassign(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "old"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks",
                    json={"title": "b", "assignee": "old"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "assignee": "new"})
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"

def test_bulk_unassign_via_empty_string(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "x"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"]], "assignee": ""})
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None

def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7

def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400

# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------

def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False

# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------

def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id

# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------

_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}

# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.

@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")

def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False

# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------

def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kbc.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kbc.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()

def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kbc.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kbc.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()

# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------

def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]

# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------

def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call

def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")

# ---------------------------------------------------------------------------
# Aux-LLM endpoints under multiplexed hosting — profile secret scope (#123372)
# ---------------------------------------------------------------------------

def test_specify_resolves_each_profiles_key_under_multiplex(kanban_home, tmp_path, monkeypatch):
    """Specify / Decompose / Estimate reach the aux client with no agent turn, so under
    multi-profile hosting an unscoped provider-key read fails closed (``LLM error:
    UnscopedSecretError``). The plugin router is mounted the way ``_mount_plugin_api_routes``
    mounts every plugin router — behind ``_plugin_route_secret_scope`` — so the launch profile
    (A) and a ``?profile=`` request (B) each resolve their OWN key, and B never leaks into A."""
    import agent.secret_scope as ss
    from fastapi import Depends
    from hermes_cli import profiles
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    from tui_gateway import launch_profile_policy
    from unittest.mock import MagicMock

    (kanban_home / ".env").write_text("KANBAN_AUX_SCOPE_TEST_KEY=key-of-launch-a\n")
    profiles_root = tmp_path / "profiles"
    (profiles_root / "workerb").mkdir(parents=True)
    (profiles_root / "workerb" / ".env").write_text("KANBAN_AUX_SCOPE_TEST_KEY=key-of-worker-b\n")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: kanban_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)

    seen: list = []

    def fake_call_llm(**kwargs):
        seen.append(ss.get_secret("KANBAN_AUX_SCOPE_TEST_KEY"))
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps({"title": "Polished", "body": "**Goal**\nDo it."})
        return resp

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban",
                       dependencies=[Depends(_plugin_route_secret_scope)])
    client = TestClient(app)

    def _specify(profile=None):
        params = {"profile": profile} if profile else None
        task = client.post("/api/plugins/kanban/tasks", params=params,
                           json={"title": "one-liner", "triage": True}).json()["task"]
        return client.post(f"/api/plugins/kanban/tasks/{task['id']}/specify", params=params,
                           json={"author": "ui-tester"}).json()

    was_active, snapshot = ss.is_multiplex_active(), launch_profile_policy._snapshot
    ss.set_multiplex_active(True)
    try:
        for profile in (None, "workerb", None):
            body = _specify(profile)
            assert body["ok"] is True, body
    finally:
        ss.set_multiplex_active(was_active)
        launch_profile_policy._snapshot = snapshot
    assert seen == ["key-of-launch-a", "key-of-worker-b", "key-of-launch-a"]


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Touch drag-vs-tap threshold (#115568)
# ---------------------------------------------------------------------------

def test_touch_card_tap_opens_instead_of_dragging():
    """attachTouchDrag() must not claim a stationary tap: without a movement threshold,
    every touch pointerdown called preventDefault() immediately, which suppresses the
    synthesized click TaskCard.handleClick relies on to call props.onOpen() (#115568).
    The bundle has no build step, so this runs the real function (extracted verbatim, not
    regex-matched) through a real pointerdown/move/up sequence with a minimal DOM stub —
    behavioral, not a source-text pin.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_touch_drag_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout


# Run clock: current run start, not first-ever start
# ---------------------------------------------------------------------------


def test_board_card_exposes_current_run_start(client):
    """#99819: after a review timeout + retry, the card must expose the fresh
    run's start (not the task's first-ever start) so the run clock ticks from
    the current attempt."""
    now = int(time.time())
    first_start = now - 7200  # task first started 2h ago
    retry_start = now - 90  # retry run started 90s ago
    conn = kbc.connect()
    try:
        t = kb.create_task(conn, title="retried", assignee="x")
        lock = "lock-runclock"
        future = now + 3600
        conn.execute(
            "UPDATE tasks SET status='running', started_at=?, claim_lock=?, "
            "claim_expires=?, worker_pid=? WHERE id=?",
            (first_start, lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, retry_start),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        # A sibling task with no run at all: key present, null.
        u = kb.create_task(conn, title="unclaimed", assignee="x")
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    columns = {c["name"]: c for c in r.json()["columns"]}
    card = next(c for c in columns["running"]["tasks"] if c["id"] == t)
    assert card["started_at"] == first_start
    # Red on base: this key did not exist at all.
    assert card["current_run_started_at"] == retry_start
    todo = next(c for c in columns["ready"]["tasks"] if c["id"] == u)
    assert todo["current_run_started_at"] is None

    # The detail endpoint carries the same contract.
    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["current_run_started_at"] == retry_start


# ---------------------------------------------------------------------------
# Priority editing after creation: native persistence, server readback, order
# ---------------------------------------------------------------------------

def _seed_ready(title: str) -> str:
    """Create an unclaimed ready card on the isolated board (the state the
    dispatcher's ready lane claims from, so the order tests exercise the real
    query rather than an ad-hoc SELECT)."""
    with kbc.connect() as conn:
        return kb.create_task(conn, title=title, assignee="implementer")


def _card(client, column: str, task_id: str, *, include_archived: bool = True) -> dict:
    """The card as the board actually serves it (same payload the columns render)."""
    suffix = "?include_archived=true" if include_archived else ""
    r = client.get(f"/api/plugins/kanban/board{suffix}")
    assert r.status_code == 200, r.text
    columns = {c["name"]: c for c in r.json()["columns"]}
    return next(c for c in columns[column]["tasks"] if c["id"] == task_id)


def test_priority_patch_persists_and_reads_back(client, kanban_home):
    """The served editor's write path: PATCH /tasks/{id} {priority} must persist the
    value the operator typed and hand it back on the next read, so the UI can close
    on SERVER truth rather than on its own optimistic guess."""
    t = _seed_ready("priority-readback")

    r = client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 7})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["priority"] == 7

    # Readback through the same endpoint the drawer refetches from.
    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["priority"] == 7

    # And it survives on the board payload the columns render from.
    columns = {c["name"]: c for c in client.get("/api/plugins/kanban/board").json()["columns"]}
    card = next(c for c in columns["ready"]["tasks"] if c["id"] == t)
    assert card["priority"] == 7


def test_priority_patch_records_a_native_reprioritized_event(client, kanban_home):
    """A priority change is lifecycle, not presentation: the board's own event log
    must show it, so the drawer's activity feed and any later audit agree."""
    t = _seed_ready("priority-event")

    assert client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 3}).status_code == 200

    events = client.get(f"/api/plugins/kanban/tasks/{t}").json()["events"]
    reprioritized = [e for e in events if e["kind"] == "reprioritized"]
    assert reprioritized, [e["kind"] for e in events]
    payload = reprioritized[-1]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload.get("priority") == 3


def test_priority_can_be_lowered_and_zeroed_after_creation(client, kanban_home):
    """0 is the default and a lower integer is a legitimate hold-back — the editor
    must be able to express both, not just raises."""
    t = _seed_ready("priority-lower")
    assert client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 5}).status_code == 200
    assert client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 0}).status_code == 200

    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["priority"] == 0


def test_higher_priority_is_claimed_first_in_dispatch_order(client, kanban_home):
    """The reason priority editing exists: among otherwise eligible cards, a higher
    integer is claimed first. `list_tasks` and both dispatcher lanes share the
    `priority DESC, created_at ASC` key, so the board order must show it."""
    low = _seed_ready("priority-order-low")
    high = _seed_ready("priority-order-high")
    assert client.patch(f"/api/plugins/kanban/tasks/{low}", json={"priority": 1}).status_code == 200
    assert client.patch(f"/api/plugins/kanban/tasks/{high}", json={"priority": 9}).status_code == 200

    with kbc.connect() as conn:
        ids = [t.id for t in kb.list_tasks(conn, status="ready")]

    assert ids.index(high) < ids.index(low)

    # The dispatcher's own lane query is the same ordering, so it agrees.
    with kbc.connect() as conn:
        lane = [r["id"] for r in kbd._lane_rows(conn, "ready")]

    assert lane.index(high) < lane.index(low)


def test_priority_edit_survives_a_status_change(client, kanban_home):
    """Reprioritised while ready, then blocked and unblocked: the scheduling input
    must not be reset by lifecycle churn (the event log is append-only and the
    column is the operator's)."""
    t = _seed_ready("priority-persist-lifecycle")
    assert client.patch(f"/api/plugins/kanban/tasks/{t}", json={"priority": 8}).status_code == 200
    assert client.patch(
        f"/api/plugins/kanban/tasks/{t}", json={"status": "blocked", "block_reason": "waiting"}
    ).status_code == 200

    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["priority"] == 8


def test_priority_editor_refuses_a_fractional_draft_in_the_shipped_bundle():
    """The browser/mobile editor is the shipped IIFE (no build step), so this drives
    its REAL parser. The behaviour under test is the refusal: the old
    `Number(v) || 0` turned a blank or fractional draft into a silent 0 and thereby
    demoted the card."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_priority_parse_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout


def test_priority_editor_write_path_settles_on_rejection_and_awaits_readback():
    """The shipped editor must not close as though it saved: the write helper has
    to rethrow a refused PATCH/GET (so the form keeps the draft and shows the
    error) and must settle the value from the SERVER readback, not its own guess."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_priority_write_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout


def test_browser_surface_files_and_labels_cards_by_the_derived_stage():
    """The desktop is not the only served surface: the browser/mobile kanban is the
    shipped IIFE (no build step), so this drives its REAL lane + chip functions. A
    `ready` card under an administrative hold is refused by the dispatcher on both
    lanes, so the bundle must file it under Blocked and label it with the fact's own
    stage — not the raw status, and not an invented state."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_admission_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout


# ---------------------------------------------------------------------------
# Terminal cards are history: no queue warning, no stale progress
# ---------------------------------------------------------------------------

def _complete(client, title: str, *, result: str = "shipped"):
    """Drive a card to `done` the way the board's own history reaches it.

    Terminal state is arranged directly (the idiom this file already uses for
    setup, e.g. the running-card fixture): the completion-evidence gate that a
    PATCH must satisfy is a different subsystem's contract, while the subject
    here is how the DASHBOARD renders a card that is already terminal.
    """
    t = _seed_ready(title)
    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?",
            (result, int(time.time()), t),
        )
        conn.commit()
    return t


def test_completed_card_reports_completion_not_a_queue_warning(client, kanban_home):
    """RED on base: a done card was labelled `Not queued` — a dispatch verdict on
    history — and the desktop renderer painted it red purely because
    `dispatch_eligible` is false on terminal cards."""
    t = _complete(client, "terminal-copy")

    card = _card(client, "done", t)
    fact = card["card_facts"]["dispatch"]

    assert fact["terminal"] is True
    assert fact["label"] not in ("Not queued", "Unresolved dispatch restriction"), fact
    assert "queued" not in fact["reason"].lower(), fact
    assert "resolution" in fact["reason"].lower(), fact
    assert fact["next_action"] is None
    assert fact["owner"] is None
    assert fact["completed_at"] is not None


def test_archived_card_reports_history_not_a_warning(client, kanban_home):
    t = _complete(client, "terminal-archived")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (t,))
        conn.commit()

    card = _card(client, "archived", t)
    fact = card["card_facts"]["dispatch"]

    assert fact["terminal"] is True
    assert fact["label"] == "Archived"
    assert "queued" not in fact["reason"].lower(), fact


def test_nonterminal_hold_still_reports_its_own_reason(client, kanban_home):
    """A card the dispatcher refuses must name its hold, and must NOT be filed under
    a lane whose tile contradicts it. A `ready` card with dispatch_eligible=0 and no
    recorded reason is a HELD card: it reads as a fault and leaves the Ready lane."""
    t = _seed_ready("nonterminal-hold")

    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (t,))
        conn.commit()

    fact = _card(client, "blocked", t)["card_facts"]["dispatch"]

    assert fact["terminal"] is False
    assert fact["stage"] == "HELD"
    assert fact["label"], fact
    assert fact["reason"], fact
    # Not a queue verdict: the card is not shown as dispatchable anywhere.
    assert fact["dispatchable"] is False
    assert fact["column"] == "blocked"
    ready_ids = {
        c["id"] for c in client.get("/api/plugins/kanban/board").json()["columns"]
        if c["name"] == "ready"
        for c in c["tasks"]
    }
    assert t not in ready_ids


def test_completed_card_shows_no_stale_child_progress(client, kanban_home):
    """RED on base: the `done` column still carried `progress` ('0/1 done'), which
    the UI rendered as a pill under a finished card."""
    parent = _seed_ready("progress-parent")
    child = _seed_ready("progress-child")
    with kbc.connect() as conn:
        assert kb.link_tasks(conn, parent, child)

    # While in flight the rollup is real and must be shown.
    assert _card(client, "ready", parent)["progress"] == {"done": 0, "total": 1}

    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?",
            ("closed", int(time.time()), parent),
        )
        conn.commit()

    card = _card(client, "done", parent)
    assert card["progress"] is None


def test_completed_card_shows_no_stale_run_progress_either(client, kanban_home):
    """RED on base: `card_facts.progress` is a DIFFERENT channel from the child
    rollup — the latest retained run summary for any card with no current run —
    and the tile renders it as 'Progress: <text> · <age>'. Done cards kept showing
    their last handoff ("Reviewed and approved …") as if work were in flight."""
    t = _seed_ready("terminal-run-progress")
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary) "
            "VALUES (?, 'done', ?, ?, 'completed', ?)",
            (t, int(time.time()) - 600, int(time.time()) - 300,
             "Reviewed and approved the corrected reconciliation"),
        )
        conn.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?",
            ("shipped", int(time.time()), t),
        )
        conn.commit()

    card = _card(client, "done", t)
    assert card["card_facts"]["progress"] is None, card["card_facts"]["progress"]

    # The same withholding must hold on the detail endpoint the drawer reads.
    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["card_facts"]["progress"] is None, detail["card_facts"]["progress"]


def test_nonterminal_card_keeps_its_run_progress(client, kanban_home):
    """The terminal carve-out must not silence genuine progress on live work."""
    t = _seed_ready("live-run-progress")
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary) "
            "VALUES (?, 'done', ?, ?, 'completed', ?)",
            (t, int(time.time()) - 600, int(time.time()) - 300, "halfway through the extraction"),
        )
        conn.commit()

    card = _card(client, "ready", t)
    assert card["card_facts"]["progress"]["text"] == "halfway through the extraction"


# ---------------------------------------------------------------------------
# Blocked cards name the action, the blocker, the resolver and the resume
# ---------------------------------------------------------------------------

def test_blocked_card_names_its_structured_blocker(client, kanban_home):
    """A blocked card must be actionable: the affected action, who resolves it and
    what resumes it. A generic 'resolve the recorded blocker' is not a report."""
    t = _seed_ready("blocked-detail")
    from hermes_cli import kanban_db as _kb

    with kbc.connect() as conn:
        assert _kb.block_task(conn, t, kind="needs_input", reason="operator must grant live A/B permission")

    card = _card(client, "blocked", t)
    fact = card["card_facts"]["dispatch"]

    assert fact["block_kind"] == "needs_input"
    assert "needs_input" in fact["label"]
    assert "live A/B permission" in fact["reason"]
    assert fact["owner"] == "operator"          # who resolves it
    assert fact["next_action"]                    # what resumes it
    assert "Resolve the native recorded blocker" not in fact["reason"]


def test_blocked_card_without_a_payload_says_so_instead_of_inventing_one(client, kanban_home):
    """An unclassified block is reported as incomplete evidence, never dressed up
    as a named prerequisite."""
    t = _seed_ready("blocked-vague")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.commit()

    fact = _card(client, "blocked", t)["card_facts"]["dispatch"]

    assert "incomplete" in fact["reason"].lower() or "no structured" in fact["reason"].lower(), fact


# ---------------------------------------------------------------------------
# The derived stage: dispatch state must agree with the dispatcher's next action
# ---------------------------------------------------------------------------
#
# The operator's corrected acceptance: never optimise for "zero cards with
# dispatch_eligible=false" (that metric is invalid — the flag is false on finished
# cards, under operator decisions and while a review is open). The invariant is that
# a card's dispatch state never disagrees with the action the dispatcher would take.

def _column_ids(client, name: str) -> set[str]:
    board = client.get("/api/plugins/kanban/board").json()
    return {
        card["id"]
        for column in board["columns"]
        if column["name"] == name
        for card in column["tasks"]
    }


def _sqlite_memory_task(*, status: str, eligible: int, authority_action: dict | None = None):
    """A bare in-memory DB carrying only what `dispatch_facts` reads.

    The derivations that do not go through the HTTP board (the flag-only review hold and
    the authority boundary) are unit-level contracts on the derivation itself, so they
    exercise the real function against the real event table rather than re-implementing
    its queries in the test.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, "
        "workspace_path TEXT, idempotency_key TEXT, workspace_kind TEXT, dispatch_eligible INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
        "kind TEXT, payload TEXT, created_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks (id, assignee, status, dispatch_eligible) VALUES (?,?,?,?)",
        ("t_x", "reviewer", status, eligible),
    )
    if authority_action is not None:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            ("t_x", "completion_requirements",
             json.dumps({"version": 1, "kind": "implementation", "local_only": True,
                         "criteria": ["accepted"], "selected_next_action": authority_action}),
             int(time.time())),
        )
    conn.commit()
    return conn


def test_admin_pending_card_never_renders_in_the_ready_lane(client, kanban_home):
    """RED on base: a card carrying an `administrative_pending` event keeps native
    status `ready`, so the board bucketed it under the Ready lane while the card's own
    dispatch fact said "Stopped · receipt administration". The native dispatcher refuses
    such a card outright (kanban_db_dispatch excludes administrative_pending), so a Ready
    tile is an outright lie about the next action."""
    t = _seed_ready("admin-held")
    with kbc.connect() as conn:
        # What set_administrative_pending records: dispatch_eligible=0, status untouched.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (t, "administrative_pending",
             json.dumps({"version": 1, "scope": "receipt_administration",
                         "reason": "bind retained completion evidence and close natively"}),
             int(time.time())),
        )
        conn.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (t,))
        conn.commit()

    board = client.get("/api/plugins/kanban/board").json()
    columns = {c["name"]: c for c in board["columns"]}
    card = next(c for c in columns["blocked"]["tasks"] if c["id"] == t)
    fact = card["card_facts"]["dispatch"]

    # The visible primary state is the operator decision, with the receipt
    # administration as its reason — not a secondary badge beside a Ready tile.
    assert fact["stage"] == "OPERATOR"
    assert fact["dispatchable"] is False
    assert fact["column"] == "blocked"
    assert fact["owner"] == "operator"
    assert "receipt administration" in fact["label"].lower()
    assert "do not rerun" in fact["next_action"].lower()
    # And the lane agrees with the fact.
    assert t not in _column_ids(client, "ready")
    assert t in _column_ids(client, "blocked")
    # The detail endpoint the drawer reads carries the same decision.
    detail = client.get(f"/api/plugins/kanban/tasks/{t}").json()["task"]
    assert detail["card_facts"]["dispatch"]["stage"] == "OPERATOR"


def test_receipt_only_state_cannot_persist_past_an_explicit_release(client, kanban_home):
    """A hold is a DECISION with a recorded release, not permanent task identity.
    `administrative_pending_released` supersedes an earlier pending event without
    deleting its audit trail, and the card must be able to leave OPERATOR."""
    t = _seed_ready("admin-released")
    with kbc.connect() as conn:
        for kind in ("administrative_pending", "administrative_pending_released"):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (t, kind, json.dumps({"reason": "operator closed the receipt"}), int(time.time())),
            )
        conn.execute("UPDATE tasks SET dispatch_eligible=1 WHERE id=?", (t,))
        conn.commit()

    fact = _card(client, "ready", t)["card_facts"]["dispatch"]
    assert fact["stage"] != "OPERATOR", fact
    assert t in _column_ids(client, "ready")


def test_ready_card_requires_a_worker_executable_action(client, kanban_home):
    """READY is a claim that "assign a profile and it runs". A `ready` card that is
    still short of operator qualification is PREPARE — filed in Todo, not Ready — even
    though its native status says otherwise."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="unqualified", assignee="implementer")
        conn.execute("UPDATE tasks SET workspace_path='/tmp/not-a-real-workspace-x' WHERE id=?", (t,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (t, "completion_requirements",
             json.dumps({"version": 1, "kind": "implementation", "qualified_for_dispatch": False,
                         "criteria": ["a"]}), int(time.time())),
        )
        conn.commit()

    fact = _card(client, "todo", t)["card_facts"]["dispatch"]
    assert fact["stage"] == "PREPARE"
    assert fact["dispatchable"] is False
    assert "qualification" in fact["reason"].lower() or "preparation" in fact["reason"].lower()
    assert t not in _column_ids(client, "ready")


def test_review_card_held_only_by_the_eligibility_flag_is_not_advertised_as_reviewable(
    client, kanban_home
):
    """The claim guard's FIRST clause is `not task.dispatch_eligible`, evaluated before
    any event is consulted (`kanban_db_dispatch._dispatch_lane_task`). A `review` card
    held by that flag alone — the operator's own recorded way of refusing dispatch, and
    the shape of the 12 live `operator_state_reconciled` rows — must therefore NOT be
    offered as dispatchable review work. The assertion is made by CALLING the guard, not
    against a hand-written expectation, so the two can never drift apart."""
    t = _seed_ready("review-flag-only-hold")
    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer', dispatch_eligible=0 WHERE id=?",
            (t,),
        )
        conn.commit()

    # What the dispatcher actually does with the row this tick (dry run: no claim is
    # taken, so the delivered candidate is not mutated by the test).
    with kbc.connect() as conn:
        guarded = kbd.DispatchResult()
        took_it = kbd._dispatch_lane_task(
            conn, {"id": t, "assignee": "reviewer"}, "reviewer", guarded,
            lane="review", dry_run=True, ttl_seconds=None, board=None,
            failure_limit=5, spawn_fn=None, per_profile_cap=None, per_profile_running={},
        )
    assert took_it is False, "the guard would claim a card the board must not advertise"
    assert any(tid == t for tid, _ in guarded.respawn_guarded), guarded

    # ...and the board must not claim otherwise.
    fact = _card(client, "blocked", t)["card_facts"]["dispatch"]
    assert fact["dispatchable"] is False, fact
    assert fact["stage"] != "REVIEW", fact
    assert t not in _column_ids(client, "review")


def test_review_card_with_the_flag_granted_clears_the_flag_clause(client, kanban_home):
    """Control: the refusal above really is the flag, not the review lane. With the flag
    granted the same card clears the guard's first clause and the derivation reports it
    as review work — so the board and the dispatcher agree in BOTH directions.

    The guard may still decline the card further down (a profile that does not exist in
    this isolated home is the next clause, and that is a routing problem, not this
    contract), so the assertion here is scoped to the clause under test."""
    t = _seed_ready("review-flag-granted")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='review', assignee='reviewer' WHERE id=?", (t,))
        conn.commit()

    with kbc.connect() as conn:
        guarded = kbd.DispatchResult()
        kbd._dispatch_lane_task(
            conn, {"id": t, "assignee": "reviewer"}, "reviewer", guarded,
            lane="review", dry_run=True, ttl_seconds=None, board=None,
            failure_limit=5, spawn_fn=None, per_profile_cap=None, per_profile_running={},
        )
    assert not any(tid == t for tid, _ in guarded.respawn_guarded), guarded

    from hermes_cli.kanban_dispatch_facts import dispatch_facts
    conn = _sqlite_memory_task(status="review", eligible=1)
    fact = dispatch_facts(conn, {"id": "t_x", "status": "review",
                                 "dispatch_eligible": 1, "assignee": "reviewer"})
    assert fact["stage"] == "REVIEW"
    assert fact["dispatchable"] is True
    assert fact["column"] == "review"


def test_review_card_under_an_authority_wait_keeps_its_named_grant():
    """`authority_wait` is a real boundary with its own resolver and grant, recorded in
    the completion contract. It must not be flattened into a generic operator
    declaration: the live card whose contract says "no live measurement currently
    authorised" would lose both its action and its owner."""
    from hermes_cli.kanban_completion_workflow import authority_wait
    from hermes_cli.kanban_dispatch_facts import dispatch_facts

    action = {"phase": "WAITING_FOR_AUTHORITY", "resolver": "Adrian",
              "action": "Grant one bounded exclusive local measurement window"}
    conn = _sqlite_memory_task(status="review", eligible=0, authority_action=action)
    assert authority_wait(conn, "t_x") == action

    fact = dispatch_facts(conn, {"id": "t_x", "status": "review",
                                 "dispatch_eligible": 0, "assignee": "reviewer"})
    assert fact["dispatchable"] is False
    assert fact["owner"] == "Adrian"
    assert fact["next_action"] == action["action"]
    assert fact["phase"] == "WAITING_FOR_AUTHORITY"


def test_awaiting_acceptance_stays_a_dispatchable_review_lane(client, kanban_home):
    """REVIEW is a real lane: an unresolved review action can accept, reject or request
    changes, so an eligible review card is dispatchable when capacity exists. It is not
    an administrative holding pen."""
    t = _seed_ready("in-review")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='review', assignee='reviewer' WHERE id=?", (t,))
        conn.commit()

    card = _card(client, "review", t)
    fact = card["card_facts"]["dispatch"]

    assert fact["stage"] == "REVIEW"
    assert fact["dispatchable"] is True
    assert fact["column"] == "review"
    assert fact["next_action"]


def test_completed_review_under_an_operator_gate_is_operator_not_review(client, kanban_home):
    """A review that has ALREADY happened and is parked behind an operator contract is
    an OPERATOR decision, not review work: the claim path refuses the card on both lanes,
    so showing it as "awaiting acceptance" in the review lane misstates the next action.
    The operator decides; no ceremonial receipt is required of the producer."""
    t = _seed_ready("review-gated")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='review', assignee='reviewer', dispatch_eligible=0 WHERE id=?", (t,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (t, "operator_review_handoff",
             json.dumps({"automatic_retry": False, "dispatch_eligible": False}), int(time.time())),
        )
        conn.commit()

    fact = _card(client, "blocked", t)["card_facts"]["dispatch"]

    assert fact["stage"] == "OPERATOR"
    assert fact["dispatchable"] is False
    assert fact["owner"] == "operator"
    assert t not in _column_ids(client, "review")


def test_running_card_reports_the_owner_and_stays_in_its_lane(client, kanban_home):
    """RUNNING is a live claim, not a queue verdict: the card names whose handoff it is
    waiting on and is not re-presented as dispatchable work."""
    now = int(time.time())
    t = _seed_ready("live-owner")
    lock = "lock-derived-stage"
    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', assignee='implementer', claim_lock=?, "
            "claim_expires=?, worker_pid=? WHERE id=?",
            (lock, now + 3600, 99999, t),
        )
        conn.commit()

    card = _card(client, "running", t)
    fact = card["card_facts"]["dispatch"]

    assert fact["stage"] == "RUNNING"
    assert fact["dispatchable"] is True
    assert fact["owner"] == "implementer"
    assert fact["next_action"]


def test_dependency_wait_is_waiting_and_not_in_ready(client, kanban_home):
    """WAITING: the future condition is known (an unfinished prerequisite), so the card
    is not dispatchable and must not sit in Ready claiming it is."""
    parent = _seed_ready("wait-parent")
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="waiting-child", assignee="implementer", parents=[parent])
        conn.commit()

    card = _card(client, "scheduled", child)
    fact = card["card_facts"]["dispatch"]

    assert fact["stage"] == "WAITING"
    assert fact["dispatchable"] is False
    assert parent in fact["reason"]
    assert child not in _column_ids(client, "ready")
    # Its lane does not contradict the fact.
    assert fact["column"] in ("todo", "scheduled")


def test_terminal_card_is_done_and_never_dispatchable(client, kanban_home):
    """DONE: history, not a queue candidate. No stale warning and no dispatch verdict."""
    t = _complete(client, "terminal-derived")
    fact = _card(client, "done", t)["card_facts"]["dispatch"]

    assert fact["stage"] == "DONE"
    assert fact["dispatchable"] is False
    assert fact["terminal"] is True
    assert fact["next_action"] is None
    assert fact["owner"] is None
    assert "queued" not in fact["reason"].lower()


def test_every_stage_names_a_concrete_next_action_or_a_terminal_resolution(client, kanban_home):
    """The whole point: for EVERY card on the board, the derived stage either names the
    concrete next action the dispatcher takes or is a terminal resolution. A stage with
    neither is the bug this card exists to remove."""
    _complete(client, "stage-done")
    _seed_ready("stage-ready")
    blocked = _seed_ready("stage-blocked")
    from hermes_cli import kanban_db as _kb

    with kbc.connect() as conn:
        assert _kb.block_task(conn, blocked, kind="needs_input", reason="operator permission")

    board = client.get("/api/plugins/kanban/board").json()
    for column in board["columns"]:
        for card in column["tasks"]:
            fact = card["card_facts"]["dispatch"]
            assert fact["stage"], card["id"]
            assert isinstance(fact["dispatchable"], bool), card["id"]
            # The lane the card is rendered in is the stage's own column.
            assert fact["column"] == column["name"], (card["id"], fact["column"], column["name"])
            # Terminal is a resolution; everything else names its next action.
            assert fact["terminal"] or fact["next_action"], (card["id"], fact)


def test_accepted_review_renders_as_completed_not_an_operator_ceremony(client, kanban_home):
    """An ACCEPTED review must not leave the operator a ceremonial receipt to click. Once
    the evidence is accepted and independently approved, the card is substantive-complete:
    the control plane closes it, and the board renders the same neutral completion fact it
    renders for any finished card — no OPERATOR hold, no stale warning, no dispatch
    verdict. (The mechanical closure itself is `complete_task` + `record_gate`, covered by
    the existing review-lifecycle tests; this pins the RENDER for that state.)"""
    t = _seed_ready("accepted-review")
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (t, "operator_review_approval",
             json.dumps({"version": 1, "reason": "Independent review passed"}), int(time.time())),
        )
        # What the accepted close records, then the control-plane closure itself.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (t, "completion_evidence_accepted",
             json.dumps({"contract_event_id": 1, "kind": "implementation", "criteria": ["a"]}),
             int(time.time())),
        )
        conn.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?",
            ("accepted and closed", int(time.time()), t),
        )
        conn.commit()

    fact = _card(client, "done", t)["card_facts"]["dispatch"]

    assert fact["stage"] == "DONE"
    assert fact["dispatchable"] is False
    assert fact["owner"] is None
    assert fact["next_action"] is None
    # No ceremony survives an accepted closure.
    assert "operator" not in json.dumps(fact).lower()
