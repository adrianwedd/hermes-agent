"""Conservative provider lanes for managed Kanban workers; no inference or credentials."""
import contextlib
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

AUX_TASKS = frozenset(('vision compression skills_hub approval review mcp title_generation memory_query_rewrite tts_audio_tags triage_specifier kanban_decomposer profile_describer goal_judge curator monitor background_review moa_reference moa_aggregator').split())

def remote_route(route):
    if not isinstance(route, dict):
        return False
    if any(route.get(k) for k in ('fallback', 'fallback_model', 'fallback_models', 'fallback_providers', 'fallback_chain')):
        return False
    provider = str(route.get('provider') or '').strip().lower()
    canonical = {'openai-codex': 'https://chatgpt.com/backend-api/codex', 'ollama-cloud': 'https://ollama.com/v1'}
    if provider not in canonical:
        return False
    from agent.secret_scope import get_secret
    # Only endpoint values are read; never invoke auth/token resolution.
    override = get_secret('HERMES_CODEX_BASE_URL' if provider == 'openai-codex' else 'OLLAMA_BASE_URL')
    # Codex runtime endpoint override precedes model.base_url. Conservatively
    # apply the same veto for an Ollama endpoint even with an explicit URL.
    expected_host = 'chatgpt.com' if provider == 'openai-codex' else 'ollama.com'
    def canonical_remote(value):
        parsed = urlparse(str(value))
        return parsed.scheme == 'https' and (parsed.hostname or '').lower() == expected_host
    if provider == 'ollama-cloud':
        # Native matched model URL precedes env; both possible sources must
        # remain remote before admission may certify the cloud lane.
        if route.get('base_url') and not canonical_remote(route['base_url']):
            return False
        if override and not canonical_remote(override):
            return False
    return canonical_remote(override or route.get('base_url') or canonical[provider])

def _cloud_moa(config, primary):
    # Exact named preset, matching runtime launch selection; never normalize
    # malformed slots into implicit providers/models.
    moa = config.get('moa') or {}
    presets = moa.get('presets') or {}
    name = str(primary.get('default') or '').strip()
    if name not in presets:
        name = str(moa.get('default_preset') or 'default').strip()
    preset = presets.get(name)
    if not isinstance(preset, dict):
        return False
    def slot_remote(slot):
        if not isinstance(slot, dict) or not slot.get('model'):
            return False
        route = dict(slot)
        return remote_route(route)
    refs = preset.get('reference_models')
    if not isinstance(refs, list) or not refs:
        return False
    if any(not isinstance(slot, dict) or type(slot.get('enabled', True)) is not bool for slot in refs):
        return False
    return (slot_remote(preset.get('aggregator')) and
            any(slot.get('enabled', True) for slot in refs) and
            all(slot_remote(slot) for slot in refs if slot.get('enabled', True)))

