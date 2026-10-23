"""Canonical next-action decision shared by every Kanban control-plane consumer.

The card is a container for scope and evidence.  Scheduling is driven by the
single next required action derived here.  This module is deliberately
read-only: mutations become visible immediately because the fingerprint and
decision are recomputed from the same native rows the claim guard reads.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


TERMINAL_STATUSES = {"done", "archived"}
INFRA_ERROR_MARKERS = (
    "dependency environment", "no dependency environment", "bootstrap",
    "module not found", "modulenotfounderror", "spawn failed", "spawn_failed",
    "executable not found", "environment is committed",
)


def infrastructure_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in INFRA_ERROR_MARKERS)


def _workspace_install_root(workspace_path: str | None) -> Path | None:
    """Return a task-local Hermes source root when its cwd shadows the host install."""
    if not workspace_path:
        return None
    root = Path(workspace_path).resolve()
    if (root / "hermes_bootstrap.py").is_file() and (root / "hermes_cli").is_dir():
        return root
    return None


def environment_fingerprint(root_override: str | Path | None = None) -> dict[str, Any]:
    """Current install dependency identity, read without repairing or mutating it."""
    try:
        from pm.environments import committed_venv, runtime_facts_path
        from pm.paths import install_root

        root = Path(root_override).resolve() if root_override else install_root().resolve()
        facts = runtime_facts_path(root)
        environment = committed_venv(root)
        payload = facts.read_bytes() if facts.is_file() else b""
        identity = hashlib.sha256(
            str(root).encode() + b"\0" + payload + b"\0" +
            str(environment or "").encode()
        ).hexdigest()
        return {
            "identity": identity,
            "healthy": bool(environment and (environment / "pyvenv.cfg").is_file()),
            "install_root": str(root),
            "environment": str(environment) if environment else None,
        }
    except Exception as exc:
        logger.exception("could not fingerprint the committed execution environment")
        return {
            "identity": hashlib.sha256(str(exc).encode()).hexdigest(),
            "healthy": False,
            "reason": str(exc),
        }


def execution_environment_fingerprint(workspace_path: str | None = None) -> dict[str, Any]:
    """Fingerprint the install a worker launched from this workspace will import."""
    workspace_root = _workspace_install_root(workspace_path)
    return environment_fingerprint(workspace_root) if workspace_root else environment_fingerprint()


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


def _dependency_waivers(conn, task_id: str) -> dict[str, dict[str, Any]]:
    """Latest operator waiver per dependency, keyed by ``(parent_id, requirement_id)``.

    A *waiver* is the one event that lets a specific requirement stop gating
    dispatch without pretending the producer delivered anything: it names the
    parent, the exact requirement identity and the declaration it answers. Any
    waiver older than the requirement declaration it names is stale and ignored.
    """
    waivers: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT id,payload FROM task_events WHERE task_id=? "
        "AND kind='dependency_requirement_waived' AND json_valid(payload) "
        "ORDER BY id", (task_id,),
    ).fetchall()
    for row in rows:
        payload = _json(row[1])
        parent_id = payload.get("parent_id")
        if not parent_id:
            continue
        key = f"{parent_id}\x00{payload.get('requirement_id')}"
        previous = waivers.get(key)
        if previous is None or int(row[0]) > int(previous.get("_event_id") or 0):
            waivers[key] = {**payload, "_event_id": int(row[0])}
    return waivers


def _dependency_state(conn, task_id: str) -> list[dict[str, Any]]:
    """Return specific dependency requirements, retaining legacy edges safely.

    New requirement metadata is append-only on the downstream card.  A matching
    ``dependency_requirement_satisfied`` event releases the requirement even if
    the producer card has unrelated work left.  Legacy edges retain their old
    terminal requirement until migrated, but are named as such rather than
    pretending the entire parent is intrinsically the scheduling unit.

    An operator *waiver* (``dependency_requirement_waived``) also releases the
    requirement, but it is reported as ``waived`` with the parent's real status:
    the upstream output was never verified and the producer is still open.
    """
    rows = conn.execute(
        "SELECT p.id,p.status FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY p.id", (task_id,),
    ).fetchall()
    waivers = _dependency_waivers(conn, task_id)
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
            "SELECT id,payload FROM task_events WHERE task_id=? "
            "AND kind='dependency_requirement_satisfied' AND json_valid(payload) "
            "AND json_extract(payload,'$.requirement_id')=? AND id>? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, requirement_id, int(declared[0]) if declared else 0),
        ).fetchone()
        accepted_payload = _json(accepted[1]) if accepted else {}
        from hermes_cli.kanban_dependency_output import accepted as accepted_output
        satisfied = (accepted_output(conn, task_id, int(declared[0]), payload, accepted_payload)
                     if declared and accepted else not declared and status in TERMINAL_STATUSES)
        waiver = waivers.get(f"{parent_id}\x00{requirement_id}")
        declaration_id = int(declared[0]) if declared else 0
        waiver_id = int((waiver or {}).get("_event_id") or 0)
        waiver_is_current = bool(waiver and waiver_id > declaration_id
                                 and waiver_id > (int(accepted[0]) if accepted else 0))
        waived = waiver_is_current and not satisfied
        record = {
            "parent_id": parent_id,
            "requirement_id": requirement_id,
            "declaration_event_id": int(declared[0]) if declared else None,
            "requirement": payload.get("requirement") or "parent terminal (legacy edge)",
            "evidence_identity": payload.get("evidence_identity"),
            "satisfied": satisfied or waived,
            "parent_status": status,
        }
        if waived:
            # Never dress a waiver up as delivered output: the upstream producer
            # is still open and no evidence identity was verified.
            record.update(
                waived=True,
                waiver_event_id=waiver_id,
                waiver_reason=(waiver or {}).get("reason"),
                upstream_verified=False,
                detail=(f"operator waiver: upstream parent {parent_id} remains "
                        f"{status} and its output was not verified"),
            )
        result.append(record)
    return result


def _external_condition_state(
    conn, task_id: str, contract_event_id: int | None, contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve declared external conditions against evidence newer than the contract.

    A declaration revision invalidates older satisfaction evidence.  Conditions
    without an explicit ``satisfied`` value fail closed until a matching
    ``external_condition_satisfied`` event is recorded.
    """
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(contract.get("external_conditions") or []):
        condition = dict(raw) if isinstance(raw, dict) else {"requirement": str(raw)}
        condition_id = str(condition.get("condition_id") or condition.get("id") or f"condition-{index + 1}")
        event = conn.execute(
            "SELECT id,payload FROM task_events WHERE task_id=? "
            "AND kind='external_condition_satisfied' AND id>? AND json_valid(payload) "
            "AND json_extract(payload,'$.condition_id')=? ORDER BY id DESC LIMIT 1",
            (task_id, int(contract_event_id or 0), condition_id),
        ).fetchone()
        evidence = _json(event[1]) if event else {}
        expected_identity = condition.get("evidence_identity")
        satisfied = condition.get("satisfied") is True or bool(
            event and (not expected_identity or evidence.get("evidence_identity") == expected_identity)
        )
        native_gate = next((g for g in (contract.get("wait_contract") or {}).get("gates", [])
                            if g["parameters"].get("condition_id") == condition_id), None)
        if native_gate:
            from hermes_cli.kanban_wait_contract import evaluate
            satisfied = evaluate(conn, task_id, native_gate)
        else:
            # Legacy prose/boolean/event strings are not authorised attestations.
            satisfied = False
        condition.update(
            condition_id=condition_id,
            declaration_event_id=contract_event_id,
            satisfaction_event_id=int(event[0]) if event else None,
            satisfied=satisfied,
        )
        result.append(condition)
    return result


