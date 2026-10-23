"""Bounded integration regressions; isolated boards, judge stub only (no inference)."""
import argparse
import contextlib
import copy
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_completion_evidence import contract_record, CompletionEvidenceError
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_wait_contract import declaration, record
from hermes_cli.kanban_wait_transitions import declare_wait, resolve_gate


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ['HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_SESSION_ID']:
        monkeypatch.delenv(name, raising=False)
    with kbc.connect(tmp_path / 'board.db') as conn:
        yield conn


def create(conn, *, goal=False):
    tid = kb.create_task(conn, title='Implement bounded offline correction', body='Retain independent review and original acceptance.', assignee='builder', workspace_kind='scratch', goal_mode=goal)
    kb.recompute_ready(conn)
    run = kb.claim_task(conn, tid, claimer='builder:test')
    assert run
    return tid, run


def phase(conn, tid):
    # Exact latest selected phase/future conditions captured read-only from the
    # circular-handoff card; no inferred replacement acceptance.
    fixture = Path(__file__).parents[1] / 'fixtures/kanban-current-integration/t_e89091c7-contract.json'
    updated = json.loads(fixture.read_text())
    # Handoff fixture is after the controller has resolved the two historical
    # capability holds; these holds are tested separately, never auto-waived.
    updated.pop('wait_contract')
    kb._append_event(conn, tid, 'completion_requirements', updated)
    return updated


def review(conn, tid, run):
    assert kb.request_review(conn, tid, summary='Correction retained with offline verification.', reviewer='reviewer', expected_run_id=run.current_run_id)
    result = kb.claim_review_task(conn, tid, claimer='reviewer:test')
    assert result
    return result


def test_cli_bounded_handoff_needs_no_judge_but_completion_still_does(board, monkeypatch):
    from hermes_cli import kanban as cli
    tid, run = create(board, goal=True)
    original = phase(board, tid)
    goals = []
    monkeypatch.setattr('agent.auxiliary_client.get_text_auxiliary_client', lambda *a, **kw: (object(),'stub'))
    def judge(**kw):
        goals.append(kw['goal'])
        if 'whole goal remains nonterminal' in kw['goal']:
            return 'done','bounded readiness',False,None,False
        return 'continue','future comparison and independent adjudication not done',False,None,False
    monkeypatch.setattr('hermes_cli.goals.judge_goal', judge)
    monkeypatch.setattr(kbc, 'connect_closing', lambda: contextlib.nullcontext(board))
    args = argparse.Namespace(task_id=tid,summary='Packets corrected; ready for independent adjudication',metadata=None,reviewer='reviewer',force=False)
    assert cli._cmd_request_review(args) == 0
    assert kb.get_task(board,tid).status == 'review'
    assert contract_record(board,tid)[1]['completion_conditions'] == original['completion_conditions']
    assert goals == []
    assert cli._goal_gate_error(board,tid,'Only packets ready','completion','blocked','continue')
    assert len(goals) == 1
    assert 'whole goal remains nonterminal' not in goals[-1]


def test_changes_tool_routes_exact_correction_without_schedule_keyword(board, monkeypatch):
    from tools import kanban_tools as kt
    tid, run = create(board)
    r = review(board,tid,run)
    monkeypatch.setenv('HERMES_KANBAN_TASK',tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(r.current_run_id))
    monkeypatch.setattr(kt,'_board',lambda *a: contextlib.nullcontext((kb,board)))
    response = json.loads(kt._handle_request_changes({'reason':'Correct only C35 selector ordering'}))
    assert 'error' not in response, response
    monkeypatch.delenv('HERMES_KANBAN_TASK');monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    kb.recompute_ready(board)
    assert decide(board,kb.get_task(board,tid))['dispatchable']
    assert 'C35' in contract_record(board,tid)[1]['selected_next_action']['action']
    assert not kb.request_changes(board,tid,reason='stale verdict',expected_run_id=r.current_run_id)[0]


def test_accepted_review_closes_but_producer_and_future_work_do_not(board):
    tid, run = create(board)
    with pytest.raises(CompletionEvidenceError): kb.complete_task(board,tid,summary='self acceptance',expected_run_id=run.current_run_id)
    r = review(board,tid,run)
    assert kb.complete_task(board,tid,summary='Independent correction accepted',expected_run_id=r.current_run_id)
    assert kb.get_task(board,tid).status == 'done'
    tid2, run2 = create(board)
    phase(board,tid2)
    r2 = review(board,tid2,run2)
    with pytest.raises(CompletionEvidenceError): kb.complete_task(board,tid2,summary='Only bounded work',expected_run_id=r2.current_run_id)
    from hermes_cli.kanban_review_transition import accept_bounded_review
    assert accept_bounded_review(board,tid2,'Bounded phase accepted',expected_run_id=r2.current_run_id)
    d = decide(board,kb.get_task(board,tid2))
    assert kb.get_task(board,tid2).status != 'done' and not d['dispatchable'] and d['workflow_stage']=='WAITING'
    assert all(not c['satisfied'] for c in contract_record(board,tid2)[1]['completion_conditions'])


