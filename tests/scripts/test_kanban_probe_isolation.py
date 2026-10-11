"""Worker environment pins must never reach ad hoc acceptance checks."""
import json
from pathlib import Path
import sys

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
