"""Tests for MoA aggregator-slot exposure used by session cost accounting.

Regression guard for the ~50% MoA cost undercount: on the MoA path the
agent's model/provider are the virtual preset name (e.g. "closed") and "moa",
which have no pricing entry. Session cost accounting must price the
aggregator's acting turn at its REAL model/provider, read from the MoA
client's ``last_aggregator_slot``. Before the fix that slot did not exist and
the aggregator's spend (often >50% of the turn) was silently dropped, leaving
the session cost as advisor-fan-out only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _response(content="ok"):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="fake")


@pytest.fixture
def moa_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: closed
  presets:
    closed:
      enabled: true
      reference_models:
        - provider: openrouter
          model: anthropic/claude-opus-4.8
        - provider: openrouter
          model: openai/gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_create_populates_last_aggregator_slot(moa_config, monkeypatch):
    """After a create() turn, last_aggregator_slot carries the REAL aggregator
    model/provider — not the virtual preset name."""
    from agent.moa_loop import MoAChatCompletions

    def fake_call_llm(**kwargs):
        return _response("acted" if kwargs.get("task") != "moa_reference" else "advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    facade = MoAChatCompletions("closed")
    # Slot is unset before any turn runs.
    assert facade.last_aggregator_slot is None

    facade.create(
        model="closed",
        messages=[{"role": "user", "content": "clean the db"}],
    )

    slot = facade.last_aggregator_slot
    assert slot is not None
    # The virtual preset name / "moa" must NOT leak into the priced slot.
    assert slot["model"] == "anthropic/claude-opus-4.8"
    assert slot["provider"] == "openrouter"
    assert slot["model"] != "closed"


def test_last_reference_slots_expose_the_resolved_adviser_routes(moa_config, monkeypatch):
    """The recorded route receipt reads ``last_reference_slots``: the advisEr routes that
    ACTUALLY served the fan-out, taken from the same metrics the observability hooks see."""
    from agent.moa_loop import MoAChatCompletions, MoAClient

    def fake_call_llm(**kwargs):
        return _response("acted" if kwargs.get("task") != "moa_reference" else "advice")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    facade = MoAChatCompletions("closed")
    assert facade.last_reference_slots == []
    facade.create(model="closed", messages=[{"role": "user", "content": "clean the db"}])

    assert facade.last_reference_slots == [
        {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        {"provider": "openrouter", "model": "openai/gpt-5.5"},
    ]
    # The OpenAI-client-shaped wrapper delegates it too (that is what turn_usage reads).
    client = MoAClient("closed")
    assert client.last_reference_slots == []
    client.chat.completions._last_reference_metrics = [{"provider": "xai", "model": "grok-4.6"}, {"model": "no-provider"}]
    assert client.last_reference_slots == [{"provider": "xai", "model": "grok-4.6"}]