def dependencies_satisfied(conn, task_id: str) -> bool:
    return all(item["satisfied"] for item in _dependency_state(conn, task_id))


def _operator_decision_pending(conn, task_id: str) -> dict[str, Any] | None:
    row = _latest_event(conn, task_id, (
        "operator_decision_pending", "operator_decision_resolved",
    ))
    if row is None:
        return None
    if row[1] == "operator_decision_resolved":
        pending = _latest_event(conn, task_id, ("operator_decision_pending",))
        resolution = _json(row[2])
        if pending is None:
            return None
        if (resolution.get("pending_event_id") != pending[0] or resolution.get("authorised") is not True
                or resolution.get("actor") != "operator"):
            return _json(pending[2])
        from hermes_cli.kanban_wait_contract import record, gate_event
        _, _, wait = record(conn, task_id)
        gate = next((g for g in (wait or {}).get("gates", []) if g["evaluator"] == "operator_decision" and g["parameters"].get("pending_event_id") == pending[0]), None)
        receipt = gate_event(conn, task_id, gate) if gate else None
        if not receipt or receipt["kind"] != "wait_gate_satisfied":
            return _json(pending[2])
        return None
    return _json(row[2])


def _execution_health(conn, task_id: str) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT id,status,outcome,error,ended_at FROM task_runs WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
    except Exception:
        logger.exception("could not read latest task-run execution health")
        row = None
    if row is None:
        return {"state": "UNKNOWN", "reason": None, "retry_after": None}
    error = str(row[3] or "")
    if infrastructure_error(error):
        workspace_row = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        current = execution_environment_fingerprint(workspace_row[0] if workspace_row else None)
        metadata_row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (row[0],),
        ).fetchone()
        metadata = _json(metadata_row[0]) if metadata_row else {}
        failed_identity = metadata.get("environment_fingerprint")
        if current.get("healthy") and (
            failed_identity is None or failed_identity != current.get("identity")
        ):
            return {
                "state": "HEALTHY", "run_id": row[0],
                "reason": "Execution environment recovered after the recorded infrastructure fault",
                "retry_after": None, "environment_fingerprint": current,
                "recovered_from": error,
            }
        return {
            "state": "INFRASTRUCTURE_FAULT", "run_id": row[0], "reason": error,
            "retry_after": "environment_fingerprint_changes", "semantic_stage_unchanged": True,
            "environment_fingerprint": current,
        }
    if row[1] == "running":
        return {"state": "ACTIVE", "run_id": row[0], "reason": None, "retry_after": None}
    return {"state": "HEALTHY" if row[2] in ("completed", "review") else "IDLE",
            "run_id": row[0], "reason": error or None, "retry_after": None}


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


