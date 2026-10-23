"""Regressions for domain-layer descendant invalidation on ancestor reopen.

``kanban_db.invalidate_descendants_for_parent_reopen`` is the single
implementation of "a done ancestor was reopened, retract everything that
assumed its result" (M3). These tests pin:

* done descendants are demoted to ``todo`` with a ``descendant_invalidated``
  event AND a comment naming the ancestor (non-silent),
* running descendants have their audit trail committed BEFORE their worker
  is terminated, and the kill routes through ``_terminate_reclaimed_worker``
  (the same helper the reclaim paths use),
* ``consecutive_failures`` resets to 0 (deliberate operator action —
  opposite of the review-loop rule pinned in M2), and
* the dashboard refuses generic terminal reopening; only an explicitly
  authorised domain transition may invoke the invalidation function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _done_parent_with_done_child(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="done")
    child_id = kb.create_task(
        conn, title="child", assignee="builder", parents=[parent_id],
    )
    assert kb.complete_task(conn, child_id, result="done")
    return parent_id, child_id


def _reopen_parent_directly(conn, parent_id: str) -> None:
    """Minimal stand-in for a reopen surface: flip done -> todo."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
            (parent_id,),
        )


def test_reopen_demotes_done_descendants_with_events_and_comments(conn):
    parent_id, child_id = _done_parent_with_done_child(conn)
    grandchild_id = kb.create_task(
        conn, title="grandchild", assignee="writer", parents=[child_id],
    )
    assert kb.complete_task(conn, grandchild_id, result="done")

    _reopen_parent_directly(conn, parent_id)
    result = kb.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    demoted = {entry["id"]: entry for entry in result["invalidated"]}
    assert set(demoted) == {child_id, grandchild_id}
    for tid in (child_id, grandchild_id):
        assert demoted[tid]["prior_status"] == "done"
        assert demoted[tid]["new_status"] == "todo"
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "todo"
        assert task.completed_at is None

        events = kb.list_events(conn, tid)
        inval = [e for e in events if e.kind == "descendant_invalidated"]
        assert len(inval) == 1
        payload = inval[0].payload
        assert payload["ancestor"] == parent_id
        assert payload["prior_status"] == "done"
        assert payload["new_status"] == "todo"

        comments = kb.list_comments(conn, tid)
        assert any(
            parent_id in c.body and c.author == "operator" for c in comments
        ), f"no invalidation comment naming {parent_id} on {tid}"

    assert result["terminations"] == []


def test_running_descendant_is_held_then_released_once_its_worker_dies(
    conn, tmp_path, monkeypatch,
):
    """A running descendant must never be released before its worker is gone.

    The invalidation records the hold in full (audit before death) and leaves the
    claim, pid and run EXACTLY as found, so no claim can be granted beside a survivor.
    Only the post-commit finalize kills the worker and, once it is confirmed dead,
    retracts the card to ``todo`` with a ``descendant_reclaimed`` event.
    """
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="done")
    child_id = kb.create_task(
        conn, title="running child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None and claimed.status == "running"
    kbd._set_worker_pid(conn, child_id, 424242)
    row = conn.execute(
        "SELECT claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?", (child_id,)
    ).fetchone()
    lock, pid, fp = row["claim_lock"], row["worker_pid"], row["worker_started_at"]

    kills: list[tuple] = []

    def dead_terminate(pid_arg, claim_lock, started_at=None, **kwargs):
        # The audit trail must already be durable when the kill fires.
        side = kbc.connect(tmp_path / "kanban.db")
        try:
            kinds = [e.kind for e in kb.list_events(side, child_id)]
        finally:
            side.close()
        assert "descendant_invalidated" in kinds
        kills.append((pid_arg, claim_lock, started_at))
        return {"prev_pid": pid_arg, "host_local": True, "termination_attempted": True,
                "terminated": True, "sigkill": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", dead_terminate)

    # Composed (caller-owned txn) so the drain is deferred to us, exactly as a terminal
    # reopen composes it: nothing may be killed or released inside the invalidation write.
    with kb.write_txn(conn):
        result = kb.invalidate_descendants_for_parent_reopen(
            conn, parent_id, author="operator",
        )

    # Held: the worker was NOT killed inside the invalidation write, and the ownership
    # the row carried is reported verbatim so the finalize can CAS on it.
    assert kills == []
    assert result["held"] and result["held"][0]["id"] == child_id
    held = result["held"][0]
    assert held["claim_lock"] == lock and held["worker_pid"] == pid
    assert held["worker_started_at"] == fp
    child = kb.get_task(conn, child_id)
    assert child is not None and child.status == "running"
    assert child.claim_lock == lock and child.worker_pid == pid
    assert kb.claim_task(conn, child_id, claimer="duplicate-probe") is None  # no duplicate

    out = kb.finalize_descendant_invalidation(conn, parent_id, result)
    assert kills and kills[0][0] == pid
    assert [item["id"] for item in out["released"]] == [child_id]
    assert out["deferred"] == []
    child = kb.get_task(conn, child_id)
    assert child is not None
    assert child.status == "todo"
    assert child.current_run_id is None
    assert child.claim_lock is None and child.worker_pid is None
    kinds = [e.kind for e in kb.list_events(conn, child_id)]
    assert "descendant_reclaimed" in kinds
    run = kb.latest_run(conn, child_id)
    assert run is not None and run.outcome == "reclaimed"


def test_surviving_descendant_worker_keeps_its_claim_and_no_duplicate(
    conn, tmp_path, monkeypatch,
):
    """A descendant worker that survives termination keeps a HELD claim (no duplicate)."""
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="done")
    child_id = kb.create_task(
        conn, title="stubborn child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None
    kbd._set_worker_pid(conn, child_id, 424242)
    lock = claimed.claim_lock

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: {
        "prev_pid": 424242, "host_local": True, "termination_attempted": True,
        "terminated": False, "sigkill": False})
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda t: True)

    _reopen_parent_directly(conn, parent_id)
    result = kb.invalidate_descendants_for_parent_reopen(conn, parent_id, author="operator")
    out = kb.finalize_descendant_invalidation(conn, parent_id, result)

    assert [item["id"] for item in out["deferred"]] == [child_id]
    assert out["released"] == []
    child = kb.get_task(conn, child_id)
    assert child is not None and child.status == "running"
    assert child.claim_lock == lock  # held, never cleared
    assert kb.claim_task(conn, child_id, claimer="duplicate-probe") is None
    deferred = [e for e in kb.list_events(conn, child_id) if e.kind == "reclaim_deferred"]
    assert deferred and (deferred[-1].payload or {}).get("reason") == "ancestor_reopened_worker_alive"


