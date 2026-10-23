"""Behaviour contracts for native wait evidence, continuation and recovery."""
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_wait_contract import declaration, facts
from hermes_cli.kanban_wait_transitions import declare_wait, resolve_gate


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    with connect(tmp_path / "board.db") as c:
        yield c


def ready(c):
    tid = kb.create_task(c, title="Existing finite action", body="Retain original deliverable.", assignee="worker", workspace_kind="scratch")
    kb.recompute_ready(c)
    return tid


def install(c, tid, mode="attestation", extra=None):
    g = declaration("known-condition", mode, resolver="operator", parameters={"required_assertions": ["condition_verified"], **(extra or {})}, action="Obtain certified evidence", reason="Condition absent", stage="BLOCKED")
    declare_wait(c, tid, [g], {"state": "todo"}, provenance={"test": "original-native-scope"})
    kb.recompute_ready(c)
    return g


def satisfy(c, tid, g, **kw):
    return resolve_gate(c, tid, g["id"], revision=g["revision"], declaration=g["declaration"], evidence={"sha256": "a" * 64, "reference": "retained qualified evidence", "assertions": {"condition_verified": True}}, **kw)


def test_manual_gate_wakes_atomically_without_unblock_surgery(board):
    tid = ready(board);g = install(board, tid)
    assert not decide(board, kb.get_task(board, tid))["dispatchable"]
    with pytest.raises(PermissionError):kb.unblock_task(board, tid)
    after = satisfy(board, tid, g)
    assert after["workflow_stage"] == "READY" and after["dispatchable"]
    assert kb.claim_task(board, tid, claimer="worker:existing")


