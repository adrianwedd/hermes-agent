"""End-to-end: a REAL MoA request through real response accounting to a persisted receipt.

The two existing regressions stop short of this. ``test_kanban_worker_route_receipt`` patches the
writer and asserts at its call site; ``test_kanban_run_routes`` hands the writer fake agent and
client slots. Neither proves that a real MoA turn — a real ``MoAClient`` resolving real
aggregator/adviser slots from a preset — reaches ``record_response_usage`` and lands on the board
as a receipt the drawer can read. This does: a real ``AIAgent(provider="moa")``, the real
``build_moa_facade`` facade, the real fan-out, the real ``record_response_usage`` and the real
``record_worker_route``, on a temporary native board with claimed worker coordinates. The only
stand-in is the provider transport (``call_llm``), which no test may point at a live provider.

Read back: the exact run metadata, the immutable event, ``run_routes`` output (the renderer's
input), and the claim fence / missing-route controls. Regression for t_e07ae2d6.
"""

from __future__ import annotations

import json
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Imported at module scope like every other AIAgent test: ``import run_agent`` boots
# ``hermes_bootstrap``, whose payload probe reads ``<checkout-parent>/manifest.json``. At
# collection time that is outside the per-test real-home I/O guard; inside a test body it is
# a guarded path and the guard refuses it.
from run_agent import AIAgent

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_SESSION = "20261007_000000_e2e0"

CONFIG = """
moa:
  default_preset: closed
  presets:
    closed:
      enabled: true
      reference_models:
        - provider: openai-codex
          model: gpt-6.1-sol
        - provider: xai
          model: grok-4.6
      aggregator:
        provider: openai-codex
        model: gpt-6.1-sol
    solo:
      enabled: false
      aggregator:
        provider: openai-codex
        model: gpt-6.1-sol
""".strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated native board with this process as the live worker on one running run."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    # A provider request from this test is a bug, not a slow path: the transport is stubbed.
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network prohibited")))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    db_path = str(kb.kanban_db_path())
    monkeypatch.setenv("HERMES_KANBAN_DB", db_path)
    from hermes_cli import kanban_worker_observability as obs
    obs._WRITTEN.clear()
    obs._LAST_WRITE.clear()
    yield conn, db_path, tmp_path
    conn.close()


def _running_run(conn, tmp_path, *, pid=None, claim="studio.test:1"):
    """One claimed, running card whose run belongs to *pid* (default: this process)."""
    tid = kb.create_task(
        conn, title="worker card", assignee="implementer",
        workspace_kind="dir", workspace_path=str(tmp_path / "ws"),
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
    os.environ["HERMES_KANBAN_TASK"] = tid
    os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
    os.environ["HERMES_KANBAN_CLAIM_LOCK"] = claim
    return tid, run_id


def _usage(inp, out):
    return SimpleNamespace(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out,
                           prompt_tokens_details=None, completion_tokens_details=None)


def _response(content, inp, out, model=None, provider=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]),
                                 finish_reason="stop")],
        usage=_usage(inp, out), model=model, provider=provider,
    )


def _transport():
    """Controlled provider transport: advisers and aggregator answer, nothing leaves the box."""

    def call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            return _response("advice", 20, 5, kwargs.get("model"), kwargs.get("provider"))
        return _response("acted", 100, 10, kwargs.get("model"), kwargs.get("provider"))

    return call_llm


def _agent(tmp_path, preset):
    """A real agent on the MoA virtual provider, built by the real init path."""
    return AIAgent(
        api_key="moa-virtual-provider", base_url="http://127.0.0.1/v1", model=preset,
        provider="moa", session_id=_SESSION, platform="cli", quiet_mode=True,
        skip_context_files=True, skip_memory=True, save_trajectories=False, enabled_toolsets=["file"],
        max_iterations=1,
    )


def _recorded_turn(agent, *, messages=None):
    """Drive one real MoA request, then fold its response through real response accounting."""
    from agent import turn_usage

    messages = messages or [{'role': 'user', 'content': 'clean the db'}]
    with patch('agent.moa_loop.call_llm', _transport()):
        agent.client.chat.completions.create(model=agent.model, messages=messages)
        return turn_usage.record_response_usage(
            agent, _response("acted", 120, 12, agent.model, agent.provider),
            messages=messages, api_call_count=1, api_duration=0.1,
            compression_attempts=0, max_compression_attempts=3,
        )


def _metadata(conn, run_id):
    return json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] or "{}")


def _selected(conn, tid):
    from hermes_cli.kanban_run_routes import run_routes
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    return run_routes(conn, task, lambda profile, sid: None)['selected']


