"""Awareness instead of gates: what a worker is told, and the two narrow holds.

The blanket grant gate is gone. What survives is the pair of hazards this machine
actually demonstrated: two heavy loads reserving the same memory, and work being
silently recreated by the dispatcher after the host went down under it.
"""
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_preflight import preflight
from hermes_cli.local_runtime import residency_gate


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_RESIDENCY_ROOT", str(tmp_path / "residency"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(tmp_path / "kanban.db") as conn:
        yield conn


def _memory(monkeypatch, *, total=256.0, available=200.0):
    monkeypatch.setattr(residency_gate, "total_gib", lambda: total)
    monkeypatch.setattr(residency_gate, "available_gib", lambda: available)


# --- the two safeguards -------------------------------------------------------

def test_a_reclaimed_run_from_before_boot_counts_as_host_loss(board, monkeypatch):
    """The reboot books the run as `reclaimed`, not `crashed`.

    Checking only the crash statuses made the safeguard never fire for the
    incident that motivated it.
    """
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    with kbc.write_txn(board):
        board.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (tid, "bench", "reclaimed", int(time.time()) - 7200),
        )
    task = kb.get_task(board, tid)

    monkeypatch.setattr(kbd, "_boot_time", lambda: int(time.time()) - 3600)

    assert kbd._last_run_interrupted_by_host_loss(board, task) is True


def test_an_interrupted_bench_card_is_not_relaunched(board, monkeypatch):
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    kb.recompute_ready(board)
    with kbc.write_txn(board):
        board.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (tid, "bench", "reclaimed", int(time.time()) - 7200),
        )
    monkeypatch.setattr(kbd, "_boot_time", lambda: int(time.time()) - 3600)
    _memory(monkeypatch, available=200.0)

    rows = kbd._lane_rows(board, "ready", result=kbd.DispatchResult())

    assert tid not in [row["id"] for row in rows]


def test_a_heavy_card_is_refused_when_the_machine_is_full(board, monkeypatch):
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    task = kb.get_task(board, tid)
    _memory(monkeypatch, total=256.0, available=20.0)

    verdict = kbd.admit_heavy_work(board, task)

    assert verdict["admitted"] is False
    assert "available 20.0 GiB" in verdict["reason"], "an undeclared heavy card is still priced and refused"


def test_a_heavy_card_is_admitted_when_there_is_room(board, monkeypatch):
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    task = kb.get_task(board, tid)
    _memory(monkeypatch, total=256.0, available=200.0)

    assert kbd.admit_heavy_work(board, task)["admitted"] is True


def test_ordinary_work_is_not_gated(board, monkeypatch):
    tid = kb.create_task(board, title="fix a typo", assignee="builder", workspace_kind="scratch")
    task = kb.get_task(board, tid)
    _memory(monkeypatch, total=256.0, available=1.0)

    assert kbd.admit_heavy_work(board, task)["admitted"] is True


def test_two_heavy_loads_cannot_reserve_the_same_memory(board, monkeypatch):
    _ = board  # the ledger lives under the isolated HERMES_RESIDENCY_ROOT
    _memory(monkeypatch, total=256.0, available=200.0)

    assert residency_gate.admit("run:first", 169.0)["admitted"] is True
    assert residency_gate.admit("run:second", 169.0)["admitted"] is False


# --- awareness ---------------------------------------------------------------

def test_preflight_reports_memory_and_coordination_hazards(board, monkeypatch):
    _memory(monkeypatch, total=256.0, available=44.0)
    mine = kb.create_task(board, title="mine", assignee="bench", workspace_kind="scratch",
                          workspace_path="/Users/adrian/repos/kanban")
    kb.create_task(board, title="theirs", assignee="builder", workspace_kind="scratch",
                   workspace_path="/Users/adrian/repos/kanban")

    brief = preflight(board, kb.get_task(board, mine))

    assert "44 GiB available" in brief
    assert "shares this workspace" in brief


def test_preflight_flags_an_old_brief(board, monkeypatch):
    _memory(monkeypatch)
    tid = kb.create_task(board, title="old brief", assignee="builder", workspace_kind="scratch")
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET created_at=? WHERE id=?", (int(time.time()) - 5 * 86400, tid))

    brief = preflight(board, kb.get_task(board, tid))

    assert "5 days old" in brief
    assert "Re-check" in brief


def test_preflight_never_blocks_the_task(board, monkeypatch):
    """Unmeasurable memory is reported, not enforced."""
    monkeypatch.setattr(residency_gate, "total_gib", lambda: None)
    monkeypatch.setattr(residency_gate, "available_gib", lambda: None)
    tid = kb.create_task(board, title="still runnable", assignee="builder", workspace_kind="scratch")

    brief = preflight(board, kb.get_task(board, tid))

    assert "unmeasurable" in brief.lower()


