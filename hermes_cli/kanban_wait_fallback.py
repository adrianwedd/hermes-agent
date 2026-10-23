"""Contracts for existing native prerequisites, without inventing prose predicates."""


def fields(conn, task_id, decision):
    from hermes_cli.kanban_wait_contract import record
    cid, contract, _ = record(conn, task_id)
    dependencies = [d for d in decision.get("dependency_requirements", []) if not d["satisfied"]]
    external = [d for d in decision.get("external_conditions", []) if not d["satisfied"]]
    stage = decision["workflow_stage"]
    mode = "automatically_observed" if dependencies else "manual_attestation" if external else "unsupported"
    predicate = {"evaluator": "native_dependencies", "dependencies": dependencies} if dependencies else {"external_conditions": external}
    if stage == "PREPARE" and not dependencies and not external:
        mode = "automatically_observed"
        row = conn.execute("SELECT workspace_kind,workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
        predicate = {"evaluator": "native_preparation", "call": "native preparation requirements are all true", "task_id": task_id,
                     "requirements": preparation_requirements(conn, task_id, decision, contract),
                     "contract_event_id": cid, "qualified_for_dispatch": contract.get("qualified_for_dispatch"),
                     "workspace_kind": row[0], "workspace_path": row[1]}
    if stage in {"READY", "REVIEW"} and decision.get("execution_health", {}).get("state") == "INFRASTRUCTURE_FAULT":
        mode = "automatically_observed"
        predicate = {"evaluator": "execution_environment_changed", "execution_health": decision["execution_health"]}
    if mode == "unsupported":
        predicate = {"evaluator": "unsupported", "native_stage": stage, "declaration_required": True}
    decision.update(why_not_executable=decision.get("reason"), predicate=predicate,
                    evaluation_mode=mode, resolver=decision.get("owner") or "control plane",
                    wake_trigger=["parent_state_transition", "dependency_requirement_satisfied", "external_condition_satisfied", "workspace_prepared", "qualification_evidence_accepted", "completion_requirements", "dispatcher_tick", "restart_reconciliation"],
                    current_unblock_action=decision.get("next_action"),
                    post_gate_continuation={"state": "review" if decision.get("native_status") == "review" else "todo", "selected_next_action": contract.get("selected_next_action")},
                    remaining_gates=dependencies + external + ([{"identity": "native_preparation", "evaluator": "native_preparation", "satisfied": False, "parameters": predicate}] if stage == "PREPARE" else []))
    if stage in {"READY", "REVIEW"} and decision.get("execution_health", {}).get("state") == "INFRASTRUCTURE_FAULT" and not dependencies and not external:
        action = "Repair the owned execution environment and observe its changed healthy fingerprint"
        decision.update(next_action=action, current_next_action=action, current_unblock_action=action, remaining_gates=[{"identity": "execution_environment", "evaluator": "execution_environment_changed", "satisfied": False, "parameters": predicate}])
    if external:
        action = "Obtain authorised evidence for external condition " + external[0]["condition_id"]
        decision.update(next_action=action, current_next_action=action, current_unblock_action=action)
    return decision


def preparation_requirements(conn, task_id, decision, contract):
    from hermes_cli.kanban_completion_evidence import validate_contract
    from hermes_cli.kanban_readiness import preparation_reason
    try:
        validate_contract(contract)
        valid = True
    except ValueError:
        valid = False
    return {"completion_contract_valid": valid,
            "native_scope_qualification_allows": contract.get("qualified_for_dispatch") is not False,
            "native_readiness_passes": preparation_reason(conn, task_id) == "",
            "next_action_selected_or_native_review": bool(decision.get("current_next_action")) or decision.get("native_status") == "review"}