def test_wrong_revision_identity_and_worker_attestation_are_rejected(board, monkeypatch):
    tid = ready(board);g = install(board, tid)
    with pytest.raises(ValueError):satisfy(board, tid, {**g, "revision": 2})
    with pytest.raises(ValueError):satisfy(board, tid, {**g, "id": "wrong"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(PermissionError):satisfy(board, tid, g)
    assert not decide(board, kb.get_task(board, tid))["dispatchable"]


def test_duplicate_satisfaction_and_recovery_ticks_are_idempotent(board):
    tid = ready(board);g = install(board, tid);satisfy(board, tid, g)
    before = board.execute('select count(*) from task_events').fetchone()[0]
    satisfy(board, tid, g);kb.recompute_ready(board);kb.recompute_ready(board)
    assert board.execute('select count(*) from task_events').fetchone()[0] == before


def test_satisfying_one_gate_preserves_other_gate_and_parent_wait(board):
    parent = ready(board);tid = ready(board);kb.link_tasks(board, parent, tid)
    g = install(board, tid)
    assert satisfy(board, tid, g)["workflow_stage"] == "WAITING"
    assert not kb.claim_task(board, tid, claimer="worker:no")


def test_only_real_temporal_schedule_wakes_when_due(board, monkeypatch):
    tid = ready(board);due = time.time() + 100
    kb.schedule_task(board, tid, reason="Actual timestamp", scheduled_at=due)
    assert kb.get_task(board, tid).status == "scheduled"
    import hermes_cli.kanban_wait_contract as module
    monkeypatch.setattr(module.time, "time", lambda: due)
    kb.recompute_ready(board)
    assert decide(board, kb.get_task(board, tid))["dispatchable"]


def test_reason_only_schedule_is_explicitly_unsupported(board):
    tid = ready(board);kb.schedule_task(board, tid, reason="Someone should approve a window")
    assert kb.get_task(board, tid).status != "scheduled"
    d = decide(board, kb.get_task(board, tid))
    assert d["evaluation_mode"] == "unsupported" and not d["dispatchable"]


def test_old_gate_cannot_mask_or_revoke_later_stop(board):
    tid = ready(board);g = install(board, tid)
    # New authority event occurs after the original wait owns its state.
    board.execute("UPDATE tasks SET status='blocked',block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
    kb._append_event(board, tid, 'blocked', {'kind': 'explicit_stop', 'reason': 'Operator STOP'})
    assert decide(board, kb.get_task(board, tid))['workflow_stage'] == 'HELD'
    assert satisfy(board, tid, g)['workflow_stage'] == 'HELD'
    assert kb.get_task(board, tid).block_kind == 'explicit_stop'
    assert not kb.claim_task(board, tid, claimer='worker:no')


def test_new_operator_pending_is_not_masked_by_external_wait(board):
    tid = ready(board);g = install(board, tid)
    kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'Choose authority', 'resolver': 'operator'})
    assert decide(board, kb.get_task(board, tid))['workflow_stage'] == 'OPERATOR'
    assert satisfy(board, tid, g)['workflow_stage'] == 'OPERATOR'


def test_unsupported_refinement_changes_representation_without_grant(board):
    tid = ready(board);kb.schedule_task(board, tid, reason='Named manual condition')
    old = facts(board, tid)['gates'][0]
    new = declaration('refined-condition', 'attestation', resolver='operator',
                      parameters={'required_assertions': ['condition_verified'], 'source_condition_declaration': old['declaration']},
                      action='Record evidence', reason='Same substantive constraint')
    declare_wait(board, tid, [new], {'state': 'todo'}, provenance={'refinement': 'same source condition'},
                 refinement={old['id']: {'declaration': old['declaration'], 'replacement_id': new['id']}})
    kb.recompute_ready(board)
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    assert satisfy(board, tid, new)['dispatchable']


def test_invalidation_and_database_reopen_use_persisted_truth(board):
    tid = ready(board);g = install(board, tid);satisfy(board, tid, g)
    result = resolve_gate(board, tid, g['id'], revision=g['revision'], declaration=g['declaration'],
                          evidence={'sha256': 'b'*64, 'reference': 'failed subsequent observation', 'assertions': {'condition_invalidated': True}}, invalidate=True)
    assert not result['dispatchable']
    filename = board.execute('PRAGMA database_list').fetchone()[2]
    board.commit()
    with connect(Path(filename)) as recovered:
        kb.recompute_ready(recovered)
        assert not decide(recovered, kb.get_task(recovered, tid))['dispatchable']
        assert satisfy(recovered, tid, g)['dispatchable']


def test_operator_resolution_requires_selected_value_and_current_pending(board):
    tid = ready(board)
    eid = kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'Choose', 'resolver': 'operator'})
    eid = board.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (tid,)).fetchone()[0]
    g = declaration('current-decision', 'operator_decision', resolver='operator',
                    parameters={'pending_event_id': eid, 'required_assertions': ['condition_verified'], 'decision_fields': ['selection'], 'decision_values': {'selection': ['approve', 'decline']}, 'decision_continuations': {'approve': {'state': 'todo'}, 'decline': {'state': 'todo', 'selected_next_action': None}}},
                    action='Record selected choice', reason='Choice outstanding')
    declare_wait(board, tid, [g], {'state': 'todo'}, provenance={'pending_event_id': eid})
    with pytest.raises(ValueError):satisfy(board, tid, g)
    with pytest.raises(ValueError):satisfy(board, tid, g, decision={'selection': 'invented'})
    assert satisfy(board, tid, g, decision={'selection': 'approve'})['dispatchable']
    kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'New independent choice'})
    with pytest.raises(ValueError):satisfy(board, tid, g, decision={'selection': 'approve'})
    assert not decide(board, kb.get_task(board, tid))['dispatchable']


