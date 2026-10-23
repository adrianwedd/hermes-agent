"""Dashboard creation through the real automatic decomposer (only inference is fake)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_decompose as decomp
from hermes_cli.kanban_completion_evidence import contract_record


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    path = Path(__file__).resolve().parents[2] / 'plugins/kanban/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('decomposition_safety_api', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def create(client, declaration=None):
    payload = dict(title='Implement bounded acceptance', body='Build the scoped fixture and verify it.', triage=True)
    if declaration is not None:
        payload['decomposition'] = declaration
    r = client.post('/tasks', json=payload)
    assert r.status_code == 200, r.text
    return r.json()['task']['id']


def events(tid):
    with kbc.connect_closing() as conn:
        return [e.payload for e in kb.list_events(conn, tid) if e.kind == 'auto_decompose_skipped']


def fake_inference(monkeypatch, callback=None, fanout=True):
    calls = []
    def infer(*args, **kwargs):
        calls.append(args)
        if callback:
            callback()
        return json.dumps({'fanout': fanout, 'title': 'Bounded build', 'body': 'Implement and test.',
                           'tasks': [{'title': 'Bounded build', 'body': 'Implement and test.', 'parents': []}]}), ''
    monkeypatch.setattr(decomp, '_call_aux', infer)
    return calls


def test_dashboard_explicit_safe_reaches_real_decomposer(client, monkeypatch):
    tid = create(client, {'decomposable': True})
    calls = fake_inference(monkeypatch)
    with kbc.connect_closing() as conn:
        _, contract = contract_record(conn, tid)
        assert contract['remaining_required_actions'][0]['decomposable'] is True
        assert contract['materialized_by'] == 'dashboard_operator_creation'
    assert tid in decomp.list_triage_ids()
    result = decomp.decompose_task(tid, author='auto-decomposer')
    assert result.ok, result.reason
    assert result.child_ids and calls
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != 'triage'
        assert kb.get_task(conn, result.child_ids[0]) is not None


@pytest.mark.parametrize('declaration,reason', [
    (None, 'decomposition_safety_not_declared'),
    ({'decomposable': False}, 'decomposition_explicitly_disallowed'),
    ({'decomposable': True, 'required_authority': 'operator'}, 'decomposition_authority_required'),
    ({'decomposable': True, 'gate_id': 'release-window'}, 'decomposition_gate_required'),
])
def test_dashboard_refusals_are_visible_durable_and_deduplicated(client, monkeypatch, declaration, reason):
    tid = create(client, declaration)
    calls = fake_inference(monkeypatch)
    for _ in range(2):
        assert tid not in decomp.list_triage_ids()
        assert not decomp.decompose_task(tid, author='auto-decomposer').ok
    assert not calls
    refusal = events(tid)
    assert len(refusal) == 1
    assert refusal[0]['reason'] == reason
    assert refusal[0]['requirement_id']
    assert refusal[0]['resolution']['command'] == f'hermes kanban specify {tid}'
    detail = client.get(f'/tasks/{tid}').json()
    assert any(e['kind'] == 'auto_decompose_skipped' for e in detail['events'])
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == 'triage'
        assert len(kb.list_tasks(conn)) == 1


@pytest.mark.parametrize('fanout', [True, False])
def test_refusal_changes_and_revocation_during_inference(client, monkeypatch, fanout):
    tid = create(client)
    assert tid not in decomp.list_triage_ids()
    def declare(**changes):
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            _, contract = contract_record(conn, tid)
            contract['remaining_required_actions'][0].update(changes)
            kb._append_event(conn, tid, 'completion_requirements', contract)
    declare(decomposable=False)
    assert tid not in decomp.list_triage_ids()
    assert [e['reason'] for e in events(tid)] == ['decomposition_safety_not_declared', 'decomposition_explicitly_disallowed']
    declare(decomposable=True)
    calls = fake_inference(monkeypatch, lambda: declare(required_authority='operator'), fanout=fanout)
    assert tid in decomp.list_triage_ids()
    assert not decomp.decompose_task(tid, author='auto-decomposer').ok
    assert len(calls) == 1
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == 'triage'
        assert len(kb.list_tasks(conn)) == 1
    assert events(tid)[-1]['reason'] == 'decomposition_authority_required'


@pytest.mark.parametrize('selected,reason', [
    ({'required_authority': 'operator'}, 'decomposition_authority_required'),
    ({'type': 'authority_grant'}, 'decomposition_authority_required'),
    ({'type': 'operator_decision'}, 'decomposition_authority_required'),
    ({'gate_id': 'release-window'}, 'decomposition_gate_required'),
    ({'type': 'gate'}, 'decomposition_gate_required'),
    ({'type': 'external_condition'}, 'decomposition_gate_required'),
    ({'type': 'wait'}, 'decomposition_gate_required'),
    pytest.param({'decomposable': False}, 'decomposition_explicitly_disallowed', id='selected-negative-decomposable'),
    pytest.param({'worker_executable': False}, 'decomposition_explicitly_disallowed', id='selected-negative-worker'),
])
@pytest.mark.parametrize('during_inference', [False, True])
@pytest.mark.parametrize('fanout', [False, True])
def test_selected_action_boundary_blocks_decomposition(
    client, monkeypatch, selected, reason, during_inference, fanout,
):
    from hermes_cli.kanban_decision import auto_decompose_allowed
    tid = create(client, {'decomposable': True})

    def revoke():
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            _, contract = contract_record(conn, tid)
            contract['selected_next_action'] = selected
            kb._append_event(conn, tid, 'completion_requirements', contract)

    if during_inference:
        assert tid in decomp.list_triage_ids()
    else:
        revoke()
    calls = fake_inference(monkeypatch, revoke if during_inference else None, fanout=fanout)
    result = decomp.decompose_task(tid, author='auto-decomposer')
    assert not result.ok, result
    assert len(events(tid)) == 1  # The refusing persistence path records it, not a later scan.
    assert len(calls) == int(during_inference)
    with kbc.connect_closing() as conn:
        allowed, decision = auto_decompose_allowed(conn, kb.get_task(conn, tid))
        assert not allowed
        assert not decision['auto_decompose_allowed']
        assert kb.get_task(conn, tid).status == 'triage'
        assert len(kb.list_tasks(conn)) == 1
        assert conn.execute('SELECT count(*) FROM task_links').fetchone()[0] == 0
    for _ in range(2):
        assert tid not in decomp.list_triage_ids()
        assert not decomp.decompose_task(tid, author='auto-decomposer').ok
    refusal = events(tid)
    assert len(refusal) == 1
    assert refusal[0]['reason'] == reason
    assert refusal[0]['required_authority'] == selected.get('required_authority')
    assert refusal[0]['gate_id'] == selected.get('gate_id')
    assert refusal[0]['selected_next_action'] == selected
    assert refusal[0]['requirement_id']
    assert refusal[0]['resolution']['owner'] == 'operator/specifier'
    assert refusal[0]['resolution']['command'] == f'hermes kanban specify {tid}'
    assert any(e['kind'] == 'auto_decompose_skipped'
               for e in client.get(f'/tasks/{tid}').json()['events'])


@pytest.mark.parametrize('requirement_decomposable', [None, True, False])
@pytest.mark.parametrize('requirement_worker', [None, True, False])
@pytest.mark.parametrize('selected_decomposable', [None, True, False])
@pytest.mark.parametrize('selected_worker', [None, True, False])
@pytest.mark.parametrize('authority_source', ['absent', 'requirement', 'selected', 'both'])
def test_cross_source_declaration_matrix(
    client, requirement_decomposable, requirement_worker,
    selected_decomposable, selected_worker, authority_source,
):
    """Absence is unknown; only requirement positives grant, either source vetoes."""
    from hermes_cli.kanban_decision import auto_decompose_allowed
    tid = create(client)
    requirement_flags = (requirement_decomposable, requirement_worker)
    selected_flags = (selected_decomposable, selected_worker)
    requirement = {key: value for key, value in zip(
        ('decomposable', 'worker_executable'), requirement_flags,
    ) if value is not None}
    selected = {key: value for key, value in zip(
        ('decomposable', 'worker_executable'), selected_flags,
    ) if value is not None}
    if authority_source in ('requirement', 'both'):
        requirement['required_authority'] = 'operator'
    if authority_source in ('selected', 'both'):
        selected['required_authority'] = 'operator'
    expected = (
        authority_source == 'absent'
        and any(value is True for value in requirement_flags)
        and not any(value is False for value in requirement_flags + selected_flags)
    )
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        _, contract = contract_record(conn, tid)
        contract['remaining_required_actions'] = [requirement]
        contract['selected_next_action'] = selected
        kb._append_event(conn, tid, 'completion_requirements', contract)
        allowed, decision = auto_decompose_allowed(conn, kb.get_task(conn, tid))
        assert allowed is expected
        assert decision['auto_decompose_allowed'] is expected
    assert (tid in decomp.list_triage_ids()) is expected
    assert (tid in decomp.list_triage_ids()) is expected
    if not expected:
        refusal = events(tid)
        assert len(refusal) == 1
        assert refusal[0]['selected_next_action'] == selected
        assert refusal[0]['requirement'] == requirement
        assert refusal[0]['resolution']['command'] == f'hermes kanban specify {tid}'
        assert any(e['kind'] == 'auto_decompose_skipped'
                   for e in client.get(f'/tasks/{tid}').json()['events'])
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == 'triage'
        assert len(kb.list_tasks(conn)) == 1
        assert conn.execute('SELECT count(*) FROM task_links').fetchone()[0] == 0


def test_existing_explicit_worker_requirement_still_decomposes(client, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Historical worker intake', body='Retained explicit worker scope.', triage=True)
        from hermes_cli.kanban_completion_workflow import ensure_scope_contract
        ensure_scope_contract(conn, tid)
        _, contract = contract_record(conn, tid)
        contract['selected_next_action'] = {}
        contract['remaining_required_actions'] = [{'action': 'Implement fixture', 'worker_executable': True}]
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'completion_requirements', contract)
    fake_inference(monkeypatch)
    assert tid in decomp.list_triage_ids()
    assert decomp.decompose_task(tid, author='auto-decomposer').ok


def test_missing_declaration_is_not_inferred_from_title(client):
    for title in ['Implement safe worker code', 'Approve release', 'Wait for release window']:
        r = client.post('/tasks', json={'title': title, 'triage': True})
        assert r.status_code == 200
        tid = r.json()['task']['id']
        assert tid not in decomp.list_triage_ids()
        assert events(tid)[0]['reason'] == 'decomposition_safety_not_declared'


@pytest.mark.parametrize('value', ['true', 1, 'yes'])
def test_declaration_requires_literal_boolean(client, value):
    r = client.post('/tasks', json={'title': 'scope', 'triage': True, 'decomposition': {'decomposable': value}})
    assert r.status_code == 422


def test_idempotent_retry_does_not_add_authority(client):
    payload = {'title': 'scope', 'triage': True, 'idempotency_key': 'safe-retry'}
    first = client.post('/tasks', json=payload).json()['task']['id']
    payload['decomposition'] = {'decomposable': True}
    assert client.post('/tasks', json=payload).json()['task']['id'] == first
    assert first not in decomp.list_triage_ids()
    assert events(first)[0]['reason'] == 'decomposition_safety_not_declared'


def test_concurrent_unchanged_scans_record_one_refusal(client):
    from concurrent.futures import ThreadPoolExecutor
    tid = create(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(tid not in ids for ids in pool.map(lambda _: decomp.list_triage_ids(), range(4)))
    assert len(events(tid)) == 1


def test_native_gate_overrides_conflicting_worker_declaration(client, monkeypatch):
    from hermes_cli.kanban_wait_contract import declaration
    from hermes_cli.kanban_decision import auto_decompose_allowed
    tid = create(client, {'decomposable': True})
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        _, contract = contract_record(conn, tid)
        contract['wait_contract'] = {
            'version': 1, 'provenance': {'source': 'operator'},
            'gates': [declaration('release-window', 'time_due', resolver='clock',
                                  parameters={'scheduled_at': 9999999999}, action='Wait for release', reason='Release window is not open')],
            'continuation': {'state': 'todo'},
        }
        kb._append_event(conn, tid, 'completion_requirements', contract)
        assert not auto_decompose_allowed(conn, kb.get_task(conn, tid))[0]
    calls = fake_inference(monkeypatch)
    assert tid not in decomp.list_triage_ids()
    assert not decomp.decompose_task(tid, author='auto-decomposer').ok
    assert not calls
    assert events(tid)[0]['gate_id'] == 'release-window'
    assert events(tid)[0]['reason'] == 'decomposition_gate_required'


def test_browser_creation_payloads_reach_real_auto_decomposition(client, monkeypatch):
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('browser creation probe requires Node')
    root = Path(__file__).resolve().parents[2]
    run = subprocess.run([node, str(root / 'tests/plugins/fixtures/kanban_decomposition_probe.js'),
                          str(root / 'plugins/kanban/dashboard/dist/index.js')],
                         capture_output=True, text=True, check=True)
    unknown, safe, ordinary = json.loads(run.stdout)
    calls = fake_inference(monkeypatch)
    unknown_id = client.post('/tasks', json=unknown).json()['task']['id']
    safe_id = client.post('/tasks', json=safe).json()['task']['id']
    assert unknown_id not in decomp.list_triage_ids()
    assert safe_id in decomp.list_triage_ids()
    assert decomp.decompose_task(safe_id, author='auto-decomposer').ok
    assert len(calls) == 1
    assert events(unknown_id)[0]['reason'] == 'decomposition_safety_not_declared'
    assert 'decomposition' not in ordinary
