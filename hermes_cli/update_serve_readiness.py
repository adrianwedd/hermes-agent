"""Readiness for backends respawned by an update: release, spawn, accept — verified, not assumed.

``_respawn_dashboard_processes`` acknowledging a spawn is not evidence that a replacement is serving.
The evidence is: the port was actually released by the outgoing process, and the replacement accepts
connections on it afterwards. Both are checked here, bounded, so an unattended update can report a
truthful failure instead of a success line while the dashboard is dead.
"""
from __future__ import annotations

import socket
import time
from typing import Callable, Iterable, Mapping, Optional

#: How long an outgoing backend gets to release its listening socket.
RELEASE_TIMEOUT_S = 15.0
#: How long a replacement gets to start accepting connections.
READY_TIMEOUT_S = 30.0
#: Poll interval for both waits.
_POLL_S = 0.1


def _listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something accepts a TCP connection on *host*:*port* right now."""
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_socket_release(
    host: str, port: int, timeout: float = RELEASE_TIMEOUT_S, *, poll: float = _POLL_S,
) -> bool:
    """True once nothing accepts on the port (the outgoing process let it go)."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not _listening(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def wait_for_ready(
    host: str, port: int, timeout: float = READY_TIMEOUT_S, *, poll: float = _POLL_S,
) -> bool:
    """True once the replacement accepts connections on the port."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _listening(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _label(entry: Mapping[str, object]) -> str:
    purpose = entry.get("purpose") or "backend"
    return f"{purpose} on {entry.get('host') or '127.0.0.1'}:{entry.get('port')}"


def verify_released_and_ready(
    entries: Iterable[Mapping[str, object]],
    spawn: Callable[[list[Mapping[str, object]]], Optional[list]],
    *,
    release_timeout: Optional[float] = None,
    ready_timeout: float = READY_TIMEOUT_S,
) -> tuple[list[str], list[Mapping[str, object]]]:
    """Release check → spawn → readiness check. ``(failures, entries actually verified ready)``.

    The release gate is opt-in (``release_timeout`` in seconds). It belongs to whoever stopped the
    backend, not to this resume path: by the time a resume runs the outgoing process should already
    be gone. When a caller does ask for it, a port still held after the wait is not spawned onto —
    that is the brittle case where a launcher refuses to bind and a supervisor's retry becomes the
    recovery mechanism — and it is reported instead. The entries that did come up are returned so
    the caller can say what ran.
    """
    failures: list[str] = []
    entries = list(entries)
    released: list[Mapping[str, object]] = []
    for entry in entries:
        port = entry.get("port")
        host = str(entry.get("host") or "127.0.0.1")
        if not isinstance(port, int):
            continue
        if release_timeout is None or wait_for_socket_release(host, port, release_timeout):
            released.append(entry)
        else:
            failures.append(f"{_label(entry)} did not release its listening socket within "
                            f"{release_timeout:g}s; it was not restarted onto a held port")

    spawn_failures = []
    if released:
        spawn_failures = list(spawn(released) or [])
        for item in spawn_failures:
            failures.append(f"{item} could not be relaunched")

    ready: list[Mapping[str, object]] = []
    if spawn_failures:
        # The verdict is already a failure and the spawn report does not attribute itself to
        # entries, so waiting the full readiness window would only delay a known-bad result.
        return failures, ready
    for entry in released:
        host = str(entry.get("host") or "127.0.0.1")
        port = entry.get("port")
        if not isinstance(port, int):
            continue
        if wait_for_ready(host, port, ready_timeout):
            ready.append(entry)
        else:
            failures.append(f"{_label(entry)} did not accept connections within {ready_timeout:g}s "
                            f"after relaunch")
    return failures, ready