def test_declaration_cannot_adopt_native_stop_or_pending_decision(board):
    tid = ready(board)
    board.execute("UPDATE tasks SET status='blocked',block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
    kb._append_event(board, tid, 'blocked', {'kind': 'explicit_stop', 'reason': 'STOP'})
    due = declaration('unrelated-due-timer', 'time_due', resolver='clock', parameters={'scheduled_at': time.time()-1}, action='Temporal recheck', reason='Due')
    with pytest.raises(PermissionError):
        declare_wait(board, tid, [due], {'state': 'todo'}, provenance={'test': 'must retain STOP'})
    assert decide(board, kb.get_task(board, tid))['workflow_stage'] == 'HELD'


def test_continuation_is_not_replayed_over_later_review(board):
    tid = ready(board);g = install(board, tid);satisfy(board, tid, g)
    claim = kb.claim_task(board, tid, claimer='worker:implementation')
    assert claim
    assert kb.request_review(board, tid, summary='Retained work ready for independent review', reviewer='reviewer', expected_run_id=claim.current_run_id)
    assert decide(board, kb.get_task(board, tid))['workflow_stage'] == 'REVIEW'
    kb.recompute_ready(board)
    assert kb.get_task(board, tid).status == 'review'
    assert kb.claim_review_task(board, tid, claimer='worker:reviewer')


def test_invalidation_then_new_receipt_restores_review_not_initial_action(board):
    tid = ready(board);g = install(board, tid);satisfy(board, tid, g)
    claim = kb.claim_task(board, tid, claimer='worker:implementation')
    kb.request_review(board, tid, summary='Review pending', reviewer='reviewer', expected_run_id=claim.current_run_id)
    resolve_gate(board, tid, g['id'], revision=g['revision'], declaration=g['declaration'],
                 evidence={'sha256': 'b'*64, 'reference': 'invalidating observation', 'assertions': {'condition_invalidated': True}}, invalidate=True)
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    assert satisfy(board, tid, g)['workflow_stage'] == 'REVIEW'
    assert kb.claim_review_task(board, tid, claimer='worker:reviewer')


def test_another_satisfied_gate_cannot_bypass_exhausted_retry_budget(board):
    tid = ready(board);g = install(board, tid)
    board.execute('UPDATE tasks SET consecutive_failures=2,max_retries=2 WHERE id=?', (tid,))
    result = satisfy(board, tid, g)
    assert not result['dispatchable'] and result['workflow_stage'] == 'BLOCKED'
    assert kb.get_task(board, tid).consecutive_failures == 2
    assert not kb.claim_task(board, tid, claimer='worker:no')


def test_decline_uses_its_declared_continuation(board):
    tid = ready(board)
    kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'Choose'})
    eid = board.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (tid,)).fetchone()[0]
    g = declaration('choice', 'operator_decision', resolver='operator', parameters={
        'pending_event_id': eid, 'required_assertions': ['condition_verified'], 'decision_fields': ['selection'],
        'decision_values': {'selection': ['approve', 'decline']},
        'decision_continuations': {'approve': {'state': 'todo'}, 'decline': {'state': 'todo', 'selected_next_action': None}}},
        action='Record choice', reason='Decision outstanding')
    declare_wait(board, tid, [g], {'state': 'todo'}, provenance={'test': 'choice semantics'})
    prediction = decide(board, kb.get_task(board, tid))['state_if_satisfied_now']
    assert prediction['decision_outcomes']['approve']['state'] == 'READY'
    assert prediction['decision_outcomes']['decline']['state'] == 'PREPARE'
    result = satisfy(board, tid, g, decision={'selection': 'decline'})
    assert result['workflow_stage'] == 'PREPARE' and not result['dispatchable']


def test_and_dependencies_wake_from_native_parent_archive_events(board):
    first = ready(board);second = ready(board);child = ready(board)
    kb.link_tasks(board, first, child);kb.link_tasks(board, second, child)
    assert decide(board, kb.get_task(board, child))['workflow_stage'] == 'WAITING'
    assert kb.archive_task(board, first)
    assert decide(board, kb.get_task(board, child))['workflow_stage'] == 'WAITING'
    assert not kb.claim_task(board, child, claimer='worker:incomplete')
    assert kb.archive_task(board, second)
    assert kb.get_task(board, child).status == 'ready'
    assert kb.claim_task(board, child, claimer='worker:parents-terminal')


