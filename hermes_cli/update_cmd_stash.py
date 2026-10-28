"""Autostash handling for ``hermes update``: stash before the pull, restore/park/discard afterwards, warn about orphans.

Split out of ``update_cmd.py``; names are re-imported there so ``hermes_cli.update_cmd.<name>`` still resolves/monkeypatches.
Origin helpers are imported lazily per function (no cycle; test patches on the origin stay effective).
"""

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Optional

from hermes_cli.update_cmd_common import _record_stop

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.update_cmd")

#: Autostash subject contract: this prefix + UTC YYYYMMDD-HHMMSS stamp
#: (producer _stash_local_changes_if_needed, consumer _warn_orphaned_update_autostashes).
_AUTOSTASH_NAME_PREFIX = "hermes-update-autostash-"

#: Age past which a leftover autostash is called out. Younger entries are normal
#: (recent --keep-stash park); older ones are almost always forgotten.
# Entries younger than this are normal (a parked stash from : the desktop updater's --keep-stash run minutes
# ago); older ones are almost : always forgotten (#63717 problem 6: an orphan persisted 9+ days unnoticed).
_AUTOSTASH_WARN_AGE_DAYS = 7

_STASH_LEFT_IN_PLACE = "  The stash was left in place. You can remove it manually after checking the result."

#: This run's autostash until it is settled: (stash ref, stashed path count). Set when the
#: update stashes local patches; cleared when they are restored, discarded or parked because
#: the user asked for it, or once a failure verdict already named them. While it is set, no
#: outcome may claim the update completed (#122557).
_pending_autostash: Optional[tuple[str, int]] = None

#: Reflog-independent reference to the same saved work (``refs/hermes-localwork/…``).
#: Created at stash time, dropped only after the restore is verified or the work is explicitly
#: discarded: a stash entry can be lost to a reflog expiry or a mistaken drop, this cannot.
_pending_autostash_ref: Optional[str] = None


