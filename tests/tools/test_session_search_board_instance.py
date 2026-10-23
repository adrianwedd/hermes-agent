"""A reusable slug must not authorize another database's retained workers."""
import json
import sqlite3

from hermes_state import SessionDB
from tools.session_search_tool import session_search


def test_same_slug_requires_current_database_receipt(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    try:
        for sid in ('old', 'current'):
            db.create_session(sid, source='kanban', origin_json=json.dumps({'kanban_board': 'shared'}))
            db.append_message(sid, role='assistant', content='instanceproof ' + sid)
            db._conn.execute('UPDATE sessions SET title=? WHERE id=?', ('instanceproof ' + sid, sid))
        for name, sid in [('old-board.db', 'old'), ('new-board.db', 'current')]:
            with sqlite3.connect(tmp_path / name) as conn:
                conn.executescript('CREATE TABLE task_runs (id INTEGER, task_id TEXT, metadata TEXT); '
                                   'CREATE TABLE task_events (run_id INTEGER, task_id TEXT, kind TEXT, payload TEXT);')
                conn.execute('INSERT INTO task_runs VALUES (1, ?, ?)', ('task', json.dumps({'worker_session_id': sid})))
        monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
        monkeypatch.setenv('HERMES_KANBAN_BOARD', 'shared')
        for name, allowed, denied in [('old-board.db', 'old', 'current'), ('new-board.db', 'current', 'old')]:
            monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / name))
            for args in ({'query': 'instanceproof'}, {}):
                result = json.loads(session_search(db=db, limit=10, **args))
                assert {r['session_id'] for r in result['results']} == {allowed}
            assert json.loads(session_search(db=db, query=denied))['count'] == 0
            assert json.loads(session_search(db=db, session_id=allowed))['success'] is True
            assert json.loads(session_search(db=db, session_id=denied))['success'] is False
            anchor = db.get_messages(denied)[0]['id']
            assert json.loads(session_search(db=db, session_id=denied, around_message_id=anchor))['success'] is False
    finally:
        db.close()
