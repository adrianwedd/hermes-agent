"""Commissioned native-transition fixture evidence for unrelated DB lifecycle tests."""
import hashlib
import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import write_txn


def complete_fixture_task(conn, task_id, **kwargs):
    # These test cards exercise native transitions, not a real coding/audit deliverable.
    assert kb.get_task(conn, task_id) is not None
    with write_txn(conn):
        kb._append_event(conn, task_id, "completion_requirements", {
            "version": 1, "kind": "audit", "criteria": ["fixture_exists"],
        })
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    output = b"Native test fixture exists; this receipt commissions only its DB transition.\n"
    aid = kb.store_attachment_bytes(conn, task_id, "fixture-existence.txt", output, uploaded_by="test")
    evidence = {"task_id": task_id, "contract_event_id": cid,
        "artifacts": [{"attachment_id": aid, "sha256": hashlib.sha256(output).hexdigest()}],
        "acceptance": [{"criterion_id": "fixture_exists", "satisfied": True, "artifact_ids": [aid]}]}
    eid = kb.store_attachment_bytes(conn, task_id, "completion-evidence.json",
        json.dumps(evidence).encode(), uploaded_by="test")
    metadata = dict(kwargs.pop("metadata", None) or {})
    metadata["completion_evidence_attachment_id"] = eid
    return kb.complete_task(conn, task_id, metadata=metadata, **kwargs)
