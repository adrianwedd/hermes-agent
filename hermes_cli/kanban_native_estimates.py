"""Persist the existing native estimate response through audited Kanban attachments."""
import hashlib,json,time
from pathlib import Path

def scope_hash(title,body):return hashlib.sha256(json.dumps([title or '',body or ''],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def validate(response):
    if not isinstance(response,dict) or response.get('ok') is not True:raise ValueError('estimate did not succeed')
    tokens=response.get('est_tokens')
    if type(tokens) is not int or not 0<tokens<=1_000_000_000:raise ValueError('invalid token estimate')
    complexity=response.get('complexity')
    if complexity not in (None,'S','M','L'):raise ValueError('invalid complexity')
    def bounded(value,limit):
        if value is None:return None
        if not isinstance(value,str) or len(value)>limit:raise ValueError('invalid estimate text')
        return value
    return {'ok':True,'est_tokens':tokens,'complexity':complexity,'rationale':bounded(response.get('rationale'),2000),'model':bounded(response.get('model'),200)}

def read_saved(conn,task_id,title,body):
    row=conn.execute("SELECT id,stored_path FROM task_attachments WHERE task_id=? AND filename LIKE 'native-estimate-%' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
    if not row:return None
    try:
        path=Path(row['stored_path'])
        if path.stat().st_size > 65536:return None
        stored=json.loads(path.read_text());value=validate(stored)
        stamp=stored.get('created_at');digest=stored.get('scope_sha256')
        if type(stamp) is not int or stamp<=0 or not isinstance(digest,str) or len(digest)!=64:return None
        value.update(created_at=stamp,scope_sha256=digest,stale_scope=digest!=scope_hash(title,body),attachment_id=row['id'],provenance={'source':'native_estimate','trigger':'explicit_user_request','actor':'user','auxiliary_task':'kanban_estimator'})
        return value
    except (OSError,ValueError,TypeError):return None

def persist(conn,task_id,response,*,expected_scope,board=None,actor='user'):
    from hermes_cli import kanban_db as kb
    value=validate(response);task=kb.get_task(conn,task_id)
    if task is None or scope_hash(task.title,task.body)!=expected_scope:raise ValueError('task scope changed during estimation')
    value.update(created_at=int(time.time()),scope_sha256=expected_scope,provenance={'source':'native_estimate','trigger':'explicit_user_request','actor':actor,'auxiliary_task':'kanban_estimator'},stale_scope=False)
    data=json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode()
    digest=hashlib.sha256(data).hexdigest()
    aid=kb.store_attachment_bytes(conn,task_id,f'native-estimate-{value["created_at"]}-{digest[:12]}.json',data,content_type='application/json',uploaded_by='codex-native-estimator',board=board)
    value['attachment_id']=aid
    try:kb.notify_task_updated(conn,task_id,['task_estimate'],board=board)
    except Exception:pass  # Persistence succeeded; notification cannot erase that receipt.
    return value
