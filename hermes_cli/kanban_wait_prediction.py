"""Pure next-state counterfactuals; never writes events or provisions workspaces."""

def predict(conn, task, decision, wait):
    from hermes_cli.kanban_decision import decide
    counterfactual = None
    if wait and wait["remaining_gates"]:
        remaining = sorted(wait["remaining_gates"], key=lambda g: {"HELD": 0, "OPERATOR": 1, "BLOCKED": 2}.get(g["stage"], 3))
        gate = remaining[0]
        if gate["evaluator"] == "unsupported":
            return {"state": "WORKFLOW_FAULT", "dispatchable": False, "reason": "No encoded satisfaction transition; declare a supported contract first"}
        counterfactual = {"gate_id": gate["id"], "operator": gate["evaluator"] == "operator_decision"}
        choices = gate["parameters"].get("decision_values", {}).get("selection", [])
        if choices:
            outcomes = {}
            for choice in choices:
                result = decide(conn, task, _counterfactual={**counterfactual, "decision_selection": {"selection": choice}}, _predict=False)
                outcomes[choice] = {"state": result["workflow_stage"], "dispatchable": result["dispatchable"], "reason": result["reason"]}
            return {"state": "DECISION_DEPENDENT", "dispatchable": False, "reason": "The declared selection determines the continuation", "decision_outcomes": outcomes}
    elif any(not x["satisfied"] for x in decision.get("dependency_requirements", [])):
        counterfactual = {"dependencies": True}
    elif any(not x["satisfied"] for x in decision.get("external_conditions", [])):
        counterfactual = {"external": True}
    elif decision["workflow_stage"] in {"READY", "REVIEW"} and decision.get("execution_health", {}).get("state") == "INFRASTRUCTURE_FAULT":
        counterfactual = {"execution_health": True}
    elif decision["workflow_stage"] == "PREPARE" and decision.get("predicate", {}).get("evaluator") == "native_preparation":
        counterfactual = {"preparation": True}
    if counterfactual is None:
        return {"state": decision["workflow_stage"], "dispatchable": decision["dispatchable"], "reason": decision["reason"]}
    after = decide(conn, task, _counterfactual=counterfactual, _predict=False)
    return {"state": after["workflow_stage"], "dispatchable": after["dispatchable"], "reason": after["reason"], "remaining_gates": after.get("remaining_gates", [])}
