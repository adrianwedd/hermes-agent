"""Operator rerouting preserves review provenance without reopening implementation."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_reroute_returned_review_preserves_implementer(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with kbc.connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='Review delivered source', assignee='builder')
        task = kb.claim_task(conn, tid, claimer='test')
        assert task
        assert kb.request_review(conn, tid, summary='Implementation delivered', reviewer='builder', expected_run_id=task.current_run_id)
        assert kb.request_review(conn, tid, summary='Route independent acceptance', reviewer='reviewer', force=True)
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        import json
        assert json.loads(row[0])['implementer'] == 'builder'
        assert kb.get_task(conn, tid).status == 'review'
        assert kb.get_task(conn, tid).assignee == 'reviewer'
        assert not kb.request_review(conn, tid, summary='Self review', reviewer=' builder ', force=True)
        assert kb.get_task(conn, tid).assignee == 'reviewer'
