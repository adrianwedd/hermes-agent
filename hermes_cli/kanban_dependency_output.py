"""Evidence-scoped dependencies using the existing native requirement events."""
import hashlib
import json


def declare(conn, child_id, parent_id, requirement_id, *, evidence_identity, requirement):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_transitions import require_operator
    require_operator(conn)
    if not requirement_id or not evidence_identity or not requirement:
        raise ValueError('Requirement identity, accepted output identity and requirement are mandatory')
    with kb.write_txn(conn):
        if not conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?', (parent_id, child_id)).fetchone():
            raise ValueError('Output requirements must bind an existing dependency edge')
        payload = {'version': 1, 'parent_id': parent_id, 'requirement_id': requirement_id,
                   'evidence_identity': evidence_identity, 'requirement': requirement, 'resolver': 'operator'}
        payload['declaration'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        kb._append_event(conn, child_id, 'dependency_requirement', payload)
    kb.recompute_ready(conn)
    return payload


def accept(conn, child_id, requirement_id, *, declaration_event_id, producer_evidence_event_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_transitions import require_operator
    require_operator(conn)
    with kb.write_txn(conn):
        row = conn.execute("SELECT payload FROM task_events WHERE id=? AND task_id=? AND kind='dependency_requirement'", (declaration_event_id, child_id)).fetchone()
        if not row:
            raise ValueError('Wrong requirement declaration')
        declared = json.loads(row[0])
        latest = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='dependency_requirement' AND json_extract(payload,'$.parent_id')=?", (child_id, declared['parent_id'])).fetchone()[0]
        if latest != declaration_event_id or declared['requirement_id'] != requirement_id:
            raise ValueError('Stale or wrong requirement identity')
        output = conn.execute("SELECT kind,payload FROM task_events WHERE id=? AND task_id=?", (producer_evidence_event_id, declared['parent_id'])).fetchone()
        if not output or output[0] not in {'completion_evidence_accepted', 'review_approved', 'output_evidence_accepted'}:
            raise ValueError('Evidence must be accepted on the declared producer')
        evidence = json.loads(output[1])
        if not output_provenance(conn, declared['parent_id'], output[0], evidence, declared['evidence_identity']):
            raise ValueError('Producer evidence does not satisfy the declared output identity')
        payload = {'parent_id': declared['parent_id'], 'requirement_id': requirement_id,
                   'evidence_identity': declared['evidence_identity'], 'declaration_event_id': declaration_event_id,
                   'producer_evidence_event_id': producer_evidence_event_id, 'authorised': True, 'actor': 'operator'}
        exists = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='dependency_requirement_satisfied' AND payload=?", (child_id, json.dumps(payload, ensure_ascii=False))).fetchone()
        if not exists:
            kb._append_event(conn, child_id, 'dependency_requirement_satisfied', payload)
    kb.recompute_ready(conn)
    return payload


def accepted(conn, child_id, declared_id, declared, accepted_payload):
    """Validate all bindings at read time, including recovery from missed events."""
    if not declared_id or not declared.get('evidence_identity'):
        return False
    if (accepted_payload.get('authorised') is not True or accepted_payload.get('actor') != 'operator'
            or accepted_payload.get('declaration_event_id') != declared_id
            or accepted_payload.get('parent_id') != declared['parent_id']
            or accepted_payload.get('evidence_identity') != declared['evidence_identity']):
        return False
    row = conn.execute('SELECT kind,payload FROM task_events WHERE id=? AND task_id=?', (accepted_payload.get('producer_evidence_event_id'), declared['parent_id'])).fetchone()
    return bool(row and row[0] in {'completion_evidence_accepted', 'review_approved', 'output_evidence_accepted'}
                and output_provenance(conn, declared['parent_id'], row[0], json.loads(row[1]), declared['evidence_identity']))


def certify_output(conn, producer_id, *, contract_event_id, artifact_attachment_id, criterion_id, evidence):
    """Accept one declared result without accepting unrelated producer work."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wait_transitions import require_operator
    from hermes_cli.kanban_completion_evidence import contract_record, _attachment
    require_operator(conn)
    if not isinstance(evidence, dict) or not evidence.get('reference') or not all((evidence.get('assertions') or {}).get(key) is True for key in ['output_verified', 'applicability_verified', 'authority_verified']):
        raise ValueError('Output acceptance needs named qualified evidence assertions')
    with kb.write_txn(conn):
        cid, contract = contract_record(conn, producer_id)
        if cid != contract_event_id or criterion_id not in (contract or {}).get('criteria', []):
            raise ValueError('Output must bind a current original criterion')
        artifact = _attachment(conn, producer_id, artifact_attachment_id)
        if not artifact:
            raise ValueError('Output must bind a retained, hashed native producer artifact')
        payload = {'authorised': True, 'actor': 'operator', 'contract_event_id': cid,
                   'criterion_id': criterion_id, 'artifact_attachment_id': artifact_attachment_id,
                   'evidence_identity': 'sha256:'+artifact['sha256'], 'evidence': evidence}
        kb._append_event(conn, producer_id, 'output_evidence_accepted', payload)
        event_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    kb.recompute_ready(conn)
    return {'producer_evidence_event_id': event_id, **payload}


def output_matches(kind, evidence, identity):
    if kind == 'completion_evidence_accepted':
        return any('sha256:'+digest == identity for _, digest in evidence.get('artifact_hashes', []))
    return bool(kind == 'output_evidence_accepted' and evidence.get('authorised') is True
                and evidence.get('actor') == 'operator' and evidence.get('evidence_identity') == identity)


def output_provenance(conn, producer_id, kind, evidence, identity):
    from hermes_cli.kanban_completion_evidence import _attachment
    if not output_matches(kind, evidence, identity):
        return False
    if kind == 'output_evidence_accepted':
        artifact = _attachment(conn, producer_id, evidence.get('artifact_attachment_id'))
        return bool(artifact and 'sha256:'+artifact['sha256'] == identity)
    return any(artifact and 'sha256:'+artifact['sha256'] == identity
               for aid, _ in evidence.get('artifact_hashes', [])
               for artifact in [_attachment(conn, producer_id, aid)])
