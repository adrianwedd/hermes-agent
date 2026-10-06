"""Stable operator handoff for declared completion evidence; never infers scope."""
import json

def operator_contract_pending(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract
    _,contract=contract_record(conn,task_id)
    try:
        validate_contract(contract)
        return False
    except ValueError:
        pass
    row=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completion_evidence_rejected' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
    if not row:
        return False
    try:
        missing=json.loads(row[0]or'{}').get('missing',[])
    except (ValueError,AttributeError):
        return False
    return any(isinstance(x,str)and x.startswith('Operator must declare')for x in missing)

def route_completion_block(conn, task_id, kind, reason, source_status, **kwargs):
    from hermes_cli import kanban_db as kb
    result=kb._route_block(kind,reason,source_status,**kwargs)
    if kind=='needs_input'and operator_contract_pending(conn,task_id):
        _,_,sql,params,payload=result
        payload.update(operator_action_required=True,automatic_retry=False)
        return 'blocked','blocked',sql,params,payload
    return result

def require_declared_contract(conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract,_refuse
    _,contract=contract_record(conn,task_id)
    try:
        validate_contract(contract)
    except ValueError as exc:
        _refuse(conn,task_id,['Operator must declare the card’s explicit completion requirements: '+str(exc)+'. Worker cannot repair this by rewriting the body; preserve artifacts, request review or block needs_input once. No synthesis/test/model rerun.'])

def append_completion_context(lines, conn, task_id):
    from hermes_cli.kanban_completion_evidence import contract_record,validate_contract
    cid,contract=contract_record(conn,task_id)
    lines.extend(['','## Native completion requirements'])
    try:
        validate_contract(contract)
    except ValueError:
        lines.append('Operator declaration missing/invalid. Preserve deliverables and request review or block needs_input once; workers cannot declare this contract. Do not repeat completed work or inference to repair metadata.')
        return
    lines.append('contract_event_id='+str(cid)+'; operator contract='+json.dumps(contract,sort_keys=True))
    lines.append('Attach completion-evidence.json binding task_id and this exact contract_event_id, hashed native artifact attachment IDs and per-criterion acceptance. Supply metadata.completion_evidence_attachment_id to kanban_complete. Requirements and publication/review gates remain mandatory; requesting review does not require terminal evidence.')
