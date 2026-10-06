"""Admitted intake cannot silently switch providers during resolution/recovery."""
from types import SimpleNamespace

import pytest

from agent.auxiliary_route_fence import admitted_route, validate_call


def test_intake_rejects_resolved_route_drift():
    route = {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    with admitted_route(route):
        with pytest.raises(RuntimeError, match="outside"):
            validate_call(SimpleNamespace(base_url="https://ollama.com/v1"), {"model": "cloud"}, "moa")
        validate_call(SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex"),
                      {"model": "gpt-6.1-sol"}, "openai-codex")


def test_intake_capacity_failure_never_enters_provider_fallback(monkeypatch):
    from agent import auxiliary_client as aux
    monkeypatch.setattr(aux, "_try_configured_fallback_chain", lambda *a, **k: pytest.fail("Provider hop"))
    with admitted_route({"provider": "openai-codex", "model": "gpt-6.1-sol"}):
        assert list(aux._ladder_provider_fallback(RuntimeError("quota exhausted"), SimpleNamespace())) == []


def test_actual_primary_and_rebuilt_retry_endpoint_are_fenced(monkeypatch):
    from agent import auxiliary_client as aux
    local_client = SimpleNamespace(base_url="http://localhost:11434")
    monkeypatch.setenv("HERMES_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex")
    monkeypatch.setattr(aux, "_prepare_same_provider_retry", lambda **kwargs: (local_client, {"model": "gpt-6.1-sol"}))
    with admitted_route({"provider": "openai-codex", "model": "gpt-6.1-sol"}):
        with pytest.raises(RuntimeError, match="outside"):
            aux._relay_sync_completion(local_client, {"model": "gpt-6.1-sol"}, provider="openai-codex")
        with pytest.raises(RuntimeError, match="outside"):
            aux._retry_same_provider_sync(resolved_provider="openai-codex", resolved_api_mode="codex_responses", task="kanban_decomposer")
