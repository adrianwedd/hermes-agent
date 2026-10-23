"""Atomic resume-condition transitions for known WAITING/BLOCKED actions."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any


def satisfy_resume_condition(
    conn, task_id: str, *, condition: str, evidence: dict[str, Any],
    next_action: str, expected_status: str | None = None,
    expected_decision_fingerprint: str | None = None, board: str | None = None,
    condition_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility boundary: resolve a declared condition, never arbitrary prose."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import decide
    from hermes_cli.kanban_wait_contract import facts
    from hermes_cli.kanban_wait_migration import migrate
    from hermes_cli.kanban_wait_transitions import resolve_gate, require_operator
    require_operator(conn)
    task = kb.get_task(conn, task_id)
    if not task or task.block_kind == "explicit_stop":
        raise PermissionError("Resume evidence cannot revoke STOP")
    before = decide(conn, task)
    if expected_status is not None and task.status != expected_status:
        raise RuntimeError("resume snapshot changed")
    if expected_decision_fingerprint and before["decision_fingerprint"] != expected_decision_fingerprint:
        raise RuntimeError("resume decision fingerprint changed")
    migrate(conn, [task_id])
    wait = facts(conn, task_id)
    gate = next((g for g in (wait or {}).get("remaining_gates", []) if g["parameters"].get("condition_id") == condition_id), None)
    if gate is None:
        raise ValueError("No matching native condition; declare its predicate first")
    if gate["evaluator"] == "observation":
        from hermes_cli.kanban_wait_observation import observe
        observe(conn, board=board)
        if not next(g for g in facts(conn, task_id)["gates"] if g["id"] == gate["id"])["satisfied"]:
            raise ValueError("Registered probe did not satisfy the native observation")
        return decide(conn, kb.get_task(conn, task_id))
    return resolve_gate(conn, task_id, gate["id"], revision=gate["revision"], declaration=gate["declaration"], evidence=evidence)


def _notebooklm_auth_probe() -> dict[str, Any] | None:
    """Return bounded non-secret evidence when the configured NLM login is valid."""
    binary = shutil.which("nlm")
    if binary is None:
        return {"probe": "notebooklm_auth", "exit_code": 127, "failure": "binary_unavailable"}
    try:
        result = subprocess.run(
            [binary, "login", "--check"], capture_output=True, text=True,
            timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"probe": "notebooklm_auth", "exit_code": 1, "failure": "probe_unavailable"}
    if result.returncode != 0:
        return {"probe": "notebooklm_auth", "exit_code": result.returncode, "failure": "login_check_failed"}
    output = (result.stdout + "\n" + result.stderr)[-4000:]
    count = re.search(r"\b(\d+)\s+notebooks?\b", output, re.IGNORECASE)
    profile = re.search(r"\bprofile\s*[:=]\s*([^\s,]+)", output, re.IGNORECASE)
    return {
        "probe": "notebooklm_auth", "exit_code": 0,
        "profile": profile.group(1) if profile else "default",
        "notebooks_visible": int(count.group(1)) if count else None,
    }


_PROBES = {"notebooklm_auth": _notebooklm_auth_probe}


def observe_resume_conditions(
    conn, *, board: str | None = None,
    probes: dict[str, Any] | None = None,
) -> list[str]:
    """Use the first-class wait boundary for registered observations."""
    from hermes_cli.kanban_wait_observation import observe
    return observe(conn, board=board, probes=probes)
