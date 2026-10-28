"""Autostash lifecycle: the saved work is pinned, and the stash is dropped only when verified.

Disposable git fixtures only — each test builds its own repository under tmp_path, so nothing here
touches the live checkout, its stashes or any service.

The defect class: `git stash apply` can exit 0 and still leave a hunk behind, and the old code
dropped the stash on that exit code. A successful apply is not evidence of a successful restore.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd_stash as stash_mod

GIT = ["git"]


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run(root, "add", "tracked.txt")
    _run(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture(autouse=True)
def _clean_module_state():
    stash_mod._pending_autostash = None
    stash_mod._pending_autostash_ref = None
    yield
    stash_mod._pending_autostash = None
    stash_mod._pending_autostash_ref = None


def _dirty(root: Path) -> None:
    (root / "tracked.txt").write_text("base\nlocal edit\n", encoding="utf-8")
    (root / "notes.md").write_text("untracked work\n", encoding="utf-8")


def _stash(root: Path) -> str:
    """Capture the tree; the stash commit is what every assertion below works from."""
    ref = stash_mod._stash_local_changes_if_needed(GIT, root)
    assert ref, "a dirty tree must produce a stash commit"
    return ref


def _stashes(root: Path) -> str:
    return _run(root, "stash", "list", "--format=%gd %H").stdout.strip()


def _localwork_refs(root: Path) -> list[str]:
    out = _run(root, "for-each-ref", "--format=%(refname)", "refs/hermes-localwork/").stdout
    return [line for line in out.splitlines() if line.startswith("refs/hermes-localwork/")]


# --- capture --------------------------------------------------------------------------------

def test_stash_pins_a_durable_ref_and_cleans_the_tree(repo: Path):
    _dirty(repo)

    stash_ref = _stash(repo)

    assert stash_ref, "a dirty tree must produce a stash commit"
    assert _stashes(repo).count("\n") == 0
    assert _run(repo, "status", "--porcelain").stdout.strip() == ""

    refs = _localwork_refs(repo)
    assert len(refs) == 1, refs
    assert stash_mod._pending_autostash_ref == refs[0]
    # The pin points at the same object the stash entry does: one recovery point, two names.
    assert _run(repo, "rev-parse", refs[0]).stdout.strip() == stash_ref
    assert stash_mod._pending_autostash is not None  # unsettled until it is verified back


# --- verified restore -----------------------------------------------------------------------

def test_verified_restore_retires_the_stash_and_the_pin(repo: Path):
    _dirty(repo)
    stash_ref = _stash(repo)
    assert _run(repo, "stash", "apply", stash_ref).returncode == 0

    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)
    assert verified, detail

    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is True
    assert _stashes(repo) == ""
    assert _localwork_refs(repo) == []
    assert stash_mod._pending_autostash is None


def test_untracked_file_is_part_of_the_verified_state(repo: Path):
    _dirty(repo)
    stash_ref = _stash(repo)
    _run(repo, "stash", "apply", stash_ref)
    (repo / "notes.md").unlink()  # the update's own file at that path would look like this

    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)

    assert verified is False
    assert "notes.md" in detail


# --- unverified restore: keep everything ----------------------------------------------------

def test_mismatch_keeps_the_stash_and_the_pin(repo: Path, monkeypatch, capsys):
    _dirty(repo)
    stash_ref = _stash(repo)
    _run(repo, "stash", "apply", stash_ref)
    monkeypatch.setattr(stash_mod, "_worktree_matches_stash", lambda *a, **k: (False, "simulated loss"))

    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is False

    assert stash_ref in _stashes(repo), "the stash must survive an unverified restore"
    assert len(_localwork_refs(repo)) == 1, "the durable pin must survive too"
    assert stash_mod._pending_autostash is not None, "the run must stay unsettled, not report success"
    out = capsys.readouterr().out
    assert "NOT dropped" in out and "Durable ref" in out


def test_a_lost_hunk_is_caught_even_though_apply_succeeded(repo: Path):
    """The old code dropped the stash on `git stash apply` rc 0. Content is what decides."""
    _dirty(repo)
    stash_ref = _stash(repo)
    assert _run(repo, "stash", "apply", stash_ref).returncode == 0
    # Simulate a restore that applied the patch elsewhere and left this file at HEAD content.
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")

    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)

    assert verified is False
    assert "tracked.txt" in detail
    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is False
    assert stash_ref in _stashes(repo)


# --- conflicts ------------------------------------------------------------------------------

def test_conflicting_apply_parks_and_keeps_the_pin(repo: Path, capsys):
    _dirty(repo)
    stash_ref = _stash(repo)
    # Upstream moves the same line the user touched: applying the stash cannot be clean.
    (repo / "tracked.txt").write_text("base\nupstream edit\n", encoding="utf-8")
    _run(repo, "commit", "-qam", "upstream")

    assert stash_mod._apply_stash(GIT, repo, stash_ref) is None

    out = capsys.readouterr().out
    assert "Durable ref" in out
    assert stash_ref in _stashes(repo)
    assert len(_localwork_refs(repo)) == 1
    assert stash_mod._pending_autostash is not None


# --- explicit discard -----------------------------------------------------------------------

def test_discard_retires_the_pin_only_when_the_drop_succeeded(repo: Path):
    _dirty(repo)
    stash_ref = _stash(repo)

    assert stash_mod._discard_stashed_changes(GIT, repo, stash_ref) is True

    assert _stashes(repo) == ""
    assert _localwork_refs(repo) == []
    assert stash_mod._pending_autostash_ref is None