def test_a_real_moa_turn_lands_its_resolved_route_as_a_persisted_receipt(board):
    conn, _db, tmp_path = board
    tid, run_id = _running_run(conn, tmp_path)
    agent = _agent(tmp_path, "closed")
    try:
        assert agent.provider == "moa" and agent.model == "closed"
        # The real facade resolved real slots from the preset before accounting ran.
        with patch('agent.moa_loop.call_llm', _transport()):
            agent.client.chat.completions.create(model="closed", messages=[{'role': 'user', 'content': 'hi'}])
        assert agent.client.last_aggregator_slot == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
        assert agent.client.last_reference_slots == [
            {"provider": "openai-codex", "model": "gpt-6.1-sol"},
            {"provider": "xai", "model": "grok-4.6"},
        ]
        _recorded_turn(agent)
    finally:
        agent.close()
        for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
            os.environ.pop(k, None)

    metadata = _metadata(conn, run_id)
    assert metadata["worker_session_id"] == _SESSION
    # The launch route is the virtual preset the agent itself reports; the runtime route is the
    # RESOLVED one. They are different facts and both are recorded.
    assert metadata["launch_route"] == {"provider": "moa", "model": "closed"}
    route = metadata["runtime_route"]
    assert route["provider"] == "moa" and route["model"] == "closed"
    assert route["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert route["advisers"] == [{"provider": "openai-codex", "model": "gpt-6.1-sol"},
                                 {"provider": "xai", "model": "grok-4.6"}]
    assert isinstance(route["observed_at"], float) and route["observed_at"] > 0

    # The immutable event carries the same receipt (run metadata is replaced at a terminal
    # transition, so the event is the durable copy).
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_session_registered'",
        (tid, run_id),
    ).fetchone()
    assert event is not None
    payload = json.loads(event[0])
    assert payload["runtime_route"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}

    # The renderer's exact input: the resolved slots reach the drawer through run_routes.
    selected = _selected(conn, tid)
    assert selected["run_id"] == run_id
    assert selected["basis"] == "Worker-recorded runtime route"
    assert selected["last_observed"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert [f"{a['provider']} / {a['model']}" for a in selected["last_observed"]["advisers"]] == [
        "openai-codex / gpt-6.1-sol", "xai / grok-4.6",
    ]


def test_the_receipt_survives_the_terminal_metadata_replacement(board):
    """``_end_run`` overwrites run metadata with the worker's handoff; the drawer must still
    find the route on a finished card, which the immutable event is for."""
    conn, _db, tmp_path = board
    tid, run_id = _running_run(conn, tmp_path)
    agent = _agent(tmp_path, "closed")
    try:
        _recorded_turn(agent)
    finally:
        agent.close()
        for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
            os.environ.pop(k, None)

    with kbc.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     (json.dumps({"worker_session_id": _SESSION}), run_id))
    assert "runtime_route" not in _metadata(conn, run_id)

    selected = _selected(conn, tid)
    assert selected["basis"] == "Worker-recorded runtime route"
    assert selected["last_observed"]["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert selected["last_observed"]["advisers"] == [{"provider": "openai-codex", "model": "gpt-6.1-sol"},
                                                     {"provider": "xai", "model": "grok-4.6"}]
    # The timestamp the drawer renders is the recorded observation, not the read time.
    assert selected["last_observed"]["observed_at"] > 0


def test_a_turn_that_resolved_no_advisers_records_absence_not_a_guess(board):
    """Missing-route control: a preset whose fan-out resolved nothing records the aggregator it
    did use and an EMPTY adviser list — never the configured roster it never called."""
    conn, _db, tmp_path = board
    tid, run_id = _running_run(conn, tmp_path)
    agent = _agent(tmp_path, "solo")
    try:
        _recorded_turn(agent)
        assert agent.client.last_reference_slots == []
    finally:
        agent.close()
        for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
            os.environ.pop(k, None)

    route = _metadata(conn, run_id)["runtime_route"]
    assert route["aggregator"] == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert route["advisers"] == []
    # The renderer reads these as "not recorded", not as a guessed adviser roster.
    assert _selected(conn, tid)["last_observed"]["advisers"] == []


def test_a_run_this_process_does_not_own_is_never_stamped(board):
    """Claim fence, end-to-end: a stale worker's real turn must not write its route onto the run
    that replaced it, and the drawer then has no route to misattribute."""
    conn, _db, tmp_path = board
    tid, run_id = _running_run(conn, tmp_path, pid=os.getpid() + 1)
    agent = _agent(tmp_path, "closed")
    try:
        before = _metadata(conn, run_id)
        _recorded_turn(agent)
        assert _metadata(conn, run_id) == before
    finally:
        agent.close()
        for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
            os.environ.pop(k, None)

    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND run_id=? "
                        "AND kind LIKE 'worker_%'", (tid, run_id)).fetchone()[0] == 0
    selected = _selected(conn, tid)
    assert selected["last_observed"] is None
    assert selected["launch"] is None
