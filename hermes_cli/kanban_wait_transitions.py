"""Privileged, evidence-bound wait transitions over the native event log."""
from __future__ import annotations

import hashlib
import json
import os
import re

from hermes_cli.kanban_wait_contract import record, validate, facts, gate_event


def require_operator(conn=None):
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_RUN_ID"):
        raise PermissionError("Workers cannot resolve authority, attest waits or revoke STOP")
    if conn is not None:
        from hermes_cli.kanban_wait_authority import reject_worker_ancestry
        reject_worker_ancestry(conn)


def declare_wait(conn, task_id, gates, continuation, *, provenance, owned_fields=None, refinement=None):
    require_operator(conn)
    from hermes_cli import kanban_db as kb
    source_id, contract, previous = record(conn, task_id)
    owned_block = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    wait = {"owned_block_event": owned_block[0] if owned_block else None, "version": 1, "gates": gates, "continuation": continuation,
            "provenance": provenance, "owned_fields": owned_fields or ["status", "block_kind", "dispatch_eligible"]}
    validate(wait)
    with kb.write_txn(conn, allow_nested=True):
        task = kb.get_task(conn, task_id)
        if task is None or task.current_run_id or task.worker_pid or task.claim_lock or task.status in {"done", "archived"}:
            raise RuntimeError("wait declaration requires an inactive nonterminal card")
        current_id, _, _ = record(conn, task_id)
        block_id = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='blocked'", (task_id,)).fetchone()[0]
        if current_id != source_id or block_id != wait["owned_block_event"]:
            raise RuntimeError("Native requirements changed; reread before declaring a wait")
        if task.block_kind == "explicit_stop" and not any(g["evaluator"] == "explicit_stop" and g["parameters"].get("stop_event_id") == block_id for g in gates):
            raise PermissionError("A declaration must retain the current native STOP")
        from hermes_cli.kanban_decision import _operator_decision_pending
        if _operator_decision_pending(conn, task_id):
            pending_id = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (task_id,)).fetchone()[0]
            if not any(g["evaluator"] == "operator_decision" and g["parameters"].get("pending_event_id") == pending_id for g in gates):
                raise PermissionError("A declaration must retain the current pending decision")
        if previous == wait:
            return wait
        # Never remove an outstanding protected gate by redeclaring a wait.
        if previous:
            old = facts(conn, task_id)["remaining_gates"]
            new = {(g["id"], g["revision"], g["declaration"]) for g in gates}
            removed = [g for g in old if (g["id"], g["revision"], g["declaration"]) not in new]
            for gate in removed:
                if gate["evaluator"] == "operator_decision" and any(g["evaluator"] == "operator_decision" and g["parameters"]["pending_event_id"] > gate["parameters"]["pending_event_id"] for g in gates):
                    latest = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='operator_decision_pending'", (task_id,)).fetchone()[0]
                    if any(g["evaluator"] == "operator_decision" and g["parameters"]["pending_event_id"] == latest for g in gates):
                        continue
                replacement = next((g for g in gates if g["id"] == gate["id"]), None)
                if replacement and _current_condition_revision(conn, task_id, gate, replacement):
                    continue
                mapping = (refinement or {}).get(gate["id"], {})
                target = next((g for g in gates if g["id"] == mapping.get("replacement_id")), None)
                if (gate["evaluator"] != "unsupported" or mapping.get("declaration") != gate["declaration"]
                        or not target or target["parameters"].get("source_condition_declaration") != gate["declaration"]):
                    raise ValueError("Outstanding gates require satisfaction or authorised unsupported-predicate refinement")
            if removed:
                kb._append_event(conn, task_id, "wait_representation_refined", {"mapping": refinement, "actor": "operator", "satisfied": False})
        updated = dict(contract)
        updated["wait_contract"] = wait
        kb._append_event(conn, task_id, "completion_requirements", updated)
        kb._append_event(conn, task_id, "wait_contract_declared", {"version": 1, "gate_ids": [g["id"] for g in gates], "provenance": provenance})
    return wait


def _current_condition_revision(conn, task_id, old, new):
    from hermes_cli.kanban_wait_contract import condition_is_current
    return (old["evaluator"] == new["evaluator"] == "attestation"
            and old["parameters"].get("condition_id") is not None
            and old["parameters"].get("condition_id") == new["parameters"].get("condition_id")
            and new["revision"] == old["revision"] + 1
            and not condition_is_current(conn, task_id, old)
            and condition_is_current(conn, task_id, new))


def _evidence(gate, evidence, decision, *, invalidate=False):
    if not isinstance(evidence, dict) or not re.fullmatch(r"[a-fA-F0-9]{64}", str(evidence.get("sha256", ""))) or not evidence.get("reference"):
        raise ValueError("attestation needs evidence reference and SHA256")
    required = ["condition_invalidated"] if invalidate else gate["parameters"]["required_assertions"]
    if not all((evidence.get("assertions") or {}).get(key) is True for key in required):
        raise ValueError("attestation is missing declared evidence assertions")
    fields = [] if invalidate else gate["parameters"].get("decision_fields", [])
    if fields and (not isinstance(decision, dict) or not all(decision.get(k) is not None for k in fields)):
        raise ValueError("decision value must include every declared decision field")
    constraints = gate["parameters"].get("decision_values", {})
    if constraints and not invalidate and not isinstance(decision, dict):
        raise ValueError("decision must record the declared selected value")
    if not invalidate and any(decision.get(key) not in values for key, values in constraints.items()):
        raise ValueError("decision value is outside declared choices")
    return evidence["sha256"]


