"""Materialise authorised native workspaces before readiness, never after claim."""
from pathlib import Path


def prepare_native_workspaces(conn):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_decision import decide
    from hermes_cli.kanban_completion_evidence import contract_record
    from hermes_cli.kanban_db_workspace import resolve_workspace, set_workspace_path
    missing = "Preparation required: resolve the owned source or evidence workspace"
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('todo','triage','ready') AND dispatch_eligible=1 "
        "AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL"
    ).fetchall()
    installed = Path(__file__).resolve().parents[1]
    for row in rows:
        task = kb.get_task(conn, row[0])
        if task is None:
            continue
        decision = decide(conn, task)
        _, contract = contract_record(conn, task.id)
        if decision["workflow_stage"] != "PREPARE" or decision["reason"] != missing or contract.get("qualified_for_dispatch") is not True:
            continue
        kind = task.workspace_kind or "scratch"
        # ``dir`` with no path cannot be resolved without an operator-supplied
        # absolute path — leave it for the operator. ``worktree``/``scratch`` with
        # no path ARE resolvable: scratch from the board's workspaces root, and
        # worktree from the board's ``default_workdir`` anchor. Auto-decomposer
        # children are born worktree-kind with an intentionally empty path (so
        # siblings don't collide on one checkout), so skipping the empty case
        # deadlocked every fan-out child in PREPARE until a manual write — the
        # dispatcher's own claim-time resolve (never reached from PREPARE) was the
        # only other materialiser. Resolve here, set the column preparation_reason
        # reads, then record the event.
        if not task.workspace_path and kind == "dir":
            continue
        if task.workspace_path:
            target = Path(task.workspace_path).expanduser().resolve()
            if not Path(task.workspace_path).is_absolute() or target == installed or installed in target.parents or target in installed.parents:
                continue
        try:
            workspace = resolve_workspace(task, board=kb.get_current_board())
        except (OSError, ValueError, RuntimeError) as exc:
            with kb.write_txn(conn):
                kb._append_event(conn, task.id, "workspace_preparation_failed", {
                    "failure_domain": "workflow", "reason": str(exc),
                    "workspace_path": str(task.workspace_path or ""),
                    "resolver": "control plane", "resume_condition": "native workspace provision succeeds",
                })
            continue
        # set_workspace_path opens its own write txn, so call it outside ours.
        set_workspace_path(conn, task.id, str(workspace))
        with kb.write_txn(conn):
            kb._append_event(conn, task.id, "workspace_prepared", {
                "workspace_path": str(workspace), "authority": "qualified_native_task_scope",
                "acceptance_changed": False,
            })
