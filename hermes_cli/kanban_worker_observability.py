"""Exact worker-session registration and CAS; never selects a run from a title or cwd.

This module is the ONLY writer of a run's ``launch_route`` and ``runtime_route``
metadata. ``kanban_run_routes`` reads both for the drawer's "Launch:" /
"Last observed:" lines, so an empty receipt there means "this run recorded
nothing", never "the configured route is unknown".
"""
import json

# (task_id, run_id) -> the route identity already durable on the board, and when the board
# was last written for it. One worker writes at most once a minute, exactly like its
# heartbeat: every API call of a 50-call turn must not become a board write.
_WRITTEN: dict = {}
_LAST_WRITE: dict = {}
_RECEIPT_COALESCE_SECONDS = 60.0


def _route_identity(session_id, receipt):
    """The part of a receipt that makes it a DIFFERENT route (``observed_at`` is freshness)."""
    return json.dumps(
        [session_id, {k: receipt.get(k) for k in ('provider', 'model', 'aggregator', 'advisers')}],
        sort_keys=True,
    )


def _slot(value):
    """``{provider, model}`` from a route slot, or None unless BOTH halves are legible."""
    if not isinstance(value, dict):
        return None
    provider = value.get('provider')
    model = value.get('model')
    if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
        return None
    return {'provider': provider[:100], 'model': model[:180]}


def route_receipt(*, provider, model, aggregator=None, advisers=(), observed_at=None):
    """Normalise one observed route into the exact shape ``kanban_run_routes.route`` reads.

    None unless the acting route itself is legible. ``aggregator``/``advisers`` stay
    None/empty when the caller could not observe them: an unobserved slot is recorded as
    absent, never filled in from config (that is what ``card_facts.model`` is for).
    """
    main_slot = _slot({'provider': provider, 'model': model})
    if not main_slot:
        return None
    main: dict = dict(main_slot)
    main['aggregator'] = _slot(aggregator)
    main['advisers'] = [slot for x in (advisers or []) if (slot := _slot(x))]
    main['observed_at'] = observed_at if type(observed_at) in (int, float) else None
    return main


def _live_claim_row(conn, *, task_id, run_id, worker_pid, claim_lock):
    """The row proving THIS process owns the current live claim on this exact run; else None."""
    return conn.execute(
        'SELECT r.metadata FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND r.task_id=? '
        'AND t.current_run_id=r.id AND t.status=\'running\' AND r.ended_at IS NULL AND r.worker_pid=? '
        'AND t.worker_pid=? AND r.claim_lock=? AND t.claim_lock=?',
        (run_id, task_id, worker_pid, worker_pid, claim_lock, claim_lock),
    ).fetchone()


def _merge_route(conn, *, task_id, run_id, worker_pid, claim_lock, session_id,
                 launch=None, observed_route=None):
    """Merge session id + launch/session routes into the run's metadata under the live claim.

    Returns ``(ok, route_changed)``. Caller must hold the native board's write txn. A
    differing existing session id is refused: compression rotates the id and needs an
    explicit additive chain, and a route receipt from another session is not this run's.
    """
    if not all([task_id, session_id, claim_lock]) or type(run_id) is not int or type(worker_pid) is not int:
        return False, False
    row = _live_claim_row(
        conn, task_id=task_id, run_id=run_id, worker_pid=worker_pid, claim_lock=claim_lock,
    )
    if row is None:
        return False, False
    try:
        meta = json.loads(row[0] or '{}')
    except (ValueError, TypeError):
        return False, False
    if not isinstance(meta, dict):
        return False, False
    existing = meta.get('worker_session_id')
    if existing and existing != session_id:
        return False, False  # compression requires explicit additive chain support
    meta['worker_session_id'] = session_id
    if 'launch_route' not in meta:
        launch_slot = _slot(launch)
        if launch_slot:
            meta['launch_route'] = launch_slot
    route_changed = False
    if observed_route is not None and meta.get('runtime_route') != observed_route:
        meta['runtime_route'] = observed_route
        route_changed = True
    encoded = json.dumps(meta, separators=(',', ':'))
    if encoded != row[0]:
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=? AND metadata IS ?', (encoded, run_id, row[0]))
    return True, route_changed


def stamp(conn, *, task_id, run_id, worker_pid, claim_lock, session_id, launch=None, observed_route=None):
    """Record this run's session id, launch route and last observed route under the live claim.

    True when the write was accepted (an identical re-stamp is a no-op that still returns
    True, so a caller cannot mistake idempotence for a lost claim). Caller must hold the
    native board's write_txn.
    """
    ok, _route_changed = _merge_route(
        conn, task_id=task_id, run_id=run_id, worker_pid=worker_pid, claim_lock=claim_lock,
        session_id=session_id, launch=launch, observed_route=observed_route,
    )
    return ok


