"""Recognise a current, consumed native wait resolution without resetting failures."""
import json


def resolved_block_supersedes_error(conn, task_id, outcome):
    if outcome != "blocked":
        return False
    # Older/public installs without native waits keep their existing guard.
    try:
        from hermes_cli.kanban_wait_contract import facts, projection_identity
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.kanban_wait_contract":
            raise
        return False
    wait = facts(conn, task_id)
    if not wait or wait["remaining_gates"]:
        return False
    block = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'", (task_id,)).fetchone()[0]
    if not block or wait.get("owned_block_event") != block:
        return False
    applied = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='wait_continuation_applied' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return bool(applied and applied["id"] > block
                and json.loads(applied["payload"]).get("identity") == projection_identity(wait))
