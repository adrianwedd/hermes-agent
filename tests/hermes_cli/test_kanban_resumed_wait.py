"""Native resolution supersedes old error text, preserving failure budgets and gates."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_resumed_wait import resolved_block_supersedes_error


def test_consumed_current_resolution_only(tmp_path, monkeypatch):
    wait_contract = pytest.importorskip('hermes_cli.kanban_wait_contract', reason='optional native-wait integration')
    from hermes_cli.kanban_wait_transitions import resolve_gate
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'home'))
    for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK'):
        monkeypatch.delenv(key, raising=False)
    with connect(tmp_path/'board.db') as c:
        tid=kb.create_task(c,title='Review after activation',assignee='reviewer',workspace_kind='scratch')
        kb.recompute_ready(c)
        kb.claim_task(c,tid)
        kb.block_task(c,tid,reason='Requires authenticated rendered check',kind='capability')
        kb.recompute_ready(c)
        c.execute("UPDATE tasks SET last_failure_error='old quota wall',consecutive_failures=1 WHERE id=?",(tid,));c.commit()
        assert not resolved_block_supersedes_error(c,tid,'blocked')
        assert check_respawn_guard(c,tid,lane='review')=='blocker_auth'
        gate=wait_contract.facts(c,tid)['remaining_gates'][0]
        kwargs=dict(revision=gate['revision'],declaration=gate['declaration'],evidence={'sha256':'a'*64,'reference':'retained-rendered-receipt','assertions':{k:True for k in gate['parameters']['required_assertions']}})
        resolve_gate(c,tid,gate['id'],**kwargs)
        assert resolved_block_supersedes_error(c,tid,'blocked')
        assert check_respawn_guard(c,tid,lane='review') is None
        assert not resolved_block_supersedes_error(c,tid,'spawn_failed')
        assert c.execute('SELECT last_failure_error,consecutive_failures FROM tasks WHERE id=?',(tid,)).fetchone()[:]==('old quota wall',1)
        # Invalidation or a later blocker cannot reuse that acceptance.
        resolve_gate(c,tid,gate['id'],**{**kwargs,'evidence':{**kwargs['evidence'],'assertions':{'condition_invalidated':True}},'invalidate':True})
        assert not resolved_block_supersedes_error(c,tid,'blocked')
