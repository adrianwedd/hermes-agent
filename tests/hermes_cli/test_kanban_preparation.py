"""Real native stage/CAS/hold/admission fixtures for bounded TODO preparation."""
import json

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_preparation as prep, kanban_provider_admission as admission
from hermes_cli.kanban_completion_evidence import set_requirements, contract_record
from hermes_cli.kanban_administrative_hold import set_administrative_pending

GOOD = dict(project_exists=True, profile_exists=True, lane='cloud', workspace_exists=True,
            source_matches=True, workspace_busy=False)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {'provider_admission': {'local': 0, 'cloud': 1}}}))
    conn = kbc.connect(db_path=tmp_path / 'kanban.db')
    yield conn, tmp_path
    conn.close()


def parked(board, *, parents=()):
    conn, root = board
    tid = kb.create_task(conn, title='Original bounded implementation', body='Exact original acceptance',
                         workspace_kind='dir', workspace_path=str(root / 'old'), parents=parents)
    kb.edit_task(conn, tid, dispatch_eligible=False)
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,)); conn.commit()
    return tid


def grant(board, tid):
    conn, root = board
    contract = {'version': 1, 'kind': 'implementation', 'local_only': True,
                'qualified_for_dispatch': False, 'criteria': ['original_acceptance'],
                'preparation': {'mode': 'automatic', 'grant_qualification': True,
                    'scope_digest': prep.scope_snapshot(conn, tid)['scope_digest'],
                    'project_id': 'p_fixture', 'workspace_path': str(root / tid),
                    'source_revision': 'a' * 40, 'isolated_workspace': True}}
    t = kb.get_task(conn, tid)
    assert set_requirements(conn, tid, contract, expected_status=t.status, expected_assignee=t.assignee)
    return contract_record(conn, tid)[0]


def good(conn, task, request):
    return dict(GOOD)


def test_parked_unprepared_to_qualified_ready_is_native_and_once(board):
    conn, _ = board; tid = parked(board)
    assert prep.prepare_task(conn, tid, facts_loader=good)['reason'] == 'operator_scope_contract_required'
    cid = grant(board, tid)
    result = prep.prepare_tick(conn, facts_loader=good)
    assert result['prepared'] == 1 and result['models_called'] == result['workers_launched'] == 0
    task = kb.get_task(conn, tid)
    assert task.status == 'ready' and task.dispatch_eligible and task.body == 'Exact original acceptance'
    new_cid, contract = contract_record(conn, tid)
    assert new_cid != cid and contract['criteria'] == ['original_acceptance']
    assert contract['qualified_for_dispatch'] and contract['preparation']['source_contract_event'] == cid
    assert prep.prepare_tick(conn, facts_loader=good)['prepared'] == 0
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_real_dependency_refusal_is_visible_deduplicated_and_recovers(board):
    conn, _ = board; parent = parked(board); tid = parked(board, parents=[parent]); grant(board, tid)
    for _ in range(2):
        assert prep.prepare_task(conn, tid, facts_loader=good)['reason'] == 'dependency_wait'
    assert kb.get_task(conn, tid).status == 'todo' and not kb.get_task(conn, tid).dispatch_eligible
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='preparation_refused'", (tid,)).fetchone()[0] == 1
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,)); conn.commit()
    assert prep.prepare_task(conn, tid, facts_loader=good)['prepared']


@pytest.mark.parametrize('field,value,reason', [('project_exists', False, 'project_unresolved'),
    ('workspace_busy', True, 'workspace_owner_conflict'), ('lane', 'local', 'cloud_route_unqualified'),
    ('source_matches', False, 'isolated_source_workspace_unqualified')])
def test_missing_prerequisite_never_becomes_ready(board, field, value, reason):
    conn, _ = board; tid = parked(board); grant(board, tid)
    def facts(*args):
        return dict(GOOD, **{field: value})
    assert prep.prepare_task(conn, tid, facts_loader=facts)['reason'] == reason
    assert kb.get_task(conn, tid).status == 'todo' and not kb.get_task(conn, tid).dispatch_eligible


def test_later_manual_hold_or_scope_edit_invalidates_grant(board):
    conn, _ = board; tid = parked(board); grant(board, tid)
    kb.edit_task(conn, tid, dispatch_eligible=False)
    assert prep.prepare_task(conn, tid, facts_loader=good)['reason'] == 'scope_or_manual_hold_changed'
    assert not kb.get_task(conn, tid).dispatch_eligible


