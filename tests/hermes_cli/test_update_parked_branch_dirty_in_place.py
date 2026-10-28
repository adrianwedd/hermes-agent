"""A dirty custom branch must still update when the configured strategy is update_in_place.

The parked-branch guard refused any dirty tree outright. `update_in_place` never moves the checkout —
it merges the target into the branch already checked out — so the autostash is not riding across
branches, and refusing made `hermes update -y` permanently skip every dirty feature branch. The
default `switch` strategy stays refusing: there the stash genuinely would cross branches.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from hermes_cli import update_cmd


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "studio/main")
    (root / "tracked.txt").write_text("base\ndirty\n", encoding="utf-8")  # uncommitted work
    return root


def _guard(monkeypatch, repo: Path, strategy: str):
    from hermes_cli import update_cmd_git

    stub = types.SimpleNamespace(
        PROJECT_ROOT=repo,
        _assess_parked_branch_switch=lambda git_cmd, cwd, cur, tgt: update_cmd_git._assess_parked_branch_switch(
            git_cmd, cwd, cur, tgt),
        _print_parked_branch_skip_warning=lambda *a, **k: None,
        _resume_windows_gateways_after_update=lambda *a, **k: None,
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: stub)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"parked_branch_strategy": strategy})
    monkeypatch.setattr(update_cmd, "_branch_head_suffix", lambda git_cmd, cwd: "")
    monkeypatch.setattr(update_cmd, "_git_run", lambda *a, **k: subprocess.run(
        ["git", *a[1]], cwd=a[2], capture_output=True, text=True, check=False))
    return update_cmd._apply_parked_branch_guard(
        ["git"], "main", "studio/main", switch_branch=False, _windows_gateway_resume=None)


def test_update_in_place_proceeds_on_a_dirty_branch(monkeypatch, repo: Path):
    parked, in_place, reason = _guard(monkeypatch, repo, "update_in_place")
    assert (parked, in_place, reason) == (False, True, "dirty")


def test_switch_strategy_still_refuses_a_dirty_branch(monkeypatch, repo: Path):
    with pytest.raises(SystemExit) as exc:
        _guard(monkeypatch, repo, "switch")
    assert exc.value.code == 1