def classify(config, provider_override=None, model_override=None, *, override_base_url=None):
    """Cloud only when primary AND every auxiliary route are explicit remote routes."""
    if not isinstance(config, dict):
        return 'local'
    primary = dict(config.get('model') or {})
    if provider_override:
        # Worker argv supplies --provider with -m, not --base-url. Runtime
        # ignores model.base_url when its configured provider differs.
        # Only OllamaCloud's credential-free endpoint rule is mirrored here;
        # unknown/other changed providers remain conservatively local.
        requested = str(provider_override).strip().lower()
        configured = str(primary.get('provider') or '').strip().lower()
        # Runtime treats this alias as the same provider and retains its URL.
        # Leave alias spellings conservative rather than drop a local endpoint.
        if configured == 'ollama_cloud':
            return 'local'
        if requested != configured:
            if requested != 'ollama-cloud' or not model_override:
                return 'local'
            if not isinstance(model_override, str) or not model_override.strip() or ':' in model_override:
                return 'local'
            declared = (config.get('providers') or {}).get(requested)
            if declared and (not isinstance(declared, dict) or set(declared) - {'enabled'}):
                return 'local'
            if any(isinstance(p, dict) and p.get('name') == requested
                   for p in (config.get('custom_providers') or [])):
                return 'local'
            primary['base_url'] = str(override_base_url or '').strip() or 'https://ollama.com/v1'
        primary['provider'] = requested
    if model_override:
        primary['default'] = model_override
        # CLI's moa:<preset> model prefix wins over --provider.
        if str(model_override).strip().lower().startswith('moa:'):
            return 'local'
    if not (_cloud_moa(config, primary) if primary.get('provider') == 'moa' else remote_route(primary)):
        return 'local'
    if config.get('fallback_models') or config.get('fallback_model') or config.get('fallback'):
        return 'local'
    fallbacks = config.get('fallback_providers') or []
    if not isinstance(fallbacks, list):
        return 'local'
    for slot in fallbacks:
        if not isinstance(slot, dict) or not slot.get('model'):
            return 'local'
        route = dict(slot)
        if route.get('provider') == 'moa':
            if not _cloud_moa(config, {'default': route.get('model')}):
                return 'local'
        elif not remote_route(route):
            return 'local'
    # MoA is selected by the effective primary provider, not by a stored preset's
    # enabled flag. A direct remote primary never installs the MoA facade.
    aux = config.get('auxiliary') or {}
    if not AUX_TASKS.issubset(aux) or any(not remote_route(v) for v in aux.values()):
        return 'local'
    return 'cloud'

def read_raw(path):
    from utils import fast_safe_load
    with Path(path).open(encoding='utf-8-sig') as f:
        value = fast_safe_load(f)
    if not isinstance(value, dict):
        raise ValueError('configuration must be a mapping')
    return value

def policy(home):
    config_path = Path(home) / 'config.yaml'
    if not config_path.exists():
        return None
    value = (read_raw(config_path).get('kanban') or {}).get('provider_admission')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'local', 'cloud'} or
            type(value.get('local')) is not int or not 0 <= value['local'] <= 32 or
            type(value.get('cloud')) is not int or not 1 <= value['cloud'] <= 32):
        raise ValueError('provider_admission requires local 0..32 and cloud 1..32 integers')
    return value

@contextlib.contextmanager
def host_lock(home):
    """Cross-profile/board single writer; failures refuse admission, never fail open."""
    path = Path(home) / 'kanban' / '.provider-admission.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    from gateway.status import _try_acquire_file_lock, _release_file_lock
    with path.open('a+', encoding='utf-8') as f:
        if not _try_acquire_file_lock(f):
            yield False
            return
        try:
            yield True
        finally:
            _release_file_lock(f)

def _live_pid(pid, started_at):
    from hermes_cli.kanban_db_dispatch import _worker_alive
    return _worker_alive(pid, started_at)


def _stored_lane(c, run_id, metadata):
    import json
    # Terminal completion can replace metadata, so the append-only admission event is authoritative.
    event = c.execute("SELECT payload FROM task_events WHERE kind='provider_admission' AND run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    try:
        payload = json.loads(event[0] if event else metadata or '{}')
        lane = payload.get('lane' if event else 'provider_admission_lane') if isinstance(payload, dict) else None
    except (ValueError, TypeError):
        lane = None
    return lane if isinstance(lane, str) and lane in {'local', 'cloud'} else 'local'


def count_lanes(paths, config_for=None):
    """Immutable launch lanes; legacy/unknown claims count local, live retained workers included."""
    import json
    counts = {'local': 0, 'cloud': 0}
    for path in {str(Path(p).resolve()) for p in paths}:
        with contextlib.closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as c:
            seen = set()
            rows = c.execute("SELECT t.current_run_id, r.metadata FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id WHERE t.status='running' OR (t.status='review' AND t.worker_pid IS NOT NULL)")
            for run_id, metadata in rows:
                seen.add(run_id)
                counts[_stored_lane(c, run_id, metadata)] += 1
            for run_id, pid, metadata, started_at in c.execute("SELECT id,worker_pid,metadata,worker_started_at FROM task_runs WHERE worker_pid IS NOT NULL"):
                if run_id in seen or not _live_pid(pid, started_at):
                    continue
                counts[_stored_lane(c, run_id, metadata)] += 1
    return counts


def record_lane(kb, conn, task, lane):
    import json
    if lane not in {'local', 'cloud'}:
        raise ValueError('invalid admitted lane')
    with kb.write_txn(conn):
        row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        metadata = json.loads(row[0] or '{}')
        metadata['provider_admission_lane'] = lane
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), task.current_run_id))
        kb._append_event(conn, task.id, 'provider_admission', {'lane': lane, 'run_id': task.current_run_id}, run_id=task.current_run_id)


