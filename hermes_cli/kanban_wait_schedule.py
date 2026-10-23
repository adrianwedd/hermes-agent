"""Temporal scheduling and honest unsupported parking through one native boundary."""
import math


def schedule(conn, task_id, *, reason=None, scheduled_at=None, expected_run_id=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_workflow import ensure_scope_contract
    from hermes_cli.kanban_wait_contract import declaration, record, validate
    if scheduled_at is not None and (isinstance(scheduled_at, bool) or not isinstance(scheduled_at, (float, int)) or not math.isfinite(scheduled_at)):
        raise ValueError("scheduled_at must be a finite Unix timestamp")
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status not in {"todo", "ready", "running", "blocked"}:
            return False
        if task.block_kind == "explicit_stop":
            raise PermissionError("Scheduling cannot revoke an explicit STOP")
        if task.current_run_id is not None and task.current_run_id != expected_run_id:
            raise PermissionError("Scheduling needs current run ownership")
        ensure_scope_contract(conn, task_id, authority="native_schedule")
        cid, contract, previous = record(conn, task_id)
        mode = "time_due" if scheduled_at is not None else "unsupported"
        gate = declaration("schedule:" + str(cid) + ":" + str(scheduled_at), mode, resolver="clock" if scheduled_at is not None else "control plane",
                           parameters={"scheduled_at": scheduled_at} if scheduled_at is not None else {"source_contract_event": cid, "missing": "temporal timestamp or declared external evidence contract"},
                           action="Wait for the encoded timestamp" if scheduled_at is not None else "Declare a temporal predicate or a named attestation contract",
                           reason=reason or ("Timestamp not yet due" if scheduled_at is not None else "Reason-only scheduling is unsupported"))
        continuation_state = "review" if kb._retry_status_for_run(conn, task_id, task.current_run_id) == "review" else "todo"
        wait = {"version": 1, "gates": (previous or {}).get("gates", []) + [gate], "owned_fields": ["status", "block_kind", "dispatch_eligible"],
                "continuation": {"state": continuation_state, "selected_next_action": contract.get("selected_next_action")},
                "provenance": {"source_contract_event": cid, "producer": "native_schedule"}}
        validate(wait)
        contract = dict(contract);contract["wait_contract"] = wait
        kb._append_event(conn, task_id, "completion_requirements", contract)
        kb._append_event(conn, task_id, "scheduled", {"reason": reason, "scheduled_at": scheduled_at, "gate_id": gate["id"]})
        kb._end_or_synthesize_run(conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason))
        conn.execute("UPDATE tasks SET status=?,dispatch_eligible=1,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?", ("scheduled" if scheduled_at is not None else "todo", task_id))
    kb.recompute_ready(conn)
    return True
