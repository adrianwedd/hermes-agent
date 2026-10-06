"""The board's route receipt: a run records the route its calls actually took.

The drawer's ``Launch:`` / ``Last observed:`` lines read ``kanban_run_routes.run_routes``,
which has exactly two sources: the run's OWN receipt (written by the worker, at the response
it describes, through ``kanban_worker_observability``) or a session-linked usage row that says
which model was billed in the session and nothing about this run. Regression for t_e07ae2d6:
the MoA aggregator/adviser slots live only in memory for the life of the turn, so before the
worker wrote them the drawer rendered ``aggregator: unknown · advisers: unknown`` for every
MoA worker run — the normal state of the feature, not an exception.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class FakeMoAClient:
    """Just the two in-memory slots the writer reads (see agent/moa_loop.py)."""

    def __init__(self, aggregator=None, advisers=()):
        from agent.moa_loop import MoAChatCompletions
        self.last_aggregator_slot = aggregator
        # The real accessor, so this test also pins the shape the writer depends on rather
        # than a hand-written stand-in that could drift from it.
        self._last_reference_metrics = [
            {"provider": x["provider"], "model": x["model"]} for x in advisers
        ] if advisers else None
        self._real_accessor = MoAChatCompletions.last_reference_slots

    @property
    def last_reference_slots(self):
        return self._real_accessor.fget(self)


class FakeAgent:
    def __init__(self, session_id, provider="moa", model="cloud"):
        self.session_id = session_id
        self.provider = provider
        self.model = model


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated board with the current process as the live worker on one running run."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    db_path = str(kb.kanban_db_path())
    monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
    from hermes_cli import kanban_worker_observability as obs
    obs._WRITTEN.clear()
    obs._LAST_WRITE.clear()
    yield conn, db_path
    conn.close()


def _running_run(conn, monkeypatch, *, pid=None, claim="studio.test:1", session_id="20261006_000000_aaaa"):
    """One claimed, running card whose run belongs to *pid* (default: this process)."""
    tid = kb.create_task(
        conn, title="worker card", assignee="implementer",
        workspace_kind="dir", workspace_path=str(Path(os.environ["HERMES_HOME"]) / "ws"),
    )
    now = int(time.time())
    pid = os.getpid() if pid is None else pid
    with kbc.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, "
            "started_at, metadata) VALUES (?, 'implementer', 'running', ?, ?, ?, ?, NULL)",
            (tid, claim, now + 3600, pid, now),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, worker_pid=?, "
            "current_run_id=? WHERE id=?",
            (claim, now + 3600, pid, run_id, tid),
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim)
    monkeypatch.setenv("HERMES_SESSION_ID", session_id)
    return tid, run_id


def _task(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def _metadata(conn, run_id):
    return json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] or "{}")


def _run_routes(conn, tid, read_session=None):
    from hermes_cli.kanban_run_routes import run_routes
    return run_routes(conn, _task(conn, tid), read_session or (lambda profile, sid: None))


def test_worker_records_the_real_aggregator_and_adviser_slots(board, monkeypatch):
    """The receipt names the slots the fan-out actually resolved, not the preset's config."""
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch)
    from hermes_cli.kanban_worker_observability import record_worker_route

    assert record_worker_route(
        FakeAgent("20261006_000000_aaaa"),
        FakeMoAClient(
            aggregator={"provider": "openai-codex", "model": "gpt-6.1-sol"},
            advisers=[{"provider": "xai", "model": "grok-4.6"}, {"provider": "ollama-cloud", "model": "actor"}],
        ),
    ) is True

    metadata = _metadata(conn, run_id)
    assert metadata["launch_route"] == {"provider": "moa", "model": "cloud"}
    assert metadata["worker_session_id"] == "20261006_000000_aaaa"
    assert metadata["runtime_route"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert metadata["runtime_route"]["advisers"] == [
        {"provider": "xai", "model": "grok-4.6"},
        {"provider": "ollama-cloud", "model": "actor"},
    ]
    assert isinstance(metadata["runtime_route"]["observed_at"], float)

    routes = _run_routes(conn, tid)
    selected = routes["selected"]
    assert selected["run_id"] == run_id
    assert selected["basis"] == "Worker-recorded runtime route"
    assert selected["launch"] == {"provider": "moa", "model": "cloud", "aggregator": None, "advisers": [], "observed_at": None}
    assert selected["last_observed"]["provider"] == "moa"
    assert selected["last_observed"]["model"] == "cloud"
    assert selected["last_observed"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert [f"{a['provider']} / {a['model']}" for a in selected["last_observed"]["advisers"]] == [
        "xai / grok-4.6", "ollama-cloud / actor",
    ]


def test_route_receipt_reads_back_after_the_terminal_metadata_replacement(board, monkeypatch):
    """``_end_run`` overwrites run metadata with the worker's handoff, so the receipt is
    also an immutable event — the drawer must still find the route on a finished card."""
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch)
    from hermes_cli.kanban_worker_observability import record_worker_route

    record_worker_route(FakeAgent("20261006_000000_aaaa"),
                        FakeMoAClient(aggregator={"provider": "openai-codex", "model": "gpt-6.1-sol"}))
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     (json.dumps({"worker_session_id": "20261006_000000_aaaa"}), run_id))

    assert "runtime_route" not in _metadata(conn, run_id)
    selected = _run_routes(conn, tid)["selected"]
    assert selected["last_observed"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert selected["basis"] == "Worker-recorded runtime route"


def test_second_distinct_route_is_appended_as_its_own_event(board, monkeypatch):
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch)
    from hermes_cli.kanban_worker_observability import record_worker_route

    record_worker_route(FakeAgent("20261006_000000_aaaa"), FakeMoAClient())
    record_worker_route(FakeAgent("20261006_000000_aaaa"),
                        FakeMoAClient(aggregator={"provider": "openai-codex", "model": "gpt-6.1-sol"}))
    # Re-recording the identical route is a no-op, not a new event on every API call.
    record_worker_route(FakeAgent("20261006_000000_aaaa"),
                        FakeMoAClient(aggregator={"provider": "openai-codex", "model": "gpt-6.1-sol"}))

    kinds = [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND run_id=? ORDER BY id", (tid, run_id))]
    assert kinds.count("worker_session_registered") == 1
    assert kinds.count("worker_route_recorded") == 1


def test_the_live_claim_fence_refuses_a_run_this_process_does_not_own(board, monkeypatch):
    """A stale worker must not stamp its route on the run that replaced it."""
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch, pid=os.getpid() + 1)
    from hermes_cli.kanban_worker_observability import record_worker_route

    before = _metadata(conn, run_id)
    assert record_worker_route(FakeAgent("20261006_000000_aaaa"), FakeMoAClient()) is False
    assert _metadata(conn, run_id) == before
    assert _run_routes(conn, tid)["selected"]["last_observed"] is None