def resolve_gate(conn, task_id, gate_id, *, revision, declaration, evidence, decision=None, invalidate=False):
    """CAS-bound attestation; the ordinary reconciliation path chooses the next state."""
    require_operator(conn)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import decide
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status in {"done", "archived", "running"} or task.current_run_id or task.worker_pid or task.claim_lock:
            raise RuntimeError("wait resolution requires an inactive nonterminal card")
        _, _, wait = record(conn, task_id)
        validate(wait)
        gate = next((g for g in wait["gates"] if g["id"] == gate_id), None)
        if gate is None or gate["revision"] != revision or gate["declaration"] != declaration:
            raise ValueError("wrong gate identity or stale declaration/revision")
        from hermes_cli.kanban_wait_contract import condition_is_current
        if not condition_is_current(conn, task_id, gate):
            raise ValueError("Condition changed; declare its current revision before attesting")
        if gate["evaluator"] in {"unsupported", "time_due", "retry_budget", "observation"}:
            raise ValueError("this gate cannot be manually certified")
        if gate["evaluator"] == "operator_decision":
            pending = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='operator_decision_pending' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            if not pending or pending[0] != gate["parameters"]["pending_event_id"]:
                raise ValueError("resolution is not bound to the current pending decision")
        if invalidate and gate["evaluator"] in {"operator_decision", "explicit_stop"}:
            raise ValueError("Protected decisions require a new native prohibition, not receipt invalidation")
        sha = _evidence(gate, evidence, decision, invalidate=invalidate)
        previous = gate_event(conn, task_id, gate)
        kind = "wait_gate_invalidated" if invalidate else "wait_gate_satisfied"
        if previous and previous["kind"] == kind:
            if previous.get("evidence_sha256") != sha or previous.get("decision") != decision:
                raise ValueError("duplicate satisfaction conflicts with accepted evidence")
        else:
            payload = {"gate_id": gate_id, "revision": revision, "declaration": declaration,
                       "authorised": True, "actor": "operator", "evidence_sha256": sha,
                       "evidence": evidence, "decision": decision,
                       "pending_event_id": gate["parameters"].get("pending_event_id")}
            if invalidate:
                _, current_contract, _ = record(conn, task_id)
                payload["resume_snapshot"] = {"state": "review" if task.status == "review" else "todo", "selected_next_action": current_contract.get("selected_next_action")}
            kb._append_event(conn, task_id, kind, payload)
            if gate["evaluator"] == "operator_decision" and not invalidate:
                kb._append_event(conn, task_id, "operator_decision_resolved", payload)
            if gate["evaluator"] == "explicit_stop" and not invalidate:
                kb._append_event(conn, task_id, "explicit_stop_revoked", payload)
        reconcile_owned_waits(conn, {task_id})
    kb.recompute_ready(conn)
    kb.notify_task_updated(conn, task_id, ["wait_contract", "dispatch_eligible", "status"])
    return decide(conn, kb.get_task(conn, task_id))


def reconcile_owned_waits(conn, task_ids=None):
    """Persist the pure satisfied-wait projection, preserving unrelated failure state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_contract import project, projection_identity
    changed = 0
    with kb.write_txn(conn, allow_nested=True):
        rows = conn.execute("SELECT * FROM tasks WHERE status NOT IN ('running','done','archived') AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL").fetchall()
        for row in rows:
            task = kb.Task.from_row(row)
            if task_ids is not None and task.id not in task_ids:
                continue
            wait = facts(conn, task.id)
            if wait is None or wait["remaining_gates"]:
                continue
            marker = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='wait_continuation_applied' ORDER BY id DESC LIMIT 1", (task.id,)).fetchone()
            identity = projection_identity(wait)
            if marker and json.loads(marker[0])["identity"] == identity:
                continue
            projected, contract = project(conn, task, wait)
            if projected is None:
                continue
            # The old blocked event belongs to this fulfilled gate. Failure
            # counters and breaker trips are deliberately not reset here.
            kb._append_event(conn, task.id, "completion_requirements", contract)
            conn.execute("UPDATE tasks SET status=?,dispatch_eligible=?,block_kind=? WHERE id=?", (projected["status"], int(projected["dispatch_eligible"]), projected.get("block_kind"), task.id))
            kb._append_event(conn, task.id, "unblocked", {"owner": "wait_contract", "gate_ids": [g["id"] for g in wait["gates"]], "resume_status": projected["status"]})
            kb._append_event(conn, task.id, "wait_continuation_applied", {"identity": identity, "native_continuation": projected["status"]})
            changed += 1
    return changed
