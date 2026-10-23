"""Operator CLI for inspectable gate contracts and authorised certificates."""
import json


def dispatch(args):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_wait_transitions import resolve_gate, declare_wait
    from hermes_cli.kanban_decision import decide
    with connect(board=args.board) as conn:
        task_id = args.task_id
        if args.wait_operation == "inspect":
            result = decide(conn, kb.get_task(conn, task_id))
        else:
            if not args.document:
                raise ValueError("This transition requires a JSON declaration/evidence document")
            with open(args.document, encoding="utf-8") as source:
                document = json.load(source)
            if args.wait_operation == "declare-output":
                from hermes_cli.kanban_dependency_output import declare
                result = declare(conn, task_id, document["parent_id"], document["requirement_id"], evidence_identity=document["evidence_identity"], requirement=document["requirement"])
            elif args.wait_operation == "certify-output":
                from hermes_cli.kanban_dependency_output import certify_output
                result = certify_output(conn, task_id, contract_event_id=document["contract_event_id"], artifact_attachment_id=document["artifact_attachment_id"], criterion_id=document["criterion_id"], evidence=document["evidence"])
            elif args.wait_operation == "accept-output":
                from hermes_cli.kanban_dependency_output import accept
                result = accept(conn, task_id, document["requirement_id"], declaration_event_id=document["declaration_event_id"], producer_evidence_event_id=document["producer_evidence_event_id"])
            elif args.wait_operation == "supersede":
                from hermes_cli.kanban_wait_supersede import supersede_gate
                result = supersede_gate(conn, task_id, **document)
            elif args.wait_operation == "declare":
                result = declare_wait(conn, task_id, document["gates"], document["continuation"], provenance=document["provenance"], refinement=document.get("refinement"))
                kb.recompute_ready(conn)
            else:
                result = resolve_gate(conn, task_id, document["gate_id"], revision=document["revision"], declaration=document["declaration"], evidence=document["evidence"], decision=document.get("decision"), invalidate=args.wait_operation == "invalidate")
        print(json.dumps(result, indent=2))
    return 0
