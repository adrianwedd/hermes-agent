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

def test_integrated_restore_preserves_both_sides(repo: Path):
    """A clean three-way merge is success, not a lost local change.

    Upstream and the user both edit the same file in different places; `git stash apply` merges them.
    The result matches neither the stashed blob nor HEAD, and byte equality alone would call this a
    failed restore. What must be demonstrated is that the user's change is still in there.
    """
    (repo / "tracked.txt").write_text("base\nlocal\n", encoding="utf-8")
    stash_ref = _stash(repo)
    (repo / "tracked.txt").write_text("upstream\nbase\n", encoding="utf-8")
    _run(repo, "commit", "-qam", "upstream")
    assert _run(repo, "stash", "apply", stash_ref).returncode == 0

    assert (repo / "tracked.txt").read_text() == "upstream\nbase\nlocal\n"
    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)
    assert verified, detail  # exact-restore comparison would have refused here

    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is True
    assert _stashes(repo) == ""
    assert _localwork_refs(repo) == []


def test_integration_that_drops_the_local_change_still_fails(repo: Path, monkeypatch):
    """The integration carve-out must not become a blank cheque: losing the hunk is still a failure."""
    (repo / "tracked.txt").write_text("base\nlocal\n", encoding="utf-8")
    stash_ref = _stash(repo)
    (repo / "tracked.txt").write_text("upstream\nbase\n", encoding="utf-8")
    _run(repo, "commit", "-qam", "upstream")
    _run(repo, "stash", "apply", stash_ref)
    # Apply the upstream content only: the local line is gone, exactly the failure to catch.
    (repo / "tracked.txt").write_text("upstream\nbase\n", encoding="utf-8")

    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)

    assert verified is False
    assert "tracked.txt" in detail
    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is False
    assert stash_ref in _stashes(repo)


def test_file_mode_change_survives_verification(repo: Path):
    """The exec bit is part of the saved change, not decoration."""
    (repo / "tracked.txt").write_text("base\nlocal\n", encoding="utf-8")
    (repo / "tracked.txt").chmod(0o755)
    stash_ref = _stash(repo)
    assert _run(repo, "stash", "apply", stash_ref).returncode == 0

    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)
    assert verified, detail

    (repo / "tracked.txt").chmod(0o644)  # the mode was dropped
    verified, detail = stash_mod._worktree_matches_stash(GIT, repo, stash_ref)
    assert verified is False and "file mode" in detail


def test_pin_retains_the_full_stash_object_graph(repo: Path):
    """The pin must survive `git stash clear`: tracked state, index parent and untracked parent."""
    _dirty(repo)
    stash_ref = _stash(repo)
    pin = stash_mod._pending_autostash_ref
    assert pin

    assert _run(repo, "stash", "clear").returncode == 0
    assert _stashes(repo) == ""  # the reflog entry is gone

    assert _run(repo, "rev-parse", pin).stdout.strip() == stash_ref
    assert _run(repo, "rev-parse", f"{pin}^3").returncode == 0  # untracked parent retained
    assert _run(repo, "show", f"{pin}^3:notes.md").stdout == "untracked work\n"
    assert _run(repo, "rev-parse", f"{pin}^2").returncode == 0  # index parent retained


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


# --- discovery of unresolved pins ------------------------------------------------------------

def test_pin_discovery_reports_provenance_and_stash_availability(repo: Path):
    _dirty(repo)
    stash_ref = _stash(repo)
    pin = stash_mod._pending_autostash_ref
    assert pin

    pins = stash_mod.discover_local_work_pins(GIT, repo)
    assert len(pins) == 1
    entry = pins[0]
    assert entry["ref"] == pin
    assert entry["commit"] == stash_ref
    assert entry["stash_entry"] is True
    assert entry["date"]
    assert entry["recover_with"] == f"git stash show -p {pin} | git apply --3way"


def test_pin_discovery_flags_the_only_remaining_copy(repo: Path, capsys):
    _dirty(repo)
    _stash(repo)
    _run(repo, "stash", "clear")  # the reflog entry is gone; the pin is all that is left

    pins = stash_mod.discover_local_work_pins(GIT, repo)
    assert len(pins) == 1 and pins[0]["stash_entry"] is False

    assert stash_mod.report_local_work_pins(GIT, repo) == 1
    out = capsys.readouterr().out
    assert "NO stash entry left" in out
    assert "never expires them" in out


def test_pin_discovery_is_empty_once_the_work_is_settled(repo: Path):
    _dirty(repo)
    stash_ref = _stash(repo)
    _run(repo, "stash", "apply", stash_ref)

    assert stash_mod._verify_and_settle_restore(GIT, repo, stash_ref) is True
    assert stash_mod.discover_local_work_pins(GIT, repo) == []
    assert stash_mod.report_local_work_pins(GIT, repo) == 0


def test_pin_discovery_never_drops_anything(repo: Path):
    """Discovery is a report: the pin and the stash entry both survive it."""
    _dirty(repo)
    stash_ref = _stash(repo)
    pin = stash_mod._pending_autostash_ref

    stash_mod.report_local_work_pins(GIT, repo)

    assert _run(repo, "rev-parse", pin).stdout.strip() == stash_ref
    assert stash_ref in _stashes(repo)