def _git_quiet(git_cmd: list[str], args: list[str], cwd: Path, **kwargs):
    """``subprocess.run`` of a git command with captured output; None when git cannot run."""
    from hermes_cli.update_custody import run_git

    try:
        return run_git(git_cmd, args, cwd=cwd, capture_output=True, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def _intent_to_add_paths(porcelain_z: str) -> tuple[str, ...]:
    """Paths recorded with ``git add -N`` (intent-to-add), read from NUL-separated porcelain output.

    Such an entry carries the empty blob with zeroed stat data, so it is never "uptodate" and
    ``git stash push`` rejects the whole stash with ``Entry 'X' not uptodate. Cannot merge.`` - a dead
    end for the user, because that is the state an editor leaves behind when it shows a new file in
    diffs. Porcelain marks it ``" A"`` (in the worktree, absent from the index); a real staged add is
    ``"A "`` and an untracked file ``"??"``. Rename records carry a second, prefix-less field, which
    the leading ``" A"`` test cannot match.
    """
    return tuple(
        record[3:] for record in porcelain_z.split("\0")
        if len(record) > 3 and record[0] == " " and record[1] == "A"
    )


def _git_paths_z(git_cmd: list[str], args: list[str], cwd: Path):
    """NUL-separated path listing as a set, or None when git failed (surrogateescape keeps odd filenames)."""
    result = _git_quiet(git_cmd, args, cwd, text=True, encoding="utf-8", errors="surrogateescape")
    if result is None or result.returncode != 0:
        return None
    return {path for path in result.stdout.split("\0") if path}


def _reset_hard(git_cmd: list[str], cwd: Path) -> None:
    from hermes_cli.update_custody import run_git

    run_git(git_cmd, ["reset", "--hard", "HEAD"], cwd=cwd, capture_output=True)


def _print_nonempty(text: str, prefix: str = "") -> None:
    if text.strip():
        print(f"{prefix}{text.strip()}")


def _print_first_line(text: str) -> None:
    if text.strip():
        print(f"  {text.strip().splitlines()[0]}")


def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path, *, checkout_move=None) -> Optional[str]:
    """Stash local changes before the update moves the checkout; the stash commit, or None when clean.
    ``checkout_move`` binds the push (and its cleanup reset) to the paused gateways' tree gate."""
    global _pending_autostash
    from hermes_cli.update_cmd_git import _git_run, _no_move
    _pending_autostash = None
    status = _git_run(git_cmd, ["status", "--porcelain", "-z"], cwd, check=True)
    if not status.stdout.strip():
        return None
    # Unmerged index entries (interrupted merge/rebase) make `git stash` fail with
    # "needs merge"; `git reset` drops only the index conflict state, not the tree.
    if _git_run(git_cmd, ["ls-files", "--unmerged"], cwd).stdout.strip():
        print("→ Clearing unmerged index entries from a previous conflict...")
        _git_quiet(git_cmd, ["reset"], cwd)

    # Intent-to-add entries are the other index state `git stash push` refuses (see
    # _intent_to_add_paths). Promoting them to real staged adds keeps their content and is lossless
    # for the user's work; after the restore they come back as staged additions, which is the closest
    # `git stash` can represent.
    intent_to_add = _intent_to_add_paths(status.stdout)
    if intent_to_add:
        print(f"→ Making {len(intent_to_add)} intent-to-add file(s) stashable...")
        add = _git_run(git_cmd, ["add", "--", *intent_to_add], cwd)
        if add.returncode != 0:
            _print_nonempty(add.stderr)

    stash_name = datetime.now(UTC).strftime(f"{_AUTOSTASH_NAME_PREFIX}%Y%m%d-%H%M%S")
    print("→ Local changes detected — stashing before update...")
    prev_stash = _git_run(git_cmd, ["rev-parse", "--verify", "refs/stash"], cwd).stdout.strip()
    with (checkout_move or _no_move)(revert=True):  # push and reset put every tracked change back to HEAD
        stash_ref = _push_stash(git_cmd, cwd, stash_name, prev_stash)
    _pending_autostash = (
        stash_ref, len([entry for entry in status.stdout.split("\0") if entry]))
    # A stash entry alone is not a recovery point: `refs/stash` is a reflog that expires, and dropping
    # an entry is one mistaken command away. Pin the same commit under a ref that nothing expires.
    global _pending_autostash_ref
    _branch = _git_run(git_cmd, ["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()
    _pending_autostash_ref = _create_local_work_ref(git_cmd, cwd, stash_ref, _branch)
    if _pending_autostash_ref:
        print(f"→ Saved local work is pinned at {_pending_autostash_ref}")
    return stash_ref


def _push_stash(git_cmd: list[str], cwd: Path, stash_name: str, prev_stash: str) -> str:
    """``git stash push``; the stash commit. Raises when nothing was saved; resets tracked leftovers
    when a push saved everything but could not clean up."""
    from hermes_cli.update_cmd_git import _git_run
    push = _git_run(git_cmd, ["stash", "push", "--include-untracked", "-m", stash_name], cwd)
    _print_nonempty(push.stdout)
    stash_probe = _git_run(git_cmd, ["rev-parse", "--verify", "refs/stash"], cwd)
    stash_ref = stash_probe.stdout.strip()
    stash_created = stash_probe.returncode == 0 and bool(stash_ref) and stash_ref != prev_stash
    if push.returncode != 0:
        if not stash_created:
            # No entry created: changes NOT saved — bail before touching HEAD.
            print("✗ Could not stash local changes — update aborted.")
            _print_first_line(push.stderr)
            print("  Commit, stash, or clean up your local changes manually, then re-run `hermes update`.")
            print("  (An index entry from `git add -N` is the usual cause of this error; `git add` the")
            print("   paths it names, or `git reset` them, and the update will proceed.)")
            raise subprocess.CalledProcessError(push.returncode, push.args, output=push.stdout, stderr=push.stderr)
        # Non-zero but entry created: push saved everything yet couldn't delete some untracked files
        # (e.g. root-owned dir). Not a failure — continue.
        _print_nonempty(push.stderr)
        print("  ⚠ Some untracked files could not be removed from the working tree (permission denied).")
        print("    They were still saved to the stash and were left in place — the update will continue.")
        # A partially-failed push also skips cleanup of TRACKED modifications; they'd break the following
        # pull. Safe to reset: all is in the stash.
        _reset_hard(git_cmd, cwd)
    return stash_ref


def _create_local_work_ref(git_cmd: list[str], cwd: Path, stash_ref: str, branch: str) -> Optional[str]:
    """Pin *stash_ref* under ``refs/hermes-localwork/…``; the refname, or None.

    Nothing expires this ref (the orphan/divergence rescue refs under the same namespace do, by
    design), so it survives a reflog expiry, a ``git stash clear`` and a drop by index. It is
    removed only when the work is back in the tree and verified, or deliberately discarded.
    """
    from hermes_cli.update_cmd_git import _git_run
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", branch or "detached")
    ref = f"refs/hermes-localwork/{safe_branch}-{stamp}-{stash_ref[:12]}"
    created = _git_run(git_cmd, ["update-ref", ref, stash_ref], cwd)
    return ref if created.returncode == 0 else None


def _drop_local_work_ref(git_cmd: list[str], cwd: Path, ref: Optional[str]) -> None:
    """Retire a ``localwork`` pin. Only ever after the work is verified back in the tree."""
    global _pending_autostash_ref
    from hermes_cli.update_cmd_git import _git_run
    if ref:
        _git_run(git_cmd, ["update-ref", "-d", ref], cwd)
    if ref and ref == _pending_autostash_ref:
        _pending_autostash_ref = None


def _restore_preserves_local_change(git_cmd: list[str], cwd: Path, stash_ref: str, path: str, target: Path) -> bool:
    """Does *target* hold this file's local change, integrated onto the updated code?

    A stash applied onto newer code is a three-way merge: git legitimately produces a file that
    contains both the user's edit and the upstream one, whose bytes match neither the stashed blob
    nor HEAD. Treating that as a failed restore would refuse updates that worked. So re-derive the
    merge from the stash's own parents — base ``stash^1``, ours ``stash``, theirs ``HEAD`` — and
    accept the file only when it is exactly that integration. A dropped hunk still differs.
    """
    import tempfile
    from hermes_cli.update_cmd_git import _git_run

    def blob(spec: str) -> Optional[str]:
        result = _git_run(git_cmd, ["show", spec], cwd)
        return result.stdout if result.returncode == 0 else None

    if not target.is_file():
        return False
    base, ours, theirs = blob(f"{stash_ref}^1:{path}"), blob(f"{stash_ref}:{path}"), blob(f"HEAD:{path}")
    if base is None or ours is None or theirs is None:
        return False
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # binary or unreadable: compare bytes only, and bytes already differed
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            candidate = Path(tmp) / name
            candidate.write_text(content, encoding="utf-8")
            paths.append(str(candidate))
        merged = _git_run(git_cmd, ["merge-file", "-p", *paths], cwd)
    if merged.returncode != 0:
        return False  # conflict markers: this is not an integration of the user's change
    return merged.stdout == current


def _worktree_matches_stash(
    git_cmd: list[str], cwd: Path, stash_ref: str,
    preexisting_untracked: set[str] | None = None,
) -> tuple[bool, str]:
    """Is the saved work actually back in the tree? ``(ok, first-mismatch detail)``.

    Compares content, not git's exit code: every tracked path the stash changed must hold the
    stashed bytes again (a deletion must still be absent), and every stashed untracked path must be
    present with the stashed bytes. A merge that reapplied the patch onto different code can still
    exit 0 while silently dropping a hunk; this is what catches that.
    """
    from hermes_cli.update_cmd_git import _git_run

    def sha(*args: str) -> Optional[str]:
        result = _git_run(git_cmd, list(args), cwd)
        return result.stdout.strip() if result.returncode == 0 else None

    changed = _git_run(git_cmd, ["diff", "--name-status", f"{stash_ref}^1", stash_ref], cwd)
    if changed.returncode != 0:
        return False, "could not read the stashed change set"
    for line in changed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][:1], parts[-1]
        target = cwd / path
        if status == "D":
            if target.exists():
                return False, f"{path} should still be deleted"
            continue
        expected = sha("rev-parse", f"{stash_ref}:{path}")
        if expected is None:
            return False, f"could not read the stashed copy of {path}"
        actual = sha("hash-object", "--", path) if target.is_file() else None
        if actual != expected and not _restore_preserves_local_change(
                git_cmd, cwd, stash_ref, path, target):
            return False, f"{path} does not hold the stashed change"
        # The exec bit is part of the change too (scripts, launchers).
        stashed_mode = sha("ls-tree", stash_ref, "--", path) or ""
        base_mode = sha("ls-tree", f"{stash_ref}^1", "--", path) or ""
        if stashed_mode[:6] != base_mode[:6] and target.exists():
            wants_exec = stashed_mode.startswith("100755")
            if os.access(target, os.X_OK) != wants_exec:
                return False, f"{path} lost its file mode"

    untracked = _git_run(git_cmd, ["ls-tree", "-r", "--name-only", f"{stash_ref}^3"], cwd)
    if untracked.returncode == 0:
        for path in untracked.stdout.splitlines():
            if not path.strip() or path in (preexisting_untracked or ()):
                continue
            target = cwd / path
            expected = sha("rev-parse", f"{stash_ref}^3:{path}")
            actual = sha("hash-object", "--", path) if target.is_file() else None
            if actual is None:
                return False, f"stashed untracked file {path} is missing"
            if expected is not None and actual != expected:
                return False, f"stashed untracked file {path} differs"
    return True, ""


def _verify_and_settle_restore(
    git_cmd: list[str], cwd: Path, stash_ref: str,
    preexisting_untracked: set[str] | None = None,
) -> bool:
    """Drop the stash only once the tree provably holds the saved work again.

    Returns True when the restore is verified and the stash and its ``localwork`` pin are retired.
    On any mismatch the stash, its pin and this run's unsettled verdict all stay: the caller must
    not report success while the user's work is somewhere other than the tree.
    """
    verified, detail = _worktree_matches_stash(git_cmd, cwd, stash_ref, preexisting_untracked)
    if not verified:
        print("✗ Local changes were applied, but the restored tree does not match what was stashed.")
        print(f"  {detail}")
        print("  The stash was NOT dropped, and the saved work stays pinned:")
        print(f"  Stash ref:  {stash_ref}")
        if _pending_autostash_ref:
            print(f"  Durable ref: {_pending_autostash_ref}")
            print(f"  Recover with: git stash show -p {_pending_autostash_ref} | git apply --3way")
        _record_stash_disposition("parked", stash_ref, f"restore could not be verified: {detail}")
        return False
    _drop_restored_stash(git_cmd, cwd, stash_ref)
    _drop_local_work_ref(git_cmd, cwd, _pending_autostash_ref)
    _record_stash_disposition("restored", stash_ref)
    return True


def _resolve_stash_selector(git_cmd: list[str], cwd: Path, stash_ref: str) -> Optional[str]:
    """Selector for the stash entry whose commit is *stash_ref*, as the bare index ``N``
    (git accepts it wherever ``stash@{N}`` is valid). Never ``stash@{N}`` itself: on native
    Windows the MSYS runtime strips the braces from git.exe's argv, so ``stash@{0}`` reaches git
    as ``stash@0`` and the drop fails (#87542)."""
    from hermes_cli.update_cmd_git import _git_run
    stash_list = _git_run(git_cmd, ["stash", "list", "--format=%gd %H"], cwd, check=True)
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(" ")
        if commit.strip() == stash_ref:
            match = re.fullmatch(r"stash@\{(\d+)\}", selector.strip())
            return match.group(1) if match else selector.strip()
    return None


def _clear_pending_autostash() -> None:
    """Forget this run's unsettled autostash after it was already reported (#122557)."""
    global _pending_autostash
    _pending_autostash = None


def _unrestored_autostash_notice() -> Optional[str]:
    """What to tell the user while this run's autostash is unsettled (#122557), else ``None``.

    Used as the completion line (it is not a success line, so the update ends partial and
    exits non-zero) and printed by failure verdicts, which otherwise never name the stash.
    """
    if _pending_autostash is None:
        return None
    stash_ref, file_count = _pending_autostash
    return (f"⚠ hermes update stashed {file_count} local modification(s) and did NOT restore them.\n"
            f"  Stash ref: {stash_ref}\n"
            f"  Review with: git stash show --stat {stash_ref}\n"
            f"  Re-apply with: git stash show -p {stash_ref} | git apply --3way")


#: Namespace for local-work pins (``refs/hermes-localwork/<branch>-<stamp>-<sha12>``). Nothing expires
#: them, which is exactly why they have to be discoverable rather than silently accumulating.
_PIN_REF_PREFIX = "refs/hermes-localwork/"


def discover_local_work_pins(git_cmd: list[str], cwd: Path) -> list[dict[str, object]]:
    """Every unresolved local-work pin: what it holds, when, and whether a stash entry still does.

    Discovery only — nothing is dropped, expired or rewritten. A pin with no matching stash entry is
    the only remaining copy of that work, so it is reported as such rather than tidied away.
    """
    from hermes_cli.update_cmd_git import _git_run
    refs = _git_run(
        git_cmd,
        ["for-each-ref", "--format=%(refname)%09%(objectname)%09%(committerdate:iso-strict)",
         _PIN_REF_PREFIX],
        cwd,
    )
    if refs.returncode != 0:
        return []
    stashes = _git_run(git_cmd, ["stash", "list", "--format=%H"], cwd)
    held = set((stashes.stdout or "").split()) if stashes.returncode == 0 else set()
    pins: list[dict[str, object]] = []
    for line in (refs.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ref, commit, when = parts[0], parts[1], parts[2]
        pins.append({
            "ref": ref,
            "commit": commit,
            "date": when,
            "stash_entry": commit in held,
            "recover_with": f"git stash show -p {ref} | git apply --3way",
        })
    return pins


def report_local_work_pins(git_cmd: list[str], cwd: Path) -> int:
    """Print unresolved pins (count returned). Called wherever stale autostashes are surfaced."""
    try:
        pins = discover_local_work_pins(git_cmd, cwd)
    except Exception as exc:  # discovery must never break an update
        logger.debug("Local-work pin discovery failed: %s", exc)
        return 0
    if not pins:
        return 0
    only_copy = [pin for pin in pins if not pin["stash_entry"]]
    print(f"⚠ {len(pins)} saved local-work reference(s) from earlier updates are still unresolved:")
    for pin in pins:
        state = "stash entry present" if pin["stash_entry"] else "NO stash entry left — this ref is the only copy"
        print(f"    {pin['ref']}  ({pin['date']}, {state})")
        print(f"      {pin['recover_with']}")
    if only_copy:
        print("  Restore or discard these deliberately; Hermes never expires them.")
    return len(pins)


def _warn_orphaned_update_autostashes(git_cmd: list[str], cwd: Path) -> int:
    """Print a notice for update autostashes older than the warn threshold; return the count (0 on any git failure).

    Autostashes legitimately outlive a run (--keep-stash, failed restore) but nothing re-surfaces them.
    Deliberately NOT a GC: a stash may be the only copy of the user's work, so Hermes never drops one.

    Autostash entries legitimately outlive an update run (``--keep-stash`` parks them; a conflicted or
    failed restore preserves them for safety), but nothing ever re-surfaces them afterwards — they sit in
    ``git stash`` invisibly for weeks (#63717 problem 6). This prints a short notice naming the stale
    entries with recovery/cleanup guidance.
    """
    from hermes_cli.update_cmd_git import _git_run
    try:
        stash_list = _git_run(git_cmd, ["stash", "list", "--format=%gd %s"], cwd)
        if stash_list.returncode != 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=_AUTOSTASH_WARN_AGE_DAYS)
        stale: list[tuple[str, str]] = []
        for line in stash_list.stdout.splitlines():
            selector, _, subject = line.strip().partition(" ")
            pos = subject.find(_AUTOSTASH_NAME_PREFIX)
            if pos < 0:
                continue
            stamp = subject[pos + len(_AUTOSTASH_NAME_PREFIX):][:15]  # "YYYYMMDD-HHMMSS"
            try:
                stash_time = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue  # age unknown — leave it alone rather than guess
            if stash_time < cutoff:
                stale.append((selector, stamp))
        if not stale:
            return 0
        print()
        print(
            f"⚠ {len(stale)} leftover update autostash entr"
            f"{'y is' if len(stale) == 1 else 'ies are'} more than "
            f"{_AUTOSTASH_WARN_AGE_DAYS} days old:"
        )
        for selector, stamp in stale:
            print(f"    {selector}  ({_AUTOSTASH_NAME_PREFIX}{stamp})")
        print("  These hold local changes stashed by earlier updates and never")
        print("  restored. Review with: git stash show -p <entry>")
        print("  Restore with: git stash apply <entry>   Discard with: git stash drop <entry>")
        return len(stale)
    except Exception as exc:
        logger.debug("Autostash age check failed: %s", exc)
        return 0


def _record_stash_disposition(outcome: str, stash_ref: str, detail: str = "", *, chosen: bool = False) -> None:
    """Note the autostash disposition in the update receipt so a parked stash is visible
    to automation reading receipts instead of stdout (#115363: an update that ended with
    local changes parked in the stash reported a bare success with no trace of them).
    A park the user did not ask for (``chosen``) keeps the stash unsettled (#122557)."""
    global _pending_autostash
    if outcome != "parked" or chosen:
        _pending_autostash = None
    from hermes_cli.update_receipt import record_step
    record_step(
        "local_changes_stash",
        outcome != "parked",
        f"{outcome}: {stash_ref}" + (f" ({detail})" if detail else ""),
    )


def _print_stash_cleanup_guidance(stash_ref: str, stash_selector: Optional[str] = None) -> None:
    print("  Check `git status` first so you don't accidentally reapply the same change twice.")
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f"  Remove it with: git stash drop {stash_selector}")
    else:
        print(f"  Look for commit {stash_ref}, then drop it by index with: git stash drop <N>")


