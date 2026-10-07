"""Card polling reports unavailable owners without repeated exception logging."""
from dataclasses import asdict
from pathlib import Path
import logging

import pytest

from hermes_cli import kanban_card_facts as facts
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_dispatch_facts import dispatch_facts


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(home / "kanban.db") as conn:
        yield home, conn


def _task(conn, owner):
    tid = kb.create_task(conn, title="Retained operator decision", assignee="default")
    conn.execute("UPDATE tasks SET dispatch_eligible=0, assignee=? WHERE id=?", (owner, tid))
    conn.commit()
    task = kb.get_task(conn, tid)
    assert task is not None
    return asdict(task)


def _poll(conn, task, loader=facts.native_config_for):
    return facts.card_facts(conn, [task], loader, lambda *_: {})[task["id"]]


def _logs(caplog):
    return [r for r in caplog.records if r.name == facts.__name__ and r.levelno >= logging.WARNING]


@pytest.mark.parametrize("owner", ["operator", "worker", "../invalid", "analyst"])
def test_unavailable_profile_is_quiet_and_rechecked(board, caplog, owner):
    home, conn = board
    task = _task(conn, owner)
    expected_dispatch = dispatch_facts(conn, task)
    card = _poll(conn, task)
    for _ in range(2):
        card = _poll(conn, task)
    assert not _logs(caplog)
    assert card["dispatch"] == expected_dispatch
    assert card["dispatch"]["dispatchable"] is False
    assert card["profile"]["available"] is False
    assert card["profile"]["reason_code"] in {"unknown_profile", "invalid_profile"}
    assert card["profile"]["reason"]
    assert card["model"]["basis"] == "Profile configuration unavailable"
    assert not (home / "profiles").exists()

    # A newly valid owner must heal without a process restart or cache expiry.
    if owner == "analyst":
        profile = home / "profiles" / owner
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model:\n  provider: test\n  default: recovered\n")
        card = _poll(conn, task)
        assert card["profile"] == {"name": owner, "available": True, "reason_code": None, "reason": None}
        assert card["model"]["primary"] == "test / recovered"
        assert card["dispatch"]["dispatchable"] is False
        assert not _logs(caplog)


