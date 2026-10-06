"""Explicit scope contracts and durable evidence at the native DONE boundary.

Request-review is intentionally unaffected. Network evidence is collected before
SQLite's terminal transaction; its ownership/contract snapshot is checked again
under the terminal lock. Historical DONE rows and run receipts are never rewritten.
"""

from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path

KINDS = {"implementation", "audit", "research", "spec", "review", "program"}
SHA = re.compile("[0-9a-f]{40}")
REPO = re.compile("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class CompletionEvidenceError(ValueError):
    def __init__(self, task_id, missing):
        self.task_id = task_id
        self.missing = missing
        super().__init__(
            "completion blocked: "
            + task_id
            + "; "
            + "; ".join(missing)
            + ". Keep the card open or request review; attach the missing evidence and retry."
        )


def contract_record(conn, task_id):
    row = conn.execute(
        "SELECT id,payload FROM task_events WHERE task_id=? AND kind='completion_requirements' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return (None, None)
    try:
        value = json.loads(row[1] or "{}")
    except (ValueError, TypeError):
        value = {}
    return (row[0], value)


def validate_contract(value):
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("kind") not in KINDS
    ):
        raise ValueError(
            "Declare version=1 and explicit kind: implementation/audit/research/spec/review/program"
        )
    criteria = value.get("criteria")
    if (
        not isinstance(criteria, list)
        or not criteria
        or any((not isinstance(c, str) or not c.strip() for c in criteria))
        or (len(set(criteria)) != len(criteria))
    ):
        raise ValueError("Declare distinct nonblank acceptance criterion IDs")
    if value["kind"] == "implementation" and not value.get("local_only", False) and (
        not REPO.fullmatch(str(value.get("repository") or ""))
    ):
        raise ValueError("Implementation contract requires repository OWNER/REPO unless the operator explicitly declares local_only=true")
    for key in [
        "local_only",
        "require_pr",
        "require_issue",
        "require_review",
        "require_commands",
        "implementation_handoff_required",
        "qualified_for_dispatch",
    ]:
        if key in value and type(value[key]) is not bool:
            raise ValueError(key + " must be a boolean")
    for key in ["required_children", "handoff_children"]:
        if key in value and (
            not isinstance(value[key], list)
            or any(
                (
                    not isinstance(t, str) or not re.fullmatch("t_[0-9a-f]+", t)
                    for t in value[key]
                )
            )
        ):
            raise ValueError(key + " must contain exact card IDs")
    if value["kind"] == "program" and (not value.get("required_children")):
        raise ValueError(
            "Program contract requires implementation child IDs; a spec alone cannot complete it"
        )
    if value.get("implementation_handoff_required") and (
        not value.get("handoff_children")
    ):
        raise ValueError("Spec contract requires linked implementation child IDs")
    return value


def set_requirements(
    conn,
    task_id,
    contract,
    *,
    expected_status,
    expected_assignee,
    expected_run_id=None,
    board=None,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    import os
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("Worker contexts cannot declare operator completion requirements")
    validate_contract(contract)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status,assignee,current_run_id,worker_pid,claim_lock FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        if tuple(row[:3]) != (expected_status, expected_assignee, expected_run_id):
            raise RuntimeError(
                "Card snapshot changed; reread before declaring completion requirements"
            )
        if row[0] == "running" or any((row[i] is not None for i in (2, 3, 4))):
            raise RuntimeError(
                "Do not change requirements under an active worker owner"
            )
        kb._append_event(conn, task_id, "completion_requirements", contract)
    kb.notify_task_updated(conn, task_id, ["completion_requirements"], board=board)
    return True


def _snapshot(conn, task_id):
    row = conn.execute(
        "SELECT status,current_run_id,assignee,completion_contract FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    cid, contract = contract_record(conn, task_id)
    return tuple(row) + (cid,) if row is not None else None


def _attachment(conn, task_id, aid):
    if type(aid) is not int:
        return None
    row = conn.execute(
        "SELECT filename,stored_path FROM task_attachments WHERE task_id=? AND id=?",
        (task_id, aid),
    ).fetchone()
    if row is None:
        return None
    try:
        path = Path(row[1])
        data = path.read_bytes()
        if not data:
            return None
        return {
            "id": aid,
            "filename": row[0],
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": data,
        }
    except OSError:
        return None


def _refuse(conn, task_id, missing, *, expected_snapshot=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        if expected_snapshot is not None and _snapshot(conn, task_id) != expected_snapshot:
            raise CompletionEvidenceError(task_id, ["Completion snapshot changed; preserve the current owner and reread"])
        _, current_contract = contract_record(conn, task_id)
        operator_review_pending = (
            isinstance(current_contract, dict)
            and current_contract.get('kind') == 'implementation'
            and current_contract.get('require_review', True)
            and expected_snapshot is not None
            and _snapshot(conn, task_id) == expected_snapshot
            and any(reason in {'Reviewed coding acceptance requires operator approval from REVIEW; request-review preserves the handoff', 'Explicit local-only coding still requires approved REVIEW unless its operator contract waives review'} for reason in missing if isinstance(reason, str))
        )
        try:
            validate_contract(current_contract)
            operator_declaration_pending = False
        except ValueError:
            operator_declaration_pending = any(
                isinstance(reason, str) and reason.startswith("Operator must declare")
                for reason in missing
            )
        if operator_declaration_pending:
            conn.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (task_id,))
            kb._append_event(conn, task_id, "operator_contract_handoff", {
                "automatic_retry": False, "dispatch_eligible": False,
                "operator_action_required": True,
            })
        if operator_review_pending:
            # A worker requesting independent review must remain eligible for
            # that lane. Only a completed review awaiting operator approval is
            # parked; existing manual/admin eligibility holds are never lifted.
            if expected_snapshot[0] == 'review':
                conn.execute("UPDATE tasks SET dispatch_eligible=0 WHERE id=?", (task_id,))
                kb._append_event(conn, task_id, 'operator_review_handoff', {'automatic_retry': False, 'dispatch_eligible': False})
            else:
                kb._append_event(conn, task_id, 'independent_review_required', {'automatic_implementation_retry': False, 'review_dispatch_allowed_if_eligible': True})
        kb._append_event(
            conn, task_id, "completion_evidence_rejected", {"missing": missing}
        )
    raise CompletionEvidenceError(task_id, missing)


def _durable_evidence(conn, task_id, cid, contract, metadata):
    aid = (
        metadata.get("completion_evidence_attachment_id")
        if isinstance(metadata, dict)
        else None
    )
    attachment = _attachment(conn, task_id, aid)
    if not attachment:
        return (
            None,
            [],
            [
                "Attach completion-evidence.json and supply metadata.completion_evidence_attachment_id"
            ],
        )
    if (
        not re.fullmatch(
            "completion-evidence(?: \\(\\d+\\))?\\.json", attachment["filename"]
        )
        or len(attachment["data"]) > 1000000
    ):
        return (
            None,
            [],
            ["Evidence attachment must be completion-evidence.json, at most 1 MiB"],
        )
    try:
        evidence = json.loads(attachment["data"])
    except (ValueError, TypeError):
        return (None, [], ["completion-evidence.json must be valid JSON"])
    if (
        not isinstance(evidence, dict)
        or evidence.get("task_id") != task_id
        or evidence.get("contract_event_id") != cid
    ):
        return (
            None,
            [],
            ["Evidence must bind this exact task and current contract_event_id"],
        )
    missing = []
    verified = [(aid, attachment["sha256"])]
    artifacts = {}
    for item in (
        evidence.get("artifacts", [])
        if isinstance(evidence.get("artifacts"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        artifact = _attachment(conn, task_id, item.get("attachment_id"))
        if (
            artifact
            and artifact["id"] != aid
            and (artifact["sha256"] == item.get("sha256"))
        ):
            artifacts[artifact["id"]] = artifact
            verified.append((artifact["id"], artifact["sha256"]))
    if not artifacts:
        missing.append(
            "At least one nonempty native artifact with matching SHA256 is required; the evidence manifest cannot vouch for itself"
        )
    acceptance = evidence.get("acceptance")
    acceptance = acceptance if isinstance(acceptance, list) else []
    for criterion in contract["criteria"]:
        matches = [
            x
            for x in acceptance
            if isinstance(x, dict)
            and x.get("criterion_id") == criterion
            and (x.get("satisfied") is True)
            and isinstance(x.get("artifact_ids"), list)
            and x["artifact_ids"]
            and all((type(a) is int and a in artifacts for a in x["artifact_ids"]))
        ]
        if not matches:
            missing.append("Acceptance evidence missing for criterion " + criterion)
    if contract.get("require_commands", contract["kind"] == "implementation"):
        commands = evidence.get("commands")
        commands = commands if isinstance(commands, list) else []
        if not any(
            (
                isinstance(x, dict)
                and isinstance(x.get("cmd"), str)
                and x["cmd"].strip()
                and (type(x.get("exit_code")) is int)
                and (x["exit_code"] == 0)
                and (type(x.get("output_attachment_id")) is int)
                and (x["output_attachment_id"] in artifacts)
                for x in commands
            )
        ):
            missing.append(
                "A reproducible successful command and durable output attachment are required"
            )
    return (evidence, verified, missing)


def _children_missing(conn, task_id, contract):
    missing = []
    linked = {
        r[0]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id=?", (task_id,)
        )
    }
    for child in contract.get("required_children", []):
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()
        _, child_contract = contract_record(conn, child)
        if (
            child not in linked
            or row is None
            or row[0] not in {"done", "archived"}
            or (not isinstance(child_contract, dict))
            or (child_contract.get("kind") not in {"implementation", "program"})
        ):
            missing.append(
                "Program implementation child "
                + child
                + " must exist, be linked and complete under an implementation/program contract"
            )
    if contract.get("implementation_handoff_required"):
        for child in contract.get("handoff_children", []):
            row = conn.execute(
                "SELECT assignee,dispatch_eligible,status FROM tasks WHERE id=?",
                (child,),
            ).fetchone()
            _, child_contract = contract_record(conn, child)
            try:
                validate_contract(child_contract)
            except ValueError:
                child_contract = None
            if (
                child not in linked
                or row is None
                or (not row[0])
                or (not child_contract)
                or (child_contract.get("kind") not in {"implementation", "program"})
            ):
                missing.append(
                    "Spec handoff "
                    + child
                    + " needs an existing linked implementation card, owner and explicit acceptance contract"
                )
                continue
            blockers = conn.execute(
                "SELECT p.id FROM task_links l JOIN tasks p ON p.id=l.parent_id WHERE l.child_id=? AND p.id!=? AND p.status NOT IN ('done','archived')",
                (child, task_id),
            ).fetchall()
            if row[1] and (
                blockers or child_contract.get("qualified_for_dispatch") is not True
            ):
                missing.append(
                    "Unqualified handoff "
                    + child
                    + " must remain dispatch-ineligible until prerequisites and qualification are satisfied"
                )
    return missing


def _coding_missing(contract, evidence, status, force, assignee, api=None):
    from hermes_cli.kanban_pr_acceptance import _api, _assignee_profile_home

    api = api or _api
    missing = []
    proof = {}
    delivery = evidence.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    repo = contract["repository"]
    sha = delivery.get("commit_sha")
    branch = delivery.get("branch")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        missing.append("Coding delivery requires exact commit_sha")
    if (
        not isinstance(branch, str)
        or not branch
        or any(
            (x in branch for x in ["..", "@{", "\\", " ", ":", "?", "*", "[", "~", "^"])
        )
    ):
        missing.append("Coding delivery requires a valid published branch")
    if contract.get("require_review", True) and (not (status == "review" and force)):
        missing.append(
            "Reviewed coding acceptance requires operator approval from REVIEW; request-review preserves the handoff"
        )
    if missing:
        return (missing, proof)
    from urllib.parse import quote

    try:
        home = _assignee_profile_home(assignee)
        commit = api("repos/" + repo + "/git/commits/" + sha, profile_home=home)
        ref = api(
            "repos/" + repo + "/git/ref/heads/" + quote(branch, safe=""),
            profile_home=home,
        )
        if commit.get("sha") != sha or (ref.get("object") or {}).get("sha") != sha:
            missing.append("Current remote commit/branch head must match commit_sha")
        proof.update(
            commit_sha=sha,
            branch=branch,
            remote_head=(ref.get("object") or {}).get("sha"),
        )
        if contract.get("require_pr", True):
            url = delivery.get("pr_url")
            match = re.fullmatch(
                "https://github\\.com/" + re.escape(repo) + "/pull/([1-9][0-9]*)",
                str(url or ""),
            )
            if not match:
                missing.append("Matching repository PR URL is required")
            else:
                pr = api("repos/" + repo + "/pulls/" + match[1], profile_home=home)
                if (
                    (pr.get("head") or {}).get("sha") != sha
                    or (pr.get("head") or {}).get("ref") != branch
                    or (pr.get("state") == "closed" and (not pr.get("merged")))
                ):
                    missing.append(
                        "Current PR head/branch must match delivery and remain open or merged"
                    )
                proof.update(pr_url=url, pr_head=(pr.get("head") or {}).get("sha"))
        if contract.get("require_issue", True):
            url = delivery.get("issue_url")
            match = re.fullmatch(
                "https://github\\.com/" + re.escape(repo) + "/issues/([1-9][0-9]*)",
                str(url or ""),
            )
            if not match:
                missing.append("Matching repository issue handoff URL is required")
            else:
                issue = api("repos/" + repo + "/issues/" + match[1], profile_home=home)
                if issue.get("html_url") != url or "pull_request" in issue:
                    missing.append(
                        "Issue handoff must resolve to the exact issue, not a PR"
                    )
                proof["issue_url"] = url
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        missing.append(
            "Remote delivery readback unavailable; fix this assignee profile’s repository access and retry without closing"
        )
    return (missing, proof)


def _local_coding_missing(contract, status, force):
    missing = []
    if contract.get("require_review", True) and not (status == "review" and force):
        missing.append("Explicit local-only coding still requires approved REVIEW unless its operator contract waives review")
    return missing, {"delivery_scope": "explicit_local_only", "basis": "Native hashed artifacts and declared acceptance; no remote publication required"}


def prepare_gate(
    conn, task_id, metadata, *, expected_run_id=None, force=False, api=None
):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    if snapshot[0] not in {"running", "ready", "blocked", "review"} or (
        expected_run_id is not None and snapshot[1] != expected_run_id
    ):
        return False
    cid, contract = contract_record(conn, task_id)
    try:
        validate_contract(contract)
    except ValueError as exc:
        _refuse(
            conn,
            task_id,
            [
                "Operator must declare the card’s explicit completion requirements: "
                + str(exc)
            ],
            expected_snapshot=snapshot,
        )
    evidence, artifacts, missing = _durable_evidence(
        conn, task_id, cid, contract, metadata
    )
    missing += _children_missing(conn, task_id, contract)
    remote = {}
    if evidence and contract["kind"] == "implementation":
        if contract.get("local_only", False):
            extra, remote = _local_coding_missing(contract, snapshot[0], force)
        else:
            extra, remote = _coding_missing(
                contract, evidence, snapshot[0], force, snapshot[2], api
            )
        missing += extra
    if missing:
        _refuse(conn, task_id, missing, expected_snapshot=snapshot)
    return {
        "snapshot": snapshot,
        "contract_event_id": cid,
        "kind": contract["kind"],
        "artifact_hashes": artifacts,
        "remote": remote,
        "criteria": contract["criteria"],
    }


def record_gate(conn, task_id, prepared):
    from hermes_cli import kanban_db as kb

    if _snapshot(conn, task_id) != prepared["snapshot"]:
        return False
    _, contract = contract_record(conn, task_id)
    if _children_missing(conn, task_id, contract):
        return False
    for aid, digest in prepared["artifact_hashes"]:
        current = _attachment(conn, task_id, aid)
        if not current or current["sha256"] != digest:
            return False
    receipt = {
        k: prepared[k]
        for k in ("contract_event_id", "kind", "artifact_hashes", "remote", "criteria")
    }
    receipt["scope"] = (
        "Declared acceptance evidence and current remote delivery; no broad issue closure or deployment claim"
    )
    kb._append_event(
        conn,
        task_id,
        "completion_evidence_accepted",
        receipt,
        run_id=prepared["snapshot"][1],
    )
    return True
