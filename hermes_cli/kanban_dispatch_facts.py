"""Dashboard compatibility view over the canonical control-plane decision."""
from __future__ import annotations

from hermes_cli.kanban_decision import TERMINAL_STATUSES, decide


STAGE_COLUMNS = {
    "TRIAGE": "triage", "PREPARE": "todo", "READY": "ready",
    "RUNNING": "running", "REVIEW": "review", "OPERATOR": "blocked",
    "BLOCKED": "blocked", "HELD": "blocked",
    "DONE": "done", "SUPERSEDED": "archived",
}


def board_column(stage, status):
    if stage == "WAITING":
        # The existing Scheduled lane is the board's non-dispatchable known-
        # wake-condition lane. Never leak a stale raw ``ready`` status into the
        # Ready column while the canonical claim guard says WAITING.
        return "scheduled"
    return STAGE_COLUMNS.get(stage, status)


def dispatch_facts(conn, task):
    """Return renderer fields without reclassifying the task.

    All semantics come from :func:`kanban_decision.decide`; this adapter only
    preserves the JSON names consumed by existing desktop/browser clients.
    """
    decision = decide(conn, task)
    stage = decision["workflow_stage"]
    status = task.get("status") if isinstance(task, dict) else getattr(task, "status", None)
    eligible = task.get("dispatch_eligible") if isinstance(task, dict) else getattr(task, "dispatch_eligible", None)
    labels = {
        "TRIAGE": "TRIAGE · next action unknown",
        "PREPARE": "PREPARE · useful preparation",
        "READY": "READY · authorised action",
        "RUNNING": "RUNNING · active owner",
        "REVIEW": "REVIEW · awaiting acceptance",
        "OPERATOR": "OPERATOR · decision required",
        "WAITING": "WAITING · known resume condition",
        "BLOCKED": "BLOCKED · exact fault",
        "HELD": "HELD · explicit stop",
        "DONE": "Completed",
        "SUPERSEDED": "Archived",
    }
    label = labels.get(stage, stage)
    if decision.get("administrative_pending"):
        label = "OPERATOR · receipt administration"
    if stage == "BLOCKED" and decision.get("block_kind"):
        label = "BLOCKED · " + str(decision["block_kind"])
    reason = decision["reason"]
    if stage in {"DONE", "SUPERSEDED"}:
        result = task.get("result") if isinstance(task, dict) else getattr(task, "result", None)
        reason = ("Final resolution: " + str(result) + ". " if result else "Final resolution recorded. ") + reason
    next_action = decision["next_action"]
    if decision.get("administrative_pending"):
        next_action = "Bind retained evidence and close natively; do not rerun completed work"
    return {
        "eligible": eligible,
        "terminal": stage in {"DONE", "SUPERSEDED"},
        "stage": stage,
        "dispatchable": decision["dispatchable"],
        "column": (
            "execution" if decision["execution_health"]["state"] == "INFRASTRUCTURE_FAULT"
            else board_column(stage, status)
        ),
        "label": label,
        "reason": reason,
        "owner": decision["owner"],
        "next_action": next_action,
        "resume_condition": decision["resume_condition"],
        "basis": "Canonical control-plane decision " + decision["decision_fingerprint"][:12],
        "decision_fingerprint": decision["decision_fingerprint"],
        "worker_executable_now": decision["worker_executable_now"],
        "execution_health": decision["execution_health"],
        "accepted_completed_actions": decision["accepted_completed_actions"],
        "accepted_evidence_identities": decision["accepted_evidence_identities"],
        "remaining_required_actions": decision["remaining_required_actions"],
        "required_authority": decision["required_authority"],
        "dependency_requirements": decision["dependency_requirements"],
        "external_conditions": decision["external_conditions"],
        "auto_decompose_allowed": decision["auto_decompose_allowed"],
        "invariant_violations": decision["invariant_violations"],
        "block_kind": decision.get("block_kind"),
        "phase": "WAITING_FOR_AUTHORITY" if decision.get("current_next_action_type") == "authority_grant" else None,
        "completed_at": task.get("completed_at") if isinstance(task, dict) else getattr(task, "completed_at", None),
    }
