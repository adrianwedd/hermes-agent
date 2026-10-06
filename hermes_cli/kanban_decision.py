"""Canonical next-action decision shared by every Kanban control-plane consumer.

The card is a container for scope and evidence.  Scheduling is driven by the
single next required action derived here.  This module is deliberately
read-only: mutations become visible immediately because the fingerprint and
decision are recomputed from the same native rows the claim guard reads.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping


TERMINAL_STATUSES = {"done", "archived"}
INFRA_ERROR_MARKERS = (
    "dependency environment", "no dependency environment", "bootstrap",
    "module not found", "modulenotfounderror", "spawn failed", "spawn_failed",
    "executable not found", "environment is committed",
)


def _mapping(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        return dict(task)
    if is_dataclass(task):
        return asdict(task)
    return {name: getattr(task, name) for name in dir(task)
            if not name.startswith("_") and not callable(getattr(task, name, None))}


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_event(conn, task_id: str, kinds: tuple[str, ...]):
    marks = ",".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT id,kind,payload,created_at FROM task_events "
        f"WHERE task_id=? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (task_id, *kinds),
    ).fetchone()


def _contract(conn, task_id: str) -> tuple[int | None, dict[str, Any]]:
    from hermes_cli.kanban_completion_evidence import contract_record
    event_id, value = contract_record(conn, task_id)
    return event_id, value if isinstance(value, dict) else {}


def _dependency_state(conn, task_id: str) -> list[dict[str, Any]]:
    """Return specific dependency requirements, retaining legacy edges safely.

    New requirement metadata is append-only on the downstream card.  A matching
    ``dependency_requirement_satisfied`` event releases the requirement even if
    the producer card has unrelated work left.  Legacy edges retain their old
    terminal requirement until migrated, but are named as such rather than
    pretending the entire parent is intrinsically the scheduling unit.
    """
    rows = conn.execute(
        "SELECT p.id,p.status FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY p.id", (task_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        parent_id, status = row[0], row[1]
        declared = conn.execute(
            "SELECT id,payload FROM task_events WHERE task_id=? "
            "AND kind='dependency_requirement' AND json_valid(payload) "
            "AND json_extract(payload,'$.parent_id')=? ORDER BY id DESC LIMIT 1",
            (task_id, parent_id),
        ).fetchone()
        payload = _json(declared[1]) if declared else {}
        requirement_id = str(payload.get("requirement_id") or f"legacy:{parent_id}:terminal")
        accepted = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? "
            "AND kind='dependency_requirement_satisfied' AND json_valid(payload) "
            "AND json_extract(payload,'$.requirement_id')=? ORDER BY id DESC LIMIT 1",
            (task_id, requirement_id),
        ).fetchone()
        satisfied = bool(accepted) or (not declared and status in TERMINAL_STATUSES)
        result.append({
            "parent_id": parent_id,
            "requirement_id": requirement_id,
            "requirement": payload.get("requirement") or "parent terminal (legacy edge)",
            "evidence_identity": payload.get("evidence_identity"),
            "satisfied": satisfied,
            "parent_status": status,
        })
    return result


def dependencies_satisfied(conn, task_id: str) -> bool:
    return all(item["satisfied"] for item in _dependency_state(conn, task_id))


def _execution_health(conn, task_id: str) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT id,status,outcome,error,ended_at FROM task_runs WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return {"state": "UNKNOWN", "reason": None, "retry_after": None}
    error = str(row[3] or "")
    lowered = error.lower()
    if any(marker in lowered for marker in INFRA_ERROR_MARKERS):
        return {
            "state": "INFRASTRUCTURE_FAULT", "run_id": row[0], "reason": error,
            "retry_after": "environment_fingerprint_changes", "semantic_stage_unchanged": True,
        }
    if row[1] == "running":
        return {"state": "ACTIVE", "run_id": row[0], "reason": None, "retry_after": None}
    return {"state": "HEALTHY" if row[2] in ("completed", "review") else "IDLE",
            "run_id": row[0], "reason": error or None, "retry_after": None}


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def decide(conn, task: Any, *, status_override: str | None = None) -> dict[str, Any]:
    """Derive the one authoritative semantic and dispatch decision for ``task``."""
    from hermes_cli.kanban_administrative_hold import administrative_pending
    from hermes_cli.kanban_completion_evidence import validate_contract
    from hermes_cli.kanban_completion_workflow import authority_wait
    from hermes_cli.kanban_readiness import preparation_reason

    t = _mapping(task)
    task_id = str(t["id"])
    status = status_override or str(t.get("status") or "")
    eligible = t.get("dispatch_eligible") is True or t.get("dispatch_eligible") == 1
    contract_event_id, contract = _contract(conn, task_id)
    dependencies = _dependency_state(conn, task_id)
    unsatisfied = [d for d in dependencies if not d["satisfied"]]
    health = _execution_health(conn, task_id)
    accepted = list(contract.get("accepted_completed_actions") or [])
    remaining = list(contract.get("remaining_required_actions") or [])
    evidence_rows = conn.execute(
        "SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN ("
        "'completion_evidence_accepted','review_approved','completed',"
        "'dependency_requirement_satisfied','preparation_qualified') ORDER BY id",
        (task_id,),
    ).fetchall()
    evidence_identities = [
        {"event_id": row[0], "kind": row[1],
         "identity": _json(row[2]).get("evidence_identity") or _json(row[2]).get("sha256")}
        for row in evidence_rows
    ]
    if not accepted:
        accepted = [f"{item['kind']} event {item['event_id']}" for item in evidence_identities]
    authority_row = _latest_event(conn, task_id, (
        "operator_authority_granted", "operator_authority_revoked",
        "operator_dispatch_release", "administrative_pending", "administrative_pending_released",
    ))
    authority_revision = authority_row[0] if authority_row else None
    selected = contract.get("selected_next_action")
    if not isinstance(selected, dict):
        selected = {}
    decision: dict[str, Any] = {
        "task_id": task_id, "native_status": status,
        "accepted_completed_actions": accepted,
        "accepted_evidence_identities": evidence_identities,
        "remaining_required_actions": remaining,
        "current_next_action": selected.get("action"),
        "current_next_action_type": selected.get("type"),
        "worker_executable_now": False,
        "required_authority": selected.get("required_authority"),
        "dependency_requirements": dependencies,
        "external_conditions": list(contract.get("external_conditions") or []),
        "current_owner": t.get("assignee"),
        "workflow_stage": "HELD", "dispatchable": False,
        "next_action": None, "owner": None,
        "reason": "No recognised executable action or release condition",
        "resume_condition": None, "auto_decompose_allowed": False,
        "execution_health": health, "invariant_violations": [],
    }

    active = bool(t.get("current_run_id") or t.get("claim_lock") or t.get("worker_pid"))
    if status in TERMINAL_STATUSES:
        stage = "DONE" if status == "done" else "SUPERSEDED"
        decision.update(workflow_stage=stage, reason="Accepted bounded scope is terminal",
                        owner=None, next_action=None, current_next_action=None)
        if remaining:
            decision["invariant_violations"].append("DONE && remaining_required_actions")
    elif active:
        decision.update(workflow_stage="RUNNING", dispatchable=True,
                        reason="A live claim owns the current action",
                        owner=t.get("assignee"), next_action="Await the current owner handoff",
                        resume_condition="current claim ends")
    elif health["state"] == "INFRASTRUCTURE_FAULT" and status in {"ready", "review"}:
        semantic = "REVIEW" if status == "review" else "READY"
        decision.update(
            workflow_stage=semantic, dispatchable=False, worker_executable_now=False,
            reason="Semantic action remains valid; deterministic launch environment is unhealthy",
            owner=t.get("assignee"),
            next_action="Run independent review" if semantic == "REVIEW" else
            (selected.get("action") or "Run the authorised action"),
            resume_condition="execution environment fingerprint changes",
        )
    elif administrative_pending(conn, task_id):
        event = _latest_event(conn, task_id, ("administrative_pending",))
        payload = _json(event[2]) if event else {}
        decision.update(workflow_stage="OPERATOR", administrative_pending=True,
                        reason=payload.get("reason") or
                        "A substantive operator decision is recorded",
                        owner="operator", next_action=payload.get("next_action") or
                        "Resolve the recorded operator decision",
                        resume_condition=payload.get("resume_condition") or
                        "operator records the decision")
    elif authority_wait(conn, task_id) is not None:
        wait = authority_wait(conn, task_id) or {}
        action = wait.get("action") or "Grant the named authority"
        decision.update(workflow_stage="WAITING", reason=action,
                        owner=wait.get("resolver") or "operator", next_action=action,
                        current_next_action=action,
                        current_next_action_type=wait.get("type") or "authority_grant",
                        required_authority=wait.get("required_authority") or wait.get("resolver"),
                        resume_condition=wait.get("resume_condition") or
                        "the named authority grant is recorded")
    elif status == "scheduled":
        decision.update(workflow_stage="WAITING", reason="Known action awaits its scheduled time",
                        owner=t.get("assignee"), next_action=selected.get("action") or
                        "Wait for the scheduled wake condition",
                        resume_condition=selected.get("resume_condition") or "scheduled time arrives")
    elif unsatisfied:
        names = ", ".join(f"{d['parent_id']}: {d['requirement']}" for d in unsatisfied)
        decision.update(workflow_stage="WAITING", reason=f"Unsatisfied dependency requirement: {names}",
                        owner="dependency resolver", next_action="Satisfy the named requirement",
                        resume_condition="accepted evidence satisfies each named requirement")
    else:
        explicitly_unqualified = contract.get("qualified_for_dispatch") is False
        guarded_contract = (
            str(t.get("idempotency_key") or "").startswith(("codex-goalpack-", "workflow-programme-"))
            or conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='completion_requirements' LIMIT 1",
                (task_id,),
            ).fetchone() is not None
        )
        try:
            validate_contract(contract)
            contract_valid = True
        except ValueError:
            # Preserve legacy intake until it enters the explicit contract
            # lifecycle. New guarded work fails closed and self-prepares.
            contract_valid = not guarded_contract
        prep = preparation_reason(conn, task_id)
        if explicitly_unqualified:
            decision.update(workflow_stage="PREPARE",
                            reason="Original scope and acceptance await qualification",
                            owner=t.get("assignee") or "preparation controller",
                            next_action="Qualify the authoritative scope, workspace, and acceptance",
                            current_next_action_type="preparation",
                            worker_executable_now=eligible)
        elif not contract_valid:
            deterministic = bool(str(t.get("title") or "").strip() and str(t.get("body") or "").strip())
            decision.update(workflow_stage="PREPARE" if deterministic else "TRIAGE",
                            reason="Completion contract must be materialised from authoritative scope"
                            if deterministic else "Authoritative scope is genuinely ambiguous",
                            owner="control plane" if deterministic else "operator",
                            next_action="Materialise deterministic scope and acceptance metadata"
                            if deterministic else "Clarify the bounded scope and acceptance",
                            current_next_action_type="deterministic_preparation" if deterministic else "scope_decision",
                            worker_executable_now=False)
        elif prep:
            decision.update(workflow_stage="PREPARE", reason=prep,
                            owner=t.get("assignee") or "preparation controller",
                            next_action=prep.removeprefix("Preparation required: ").strip(),
                            current_next_action_type="preparation",
                            worker_executable_now=eligible)
        elif status == "blocked":
            event = _latest_event(conn, task_id, ("blocked", "block_loop_detected"))
            payload = _json(event[2]) if event else {}
            if health["state"] == "INFRASTRUCTURE_FAULT" and payload.get("kind") in (None, "transient"):
                resume = payload.get("resume_status") or payload.get("source_status") or "ready"
                decision.update(workflow_stage="REVIEW" if resume == "review" else "READY",
                                reason="Semantic action remains valid; launch environment is unhealthy",
                                owner=t.get("assignee"), next_action=selected.get("action") or
                                ("Run independent review" if resume == "review" else "Run the authorised action"),
                                resume_condition="environment fingerprint changes",
                                worker_executable_now=False, dispatchable=False)
            else:
                reason = payload.get("reason")
                resume = payload.get("resume_condition") or payload.get("resume_status")
                resolver = payload.get("resolver")
                if not (reason and resume and resolver):
                    decision["invariant_violations"].append("BLOCKED lacks blocker/resolver/resume_condition")
                kind = payload.get("kind") or t.get("block_kind")
                if not resolver:
                    resolver = {"needs_input": "operator", "capability": "operator",
                                "transient": "dispatcher"}.get(kind)
                decision.update(workflow_stage="BLOCKED", block_kind=kind,
                                reason=reason or "Blocker classification is incomplete",
                                owner=resolver or t.get("assignee"),
                                next_action=payload.get("blocked_action") or selected.get("action") or
                                "Resolve the exact recorded fault",
                                resume_condition=resume)
        elif status == "review":
            handoff = _latest_event(conn, task_id, ("operator_review_handoff",))
            if handoff is not None:
                decision.update(workflow_stage="OPERATOR",
                                reason="Independent review is complete; a substantive acceptance decision remains",
                                owner="operator",
                                next_action="Accept retained evidence or request concrete changes",
                                resume_condition="operator records acceptance or requested changes")
            elif eligible:
                decision.update(workflow_stage="REVIEW", dispatchable=True,
                                worker_executable_now=True,
                                reason="A substantive independent review remains unresolved",
                                owner=t.get("assignee") or "reviewer",
                                next_action=selected.get("action") or
                                "Accept, reject, or request concrete changes",
                                current_next_action_type="review")
            else:
                decision.update(workflow_stage="HELD",
                                reason="Review claim is refused by dispatch_eligible=false",
                                owner="operator", next_action="Release or explain the eligibility hold",
                                resume_condition="dispatch eligibility changes")
                decision["invariant_violations"].append(
                    "REVIEW advertised runnable but claim rejects")
        elif status in ("ready", "todo") and eligible:
            action = selected.get("action") or "Execute the bounded accepted scope"
            decision.update(workflow_stage="READY", dispatchable=True,
                            worker_executable_now=True, reason="All current action prerequisites are satisfied",
                            owner=t.get("assignee"), next_action=action,
                            current_next_action=action,
                            current_next_action_type=selected.get("type") or "worker_action")
        elif status == "triage":
            children = conn.execute("SELECT 1 FROM task_links WHERE parent_id=? LIMIT 1", (task_id,)).fetchone()
            unknown = not selected.get("action")
            decision.update(workflow_stage="TRIAGE", reason="The next required action is unknown",
                            owner=t.get("assignee") or "triage controller",
                            next_action="Derive one bounded unsatisfied requirement")
            decision["auto_decompose_allowed"] = bool(unknown and remaining and not children)
        else:
            decision.update(workflow_stage="HELD",
                            reason="No authorised worker-executable action is currently enabled",
                            owner="operator", next_action="Inspect the explicit eligibility hold",
                            resume_condition="the hold is explicitly released")

    if decision["workflow_stage"] == "READY" and not decision["worker_executable_now"]:
        decision["invariant_violations"].append("READY && no worker action")
        decision["dispatchable"] = False
    if decision["workflow_stage"] in {"WAITING", "OPERATOR", "HELD", "DONE", "SUPERSEDED"}:
        decision["auto_decompose_allowed"] = False

    fingerprint_inputs = {
        "contract_event_id": contract_event_id,
        "contract": contract,
        "accepted_evidence_identities": evidence_identities,
        "operator_authority_revision": authority_revision,
        "dependencies": dependencies,
        "status": status,
        "dispatch_eligible": eligible,
        "ownership": [t.get("assignee"), t.get("workspace_kind"), t.get("workspace_path"),
                      t.get("current_run_id"), t.get("claim_lock")],
        "stage": decision["workflow_stage"],
        "next_action": decision["next_action"],
        "execution_health": health,
    }
    decision["decision_fingerprint"] = _fingerprint(fingerprint_inputs)
    decision["invalidated_by"] = [
        "contract revision", "accepted evidence identity", "authority revision",
        "dependency requirement state", "workspace/source identity", "selected next action",
        "claim ownership", "execution environment fingerprint",
    ]
    return decision


def claim_allowed(conn, task: Any, lane: str) -> tuple[bool, dict[str, Any]]:
    decision = decide(conn, task)
    expected = "REVIEW" if lane == "review" else "READY"
    return bool(decision["workflow_stage"] == expected and decision["dispatchable"]), decision


def auto_decompose_allowed(conn, task: Any) -> tuple[bool, dict[str, Any]]:
    decision = decide(conn, task)
    if not decision["auto_decompose_allowed"]:
        return False, decision
    previous = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? "
        "AND kind='control_decision_recorded' ORDER BY id DESC LIMIT 1",
        (decision["task_id"],),
    ).fetchone()
    payload = _json(previous[0]) if previous else {}
    unchanged_no_work = (
        payload.get("decision_fingerprint") == decision["decision_fingerprint"]
        and payload.get("outcome") == "no_worker_executable_action"
    )
    return not unchanged_no_work, decision


def record_decision(conn, task_id: str, decision: dict[str, Any], outcome: str) -> bool:
    """Append one assessment per fingerprint/outcome; unchanged ticks are no-ops."""
    from hermes_cli import kanban_db as kb
    previous = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? "
        "AND kind='control_decision_recorded' ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = {
        "decision_fingerprint": decision["decision_fingerprint"],
        "workflow_stage": decision["workflow_stage"],
        "next_action": decision["next_action"], "outcome": outcome,
        "invalidated_by": decision["invalidated_by"],
    }
    if previous and _json(previous[0]) == payload:
        return False
    kb._append_event(conn, task_id, "control_decision_recorded", payload)
    return True
