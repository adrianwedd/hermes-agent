import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb,kanban_db_connect as kbc,kanban_completion_evidence as ce,kanban_reassessment as r

@pytest.fixture
def card(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'.hermes'));monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    kb.init_db()
    with kbc.connect_closing() as c:
        workspace=tmp_path/'owned';workspace.mkdir()
        tid=kb.create_task(c,title='Existing scope',body='Prepare retained evidence; final exclusive local A/B still required.',assignee='default',workspace_kind='dir',workspace_path=str(workspace),initial_status='blocked')
        aid=kb.store_attachment_bytes(c,tid,'accepted-diagnosis.md',b'Already accepted; do not repeat.',uploaded_by='operator')
        contract={'version':1,'kind':'research','criteria':['accepted_diagnosis','real_exclusive_trial','same_or_better_success'],'qualified_for_dispatch':False,'reassessment':{'mode':'offline_resolution','state':'pending','next_action':'Bind accepted diagnosis to offline plan; identify exact remaining external approval.','external_prerequisite':'Explicit approved exclusive local A/B window','input_attachment_ids':[aid],'input_digest':r.input_digest(c,tid,[aid])}}
        ce.set_requirements(c,tid,contract,expected_status='blocked',expected_assignee='default')
        yield c,tid,contract,aid

def facts(c,t):return 'cloud',True

def test_false_block_returns_existing_native_ready_without_waiving_acceptance(card):
    c,tid,contract,aid=card;t=kb.get_task(c,tid);before=c.execute('select count(*) from tasks').fetchone()[0]
    result=r.apply_grant(c,tid,facts_loader=facts)
    assert result['prepared'] and kb.get_task(c,tid).status=='ready'
    _,current=ce.contract_record(c,tid)
    assert current['criteria']==contract['criteria'] and current['reassessment']['external_prerequisite']==contract['reassessment']['external_prerequisite']
    assert kb.get_task(c,tid).body==t.body and kb.get_task(c,tid).assignee==t.assignee
    assert c.execute('select count(*) from tasks').fetchone()[0]==before
    assert 'Protected final prerequisite' in kb.build_worker_context(c,tid)
    assert kb.get_task(c,tid).max_retries==0 and kb.get_task(c,tid).goal_max_turns==24

def test_no_resolution_grant_does_not_release_local_trial(card):
    c,tid,contract,aid=card;contract.pop('reassessment');ce.set_requirements(c,tid,contract,expected_status='blocked',expected_assignee='default')
    assert r.apply_grant(c,tid,facts_loader=facts)['reason']=='explicit_resolution_grant_required'
    assert kb.get_task(c,tid).status=='blocked' and ce.contract_record(c,tid)[1]['qualified_for_dispatch'] is False

@pytest.mark.parametrize('stop_on_parent',[False,True])
def test_explicit_stop_on_task_or_ancestor_preserved(card,stop_on_parent):
    c,tid,contract,aid=card;target=tid
    if stop_on_parent:
        target=kb.create_task(c,title='Stopped programme',assignee='default',initial_status='blocked');kb.link_tasks(c,target,tid)
    kb.add_comment(c,target,'desktop','STOP')
    assert r.apply_grant(c,tid,facts_loader=facts)['reason']=='explicit_user_stop'
    assert kb.get_task(c,tid).status=='blocked'

def test_input_edit_invalidates_grant_before_facts_read(card):
    c,tid,contract,aid=card;a=kb.get_attachment(c,aid);Path(a.stored_path).write_bytes(b'Changed evidence')
    def forbidden(c,t):raise AssertionError('Must not inspect route after changed input')
    assert r.apply_grant(c,tid,facts_loader=forbidden)['reason']=='retained_inputs_or_scope_changed'

def test_global_local_lane_cannot_be_used(card):
    c,tid,contract,aid=card
    assert r.apply_grant(c,tid,facts_loader=lambda c,t:('local',True))['reason']=='cloud_route_or_workspace_unqualified'

def test_repeated_tick_does_not_accumulate_identical_blockers(card):
    c,tid,contract,aid=card
    for _ in range(3):assert r.prepare_tick(c,facts_loader=lambda c,t:('cloud',False))==0
    assert c.execute("select count(*) from task_events where task_id=? and kind='reassessment_refused'",(tid,)).fetchone()[0]==1

def test_worker_cannot_authorise_own_resolution(card,monkeypatch):
    c,tid,contract,aid=card;monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    with pytest.raises(PermissionError):r.apply_grant(c,tid,facts_loader=facts)

def test_ancestor_stop_arriving_during_facts_read_is_preserved(card):
    c,tid,contract,aid=card
    parent=kb.create_task(c,title='Parent',assignee='default',initial_status='blocked')
    c.execute("UPDATE tasks SET status='done' WHERE id=?",(parent,));c.commit();kb.link_tasks(c,parent,tid)
    contract['reassessment']['input_digest']=r.input_digest(c,tid,[aid]);ce.set_requirements(c,tid,contract,expected_status='blocked',expected_assignee='default')
    called=False
    def race(c,t):
        nonlocal called
        if not called:
            called=True;kb.add_comment(c,parent,'desktop','STOP')
        return 'cloud',True
    assert r.apply_grant(c,tid,facts_loader=race)['reason']=='explicit_user_stop'
    assert kb.get_task(c,tid).status=='blocked'

@pytest.mark.parametrize('malformed',[None,[],{'mode':'offline_resolution','input_attachment_ids':['wrong']}])
def test_malformed_grants_fail_closed(card,malformed):
    c,tid,contract,aid=card;contract['reassessment']=malformed;ce.set_requirements(c,tid,contract,expected_status='blocked',expected_assignee='default')
    assert not r.apply_grant(c,tid,facts_loader=facts)['prepared']
    lines=[];r.append_context(lines,c,tid);assert lines==[]

def test_nonpositive_existing_limits_cannot_disable_bounds(card):
    c,tid,contract,aid=card;c.execute('UPDATE tasks SET max_runtime_seconds=-1,goal_max_turns=0 WHERE id=?',(tid,));c.commit()
    assert r.apply_grant(c,tid,facts_loader=facts)['prepared']
    t=kb.get_task(c,tid);assert t.max_runtime_seconds==1800 and t.goal_max_turns==24

def test_consumed_attempt_guard_uses_run_id_not_same_second(card):
    from hermes_cli.kanban_readiness import preparation_reason
    c,tid,contract,aid=card
    assert r.apply_grant(c,tid,facts_loader=facts)['prepared']
    assert not preparation_reason(c,tid)
    t=kb.claim_task(c,tid,claimer='test');assert t and t.current_run_id
    # Native task_run record is bound to the one grant; a later ready cannot reuse it.
    c.execute("UPDATE tasks SET status='ready',current_run_id=NULL,worker_pid=NULL,claim_lock=NULL WHERE id=?",(tid,));c.commit()
    assert 'consumed' in preparation_reason(c,tid)
