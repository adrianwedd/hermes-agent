"""Read-only exact worker-session linkage; never guess from titles/cwd."""
import json
from hermes_cli.kanban_worker_observability import registered_receipt

def find_link(conn,session_id,board):
    links={}
    for row in conn.execute('SELECT r.id,r.profile,r.metadata,t.id,t.title FROM task_runs r JOIN tasks t ON t.id=r.task_id ORDER BY r.id'):
        rid,profile,raw,tid,title=row
        try:sid=json.loads(raw or '{}').get('worker_session_id')
        except (ValueError,TypeError):continue
        if not sid:sid=registered_receipt(conn,tid,rid).get('worker_session_id')
        if sid==session_id:links[tid]={'run_id':rid,'run_profile':profile,'card_id':tid,'card_title':title,'board':board,'session_id':sid}
    return list(links.values())
