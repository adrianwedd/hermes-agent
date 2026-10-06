from hermes_cli.kanban_worker_observability import registered_receipt
"""Read-only, credential-free card facts. No workers, model calls or lifecycle writes."""
import json,re

def text(value,limit=240):
    value=str(value or '')
    value=re.sub(r'https?://\S+', '[link]', value)
    value=re.sub(r'(?i)\b(token|api[_ -]?key|password|secret|authorization)\s*[:=]\s*\S+', r'\1=[redacted]',value)
    return ' '.join(value.split())[:limit]

def model_facts(config,task,preset_resolver):
    primary=dict(config.get('model') or {})
    if task.get('model_override'):primary['default']=task['model_override']
    if task.get('provider_override'):primary['provider']=task['provider_override']
    provider=primary.get('provider');model=primary.get('default') or primary.get('model')
    def label(slot):
        if not isinstance(slot,dict):return 'Unknown'
        return text(' / '.join(str(x) for x in [slot.get('provider'),slot.get('model') or slot.get('default')] if x),120) or 'Unknown'
    if provider=='moa':
        try:
            preset=preset_resolver(config.get('moa') or {},model)
            refs=[label(x) for x in preset.get('reference_models',[]) if x.get('enabled',True)]
            return dict(primary='MoA · '+label(preset.get('aggregator')),advisers=refs,basis='Configured route; observed runtime may differ')
        except Exception:return dict(primary='MoA · unknown aggregator',advisers=[],basis='Configured route unresolved')
    return dict(primary=label({'provider':provider,'model':model}),advisers=[],basis='Configured route; observed runtime may differ')

