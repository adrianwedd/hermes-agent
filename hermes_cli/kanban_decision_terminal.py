"""Project accepted terminal scope without rewriting its historical contract."""

def _accepted_scope(conn, task_id, contract_event_id):
    if contract_event_id is None:
        return False
    terminal = conn.execute(
        "SELECT id,kind,payload FROM task_events WHERE task_id=? AND "
        "(kind='completed' OR (kind='resolved' AND json_valid(payload) "
        "AND json_extract(payload,'$.by')='operator_close' "
        "AND json_extract(payload,'$.outcome')='closed')) ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if terminal is None or terminal[0] <= contract_event_id:
        return False
    if terminal[1] == "resolved":
        # Native operator close accepts the current contract under its write
        # lock. A later contract declaration above invalidates that acceptance.
        return True
    receipt = conn.execute(
        "SELECT json_extract(payload,'$.contract_event_id') FROM task_events WHERE task_id=? "
        "AND kind='completion_evidence_accepted' AND id>? AND id<? "
        "AND json_valid(payload) ORDER BY id DESC LIMIT 1",
        (task_id, contract_event_id, terminal[0]),
    ).fetchone()
    return bool(receipt and receipt[0] == contract_event_id)


def apply_terminal_decision(conn, task_id, status, contract_event_id, decision):
    decision.update(
        workflow_stage="DONE" if status == "done" else "SUPERSEDED",
        reason="Accepted bounded scope is terminal", owner=None,
        next_action=None, current_next_action=None,
    )
    if _accepted_scope(conn, task_id, contract_event_id):
        decision.update(remaining_required_actions=[], selected_next_action={},
                        current_next_action_type=None, required_authority=None)
    elif decision["remaining_required_actions"]:
        decision["invariant_violations"].append("DONE && remaining_required_actions")
