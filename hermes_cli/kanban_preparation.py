"""Bounded TODO qualification from an existing operator preparation grant.

The controller does not invent scope or acceptance, lift manual holds, infer
approval from prose, call models, or launch workers. Managed claims retain the
existing inclusive host admission budget after a genuinely ready promotion.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sqlite3
import re
import tempfile
from pathlib import Path

_FIELDS = ('id,title,body,status,assignee,workspace_kind,workspace_path,project_id,'
           'provider_override,model_override,dispatch_eligible,current_run_id,worker_pid,claim_lock')


def scope_snapshot(conn, task_id):
    row = conn.execute(f'SELECT {_FIELDS} FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return None
    fields = dict(zip(_FIELDS.split(','), row))
    # Claims and parent completion can change while an authorised preparation
    # waits. Scope/assignment/workspace edits and manual comments invalidate it.
    bound = {k: v for k, v in fields.items() if k not in {
        'status', 'dispatch_eligible', 'current_run_id', 'worker_pid', 'claim_lock'}}
    bound['parents'] = sorted(r[0] for r in conn.execute(
        'SELECT parent_id FROM task_links WHERE child_id=?', (task_id,)))
    bound['manual_comment'] = conn.execute(
        'SELECT max(id) FROM task_comments WHERE task_id=?', (task_id,)).fetchone()[0]
    bound['last_edit'] = conn.execute(
        "SELECT max(id) FROM task_events WHERE task_id=? AND kind IN "
        "('edited','status','blocked','unblocked','reassigned','workspace_changed')", (task_id,)).fetchone()[0]
    fields['scope_digest'] = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    return fields


def workspace_busy(conn, task_id, path):
    """Conservative cross-board/custom-DB ownership, including retained PIDs."""
    from hermes_cli.kanban_db_workspace import _sibling_board_db_files
    from hermes_cli.kanban_db_dispatch import _worker_alive
    target = Path(path).expanduser().resolve()
    def overlaps(value):
        if not value:
            return False
        other = Path(value).expanduser().resolve()
        return target == other or target in other.parents or other in target.parents
    def occupied(database, current):
        for row in database.execute("SELECT id,workspace_path FROM tasks WHERE status NOT IN ('done','archived')"):
            if (not current or row[0] != task_id) and overlaps(row[1]):
                return True
        for row in database.execute("SELECT t.workspace_path,r.worker_pid,r.worker_started_at FROM task_runs r "
                                    "JOIN tasks t ON t.id=r.task_id WHERE r.worker_pid IS NOT NULL"):
            if overlaps(row[0]) and _worker_alive(row[1], row[2]):
                return True
        return False
    try:
        if occupied(conn, True):
            return True
        for file in _sibling_board_db_files(conn):
            with sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=1) as other:
                if occupied(other, False):
                    return True
    except (OSError, sqlite3.Error):
        return True  # Unknown board ownership is never an isolation certificate.
    return False


def runtime_facts(conn, task, grant):
    from hermes_cli import kanban_db as kb, kanban_provider_admission as admission
    from hermes_cli import projects_db
    from hermes_cli.profiles import profile_exists
    facts = {'project_exists': False, 'profile_exists': False, 'lane': 'unknown',
             'workspace_exists': False, 'source_matches': False, 'workspace_busy': False}
    project_id = grant.get('project_id')
    if project_id:
        with projects_db.connect_closing() as pc:
            facts['project_exists'] = projects_db.get_project(pc, project_id) is not None
    facts['profile_exists'] = bool(task.get('assignee') and profile_exists(task['assignee']))
    if facts['profile_exists']:
        facts['lane'] = admission.task_lane(kb.get_task(conn, task['id']))
    path = Path(str(grant.get('workspace_path') or '')).expanduser()
    facts['workspace_exists'] = path.is_absolute() and path.is_dir()
    if facts['workspace_exists']:
        try:
            rev = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                                          text=True, encoding='utf-8', errors='replace', timeout=5,
                                          stderr=subprocess.DEVNULL).strip()
            dirty = subprocess.check_output(['git', '-C', str(path), '-c', 'core.fsmonitor=false', 'status', '--porcelain',
                                            '--untracked-files=all'], text=True, encoding='utf-8',
                                           errors='replace', timeout=5).strip()
            toplevel = subprocess.check_output(
                ['git', '-C', str(path), 'rev-parse', '--show-toplevel'], text=True,
                encoding='utf-8', errors='replace', timeout=5,
            ).strip()
            facts['source_matches'] = rev == grant.get('source_revision') and not dirty and path.resolve() == Path(toplevel).resolve()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        target = path.resolve()
        installed = Path(__file__).resolve().parents[1]
        if target == installed or installed in target.parents or target in installed.parents:
            facts['source_matches'] = False
        facts['workspace_busy'] = workspace_busy(conn, task['id'], target)
    return facts


def grant_error(grant):
    if not isinstance(grant, dict):
        return 'operator_preparation_grant_required'
    for key in ('project_id', 'workspace_path', 'scope_digest', 'source_revision'):
        value = grant.get(key)
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            return 'malformed_preparation_grant'
    if not re.fullmatch(r'[0-9a-f]{40}', grant['source_revision']):
        return 'malformed_preparation_grant'
    if not re.fullmatch(r'[0-9a-f]{64}', grant['scope_digest']):
        return 'malformed_preparation_grant'
    return ''


def provision_authorised_workspace(grant):
    """Create the exact isolated checkout already authorised by the grant.

    No inference is permitted: remote, immutable revision, destination and the
    explicit provisioning grant must all be present. Existing paths are never
    overwritten or repaired here.
    """
    if not isinstance(grant, dict) or grant.get('provision_missing_workspace') is not True:
        return False, 'workspace_provision_not_authorised'
    remote = grant.get('canonical_remote')
    revision = grant.get('source_revision')
    raw_path = grant.get('workspace_path')
    if not all(isinstance(value, str) and value.strip() for value in (remote, revision, raw_path)):
        return False, 'workspace_provision_identity_incomplete'
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or path.exists():
        return False, 'workspace_destination_not_new'
    if grant.get('isolated_workspace') is not True:
        return False, 'isolated_workspace_required'
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix='.hermes-preparation-', dir=path.parent))
    checkout = staging_root / 'checkout'
    try:
        subprocess.run(
            ['git', 'clone', '--no-checkout', '--', remote, str(checkout)],
            check=True, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace',
        )
        subprocess.run(
            ['git', '-C', str(checkout), 'checkout', '--detach', revision],
            check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace',
        )
        head = subprocess.check_output(
            ['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True,
            encoding='utf-8', errors='replace', timeout=10,
        ).strip()
        origin = subprocess.check_output(
            ['git', '-C', str(checkout), 'remote', 'get-url', 'origin'], text=True,
            encoding='utf-8', errors='replace', timeout=10,
        ).strip()
        if head != revision or origin != remote:
            return False, 'provisioned_workspace_identity_mismatch'
        checkout.rename(path)
    except (OSError, subprocess.SubprocessError):
        return False, 'workspace_provision_failed'
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return True, ''


def refusal(conn, task, contract, grant, facts):
    from hermes_cli.kanban_administrative_hold import administrative_pending
    if task['status'] != 'todo':
        return 'stage_not_todo'
    if any(task[k] is not None for k in ('current_run_id', 'worker_pid', 'claim_lock')):
        return 'active_owner'
    from hermes_cli.kanban_completion_workflow import authority_wait
    if authority_wait(conn, task['id']) is not None:
        return 'waiting_for_authority'
    if administrative_pending(conn, task['id']):
        return 'administrative_stop'
    if not isinstance(grant, dict) or grant.get('mode') != 'automatic' or grant.get('grant_qualification') is not True:
        return 'operator_preparation_grant_required'
    if grant.get('scope_digest') != task['scope_digest']:
        return 'scope_or_manual_hold_changed'
    if grant.get('state', 'pending') != 'pending':
        return 'preparation_already_consumed'
    if contract.get('qualified_for_dispatch') is not False:
        return 'explicit_pending_qualification_required'
    if task['project_id'] not in (None, grant.get('project_id')):
        return 'project_binding_changed'
    if not facts['project_exists']:
        return 'project_unresolved'
    if not facts['profile_exists']:
        return 'profile_unresolved'
    if facts['lane'] != 'cloud':
        return 'cloud_route_unqualified'
    if not facts['workspace_exists'] or not facts['source_matches']:
        return 'isolated_source_workspace_unqualified'
    if facts['workspace_busy']:
        return 'workspace_owner_conflict'
    if grant.get('isolated_workspace') is not True:
        return 'isolated_workspace_required'
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                    "WHERE l.child_id=? AND p.status NOT IN ('done','archived') LIMIT 1",
                    (task['id'],)).fetchone():
        return 'dependency_wait'
    return ''


def _prepare_task_locked(conn, task_id, *, facts_loader=runtime_facts):
    """One atomic qualification; its current operator scope stays unchanged."""
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise PermissionError('Workers cannot apply operator preparation grants')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_completion_evidence import contract_record, validate_contract
    task = scope_snapshot(conn, task_id)
    if task is None:
        return {'task_id': task_id, 'prepared': False, 'reason': 'unknown_task'}
    cid, contract = contract_record(conn, task_id)
    try:
        validate_contract(contract)
    except ValueError:
        return {'task_id': task_id, 'prepared': False, 'reason': 'operator_scope_contract_required'}
    grant = contract.get('preparation')
    if not isinstance(grant, dict):
        return {'task_id': task_id, 'prepared': False, 'reason': 'operator_preparation_grant_required'}
    error = grant_error(grant)
    if error:
        return {'task_id': task_id, 'prepared': False, 'reason': error, 'contract_event': cid}
    empty_facts = dict(project_exists=False, profile_exists=False, lane='unknown',
                       workspace_exists=False, source_matches=False, workspace_busy=False)
    preflight = refusal(conn, task, contract, grant, empty_facts)
    if preflight not in {'project_unresolved', 'profile_unresolved',
                         'isolated_source_workspace_unqualified'}:
        return {
            'task_id': task_id, 'prepared': False, 'reason': preflight,
            'contract_event': cid, 'scope_digest': task['scope_digest'],
        }
    facts = facts_loader(conn, task, grant)
    provisioned = False
    if not facts['workspace_exists'] and grant.get('provision_missing_workspace') is True:
        provisioned, provision_reason = provision_authorised_workspace(grant)
        if not provisioned:
            return {
                'task_id': task_id, 'prepared': False, 'reason': provision_reason,
                'contract_event': cid, 'scope_digest': task['scope_digest'],
            }
        facts = facts_loader(conn, task, grant)
    reason = refusal(conn, task, contract, grant, facts)
    outcome = {'task_id': task_id, 'prepared': False, 'reason': reason, 'contract_event': cid,
               'scope_digest': task['scope_digest']}
    if reason:
        payload = {'contract_event': cid, 'scope_digest': task['scope_digest'], 'reason': reason}
        encoded = json.dumps(payload, sort_keys=True)
        with kbc.write_txn(conn):
            if scope_snapshot(conn, task_id) == task and contract_record(conn, task_id)[0] == cid:
                last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='preparation_refused' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                if last is None or json.dumps(json.loads(last[0]), sort_keys=True) != encoded:
                    kb._append_event(conn, task_id, 'preparation_refused', payload)
        return outcome
    with kbc.write_txn(conn):
        current = scope_snapshot(conn, task_id)
        current_cid, current_contract = contract_record(conn, task_id)
        if current != task or current_cid != cid or current_contract != contract:
            outcome['reason'] = 'snapshot_changed'
            return outcome
        # External workspace/profile facts are also rechecked at the boundary.
        reason = refusal(conn, current, contract, grant, facts_loader(conn, current, grant))
        if reason:
            outcome['reason'] = reason
            return outcome
        qualified = dict(contract)
        qualified['qualified_for_dispatch'] = True
        qualified['preparation'] = dict(grant, state='qualified', source_contract_event=cid)
        kb._append_event(conn, task_id, 'completion_requirements', qualified)
        qualified_cid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.execute("UPDATE tasks SET dispatch_eligible=1,workspace_kind='dir',workspace_path=?,project_id=? "
                     "WHERE id=? AND status='todo' AND current_run_id IS NULL AND worker_pid IS NULL AND claim_lock IS NULL",
                     (grant['workspace_path'], grant['project_id'], task_id))
        kb._append_event(conn, task_id, 'preparation_qualified', {
            'source_contract_event': cid, 'qualified_contract_event': qualified_cid,
            'scope_digest': task['scope_digest'], 'lane': 'cloud', 'models_called': 0,
            'owner': 'bounded_existing_controller', 'workspace_path': grant['workspace_path'],
            'workspace_provisioned': provisioned,
            'source_revision': grant['source_revision'],
            'canonical_remote': grant.get('canonical_remote'),
        })
        outcome.update(prepared=True, reason='', qualified_contract_event=qualified_cid)
    # The shared native readiness path, not a second scheduler, owns promotion.
    kb.recompute_ready(conn)
    outcome['status'] = kb.get_task(conn, task_id).status
    kb.notify_task_updated(conn, task_id, ['completion_requirements', 'dispatch_eligible', 'workspace_path'])
    return outcome


def prepare_task(conn, task_id, *, facts_loader=runtime_facts):
    # Match managed_claim's lock order: host admission lock before DB writes.
    from hermes_cli import kanban_db as kb, kanban_provider_admission as admission
    with admission.host_lock(kb.kanban_home()) as acquired:
        if not acquired:
            return {'task_id': task_id, 'prepared': False, 'reason': 'provider_admission_busy'}
        outcome = _prepare_task_locked(conn, task_id, facts_loader=facts_loader)
        if not outcome['prepared'] and outcome['reason'] != 'unknown_task':
            from hermes_cli import kanban_db_connect as kbc
            payload = {k: v for k, v in outcome.items() if k not in {'prepared', 'task_id'}}
            with kbc.write_txn(conn):
                last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='preparation_refused' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                if last is None or json.loads(last[0]) != payload:
                    kb._append_event(conn, task_id, 'preparation_refused', payload)
        return outcome


def prepare_tick(conn, *, limit=1, scan_limit=64, facts_loader=runtime_facts):
    """Bounded existing TODO scan; no fanout, model call, or worker launch."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    results = []
    prepared = 0
    rows = conn.execute("SELECT t.id FROM tasks t JOIN task_events e ON e.id=(SELECT max(id) "
                        "FROM task_events WHERE task_id=t.id AND kind='completion_requirements') "
                        "WHERE t.status='todo' AND json_valid(e.payload) "
                        "AND json_extract(e.payload,'$.preparation.mode')='automatic' "
                        "ORDER BY coalesce((SELECT max(id) FROM task_events WHERE task_id=t.id "
                        "AND kind='preparation_checked'),0),t.priority DESC,t.created_at,t.id LIMIT ?",
                        (max(1, min(int(scan_limit), 128)),)).fetchall()
    for row in rows:
        from hermes_cli.kanban_decision import decide
        current_task = kb.get_task(conn, row[0])
        current_decision = decide(conn, current_task) if current_task else None
        latest = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='preparation_checked' "
            "ORDER BY id DESC LIMIT 1", (row[0],),
        ).fetchone()
        try:
            latest_payload = json.loads(latest[0] or '{}') if latest else {}
        except (TypeError, ValueError):
            latest_payload = {}
        if (current_decision and latest_payload.get('decision_fingerprint') ==
                current_decision['decision_fingerprint'] and
                latest_payload.get('prepared') is False):
            # Event-driven reassessment: an unchanged refusal is not useful
            # work and must not produce one event/model/worker cycle per tick.
            continue
        result = prepare_task(conn, row[0], facts_loader=facts_loader)
        results.append(result)
        with kbc.write_txn(conn):
            refreshed = kb.get_task(conn, row[0])
            refreshed_decision = decide(conn, refreshed) if refreshed else current_decision
            kb._append_event(conn, row[0], 'preparation_checked', {
                'reason': result['reason'], 'prepared': bool(result['prepared']),
                'decision_fingerprint': refreshed_decision['decision_fingerprint']
                if refreshed_decision else None,
            })
        prepared += int(result['prepared'])
        if prepared >= max(1, min(int(limit), 1)):
            break
    return {'prepared': prepared, 'checked': len(results), 'results': results,
            'models_called': 0, 'workers_launched': 0}