# health: allow CC,FUNC_LINES -- one ordered authoritative state table keeps transition precedence auditable
def decide(conn, task: Any, *, status_override: str | None = None, _wait_projection=None, _counterfactual=None, _predict=True) -> dict[str, Any]:
    """Derive the one authoritative semantic and dispatch decision for ``task``."""
    from hermes_cli.kanban_administrative_hold import administrative_pending
    from hermes_cli.kanban_completion_evidence import validate_contract
    from hermes_cli.kanban_completion_workflow import authority_wait
    from hermes_cli.kanban_readiness import preparation_reason

    t = _mapping(task)
    from hermes_cli.kanban_wait_contract import facts as wait_facts, project as wait_project
    native_wait = wait_facts(conn, str(t["id"]), satisfied_gate=(_counterfactual or {}).get("gate_id"))
    if (_wait_projection is None and native_wait is not None and not native_wait["remaining_gates"]
            and t.get("status") not in TERMINAL_STATUSES
            and not any(t.get(k) for k in ["current_run_id", "worker_pid", "claim_lock"])):
        projected, adjusted = wait_project(conn, t, native_wait, decision_selection=(_counterfactual or {}).get("decision_selection"))
        if projected is not None:
            return decide(conn, projected, _wait_projection=adjusted, _counterfactual=_counterfactual, _predict=_predict)
    task_id = str(t["id"])
    current_block = _latest_event(conn, task_id, ("blocked",))
    stop_revoked = any(g["evaluator"] == "explicit_stop" and g["satisfied"] and current_block
                       and g["parameters"].get("stop_event_id") == current_block[0] for g in (native_wait or {}).get("gates", []))
    status = status_override or str(t.get("status") or "")
    eligible = t.get("dispatch_eligible") is True or t.get("dispatch_eligible") == 1
    contract_event_id, contract = _contract(conn, task_id)
    if _wait_projection is not None:
        contract = _wait_projection
    dependencies = _dependency_state(conn, task_id)
    if (_counterfactual or {}).get("dependencies"):
        dependencies = [{**d, "satisfied": True} for d in dependencies]
    unsatisfied = [d for d in dependencies if not d["satisfied"]]
    external_conditions = _external_condition_state(conn, task_id, contract_event_id, contract)
    counter_gate = (_counterfactual or {}).get("gate_id")
    if counter_gate and native_wait:
        targeted = next((g for g in native_wait["gates"] if g["id"] == counter_gate), {})
        condition_id = targeted.get("parameters", {}).get("condition_id")
        if condition_id:
            external_conditions = [{**item, "satisfied": item["satisfied"] or item["condition_id"] == condition_id} for item in external_conditions]
    if (_counterfactual or {}).get("external"):
        external_conditions = [{**d, "satisfied": True} for d in external_conditions]
    unmet_external = [item for item in external_conditions if not item["satisfied"]]
    health = _execution_health(conn, task_id)
    if (_counterfactual or {}).get("execution_health"):
        health = {**health, "state": "HEALTHY", "counterfactual": True}
    from hermes_cli.kanban_wait_runtime import retry_state
    retry = retry_state(conn, t)
    accepted = list(contract.get("accepted_completed_actions") or [])
    remaining = list(contract.get("remaining_required_actions") or [])
    remaining_declared = "remaining_required_actions" in contract
    evidence_rows = conn.execute(
        "SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN ("
        "'completion_evidence_accepted','review_approved','completed',"
        "'dependency_requirement_satisfied') ORDER BY id",
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
        "operator_decision_pending", "operator_decision_resolved",
    ))
    authority_revision = authority_row[0] if authority_row else None
    selected = contract.get("selected_next_action")
    if not isinstance(selected, dict):
        selected = {}
    if contract_event_id is None and str(t.get("title") or "").strip():
        # Pre-contract legacy cards still carry one authoritative bounded scope:
        # their title/body. Deriving that exact action preserves old boards
        # without inventing generic work from nonterminal status. Once a native
        # contract exists, omission of selected_next_action fails closed.
        selected = {
            "action": str(t.get("title")).strip(),
            "type": "legacy_authoritative_scope",
            "required_authority": None,
        }
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
        "external_conditions": external_conditions,
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
    # Explicit stop is a native authority boundary. A caller may supply a
    # semantic resume-status override for ordinary Blocked recovery, but that
    # must never hide the row's recorded operator prohibition.
    elif t.get("status") == "blocked" and t.get("block_kind") == "explicit_stop" and not stop_revoked:
        decision.update(
            workflow_stage="HELD", block_kind="explicit_stop",
            reason="Operator explicit stop remains in force",
            owner="operator", next_action="Preserve the stopped state",
            resume_condition="operator explicitly revokes the stop",
        )
    elif (_counterfactual or {}).get("operator") is not True and (operator_decision := _operator_decision_pending(conn, task_id)):
        decision.update(
            workflow_stage="OPERATOR",
            reason=operator_decision.get("reason") or "A substantive operator decision remains",
            owner=operator_decision.get("resolver") or "operator",
            next_action=operator_decision.get("next_action") or "Resolve the named operator decision",
            resume_condition=operator_decision.get("resume_condition") or
            "operator records the decision",
        )
    elif administrative_pending(conn, task_id):
        event = _latest_event(conn, task_id, ("administrative_pending",))
        payload = _json(event[2]) if event else {}
        decision.update(workflow_stage="WORKFLOW_FAULT", administrative_pending=True,
                        reason=payload.get("reason") or "Completion administration has not been reconciled",
                        owner="control plane", next_action="Bind retained evidence and close natively",
                        resume_condition="control plane reconciles retained completion evidence")
    elif _wait_projection is None and authority_wait(conn, task_id) is not None:
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
        scheduled = _latest_event(conn, task_id, ("scheduled",))
        wait = _json(scheduled["payload"]) if scheduled is not None else {}
        condition = wait.get("condition") or wait.get("reason")
        if condition:
            decision.update(workflow_stage="WAITING", reason=condition,
                            owner=wait.get("resolver") or selected.get("resolver") or t.get("assignee"),
                            next_action=wait.get("next_action") or "Satisfy the recorded wake condition",
                            resume_condition=wait.get("resume_condition") or selected.get("resume_condition") or condition)
        else:
            decision.update(workflow_stage="WORKFLOW_FAULT", owner="control plane",
                            reason="Scheduled placement has no recorded wake condition",
                            next_action="Recover the exact retained wait condition and its resolver",
                            resume_condition="a concrete wake condition is recorded")
    elif unsatisfied:
        names = ", ".join(f"{d['parent_id']}: {d['requirement']}" for d in unsatisfied)
        decision.update(workflow_stage="WAITING", reason=f"Unsatisfied dependency requirement: {names}",
                        owner="dependency resolver", next_action="Satisfy the named requirement",
                        resume_condition="accepted evidence satisfies each named requirement")
    elif unmet_external:
        condition = unmet_external[0]
        action = condition.get("action") or selected.get("action") or "Wait for the named external condition"
        decision.update(
            workflow_stage="WAITING",
            reason=condition.get("requirement") or f"External condition {condition['condition_id']} is not satisfied",
            owner=condition.get("resolver") or selected.get("resolver") or "external resolver",
            next_action=action,
            current_next_action=action,
            current_next_action_type=condition.get("type") or selected.get("type") or "external_condition",
            resume_condition=condition.get("resume_condition") or selected.get("resume_condition") or
            f"external condition {condition['condition_id']} is satisfied",
        )
    elif not retry["satisfied"]:
        decision.update(workflow_stage="BLOCKED", dispatchable=False, worker_executable_now=False,
                        reason="Retry budget exhausted: " + str(retry["counter"]) + "/" + str(retry["limit"]),
                        owner="dispatcher", next_action="Repair the failure or change the authorised retry policy",
                        retry_budget=retry, resume_condition="consecutive_failures < effective retry limit")
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
    else:
        from hermes_cli.kanban_decision_action import apply_action_decision
        apply_action_decision(conn, t, decision, contract, status, eligible, selected, health, accepted, remaining, remaining_declared, evidence_identities, preparation_satisfied=bool((_counterfactual or {}).get("preparation")))

    if decision["workflow_stage"] == "READY" and not decision["current_next_action"]:
        decision["invariant_violations"].append("READY && no worker action")
        decision["dispatchable"] = False
    if decision["workflow_stage"] in {"WAITING", "OPERATOR", "HELD", "DONE", "SUPERSEDED"}:
        decision["auto_decompose_allowed"] = False

    from hermes_cli.kanban_wait_contract import decision_fields
    if status not in TERMINAL_STATUSES and not active:
        decision_fields(conn, task_id, decision, wait_override=native_wait)
        if _predict:
            from hermes_cli.kanban_wait_prediction import predict
            decision["state_if_satisfied_now"] = predict(conn, task, decision, native_wait)
    fingerprint_inputs = {
        "contract_event_id": contract_event_id,
        "contract": contract,
        "native_wait_facts": native_wait,
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
