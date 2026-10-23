"""Conservative native-gate migration: represent restrictions, never satisfy them."""
from __future__ import annotations

import copy
import hashlib
import json

from hermes_cli.kanban_wait_contract import declaration, record


def _latest(conn, task_id, kinds):
    import json
    row = conn.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN (" + ",".join("?" for _ in kinds) + ") ORDER BY id DESC LIMIT 1", (task_id, *kinds)).fetchone()
    if row and row[1] == "blocked":
        retired = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='wait_gate_superseded' "
            "AND id>? AND json_valid(payload) "
            "AND json_extract(payload, '$.gate.evaluator') IN ('attestation','unsupported') "
            "AND json_extract(payload, '$.gate.parameters.source_event_id')=? LIMIT 1",
            (task_id, row[0], row[0]),
        ).fetchone()
        if retired:
            # Do not fall back to still older, obsolete blocker history.
            return None, None, {}
    return (row[0], row[1], json.loads(row[2] or "{}")) if row else (None, None, {})


def manual_condition(identity, native_condition, source_event_id, *, stage):
    """Attest the unchanged native restriction, identified by content and source.

    There is no semantic parser: every clause of the original condition remains
    part of the evidence obligation. Declaring this contract grants nothing.
    """
    encoded = json.dumps(native_condition, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return declaration(identity, "attestation", resolver=native_condition.get("resolver") or "operator",
                       parameters={"source_event_id": source_event_id, "native_condition": native_condition,
                                   "native_condition_sha256": digest, "condition_id": native_condition.get("condition_id"),
                                   "required_assertions": ["all_original_constraints_verified", "authority_verified", "evidence_applicability_verified"]},
                       action="Record authorised evidence satisfying every clause of native condition " + identity,
                       reason="Native external restriction requires explicit qualified attestation", stage=stage)


def append_external_gates(gates, known, contract, selected, cid):
    from hermes_cli.kanban_wait_observation import PROBE_NAMES
    for condition in contract.get("external_conditions", []):
        if not isinstance(condition, dict) or not condition.get("condition_id"):
            continue
        if selected.get("condition_probe") in PROBE_NAMES and condition["condition_id"] == selected.get("condition_id"):
            continue
        identity = "external:" + condition["condition_id"]
        from hermes_cli.kanban_wait_contract import condition_digest
        previous = next((g for g in gates if g["id"] == identity), None)
        if previous is None or previous["parameters"].get("native_condition_sha256") != condition_digest(condition):
            replacement = manual_condition(identity, condition, cid, stage="WAITING")
            if previous:
                replacement = declaration(identity, "attestation", resolver=replacement["resolver"], parameters=replacement["parameters"], action=replacement["current_unblock_action"], reason=replacement["reason"], revision=previous["revision"] + 1)
                gates.remove(previous)
            gates.append(replacement)


def proposal(conn, task):
    """Only native markers select evaluators. Opaque reasons become unsupported."""
    cid, contract, existing = record(conn, task.id)
    if existing is not None:
        from hermes_cli.kanban_wait_contract import validate
        try:
            validate(existing)
        except (ValueError, TypeError):
            return None  # Persisted corruption is an explicit unsupported fault.
    from hermes_cli.kanban_decision import decide
    d = decide(conn, task)
    if d["workflow_stage"] in {"DONE", "SUPERSEDED"}:
        return None
    selected = contract.get("selected_next_action") or {}
    from hermes_cli import kanban_db as kb
    continuation = {"state": "review" if task.status == "review" or kb._resume_status_from_events(conn, task.id) == "review" else "todo",
                    "selected_next_action": copy.deepcopy(selected) if selected.get("phase") not in {"WAITING_FOR_AUTHORITY", "OPERATOR"} else None}
    gates = copy.deepcopy((existing or {}).get("gates", []))
    known = {g["id"] for g in gates}
    source_id, source_kind, payload = _latest(conn, task.id, ("operator_decision_pending", "operator_decision_resolved"))
    if task.status == "blocked" and task.block_kind == "explicit_stop":
        event_id, _, p = _latest(conn, task.id, ("blocked",))
        if "stop:" + str(event_id) not in known:
            gates.append(declaration("stop:" + str(event_id), "explicit_stop", resolver="operator",
                     parameters={"stop_event_id": event_id, "required_assertions": ["explicit_stop_revoked"]},
                     action="Record an authorised STOP revocation; preserve every other gate", reason="Explicit STOP remains in force"))
    if d["workflow_stage"] == "OPERATOR":
        source_id, source_kind, payload = _latest(conn, task.id, ("operator_decision_pending",))
    if source_kind == "operator_decision_pending" and "operator:" + str(source_id) not in known:
        gates = [g for g in gates if g["evaluator"] != "operator_decision"]
        gates.append(declaration("operator:" + str(source_id), "operator_decision", resolver=payload.get("resolver") or "operator",
                     parameters={"pending_event_id": source_id, "decision_fields": ["selection"], "required_assertions": ["decision_authorised", "selected_continuation_authorised"]},
                     action=payload.get("next_action") or "Record the current authorised decision selection",
                     reason=payload.get("reason") or "Operator decision remains unresolved"))
    from hermes_cli.kanban_wait_observation import PROBE_NAMES, observation_gate
    if selected.get("condition_probe") in PROBE_NAMES and not existing:
        identity = "observation:" + str(selected.get("condition_id") or selected["condition_probe"])
        gates.append(observation_gate(identity, selected, cid))
        if selected.get("resume_next_action"):
            continuation["selected_next_action"] = {"phase": "READY", "type": "worker_action", "action": selected["resume_next_action"]}
    append_external_gates(gates, known, contract, selected, cid)
    block_id, _, block_payload = _latest(conn, task.id, ("blocked",))
    if (existing and task.status == "blocked" and task.block_kind != "explicit_stop"
            and block_id and block_id != existing.get("owned_block_event")
            and "blocked:" + str(block_id) not in known):
        gates.append(manual_condition("blocked:" + str(block_id), block_payload, block_id, stage="BLOCKED"))
    failure_id, _, failure_payload = _latest(conn, task.id, ("gave_up",))
    if task.status == "blocked" and failure_payload.get("terminal_provider") is True and "provider:" + str(failure_id) not in known:
        gates.append(manual_condition("provider:" + str(failure_id), failure_payload, failure_id, stage="BLOCKED"))
        if failure_payload.get("retry_status") == "review":
            continuation["state"] = "review"
    if not gates and task.status == "blocked" and task.block_kind is None and block_id is None:
        gates.append(declaration("retry-budget", "retry_budget", resolver="dispatcher",
                     parameters={"counter": "tasks.consecutive_failures", "limit": "tasks.max_retries or latest wait_runtime_policy.failure_limit"},
                     action="Repair the failure or change the authorised retry policy; counters remain intact",
                     reason="Current failure budget prevents another attempt", stage="BLOCKED"))
    if not gates and (task.status == "blocked" or d["workflow_stage"] in {"WAITING", "BLOCKED", "WORKFLOW_FAULT"}):
        if selected.get("phase") == "WAITING_FOR_AUTHORITY" or task.status == "scheduled":
            eid, _, p = _latest(conn, task.id, ("scheduled",))
            due = p.get("scheduled_at")
            if due is not None:
                gates.append(declaration("time:" + str(eid), "time_due", resolver="clock",
                             parameters={"scheduled_at": due}, action="Wait until the encoded scheduled_at", reason="Temporal predicate is not due"))
            else:
                source = {"source_event_id": eid or cid, "scheduled_payload": p, "selected_next_action": selected, "condition_id": selected.get("condition_id"), "resolver": selected.get("resolver")}
                gates.append(manual_condition("authority:" + str(eid or cid), source, eid or cid, stage="WAITING"))
        elif task.status == "blocked" or d["workflow_stage"] in {"BLOCKED", "WORKFLOW_FAULT"}:
            eid, _, p = _latest(conn, task.id, ("blocked", "block_loop_detected"))
            source = {**p, "native_block_kind": task.block_kind, "last_failure_error": task.last_failure_error, "consecutive_failures": task.consecutive_failures}
            gates.append(manual_condition("blocked:" + str(eid or cid), source, eid or cid, stage="BLOCKED"))
    from hermes_cli.kanban_wait_runtime import retry_state
    budget = retry_state(conn, task)
    if not budget["satisfied"] and not any(g["evaluator"] == "retry_budget" for g in gates):
        gates.append(declaration("retry-budget", "retry_budget", resolver="dispatcher",
                     parameters={"counter": "tasks.consecutive_failures", "limit": "tasks.max_retries or latest wait_runtime_policy.failure_limit"},
                     action="Repair the failure or change the authorised retry policy; counters remain intact",
                     reason="Current failure budget prevents another attempt", stage="BLOCKED"))
    continuation = preserved_continuation(conn, task, existing, continuation)
    if not gates or (existing is not None and gates == existing["gates"] and continuation == existing["continuation"]):
        return None
    return {"gates": gates, "continuation": continuation, "provenance": {"source_contract_event": cid, "migration": "native-marker-only-v1", "substantive_conditions_satisfied": False}, "old_decision": d}


def preserved_continuation(conn, task, existing, proposed):
    from hermes_cli import kanban_db as kb
    if not existing:
        return proposed
    current = existing["continuation"]
    marker = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='wait_continuation_applied'", (task.id,)).fetchone()[0]
    block = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'", (task.id,)).fetchone()[0]
    review = task.status == "review" or kb._resume_status_from_events(conn, task.id) == "review"
    unconsumed_migration = existing.get("provenance", {}).get("migration") == "native-marker-only-v1" and not marker
    new_phase = marker and (task.status == "review" or block != existing.get("owned_block_event"))
    if review and current["state"] != "review" and (unconsumed_migration or new_phase):
        return {**current, "state": "review"}
    return current


def migrate(conn, task_ids=None, *, failure_limit=None):
    import os
    if os.environ.get("HERMES_KANBAN_TASK"):
        return []
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_transitions import declare_wait
    ids = task_ids or [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived','running') AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL")]
    migrated = []
    for tid in ids:
        task = kb.get_task(conn, tid)
        if task is None:
            continue
        if task.consecutive_failures or (task.block_kind is None and task.status == "blocked"):
            if task.status in {"running", "done", "archived"}:
                continue
            latest = _latest(conn, tid, ("blocked",))[0]
            if latest is None or task.consecutive_failures:
                _, _, policy = _latest(conn, tid, ("wait_runtime_policy",))
                limit = policy.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT) if failure_limit is None else failure_limit
                if policy.get("failure_limit") != limit:
                    with kb.write_txn(conn):
                        kb._append_event(conn, tid, "wait_runtime_policy", {"failure_limit": limit})
        from hermes_cli.kanban_completion_workflow import ensure_scope_contract
        ensure_scope_contract(conn, tid, authority="control_plane_pre_promotion")
        p = proposal(conn, task)
        if p:
            wait = declare_wait(conn, tid, p["gates"], p["continuation"], provenance=p["provenance"])
            migrated.append({"id": tid, "old_gate": p["old_decision"], "new_wait_contract": wait})
    return migrated
