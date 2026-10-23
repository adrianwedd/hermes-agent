#!/usr/bin/env python3
"""Run an ad hoc Kanban check in a fresh temporary home, never in a worker's board.

Usage: python scripts/run_kanban_probe.py -- <command> [arguments...]
The command must finish its own children before returning. No detached probe mode.
"""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile


def probe_environment(root: Path) -> dict[str, str]:
    """Allowlist process essentials; discard all inherited worker and board pins."""
    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "TZ",
    ) if key in os.environ}
    home = root / "home"
    home.mkdir()
    state = root / "hermes"
    state.mkdir()
    env.update(
        HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state),
        HERMES_KANBAN_HOME=str(state), HERMES_KANBAN_DB=str(state / "kanban.db"),
        HERMES_KANBAN_BOARD="default", HERMES_KANBAN_WORKSPACES_ROOT=str(root / "workspaces"),
        HERMES_KANBAN_LOGS_ROOT=str(root / "logs"), HERMES_DISABLE_LAZY_INSTALLS="1",
    )
    (state / "config.yaml").write_text(json.dumps({"kanban": {
        "board_creation_allowlist": [], "dispatch_in_gateway": False,
        "auto_decompose": False,
    }}), encoding="utf-8")
    return env


PROBE_TIMEOUT_SECONDS = 600


def run_probe(command: list[str], root: Path) -> int:
    """Reap the probe and terminate its POSIX process group before removing its home."""
    process = subprocess.Popen(command, env=probe_environment(root), cwd=root,
                               start_new_session=(os.name == "posix"))
    try:
        return process.wait(timeout=PROBE_TIMEOUT_SECONDS)
    finally:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok -- POSIX branch only
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise SystemExit("usage: run_kanban_probe.py -- <command> [arguments...]")
    with tempfile.TemporaryDirectory(prefix="hermes-kanban-probe-") as temporary:
        root = Path(temporary).resolve()
        return run_probe(command, root)


if __name__ == "__main__":
    raise SystemExit(main())
