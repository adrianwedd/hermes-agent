"""Passive delivery facts from exact-card structured publication attachments."""
from __future__ import annotations
import hashlib,json,re
from pathlib import Path
from datetime import datetime
_SHA=re.compile(r'[0-9a-f]{40}')
_REPO=re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
_NAMES={'published-delivery-receipt.json','delivery-receipt.json'}
def _text(value,limit=1000):
    if not isinstance(value,str):return None
    value=re.sub(r'(?:/Users/|/private/|/tmp/|file:)[^\s,;]+','[local path]',value)
    value=re.sub(r'(?i)\b(token|api[_ -]?key|password|secret|authorization)\s*[:=]\s*\S+',r'\1=[redacted]',value)
    return value[:limit]
def _sha(value):return value if isinstance(value,str) and _SHA.fullmatch(value) else None
def _time(value):
    if not isinstance(value,str):return None
    try:datetime.fromisoformat(value.replace('Z','+00:00'));return value
    except ValueError:return None
def acceptance_text(value,publication_verified):
    note=_text(value)
    if publication_verified and note and re.search(r'no commit SHA/PR exists',note,re.I):
        return 'Historical acceptance note conflicts with verified publication above. Required acceptance evidence still needs independent review.'
    return note
def normalize_receipt(raw,task_id,source):
    if not isinstance(raw,dict) or raw.get('card',raw.get('card_id'))!=task_id:return None
    repo=raw.get('repo',raw.get('repository'))
    if not isinstance(repo,str) or not _REPO.fullmatch(repo):return None
    sha=_sha(raw.get('commit_sha'));checked=_time(raw.get('verified_at_utc'))
    pr=raw.get('verified_remote_pr');pr=pr if isinstance(pr,dict) else {}
    url=raw.get('pr_url');pr_url=url if isinstance(url,str) and re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/pull/[1-9][0-9]*',url) else None
    head=_sha(pr.get('headRefOid'));pr_verified=bool(checked and sha and head==sha and pr.get('url')==pr_url and pr_url)
    # A commit, a successful push claim, or a PR head is NOT proof of the branch ref.
    branch=_text(raw.get('branch'),200);remote=raw.get('verified_remote_branch');remote=remote if isinstance(remote,dict) else {}
    remote_head=_sha(remote.get('head_sha'));branch_checked=_time(remote.get('checked_at_utc'));branch_verified=bool(branch_checked and sha and remote_head==sha and remote.get('branch')==branch and remote.get('repo')==repo)
    issue=raw.get('issue');issue=issue if isinstance(issue,str) and re.fullmatch(r'https://github\.com/'+re.escape(repo)+r'/issues/[1-9][0-9]*',issue) else None
    return {'repository':repo,'commit_sha':sha,'branch':branch,'remote_branch_head':remote_head if branch_verified else None,'remote_branch_checked_at':branch_checked if branch_verified else None,'push_status':'verified' if branch_verified else 'reported_unverified' if raw.get('pushed') is True else 'unknown','pr_url':pr_url,'pr_head_sha':head if pr_verified else None,'pr_verified':pr_verified,'pr_state':pr.get('state') if pr_verified else None,'pr_draft':pr.get('isDraft') if pr_verified and type(pr.get('isDraft')) is bool else None,'issue_url':issue,'acceptance':acceptance_text(raw.get('tests'),pr_verified),'acceptance_basis':'Publisher receipt; tests not re-executed by UI','checked_at':checked,'source':source}
def delivery_facts(conn,task_id):
    for row in conn.execute('SELECT id,filename,stored_path FROM task_attachments WHERE task_id=? ORDER BY id DESC',(task_id,)):
        aid,name,path=row
        if name not in _NAMES:continue
        try:
            p=Path(path)
            if p.stat().st_size>1_000_000:continue
            data=p.read_bytes();raw=json.loads(data)
        except (OSError,ValueError,TypeError):continue
        source={'attachment_id':aid,'filename':name,'sha256':hashlib.sha256(data).hexdigest(),'basis':'Structured exact-card publication receipt'}
        fact=normalize_receipt(raw,task_id,source)
        if fact:return fact
    return None