def task_lane(task):
    from hermes_cli.profiles import resolve_profile_env
    try:
        home = Path(resolve_profile_env(task.assignee or 'default'))
        config = read_raw(home / 'config.yaml')
        endpoint = None
        if str(task.provider_override or '').strip().lower() == 'ollama-cloud' and task.model_override:
            # Same assigned-profile scope used when constructing worker env;
            # inspect only the nonsecret endpoint, never resolve credentials.
            from hermes_cli.kanban_db_dispatch import _worker_profile_scope
            from agent.secret_scope import get_secret
            with _worker_profile_scope(str(home)):
                endpoint = get_secret('OLLAMA_BASE_URL')
        from hermes_cli.kanban_db_dispatch import _worker_profile_scope
        with _worker_profile_scope(str(home)):
            return classify(config, task.provider_override, task.model_override, override_base_url=endpoint)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
        from hermes_cli import kanban_db as kb
        kb._log.exception('Cannot qualify assigned-profile provider lane; refusing local admission')
        return 'local'

def admit(kb, conn, task, lane=None):
    limits = policy(kb.kanban_home())
    if limits is None:
        return True
    paths = [kb.kanban_db_path(board=m.get('slug')) for m in kb.list_boards(include_archived=True)]
    # Explicitly pinned/custom DBs are part of the count even if not registered as a board.
    paths.extend(Path(row[2]) for row in conn.execute('PRAGMA database_list') if row[1] == 'main' and row[2])
    from hermes_cli.profiles import resolve_profile_env
    counts = count_lanes(paths, lambda name: read_raw(Path(resolve_profile_env(name or 'default')) / 'config.yaml'))
    lane = lane or task_lane(task)
    return counts[lane] < limits[lane]


def managed_claim(fn):
    """Serialize admission through commit across boards; unknown routes fail closed.

    No route inference from current configuration is written to historical runs.
    """
    from functools import wraps
    @wraps(fn)
    def guarded(conn, task_id, **kwargs):
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, task_id)
        if task is None or not task.dispatch_eligible:
            return None
        limits = policy(kb.kanban_home())
        if limits is None:
            return fn(conn, task_id, **kwargs)
        with host_lock(kb.kanban_home()) as acquired:
            if not acquired:
                return None
            task = kb.get_task(conn, task_id)
            if task is None or not task.dispatch_eligible:
                return None
            snapshot = (task.assignee, task.provider_override, task.model_override, int(task.dispatch_eligible))
            lane = task_lane(task)
            paths = [kb.kanban_db_path(board=m.get('slug')) for m in kb.list_boards(include_archived=True)]
            paths.extend(Path(row[2]) for row in conn.execute('PRAGMA database_list') if row[1] == 'main' and row[2])
            counts = count_lanes(paths)
            # Legacy unknown claims reserve aggregate cloud capacity as well.
            if sum(counts.values()) >= sum(limits.values()) or counts[lane] >= limits[lane]:
                return None
            claimed = fn(conn, task_id, _expected_route=snapshot, **kwargs)
            if claimed is not None:
                record_lane(kb, conn, claimed, lane)
            return claimed
    return guarded