def card_facts(conn,tasks,config_for,preset_resolver):
    from hermes_cli.kanban_delivery_receipts import delivery_facts
    from hermes_cli.kanban_dispatch_facts import TERMINAL_STATUSES, dispatch_facts
    result={};configs={}
    for task in tasks:
        t=dict(task);tid=t['id'];status=t['status'];reason=None
        if status=='blocked':
            note=conn.execute("SELECT body FROM task_comments WHERE task_id=? AND body LIKE 'GENUINE BLOCKER (capacity is not the blocker):%' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
            if note:reason=text(note[0].split(':',1)[1])
            if not reason:
                event=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
                try:raw=json.loads(event[0] or '{}').get('reason') if event else None
                except (TypeError,ValueError):raw=None
                if raw and raw!='initial_status':reason=text(raw)
        failure=text(t.get('last_failure_error')) if status=='blocked' and not reason else None
        wait={'todo':'Awaiting preparation','ready':'Awaiting dispatch','scheduled':'Awaiting scheduled time','triage':'Awaiting specification'}.get(status)
        blocker={'label':'Blocker' if reason else 'Last recorded failure' if failure else 'Blocker unknown','detail':reason or failure or 'No explicit blocker evidence recorded'} if status=='blocked' else ({'label':'Waiting','detail':wait} if wait else None)
        # A terminal card's retained run summary is HISTORY, not progress: rendered
        # as "Progress: …" under a finished card it reads as work still in flight.
        # Withhold it for terminal statuses, the same way plugin_api.get_board
        # withholds the child rollup. Only explicit worker comments from the
        # current run count as progress.
        progress=None
        if status not in TERMINAL_STATUSES:
            if t.get('assignee') and t.get('current_run_id'):
                run=conn.execute('SELECT started_at FROM task_runs WHERE id=?',(t['current_run_id'],)).fetchone()
                if run:
                    note=conn.execute('SELECT body,created_at FROM task_comments WHERE task_id=? AND author=? AND created_at>=? ORDER BY id DESC LIMIT 1',(tid,t['assignee'],run[0])).fetchone()
                    if note:progress={'text':text(note[0]),'at':note[1],'basis':'Worker comment during current run'}
            if not t.get('current_run_id'):
                run=conn.execute("SELECT summary,ended_at FROM task_runs WHERE task_id=? AND summary IS NOT NULL ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
                if run and run[0]:progress={'text':text(run[0]),'at':run[1],'basis':'Latest retained run handoff'}
        owner=t.get('assignee')
        if owner not in configs:
            try:configs[owner]=config_for(owner or 'default')
            except Exception:configs[owner]={}
        result[tid]={'dispatch':dispatch_facts(conn,t),'blocker':blocker,'progress':progress,'delivery':delivery_facts(conn,tid),'heartbeat_at':t.get('last_heartbeat_at'),'model':model_facts(configs[owner],t,preset_resolver)}
    return result

def native_config_for(profile):
    from pathlib import Path
    from hermes_cli.profiles import resolve_profile_env
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override
    home=resolve_profile_env(profile);token=set_hermes_home_override(home)
    try:return load_user_config_effective(Path(home)/'config.yaml',fail_closed=True)
    finally:reset_hermes_home_override(token)

def token_facts(conn,task,read_session):
    """Only explicitly linked worker sessions. Cached input is part of total input."""
    current=None;records=[];seen=set();unknown=0
    for row in conn.execute('SELECT id,profile,metadata FROM task_runs WHERE task_id=? ORDER BY id',(task['id'],)):
        rid,profile,raw=row
        try:sid=json.loads(raw or '{}').get('worker_session_id')
        except (TypeError,ValueError):sid=None
        if not sid:sid=registered_receipt(conn,task['id'],rid).get('worker_session_id')
        if not sid:unknown+=1;continue
        try:usage=read_session(profile,sid)
        except Exception:usage=None
        fields=['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens']
        if not usage or not any(type(usage.get(k)) is int and usage[k]>0 for k in fields):unknown+=1;continue
        known=all(type(usage.get(k)) is int and usage[k]>=0 for k in fields)
        inp=sum(usage[k] for k in fields if k!='output_tokens' and type(usage.get(k)) is int and usage[k]>=0)
        out=usage.get('output_tokens') if type(usage.get('output_tokens')) is int and usage['output_tokens']>=0 else None
        record={'uncached_input':usage.get('input_tokens'),'cache_read':usage.get('cache_read_tokens'),'cache_write':usage.get('cache_write_tokens'),'total':inp+(out or 0),'input':inp,'output':out,'partial':True,'counters_complete':known,'scope':'current run','run_id':rid}
        if task.get('current_run_id')==rid:current=record
        if sid not in seen:records.append(record);seen.add(sid)
    lifetime=None
    if records:lifetime={'uncached_input':sum(x['uncached_input'] for x in records) if all(type(x['uncached_input']) is int for x in records) else None,'cache_read':sum(x['cache_read'] for x in records) if all(type(x['cache_read']) is int for x in records) else None,'cache_write':sum(x['cache_write'] for x in records) if all(type(x['cache_write']) is int for x in records) else None,'total':sum(x['total'] for x in records),'input':sum(x['input'] for x in records),'output':sum(x['output'] for x in records) if all(x['output'] is not None for x in records) else None,'counters_complete':all(x['counters_complete'] for x in records),'partial':True,'scope':'card cumulative recorded runs','recorded_runs':len(records),'missing_runs':unknown}
    return {'current':current,'cumulative':lifetime,'coverage':'Partial: linked worker sessions only. Reported MoA adviser usage is folded into session counters; auxiliary and unreported calls are not guaranteed. Input includes cache reads/writes; no money or quota conversion.'}

def native_read_session(profile,sid):
    import sqlite3,contextlib
    from pathlib import Path
    from hermes_cli.profiles import resolve_profile_env,profile_root_for_env_home
    home=Path(resolve_profile_env(profile));root=home.parent.parent if home.parent.name=='profiles' else home
    for path in dict.fromkeys([home/'state.db',root/'state.db']):
        if not path.is_file():continue
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row
            row=c.execute('SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens FROM sessions WHERE id=?',(sid,)).fetchone()
            if row:
                result=dict(row)
                try:
                    result['observed_routes']=[dict(x) for x in c.execute('SELECT model,billing_provider,task,api_call_count,first_seen,last_seen FROM session_model_usage WHERE session_id=? ORDER BY last_seen',(sid,))]
                except sqlite3.OperationalError:result['observed_routes']=[]
                return result
    return None
