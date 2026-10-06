"""Share the host worker budget with bounded synchronous Triage intake."""
from contextlib import contextmanager


@contextmanager
def intake_permit(aux_task):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_provider_admission as admission
    home = kb.kanban_home()
    limits = admission.policy(home)
    if limits is None:
        yield ""
        return
    # Hold the same nonblocking cross-board lock through the auxiliary request.
    # Other intake and worker claims defer until this synchronous slot is freed;
    # no durable lease can be orphaned by a crashed gateway.
    with admission.host_lock(home) as acquired:
        if not acquired:
            yield "provider admission busy"
            return
        config = admission.read_raw(home / "config.yaml")
        route = (config.get("auxiliary") or {}).get(aux_task)
        if (not isinstance(route, dict) or not route.get("model")
                or not admission.remote_route(route)):
            yield "intake requires an explicit Codex or Ollama Cloud route without fallbacks"
            return
        paths = [kb.kanban_db_path(board=b.get("slug"))
                 for b in kb.list_boards(include_archived=True)]
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            paths.extend(row[2] for row in conn.execute("PRAGMA database_list")
                         if row[1] == "main" and row[2])
        counts = admission.count_lanes(paths)
        if counts["cloud"] >= limits["cloud"] or sum(counts.values()) >= sum(limits.values()):
            yield "provider admission capacity exhausted"
            return
        from agent.auxiliary_route_fence import admitted_route
        with admitted_route(route):
            yield ""
