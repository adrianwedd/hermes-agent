"""Smart-approval transport failures remain distinct from decisions."""

import asyncio
import contextvars
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import approval, terminal_tool
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
    assert manual is None
    assert denied is False


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
