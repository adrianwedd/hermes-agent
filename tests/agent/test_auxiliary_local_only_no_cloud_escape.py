"""Local-only auxiliary routing must never escape to an inherited cloud account.

Adversarial offline suite for the local-means-local boundary. Every provider resolver is
stubbed — nothing here can reach the network, and any cloud candidate *probed* (not just
*selected*) is recorded and fails the test.

Auxiliary path inventory exercised here (path -> policy -> test):

1. ``_resolve_auto_route`` (text aux: compression/summary/memory flush)
     policy: discovery chain only with no selected main provider (pre-existing gate).
2. ``_try_payment_fallback`` (post-402/conn-error aux fallback)
     policy: same gate (pre-existing).
3. ``_vision_auto_route`` (vision auto-detect)
     policy: NEW — same gate; a selected local main that cannot answer vision fails
     closed instead of traversing ``_VISION_AUTO_PROVIDER_ORDER``.
4. ``resolve_vision_provider_client(provider="auto")`` (public entry for #3).
5. ``get_available_vision_backends`` (setup/tool-gating enumeration)
     policy: NEW — same gate; enumerating "whatever is logged in" is discovery too.
6. ``resolve_vision_provider_client(provider="openrouter")`` etc. (explicit opt-in)
     policy: preserved — an explicitly named provider is the user's selection, never
     arbitrary discovery; must still win behind a local main.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as aux

# Credentials that exist in the environment from an earlier setup (the incident's shape:
# inherited cloud creds present, session deliberately pointed at a local model).
INHERITED = {
    "openrouter": MagicMock(name="openrouter-client"),
    "nous": MagicMock(name="nous-client"),
    "deepinfra": MagicMock(name="deepinfra-client"),
}


def _cloud_spy():
    """Spy over strict vision backend resolution; returns (mock, probed_providers)."""
    probed: list[str] = []

    def fake_backend(provider: str, model=None):
        probed.append(provider)
        client = INHERITED.get(provider)
        return (client, f"{provider}-vision") if client is not None else (None, None)

    return patch.object(aux, "_resolve_strict_vision_backend", side_effect=fake_backend), probed


LOCAL_MAIN = {"provider": "custom", "model": "qwen3-local", "base_url": "http://127.0.0.1:11434/v1"}


def _local_main_cannot_vision():
    """Main provider selected, its vision client unavailable (model is text-only)."""
    return patch.object(aux, "_vision_main_provider_client", return_value=(None, None))


# ── 3/4. vision auto-route: local main + inherited cloud creds ──────────────────

def test_local_main_vision_unavailable_does_not_probe_cloud_order():
    """Regression: previously _vision_auto_route traversed _VISION_AUTO_PROVIDER_ORDER
    unconditionally, so a qwen-local session with stale OpenRouter creds silently billed
    OpenRouter for every image."""
    spy, probed = _cloud_spy()
    with _local_main_cannot_vision(), spy, \
         patch.object(aux, "_read_main_provider", return_value="custom"):
        provider, client, model = aux._vision_auto_route(
            dict(LOCAL_MAIN), resolved_model=None, resolved_api_mode=None, async_mode=False)
    assert (provider, client, model) == (None, None, None)
    assert probed == [], f"cloud vision backends probed behind a selected main: {probed}"


def test_public_vision_auto_entry_fails_closed_for_local_main():
    spy, probed = _cloud_spy()
    with _local_main_cannot_vision(), spy, \
         patch.object(aux, "_read_main_provider", return_value="custom"), \
         patch.object(aux, "_resolve_task_provider_model",
                      return_value=("auto", None, "", None, None)):
        provider, client, model = aux.resolve_vision_provider_client(main_runtime=dict(LOCAL_MAIN))
    assert (provider, client, model) == (None, None, None)
    assert probed == []


def test_session_runtime_selection_overrides_persisted_auto():
    """A live local session over a persisted ``provider: auto`` is still a selection —
    the disk value must not re-open discovery (mirrors the text-route contract)."""
    spy, probed = _cloud_spy()
    with _local_main_cannot_vision(), spy, \
         patch.object(aux, "_read_main_provider", return_value="auto"):
        provider, client, model = aux._vision_auto_route(
            dict(LOCAL_MAIN), resolved_model=None, resolved_api_mode=None, async_mode=False)
    assert (provider, client, model) == (None, None, None)
    assert probed == []


# ── primary provider failure (selected provider that IS in the vision order) ────

def test_selected_cloud_main_failing_does_not_hop_accounts():
    """openrouter selected as main and failing must not fall through to nous/deepinfra —
    'my main failed' is not consent to bill a different account."""
    spy, probed = _cloud_spy()
    with _local_main_cannot_vision(), spy, \
         patch.object(aux, "_read_main_provider", return_value="openrouter"):
        provider, client, model = aux._vision_auto_route(
            {"provider": "openrouter", "model": "some-model"},
            resolved_model=None, resolved_api_mode=None, async_mode=False)
    assert (provider, client, model) == (None, None, None)
    assert probed == []


# ── no selection: the convenience discovery stays available ─────────────────────

def test_no_selected_main_provider_still_discovers_vision():
    spy, probed = _cloud_spy()
    with patch.object(aux, "_vision_main_provider_client", return_value=(None, None)), spy, \
         patch.object(aux, "_read_main_provider", return_value="auto"):
        provider, client, model = aux._vision_auto_route(
            {"provider": "auto"}, resolved_model=None, resolved_api_mode=None, async_mode=False)
    assert provider == "openrouter" and client is INHERITED["openrouter"]
    assert probed[0] == "openrouter"


# ── 5. enumeration (tool gating / setup summary) ────────────────────────────────

def test_available_backends_behind_local_main_lists_only_main():
    """Gate #5: with a selected main that has no vision, enumeration must not list cloud
    accounts as available backends (that is how tools get enabled against inherited creds)."""
    spy, probed = _cloud_spy()
    with patch.object(aux, "_read_main_provider", return_value="custom"), \
         patch.object(aux, "_read_main_model", return_value="qwen3-local"), \
         patch.object(aux, "resolve_provider_client", return_value=(MagicMock(), "qwen3-local")), spy:
        available = aux.get_available_vision_backends()
    assert available == ["custom"]
    assert probed == []


def test_available_backends_with_no_selection_discovers():
    spy, probed = _cloud_spy()
    with patch.object(aux, "_read_main_provider", return_value="auto"), spy:
        available = aux.get_available_vision_backends()
    assert "openrouter" in available and "nous" in available


# ── 6. explicit opt-in preserved ────────────────────────────────────────────────

def test_explicit_vision_provider_opt_in_still_routes():
    """Explicitly configured ``auxiliary.vision.provider`` is a selection, not discovery:
    behind a local main it must still resolve (the approved explicit cloud lane)."""
    spy, probed = _cloud_spy()
    with spy, patch.object(aux, "_read_main_provider", return_value="custom"), \
         patch.object(aux, "_resolve_task_provider_model",
                      return_value=("openrouter", None, "", None, None)):
        provider, client, model = aux.resolve_vision_provider_client(main_runtime=dict(LOCAL_MAIN))
    assert provider == "openrouter" and client is INHERITED["openrouter"]
    assert probed == ["openrouter"]


def test_direct_base_url_override_still_routes():
    """A user-declared vision base_url beats the gate — explicit endpoint, explicit consent."""
    client = MagicMock(name="direct-client")
    with patch.object(aux, "_resolve_task_provider_model",
                      return_value=("custom", "vision-model", "http://127.0.0.1:11434/v1", None, None)), \
         patch.object(aux, "resolve_provider_client", return_value=(client, "vision-model")):
        provider, got, model = aux.resolve_vision_provider_client(main_runtime=dict(LOCAL_MAIN))
    assert got is client and provider == "custom"


# ── 1/2. text-route gate regression (pre-existing gate stays wired) ─────────────

def test_text_route_still_refuses_to_guess_with_inherited_creds():
    runtime = {"provider": "xai-oauth", "model": "grok-4.6", "base_url": "https://api.x.ai/v1", "api_key": "dead"}
    with patch.object(aux, "_try_main_provider_route", return_value=None), \
         patch.object(aux, "_try_configured_fallback_chain", return_value=(None, None, "")), \
         patch.object(aux, "_try_main_fallback_chain", return_value=(None, None, "")), \
         patch.object(aux, "_try_openrouter", return_value=(None, None)), \
         patch.object(aux, "_try_nous", return_value=(MagicMock(name="nous"), "nous-model")), \
         patch.object(aux, "_read_main_provider", return_value="auto"):
        assert aux._resolve_auto_route(main_runtime=runtime, task="compression") == (None, None, "")
        assert aux._try_payment_fallback("xai-oauth", task="compression", main_runtime=runtime) == (None, None, "")


# ── full call path: local-only failure must surface locally, never hop to a cloud account ──
#
# The gates above prove *resolution* never reaches a cloud account. These close the other
# half of the required matrix: when the selected local route answers but is broken, the
# call-level contract must deliver a visible local failure — not a silent cloud substitution.
#
# The route must resolve to ``auto`` for the recovery ladder to be *able* to reach the
# discovery chain, while the SESSION's main provider stays local. That is the local-only
# shape: task routing is unset (auto) but the session is deliberately pointed at a local
# model, and discovery is exactly what must stay shut.

_LOCAL_ROUTE = {"provider": "custom", "model": "qwen3-local", "base_url": "http://127.0.0.1:11434/v1"}


def _fail_closed_at_call(task, error, *, open_gate=False):
    """One auto-routed aux call whose local route throws ``error``.

    ``_relay_sync_completion`` raises inside the call loop, so the real recovery ladder runs
    with every cloud candidate spied and forced empty — no transport is touched.
    Returns ``(pytest.ExceptionInfo, probed_cloud_candidates)``.
    """
    local_client = MagicMock(name="local-client")
    probed: list = []

    def _spy(label):
        def _fn(*a, **k):
            probed.append(label)
            return (None, None)
        return _fn

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(
            aux, "_resolve_task_provider_model", return_value=("auto", "qwen3-local", "", None, None)))
        stack.enter_context(patch.object(
            aux, "resolve_provider_client", return_value=(local_client, "qwen3-local")))
        for _label in ("openrouter", "nous", "local/custom", "api-key"):
            _fn = {"openrouter": "_try_openrouter", "nous": "_try_nous",
                   "local/custom": "_try_custom_endpoint", "api-key": "_resolve_api_key_provider"}[_label]
            stack.enter_context(patch.object(aux, _fn, side_effect=_spy(_label)))
        stack.enter_context(patch.object(aux, "_try_configured_fallback_chain", return_value=(None, None, "")))
        stack.enter_context(patch.object(aux, "_try_main_fallback_chain", return_value=(None, None, "")))
        stack.enter_context(patch.object(aux, "_read_main_provider", return_value="custom"))
        stack.enter_context(patch.object(aux, "_transient_retry_count", return_value=0))
        stack.enter_context(patch.object(aux, "_relay_sync_completion", side_effect=error))
        if open_gate:  # falsification only — proves the spies are reachable
            stack.enter_context(patch.object(aux, "_discovery_chain_allowed", return_value=True))
        with pytest.raises(RuntimeError) as exc:
            aux.call_llm(task=task, main_runtime=dict(_LOCAL_ROUTE),
                         messages=[{"role": "user", "content": "hi"}])
    return exc, probed


def test_malformed_local_response_fails_visible_and_stays_local():
    """A malformed local reply (HTTP 200, no ``choices[0].message``) is a local capability
    failure: it must raise the local error, and no cloud provider may even be probed."""
    error = RuntimeError(
        "Auxiliary compression: LLM returned invalid response (type=dict): "
        "'{}'. Expected object with .choices[0].message — check provider adapter or custom "
        "endpoint compatibility."
    )
    exc, probed = _fail_closed_at_call("compression", error)
    assert "invalid response" in str(exc.value)
    assert probed == [], f"cloud candidates probed after a malformed local reply: {probed}"


def test_local_aux_timeout_fails_visible_and_stays_local():
    """A timeout from the local auxiliary must fail closed visibly — not bill an inherited
    cloud account for the same summarisation."""
    error = RuntimeError("Auxiliary compression: request to http://127.0.0.1:11434/v1 timed out")
    exc, probed = _fail_closed_at_call("compression", error)
    assert "timed out" in str(exc.value)
    assert probed == [], f"cloud candidates probed after a local timeout: {probed}"


def test_call_level_spies_are_reachable_when_the_gate_is_open():
    """Anti-placebo guard for the two tests above: with discovery forced open the ladder DOES
    probe cloud candidates, so their ``probed == []`` assertion is a real signal."""
    error = RuntimeError("Auxiliary compression: request to http://127.0.0.1:11434/v1 timed out")
    _, probed = _fail_closed_at_call("compression", error, open_gate=True)
    assert probed, "gate open but nothing probed — the call-level tests above are placebo"
