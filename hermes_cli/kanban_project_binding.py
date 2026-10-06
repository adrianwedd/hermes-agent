"""Guarded metadata-only project binding for an existing inactive card."""
from __future__ import annotations

def bind_project(conn,task_id,*,project_id,expected_status,expected_assignee,expected_workspace_path,expected_project_id,board=None):
    from hermes_cli import kanban_db as kb,projects_db as pdb
    from hermes_cli.kanban_db_connect import write_txn
    if not isinstance(project_id,str) or not project_id.strip():raise ValueError('project_id must identify an existing project')
    with pdb.connect_closing() as pc:
        project=pdb.get_project(pc,project_id.strip())
    if project is None or project.archived:raise ValueError('project is missing or archived')
    expected=(expected_status,expected_assignee,expected_workspace_path,expected_project_id)
    with write_txn(conn):
        row=conn.execute('SELECT status,assignee,workspace_path,project_id,current_run_id,worker_pid,claim_lock FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row is None:return False
        if row[0]=='running' or any(row[i] is not None for i in (4,5,6)):raise RuntimeError('cannot bind an owned card')
        if tuple(row[:4])!=expected:raise RuntimeError('card snapshot changed; reread before binding')
        if row[3]==project.id:return True
        changed=conn.execute('UPDATE tasks SET project_id=? WHERE id=? AND status=? AND assignee IS ? AND workspace_path IS ? AND project_id IS ? AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL',(project.id,task_id,*expected)).rowcount
        if changed!=1:raise RuntimeError('card changed during project binding')
        kb._append_event(conn,task_id,'project_bound',{'previous_project_id':row[3],'project_id':project.id,'preserved':['status','assignee','workspace_path','workspace_kind','repo_path','branch_name'],'source':'guarded_existing_card_binding'})
    kb.notify_task_updated(conn,task_id,['project_id'],board=board)
    return True
