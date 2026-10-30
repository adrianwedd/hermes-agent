"""An unchanged refusal must not buy another preparation cycle, forever.

`preparation_checked` fired 538 times on one card in six hours without producing
a single outcome. The dedupe compared the stored `decision_fingerprint` and
required `prepared is False`; payloads written by older code carry only
`{"reason": ...}`, so the comparison could never match and the card was
re-prepared on every tick. One backfill event now retires the legacy row.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_preparation import prepare_tick


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(tmp_path / "kanban.db") as conn:
        yield conn


def _automatic_todo(conn):
    """A todo card the preparation scan is willing to look at."""
    tid = kb.create_task(conn, title="Automatic preparation card", workspace_kind="scratch")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completion_requirements",
                         {"preparation": {"mode": "automatic"}})
        # What the older writer left behind: no fingerprint, no `prepared` key.
        kb._append_event(conn, tid, "preparation_checked", {"reason": "scope_or_manual_hold_changed"})
    return tid


def _checked(conn, tid):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='preparation_checked' ORDER BY id",
        (tid,),
    ).fetchall()


def test_a_legacy_refusal_is_re_prepared_once_and_then_retired(board):
    """The old row carries no fingerprint, so it cannot be compared.

    Prepare once — the old refusal may no longer hold (a profile now exists, a
    workspace is now free) — then let the recorded fingerprint make every
    unchanged tick a no-op.
    """
    tid = _automatic_todo(board)
    assert len(_checked(board, tid)) == 1

    prepare_tick(board, limit=1, scan_limit=8, facts_loader=lambda *a, **k: {})

    rows = _checked(board, tid)
    assert len(rows) == 2, "one real preparation attempt, recorded with a fingerprint"
    assert json.loads(rows[-1][0])["decision_fingerprint"]

    # The next ticks have nothing new to say and must add no event at all.
    for _ in range(3):
        prepare_tick(board, limit=1, scan_limit=8, facts_loader=lambda *a, **k: {})
    assert len(_checked(board, tid)) == 2


def test_an_unchanged_refusal_does_not_emit_one_event_per_tick(board):
    tid = _automatic_todo(board)
    prepare_tick(board, limit=1, scan_limit=8, facts_loader=lambda *a, **k: {})
    baseline = len(_checked(board, tid))

    for _ in range(5):
        prepare_tick(board, limit=1, scan_limit=8, facts_loader=lambda *a, **k: {})

    assert len(_checked(board, tid)) == baseline
