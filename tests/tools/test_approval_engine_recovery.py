"""Smart-approval transport failures remain distinct from decisions."""

import asyncio
import contextvars
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import approval, code_execution_tool, terminal_tool
from tools.approval_smart import _smart_approve


def test_guardian_runs_off_event_loop_with_profile_context():
    scope = contextvars.ContextVar("approval_test_scope")
    scope.set("researcher")

    def guardian(**_kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert scope.get() == "researcher"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="APPROVE"))])

    async def invoke():
        with patch("agent.auxiliary_client.call_llm", side_effect=guardian):
            return _smart_approve("python -c 'print(1)'", "script execution")

    assert asyncio.run(invoke()) == "approve"


def test_guardian_failure_is_not_a_denial_or_manual_wait(monkeypatch):
    monkeypatch.setattr(approval, "_smart_verdict", lambda *_: "error")
    args = (approval._COMMAND_GATE, "python -c 'print(1)'", "script execution",
            "script execution", ["script execution"])
    result, denied = approval._smart_gate(*args, "worker", human_present=False)
    assert result["status"] == "approval_error"
    assert result["approved"] is False
    assert result["approval_error"] is True
    assert denied is False
    manual, denied = approval._smart_gate(*args, "owner", human_present=True)
    assert manual["status"] == "approval_error"
    assert denied is False


def test_public_gateway_command_guard_reports_engine_fault_without_wait(monkeypatch):
    monkeypatch.setattr(approval, "_presence", lambda _callback=None: (None, False, True, False))
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval, "_smart_verdict", lambda *_: "error")
    monkeypatch.setattr(approval, "_floor_block", lambda *_a, **_k: None)
    monkeypatch.setattr(approval, "_tirith_scan", lambda *_: {"action": "allow"})
    monkeypatch.setattr(approval, "detect_dangerous_command", lambda *_: (True, "test", "test"))
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda *_: False)
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    monkeypatch.setattr(approval, "_await_gateway_decision",
                        lambda *_a, **_k: pytest.fail("engine fault must not wait for a human"))
    result = approval.check_all_command_guards("test command", "local")
    assert result["status"] == "approval_error"
    assert result["approval_error"] is True


def test_public_execute_code_guard_reports_engine_fault_without_wait(monkeypatch):
    monkeypatch.setattr(approval, "_presence", lambda _callback=None: (None, False, True, False))
    monkeypatch.setattr(approval, "_unattended_contexts", lambda: [])
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval, "_smart_verdict", lambda *_: "error")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    monkeypatch.setattr(approval, "_await_gateway_decision",
                        lambda *_a, **_k: pytest.fail("engine fault must not wait for a human"))
    result = approval.check_execute_code_guard("print(1)", "local")
    assert result["status"] == "approval_error"
    assert result["approval_error"] is True


def test_terminal_reports_approval_engine_fault(monkeypatch):
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *_a, **_k: {
        "approved": False, "status": "approval_error", "approval_error": True,
        "message": "Smart approval engine failed before a decision.",
    })
    with pytest.raises(terminal_tool._Rejected) as rejected:
        terminal_tool._run_approval_guards("python -c 'print(1)'", "local", {}, force=False)
    result = json.loads(rejected.value.result_json)
    assert result["status"] == "approval_error"
    assert result["approval_error"] is True


def test_execute_code_reports_approval_engine_fault(monkeypatch):
    monkeypatch.setattr(code_execution_tool, "SANDBOX_AVAILABLE", True)
    monkeypatch.setattr(code_execution_tool, "_load_config", lambda: {"timeout": 30})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local"})
    monkeypatch.setattr(terminal_tool, "_docker_has_host_access", lambda *_: False)
    monkeypatch.setattr(approval, "check_execute_code_guard", lambda *_a, **_k: {
        "approved": False, "status": "approval_error", "approval_error": True,
        "message": "Smart approval engine failed before a decision.",
    })
    with patch("tools.terminal_scope.enforce_no_refusal"), \
         patch("agent.deadline.run_bounded_sync", return_value=SimpleNamespace(timed_out=False, value=False)):
        result = json.loads(code_execution_tool.execute_code("print(1)"))
    assert result["status"] == "approval_error"
    assert result["approval_error"] is True