def _stash_apply_failed_only_on_existing_untracked(stderr: str) -> bool:
    """True when a ``git stash apply`` failure is ONLY about untracked files that already exist in the tree.

    Tail of the permission-denied class: push swept undeletable files into the stash but couldn't remove them;
    apply restores tracked changes, then refuses to overwrite those files and exits non-zero though nothing was lost.
    Any other error line (e.g. ``would be overwritten by merge``) means the tracked apply failed -> False.
    """
    saw_untracked_error = False
    for ln in (ln.strip() for ln in (stderr or "").splitlines() if ln.strip()):
        if "already exists, no checkout" in ln or "could not restore untracked files from stash" in ln:
            saw_untracked_error = True
        elif not ln.startswith(("warning:", "hint:")):
            return False
    return saw_untracked_error


def _untracked_files_replaced_by_update(git_cmd: list[str], cwd: Path, stash_ref: str, stderr: str) -> list[str]:
    """Paths ``git stash apply`` refused ("<path> already exists, no checkout") because the update
    now tracks its own file there (``HEAD:<path>``) and the tree does not hold the stash's untracked
    copy (``<stash>^3``). An untracked occupant HEAD does not track is the #70127 file that could
    not be deleted at stash time, whatever its content now; it is never the update's."""
    from hermes_cli.update_custody import run_git

    replaced = []
    suffix = " already exists, no checkout"
    for ln in (stderr or "").splitlines():
        ln = ln.strip()
        if not ln.endswith(suffix):
            continue
        rel = ln[: -len(suffix)]
        tracked = run_git(git_cmd, ["cat-file", "-e", f"HEAD:{rel}"], cwd=cwd, capture_output=True, check=False)
        if tracked.returncode != 0:
            continue
        stashed = run_git(git_cmd, ["show", f"{stash_ref}^3:{rel}"], cwd=cwd, capture_output=True, check=False)
        try:
            current = (Path(cwd) / rel).read_bytes()
        except OSError:
            current = None
        if stashed.returncode != 0 or current != stashed.stdout:
            replaced.append(rel)
    return replaced


