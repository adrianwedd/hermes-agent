"""Native argument capture and restart must preserve Python launcher programs."""

import json
import subprocess
import sys

import psutil
import pytest

from hermes_cli import main_dashboard
from tests.waiting import wait_until


@pytest.mark.platforms("macos")
def test_multiline_launcher_survives_enumeration_and_respawn(tmp_path, monkeypatch):
    receipt = tmp_path / "child.json"
    code = (
        "import json, os, pathlib, signal, sys\n"
        f"pathlib.Path({str(receipt)!r}).write_text(json.dumps([os.getpid(), sys.argv]))\n"
        "signal.pause()\n"
    )
    argv = [sys.executable, "-I", "-c", code, "argument with spaces", "line\nbreak"]
    child = subprocess.Popen(argv)
    replacement = None
    try:
        wait_until(receipt.exists, timeout=10)
        original_receipt = json.loads(receipt.read_text())
        captured = main_dashboard._dashboard_cmdline_for_pid(child.pid)
        assert captured == argv
        child.terminate()
        child.wait(timeout=10)
        receipt.unlink()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        assert main_dashboard._respawn_dashboard_processes([captured]) == []
        wait_until(receipt.exists, timeout=10)
        pid, relaunched_args = json.loads(receipt.read_text())
        replacement = psutil.Process(pid)
        assert pid != original_receipt[0]
        assert relaunched_args == original_receipt[1]
        assert main_dashboard._dashboard_cmdline_for_pid(pid) == argv
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        if replacement is None and receipt.exists():
            pid = json.loads(receipt.read_text())[0]
            if psutil.pid_exists(pid) and pid != child.pid:
                replacement = psutil.Process(pid)
        if replacement is not None:
            replacement.terminate()
            replacement.wait(timeout=10)


@pytest.mark.platforms("macos")
def test_unreadable_native_arguments_do_not_fall_back_to_display_text(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, "Process", denied)
    assert main_dashboard._dashboard_cmdline_for_pid(123) is None
