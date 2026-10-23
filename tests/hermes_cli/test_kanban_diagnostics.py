"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48


def test_review_handoff_clears_historical_crash_streak():
    runs = [
        _run("crashed", run_id=1, error="old worker failure"),
        _run("crashed", run_id=2, error="old worker failure"),
        _run("review_requested", run_id=3),
    ]
    reviewed = kd.compute_task_diagnostics(_task(status="review"), [], runs)
    assert "repeated_crashes" not in {d.kind for d in reviewed}

    # A fresh crash after handoff is a new streak, not the old two plus one.
    reopened = kd.compute_task_diagnostics(
        _task(status="ready"), [], [*runs, _run("crashed", run_id=4)],
    )
    assert "repeated_crashes" not in {d.kind for d in reopened}

    # No successful handoff: the same crashes still require diagnosis.
    failing = kd.compute_task_diagnostics(_task(status="ready"), [], runs[:2])
    assert "repeated_crashes" in {d.kind for d in failing}


def test_review_event_without_run_resets_only_prior_crashes():
    runs = [_run("crashed", run_id=1), _run("crashed", run_id=2)]
    events = [_event("crashed"), _event("crashed"), _event("review_requested")]
    reviewed = kd.compute_task_diagnostics(_task(status="review"), events, runs)
    assert "repeated_crashes" not in {d.kind for d in reviewed}

    # A new crash after the handoff starts a fresh streak, not the old streak.
    once = kd.compute_task_diagnostics(
        _task(status="ready"), [*events, _event("crashed")],
        [*runs, _run("crashed", run_id=3)],
    )
    assert "repeated_crashes" not in {d.kind for d in once}
    twice = kd.compute_task_diagnostics(
        _task(status="ready"), [*events, _event("crashed"), _event("crashed")],
        [*runs, _run("crashed", run_id=3), _run("crashed", run_id=4)],
    )
    assert "repeated_crashes" in {d.kind for d in twice}

    # A clean worker exit without its terminal call is stored as a crashed run
    # but the corresponding event is named protocol_violation.
    protocol = kd.compute_task_diagnostics(
        _task(status="ready"),
        [*events, _event("protocol_violation"), _event("protocol_violation")],
        [*runs, _run("crashed", run_id=3), _run("crashed", run_id=4)],
    )
    assert "repeated_crashes" in {d.kind for d in protocol}


def test_review_boundary_uses_event_id_after_clock_step_back():
    """The single-card CLI lists by timestamp, not necessarily insertion order."""
    inserted = [
        {**_event("crashed", ts=100), "id": 1},
        {**_event("crashed", ts=101), "id": 2},
        {**_event("review_requested", ts=200), "id": 3},
        {**_event("crashed", ts=199), "id": 4},
        {**_event("crashed", ts=199), "id": 5},
    ]
    cli_order = sorted(inserted, key=lambda event: (event["created_at"], event["id"]))
    runs = [_run("crashed", run_id=run_id) for run_id in range(1, 5)]
    diags = kd.compute_task_diagnostics(_task(status="ready"), cli_order, runs)
    crashes = [diag for diag in diags if diag.kind == "repeated_crashes"]
    assert len(crashes) == 1
    assert crashes[0].data["consecutive_crashes"] == 2


def test_block_unblock_cycling_is_history_on_a_terminal_card():
    """RED on base: ``_rule_block_unblock_cycling`` was the only rule in
    ``_RULES`` without a terminal-status guard, so a ``done`` card with a
    retained block→unblock streak still carried the warning through the served
    payload — the red fault badge the operator's contract forbids ("Completed
    cards must not show stale progress or red warnings"). A finished card
    cannot be blocked again, so the window is frozen history; the sibling rules
    (``_rule_repeated_failures``, ``_rule_repeated_crashes``) already exempt
    done/archived for exactly that reason.

    The control arm pins that the carve-out is targeted: the identical event
    history on a live card is still a real distress signal.
    """
    now = 100_000
    events: list = []
    # 4 block→unblock pairs: the first block opens the window and does not count,
    # so this is the 3 cycles the default threshold needs (same shape on both arms).
    for i in range(4):
        events.append(_event("blocked", ts=now - 800 + i * 200, reason="flaky harness"))
        events.append(_event("unblocked", ts=now - 700 + i * 200))

    live = kd.compute_task_diagnostics(_task(status="ready"), events, [], now=now)
    assert [d.kind for d in live] == ["block_unblock_cycling"], [d.kind for d in live]

    for status in ("done", "archived"):
        diags = kd.compute_task_diagnostics(_task(status=status), events, [], now=now)
        assert "block_unblock_cycling" not in [d.kind for d in diags], (status, diags)


