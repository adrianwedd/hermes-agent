"""Completion contracts derived before dispatch from authoritative card scope."""
import hashlib
import json


def ensure_scope_contract(conn, task_id, *, authority='control_plane'):
    """Materialise a deterministic v1 contract from the already-authoritative card.

    This is bookkeeping, not a new acceptance decision. It is safe only while
    no worker owns the card and records the exact title/body hash used.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    cid, contract = contract_record(conn, task_id)
    try:
        validate_contract(contract)
        return cid, contract
    except ValueError:
        pass
    row = conn.execute(
        'SELECT title,body,status,current_run_id,worker_pid,claim_lock,created_at FROM tasks WHERE id=?',
        (task_id,),
    ).fetchone()
    if row is None:
        return None, None
    title = str(row[0] or '').strip()
    body = str(row[1] or '').strip()
    if not title:
        return None, None
    lowered = (title + '\n' + body[:1000]).lower()
    if 'audit' in lowered:
        kind = 'audit'
    elif 'review' in lowered or 'verify' in lowered:
        kind = 'review'
    elif 'research' in lowered or 'investigate' in lowered or 'recon' in lowered:
        kind = 'research'
    elif 'design' in lowered or 'define' in lowered or 'spec' in lowered:
        kind = 'spec'
    elif body:
        kind = 'implementation'
    else:
        # Legacy title-only cards predate explicit contracts. Their bounded
        # title plus an explicit completion result is authoritative enough to
        # repair bookkeeping without inventing implementation/review ceremony.
        kind = 'research'
    scope = title + (('\n\n' + body) if body else '')
    digest = hashlib.sha256(scope.encode('utf-8')).hexdigest()
    contract = {
        'version': 1,
        'kind': kind,
        'local_only': True,
        'require_review': kind in ('implementation', 'program') and bool(body),
        'require_commands': kind == 'implementation' and bool(body),
        'criteria': [
            *(['authoritative_scope_satisfied', 'required_verification_recorded',
               'retained_artifacts_identified', 'unresolved_acceptance_gaps_empty']
              if body else ['explicit_completion_result_recorded']),
        ],
        'scope': scope,
        'scope_sha256': digest,
        'scope_source': 'task_title_body_at_pre_dispatch_boundary',
        'task_created_at': row[6],
        'materialized_by': authority,
        'qualified_for_dispatch': True,
        'legacy_completion_repair': not bool(body),
    }
    kb._append_event(conn, task_id, 'completion_requirements', contract)
    new_cid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
    kb._append_event(conn, task_id, 'completion_contract_materialized', {
        'contract_event_id': new_cid, 'scope_sha256': digest,
        'authority': authority, 'post_hoc_acceptance_change': False,
    })
    return new_cid, contract


def authority_wait(conn, task_id):
    """Return the current contract's explicit authority boundary, if any."""
    from hermes_cli.kanban_completion_evidence import contract_record
    _, contract = contract_record(conn, task_id)
    action = contract.get('selected_next_action') if isinstance(contract, dict) else None
    if isinstance(action, dict) and action.get('phase') == 'WAITING_FOR_AUTHORITY':
        return action
    return None

def operator_contract_pending(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract
    _,contract=contract_record(conn,task_id)
    if authority_wait(conn, task_id) is not None:
        return True
    try:
        validate_contract(contract)
        return False
    except ValueError:
        pass
    # A valid operator declaration is the only release; later/corrupt rejection
    # payloads cannot erase a structural handoff. Legacy rejections are native
    # evidence of the same completion boundary, not proof of completed work.
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
        "('operator_contract_handoff','completion_evidence_rejected') LIMIT 1",
        (task_id,),
    ).fetchone() is not None

def route_completion_block(conn, task_id, kind, reason, source_status, **kwargs):
    from hermes_cli import kanban_db as kb
    result=kb._route_block(kind,reason,source_status,**kwargs)
    if kind=='needs_input'and operator_contract_pending(conn,task_id):
        _,_,sql,params,payload=result
        wait = authority_wait(conn, task_id)
        payload.update(operator_action_required=wait is None, automatic_retry=False)
        if wait is not None:
            payload.update(phase='WAITING_FOR_AUTHORITY', operator_decision_required=True, selected_next_action=wait)
        if source_status == 'review' and wait is None:
            payload.update(reason='Review/closure metadata remains to be reconciled; retained work stays in Review')
            return 'review','review_pending_operator','block_kind = NULL, block_recurrences = 0',(),payload
        return 'blocked','blocked',sql,params,payload
    return result

def require_declared_contract(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract,_refuse,_snapshot
    ensure_scope_contract(conn, task_id, authority='control_plane_completion_repair')
    snapshot = _snapshot(conn, task_id)
    _,contract=contract_record(conn,task_id)
    try:
        validate_contract(contract)
    except ValueError as exc:
        _refuse(conn,task_id,['Authoritative scope is genuinely ambiguous or missing: '+str(exc)+'. Return to TRIAGE/PREPARE for a substantive scope decision; do not create a receipt-only Blocked state.'], expected_snapshot=snapshot)

def append_completion_context(lines, conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract
    cid,contract=contract_record(conn,task_id)
    lines.extend(['','## Native completion requirements'])
    try:
        validate_contract(contract)
    except ValueError:
        lines.append('Completion contract missing/invalid. This is a pre-dispatch control-plane defect. Preserve deliverables; the control plane must materialise the contract from authoritative scope or return genuinely ambiguous scope to TRIAGE/PREPARE. Never create a receipt-only Blocked state.')
        return
    lines.append('contract_event_id='+str(cid)+'; operator contract='+json.dumps(contract,sort_keys=True))
    lines.append('Attach completion-evidence.json binding task_id and this exact contract_event_id, hashed native artifact attachment IDs and per-criterion acceptance. Supply metadata.completion_evidence_attachment_id to kanban_complete. Requirements and publication/review gates remain mandatory; requesting review does not require terminal evidence.')
