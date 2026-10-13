"""PR attribution must not include authors already accepted on its target branch."""

import os
import subprocess

from scripts import audit_pr_attribution as audit


def _git(repo, *args, author="fixture@example.test"):
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL=author,
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL=author,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True,
        text=True, check=True,
    )
    return result.stdout.strip()


def test_pr_base_excludes_its_accepted_authors(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "repair-base")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "accepted", author="accepted@example.test")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "fix")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "new", author="new@example.test")
    _git(repo, "update-ref", "refs/remotes/origin/main", main)
    _git(repo, "update-ref", "refs/remotes/origin/repair-base", base)
    monkeypatch.setattr(audit, "REPO_ROOT", repo)

    assert audit.new_emails("repair-base") == ["new@example.test"]
    assert audit.new_emails("main") == ["accepted@example.test", "new@example.test"]