def test_finalize_refuses_to_clear_a_claim_that_changed_under_it(
    conn, tmp_path, monkeypatch,
):
    """Snapshot CAS: a concurrent re-claim between hold and finalize is left untouched."""
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="done")
    child_id = kb.create_task(
        conn, title="running child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None
    kbd._set_worker_pid(conn, child_id, 424242)

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: {
        "prev_pid": 424242, "host_local": True, "termination_attempted": True,
        "terminated": True, "sigkill": False})

    _reopen_parent_directly(conn, parent_id)
    with kb.write_txn(conn):  # composed: the drain is ours to perform
        result = kb.invalidate_descendants_for_parent_reopen(conn, parent_id, author="operator")

    # A concurrent writer re-claims the card with a new owner before the finalize runs.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_lock = 'someone-else' WHERE id = ?", (child_id,))

    out = kb.finalize_descendant_invalidation(conn, parent_id, result)

    assert out["released"] == [] and out["deferred"] == []
    child = kb.get_task(conn, child_id)
    assert child is not None and child.status == "running"
    assert child.claim_lock == "someone-else"  # a claim we did not hold is never cleared
    assert kb.claim_task(conn, child_id, claimer="duplicate-probe") is None


def test_counter_reset_on_invalidated_descendants(conn):
    parent_id, child_id = _done_parent_with_done_child(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 4 WHERE id = ?",
            (child_id,),
        )

    _reopen_parent_directly(conn, parent_id)
    kb.invalidate_descendants_for_parent_reopen(conn, parent_id, author="op")

    child = kb.get_task(conn, child_id)
    assert child is not None
    # Deliberate operator action = fresh start with the breaker; contrast
    # with reopen_review_task, which PRESERVES the counter (M2 rule).
    assert child.consecutive_failures == 0


def test_dashboard_generic_status_cannot_reopen_accepted_parent(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import importlib.util
    import sys

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_m3_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    def build_graph(tag: str):
        with kbc.connect() as c:
            parent = kb.create_task(c, title=f"{tag}-parent", assignee="planner")
            assert kb.complete_task(c, parent, result="done")
            child = kb.create_task(
                c, title=f"{tag}-child", assignee="builder", parents=[parent],
            )
            assert kb.complete_task(c, child, result="done")
        return parent, child

    dash_parent, dash_child = build_graph("dash")

    # A dashboard status edit cannot revoke acceptance or invalidate children.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{dash_parent}", json={"status": "todo"},
    )
    assert r.status_code == 409, r.text

    with kbc.connect() as c:
        assert kb.get_task(c, dash_parent).status == "done"
        assert kb.get_task(c, dash_child).status == "done"
        assert not any(e.kind == "descendant_invalidated"
                       for e in kb.list_events(c, dash_child))
