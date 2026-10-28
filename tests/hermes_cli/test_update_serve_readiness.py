"""Service lifecycle after an update: release, spawn, readiness — verified, not assumed.

Real loopback sockets on ephemeral ports; nothing here starts, stops or inspects a live Hermes
service. The claim under test is the one that failed in production: a relaunch that returned
"requested" was treated as a healthy replacement.
"""
from __future__ import annotations

import socket

import pytest

from hermes_cli import update_serve_readiness as ready_mod


class _Listener:
    """A bound, listening socket that answers one accept at a time."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_release_wait_is_true_when_nothing_listens():
    assert ready_mod.wait_for_socket_release("127.0.0.1", _free_port(), 0.2) is True


def test_release_wait_is_false_while_the_socket_is_held():
    listener = _Listener()
    try:
        assert ready_mod.wait_for_socket_release("127.0.0.1", listener.port, 0.3) is False
    finally:
        listener.close()


def test_release_wait_succeeds_once_the_holder_exits():
    listener = _Listener()
    port = listener.port
    threading_close = listener.close
    # Close after a short delay: the wait must observe the release, not time out.
    import threading
    threading.Timer(0.2, threading_close).start()
    assert ready_mod.wait_for_socket_release("127.0.0.1", port, 5.0) is True


def test_readiness_is_false_when_the_replacement_never_binds():
    assert ready_mod.wait_for_ready("127.0.0.1", _free_port(), 0.3) is False


def test_readiness_is_true_once_something_accepts():
    listener = _Listener()
    try:
        assert ready_mod.wait_for_ready("127.0.0.1", listener.port, 2.0) is True
    finally:
        listener.close()


def test_held_port_is_reported_and_never_spawned_onto():
    """The brittle case: spawning onto a held port is what made launchd retries the recovery path."""
    listener = _Listener()
    spawned: list[list] = []
    try:
        failures, ready = ready_mod.verify_released_and_ready(
            [{"purpose": "dashboard", "host": "127.0.0.1", "port": listener.port}],
            lambda entries: spawned.append(list(entries)) or [],
            release_timeout=0.3, ready_timeout=0.3,
        )
    finally:
        listener.close()

    assert spawned == [], "no replacement may be launched onto a socket that was not released"
    assert ready == []
    assert len(failures) == 1 and "did not release" in failures[0]


def test_spawn_then_verify_ready_and_reports_a_silent_replacement():
    port = _free_port()
    entries = [{"purpose": "dashboard", "host": "127.0.0.1", "port": port}]
    listener: list[_Listener] = []

    def spawn(selected):
        assert selected == entries
        holder = _Listener.__new__(_Listener)
        holder.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.sock.bind(("127.0.0.1", port))
        holder.sock.listen(8)
        holder.port = port
        listener.append(holder)
        return []

    try:
        failures, ready = ready_mod.verify_released_and_ready(
            entries, spawn, release_timeout=1.0, ready_timeout=2.0)
        assert failures == []
        assert ready == entries
    finally:
        for holder in listener:
            holder.close()


def test_a_spawn_that_accepts_nothing_is_a_failure():
    port = _free_port()
    entries = [{"purpose": "serve", "host": "127.0.0.1", "port": port}]

    failures, ready = ready_mod.verify_released_and_ready(
        entries, lambda selected: [], release_timeout=0.3, ready_timeout=0.3)

    assert ready == []
    assert len(failures) == 1 and "did not accept connections" in failures[0]


def test_spawn_failures_are_carried_into_the_verdict():
    port = _free_port()
    entries = [{"purpose": "serve", "host": "127.0.0.1", "port": port}]

    failures, _ = ready_mod.verify_released_and_ready(
        entries, lambda selected: ["pid 1234"], release_timeout=0.3, ready_timeout=0.3)

    assert any("could not be relaunched" in f for f in failures)


@pytest.mark.parametrize("entry", [
    {"purpose": "serve", "host": "127.0.0.1", "port": None},
    {"purpose": "serve", "host": "127.0.0.1", "port": "9119"},
])
def test_entries_without_a_usable_port_are_ignored(entry):
    spawned: list[list] = []
    failures, ready = ready_mod.verify_released_and_ready(
        [entry], lambda entries: spawned.append(list(entries)) or [],
        release_timeout=0.1, ready_timeout=0.1)
    assert (failures, ready, spawned) == ([], [], [])