def test_unobserved_sub_slots_stay_absent_and_the_session_fallback_invents_nothing(board, monkeypatch):
    """A MoA route with no recorded slots is 'no route recorded', not a guessed roster:
    the session usage row carries the billed model only."""
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch, session_id="20261006_000000_bbbb")
    from hermes_cli.kanban_worker_observability import record_worker_route

    record_worker_route(FakeAgent("20261006_000000_bbbb"), FakeMoAClient())

    def read_session(profile, sid):
        assert sid == "20261006_000000_bbbb"
        return {"observed_routes": [
            {"model": "cloud", "billing_provider": "moa", "task": "", "api_call_count": 50,
             "first_seen": 1.0, "last_seen": 2.0},
        ]}

    observed = _run_routes(conn, tid, read_session)["selected"]["last_observed"]
    assert observed["provider"] == "moa" and observed["model"] == "cloud"
    assert observed["aggregator"] is None
    assert observed["advisers"] == []


def test_no_receipt_and_no_session_link_renders_no_route_at_all(board, monkeypatch):
    conn, _db = board
    tid, run_id = _running_run(conn, monkeypatch)
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (run_id,))
    selected = _run_routes(conn, tid)["selected"]
    assert selected["launch"] is None
    assert selected["last_observed"] is None
    assert selected["basis"] == "No exact linked route receipt; historical model unknown"


def test_route_receipt_rejects_half_a_slot(board):
    """Neither a provider without a model nor a non-numeric timestamp is a route."""
    from hermes_cli.kanban_worker_observability import route_receipt

    assert route_receipt(provider="moa", model=None) is None
    assert route_receipt(provider=None, model="cloud") is None
    receipt = route_receipt(provider="moa", model="cloud", aggregator={"provider": "xai"},
                            advisers=[{"model": "grok-4.6"}, "junk"], observed_at="soon")
    assert receipt["aggregator"] is None
    assert receipt["advisers"] == []
    assert receipt["observed_at"] is None