def evidence(gate):
    return {'reference':'fixture://independent-evidence','sha256':'a'*64,'assertions':{a:True for a in gate['parameters']['required_assertions']}}


def resolve(conn,tid,g):
    return resolve_gate(conn,tid,g['id'],revision=g['revision'],declaration=g['declaration'],evidence=evidence(g),decision={'selection':'bounded offline work'})


def test_stop_only_revocation_and_current_operator_resolution(board):
    tid = kb.create_task(board,title='Inspect offline report',body='Offline only',assignee='builder',workspace_kind='scratch')
    kb.recompute_ready(board)
    # Native explicit STOP is operator metadata, not a worker block-kind.
    board.execute("UPDATE tasks SET status='blocked',block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?",(tid,))
    kb._append_event(board,tid,'blocked',{'kind':'explicit_stop','reason':'STOP'})
    kb._append_event(board,tid,'operator_decision_pending',{'reason':'Choose offline source','resolver':'Adrian','next_action':'Select retained source'})
    kb.recompute_ready(board)
    gates = record(board,tid)[2]['gates']
    stop = next(g for g in gates if g['evaluator']=='explicit_stop')
    decision = next(g for g in gates if g['evaluator']=='operator_decision')
    result = resolve(board,tid,stop)
    assert not result['dispatchable']
    assert any(g['evaluator']=='operator_decision' for g in result['remaining_gates'])
    with pytest.raises(ValueError): resolve_gate(board,tid,decision['id'],revision=99,declaration=decision['declaration'],evidence=evidence(decision),decision={'selection':'offline'})
    result = resolve(board,tid,decision)
    assert result['dispatchable'] and result['workflow_stage']=='READY'
    # A new decision cannot be cancelled by replaying a once-valid older receipt.
    kb._append_event(board,tid,'operator_decision_pending',{'reason':'New source boundary','resolver':'Adrian','next_action':'Choose new source'})
    kb.recompute_ready(board)
    assert not decide(board,kb.get_task(board,tid))['dispatchable']
    with pytest.raises(ValueError): resolve(board,tid,decision)


def test_time_and_dependency_wakes_require_actual_predicates(board,monkeypatch):
    import time
    now = int(time.time())
    parent = kb.create_task(board,title='Required predecessor',initial_status='blocked')
    tid = kb.create_task(board,title='Inspect retained report',body='Offline only',assignee='builder',workspace_kind='scratch',parents=[parent])
    assert kb.schedule_task(board,tid,reason='Due after fixture clock',scheduled_at=now+500)
    kb.recompute_ready(board)
    assert not decide(board,kb.get_task(board,tid))['dispatchable']
    monkeypatch.setattr(time,'time',lambda: now+501)
    kb.recompute_ready(board)
    assert not decide(board,kb.get_task(board,tid))['dispatchable']
    board.execute("UPDATE tasks SET status='done' WHERE id=?",(parent,))
    kb.recompute_ready(board)
    assert decide(board,kb.get_task(board,tid))['dispatchable']


def test_completed_card_stale_wait_events_cannot_resurrect_work(board):
    tid,run = create(board)
    r = review(board,tid,run)
    assert kb.complete_task(board,tid,summary='Independent acceptance',expected_run_id=r.current_run_id)
    for kind in ['operator_decision_pending','scheduled','changes_requested']:
        kb._append_event(board,tid,kind,{'reason':'historical stale event'})
    kb.recompute_ready(board)
    d = decide(board,kb.get_task(board,tid))
    assert kb.get_task(board,tid).status=='done' and d['workflow_stage']=='DONE' and not d['dispatchable']
    assert kb.claim_task(board,tid,claimer='stale:retry') is None


def test_e5d24de5_accepted_correction_selects_root_integration_not_old_round(board):
    from hermes_cli.kanban_wait_supersede import supersede_gate
    fixture = Path(__file__).parents[1] / 'fixtures/kanban-current-integration/t_e5d24de5-contract.json'
    accepted = json.loads(fixture.read_text())
    integration = accepted['selected_next_action']
    tid = kb.create_task(board,title='Accepted r6 correction',assignee='reviewer',workspace_kind='scratch')
    kb._append_event(board,tid,'completion_requirements',accepted)
    # Latest card already superseded Round 5, retaining a genuine retry gate.
    # Exhaustion must hold; repairing that predicate must select root, not review.
    board.execute('UPDATE tasks SET max_retries=3,consecutive_failures=3 WHERE id=?',(tid,))
    kb.recompute_ready(board)
    assert not decide(board,kb.get_task(board,tid))['dispatchable']
    board.execute('UPDATE tasks SET consecutive_failures=0,assignee=? WHERE id=?',('default',tid))
    kb.recompute_ready(board)
    result = decide(board,kb.get_task(board,tid))
    assert result['dispatchable'] and result['workflow_stage']=='READY'
    kb.recompute_ready(board)
    after = contract_record(board,tid)[1]
    assert after['selected_next_action']==integration
    assert after['criteria']==accepted['criteria']
    assert not result['remaining_gates']
    assert kb.get_task(board,tid).status!='done'
    assert kb.claim_task(board,tid,claimer='default:integration') is not None


