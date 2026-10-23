"""Cloud provider overrides must not consume the disabled local lane."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_provider_admission as admission
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def cloud_config(monkeypatch):
    monkeypatch.setattr("agent.secret_scope.get_secret", lambda name: None)
    return {
        "model": {"provider": "ollama-cloud", "default": "cloud-model",
                  "base_url": "https://ollama.com/v1"},
        "auxiliary": {name: {"provider": "ollama-cloud", "model": "cloud-helper"}
                      for name in admission.AUX_TASKS},
        "kanban": {"provider_admission": {"local": 0, "cloud": 1}},
    }


@pytest.mark.parametrize("endpoint,expected", [
    (None, "cloud"),
    ("https://chatgpt.com/backend-api/codex", "cloud"),
    ("http://127.0.0.1:8080/v1", "local"),
    ("https://unqualified.example/v1", "local"),
])
def test_codex_override_uses_codex_endpoint_not_previous_provider(cloud_config, monkeypatch, endpoint, expected):
    cloud_config["model"]["base_url"] = "http://127.0.0.1:11434/v1"
    monkeypatch.setattr("agent.secret_scope.get_secret", lambda name: endpoint if name == "HERMES_CODEX_BASE_URL" else None)
    assert admission.classify(cloud_config, "openai-codex", "gpt-6.1-sol") == expected


@pytest.mark.parametrize("changes,provider,model", [
    ({"auxiliary": {"compression": {"provider": "local", "model": "helper"}}}, "openai-codex", "gpt-6.1-sol"),
    ({"fallback_models": ["local-helper"]}, "openai-codex", "gpt-6.1-sol"),
    ({"providers": {"openai-codex": {"base_url": "http://127.0.0.1:8080"}}}, "openai-codex", "gpt-6.1-sol"),
    ({}, "unqualified-provider", "gpt-6.1-sol"),
    ({}, "openai-codex", None),
    ({}, "openai-codex", "moa:local-preset"),
])
def test_override_does_not_waive_unqualified_routes(cloud_config, changes, provider, model):
    cloud_config.update(changes)
    assert admission.classify(cloud_config, provider, model) == "local"


def test_native_claim_admits_codex_override_and_keeps_cloud_cap(cloud_config, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DB"]:
        monkeypatch.delenv(name, raising=False)
    # Use the real profile route and policy readers against disposable YAML.
    import hermes_yaml
    (home / "config.yaml").write_text(hermes_yaml.safe_dump(cloud_config))
    with connect(home / "kanban.db") as conn:
        ids = [kb.create_task(conn, title="Bounded cloud work", assignee="default",
                              workspace_kind="scratch",
                              provider_override="openai-codex", model_override="gpt-6.1-sol")
               for _ in range(2)]
        kb.recompute_ready(conn)
        first = kb.claim_task(conn, ids[0], claimer="fixture:owner")
        assert first is not None
        receipt = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='provider_admission'", (ids[0],)).fetchone()
        import json
        assert json.loads(receipt[0])["lane"] == "cloud"
        assert kb.claim_task(conn, ids[1], claimer="fixture:owner") is None
        assert admission.claim_refusal_reason(clear=True) == "provider_admission_capacity_exhausted"
        assert admission.claim_refusal_reason() is None
        from hermes_cli.kanban_dispatch_evidence import claim_for_dispatch
        claimed, refusal = claim_for_dispatch(kb.claim_task, conn, ids[1], claimer="fixture:owner")
        assert claimed is None
        assert refusal == "provider_admission_capacity_exhausted"
        assert kb.get_task(conn, ids[1]).status == "ready"


@pytest.mark.parametrize("provider,expected", [("ollama-cloud", "cloud"), ("openai-codex", "cloud"), ("local", "local"), ("auto", "local")])
def test_auto_auxiliary_inherits_only_concrete_remote_primary(cloud_config, provider, expected):
    cloud_config["model"] = {"provider": provider, "default": "main-model"}
    cloud_config["auxiliary"] = {name: {"provider": "auto", "model": ""} for name in admission.AUX_TASKS}
    assert admission.classify(cloud_config) == expected


@pytest.mark.parametrize("route", [
    {"provider": "auto", "fallback_chain": ["local"]},
    {"provider": "auto", "base_url": "http://127.0.0.1:8080/v1"},
    {"provider": "local", "model": "helper"},
])
def test_auto_auxiliary_keeps_route_guards(cloud_config, route):
    cloud_config["auxiliary"]["compression"] = route
    assert admission.classify(cloud_config) == "local"


def test_auto_intake_uses_concrete_route_fence(cloud_config, tmp_path, monkeypatch):
    from hermes_cli.kanban_intake_admission import intake_permit
    from agent.auxiliary_route_fence import validate_call, fallback_allowed
    from types import SimpleNamespace
    import hermes_yaml
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    cloud_config["auxiliary"]["triage_specifier"] = {"provider": "auto", "model": ""}
    (home / "config.yaml").write_text(hermes_yaml.safe_dump(cloud_config))
    with intake_permit("triage_specifier") as refusal:
        assert refusal == ""
        assert not fallback_allowed()
        validate_call(SimpleNamespace(base_url="https://ollama.com/v1"), {"model": "cloud-model"}, "ollama-cloud")
        with pytest.raises(RuntimeError):
            validate_call(SimpleNamespace(base_url="http://localhost:8080"), {"model": "cloud-model"}, "ollama-cloud")
    assert fallback_allowed()


def test_auto_vision_does_not_discover_after_selected_provider_fails(monkeypatch):
    from agent import auxiliary_client as aux
    monkeypatch.setattr(aux, "_vision_main_provider_client", lambda *args: (None, None))
    def unexpected_discovery(*args):
        pytest.fail("selected provider must not escape into credential discovery")
    monkeypatch.setattr(aux, "_resolve_strict_vision_backend", unexpected_discovery)
    assert aux._vision_auto_route({"provider": "ollama-cloud", "model": "main"}, None, None, False) == (None, None, None)
