"""Worker environment pins must never reach ad hoc acceptance checks."""
import json
from pathlib import Path
import sys
import subprocess
import time

import psutil
import pytest

from scripts import run_kanban_probe

from scripts.run_kanban_probe import main


def test_probe_scrubs_worker_pins_and_cleans_its_home(tmp_path, monkeypatch):
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("HERMES_KANBAN_HOME", "/production")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/production/kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_production")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "/production")
    source = Path(__file__).resolve().parents[2]
    command = (
        "import json,os,pathlib,sys; sys.path.insert(0,sys.argv[2]); "
        "from hermes_cli import kanban_db as kb; "
        "kb.init_db(); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ, "
        "resolved_board_home=str(kb.kanban_home()), resolved_db=str(kb.kanban_db_path()))))"
    )
    assert main([sys.executable, "-c", command, str(receipt), str(source)]) == 0
    env = json.loads(receipt.read_text())
    assert env["HERMES_KANBAN_HOME"] == env["HERMES_HOME"]
    assert Path(env["HERMES_KANBAN_DB"]).parent == Path(env["HERMES_HOME"])
    assert env["HERMES_KANBAN_BOARD"] == "default"
    assert env["resolved_board_home"] == env["HERMES_HOME"]
    assert env["resolved_db"] == env["HERMES_KANBAN_DB"]
    assert not any(key in env for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"))
    assert not Path(env["HERMES_HOME"]).exists()


@pytest.mark.platforms("not windows")
@pytest.mark.parametrize("timeout", [False, True])
def test_probe_terminates_background_children_before_home_cleanup(tmp_path, monkeypatch, timeout):
    receipt = tmp_path / "child.json"
    child_code = (
        "import json,os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'home':os.environ['HERMES_HOME']})); "
        "print('ready',flush=True); time.sleep(600)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],stdout=subprocess.PIPE,text=True); "
        "assert child.stdout.readline().strip()=='ready'; "
        "time.sleep(600 if sys.argv[3]=='timeout' else 0)"
    )
    monkeypatch.setattr(run_kanban_probe, "PROBE_TIMEOUT_SECONDS", 5)
    command = [sys.executable, "-c", parent_code, child_code, str(receipt), "timeout" if timeout else "exit"]
    try:
        if timeout:
            with pytest.raises(subprocess.TimeoutExpired):
                main(command)
        else:
            assert main(command) == 0
        saved = json.loads(receipt.read_text())
        assert not Path(saved["home"]).exists()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if psutil.Process(saved["pid"]).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.05)
        else:
            pytest.fail("probe child survived its parent's completion")
    finally:
        if receipt.exists():
            try:
                psutil.Process(json.loads(receipt.read_text())["pid"]).kill()
            except psutil.NoSuchProcess:
                pass
