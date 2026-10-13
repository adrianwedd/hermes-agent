"""A worker session links only to its own still-live board claim."""

import json
import os
import sqlite3
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

from hermes_cli import kanban_worker_observability as observability
from tools import kanban_tools


def test_startup_receipt_is_exact_and_idempotent(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "board.db")
    conn.executescript(
        "CREATE TABLE tasks (id TEXT, current_run_id INTEGER, status TEXT, worker_pid INTEGER, claim_lock TEXT);"
        "CREATE TABLE task_runs (id INTEGER, task_id TEXT, metadata TEXT, ended_at INTEGER, "
        "worker_pid INTEGER, claim_lock TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, "
        "kind TEXT, payload TEXT);"
    )
    conn.execute("INSERT INTO tasks VALUES ('task-a', 7, 'running', ?, 'claim-a')", (os.getpid(),))
    conn.execute("INSERT INTO task_runs VALUES (7, 'task-a', '{}', NULL, ?, 'claim-a')", (os.getpid(),))
    conn.commit()

    class Board:
        write_txn = staticmethod(lambda _conn: nullcontext())

        @staticmethod
        def _append_event(_conn, task_id, kind, payload, run_id):
            _conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                          (task_id, run_id, kind, json.dumps(payload)))

    @contextmanager
    def board(_slug, **_kwargs):
        yield Board, conn
        conn.commit()

    monkeypatch.setattr(kanban_tools, "_board", board)
    monkeypatch.setattr(kanban_tools, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-a")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-a")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    agent = SimpleNamespace(session_id="worker-session", provider="local", model="model-a")

    assert observability.register_from_agent(agent) is True
    assert observability.register_from_agent(agent) is True
    assert json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=7").fetchone()[0])[
        "worker_session_id"] == "worker-session"
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
    assert observability.registered_receipt(conn, "task-a", 7)["worker_session_id"] == "worker-session"

    # Reclaimed runs and mismatched claim locks cannot mint a new receipt.
    conn.execute("UPDATE tasks SET current_run_id=8 WHERE id='task-a'")
    conn.commit()
    assert observability.register_from_agent(SimpleNamespace(session_id="stale")) is False
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
    conn.execute("UPDATE tasks SET current_run_id=7 WHERE id='task-a'")
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "wrong-claim")
    assert observability.register_from_agent(SimpleNamespace(session_id="wrong-claim")) is False
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
    conn.close()
