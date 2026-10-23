"""Dashboard readiness must not manufacture PREPARE -> READY -> PREPARE events."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    from hermes_cli import kanban_decision
    monkeypatch.setattr(kanban_decision, 'execution_environment_fingerprint',
                        lambda *a, **kw: {'identity': 'test', 'healthy': True})
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Reconcile the five situations',
                             body='Acceptance: verify the bounded source evidence.',
                             assignee='steward', workspace_kind='worktree',
                             initial_status='running')
        kb._append_event(conn, tid, 'completion_requirements', {
            'version': 1, 'kind': 'implementation', 'local_only': True,
            'require_review': True, 'require_commands': True,
            'criteria': ['authoritative_scope_satisfied'],
            'qualified_for_dispatch': True, 'scope': 'bounded fixture',
            'scope_sha256': 'fixture',
            'selected_next_action': {'phase': 'READY', 'type': 'worker_action',
                                     'action': 'Verify the five situations'},
        })
        conn.execute("UPDATE tasks SET status='todo',workspace_path=NULL WHERE id=?", (tid,))
        for title in ('parent one', 'parent two', 'parent three'):
            parent = kb.create_task(conn, title=title, initial_status='running')
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
            conn.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', (parent, tid))
        app = FastAPI()
        app.include_router(_load_plugin_router(), prefix='/api/plugins/kanban')
        yield conn, tid, TestClient(app)


def events(conn, tid):
    return [tuple(r) for r in conn.execute(
        'SELECT id,kind,payload FROM task_events WHERE task_id=? ORDER BY id', (tid,))]


def test_repeated_ready_requests_refuse_preparation_without_event_churn(board):
    conn, tid, client = board
    rounds = []
    for attempt in range(5):
        before = events(conn, tid)
        response = client.patch(f'/api/plugins/kanban/tasks/{tid}', json={'status': 'ready'})
        assert response.status_code == 409, response.text
        if attempt == 0:
            assert 'resolve the owned source or evidence workspace' in response.json()['detail']
        assert kb.get_task(conn, tid).status != 'ready'
        # The refused request itself writes nothing.
        assert events(conn, tid) == before
        kb.recompute_ready(conn)
        rounds.append(events(conn, tid))
    # recompute_ready may record the unmet preparation once; repeated requests
    # must not keep adding PREPARE -> READY -> PREPARE events after that.
    assert rounds[-1] == rounds[-2] == rounds[-3]
    assert not any(kind == 'status' for _, kind, _ in rounds[-1])


@pytest.mark.parametrize('workspace_kind', ['worktree', 'scratch'])
def test_resolved_preparation_can_become_ready_and_repeat_is_noop(board, tmp_path, workspace_kind):
    conn, tid, client = board
    workspace = tmp_path / 'owned-source'
    workspace.mkdir()
    conn.execute('UPDATE tasks SET workspace_kind=?,workspace_path=? WHERE id=?',
                 (workspace_kind, str(workspace) if workspace_kind == 'worktree' else None, tid))
    response = client.patch(f'/api/plugins/kanban/tasks/{tid}', json={'status': 'ready'})
    assert response.status_code == 200, response.text
    assert kb.get_task(conn, tid).status == 'ready'
    before = events(conn, tid)
    for _ in range(3):
        response = client.patch(f'/api/plugins/kanban/tasks/{tid}', json={'status': 'ready'})
        assert response.status_code == 200, response.text
        assert kb.recompute_ready(conn) == 0
        assert events(conn, tid) == before