def test_stop_and_done_are_never_reopened_or_duplicated(board):
    conn, _ = board; tid = parked(board); cid = grant(board, tid)
    set_administrative_pending(conn, tid, expected_status='todo', expected_assignee=None,
        expected_contract_event=cid, reason='Explicit accepted STOP; no more workers')
    assert prep.prepare_task(conn, tid, facts_loader=good)['reason'] == 'administrative_stop'
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,)); conn.commit()
    assert prep.prepare_tick(conn, facts_loader=good)['prepared'] == 0
    assert kb.get_task(conn, tid).status == 'done'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_active_or_stale_snapshot_cannot_overwrite_owner_or_scope(board):
    conn, _ = board; tid = parked(board); grant(board, tid); calls = 0
    def raced(conn, task, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            kb.add_comment(conn, tid, 'operator', 'New scope decision; preserve it')
        return dict(GOOD)
    assert prep.prepare_task(conn, tid, facts_loader=raced)['reason'] == 'snapshot_changed'
    assert not kb.get_task(conn, tid).dispatch_eligible


def test_tick_cap_and_real_managed_claim_budget_are_not_multiplied(board, monkeypatch):
    conn, root = board; first = parked(board); second = parked(board)
    grant(board, first); grant(board, second)
    assert prep.prepare_tick(conn, limit=100, facts_loader=good)['prepared'] == 1
    assert prep.prepare_tick(conn, facts_loader=good)['prepared'] == 1
    monkeypatch.setattr(admission, 'task_lane', lambda task: 'cloud')
    assert kb.claim_task(conn, first) is not None
    assert kb.claim_task(conn, second) is None  # real host-lock/count/admission path
    assert conn.execute("SELECT count(*) FROM task_runs WHERE status='running'").fetchone()[0] == 1


def test_worker_cannot_apply_preparation_authority(board, monkeypatch):
    conn, _ = board; tid = parked(board); grant(board, tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    with pytest.raises(PermissionError):
        prep.prepare_task(conn, tid, facts_loader=good)


def test_real_cross_board_overlap_and_retained_live_worker_are_not_isolated(board):
    import os
    conn, root = board
    tid = parked(board)
    target = root / 'isolated'; target.mkdir()
    other_path = root / 'kanban' / 'boards' / 'other' / 'kanban.db'
    other_path.parent.mkdir(parents=True)
    with kbc.connect_closing(db_path=other_path) as other:
        foreign = kb.create_task(other, title='Foreign board owner', workspace_kind='dir',
                                 workspace_path=str(target / 'nested'))
        assert prep.workspace_busy(conn, tid, target)
        other.execute("UPDATE tasks SET status='done' WHERE id=?", (foreign,)); other.commit()
        assert not prep.workspace_busy(conn, tid, target)
        # A terminal card can still retain a live worker. This is real PID
        # liveness, not a mocked route or ownership decision.
        other.execute("INSERT INTO task_runs(task_id,profile,status,worker_pid,started_at) VALUES(?,?,?,?,?)",
                      (foreign, 'fixture', 'done', os.getpid(), 1)); other.commit()
        assert prep.workspace_busy(conn, tid, target)


def test_final_recheck_holds_shared_admission_lock(board):
    conn, root = board; tid = parked(board); grant(board, tid); calls = 0
    def facts(*args):
        nonlocal calls
        calls += 1
        with admission.host_lock(root) as acquired:
            assert not acquired
        return dict(GOOD)
    assert prep.prepare_task(conn, tid, facts_loader=facts)['prepared']
    assert calls == 2


def test_other_controller_or_worker_claim_defers_preparation(board):
    conn, root = board; tid = parked(board); grant(board, tid)
    with admission.host_lock(root) as acquired:
        assert acquired
        assert prep.prepare_task(conn, tid, facts_loader=good)['reason'] == 'provider_admission_busy'
    assert not kb.get_task(conn, tid).dispatch_eligible


def test_real_runtime_facts_pin_clean_source_revision(board, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from contextlib import nullcontext
    from hermes_cli import projects_db, profiles
    conn, root = board; tid = parked(board); grant(board, tid)
    workspace = root / tid; workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    (workspace / 'scope.txt').write_text('original scope\n')
    subprocess.run(['git', '-C', str(workspace), 'add', 'scope.txt'], check=True)
    subprocess.run(['git', '-C', str(workspace), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture'], check=True)
    rev = subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip()
    kb.reassign_task(conn, tid, 'fixture')
    monkeypatch.setattr(projects_db, 'connect_closing', lambda: nullcontext(None))
    monkeypatch.setattr(projects_db, 'get_project', lambda conn, pid: SimpleNamespace(id=pid))
    monkeypatch.setattr(profiles, 'profile_exists', lambda name: name == 'fixture')
    monkeypatch.setattr(admission, 'task_lane', lambda task: 'cloud')
    _, contract = contract_record(conn, tid); request = dict(contract['preparation'], source_revision=rev)
    facts = prep.runtime_facts(conn, prep.scope_snapshot(conn, tid), request)
    assert facts == GOOD
    (workspace / 'scope.txt').write_text('changed scope\n')
    assert not prep.runtime_facts(conn, prep.scope_snapshot(conn, tid), request)['source_matches']
