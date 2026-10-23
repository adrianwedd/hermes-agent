"""Shared native condition evaluation at normal and operator completion."""


def condition_state(conn, task_id, contract_event_id, contract, field):
    """Keep receipts bound to the exact current condition, including revisions.

    Completion conditions use the same native gates as dispatch conditions.
    Projecting a different condition list must retain its wait contract without
    allowing a receipt for old condition content to satisfy the new declaration.
    """
    from hermes_cli.kanban_decision import _external_condition_state
    from hermes_cli.kanban_wait_contract import condition_digest

    declared = (contract or {}).get(field) or []
    wait = (contract or {}).get("wait_contract") or {}
    gates = [
        gate for gate in wait.get("gates", [])
        if not gate["parameters"].get("native_condition_sha256") or any(
            gate["parameters"]["native_condition_sha256"] == condition_digest(condition)
            for condition in declared if isinstance(condition, dict)
        )
    ]
    return _external_condition_state(conn, task_id, contract_event_id, {
        "external_conditions": declared, "wait_contract": {**wait, "gates": gates},
    })