def _env_coordinates():
    """The dispatcher-pinned ``(task_id, run_id, claim_lock, session_id)`` for THIS process."""
    import os
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    raw_run = os.environ.get('HERMES_KANBAN_RUN_ID')
    claim = os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
    if not task_id or not raw_run or not claim:
        return None
    try:
        run_id = int(raw_run)
    except (TypeError, ValueError):
        return None
    return task_id, run_id, claim


def record_worker_route(agent, moa_client=None):
    """Persist this worker's launch route and last observed MoA route on its own run.

    Called by the worker's own turn loop (``agent/turn_usage.py``) for each recorded API
    response, so the receipt describes a call that actually happened and carries the real
    aggregator/adviser slots the MoA fan-out resolved. Returns True when a write landed;
    False when this process is not the live claim owner, is not a dispatched worker, or
    already recorded exactly this route.

    The route is ALSO appended as an immutable ``worker_route_recorded`` event, because a
    terminal transition replaces ``task_runs.metadata`` with the worker's own handoff.
    """
    import os
    import time
    coordinates = _env_coordinates()
    session_id = getattr(agent, 'session_id', None)
    if coordinates is None or not isinstance(session_id, str) or not session_id:
        return False
    task_id, run_id, claim = coordinates
    from tools.kanban_tools import _board, _is_dispatcher_owned_worker
    if not _is_dispatcher_owned_worker():
        return False
    provider = getattr(agent, 'provider', None)
    model = getattr(agent, 'model', None)
    aggregator = getattr(moa_client, 'last_aggregator_slot', None) if moa_client is not None else None
    advisers = getattr(moa_client, 'last_reference_slots', None) if moa_client is not None else None
    receipt = route_receipt(
        provider=provider, model=model, aggregator=aggregator,
        advisers=[x for x in (advisers or []) if isinstance(x, dict)], observed_at=time.time(),
    )
    if receipt is None:
        return False
    identity = _route_identity(session_id, receipt)
    now = time.monotonic()
    if _WRITTEN.get((task_id, run_id)) == identity and (
        now - _LAST_WRITE.get((task_id, run_id), 0.0) < _RECEIPT_COALESCE_SECONDS
    ):
        return False
    with _board(None, quiet_close=True) as (kb, conn):
        with kb.write_txn(conn):
            before = conn.execute(
                'SELECT metadata FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id),
            ).fetchone()
            if before is None:
                return False
            try:
                old = json.loads(before[0] or '{}')
            except (ValueError, TypeError):
                old = {}
            ok, route_changed = _merge_route(
                conn, task_id=task_id, run_id=run_id, worker_pid=os.getpid(), claim_lock=claim,
                session_id=session_id,
                launch={'provider': provider, 'model': model}, observed_route=receipt,
            )
            if not ok:
                # Lost the live claim (or a rotated session id with no additive chain yet):
                # the receipt would describe a run this process no longer owns.
                return False
            registered = json.loads(
                conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()[0]
            )
            startup = not isinstance(old, dict) or not old.get('worker_session_id')
            if startup or route_changed:
                # An immutable event per recorded route: the run's own metadata is replaced by
                # the worker's handoff at a terminal transition, and a startup receipt alone
                # cannot carry a route observed after startup.
                kb._append_event(conn, task_id, 'worker_session_registered' if startup else 'worker_route_recorded', {
                    'worker_session_id': session_id,
                    'launch_route': registered.get('launch_route'),
                    'runtime_route': registered.get('runtime_route'),
                }, run_id=run_id)
            notify_fields = ['worker_session_id'] if startup else ['runtime_route']
        kb.notify_task_updated(conn, task_id, notify_fields, board=os.environ.get('HERMES_KANBAN_BOARD'))
    _WRITTEN[(task_id, run_id)] = identity
    _LAST_WRITE[(task_id, run_id)] = now
    return True


def register_from_agent(agent):
    """Worker-only startup receipt; context + exact live claim fence every write."""
    return record_worker_route(agent)


def registered_receipt(conn, task_id, run_id):
    """Earliest immutable startup receipt for this exact run, never inferred configuration.

    Merged with the LATEST recorded runtime route for the run: the startup receipt is the
    earliest evidence of who ran, while the route receipt is the newest one, and the run's
    own metadata is replaced by the worker's handoff at a terminal transition.
    """
    import sqlite3
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_session_registered' "
            "ORDER BY id LIMIT 1", (task_id, run_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    receipt = {}
    if row is not None:
        try:
            value = json.loads(row[0] or '{}')
        except (TypeError, ValueError):
            value = {}
        receipt = value if isinstance(value, dict) else {}
    try:
        latest = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind IN ('worker_session_registered','worker_route_recorded') "
            "AND payload LIKE '%\"runtime_route\"%' ORDER BY id DESC LIMIT 1", (task_id, run_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return receipt
    if latest is None:
        return receipt
    try:
        value = json.loads(latest[0] or '{}')
    except (TypeError, ValueError):
        return receipt
    if not isinstance(value, dict) or not value.get('runtime_route'):
        return receipt
    return {**receipt, 'runtime_route': value['runtime_route']}
