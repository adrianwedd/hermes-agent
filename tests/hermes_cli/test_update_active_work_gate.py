"""An update must not stop a gateway that is in the middle of agent turns.

The pause drains a gateway for a bounded budget (~45s) and then SIGTERMs it, so a longer turn is cut
off mid-work. `active_gateway_work()` reads the runtime record so the update can refuse before
anything is touched. Disposable HERMES_HOME fixtures only — no live gateway is read or signalled.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import update_cmd_posix_pause as pause_mod


def _home_with_record(root: Path, name: str, **fields) -> Path:
    home = root / name
    home.mkdir(parents=True)
    record = {"pid": 4242, "kind": "hermes-gateway", "gateway_state": "running", "active_agents": 0}
    record.update(fields)
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    return home


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """Point the profile-home lookup at disposable directories."""

    def _install(pairs):
        monkeypatch.setattr(
            "hermes_cli.update_receipt._profile_homes", lambda: [(p, h) for p, h in pairs])

    _install([])
    return _install


def test_idle_gateway_does_not_block(homes, tmp_path: Path):
    home = _home_with_record(tmp_path, "default", active_agents=0)
    homes([("default", home)])
    assert pause_mod.active_gateway_work() == []


def test_running_turn_is_reported(homes, tmp_path: Path):
    home = _home_with_record(tmp_path, "default", active_agents=2, pid=57892)
    homes([("default", home)])

    busy = pause_mod.active_gateway_work()

    assert busy == [{"profile": "default", "home": str(home), "active": 2, "pid": 57892}]


def test_stopped_gateway_is_not_counted(homes, tmp_path: Path):
    """A stopped gateway cannot be interrupted, whatever its last turn count said."""
    home = _home_with_record(tmp_path, "default", gateway_state="stopped", active_agents=3)
    homes([("default", home)])
    assert pause_mod.active_gateway_work() == []


def test_missing_or_malformed_record_is_not_activity(homes, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "gateway_state.json").write_text("{not json", encoding="utf-8")
    homes([("empty", empty), ("broken", broken)])
    assert pause_mod.active_gateway_work() == []


def test_one_busy_profile_among_idle_ones_is_enough(homes, tmp_path: Path):
    idle = _home_with_record(tmp_path, "idle", active_agents=0)
    busy = _home_with_record(tmp_path, "lulzbench", active_agents=1)
    homes([("idle", idle), ("lulzbench", busy)])

    assert [item["profile"] for item in pause_mod.active_gateway_work()] == ["lulzbench"]


def test_active_work_is_a_known_stop_class():
    from hermes_cli.observability.shared_metrics_contract import UPDATE_STOP_CLASSES

    assert "active_work" in UPDATE_STOP_CLASSES
