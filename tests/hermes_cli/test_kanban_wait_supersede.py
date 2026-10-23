import copy
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_wait_contract import declaration, record
from hermes_cli.kanban_wait_transitions import declare_wait
from hermes_cli.kanban_wait_supersede import supersede_gate

@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    with connect(tmp_path / 'board.db') as c:
        tid = kb.create_task(c, title='Integrate accepted correction', body='Retain final verification.', assignee='reviewer', workspace_kind='scratch')
        kb.recompute_ready(c)
        g = declaration('old-correction', 'attestation', resolver='operator', parameters={'required_assertions':['condition_verified']}, action='Attest old correction', reason='Old review block', stage='BLOCKED')
        declare_wait(c, tid, [g], {'state':'review', 'selected_next_action':{'phase':'READY','type':'review_correction','action':'Redo Round 5'}}, provenance={'test':'retained'})
        kb.recompute_ready(c)
        cid, contract, _ = record(c, tid)
        doc = dict(contract_event_id=cid, gate_id=g['id'], revision=g['revision'], declaration=g['declaration'], reason='Operator identifies stale correction gate', selected_next_action={'phase':'READY','type':'worker_action','action':'Integrate accepted r6 and verify served UI','required_authority':None}, assignee='root')
        yield c, tid, doc, copy.deepcopy(contract)

def test_supersede_does_not_attest_and_preserves_acceptance(case):
    c, tid, doc, original = case
    result = supersede_gate(c, tid, **doc)
    _, updated, wait = record(c, tid)
    assert wait is None
    assert updated['selected_next_action'] == doc['selected_next_action']
    assert {k:v for k,v in updated.items() if k!='selected_next_action'} == {k:v for k,v in original.items() if k not in {'selected_next_action','wait_contract'}}
    assert kb.get_task(c, tid).assignee == 'root'
    assert result['workflow_stage'] != 'REVIEW'
    assert c.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='wait_gate_satisfied'", (tid,)).fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='wait_gate_superseded'", (tid,)).fetchone()[0] == 1
    kb.recompute_ready(c)
    assert record(c, tid)[2] is None

@pytest.mark.parametrize('field,value', [('contract_event_id',-1),('revision',99),('declaration','stale'),('reason',''),('selected_next_action',{})])
def test_stale_or_incomplete_request_is_atomic(case, field, value):
    c, tid, doc, original = case
    doc[field] = value
    with pytest.raises(ValueError): supersede_gate(c, tid, **doc)
    assert record(c, tid)[1] == original

def test_workers_cannot_supersede(case, monkeypatch):
    c, tid, doc, original = case
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    with pytest.raises(PermissionError): supersede_gate(c, tid, **doc)
    assert record(c, tid)[1] == original

def test_preserves_other_gate(case):
    c, tid, doc, original = case
    other = declaration('other', 'attestation', resolver='operator', parameters={'required_assertions':['condition_verified']}, action='Verify other', reason='Other condition', stage='BLOCKED')
    original['wait_contract']['gates'].append(other)
    with kb.write_txn(c): kb._append_event(c,tid,'completion_requirements',original)
    doc['contract_event_id'] = record(c,tid)[0]
    result = supersede_gate(c,tid,**doc)
    assert record(c,tid)[2]['gates'] == [other]
    assert not result['dispatchable']

@pytest.mark.parametrize('evaluator,params', [
    ('explicit_stop', {'stop_event_id':1,'required_assertions':['explicit_stop_revoked']}),
    ('operator_decision', {'pending_event_id':1,'required_assertions':['decision_authorised']}),
    ('retry_budget', {'counter':'tasks.consecutive_failures','limit':'tasks.max_retries'}),
])
def test_protected_evaluators_cannot_be_superseded(case, evaluator, params):
    c, tid, doc, original = case
    g = declaration('protected', evaluator, resolver='operator', parameters=params, action='Retain protection', reason='Protected boundary', stage='BLOCKED')
    original['wait_contract']['gates'] = [g]
    with kb.write_txn(c): kb._append_event(c,tid,'completion_requirements',original)
    doc.update(contract_event_id=record(c,tid)[0],gate_id=g['id'],revision=g['revision'],declaration=g['declaration'])
    with pytest.raises(PermissionError): supersede_gate(c,tid,**doc)
    assert record(c,tid)[1] == original

def test_claimed_task_cannot_be_superseded(case):
    c, tid, doc, original = case
    with kb.write_txn(c): c.execute("UPDATE tasks SET claim_lock='live' WHERE id=?",(tid,))
    with pytest.raises(RuntimeError): supersede_gate(c,tid,**doc)
    assert record(c,tid)[1] == original


def test_superseded_native_block_stays_retired_after_retry_breaker(tmp_path, monkeypatch):
    from hermes_cli.kanban_wait_migration import proposal
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    with connect(tmp_path / 'board.db') as c:
        tid = kb.create_task(c, title='Integrate accepted correction', assignee='reviewer', workspace_kind='scratch')
        kb.recompute_ready(c)
        kb.block_task(c, tid, reason='Old correction', kind='capability')
        kb.recompute_ready(c)
        cid, _, wait = record(c, tid)
        assert wait is not None
        old = next(g for g in wait['gates'] if g['id'].startswith('blocked:'))
        supersede_gate(c, tid, contract_event_id=cid, gate_id=old['id'], revision=old['revision'],
                       declaration=old['declaration'], reason='Correction accepted; verify integration',
                       selected_next_action={'phase':'READY','type':'worker_action','action':'Verify served integration'},
                       assignee='default')
        assert record(c, tid)[2] is None
        with kb.write_txn(c):
            c.execute("UPDATE tasks SET status='blocked',block_kind=NULL,consecutive_failures=3 WHERE id=?", (tid,))
            kb._append_event(c, tid, 'gave_up', {'protocol_violations':3,'sticky':True,'retry_status':'ready'})
        proposed = proposal(c, kb.get_task(c, tid))
        assert [g['id'] for g in proposed['gates']] == ['retry-budget']
        kb.block_task(c, tid, reason='New independent blocker', kind='capability')
        kb.recompute_ready(c)
        current_wait = record(c, tid)[2]
        assert current_wait is not None
        current = current_wait['gates']
        assert any(g['id'].startswith('blocked:') and g['id'] != old['id'] for g in current)
        with kb.write_txn(c):
            c.execute("UPDATE tasks SET status='blocked',block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
            kb._append_event(c, tid, 'blocked', {'kind':'explicit_stop','reason':'New operator STOP'})
        proposed = proposal(c, kb.get_task(c, tid))
        assert any(g['evaluator'] == 'explicit_stop' for g in proposed['gates'])