def _park_stashed_changes(stash_ref: str) -> None:
    """Leave a pre-update autostash parked (``--keep-stash``, the desktop updater's mode): local source
    edits must never be silently re-applied onto updated code; the entry stays in ``git stash``."""
    print()
    print("ℹ️  Local changes were stashed before updating and were NOT re-applied (--keep-stash).")
    print(f"  Stash ref: {stash_ref}")
    print(f"  Restore manually with: git stash apply {stash_ref}")
    _record_stash_disposition("parked", stash_ref, "--keep-stash", chosen=True)


def _git_untracked_paths(git_cmd: list[str], cwd: Path) -> set[str] | None:
    """Return untracked paths, or ``None`` when Git cannot enumerate them."""
    paths = _git_paths_z(git_cmd, ["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    if paths is None:
        print("  ⚠ Could not enumerate untracked files while validating the restored stash.")
    return paths


def _restored_python_paths(git_cmd: list[str], cwd: Path) -> tuple[str, ...] | None:
    """Restored ``.py`` paths changed from ``HEAD``; deliberately Python-only (entry scripts stay outside the health check)."""
    from hermes_cli.update_cmd import _git_untracked_paths
    paths = _git_paths_z(git_cmd, ["diff", "--name-only", "-z", "HEAD", "--", "*.py"], cwd)
    if paths is None:
        print("  ⚠ Could not enumerate tracked Python files restored from the stash.")
        return None
    untracked = _git_untracked_paths(git_cmd, cwd)
    if untracked is None:
        return None
    paths.update(path for path in untracked if path.endswith(".py"))
    return tuple(sorted(paths))


#: Closed allowlist of suffixes that no Python import can load or read during startup.
#: Everything else (.py, .so/.pyd/.dll/.dylib, .pyc, .pth, .egg-link, data files such as
#: .json/.yaml/.toml that modules read on import, extensionless files) keeps the import check.
_NON_RUNTIME_SUFFIXES = frozenset({
    ".md", ".mdx", ".rst", ".txt", ".adoc",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
})


def _restore_is_non_runtime(git_cmd: list[str], cwd: Path, stash_ref: str) -> bool:
    """True only when every path the stash restores is demonstrably unable to change an import.

    The set is read from the stash commit itself (worktree and index changes against its base,
    renames split into delete + add, plus its untracked tree), never from the working tree,
    which also holds what the clean import probe wrote. Each path must have an allowlisted
    suffix, must not be a symlink, and may only sit in a directory ``HEAD`` already tracks, so
    the restore cannot create a namespace-package directory. Any Git failure counts as
    runtime-affecting.
    """
    restored: set[str] = set()
    for side in (stash_ref, f"{stash_ref}^2"):  # worktree, index
        changed = _git_paths_z(
            git_cmd, ["diff", "--name-only", "--no-renames", "-z", f"{stash_ref}^1", side, "--"], cwd)
        if changed is None:
            return False
        restored |= changed
    has_untracked = _git_quiet(git_cmd, ["rev-parse", "-q", "--verify", f"{stash_ref}^3"], cwd)
    if has_untracked is None:
        return False
    if has_untracked.returncode == 0:
        untracked = _git_paths_z(git_cmd, ["ls-tree", "-r", "--name-only", "-z", f"{stash_ref}^3"], cwd)
        if untracked is None:
            return False
        restored |= untracked
    if not restored:
        return True
    head_dirs = _git_paths_z(git_cmd, ["ls-tree", "-r", "-d", "--name-only", "-z", "HEAD"], cwd)
    if head_dirs is None:
        return False
    for path in restored:
        parent = Path(path).parent.as_posix()
        if (Path(path).suffix.lower() not in _NON_RUNTIME_SUFFIXES or (cwd / path).is_symlink()
                or (parent != "." and parent not in head_dirs)):
            return False
    return True


def _reject_unsafe_stash_restore(
    git_cmd: list[str], cwd: Path, stash_ref: str, preexisting_untracked: set[str], failing_target: str,
    detail: str | None, *, checkout_move=None,
) -> None:
    """Restore the clean updated tree, preserve the stash, and abort the update."""
    from hermes_cli.update_cmd import _git_untracked_paths
    from hermes_cli.update_cmd_git import _no_move
    print()
    print("✗ Restored local changes made the Hermes agent unexecutable.")
    print(f"  Health check failed: {failing_target}")
    if detail:
        for line in str(detail).splitlines()[:6]:
            print(f"    {line}")

    def _ok(result) -> bool:
        return result is not None and result.returncode == 0

    current_untracked = _git_untracked_paths(git_cmd, cwd)
    restored_untracked = current_untracked - preexisting_untracked if current_untracked is not None else set()
    with (checkout_move or _no_move)(revert=True):
        reset = _git_quiet(git_cmd, ["reset", "--hard", "HEAD"], cwd)
        clean = _git_quiet(git_cmd, ["clean", "-fd", "--", *sorted(restored_untracked)], cwd) if restored_untracked else None
    cleanup_ok = current_untracked is not None and _ok(reset) and (not restored_untracked or _ok(clean))
    if cleanup_ok:
        cleanup_ok = _ok(_git_quiet(git_cmd, ["diff", "--quiet", "HEAD", "--"], cwd))
    if cleanup_ok:
        print("  The clean updated tree has been restored; the gateway was not restarted.")
    else:
        print("  ⚠ The clean updated tree could not be fully restored automatically.")
        print("    Inspect `git status` and run `git reset --hard HEAD` before retrying.")
    print("  Platform connectivity alone does not mean the agent can execute turns.")
    print(f"  Your local changes remain preserved in stash: {stash_ref}")
    print(f"  Inspect them with: git stash show --stat {stash_ref}")
    print(f"  Restore manually after fixing them: git stash apply {stash_ref}")
    _clear_pending_autostash()  # named right above, and the update fails
    _record_stop("stash_restore_rejected")
    raise SystemExit(1)


def _confirm_restore(stash_ref: str, input_fn) -> bool:
    """Interactive gate; a remote ``input_fn`` defaults to No (``[y/N]``), the local prompt to Yes."""
    remote_prompt = input_fn is not None
    prompt_suffix = "[y/N]" if remote_prompt else "[Y/n]"
    print()
    print("⚠ Local changes were stashed before updating.")
    print("  Restoring them may reapply local customizations onto the updated codebase.")
    print("  Review the result afterward if Hermes behaves unexpectedly.")
    print(f"Restore local changes now? {prompt_suffix}")
    if remote_prompt:
        response = input_fn(f"Restore local changes now? {prompt_suffix}", "n")
    else:
        try:
            response = input().strip().lower()
        except (EOFError, UnicodeDecodeError):
            response = "n"  # closed stdin/encoding error must not crash mid-restore
    if response in {"y", "yes"} or (not remote_prompt and response == ""):
        return True
    print("Skipped restoring local changes.")
    print("Your changes are still preserved in git stash.")
    print(f"Restore manually with: git stash apply {stash_ref}")
    return False


def _apply_stash(git_cmd: list[str], cwd: Path, stash_ref: str, checkout_move=None) -> list[str] | None:
    """``git stash apply``; the untracked paths the update replaced (empty on a full restore), or
    None (tree reset, stash kept) on conflicts or any failure other than the already-exists class.
    ``checkout_move`` binds the apply (and a conflict's reset) to the paused gateways' tree gate."""
    from hermes_cli.update_cmd_git import _no_move
    print("→ Restoring local changes...")
    with (checkout_move or _no_move)(stash_ref) as step:
        restore, conflicted_files = _apply_stash_step(git_cmd, cwd, stash_ref)
        # Tracked changes applied in full (the already-exists class refuses only untracked files):
        # the tree this changed is the restore's result, not a tear.
        step["whole"] = not conflicted_files and (
            restore.returncode == 0 or _stash_apply_failed_only_on_existing_untracked(restore.stderr))
    if step["whole"] and restore.returncode == 0:
        return []
    if step["whole"]:
        # Tracked changes applied; git refused to overwrite untracked files that already exist. That is
        # harmless only when the occupant IS the stashed copy (undeletable at stash time, #70127). When
        # the update added its own file at that path, the stash is the only copy of the user's file.
        replaced = _untracked_files_replaced_by_update(git_cmd, cwd, stash_ref, restore.stderr)
        if not replaced:
            print("  ⚠ Some stashed untracked files already exist in the working tree and were kept as-is.")
        return replaced
    print("✗ Update pulled new code, but restoring local changes hit conflicts.")
    _print_nonempty(restore.stdout)
    _print_nonempty(restore.stderr)
    if conflicted_files:
        print("\nConflicted files:")
        for f in conflicted_files.splitlines():
            print(f"  • {f}")
    print("\nYour stashed changes are preserved — nothing is lost.")
    print(f"  Stash ref: {stash_ref}")
    if _pending_autostash_ref:
        print(f"  Durable ref: {_pending_autostash_ref} (nothing expires this one)")
    print("Working tree reset to clean state.")
    print(f"Restore your changes later with: git stash apply {stash_ref}")
    _record_stash_disposition("parked", stash_ref, "restore hit conflicts")
    return None  # code update succeeded; cmd_update continues (deps, skills, gateway)


def _apply_stash_step(git_cmd: list[str], cwd: Path, stash_ref: str):
    """``git stash apply`` and its conflicted paths. Any failure but the already-exists class is
    reset to the clean tree here, inside the bound step: conflict markers make hermes unrunnable,
    and the changes stay in the stash."""
    from hermes_cli.update_cmd_git import _git_run
    restore = _git_run(git_cmd, ["stash", "apply", stash_ref], cwd)
    unmerged = _git_run(git_cmd, ["diff", "--name-only", "--diff-filter=U"], cwd)  # conflicts can exist even on rc 0
    conflicted_files = unmerged.stdout.strip()
    if conflicted_files or (restore.returncode != 0
                            and not _stash_apply_failed_only_on_existing_untracked(restore.stderr)):
        _reset_hard(git_cmd, cwd)
    return restore, conflicted_files


def _drop_restored_stash(git_cmd: list[str], cwd: Path, stash_ref: str) -> None:
    from hermes_cli.update_cmd_git import _git_run
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print("⚠ Local changes were restored, but Hermes couldn't find the stash entry to drop.")
        print(_STASH_LEFT_IN_PLACE)
        _print_stash_cleanup_guidance(stash_ref)
        return
    drop = _git_run(git_cmd, ["stash", "drop", stash_selector], cwd)
    if drop.returncode != 0:
        print("⚠ Local changes were restored, but Hermes couldn't drop the saved stash entry.")
        _print_nonempty(drop.stdout)
        _print_nonempty(drop.stderr)
        print(_STASH_LEFT_IN_PLACE)
        _print_stash_cleanup_guidance(stash_ref, stash_selector)


def _restore_stashed_changes(
    git_cmd: list[str], cwd: Path, stash_ref: str, prompt_user: bool = False, input_fn=None, *,
    checkout_move=None,
) -> bool:
    from hermes_cli.update_cmd import _critical_module_import_failures, _git_untracked_paths, _restored_python_paths, _validate_python_files_syntax
    if prompt_user and not _confirm_restore(stash_ref, input_fn):
        _record_stash_disposition("parked", stash_ref, "restore declined", chosen=True)
        return False
    preexisting_untracked = _git_untracked_paths(git_cmd, cwd)
    if preexisting_untracked is None:
        print("  The stash was not restored because its cleanup baseline is unknown.")
        print(f"  Restore manually with: git stash apply {stash_ref}")
        _record_stash_disposition("parked", stash_ref, "untracked baseline unknown")
        return False
    clean_import_failures = _critical_module_import_failures(cwd, report_runtime_errors=True)
    replaced = _apply_stash(git_cmd, cwd, stash_ref, checkout_move)
    if replaced is None:
        return False  # disposition already recorded inside _apply_stash

    def reject(failing_target: str, detail) -> None:
        _reject_unsafe_stash_restore(
            git_cmd, cwd, stash_ref, preexisting_untracked, failing_target, detail, checkout_move=checkout_move)

    restored_python = _restored_python_paths(git_cmd, cwd)
    if restored_python is None:
        reject("restored Python source discovery", "could not determine which restored Python files require validation")
    syntax_ok, failing_path, syntax_error = _validate_python_files_syntax(cwd, restored_python)
    if not syntax_ok:
        reject(failing_path or "restored Python source", syntax_error)
    # Probe outcomes also move with install state between the two runs (launch preparation,
    # dependency sync), so a restore that cannot affect an import (docs, images) must not be
    # rejected on that difference (#130101). Anything else, including a native extension or
    # bytecode that shadows a healthy module, is still compared.
    non_runtime = _restore_is_non_runtime(git_cmd, cwd, stash_ref)
    for module, error in (() if non_runtime else
                          _critical_module_import_failures(cwd, report_runtime_errors=True).items()):
        if clean_import_failures.get(module) != error:
            reject(f"agent import {module or 'unknown'}", error[1])
            break
    if replaced:
        # The restored tree is healthy, but dropping the stash would lose the user's copies.
        print(f"⚠ The update added {len(replaced)} file(s) where you had untracked files of the same name:")
        for path in replaced[:10]:
            print(f"    {path}")
        print("  The updated files are in place; your versions are kept in the stash, which was NOT dropped.")
        print(f"  Stash ref: {stash_ref}")
        print(f"  Recover a file with: git show {stash_ref}^3:<path> > <path>.mine")
        if _pending_autostash_ref:
            print(f"  Durable ref: {_pending_autostash_ref} (nothing expires this one)")
        _record_stash_disposition("parked", stash_ref, f"untracked files replaced by the update: {', '.join(replaced[:10])}")
        return False
    if not _verify_and_settle_restore(git_cmd, cwd, stash_ref, preexisting_untracked):
        return False
    print("⚠ Local changes were restored on top of the updated codebase.")
    print("  Review `git diff` / `git status` if Hermes behaves unexpectedly.")
    return True


def _discard_stashed_changes(git_cmd: list[str], cwd: Path, stash_ref: str) -> bool:
    """Drop a pre-update stash without applying (non-interactive ``updates.non_interactive_local_changes: discard``).

    Unlike reset --hard + clean -fd this touches only what was stashed; ignored paths are never affected.
    Returns True if dropped, False on git failure (stash left in place).
    """
    from hermes_cli.update_cmd_git import _git_run
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Configured to discard local changes on non-interactive update, "
            "but Hermes couldn't find the stash entry to drop."
        )
        _print_stash_cleanup_guidance(stash_ref)
        return False
    drop = _git_run(git_cmd, ["stash", "drop", stash_selector], cwd)
    if drop.returncode != 0:
        print("⚠ Configured to discard local changes, but Hermes couldn't drop the saved stash entry.")
        _print_first_line(drop.stderr)
        _print_stash_cleanup_guidance(stash_ref, stash_selector)
        _record_stash_disposition("parked", stash_ref, "configured discard failed")
        return False
    print("→ Discarded local source changes (updates.non_interactive_local_changes=discard).")
    _drop_local_work_ref(git_cmd, cwd, _pending_autostash_ref)
    _record_stash_disposition("discarded", stash_ref)
    return True
