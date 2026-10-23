"""Complete isolated native-worker fixtures shared by attachment/tool tests."""
import hashlib
import json
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def source_layout(monkeypatch):
    """Unit tests run from source, not the real parent sealed installation."""
    monkeypatch.setattr("pm.environments.payload_venv", lambda _root: None)


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    # A real dispatcher always pins the worker's run id; simulate that so the
    # run-lifecycle tools can prove ownership (see test_unbound_worker_cannot_mutate_card).
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid



def completion_metadata(conn, task_id):
    """Supply prior durable evidence so a judge test reaches its own gate."""
    try:
        from hermes_cli.kanban_completion_evidence import contract_record
    except ModuleNotFoundError as exc:
        if exc.name != 'hermes_cli.kanban_completion_evidence':
            raise
        return {}
    from hermes_cli import kanban_db as kb
    cid, contract = contract_record(conn, task_id)
    data=b'isolated unit verification output\n'
    aid=kb.store_attachment_bytes(conn,task_id,'verification.txt',data)
    evidence={'task_id':task_id,'contract_event_id':cid,
              'artifacts':[{'attachment_id':aid,'sha256':hashlib.sha256(data).hexdigest()}],
              'acceptance':[{'criterion_id':c,'satisfied':True,'artifact_ids':[aid]} for c in contract['criteria']],
              'commands':[{'cmd':'unit verification fixture','exit_code':0,'output_attachment_id':aid}]}
    manifest=kb.store_attachment_bytes(conn,task_id,'completion-evidence.json',json.dumps(evidence).encode())
    return {'completion_evidence_attachment_id':manifest}