def test_e89091c7_native_capability_holds_require_controller_resolution(board):
    fixture = Path(__file__).parents[1] / 'fixtures/kanban-current-integration/t_e89091c7-contract.json'
    contract = json.loads(fixture.read_text())
    tid = kb.create_task(board,title='Corrected ten-packet phase',assignee='builder',workspace_kind='scratch')
    kb._append_event(board,tid,'completion_requirements',contract)
    kb.recompute_ready(board)
    d = decide(board,kb.get_task(board,tid))
    assert not d['dispatchable']
    assert len(d['remaining_gates']) == 2
    assert all(g['evaluator']=='attestation' and g['resolver']=='operator' and g['current_unblock_action'] and g['wake_trigger'] for g in d['remaining_gates'])
    assert kb.claim_task(board,tid,claimer='builder:cannot-bypass') is None


def test_e89091c7_native_hold_resolution_review_waiting_continuity(board, monkeypatch):
    from hermes_cli.kanban_review_transition import accept_bounded_review
    from hermes_cli.kanban_wait_contract import gate_event

    fixture = Path(__file__).parents[1] / 'fixtures/kanban-current-integration/t_e89091c7-contract.json'
    original = json.loads(fixture.read_text())
    tid = kb.create_task(board, title='Retained offline packets', assignee='builder', workspace_kind='scratch')
    kb._append_event(board, tid, 'completion_requirements', original)
    kb.recompute_ready(board)
    gates = original['wait_contract']['gates']
    assert record(board, tid)[1] == original
    assert kb.claim_task(board, tid, claimer='builder:held') is None

    # Controller attestations are fixture evidence, never a grant on the live card.
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    with pytest.raises(PermissionError):
        resolve(board, tid, gates[0])
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    with pytest.raises(ValueError):
        resolve_gate(board, tid, gates[0]['id'], revision=99,
                     declaration=gates[0]['declaration'], evidence=evidence(gates[0]))
    first = resolve(board, tid, gates[0])
    assert not first['dispatchable']
    assert [g['id'] for g in first['remaining_gates']] == [gates[1]['id']]
    assert kb.claim_task(board, tid, claimer='builder:still-held') is None
    second = resolve(board, tid, gates[1])
    assert second['dispatchable'] and second['workflow_stage'] == 'READY'
    assert record(board, tid)[1]['selected_next_action'] == original['selected_next_action']
    assert record(board, tid)[2]['gates'] == gates
    assert all(gate_event(board, tid, g)['actor'] == 'operator' for g in gates)

    run = kb.claim_task(board, tid, claimer='builder:offline-correction')
    assert run
    assert not accept_bounded_review(board, tid, 'Producer is not reviewer', expected_run_id=run.current_run_id)
    reviewer = review(board, tid, run)
    assert reviewer.assignee == 'reviewer'
    assert not accept_bounded_review(board, tid, 'Stale producer verdict', expected_run_id=run.current_run_id)
    with pytest.raises(CompletionEvidenceError):
        kb.complete_task(board, tid, summary='No future authority', expected_run_id=reviewer.current_run_id)
    assert accept_bounded_review(board, tid, 'Independent offline adjudication accepted', expected_run_id=reviewer.current_run_id)
    for _ in range(2):
        kb.recompute_ready(board)
        decision = decide(board, kb.get_task(board, tid))
        assert decision['workflow_stage'] == 'WAITING' and not decision['dispatchable']
        assert kb.get_task(board, tid).status != 'done'
        assert kb.claim_task(board, tid, claimer='builder:no-future-grant') is None
    retained = contract_record(board, tid)[1]
    assert retained['completion_conditions'] == original['completion_conditions']
    assert retained['criteria'] == original['criteria']
    assert retained['scope'] == original['scope']
    assert retained['selected_next_action']['phase'] == 'WAITING_FOR_AUTHORITY'
    assert retained['wait_contract']['gates'][:len(gates)] == gates


def test_undeclared_phase_does_not_crash_or_grant_bounded_scope(board):
    from hermes_cli.kanban_review_transition import bounded_review_goal
    tid = kb.create_task(board,title='Unmaterialized task',assignee='builder')
    assert bounded_review_goal(board,tid) is None
