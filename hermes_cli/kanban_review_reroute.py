"""Explicit operator routing for an inactive independent review."""
import json


def reroute_pending_review(conn, task_id, reviewer, summary, metadata, *, force=False, expected_run_id=None):
    """Explicit operator recovery of a returned review, preserving its implementer."""
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if not task or task.status != 'review':
        return None
    if not force or expected_run_id is not None or not reviewer or not summary:
        return False, 'rerouting a pending review requires an explicit operator reviewer and summary'
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task.status != 'review' or task.claim_lock is not None:
            return False, 'review ownership changed'
        row = conn.execute("SELECT payload,run_id FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        prior = json.loads(row[0]) if row else {}
        implementer = prior.get('implementer')
        if not implementer or reviewer == implementer:
            return False, 'reviewer must differ from the retained implementer'
        conn.execute('UPDATE tasks SET assignee=?,dispatch_eligible=1 WHERE id=?', (reviewer, task_id))
        kb._append_event(conn, task_id, 'review_requested', {
            'implementer': implementer, 'reviewer': reviewer, 'summary': summary,
            'replaces_review_event_run': row[1], 'operator_reroute': True,
            'metadata': metadata or {},
        }, run_id=row[1])
    kb.recompute_ready(conn)
    return True, None
