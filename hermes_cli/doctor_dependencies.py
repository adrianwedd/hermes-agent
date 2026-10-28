"""Read-only OSV audit of the running Python environment and the source lock."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import tomllib
import urllib.request

from hermes_cli.doctor_report import Finding, check_fail, check_info, check_ok, check_unverified, check_warn, doctor_check, warn_on_error


def _gateway_dependency_paths(project_root):
    """Resolve an active gateway's leased PM environment without interrupting it."""
    import psutil
    from gateway.host_topology import host_gateway_topology
    from pm.environments import committed_venv, site_packages

    topology = host_gateway_topology()
    if topology is None:
        return []
    environments = set()
    for opened in psutil.Process(topology.pid).open_files():
        path = Path(opened.path)
        if path.parent.name == '.leases' and path.parent.parent.parent.name == 'environments':
            environments.add(path.parent.parent / 'venv')
    if not environments:
        check_unverified(f'Gateway PID {topology.pid} dependency environment', '(no readable PM environment lease)')
        return []
    selected = committed_venv(project_root)
    paths = []
    for environment in sorted(environments):
        if selected != environment:
            check_warn(f'Gateway PID {topology.pid} retains an older dependency environment',
                       f'({environment.parent.name}; differs from the selected environment; reload only in an authorised idle window)')
        paths.append((site_packages(environment), f'gateway PID {topology.pid} environment {environment.parent.name}'))
    return paths


def _python_inventory(project_root):
    inventory: dict[tuple[str, str], set[str]] = {}
    project_name = tomllib.loads((project_root / 'pyproject.toml').read_text(encoding='utf-8-sig'))['project']['name']
    seen = {project_name.lower().replace('_', '-')}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get('Name')
        normalized = name.lower().replace('_', '-') if name else ''
        if normalized and normalized not in seen and dist.version:
            seen.add(normalized)
            inventory.setdefault((normalized, dist.version), set()).add('running environment')
    with warn_on_error('Active gateway dependency inventory could not complete'):
        for site, scope in _gateway_dependency_paths(project_root):
            for dist in importlib.metadata.distributions(path=[str(site)]):
                name = dist.metadata.get('Name')
                normalized = name.lower().replace('_', '-') if name else ''
                if normalized and normalized != project_name.lower().replace('_', '-'):
                    inventory.setdefault((normalized, dist.version), set()).add(scope)
    lock = project_root / 'uv.lock'
    if lock.is_file():
        for package in tomllib.loads(lock.read_text(encoding='utf-8-sig'))['package']:
            if package.get('source', {}).get('registry'):
                inventory.setdefault((package['name'], package['version']), set()).add('source lock')
    return sorted(inventory.items())


@doctor_check('Python dependency audit could not complete', '{e}')
def _check_python_dependencies(should_fix: bool, finding: Finding) -> None:
    from hermes_cli.doctor import PROJECT_ROOT
    entries = _python_inventory(PROJECT_ROOT)
    if not entries:
        raise ValueError('No Python dependency inventory available')
    queries = [{'package': {'name': name, 'ecosystem': 'PyPI'}, 'version': version}
               for (name, version), _ in entries]
    request = urllib.request.Request(
        'https://api.osv.dev/v1/querybatch',
        data=json.dumps({'queries': queries}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        results = json.load(response)['results']
    if len(results) != len(entries):
        raise ValueError('OSV returned an incomplete dependency audit')
    vulnerable = 0
    for ((name, version), scopes), result in zip(entries, results):
        if not isinstance(result, dict):
            raise ValueError('OSV returned an invalid package result')
        advisories = result.get('vulns', [])
        if not isinstance(advisories, list):
            raise ValueError('OSV returned invalid advisory data')
        if not advisories:
            continue
        vulnerable += 1
        ids = ', '.join(item['id'] for item in advisories)
        scope = ', '.join(sorted(scopes))
        check_fail(f'{name} {version}', f'({scope}; {ids})')
        replacements = {scope: other_version for ((other_name, other_version), other_scopes), other_result
                        in zip(entries, results)
                        if other_name == name and isinstance(other_result, dict) and other_result.get('vulns', []) == []
                        for scope in other_scopes if scope in {'running environment', 'source lock'}}
        remedy = (f"The command environment ({replacements['running environment']}) and source lock "
                  f"({replacements['source lock']}) already audit clean; "
                  "reload this gateway only in an authorised idle window."
                  if {'running environment', 'source lock'} <= replacements.keys() else
                  'Verify affected APIs and patched versions, update pyproject.toml/uv.lock through Hermes PM, '
                  'then stage the corrected environment. Restart active holders only in an authorised idle window.')
        finding.security.append(f'Python {name} {version} ({scope}): {ids}. {remedy}')
    if not vulnerable:
        check_ok(f'Python dependencies ({len(entries)} package/version pairs)',
                 '(no known vulnerabilities in OSV for the command, inspected gateway environments and available source lock)')
    check_info('This audits dependencies; application advisories are reported under Security Advisories. '
               'Advisories identify affected package versions; exploitability depends on the APIs and platform used. '
               'Already-running gateway processes may retain an older environment.')
