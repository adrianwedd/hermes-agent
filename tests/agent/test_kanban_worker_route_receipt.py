"""The Kanban worker's turn loop must hand its ACTUAL route to the board's receipt writer.

``agent/turn_usage.record_response_usage`` is the one place a worker turn knows both its own
route (``agent.provider``/``agent.model``) and the resolved MoA slots (the client's
``last_aggregator_slot`` / ``last_reference_slots``). Without this wiring the writer exists but
is never called, and every MoA worker run keeps rendering ``aggregator: unknown``
(regression for t_e07ae2d6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _RecorderReached(BaseException):
    """Raised by the patched recorder to stop ``record_response_usage`` at the call site.

    Deliberately NOT an ``Exception``: the receipt call is best-effort by design and catches
    ``Exception``, so an Exception-shaped sentinel would be swallowed exactly as production
    swallows a real receipt failure — and the test would drift past its own call site.
    """


class _PassedCallSite(BaseException):
    """Raised by the compressor: the first thing touched AFTER the receipt call site."""


def _agent(compressor=None):
    """Just enough agent for ``record_response_usage`` to reach the receipt call site."""
    return SimpleNamespace(
        context_compressor=compressor or SimpleNamespace(
            awaiting_real_usage_after_compression=False, threshold_tokens=0),
        session_api_calls=0,
        provider="moa",
        model="cloud",
        base_url="https://example.invalid/v1",
        api_key="k",
        api_mode="chat_completions",
        session_id="20261006_000000_aaaa",
        client=SimpleNamespace(
            last_aggregator_slot={"provider": "openai-codex", "model": "gpt-6.1-sol"},
            last_reference_slots=[{"provider": "xai", "model": "grok-4.6"}],
        ),
        _current_streamed_assistant_text="",
    )


def _response():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12,
                            prompt_tokens_details=None, completion_tokens_details=None)
    return SimpleNamespace(usage=usage, model="cloud", provider="moa")


def _record(agent):
    from agent import turn_usage
    return turn_usage.record_response_usage(
        agent, _response(), messages=[{"role": "user", "content": "hi"}], api_call_count=1,
        api_duration=0.1, compression_attempts=0, max_compression_attempts=3,
    )


def test_a_kanban_worker_turn_hands_its_route_to_the_receipt_writer(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    seen = []

    import hermes_cli.kanban_worker_observability as obs
    from agent import turn_usage

    assert turn_usage._is_kanban_worker(_agent()) is True

    def recorder(agent, moa_client=None):
        seen.append((agent, moa_client))
        raise _RecorderReached

    monkeypatch.setattr(obs, "record_worker_route", recorder)

    with pytest.raises(_RecorderReached):
        _record(_agent())

    assert len(seen) == 1
    agent, client = seen[0]
    # The client is handed over so the receipt can carry the RESOLVED aggregator/advisers,
    # not just the virtual "moa / <preset>" the agent itself reports.
    assert client.last_aggregator_slot == {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    assert client.last_reference_slots == [{"provider": "xai", "model": "grok-4.6"}]
    assert agent.provider == "moa"


@pytest.mark.parametrize("task_env", [None, "   "])
def test_a_non_worker_turn_is_not_a_worker_and_never_touches_the_board(monkeypatch, task_env):
    """A CLI or gateway turn has no board run: the receipt must stay out of its path.

    A whitespace-only task id is not a worker here either, exactly as in the exit-code and
    turn-recovery gates (``agent/kanban_turn_recovery.kanban_task_id`` strips once).
    """
    if task_env is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_env)
    called = []

    import hermes_cli.kanban_worker_observability as obs
    from agent import turn_usage

    monkeypatch.setattr(obs, "record_worker_route", lambda *a, **k: called.append(a))
    assert turn_usage._is_kanban_worker(_agent()) is False

    # The compressor is the first thing touched AFTER the receipt call site. Raising there
    # (a BaseException, so it is not swallowed) proves the turn reached the code after the
    # guarded receipt WITHOUT the recorder having been called.
    compressor = SimpleNamespace(
        update_from_response=lambda *a, **k: (_ for _ in ()).throw(_PassedCallSite))
    with pytest.raises(_PassedCallSite):
        _record(_agent(compressor=compressor))
    assert called == []