def test_predicate_survives_fresh_process_restart(board, tmp_path):
    import subprocess
    import sys
    tid = ready(board);g = install(board, tid)
    filename = board.execute('PRAGMA database_list').fetchone()[2]
    program = '''
import json,sys
from pathlib import Path
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_wait_transitions import resolve_gate
from hermes_cli.kanban_decision import decide
with connect(Path(sys.argv[1])) as conn:
 kb.recompute_ready(conn)
 assert not decide(conn,kb.get_task(conn,sys.argv[2]))['dispatchable']
 gate=json.loads(sys.argv[3])
 result=resolve_gate(conn,sys.argv[2],gate['id'],revision=gate['revision'],declaration=gate['declaration'],evidence={'sha256':'a'*64,'reference':'isolated restarted process','assertions':{'condition_verified':True}})
 assert result['dispatchable']
 kb.recompute_ready(conn)
 assert kb.claim_task(conn,sys.argv[2],claimer='worker:restarted')
'''
    result = subprocess.run([sys.executable, '-c', program, filename, tid, json.dumps(g)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]


def test_accepted_output_dependency_consumes_verified_artifact_not_parent_terminal(board, tmp_path):
    from hermes_cli.kanban_completion_evidence import contract_record
    from hermes_cli.kanban_dependency_output import declare, accept, certify_output
    import hashlib
    parent = ready(board);child = ready(board);kb.link_tasks(board, parent, child)
    artifact = tmp_path/'retained-result.md';artifact.write_text('A bounded accepted result.\n')
    aid = kb.add_attachment(board, parent, filename=artifact.name, stored_path=str(artifact))
    cid, contract = contract_record(board, parent)
    result = certify_output(board, parent, contract_event_id=cid, artifact_attachment_id=aid, criterion_id=contract['criteria'][0],
                            evidence={'reference': 'Qualified independent fixture review', 'assertions': {'output_verified': True, 'applicability_verified': True, 'authority_verified': True}})
    identity = 'sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert result['evidence_identity'] == identity
    declare(board, child, parent, 'accepted-report', evidence_identity=identity, requirement='Accepted bounded producer report')
    did = board.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='dependency_requirement'", (child,)).fetchone()[0]
    assert not decide(board, kb.get_task(board, child))['dispatchable']
    with pytest.raises(ValueError):accept(board, child, 'wrong-condition', declaration_event_id=did, producer_evidence_event_id=result['producer_evidence_event_id'])
    accept(board, child, 'accepted-report', declaration_event_id=did, producer_evidence_event_id=result['producer_evidence_event_id'])
    assert kb.get_task(board, parent).status not in {'done', 'archived'}
    assert kb.claim_task(board, child, claimer='worker:accepted-output')


def test_revoking_stop_exposes_a_distinct_remaining_external_gate(board):
    tid = ready(board);external = install(board, tid)
    board.execute("UPDATE tasks SET status='blocked',block_kind='explicit_stop',dispatch_eligible=0 WHERE id=?", (tid,))
    kb._append_event(board, tid, 'blocked', {'kind': 'explicit_stop', 'reason': 'Stop remains explicit'})
    kb.recompute_ready(board)
    stop = next(g for g in facts(board, tid)['remaining_gates'] if g['evaluator'] == 'explicit_stop')
    result = resolve_gate(board, tid, stop['id'], revision=stop['revision'], declaration=stop['declaration'],
                          evidence={'sha256':'c'*64,'reference':'isolated authorised revocation','assertions':{'explicit_stop_revoked':True}})
    assert result['workflow_stage'] == 'BLOCKED' and not result['dispatchable']
    assert [g['id'] for g in facts(board, tid)['remaining_gates']] == [external['id']]
    assert satisfy(board, tid, external)['dispatchable']


def test_operator_resolution_preserves_terminal_provider_hold(board):
    tid = ready(board)
    kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'Choose'})
    kb._append_event(board, tid, 'gave_up', {'terminal_provider': True, 'sticky': True, 'retry_status': 'ready'})
    board.execute("UPDATE tasks SET status='blocked',consecutive_failures=1,max_retries=5 WHERE id=?", (tid,))
    kb.recompute_ready(board)
    operator = next(g for g in facts(board, tid)['remaining_gates'] if g['evaluator'] == 'operator_decision')
    result = resolve_gate(board, tid, operator['id'], revision=operator['revision'], declaration=operator['declaration'],
                          evidence={'sha256':'a'*64,'reference':'selected authorised outcome','assertions':{'decision_authorised':True,'selected_continuation_authorised':True}}, decision={'selection':'selected'})
    assert result['workflow_stage'] == 'BLOCKED' and not result['dispatchable']
    assert any(g['id'].startswith('provider:') for g in facts(board, tid)['remaining_gates'])