def test_terminal_card_diagnostics_are_history_not_a_live_fault():
    """RED on base: two rules in ``_RULES`` had no terminal-status guard, and both are
    reachable on a finished card through the real write path.

    - ``_rule_prose_phantom_refs`` reads ``suspected_hallucinated_references``, which
      ``kanban_db._flag_phantom_prose_refs`` appends AFTER the ``completed`` event, so the
      flag is active on a ``done`` card.
    - ``_rule_hallucinated_cards`` reads ``completion_blocked_hallucination``;
      ``_active_hallucination_events`` clears only on a later ``completed``/``edited``, and
      ``archive_task`` appends ``archived`` — so the flag is active on an ``archived`` card.

    Both therefore reached the served payload and the browser/mobile drawer, painting a
    fault section on finished work (the operator's "completed cards must not show stale
    progress or red warnings" contract). The exemption now lives ONCE in
    ``compute_task_diagnostics`` so no future rule can reintroduce the class.

    The control arm pins that the gate is targeted, not a blanket silencing: the identical
    event history on a live card is still a real signal.
    """
    now = 100_000
    blocked = [_event("completion_blocked_hallucination", ts=now - 100, phantom_cards=["t_deadbeef"])]
    prose = [_event("suspected_hallucinated_references", ts=now - 100, phantom_refs=["t_deadbeef"])]

    for events, kind in ((blocked, "hallucinated_cards"), (prose, "prose_phantom_refs")):
        # Control: a live card with the same history is a real distress signal.
        live = kd.compute_task_diagnostics(_task(status="ready"), events, [], now=now)
        assert kind in [d.kind for d in live], (kind, live)
        # Subject: a finished card cannot hit that failure again, so it is history.
        for status in ("done", "archived"):
            diags = kd.compute_task_diagnostics(_task(status=status), events, [], now=now)
            assert diags == [], (kind, status, diags)


def test_every_terminal_card_yields_no_diagnostics_whatever_the_history():
    """Board-wide invariant, not a per-rule list: a done/archived card must yield NO
    diagnostic from any rule, present or future. Seeds one event per known rule trigger
    and asserts the engine returns nothing for a terminal status while the same card in a
    live status does fire — so a rule added later without its own guard cannot reopen the
    "completed cards must not show red warnings" contract by omission.
    """
    now = 100_000
    events = [
        _event("completion_blocked_hallucination", ts=now - 90, phantom_cards=["t_deadbeef"]),
        _event("suspected_hallucinated_references", ts=now - 80, phantom_refs=["t_deadbeef"]),
        _event("blocked", ts=now - 70, reason="flaky harness"),
        _event("unblocked", ts=now - 60),
        _event("blocked", ts=now - 50, reason="flaky harness"),
        _event("unblocked", ts=now - 40),
        _event("blocked", ts=now - 30, reason="flaky harness"),
        _event("unblocked", ts=now - 20),
        _event("blocked", ts=now - 10, reason="flaky harness"),
    ]
    # The control arm proves the fixture is not vacuous: at least one rule fires live.
    live = kd.compute_task_diagnostics(_task(status="ready"), events, [], now=now)
    assert live, "fixture must trigger at least one rule on a live card"
    for status in ("done", "archived"):
        assert kd.compute_task_diagnostics(_task(status=status), events, [], now=now) == [], status









# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_summaryless_review_handoff_clears_crash_history(kanban_home):
    """The public transition records an event but no run on this path."""
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="review after failures", assignee="worker")
        for attempt in range(2):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                "VALUES (?, 'crashed', 'crashed', ?, ?)",
                (task_id, 100 + attempt, 100 + attempt),
            )
            kb._append_event(conn, task_id, "crashed", {"attempt": attempt})
        conn.commit()

        assert kb.request_review(conn, task_id, reviewer="reviewer") is True
        runs = kb.list_runs(conn, task_id)
        events = kb.list_events(conn, task_id)
        assert len(runs) == 2  # no synthesized review run
        assert any(event.kind == "review_requested" for event in events)
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        diags = kd.compute_task_diagnostics(task, events, runs)
        assert "repeated_crashes" not in {d.kind for d in diags}
    finally:
        conn.close()


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