def test_an_unknown_heavy_job_is_priced_from_measured_evidence(board, monkeypatch):
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    task = kb.get_task(board, tid)
    _memory(monkeypatch, total=256.0, available=120.0)
    residency_gate.calibrate("GLM-5.3-Flash-oQ4e-mtp", 169.235, source="run-full-A")

    verdict = kbd.admit_heavy_work(board, task)

    # A 64 GiB floor would have waved this through; the measured 169.2 GiB does not.
    assert verdict["admitted"] is False
    assert "169.2 GiB" in verdict["reason"], "the heaviest measured load prices an unknown job"


def test_with_no_evidence_an_unknown_job_is_priced_conservatively(board, monkeypatch):
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    task = kb.get_task(board, tid)
    _memory(monkeypatch, total=256.0, available=200.0)

    verdict = kbd.admit_heavy_work(board, task)

    # 60% of 256 GiB = 153.6 -> does not fit in 200 GiB? it does; shrink headroom
    assert verdict["admitted"] is True
    monkeypatch.setattr(residency_gate, "available_gib", lambda: 120.0)
    assert kbd.admit_heavy_work(board, task)["admitted"] is False


def test_a_resident_model_is_not_counted_twice(board, monkeypatch):
    """`available` already excludes resident RSS; only pending holds are subtracted.

    A 100 GiB load shows up as the OS reporting 100 GiB available, not as 200 GiB
    available minus 100 GiB of RSS read back from the process table.
    """
    _memory(monkeypatch, total=256.0, available=100.0)
    monkeypatch.setattr(residency_gate, "observed_model_gib", lambda: 100.0)

    verdict = residency_gate.admit("run:next", 169.0)

    assert verdict["admitted"] is False
    assert verdict["available_gib"] == pytest.approx(100.0), "the observed RSS must not be deducted again"


def test_observed_processes_do_not_reduce_headroom_a_second_time(board, monkeypatch):
    _memory(monkeypatch, total=256.0, available=200.0)
    monkeypatch.setattr(residency_gate, "observed_model_gib", lambda: 50.0)

    # 150 GiB fits in 200 GiB of genuinely available memory, resident load and all.
    assert residency_gate.admit("run:next", 150.0)["admitted"] is True


def test_a_pending_hold_is_still_subtracted(board, monkeypatch):
    """The gap this reservation exists for: admitted, not yet resident."""
    _memory(monkeypatch, total=256.0, available=200.0)
    monkeypatch.setattr(residency_gate, "observed_model_gib", lambda: 0.0)

    assert residency_gate.admit("dispatch:t_a", 120.0)["admitted"] is True

    assert residency_gate.admit("dispatch:t_b", 120.0)["admitted"] is False


def test_reboot_recovery_waits_for_capacity_not_for_a_clock(board, monkeypatch):
    """The reassessment that matters is measured capacity, not a scheduler tick."""
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    kb.recompute_ready(board)
    with kbc.write_txn(board):
        board.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (tid, "bench", "reclaimed", int(time.time()) - 7200),
        )
    monkeypatch.setattr(kbd, "_boot_time", lambda: int(time.time()) - 3600)

    # The machine that went down is still full: hold, and say why.
    _memory(monkeypatch, total=256.0, available=20.0)
    held = kbd._lane_rows(board, "ready", result=kbd.DispatchResult())
    assert tid not in [row["id"] for row in held]

    # Capacity has come back: the same interrupted card is runnable again, with the
    # preflight carrying the interrupted run so the worker re-reads conditions.
    monkeypatch.setattr(residency_gate, "available_gib", lambda: 240.0)
    resumed = kbd._lane_rows(board, "ready", result=kbd.DispatchResult())
    assert tid in [row["id"] for row in resumed]


def _replaced_tick_test(board, monkeypatch):  # retained below for reference, not collected
    tid = kb.create_task(board, title="bench sweep", assignee="bench", workspace_kind="scratch")
    kb.recompute_ready(board)
    with kbc.write_txn(board):
        board.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
            (tid, "bench", "reclaimed", int(time.time()) - 7200),
        )
    boot = int(time.time()) - 3600
    monkeypatch.setattr(kbd, "_boot_time", lambda: boot)
    _memory(monkeypatch, total=256.0, available=240.0)

    first = kbd._lane_rows(board, "ready", result=kbd.DispatchResult())
    second = kbd._lane_rows(board, "ready", result=kbd.DispatchResult())

    assert tid not in [row["id"] for row in first], "the boot must not relaunch blind"
    assert tid in [row["id"] for row in second], "reassessment is a pause, not a quarantine"