@pytest.mark.parametrize('params', [
    {'required_assertions': ['probe_succeeded']},
    {'probe_name': 'unregistered', 'required_assertions': ['probe_succeeded']},
    {'probe_name': 'notebooklm_auth', 'required_assertions': ['probe_succeeded'], 'decision_values': None},
    {'probe_name': 'notebooklm_auth', 'required_assertions': ['probe_succeeded'], 'decision_continuations': []},
])
def test_malformed_observation_is_rejected_before_persistence(board, params):
    tid = ready(board)
    gate = declaration('probe', 'observation', resolver='observer', parameters=params, action='Observe', reason='Absent')
    with pytest.raises(ValueError):
        declare_wait(board, tid, [gate], {'state': 'todo'}, provenance={'test': 'schema'})
    assert facts(board, tid) is None


def test_completion_declaration_cannot_create_an_unvalidated_wait(board):
    from hermes_cli.kanban_completion_evidence import contract_record, set_requirements
    tid = ready(board)
    _, contract = contract_record(board, tid)
    task = kb.get_task(board, tid)
    with pytest.raises(ValueError, match='native wait declaration'):
        set_requirements(board, tid, {**contract, 'wait_contract': {'version': 1}}, expected_status=task.status, expected_assignee=task.assignee, expected_run_id=task.current_run_id)
    assert facts(board, tid) is None


def test_changed_external_condition_requires_new_bound_evidence(board):
    from hermes_cli.kanban_completion_evidence import contract_record
    from hermes_cli.kanban_wait_migration import migrate
    tid = ready(board)
    _, contract = contract_record(board, tid)
    first = {'condition_id': 'window', 'exclusive': True, 'runtime': 'original'}
    kb._append_event(board, tid, 'completion_requirements', {**contract, 'external_conditions': [first]})
    migrate(board, [tid]);gate = facts(board, tid)['gates'][0]
    evidence = {'sha256': 'a' * 64, 'reference': 'isolated authorised window', 'assertions': {key: True for key in gate['parameters']['required_assertions']}}
    resolve_gate(board, tid, gate['id'], revision=gate['revision'], declaration=gate['declaration'], evidence=evidence)
    assert decide(board, kb.get_task(board, tid))['dispatchable']
    _, current = contract_record(board, tid)
    kb._append_event(board, tid, 'completion_requirements', {**current, 'external_conditions': [{**first, 'runtime': 'stronger'}]})
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    with pytest.raises(ValueError, match='Condition changed'):
        resolve_gate(board, tid, gate['id'], revision=gate['revision'], declaration=gate['declaration'], evidence=evidence)
    kb.recompute_ready(board)
    revised = facts(board, tid)['gates'][0]
    assert revised['revision'] == gate['revision'] + 1 and not revised['satisfied']
    resolve_gate(board, tid, revised['id'], revision=revised['revision'], declaration=revised['declaration'], evidence={**evidence, 'sha256': 'b' * 64})
    assert decide(board, kb.get_task(board, tid))['dispatchable']


def test_worker_ancestry_cannot_drop_environment_to_gain_authority(board):
    import os
    from hermes_cli.kanban_wait_authority import reject_worker_ancestry
    tid = ready(board)
    board.execute('INSERT INTO task_runs(task_id,status,started_at,worker_pid) VALUES(?,?,?,?)', (tid, 'running', time.time(), os.getppid()))
    with pytest.raises(PermissionError, match='ancestry'):
        reject_worker_ancestry(board)


def test_native_workspace_provision_after_parent_completion_needs_no_poke(board, tmp_path):
    from hermes_cli.kanban_completion_evidence import contract_record
    parent = ready(board);tid = ready(board)
    path = tmp_path / 'owned-evidence'
    board.execute('UPDATE tasks SET workspace_kind=?,workspace_path=?,status=? WHERE id=?', ('dir', str(path), 'todo', tid))
    kb.link_tasks(board, parent, tid)
    kb.recompute_ready(board)
    assert not path.exists() and decide(board, kb.get_task(board, tid))['workflow_stage'] == 'WAITING'
    # Native archive is terminal; it triggers ordinary recompute and provisioning.
    kb.archive_task(board, parent)
    assert path.is_dir()
    assert decide(board, kb.get_task(board, tid))['dispatchable']
    assert board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='workspace_prepared'", (tid,)).fetchone()


