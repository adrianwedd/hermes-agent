"""A Codex chat shim must reach its Responses converter through managed Relay."""
from types import SimpleNamespace

from agent import auxiliary_client as aux, relay_llm, relay_runtime
from nemo_relay import codecs
import nemo_relay as relay


CHAT = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Offline fixture"}]}
META = {"api_mode": "codex_responses", "auxiliary_task": "moa_reference"}


def test_codex_shim_managed_relay_preserves_real_wire_conversion(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins.toml"
    plugins.write_text("")
    monkeypatch.setenv("HERMES_NEMO_RELAY_PLUGINS_TOML", str(plugins))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id="offline-codec", platform="cli")
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="offline-turn", task_id="offline-task")
    lease.host.retain_managed_execution("offline-test")
    client = aux.CodexAuxiliaryClient(SimpleNamespace(api_key="offline", base_url="https://chatgpt.com/backend-api/codex/"), "gpt-6.1-sol")
    calls = []

    def provider(request):
        wire, _, _ = client.chat.completions._build_responses_kwargs(request)
        codecs.OpenAIResponsesCodec().decode(relay.LLMRequest({}, wire))
        calls.append(wire)
        return {"id": "offline", "model": "gpt-6.1-sol", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "offline answer"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(aux, "_relay_auxiliary_metadata", lambda **kw: ("openai-codex", "gpt-6.1-sol", META))
    try:
        result = aux._relay_sync_completion(client, CHAT, provider="openai-codex", api_mode="codex_responses", create=provider)
        assert len(calls) == 1
        assert calls[0]["input"]
        assert "messages" not in calls[0]
        assert result["choices"][0]["message"]["content"] == "offline answer"
    finally:
        lease.host.release_managed_execution("offline-test")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def test_only_shim_boundary_changes_codec_metadata():
    client = aux.CodexAuxiliaryClient(SimpleNamespace(api_key="offline", base_url="https://chatgpt.com/backend-api/codex/"), "gpt-6.1-sol")
    fixed = aux._relay_completion_metadata(client, META)
    assert fixed["api_mode"] == "chat_completions"
    assert fixed["provider_api_mode"] == "codex_responses"
    assert META["api_mode"] == "codex_responses"
    assert aux._relay_completion_metadata(object(), META) is META
    codecs.OpenAIChatCodec().decode(relay.LLMRequest({}, relay_llm._relay_request_body(CHAT, fixed)))
