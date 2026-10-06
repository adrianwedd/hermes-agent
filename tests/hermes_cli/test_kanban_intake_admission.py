"""Triage calls consume the worker budget before any model dispatch."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_provider_admission as admission
from hermes_cli import kanban_specify as specify


@pytest.fixture
def intake(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    route = {"provider": "openai-codex", "model": "gpt-6.1-sol", "fallback_chain": []}
    config = {"kanban": {"provider_admission": {"local": 0, "cloud": 2}},
              "auxiliary": {"kanban_decomposer": route}}
    (tmp_path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="Intake fixture", workspace_kind="scratch")
    return tmp_path, config


def call():
    return specify._call_aux("decompose", "fixture", aux_task="kanban_decomposer",
                             system="Fixture", user="Fixture", max_tokens=100, timeout=1)


@pytest.mark.parametrize("counts", [{"local": 0, "cloud": 2}, {"local": 2, "cloud": 0}])
def test_full_budget_never_calls_model(monkeypatch, intake, counts):
    from agent import auxiliary_client as aux
    monkeypatch.setattr(admission, "count_lanes", lambda paths: counts)
    monkeypatch.setattr(aux, "call_llm", lambda **kwargs: pytest.fail("Unadmitted inference"))
    text, reason = call()
    assert text is None and "capacity" in reason


def test_admitted_call_holds_cross_board_lock_and_releases(monkeypatch, intake):
    from agent import auxiliary_client as aux
    from agent.auxiliary_route_fence import fallback_allowed
    home, _ = intake
    monkeypatch.setattr(admission, "count_lanes", lambda paths: {"local": 0, "cloud": 1})

    def model(**kwargs):
        with admission.host_lock(home) as acquired:
            assert not acquired
        assert not fallback_allowed()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="fixture"))])

    monkeypatch.setattr(aux, "call_llm", model)
    assert call() == ("fixture", "")
    assert fallback_allowed()
    with admission.host_lock(home) as acquired:
        assert acquired


@pytest.mark.parametrize("route", [{"provider": "moa", "model": "cloud"},
                                  {"provider": "ollama-cloud", "model": "fixture", "base_url": "http://localhost:11434"},
                                  {"provider": "openai-codex", "model": "fixture", "fallback_chain": [{"provider": "other"}]}])
def test_unqualified_route_never_calls_model(monkeypatch, intake, route):
    from agent import auxiliary_client as aux
    home, config = intake
    config["auxiliary"]["kanban_decomposer"] = route
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(aux, "call_llm", lambda **kwargs: pytest.fail("Unqualified inference"))
    text, reason = call()
    assert text is None and "explicit" in reason


def test_error_releases_admission_and_route_fence(monkeypatch, intake):
    from agent import auxiliary_client as aux
    from agent.auxiliary_route_fence import fallback_allowed
    home, _ = intake
    monkeypatch.setattr(admission, "count_lanes", lambda paths: {"local": 0, "cloud": 0})

    def failed(**kwargs):
        raise RuntimeError("Offline fixture failure")

    monkeypatch.setattr(aux, "call_llm", failed)
    assert call()[0] is None
    assert fallback_allowed()
    with admission.host_lock(home) as acquired:
        assert acquired
