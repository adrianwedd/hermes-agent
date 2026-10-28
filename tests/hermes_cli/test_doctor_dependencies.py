"""Python audit identifies installed/source versions without mutation and fails closed."""
from contextlib import contextmanager
import json

import pytest

from hermes_cli import doctor_dependencies as dependencies


@pytest.mark.parametrize('results, security, unverified', [
    ([{}], False, False),
    ([{'vulns': [{'id': 'GHSA-test'}]}], True, False),
    ([], False, True),
])
def test_osv_results_preserve_evidence_states(results, security, unverified, monkeypatch):
    import io
    monkeypatch.setattr(dependencies, '_python_inventory', lambda root: [(('example', '1.0'), {'source lock'})])
    @contextmanager
    def response(request, timeout):
        assert request.full_url == 'https://api.osv.dev/v1/querybatch'
        assert json.loads(request.data)['queries'][0]['version'] == '1.0'
        yield io.StringIO(json.dumps({'results': results}))
    monkeypatch.setattr(dependencies.urllib.request, 'urlopen', response)
    finding = dependencies._check_python_dependencies(False)
    assert bool(finding.security) is security
    assert bool(finding.unverified) is unverified
    assert not finding.issues


def test_osv_unreachable_is_not_a_clean_audit(monkeypatch, capsys):
    monkeypatch.setattr(dependencies, '_python_inventory', lambda root: [(('example', '1.0'), {'running environment'})])
    def refuse(*args, **kwargs):
        raise OSError('offline')
    monkeypatch.setattr(dependencies.urllib.request, 'urlopen', refuse)
    finding = dependencies._check_python_dependencies(False)
    assert finding.unverified and not finding.security
    assert 'no known vulnerabilities' not in capsys.readouterr().out


def test_inventory_preserves_import_precedence_and_excludes_application(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "hermes-agent"\n', encoding='utf-8')
    def dist(name, version):
        return SimpleNamespace(metadata={'Name': name}, version=version)
    monkeypatch.setattr(dependencies.importlib.metadata, 'distributions', lambda: [
        dist('hermes-agent', '0.0.0'), dist('example', '2.0'), dist('example', '1.0')])
    monkeypatch.setattr(dependencies, '_gateway_dependency_paths', lambda root: [])
    assert dependencies._python_inventory(tmp_path) == [(('example', '2.0'), {'running environment'})]


@pytest.mark.parametrize('leased', [True, False])
def test_gateway_inventory_identifies_retained_environment_without_restart(tmp_path, monkeypatch, leased):
    from types import SimpleNamespace
    from hermes_cli.doctor_report import Finding, finding_scope
    project = tmp_path / 'project'
    old = tmp_path / 'environments' / 'old' / 'venv'
    new = tmp_path / 'environments' / 'new' / 'venv'
    opened = [SimpleNamespace(path=str(old.parent / '.leases' / 'lease'))] if leased else []
    monkeypatch.setattr('gateway.host_topology.host_gateway_topology', lambda: SimpleNamespace(pid=123))
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(open_files=lambda: opened))
    monkeypatch.setattr('pm.environments.committed_venv', lambda root: new)
    monkeypatch.setattr('pm.environments.site_packages', lambda env: env / 'site')
    finding = Finding()
    with finding_scope(finding):
        paths = dependencies._gateway_dependency_paths(project)
    assert bool(paths) is leased
    assert bool(finding.warnings) is leased
    assert bool(finding.unverified) is not leased


def test_remedy_distinguishes_clean_source_and_command_versions(monkeypatch, capsys):
    import io
    monkeypatch.setattr(dependencies, '_python_inventory', lambda root: [
        (('example', '1'), {'gateway PID 123'}),
        (('example', '2'), {'source lock'}),
        (('example', '3'), {'running environment'}),
    ])
    @contextmanager
    def response(*args, **kwargs):
        yield io.StringIO(json.dumps({'results': [{'vulns': [{'id': 'GHSA-test'}]}, {}, {}]}))
    monkeypatch.setattr(dependencies.urllib.request, 'urlopen', response)
    finding = dependencies._check_python_dependencies(False)
    assert len(finding.security) == 1
    assert 'command environment (3) and source lock (2) already audit clean' in finding.security[0]
    assert 'reload this gateway only in an authorised idle window' in finding.security[0]
