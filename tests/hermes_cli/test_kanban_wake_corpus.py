"""Counterfactual acceptance over the native 2026-10-07 55-card audit cohort."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_decision import decide
from hermes_cli.kanban_wait_contract import facts
from hermes_cli.kanban_wait_migration import migrate
from hermes_cli.kanban_wait_transitions import resolve_gate

CORPUS = json.loads((Path(__file__).parents[1] / 'fixtures/kanban_wake_55.json').read_text())


@pytest.fixture
def isolated(tmp_path, monkeypatch, request):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'home'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    # Native workspace paths are evidence, never execution targets in a copied board.
    import hermes_cli.kanban_native_preparation as prep
    monkeypatch.setattr(prep, 'prepare_native_workspaces', lambda c: None)
    card = request.node.callspec.params["card"]
    retained = {card["id"], *(d["id"] for d in card["deps"])}
    with connect(tmp_path/'fixture.db') as conn:
        native_columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for original in CORPUS['tasks']:
            if original["id"] not in retained:
                continue
            row = {k: v for k, v in original.items() if k in native_columns}
            if row.get('workspace_path'):
                row['workspace_path'] = str(tmp_path / 'workspaces' / row['id'])
                audit = next((a for a in CORPUS['audit'] if a['id'] == row['id']), {})
                if audit.get('after_claimable'):
                    Path(row['workspace_path']).mkdir(parents=True)
            cols = list(row)
            conn.execute('INSERT INTO tasks ('+','.join(cols)+') VALUES ('+','.join('?'*len(cols))+')', tuple(row[c] for c in cols))
        for event in CORPUS['events']:
            if event["task_id"] not in retained:
                continue
            cols = list(event)
            conn.execute('INSERT INTO task_events ('+','.join(cols)+') VALUES ('+','.join('?'*len(cols))+')', tuple(event[c] for c in cols))
        for link in CORPUS['links']:
            if link["child_id"] != card["id"] or link["parent_id"] not in retained:
                continue
            cols = list(link)
            conn.execute('INSERT INTO task_links ('+','.join(cols)+') VALUES ('+','.join('?'*len(cols))+')', tuple(link[c] for c in cols))
        conn.commit()
        yield conn


@pytest.mark.parametrize('card', CORPUS['audit'], ids=lambda c: c['id']+':'+c['kind'])
def test_each_native_gate_wakes_or_exposes_remaining_gate(isolated, card):
    c = isolated;tid = card['id']
    migrate(c, [tid]);kb.recompute_ready(c)
    before = decide(c, kb.get_task(c, tid))
    assert not before['dispatchable'], (tid, before['workflow_stage'])
    assert all(key in before for key in ('why_not_executable', 'predicate', 'evaluation_mode', 'resolver', 'wake_trigger', 'current_unblock_action', 'post_gate_continuation', 'state_if_satisfied_now'))
    if card['kind'] == 'dependency':
        # Terminal native parent transitions, including AND, are covered separately
        # through complete_task; here preserve every parent except this exact predicate.
        for dep in card['deps']:
            c.execute("UPDATE tasks SET status='done',current_run_id=NULL,worker_pid=NULL,claim_lock=NULL WHERE id=?", (dep['id'],))
        c.commit();kb.recompute_ready(c)
        after = decide(c, kb.get_task(c, tid))
    else:
        wait = facts(c, tid)
        assert wait and wait['remaining_gates']
        gate = next(g for g in wait['remaining_gates'] if g['evaluator'] == ('explicit_stop' if card['kind'] == 'stop' else 'operator_decision' if card['kind'] == 'operator' else 'attestation'))
        evidence = {'sha256': 'a'*64, 'reference': 'ISOLATED counterfactual fixture only', 'assertions': {key: True for key in gate['parameters']['required_assertions']}}
        after = resolve_gate(c, tid, gate['id'], revision=gate['revision'], declaration=gate['declaration'], evidence=evidence, decision={'selection': 'fixture-authorised-selection'} if gate['evaluator'] == 'operator_decision' else None)
        assert gate['id'] not in [g.get('id') for g in after['remaining_gates']]
    assert before['state_if_satisfied_now']['state'] == after['workflow_stage'], (tid, before['state_if_satisfied_now'], after['workflow_stage'])
    assert before['state_if_satisfied_now']['dispatchable'] == after['dispatchable']
    assert after['workflow_stage'] != 'WORKFLOW_FAULT', (tid, after['reason'])
    assert after['dispatchable'] == (after['workflow_stage'] in {'READY', 'REVIEW'}), (tid, after['workflow_stage'], after['reason'])
    if after['workflow_stage'] == 'PREPARE':
        assert after['current_unblock_action'] and after['reason']
        assert not all(after['predicate']['requirements'].values())
    claim = kb.claim_review_task if after['workflow_stage'] == 'REVIEW' else kb.claim_task
    assert bool(claim(c, tid, claimer='worker:fixture')) == after['dispatchable']
