"""`hermes update -y` may not proceed without a recovery point.

Regression coverage for the unattended gate added to `update_cmd_maint`:

  * a recovery point switched off by config is fatal for -y, not a silent continue;
  * a requested-but-failed snapshot is fatal for -y;
  * a failed full backup is fatal for -y in `full` mode;
  * interactive runs keep the documented best-effort behaviour;
  * `--no-backup` remains the explicit, per-run acceptance of the risk.

Every test drives the decision functions directly with a fake argparse namespace and stubbed
snapshot/zip helpers, so nothing here touches a live install, a stash or a running service.
"""
from __future__ import annotations

import types

import pytest

from hermes_cli import update_cmd_maint as upd


def _args(*, yes: bool, no_backup: bool = False, backup: bool = False):
    return types.SimpleNamespace(yes=yes, no_backup=no_backup, backup=backup)


@pytest.fixture(autouse=True)
def _clean_outcome():
    """Each test starts from "the backup step has not run yet"."""
    upd._LAST_PRE_UPDATE_BACKUP.clear()
    yield
    upd._LAST_PRE_UPDATE_BACKUP.clear()


def _cfg(monkeypatch, mode):
    monkeypatch.setattr(upd, "_load_updates_cfg", lambda: {"pre_update_backup": mode, "backup_keep": 5})


def _snapshot(monkeypatch, value):
    def fake():
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(upd, "_run_quick_snapshots", fake)


def test_unattended_refuses_when_config_disables_the_recovery_point(monkeypatch, capsys):
    _cfg(monkeypatch, "off")
    args = _args(yes=True)

    assert upd._run_pre_update_backup(args) is None

    reason = upd.unattended_backup_refusal(args)
    assert reason and "updates.pre_update_backup is off" in reason
    assert "--no-backup" in reason  # names the explicit opt-out
    # Config-level off still prints nothing on stdout: the refusal message is the caller's job.
    assert capsys.readouterr().out == ""


def test_unattended_refuses_when_the_snapshot_fails(monkeypatch):
    _cfg(monkeypatch, "quick")
    _snapshot(monkeypatch, None)
    args = _args(yes=True)

    assert upd._run_pre_update_backup(args) is None

    reason = upd.unattended_backup_refusal(args)
    assert reason and reason.startswith("pre-update snapshot failed")
    assert "--no-backup" in reason


def test_unattended_refuses_when_the_snapshot_raises(monkeypatch):
    _cfg(monkeypatch, "quick")
    _snapshot(monkeypatch, RuntimeError("disk full"))
    args = _args(yes=True)

    upd._run_pre_update_backup(args)

    reason = upd.unattended_backup_refusal(args)
    assert reason and "disk full" in reason  # the failure detail survives into the refusal


def test_unattended_proceeds_when_the_snapshot_exists(monkeypatch):
    _cfg(monkeypatch, "quick")
    _snapshot(monkeypatch, "pre-update-20261028-000001")
    args = _args(yes=True)

    assert upd._run_pre_update_backup(args) == "pre-update-20261028-000001"
    assert upd.unattended_backup_refusal(args) is None
    assert upd.pre_update_backup_outcome() == {
        "mode": "quick", "snapshot_id": "pre-update-20261028-000001",
        "full_saved": None, "detail": "",
    }


def test_unattended_refuses_when_the_full_backup_fails(monkeypatch):
    _cfg(monkeypatch, "full")
    _snapshot(monkeypatch, "pre-update-20261028-000002")
    monkeypatch.setattr(upd, "_run_full_backup", lambda: False)
    args = _args(yes=True, backup=True)

    upd._run_pre_update_backup(args)

    reason = upd.unattended_backup_refusal(args)
    assert reason and "state backup failed" in reason


def test_unattended_proceeds_when_the_full_backup_lands(monkeypatch):
    _cfg(monkeypatch, "full")
    _snapshot(monkeypatch, "pre-update-20261028-000003")
    monkeypatch.setattr(upd, "_run_full_backup", lambda: True)
    args = _args(yes=True, backup=True)

    upd._run_pre_update_backup(args)

    assert upd.unattended_backup_refusal(args) is None


def test_interactive_run_keeps_best_effort_backups(monkeypatch):
    """No -y means no refusal: an operator is present to see the warning and decide."""
    _cfg(monkeypatch, "quick")
    _snapshot(monkeypatch, None)
    args = _args(yes=False)

    upd._run_pre_update_backup(args)

    assert upd.unattended_backup_refusal(args) is None


def test_no_backup_is_the_explicit_opt_out(monkeypatch):
    _cfg(monkeypatch, "off")
    args = _args(yes=True, no_backup=True)

    upd._run_pre_update_backup(args)

    assert upd.unattended_backup_refusal(args) is None


def test_refusal_is_none_before_the_backup_step_runs():
    """An un-run backup step must not manufacture a refusal for callers that never asked."""
    assert upd.unattended_backup_refusal(_args(yes=True)) is None


def test_backup_required_is_a_known_stop_class():
    """The token the caller records at exit must be one the metrics contract accepts."""
    from hermes_cli.observability.shared_metrics_contract import UPDATE_STOP_CLASSES

    assert "backup_required" in UPDATE_STOP_CLASSES


def test_gate_sits_after_the_no_op_exits():
    """A verified no-op must still exit 0, so the refusal cannot precede the up-to-date paths.

    Structural, because running the whole ``cmd_update`` would need a disposable install: the gate
    must fire only once the run has established that this checkout really will move.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "hermes_cli" / "update_cmd.py").read_text(encoding="utf-8")
    call_lines = [i for i, line in enumerate(src.splitlines())
                  if line.strip() == "_refuse_without_recovery_point()"]
    assert len(call_lines) == 2, "one call site per apply path (ZIP swap, git pull)"
    lines = src.splitlines()
    target_selected = next(i for i, l in enumerate(lines) if "_check.select_apply_target(" in l)
    already_up_to_date = next(i for i, l in enumerate(lines) if l.strip() == "if commit_count == 0:")
    assert all(i > target_selected for i in call_lines)
    # The git path's refusal is after BOTH up-to-date exits; the ZIP path guards its own no-op.
    assert call_lines[-1] > already_up_to_date
    assert "if not (release_sha and target_is_head):" in src
