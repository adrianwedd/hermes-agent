"""Four failure classes that used to look identical to the caller, kept distinguishable.

A healthy loopback gateway whose event loop is blocked (a synchronous hook, say) cannot answer a
WebSocket keepalive ping, so ``websockets`` kills the connection with
``sent 1011 (internal error) keepalive ping timeout``. The caller then saw a raw library exception
instead of a transport verdict -- and cosmetic status polling turned it into an uncaught
background-thread traceback that read as an agent failure.

These tests pin the four classes apart:

* **event-loop starvation** -- a loopback dial carries no WS keepalive (``ping_interval`` is off),
  because the ping cannot tell a busy peer from a dead one; a frozen loop is bounded by the RPC and
  event budgets instead. Remote dials keep the library cadence.
* **transport failure** -- any socket exception surfaces as ``GatewayClientError`` carrying the
  unknown-outcome wording, never as ``ConnectionClosedError``.
* **gateway death** -- a request that never reached a socket says so ("nothing was sent").
* **client keepalive regression** -- the loopback carve-out is asserted directly, so re-introducing
  the default 20/20 keepalive on a loopback dial fails here rather than in the field.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import time

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from hermes_cli.gateway_client import (
    DISCONNECTED, REMOTE_WS_PING_INTERVAL_S, REMOTE_WS_PING_TIMEOUT_S,
    GatewayClient, GatewayClientError, _keepalive_kwargs, connect_gateway,
)
from hermes_cli.gateway_connection import NOT_SENT, GatewayConnection
from hermes_cli.gateway_tools import GatewayProcessView


def _keepalive_timeout_error():
    """The exact failure the gateway produced: websockets gave up on its own keepalive ping."""
    return ConnectionClosedError(None, Close(1011, 'keepalive ping timeout'))


# -- client keepalive regression -------------------------------------------------------------

def test_loopback_dial_carries_no_ws_keepalive():
    """The ping is not a liveness oracle for a peer whose loop we can starve."""
    for url in ('ws://127.0.0.1:8123/api/ws', 'ws://localhost:8123/api/ws',
                'ws://[::1]:8123/api/ws', 'http://127.0.0.1:8123/api/ws'):
        assert _keepalive_kwargs(url) == {}, url


def test_remote_dial_keeps_the_library_keepalive_cadence():
    """A tunnelled/proxied owner can go half-open, and there the ping does detect it."""
    kwargs = _keepalive_kwargs('wss://owner.example.com/api/ws')
    assert kwargs == {'ping_interval': REMOTE_WS_PING_INTERVAL_S,
                      'ping_timeout': REMOTE_WS_PING_TIMEOUT_S}


def test_connect_gateway_dials_loopback_without_keepalive(monkeypatch):
    """``connect_gateway`` applies the carve-out (not just the helper)."""
    seen = {}

    @contextlib.asynccontextmanager
    async def fake_connect(url, **kwargs):
        seen['url'], seen['kwargs'] = url, kwargs
        yield _NullSocket()

    import websockets.asyncio.client as ws_client
    monkeypatch.setattr(ws_client, 'connect', fake_connect)
    monkeypatch.setenv('HERMES_TUI_GATEWAY_URL', 'ws://127.0.0.1:8123/api/ws')

    async def drive():
        async with connect_gateway() as client:
            assert isinstance(client, GatewayClient)

    asyncio.run(drive())
    assert seen['url'] == 'ws://127.0.0.1:8123/api/ws'
    assert seen['kwargs'].get('ping_interval') is None, seen['kwargs']
    assert seen['kwargs'].get('proxy') is None  # the loopback proxy bypass is untouched


class _NullSocket:
    """A socket stand-in for a dial that opens and closes without traffic."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def send(self, raw):
        return None

    subprotocol = 'hermes-gateway-v1'


# -- transport failure -----------------------------------------------------------------------

class _ClosedSocket:
    def __init__(self):
        self.subprotocol = 'hermes-gateway-v1'

    async def send(self, raw):
        raise _keepalive_timeout_error()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_a_keepalive_drop_is_a_transport_error_not_a_library_exception():
    async def drive():
        client = GatewayClient(_ClosedSocket())
        async with client:
            await client.rpc('subagent.list', session_id='s1')

    with pytest.raises(GatewayClientError) as caught:
        asyncio.run(drive())
    assert str(caught.value) == DISCONNECTED
    assert not isinstance(caught.value, ConnectionClosedError)


class _ScriptedConnection(GatewayConnection):
    """``_send`` returns a settled future, so ``call_tagged``'s error mapping is tested alone."""

    def __init__(self, *, exc=None, result=None):
        super().__init__()
        self._exc, self._result = exc, result

    def _send(self, method, budget, params):
        future = concurrent.futures.Future()
        if self._exc is not None:
            future.set_exception(self._exc)
        else:
            future.set_result(self._result)
        return future, 1


