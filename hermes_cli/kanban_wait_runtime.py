"""Native retry policy as a current requirement, never a historical disable bit."""
import json


def retry_state(conn, task):
    if isinstance(task, dict):
        tid = task['id'];count = int(task.get('consecutive_failures') or 0);limit = task.get('max_retries')
    else:
        tid = task.id;count = int(task.consecutive_failures or 0);limit = task.max_retries
    policy = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='wait_runtime_policy' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    from hermes_cli import kanban_db as kb
    limit = limit if limit is not None else json.loads(policy[0])['failure_limit'] if policy else kb.DEFAULT_FAILURE_LIMIT
    return {'counter': count, 'limit': limit, 'satisfied': count < limit, 'evaluator': 'retry_budget'}
