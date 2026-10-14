"""An approval-engine fault must survive compression as a not-run action."""

import json

import pytest

from agent.context_compressor import _summarize_tool_result


@pytest.mark.parametrize("tool_name,args", [
    ("terminal", {"command": "probe"}),
    ("execute_code", {"code": "print('probe')"}),
])
def test_approval_error_is_not_summarized_as_execution(tool_name, args):
    content = json.dumps({"status": "approval_error", "error": "guardian exception", "approval_error": True})
    summary = _summarize_tool_result(tool_name, json.dumps(args), content)
    assert "approval engine failed, not run" in summary
    assert "ran `" not in summary