def test_call_tagged_normalises_every_non_gateway_exception():
    conn = _ScriptedConnection(exc=_keepalive_timeout_error())
    with pytest.raises(GatewayClientError) as caught:
        conn.call('subagent.list', session_id='s1', timeout=5)
    assert str(caught.value) == DISCONNECTED
    assert isinstance(caught.value.__cause__, ConnectionClosedError)


def test_call_tagged_leaves_gateway_errors_and_stops_alone():
    conn = _ScriptedConnection(exc=GatewayClientError(NOT_SENT))
    with pytest.raises(GatewayClientError) as caught:
        conn.call('subagent.list', session_id='s1', timeout=5)
    assert str(caught.value) == NOT_SENT


def test_call_tagged_still_reports_a_silent_socket_as_no_answer():
    class _Hang(concurrent.futures.Future):
        def result(self, timeout=None):
            raise TimeoutError

    class _Hanging(_ScriptedConnection):
        def _send(self, method, budget, params):
            return _Hang(), 1

    with pytest.raises(GatewayClientError) as caught:
        _Hanging().call('subagent.list', session_id='s1', timeout=5)
    assert 'did not answer subagent.list' in str(caught.value)


# -- gateway death ---------------------------------------------------------------------------

def test_a_request_that_never_reached_a_socket_is_not_in_doubt():
    conn = GatewayConnection()  # no live socket and no connect factory: ``_send`` refuses
    conn.start = lambda timeout=90: None  # type: ignore[assignment]  -- skip the dial
    with pytest.raises(GatewayClientError) as caught:
        conn.call('subagent.list', session_id='s1', timeout=5)
    assert str(caught.value) == NOT_SENT
    assert 'outcome is unknown' not in str(caught.value)


# -- starvation must not read as an agent failure --------------------------------------------

class _FakeAgent:
    def __init__(self, connection):
        self.connection = connection
        self.session_id = 'sess-preserved'

    _agent_running = True


class _FlakyConnection:
    """Answers once, then cannot reach the gateway, then answers again (a redial)."""

    def __init__(self, rows, fail_times=1):
        self.rows, self.fail_times, self.calls = rows, fail_times, 0

    def call(self, method, *, timeout=None, **params):
        assert method == 'subagent.list'
        assert params.get('session_id') == 'sess-preserved', 'the session id must survive the loss'
        self.calls += 1
        if self.calls <= self.fail_times:
            raise GatewayClientError(DISCONNECTED)
        return {'subagents': list(self.rows)}


def test_a_lost_gateway_keeps_the_last_known_rows_and_says_they_are_stale():
    view = GatewayProcessView(_FakeAgent(_FlakyConnection([{'subagent_id': 'a', 'goal': 'work'}])))
    view.subagents = [{'subagent_id': 'a', 'goal': 'work'}]  # the last answer before the loss
    view._subagents_polled = 0.0

    rows = view.subagent_rows(busy=True)

    assert [r['subagent_id'] for r in rows] == ['a'], 'an unreachable gateway is not "no children"'
    assert view.subagents_stale is True


def test_reaching_the_gateway_again_clears_the_stale_mark():
    view = GatewayProcessView(_FakeAgent(_FlakyConnection([{'subagent_id': 'b'}], fail_times=1)))
    view.subagents = [{'subagent_id': 'a'}]
    view.subagent_rows(busy=True)
    assert view.subagents_stale is True

    view._subagents_polled = 0.0
    rows = view.subagent_rows(busy=True)

    assert [r['subagent_id'] for r in rows] == ['b']
    assert view.subagents_stale is False


# -- cosmetic polling must never kill the UI thread ------------------------------------------

class _ExplodingMonitor:
    """A monitor whose tick raises whatever the gateway just raised at it."""

    def __init__(self, exc, stop_after):
        self.exc, self.stop_after, self.ticks, self.cli = exc, stop_after, 0, None

    def tick(self):
        self.ticks += 1
        if self.ticks >= self.stop_after:
            self.cli._should_exit = True
        raise self.exc


def test_the_spinner_thread_survives_a_failing_poll(monkeypatch):
    """An uncaught tick exception used to print a background-thread traceback and stop the UI."""
    from hermes_cli.cli_tui_mixin import CLITuiMixin

    class _StubCLI:
        _should_exit = False
        _app = object()   # non-None: the loop does not idle-sleep
        _command_running = False

        def __init__(self, monitor):
            self._subagent_monitor = monitor

        def _invalidate(self, min_interval=0.0):
            return None

    monitor = _ExplodingMonitor(ConnectionClosedError(None, Close(1011, 'keepalive ping timeout')), 3)
    cli = _StubCLI(monitor)
    monitor.cli = cli

    escaped = []
    monkeypatch.setattr('threading.excepthook', lambda args: escaped.append(args.exc_value))

    CLITuiMixin._tui_spinner_loop(cli)  # returns only because the stub asked it to stop

    assert monitor.ticks == 3, 'the loop must survive a raising tick and keep polling'
    assert cli._should_exit is True
