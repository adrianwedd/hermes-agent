"""Board-scoped suppression receipts for every no-spawn outcome (#123963)."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "normal")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(kbd, "_run_reclaim_phase", lambda *a, **kw: None)
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Waiting", assignee="worker")
        yield conn, tid


@pytest.mark.parametrize("case,reason", [
    ("board_cap", "max_spawn"), ("host_cap", "max_in_progress"),
    ("held", "active_pr"), ("unassigned", "skipped_unassigned"),
    ("nonspawnable", "skipped_nonspawnable"),
    ("profile_cap", "skipped_per_profile_capped"),
    ("unknown", "unknown"), ("error", "dispatch_error"),
    ("claim", "claim_conflict"), ("workspace", "workspace_failed"),
    ("spawn", "spawn_failed"), ("memory", "memory_pressure:critical"),
    ("admission", "admission:operator"), ("locked", "skipped_locked"),
])
def test_nonspawn_tick_has_machine_readable_evidence(board, monkeypatch, case, reason):
    conn, tid = board
    kwargs: dict[str, Any] = {"spawn_fn": lambda *a: None}
    if case == "board_cap":
        kwargs["max_spawn"] = 0
    elif case == "host_cap":
        kwargs["max_in_progress"] = 1
        monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board: 1)
    elif case == "held":
        monkeypatch.setattr(kbd, "check_respawn_guard", lambda *a, **kw: "active_pr")
    elif case == "unassigned":
        conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (tid,))
    elif case == "nonspawnable":
        monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: False)
    elif case == "profile_cap":
        kb.create_task(conn, title="Busy", assignee="worker")
        conn.execute("UPDATE tasks SET status='running' WHERE id != ?", (tid,))
        kwargs["max_in_progress_per_profile"] = 1
    elif case == "unknown":
        monkeypatch.setattr(kbd, "_lane_rows", lambda *a, **kw: [])
    elif case == "error":
        def fail(*a, **kw):
            raise RuntimeError("injected tick failure")
        monkeypatch.setattr(kbd, "_tick_spawn_budget", fail)
    elif case == "claim":
        monkeypatch.setattr(kb, "claim_task", lambda *a, **kw: None)
    elif case == "workspace":
        def fail(*a, **kw):
            raise OSError("injected workspace failure")
        monkeypatch.setattr(kbd._kbw, "resolve_workspace", fail)
    elif case == "spawn":
        def fail(*a, **kw):
            raise OSError("injected spawn failure")
        kwargs["spawn_fn"] = fail
    elif case == "memory":
        monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "critical")
    elif case == "admission":
        from hermes_cli import kanban_decision
        monkeypatch.setattr(kanban_decision, "claim_allowed",
                            lambda *a: (False, {"workflow_stage": "OPERATOR", "reason": "private operator prose"}))
    elif case == "locked":
        @contextmanager
        def locked(*a):
            yield False
        monkeypatch.setattr(kbc, "_dispatch_tick_lock", locked)
    if case == "error":
        with pytest.raises(RuntimeError) as caught:
            kbd.dispatch_once(conn, **kwargs)
        result = caught.value.dispatch_result
    else:
        result = kbd.dispatch_once(conn, **kwargs)
    assert not result.spawned
    assert result.ready_total == 1
    assert result.suppression_reasons[reason] >= 1
    assert "unknown" not in result.suppression_reasons or case == "unknown"
    assert reason.split(":")[0] in kbd.describe_suppression([result])
    if case != "locked":  # The losing dispatcher must not write beside its owner.
        row = conn.execute("SELECT reasons FROM dispatch_evidence ORDER BY id DESC LIMIT 1").fetchone()
        assert json.loads(row[0])[reason] >= 1


@pytest.mark.parametrize("held", [False, True])
def test_prelock_error_receipt_requires_board_lock(board, monkeypatch, held):
    from hermes_cli import kanban_resume
    conn, tid = board
    locking = []
    active = []
    original_finish = kbd.finish_tick

    @contextmanager
    def lock(path):
        locking.append(path)
        active.append(held)
        try:
            yield held
        finally:
            active.pop()

    def finish(conn, result, *, persist=True):
        if persist:
            assert active == [True], "error receipt written without dispatch lock"
        return original_finish(conn, result, persist=persist)

    def fail(*args, **kwargs):
        raise RuntimeError("injected resume observation failure")

    monkeypatch.setattr(kanban_resume, "observe_resume_conditions", fail)
    monkeypatch.setattr(kbc, "_dispatch_tick_lock", lock)
    monkeypatch.setattr(kbd, "finish_tick", finish)
    before = conn.total_changes
    with pytest.raises(RuntimeError, match="injected resume observation failure") as caught:
        kbd.dispatch_once(conn)
    result = caught.value.dispatch_result
    assert locking == [kb.kanban_db_path()]
    assert result.suppression_reasons == ({"dispatch_error": 1} if held else
                                          {"dispatch_error": 1, "skipped_locked": 1})
    assert result.skipped_locked is (not held)
    rows = conn.execute("SELECT reasons FROM dispatch_evidence").fetchall()
    if held:
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == {"dispatch_error": 1}
    else:
        assert rows == []
        assert conn.total_changes == before


def test_stall_receipts_coalesce_survive_reopen_and_keep_recovery(board, monkeypatch):
    conn, tid = board
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *a, **kw: "active_pr")
    for _ in range(12):
        kbd.dispatch_once(conn, spawn_fn=lambda *a: None)
    with kbc.connect_closing() as reopened:
        rows = reopened.execute("SELECT * FROM dispatch_evidence ORDER BY id").fetchall()
        assert len(rows) == 1
        assert rows[0]["ticks"] == 12
        assert json.loads(rows[0]["reasons"]) == {"active_pr": 1}
        assert rows[0]["last_seen"] >= rows[0]["first_seen"]
        assert reopened.execute("SELECT COUNT(*) FROM task_events WHERE kind='respawn_guarded'").fetchone()[0] == 1
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *a, **kw: None)
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a: None)
    assert len(result.spawned) == 1
    assert not result.suppression_reasons
    with kbc.connect_closing() as reopened:
        rows = reopened.execute("SELECT * FROM dispatch_evidence ORDER BY id").fetchall()
        assert [r["state"] for r in rows] == ["suppressed", "spawned"]
        assert json.loads(rows[0]["reasons"]) == {"active_pr": 1}
    # A busy/idle healthy day must not evict yesterday's stall receipt.
    from hermes_cli.kanban_dispatch_evidence import finish_tick, MAX_EPISODES
    for n in range(MAX_EPISODES * 2):
        finish_tick(conn, kbd.DispatchResult(spawned=[(tid, "worker", "")] if n % 2 else []))
    assert conn.execute("SELECT COUNT(*) FROM dispatch_evidence").fetchone()[0] == 2
    assert json.loads(conn.execute("SELECT reasons FROM dispatch_evidence ORDER BY id LIMIT 1").fetchone()[0]) == {"active_pr": 1}
    with kbc.connect_closing(board="other") as other:
        assert other.execute("SELECT COUNT(*) FROM dispatch_evidence").fetchone()[0] == 0


def test_watcher_suppression_and_error_keep_board_evidence(board, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _DispatcherSettings
    conn, tid = board
    settings = _DispatcherSettings(60, 0, None, 3, 0, True, None, None)
    dispatcher = _KanbanDispatcher(kb, settings)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    for _ in range(8):
        results = dispatcher.suppressed_tick("gateway_paused")
        assert results[0][1].suppression_reasons == {"gateway_paused": 1}
    rows = conn.execute("SELECT * FROM dispatch_evidence").fetchall()
    assert len(rows) == 1 and rows[0]["ticks"] == 8
    assert dispatcher.tick_once()[0][1].suppression_reasons == {"max_spawn": 1}
    def fail(*a, **kw):
        raise RuntimeError("injected")
    monkeypatch.setattr(kbd, "dispatch_once", fail)
    result = dispatcher.tick_once()[0][1]
    assert result.suppression_reasons == {"dispatch_error": 1}
    assert json.loads(conn.execute("SELECT reasons FROM dispatch_evidence ORDER BY id DESC LIMIT 1").fetchone()[0]) == {"dispatch_error": 1}


def test_history_is_bounded_and_dry_run_does_not_write(board):
    from hermes_cli.kanban_dispatch_evidence import finish_tick, MAX_EPISODES
    conn, tid = board
    for n in range(MAX_EPISODES + 12):
        finish_tick(conn, kbd.DispatchResult(ready_total=1, suppression_reasons={f"test_gate_{n % 2}": 1}))
    assert conn.execute("SELECT COUNT(*) FROM dispatch_evidence").fetchone()[0] == MAX_EPISODES
    before = [tuple(r) for r in conn.execute("SELECT * FROM dispatch_evidence")]
    kbd.dispatch_once(conn, dry_run=True, max_spawn=0)
    assert before == [tuple(r) for r in conn.execute("SELECT * FROM dispatch_evidence")]


@pytest.mark.parametrize("reason", ["max_spawn", "active_pr", "unknown"])
def test_gateway_stuck_alarm_names_suppression(board, monkeypatch, caplog, reason):
    import asyncio
    import logging
    from types import SimpleNamespace
    from gateway import kanban_watchers as watchers
    result = kbd.DispatchResult(ready_total=1, suppression_reasons={} if reason == "unknown" else {reason: 1})
    fake = SimpleNamespace(tick_once=lambda: [("default", result)], ready_nonempty=lambda: True)
    monkeypatch.setattr(watchers, "_KanbanDispatcher", lambda *a: fake)
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(watchers, "_resolve_auto_decompose_settings", lambda *a: (False, 0))
    async def no_delay(*a):
        pass
    monkeypatch.setattr(watchers.asyncio, "sleep", no_delay)
    async def direct(fn, *a, **kw):
        return fn(*a, **kw)
    monkeypatch.setattr(watchers, "_to_thread_process_service", direct)
    class Watcher(watchers.GatewayKanbanWatchersMixin):
        _running = True
        ticks = 0
        def _kanban_dispatcher_boot(self):
            return lambda: {}, kb, {}
        def _release_kanban_dispatcher_lock(self):
            pass
        async def _sleep_between_ticks(self, interval):
            self.ticks += 1
            self._running = self.ticks < 6
    with caplog.at_level(logging.WARNING):
        asyncio.run(Watcher()._kanban_dispatcher_watcher())
    alarms = [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]
    assert alarms and all(f"{reason}=1" in message for message in alarms)


@pytest.mark.parametrize("disabled_by", ["env", "config"])
def test_disabled_gateway_records_existing_board_once(board, monkeypatch, disabled_by):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import config
    conn, tid = board
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "0" if disabled_by == "env" else "")
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    assert GatewayKanbanWatchersMixin()._kanban_dispatcher_boot() is None
    rows = conn.execute("SELECT * FROM dispatch_evidence").fetchall()
    assert len(rows) == 1 and rows[0]["ticks"] == 1
    assert json.loads(rows[0]["reasons"]) == {"gateway_disabled": 1}
    assert kb.get_task(conn, tid).status == "ready"


def test_dispatch_json_includes_durable_board_receipt(board, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli import kanban_ops, config
    monkeypatch.setattr(config, "load_config", lambda: {})
    args = SimpleNamespace(dry_run=False, max=0, json=True, failure_limit=3)
    assert kanban_ops._cmd_dispatch(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_total"] == 1
    assert payload["suppression_reasons"] == {"max_spawn": 1}
    assert payload["dispatch_evidence"][0]["reasons"] == {"max_spawn": 1}