def test_native_observer_invalidates_expired_auth_and_can_observe_recovery(board):
    from hermes_cli.kanban_wait_observation import observation_gate, observe
    tid = ready(board)
    gate = observation_gate('auth', {'condition_probe': 'notebooklm_auth'}, 1)
    declare_wait(board, tid, [gate], {'state': 'todo'}, provenance={'test': 'auth'})
    good = {'probe': 'notebooklm_auth', 'exit_code': 0}
    bad = {'probe': 'notebooklm_auth', 'exit_code': 1}
    observe(board, probes={'notebooklm_auth': lambda: good})
    assert decide(board, kb.get_task(board, tid))['dispatchable']
    observe(board, probes={'notebooklm_auth': lambda: bad})
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    observe(board, probes={'notebooklm_auth': lambda: good})
    assert decide(board, kb.get_task(board, tid))['dispatchable']


def test_registered_auth_probe_negative_result_invalidates_existing_receipt(board, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_resume
    from hermes_cli.kanban_wait_observation import observation_gate, observe
    tid = ready(board)
    gate = observation_gate('real-auth', {'condition_probe': 'notebooklm_auth'}, 1)
    declare_wait(board, tid, [gate], {'state': 'todo'}, provenance={'test': 'registered-real-probe'})
    monkeypatch.setattr(kanban_resume.shutil, 'which', lambda name: '/isolated/nlm')
    monkeypatch.setattr(kanban_resume.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='1 notebook', stderr=''))
    observe(board)
    assert decide(board, kb.get_task(board, tid))['dispatchable']
    monkeypatch.setattr(kanban_resume.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='sensitive login failure', stderr=''))
    observe(board)
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    assert 'sensitive login failure' not in str(board.execute("SELECT payload FROM task_events WHERE kind='wait_gate_invalidated'").fetchall())


def test_wait_cli_inspection_uses_native_connection_boundary(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli.kanban_wait_cli import dispatch
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'native-cli-home'))
    with connect(board='default') as conn:
        tid = ready(conn);install(conn, tid)
    assert dispatch(SimpleNamespace(board='default', task_id=tid, wait_operation='inspect')) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['predicate']['identity'] == 'known-condition'
    assert payload['current_unblock_action'] == 'Obtain certified evidence'


def test_worker_request_changes_tool_preserves_review_boundary(board, monkeypatch):
    from contextlib import contextmanager
    from tools import kanban_tools as kt
    tid = ready(board)
    claimed = kb.claim_task(board, tid, claimer='worker:implementation')
    assert kb.request_review(board, tid, summary='Retained work', reviewer='reviewer', expected_run_id=claimed.current_run_id)
    reviewer = kb.claim_review_task(board, tid, claimer='reviewer:independent')
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(reviewer.current_run_id))
    monkeypatch.setenv('HERMES_PROFILE', 'reviewer')
    @contextmanager
    def isolated_board(*a, **kw):
        yield kb, board
    monkeypatch.setattr(kt, '_board', isolated_board)
    result = json.loads(kt._handle_request_changes({'reason': 'Address the retained contrary evidence'}))
    assert result['ok'] and kb.get_task(board, tid).assignee == 'worker'


def test_native_preparation_prediction_matches_actual_provision(board, tmp_path):
    tid = ready(board)
    path = tmp_path / 'new-owned-source'
    board.execute('UPDATE tasks SET workspace_kind=?,workspace_path=?,status=? WHERE id=?', ('dir', str(path), 'todo', tid))
    before = decide(board, kb.get_task(board, tid))
    assert before['workflow_stage'] == 'PREPARE'
    assert before['state_if_satisfied_now']['state'] == 'READY'
    kb.recompute_ready(board)
    after = decide(board, kb.get_task(board, tid))
    assert path.is_dir() and after['workflow_stage'] == before['state_if_satisfied_now']['state']
    assert after['dispatchable'] == before['state_if_satisfied_now']['dispatchable']


def test_temporal_wait_preserves_native_reviewer_run_phase(board):
    tid = ready(board)
    claim = kb.claim_task(board, tid, claimer='worker:implementation')
    kb.request_review(board, tid, summary='Retained review handoff', reviewer='reviewer', expected_run_id=claim.current_run_id)
    reviewer = kb.claim_review_task(board, tid, claimer='reviewer:independent')
    due = time.time() + 60
    assert kb.schedule_task(board, tid, scheduled_at=due, expected_run_id=reviewer.current_run_id)
    assert not decide(board, kb.get_task(board, tid))['dispatchable']
    assert facts(board, tid)['continuation']['state'] == 'review'
    # Clock observation is the real evaluator; no manual gate receipt is inserted.
    from unittest.mock import patch
    with patch('hermes_cli.kanban_wait_contract.time.time', return_value=due + 1):
        kb.recompute_ready(board)
        assert kb.get_task(board, tid).status == 'review'
        assert kb.claim_review_task(board, tid, claimer='reviewer:resumed')


