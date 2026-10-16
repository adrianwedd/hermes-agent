"""Speech hooks must defer to a native consumer without changing response text."""
from types import SimpleNamespace

import pytest

from agent import turn_finalizer
from cli import HermesCLI, _ChatTurn


@pytest.mark.parametrize("owner", [None, "native"])
def test_post_llm_hook_receives_the_turns_playback_owner(monkeypatch, owner):
    calls = []
    agent = SimpleNamespace(session_id="session", model="unchanged-model", _tts_playback_owner=owner)
    monkeypatch.setattr(turn_finalizer, "apply_llm_output_transform", lambda *a, **k: (a[1], False, None))
    monkeypatch.setattr(turn_finalizer, "_invoke_hook_safely", lambda name, logger, **data: calls.append((name, data)))
    result = turn_finalizer._apply_output_hooks(
        agent, "Exact response.", None, platform="cli", effective_task_id=None,
        turn_id="turn", original_user_message="Hello", messages=[])
    assert result == ("Exact response.", False, None)
    assert calls[0][0] == "post_llm_call"
    assert calls[0][1]["tts_playback_owner"] == owner
    assert calls[0][1]["assistant_response"] == "Exact response."


@pytest.mark.parametrize("fails", [False, True])
def test_owner_is_agent_local_and_restored_on_exit(fails):
    from agent.tts_playback import tts_playback_owner

    parent, child = SimpleNamespace(), SimpleNamespace()
    try:
        with tts_playback_owner(parent, "native"):
            assert parent._tts_playback_owner == "native"
            assert not hasattr(child, "_tts_playback_owner")
            with tts_playback_owner(parent, None):
                assert parent._tts_playback_owner is None
            assert parent._tts_playback_owner == "native"
            if fails:
                raise RuntimeError("failed turn")
    except RuntimeError:
        assert fails
    assert not hasattr(parent, "_tts_playback_owner")


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_marks_only_native_tts_turns(enabled):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "ownership-test"
    cli.conversation_history = [{"role": "user", "content": "Exact request."}]
    cli._voice_tts = enabled
    cli._flush_credit_notices = lambda: None
    observed = []

    def run_conversation(**kwargs):
        observed.append((cli.agent._tts_playback_owner, kwargs["user_message"]))
        return {"final_response": "Exact response."}

    cli.agent = SimpleNamespace(run_conversation=run_conversation)
    turn = _ChatTurn()
    cli._chat_run_agent(turn, "Exact request.")
    assert observed == [("native" if enabled else None, "Exact request.")]
    assert turn.result == {"final_response": "Exact response."}
    assert not hasattr(cli.agent, "_tts_playback_owner")
