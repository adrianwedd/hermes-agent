"""Versioned predicates over native Kanban requirements; no free-text evaluator."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

MODES = {"time_due": "automatically_observed", "retry_budget": "automatically_observed", "observation": "automatically_observed", "attestation": "manual_attestation",
         "operator_decision": "manual_attestation", "explicit_stop": "manual_attestation",
         "unsupported": "unsupported"}
STAGES = {"operator_decision": "OPERATOR", "explicit_stop": "HELD", "unsupported": "WORKFLOW_FAULT"}


def record(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record
    event_id, contract = contract_record(conn, task_id)
    contract = contract or {}
    return event_id, contract, contract.get("wait_contract")


def validate(wait):
    if not isinstance(wait, dict) or wait.get("version") != 1 or not isinstance(wait.get("gates"), list) or not wait["gates"]:
        raise ValueError("invalid native wait contract")
    if not isinstance(wait.get("provenance"), dict) or not wait["provenance"]:
        raise ValueError("wait requires native provenance")
    if not isinstance(wait.get("owned_fields", []), list) or not all(isinstance(x, str) for x in wait.get("owned_fields", [])):
        raise ValueError("owned_fields must be a list of field names")
    if not set(wait.get("owned_fields", [])) <= {"status", "dispatch_eligible", "block_kind"}:
        raise ValueError("wait cannot own unrelated state")
    seen = set()
    for gate in wait["gates"]:
        if not isinstance(gate, dict):
            raise ValueError("gate must be an object")
        revision = gate.get("revision")
        if (not isinstance(gate.get("id"), str) or not gate["id"] or gate["id"] in seen
                or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
                or not isinstance(gate.get("evaluator"), str) or gate["evaluator"] not in MODES or not isinstance(gate.get("resolver"), str) or not gate["resolver"].strip()
                or not isinstance(gate.get("wake_trigger"), list) or not gate["wake_trigger"]
                or not isinstance(gate.get("parameters"), dict)
                or not isinstance(gate.get("reason"), str) or not gate["reason"].strip()
                or not isinstance(gate.get("stage"), str) or gate["stage"] not in {"HELD", "OPERATOR", "WAITING", "BLOCKED", "WORKFLOW_FAULT"}
                or not isinstance(gate.get("current_unblock_action"), str) or not gate["current_unblock_action"].strip()
                or not all(isinstance(x, str) and x for x in gate.get("wake_trigger", []))):
            raise ValueError("gate needs identity/revision, registered evaluator, resolver, stage, action and triggers")
        seen.add(gate["id"])
        expected = declaration(gate["id"], gate["evaluator"], resolver=gate["resolver"],
                               parameters=gate["parameters"], action=gate["current_unblock_action"],
                               reason=gate.get("reason"), stage=gate["stage"], revision=revision)
        if gate.get("declaration") != expected["declaration"]:
            raise ValueError("gate declaration does not match predicate identity")
        params = gate["parameters"]
        if gate["evaluator"] == "time_due":
            due = params.get("scheduled_at")
            if isinstance(due, bool) or not isinstance(due, (int, float)) or not math.isfinite(due):
                raise ValueError("temporal gate requires a finite Unix scheduled_at")
        if gate["evaluator"] in {"operator_decision", "explicit_stop"}:
            key = "pending_event_id" if gate["evaluator"] == "operator_decision" else "stop_event_id"
            if type(params.get(key)) is not int or params[key] <= 0:
                raise ValueError("protected gate requires a bound native source event")
        if gate["evaluator"] not in {"time_due", "retry_budget", "unsupported"}:
            assertions = params.get("required_assertions")
            if not isinstance(assertions, list) or not assertions or not all(isinstance(x, str) and x for x in assertions):
                raise ValueError("attested gate requires named evidence assertions")
        _validate_parameters(gate)
        choices = params.get("decision_values", {}).get("selection", [])
        continuations = params.get("decision_continuations", {})
        if choices and any(choice not in continuations or continuations[choice].get("state") not in {"todo", "review"} for choice in choices):
            raise ValueError("Every declared decision choice requires a native continuation")
    continuation = wait.get("continuation")
    if not isinstance(continuation, dict) or continuation.get("state") not in {"todo", "review"}:
        raise ValueError("wait needs an explicit native continuation")
    selected = continuation.get("selected_next_action")
    if selected is not None and not isinstance(selected, dict):
        raise ValueError("continuation action must be a native action object or null")


def _validate_parameters(gate):
    params = gate["parameters"]
    if gate["evaluator"] == "observation":
        from hermes_cli.kanban_wait_observation import PROBE_NAMES
        if params.get("probe_name") not in PROBE_NAMES:
            raise ValueError("observation requires a registered probe")
    fields = params.get("decision_fields", [])
    values = params.get("decision_values", {})
    branches = params.get("decision_continuations", {})
    if not isinstance(fields, list) or not all(isinstance(x, str) and x for x in fields):
        raise ValueError("decision_fields requires field names")
    if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, list) and v and all(isinstance(x, str) and x for x in v) for k, v in values.items()):
        raise ValueError("decision_values requires named lists of choices")
    if not isinstance(branches, dict) or not all(isinstance(k, str) and isinstance(v, dict) and v.get("state") in {"todo", "review"} and (v.get("selected_next_action") is None or isinstance(v["selected_next_action"], dict)) for k, v in branches.items()):
        raise ValueError("decision_continuations requires native continuation objects")


def gate_event(conn, task_id, gate):
    row = conn.execute(
        "SELECT id,payload,kind FROM task_events WHERE task_id=? "
        "AND kind IN ('wait_gate_satisfied','wait_gate_invalidated') AND json_valid(payload) "
        "AND json_extract(payload,'$.gate_id')=? AND json_extract(payload,'$.revision')=? "
        "ORDER BY id DESC LIMIT 1", (task_id, gate["id"], gate["revision"]),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[1])
    # An arbitrary matching ID is not a certificate. Only the native privileged
    # transition writes an authorised, declaration-bound evidence receipt.
    expected_actor = "observer:" + gate["parameters"]["probe_name"] if gate["evaluator"] == "observation" else "operator"
    if (payload.get("authorised") is not True or payload.get("declaration") != gate.get("declaration")
            or not re.fullmatch(r"[a-fA-F0-9]{64}", str(payload.get("evidence_sha256", ""))) or payload.get("actor") != expected_actor):
        return None
    return {"id": row[0], "kind": row[2], **payload}


def condition_digest(condition):
    return hashlib.sha256(json.dumps(condition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def condition_is_current(conn, task_id, gate):
    params = gate["parameters"]
    if not params.get("native_condition_sha256") or not params.get("condition_id"):
        return True
    _, contract, _ = record(conn, task_id)
    conditions = contract.get("external_conditions", [])
    current = next((c for c in conditions if isinstance(c, dict) and c.get("condition_id") == params["condition_id"]), None)
    # Authority markers outside external_conditions retain their own source identity.
    return current is None or condition_digest(current) == params["native_condition_sha256"]


def evaluate(conn, task_id, gate, *, now=None):
    mode = gate["evaluator"]
    if not condition_is_current(conn, task_id, gate):
        return False
    if mode == "retry_budget":
        task = conn.execute("SELECT consecutive_failures,max_retries FROM tasks WHERE id=?", (task_id,)).fetchone()
        policy = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='wait_runtime_policy' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return bool(task and policy and task[0] < (task[1] if task[1] is not None else json.loads(policy[0])["failure_limit"]))
    if mode == "time_due":
        return (time.time() if now is None else now) >= gate["parameters"]["scheduled_at"]
    if mode == "unsupported":
        return False
    evidence = gate_event(conn, task_id, gate)
    if evidence is None or evidence["kind"] != "wait_gate_satisfied":
        return False
    if mode == "operator_decision":
        pending = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='operator_decision_pending' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return bool(pending and pending[0] == gate["parameters"]["pending_event_id"]
                    and evidence.get("pending_event_id") == pending[0])
    return True


def facts(conn, task_id, *, now=None, satisfied_gate=None):
    _, _, wait = record(conn, task_id)
    if wait is None:
        return None
    try:
        validate(wait)
    except (ValueError, TypeError):
        gate = declaration("invalid-native-wait", "unsupported", resolver="operator", parameters={"contract_sha256": condition_digest(wait)}, action="Repair the invalid native wait contract through an authorised declaration", reason="Invalid native wait contract; no executable predicate")
        gate.update(satisfied=False, evaluation_mode="unsupported", receipt_event_id=None)
        return {"version": 1, "gates": [gate], "remaining_gates": [gate], "continuation": {"state": "todo"}, "provenance": {"invalid_persisted_contract": True}}
    gates = []
    for gate in wait["gates"]:
        receipt = gate_event(conn, task_id, gate) if gate["evaluator"] not in {"time_due", "retry_budget", "unsupported"} else None
        derived = {**gate, "satisfied": gate["id"] == satisfied_gate or evaluate(conn, task_id, gate, now=now),
                   "evaluation_mode": MODES[gate["evaluator"]], "receipt_event_id": (receipt or {}).get("id")}
        if gate["evaluator"] == "retry_budget":
            from hermes_cli.kanban_wait_runtime import retry_state
            derived["observation"] = retry_state(conn, {"id": task_id, **dict(conn.execute("SELECT consecutive_failures,max_retries FROM tasks WHERE id=?", (task_id,)).fetchone())})
        gates.append(derived)
    return {**wait, "gates": gates, "remaining_gates": [g for g in gates if not g["satisfied"]]}


def declaration(gate_id, evaluator, *, resolver, parameters, action, reason, stage="WAITING", revision=1):
    identity = hashlib.sha256(json.dumps({"id": gate_id, "revision": revision, "evaluator": evaluator,
                                        "parameters": parameters}, sort_keys=True).encode()).hexdigest()
    return {"id": gate_id, "revision": revision, "declaration": identity, "evaluator": evaluator,
            "parameters": parameters, "resolver": resolver, "stage": STAGES.get(evaluator, stage),
            "reason": reason, "current_unblock_action": action,
            "wake_trigger": ["dispatcher_tick", "restart_reconciliation", "wait_gate_satisfied", "wait_gate_invalidated"]
            + (["scheduled_at_due"] if evaluator == "time_due" else [])}


def projection_identity(wait):
    return hashlib.sha256(json.dumps(wait["gates"], sort_keys=True).encode()).hexdigest()


def project(conn, task, wait, *, decision_selection=None):
    """Pure post-gate state: clear only fields explicitly owned by this wait."""
    from dataclasses import asdict, is_dataclass
    _, contract, _ = record(conn, task["id"] if isinstance(task, dict) else task.id)
    t = dict(task) if isinstance(task, dict) else asdict(task) if is_dataclass(task) else vars(task).copy()
    # Protected native prohibitions cannot be adopted by an unrelated wait.
    block_id = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'", (t["id"],)).fetchone()[0]
    if t.get("block_kind") == "explicit_stop" and not any(g["evaluator"] == "explicit_stop" and g["parameters"].get("stop_event_id") == block_id and g["satisfied"] for g in wait["gates"]):
        return None, None
    from hermes_cli.kanban_decision import _operator_decision_pending
    if _operator_decision_pending(conn, t["id"]):
        pending_id = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (t["id"],)).fetchone()[0]
        if not any(g["evaluator"] == "operator_decision" and g["parameters"].get("pending_event_id") == pending_id and g["satisfied"] for g in wait["gates"]):
            return None, None
    marker = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='wait_continuation_applied' ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if marker and json.loads(marker[0])["identity"] == projection_identity(wait):
        return None, None  # Ordinary later native lifecycle now owns the action.
    continuation = wait["continuation"]
    invalidation = conn.execute("SELECT payload,id FROM task_events WHERE task_id=? AND kind='wait_gate_invalidated' ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if invalidation and json.loads(invalidation[0]).get("resume_snapshot"):
        continuation = json.loads(invalidation[0])["resume_snapshot"]
    for gate in wait["gates"]:
        if gate["evaluator"] == "operator_decision" and gate["satisfied"]:
            receipt = gate_event(conn, t["id"], gate)
            choice = decision_selection or (receipt or {}).get("decision") or {}
            boundary = max(invalidation[1] if invalidation else 0, wait.get("owned_block_event") or 0)
            if decision_selection is not None or (receipt or {}).get("id", 0) > boundary:
                continuation = gate["parameters"].get("decision_continuations", {}).get(choice.get("selection"), continuation)
    owned = set(wait.get("owned_fields", []))
    block = conn.execute("SELECT id,kind FROM task_events WHERE task_id=? AND kind IN ('blocked','unblocked') ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if block and block[1] == "blocked" and block[0] != wait.get("owned_block_event"):
        # A later unrelated blocker owns these fields now; old gate acceptance
        # must not undo the new STOP, failure policy or authority boundary.
        return None, None
    if "status" in owned:
        t["status"] = continuation["state"]
    if "dispatch_eligible" in owned:
        t["dispatch_eligible"] = True
    if "block_kind" in owned:
        t["block_kind"] = None
    adjusted = dict(contract)
    selected = continuation.get("selected_next_action")
    if "selected_next_action" in continuation:
        adjusted["selected_next_action"] = selected
    return t, adjusted


def decision_fields(conn, task_id, decision, *, wait_override=None):
    """Expose current unblock separately from future work to every consumer."""
    wait = wait_override if wait_override is not None else facts(conn, task_id)
    protected = decision.get("workflow_stage") in {"HELD", "OPERATOR"}
    candidates = (wait or {}).get("remaining_gates", [])
    candidates = [g for g in candidates if not protected or g["stage"] == decision["workflow_stage"]]
    if candidates:
        gate = sorted(candidates, key=lambda g: {"HELD": 0, "OPERATOR": 1, "BLOCKED": 2}.get(g["stage"], 3))[0]
        from hermes_cli.kanban_wait_explanation import gate_explanation
        reason, action = gate_explanation(gate)
        decision.update(workflow_stage=gate["stage"], dispatchable=False, worker_executable_now=False,
                        reason=reason, owner=gate["resolver"], next_action=action,
                        current_next_action=action, current_next_action_type="unblock",
                        resume_condition={"evaluator": gate["evaluator"], "parameters": gate["parameters"]})
        decision.update(why_not_executable=reason, predicate={"identity": gate["id"], "revision": gate["revision"], **gate["parameters"]},
                        evaluation_mode=gate["evaluation_mode"], resolver=gate["resolver"], wake_trigger=gate["wake_trigger"],
                        current_unblock_action=action, post_gate_continuation=wait["continuation"],
                        remaining_gates=wait["remaining_gates"])
    else:
        from hermes_cli.kanban_wait_fallback import fields
        fields(conn, task_id, decision)
    return decision
