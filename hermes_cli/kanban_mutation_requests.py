"""Revision-bound MCP mutations using the native task event log, without a new store."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path

_request = ContextVar('kanban_mutation_request', default=None)

class RevisionConflict(ValueError):
    pass

def revision(conn, task_id):
    return int(conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?',(task_id,)).fetchone()[0])

def receipt(conn, task_id, request_id, actor, digest):
    rows=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind IN ('mutation_request_committed','mutation_request_result') ORDER BY id DESC",(task_id,))
    for row in rows:
        payload=json.loads(row[0])
        if payload.get('request_id') != request_id or payload.get('actor') != actor:
            continue
        if payload.get('digest') != digest:
            raise RevisionConflict('request_id was already used with different arguments')
        return payload['result']
    return None

@contextmanager
def mutation_request(conn, task_id, expected_revision, request_id, action, args, actor):
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise PermissionError('Dispatcher workers cannot invoke the external operator mutation surface')
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('expected_revision must be the freshly read integer event revision')
    if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>200:
        raise ValueError('request_id must be a nonempty bounded idempotency key')
    from hermes_cli.kanban_db_connect import _main_db_file
    digest=hashlib.sha256(json.dumps({'action':action,'args':args},sort_keys=True,default=str).encode()).hexdigest()
    state={'task_id':task_id,'expected_revision':expected_revision,'request_id':request_id,'actor':actor,'digest':digest,'db':str(Path(_main_db_file(conn)).resolve()),'committed':False,'replay':receipt(conn,task_id,request_id,actor,digest)}
    token=_request.set(state)
    try:
        yield state
    finally:
        _request.reset(token)

def before_write(conn):
    state=_request.get()
    if state is None or state['committed']:
        return None
    from hermes_cli.kanban_db_connect import _main_db_file
    if str(Path(_main_db_file(conn)).resolve()) != state['db']:
        raise RevisionConflict('mutation changed board scope')
    replay=receipt(conn,state['task_id'],state['request_id'],state['actor'],state['digest'])
    if replay is not None:
        state['replay']=replay
        raise RevisionConflict('request already committed; return its retained receipt')
    if revision(conn,state['task_id']) != state['expected_revision']:
        raise RevisionConflict('task event revision changed; reread before mutation')
    return (state,conn.total_changes)

def before_commit(conn, checked):
    if checked is None:
        return
    state,initial_changes=checked
    if conn.total_changes==initial_changes:
        return
    from hermes_cli import kanban_db as kb
    task=kb.get_task(conn,state['task_id'])
    result={'ok':False,'error':'committed_result_unavailable','operation_committed':True,'task_id':state['task_id'],'status':task.status,'priority':task.priority,'request_id':state['request_id'],'detail':'Mutation committed; do not repeat it. Exact result recording is pending or interrupted.'}
    kb._append_event(conn,state['task_id'],'mutation_request_committed',{'request_id':state['request_id'],'actor':state['actor'],'digest':state['digest'],'expected_revision':state['expected_revision'],'result':result})
    state['pending_result']=result

def after_commit(checked):
    if checked is not None and 'pending_result' in checked[0]:
        checked[0]['committed']=True


def retain_result(conn, state, rendered):
    """Append exact result after a commit. Interrupted recording never repeats a mutation."""
    if not state['committed']:
        return
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    result=json.loads(rendered)
    with write_txn(conn):
        kb._append_event(conn,state['task_id'],'mutation_request_result',{'request_id':state['request_id'],'actor':state['actor'],'digest':state['digest'],'result':result})
