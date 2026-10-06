"""Exact run/session links only. Never derives historical routes from current config."""
import json
from hermes_cli.kanban_worker_observability import registered_receipt

def slot(value):
 if not isinstance(value,dict):return None
 provider=value.get('provider');model=value.get('model')
 if not isinstance(provider,str) or not isinstance(model,str) or not provider or not model:return None
 return {'provider':provider[:100],'model':model[:180]}

def route(value):
 if not isinstance(value,dict):return None
 main=slot(value)
 if not main:return None
 main.update(aggregator=slot(value.get('aggregator')),advisers=[s for x in value.get('advisers',[]) if (s:=slot(x))] if isinstance(value.get('advisers'),list) else [],observed_at=value.get('observed_at') if type(value.get('observed_at')) in (int,float) else None)
 return main

def run_routes(conn,task,read_session):
 rows=conn.execute('SELECT id,profile,started_at,ended_at,metadata FROM task_runs WHERE task_id=? ORDER BY id DESC',(task['id'],));results=[]
 for rid,profile,start,end,raw in rows:
  try:meta=json.loads(raw or '{}')
  except (ValueError,TypeError):meta={}
  if not isinstance(meta,dict):meta={}
  startup=registered_receipt(conn,task['id'],rid)
  launch=route(meta.get('launch_route')) or route(startup.get('launch_route'));observed=route(meta.get('runtime_route'));basis='Worker-recorded runtime route' if observed else None;sid=meta.get('worker_session_id') or startup.get('worker_session_id')
  if isinstance(sid,str) and sid:
   try:session=read_session(profile,sid) or {}
   except Exception:session={}
   records=session.get('observed_routes') or []
   primary=[x for x in records if not x.get('task') and x.get('api_call_count',0)>0]
   if primary:
    last=max(primary,key=lambda x:x.get('last_seen') or 0);candidate=route({'provider':last.get('billing_provider'),'model':last.get('model'),'observed_at':last.get('last_seen')})
    if candidate and (not observed or (candidate['observed_at'] or 0)>=(observed['observed_at'] or 0)):
     candidate['advisers']=[s for x in records if x.get('task')=='moa_reference' and (s:=slot({'provider':x.get('billing_provider'),'model':x.get('model')}))]
     agg=[x for x in records if x.get('task')=='moa_aggregator']
     candidate['aggregator']=slot({'provider':agg[-1].get('billing_provider'),'model':agg[-1].get('model')}) if agg else None
     observed=candidate;basis='Linked session model-usage receipt; last recorded call, not proof of in-flight route'
  results.append({'run_id':rid,'agent':profile or 'unknown','started_at':start,'ended_at':end,'launch':launch,'last_observed':observed,'basis':basis or 'No exact linked route receipt; historical model unknown'})
 current=next((x for x in results if x['run_id']==task.get('current_run_id')),None)
 return {'selected':current or (results[0] if results else None),'history':results,'planned_agent':task.get('assignee') or 'default','historical_inference':False}
