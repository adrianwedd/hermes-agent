"""CLI completion refusal rendering at the native DONE boundary."""

from __future__ import annotations
import argparse


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark tasks done while rendering completion-evidence refusals."""
    from hermes_cli import kanban as cli
    from hermes_cli.kanban_completion_evidence import CompletionEvidenceError

    ids, rc = cli._require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Handoff fields are per-run; refuse to copy them across N runs.
    if len(ids) > 1 and (summary or raw_meta):
        return cli._err(
            "kanban: --summary / --metadata are per-task and can't be used "
            "with multiple ids (would apply the same handoff to every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.",
            2,
        )
    metadata, rc = cli._parse_metadata_flag(raw_meta)
    if rc:
        return rc
    fail_msg: dict[str, str] = {}
    with cli.kbc.connect_closing() as conn:

        def op(tid):
            from hermes_cli.kanban_completion_workflow import require_declared_contract
            try:
                require_declared_contract(conn, tid)
                from hermes_cli.kanban_completion_evidence import prepare_gate
                evidence_gate = prepare_gate(conn, tid, metadata, expected_run_id=cli._worker_run_id_for(tid), force=bool(getattr(args, "force", False)))
                if evidence_gate is False:
                    fail_msg[tid] = "completion evidence snapshot changed; retry handoff"
                    return False
            except CompletionEvidenceError as exc:
                fail_msg[tid] = str(exc)
                return False
            gate_err = cli._goal_gate_error(
                conn,
                tid,
                (summary or args.result or "").strip(),
                "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.",
            )
            if gate_err:
                fail_msg[tid] = gate_err
                return False
            fail_msg[tid] = f"cannot complete {tid} (unknown id or terminal state)"
            try:
                done = cli.kb.complete_task(
                    conn,
                    tid,
                    result=args.result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=cli._worker_run_id_for(tid),
                    force=bool(getattr(args, "force", False)),
                    _prepared_evidence=evidence_gate,
                )
            except cli.kb.LiveClaimError:
                fail_msg[tid] = (
                    f"cannot complete {tid}: a live worker is running it. Wait for the "
                    f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                    f"--force to close its run and complete anyway."
                )
                return False
            except CompletionEvidenceError as evidence_err:
                fail_msg[tid] = str(evidence_err)
                return False
            except cli.kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (
                    f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                    f"describing what was done (an empty completion is not evidence)."
                )
                return False
            if not done:
                # complete_task returns bare False for a dependency refusal too;
                # name the open parents instead of claiming the id is unknown.
                blockers = cli.kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (
                        f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                        f"complete the parents first, or `hermes kanban unlink <parent> {tid}`."
                    )
            return done

        return cli._bulk_apply(
            ids, op, lambda tid: f"Completed {tid}", fail_msg.__getitem__
        )


def declare_requirements(args: argparse.Namespace) -> int:
    """Supported operator migration for inactive legacy cards, guarded by snapshot."""
    import json
    import os
    from pathlib import Path
    from hermes_cli import kanban as cli, kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_completion_evidence import set_requirements, contract_record

    if os.environ.get("HERMES_KANBAN_TASK"):
        return cli._err("Only an operator outside a worker context may declare completion requirements")
    path = Path(args.file)
    try:
        if path.stat().st_size > 1024 * 1024:
            return cli._err("Completion requirements file exceeds 1 MiB", 2)
        contract = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return cli._err(f"Cannot read completion requirements: {exc}", 2)
    with connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return cli._err(f"Unknown task {args.task_id}", 2)
        set_requirements(conn, args.task_id, contract,
                         expected_status=args.expect_status,
                         expected_assignee=args.expect_assignee,
                         board=getattr(args, "board", None))
        event_id, _ = contract_record(conn, args.task_id)
    result = {"task_id": args.task_id, "contract_event_id": event_id, "requirements": contract}
    print(json.dumps(result) if args.json else f"Declared requirements for {args.task_id}: event {event_id}")
    return 0