def test_new_native_review_block_preserves_review_continuation(board):
    from hermes_cli.kanban_wait_migration import migrate
    tid = ready(board)
    run = kb.claim_task(board, tid, claimer='worker:implementation')
    kb.request_review(board, tid, summary='Retained review work', reviewer='reviewer', expected_run_id=run.current_run_id)
    reviewer = kb.claim_review_task(board, tid, claimer='reviewer:waiting')
    assert kb.block_task(board, tid, kind='capability', reason='Qualified review evidence remains necessary', expected_run_id=reviewer.current_run_id)
    migrate(board, [tid])
    wait = facts(board, tid)
    assert wait['continuation']['state'] == 'review'
    g = wait['gates'][0]
    evidence = {'sha256': 'a'*64, 'reference': 'isolated qualified review evidence', 'assertions': {k: True for k in g['parameters']['required_assertions']}}
    after = resolve_gate(board, tid, g['id'], revision=g['revision'], declaration=g['declaration'], evidence=evidence)
    assert after['workflow_stage'] == 'REVIEW' and after['dispatchable']
    assert kb.claim_review_task(board, tid, claimer='reviewer:resumed')


def test_old_approval_cannot_replay_implementation_after_new_review_block(board):
    tid = ready(board)
    kb._append_event(board, tid, 'operator_decision_pending', {'reason': 'Explicit branch'})
    eid = board.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (tid,)).fetchone()[0]
    op = declaration('approval', 'operator_decision', resolver='operator', parameters={
        'pending_event_id': eid, 'required_assertions': ['condition_verified'], 'decision_fields': ['selection'],
        'decision_values': {'selection': ['approve']}, 'decision_continuations': {'approve': {'state': 'todo'}}}, action='Record choice', reason='Pending')
    declare_wait(board, tid, [op], {'state': 'todo'}, provenance={'test': 'initial approval'})
    satisfy(board, tid, op, decision={'selection': 'approve'})
    run = kb.claim_task(board, tid, claimer='worker:implementation')
    kb.request_review(board, tid, summary='Completed implementation', reviewer='reviewer', expected_run_id=run.current_run_id)
    reviewer = kb.claim_review_task(board, tid, claimer='reviewer:independent')
    kb.block_task(board, tid, kind='capability', reason='Current review needs qualified evidence', expected_run_id=reviewer.current_run_id)
    kb.recompute_ready(board)
    wait = facts(board, tid)
    assert wait['continuation']['state'] == 'review'
    gate = wait['remaining_gates'][0]
    evidence = {'sha256': 'b'*64, 'reference': 'isolated qualified review evidence', 'assertions': {key: True for key in gate['parameters']['required_assertions']}}
    after = resolve_gate(board, tid, gate['id'], revision=gate['revision'], declaration=gate['declaration'], evidence=evidence)
    assert after['workflow_stage'] == 'REVIEW' and after['dispatchable']


def test_preparation_predicate_preserves_existing_legacy_qualification_policy(board):
    from hermes_cli.kanban_completion_evidence import contract_record
    tid = ready(board)
    _, contract = contract_record(board, tid)
    legacy = {k: v for k, v in contract.items() if k != 'qualified_for_dispatch'}
    legacy['selected_next_action'] = None
    kb._append_event(board, tid, 'completion_requirements', legacy)
    before = decide(board, kb.get_task(board, tid))
    assert before['workflow_stage'] == 'PREPARE'
    assert not all(before['predicate']['requirements'].values())
    kb._append_event(board, tid, 'completion_requirements', {**legacy, 'selected_next_action': contract['selected_next_action']})
    kb.recompute_ready(board)
    after = decide(board, kb.get_task(board, tid))
    assert after['dispatchable'] and after['workflow_stage'] == before['state_if_satisfied_now']['state']