@pytest.mark.parametrize("owner,reason", [
    ("missing-worker", "unknown_profile"), ("../invalid", "invalid_profile"),
    ("broken-worker", "config_load_failed"),
])
def test_eligible_unavailable_owner_keeps_stage_but_cannot_execute(board, owner, reason):
    home, conn = board
    if reason == "config_load_failed":
        profile = home / "profiles" / owner
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model: [broken\n")
    tid = kb.create_task(conn, title="Authorised action", assignee=owner)
    stored = kb.get_task(conn, tid)
    assert stored is not None
    task = asdict(stored)
    assert task["dispatch_eligible"] == 1
    canonical = dispatch_facts(conn, task)
    assert canonical["stage"] == "READY" and canonical["dispatchable"]
    before = conn.total_changes
    for _ in range(3):
        card = _poll(conn, task)
        assert card["profile"]["reason_code"] == reason
        assert card["dispatch"]["dispatchable"] is False
        assert card["dispatch"]["worker_executable_now"] is False
        for key in canonical.keys() - {"dispatchable", "worker_executable_now"}:
            assert card["dispatch"][key] == canonical[key]
    assert conn.total_changes == before
    stored = kb.get_task(conn, tid)
    assert stored is not None
    assert asdict(stored) == task
    if reason != "config_load_failed":
        assert not (home / "profiles").exists()

    # Repair the owner, not its authorised semantic stage or eligibility.
    (home / "config.yaml").write_text("model:\n  provider: test\n  default: recovered\n")
    conn.execute("UPDATE tasks SET assignee='default' WHERE id=?", (tid,))
    conn.commit()
    stored = kb.get_task(conn, tid)
    assert stored is not None
    task = asdict(stored)
    before = conn.total_changes
    card = _poll(conn, task)
    assert card["profile"]["available"] is True
    assert card["dispatch"] == dispatch_facts(conn, task)
    assert card["dispatch"]["stage"] == "READY"
    assert card["dispatch"]["dispatchable"] is True
    assert card["dispatch"]["worker_executable_now"] is True
    assert conn.total_changes == before


def test_unexpected_config_failure_is_observable_without_poll_flood(board, caplog):
    home, conn = board
    profile = home / "profiles" / "analyst"
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_text("model: [broken\n")
    task = _task(conn, "analyst")
    card = _poll(conn, task)
    for _ in range(2):
        card = _poll(conn, task)
    assert len(_logs(caplog)) == 1
    assert _logs(caplog)[0].exc_info is not None
    assert card["profile"]["available"] is False
    assert card["profile"]["reason_code"] == "config_load_failed"
    assert card["profile"]["reason"]
    assert card["dispatch"]["dispatchable"] is False

    config.write_text("model:\n  provider: test\n  default: repaired\n")
    assert _poll(conn, task)["model"]["primary"] == "test / repaired"
    # A later outage is a fresh diagnostic, not suppressed for the process lifetime.
    config.write_text("model: [broken\n")
    card = _poll(conn, task)
    for _ in range(2):
        card = _poll(conn, task)
    assert len(_logs(caplog)) == 2
    assert card["profile"]["reason_code"] == "config_load_failed"


@pytest.mark.parametrize("owner", [None, "", "default"])
def test_default_profile_is_not_misclassified_as_missing(board, owner):
    home, conn = board
    (home / "config.yaml").write_text("model:\n  provider: test\n  default: root-model\n")
    task = _task(conn, owner)
    card = _poll(conn, task)
    assert card["model"]["primary"] == "test / root-model"
    assert card["profile"] == {"name": "default", "available": True, "reason_code": None, "reason": None}
    assert card["dispatch"]["dispatchable"] is False


@pytest.mark.parametrize("state", ["empty", "deleted"])
def test_nonlive_profile_directory_is_not_a_valid_owner(board, caplog, state):
    from hermes_constants import profile_tombstone_path

    home, conn = board
    profile = home / "profiles" / "analyst"
    profile.mkdir(parents=True)
    if state == "deleted":
        (profile / "config.yaml").write_text("model: stale\n")
        tombstone = profile_tombstone_path(profile)
        tombstone.parent.mkdir(parents=True)
        tombstone.touch()
    task = _task(conn, "analyst")
    for _ in range(3):
        card = _poll(conn, task)
        assert card["profile"]["reason_code"] == "unknown_profile"
        assert card["dispatch"]["dispatchable"] is False
    assert not _logs(caplog)


@pytest.mark.parametrize("error_type", [FileNotFoundError, ValueError, PermissionError])
def test_config_errors_are_not_mistaken_for_unknown_profiles(board, monkeypatch, caplog, error_type):
    from hermes_cli import config_effective
    from hermes_constants import get_hermes_home

    home, conn = board
    profile = home / "profiles" / "analyst"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: test\n")
    calls = []

    def broken_loader(*args, **kwargs):
        assert get_hermes_home() == profile
        calls.append(args)
        raise error_type("configuration backend failed")

    monkeypatch.setattr(config_effective, "load_user_config_effective", broken_loader)
    clock = [0.0]
    monkeypatch.setattr(facts, "monotonic", lambda: clock[0])
    tasks = [_task(conn, "analyst"), _task(conn, "analyst")]
    for _ in range(3):
        cards = facts.card_facts(conn, tasks, facts.native_config_for, lambda *_: {})
        assert all(c["profile"]["reason_code"] == "config_load_failed" for c in cards.values())
        assert get_hermes_home() == home  # exceptions must restore the override
    assert len(calls) == 3  # per-pass dedup, but never a stale negative cache
    assert len(_logs(caplog)) == 1
    clock[0] = facts._CONFIG_FAILURE_LOG_INTERVAL
    _poll(conn, tasks[0])
    assert len(_logs(caplog)) == 2  # bounded periodic reminder, not silent forever
    other_home = home / "other-root"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other_home))

    def independent_failure(_):
        raise error_type("independent configuration backend failed")

    card = _poll(conn, tasks[0], loader=independent_failure)
    assert card["profile"]["reason_code"] == "config_load_failed"
    assert len(_logs(caplog)) == 3  # an independent home is not silenced
