"""Doctor reports evidence gaps independently from optional capabilities and failures."""
from types import SimpleNamespace
import json
import subprocess
from unittest.mock import patch

import pytest

from hermes_cli import doctor, doctor_tools, doctor_state, doctor_platform
from hermes_cli.doctor_report import (
    Finding, check_unavailable, check_unverified, check_warn, doctor_check,
)


@pytest.mark.parametrize('kind, expected, state', [
    ('optional', 0, 'healthy'), ('warning', 0, 'degraded'),
    ('unverified', 2, 'unverified'), ('error', 1, 'degraded'),
    ('security', 1, 'degraded'), ('exception', 2, 'unverified'),
])
def test_status_and_exit_match_evidence(kind, expected, state, monkeypatch, capsys):
    @doctor_check()
    def check(fix, finding):
        def exception():
            raise RuntimeError('probe unavailable')
        actions = {
            'optional': lambda: check_unavailable('unused provider', 'configure only if needed'),
            'warning': lambda: check_warn('retained storage exceeds advisory threshold'),
            'unverified': lambda: check_unverified('database write health', 'live writer'),
            'exception': exception,
            'error': lambda: finding.issues.append('action required'),
            'security': lambda: finding.security.append('action required'),
        }
        actions[kind]()
    monkeypatch.setattr(doctor, 'DOCTOR_CHECKS', ((None, check),))
    assert doctor.run_doctor(SimpleNamespace(fix=False, live=False)) == expected
    output = capsys.readouterr().out
    assert f'Health: {state}' in output
    if kind in ('warning', 'unverified', 'exception', 'security'):
        assert 'All checks passed' not in output
    if kind == 'optional':
        assert 'Unavailable capabilities (optional)' in output
        assert 'Errors:' not in output


@pytest.mark.parametrize('result', [
    subprocess.CompletedProcess([], 1, '{"error":{"code":"ENOAUDIT"}}', ''),
    subprocess.CompletedProcess([], 0, '{}', ''),
    subprocess.CompletedProcess([], 0, 'invalid JSON', ''),
    subprocess.CompletedProcess([], 2, '{"metadata":{"vulnerabilities":{}}}', ''),
    subprocess.TimeoutExpired('npm', 30),
])
def test_failed_audits_are_unverified(result, monkeypatch, capsys):
    monkeypatch.setattr('hermes_constants.with_hermes_node_path', dict)
    @doctor_check()
    def check(fix, finding):
        doctor_tools._audit_one('npm', '.', 'tree', [], finding.security)
    with patch.object(doctor_tools.subprocess, 'run', **(
        {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
    )):
        finding = check(False)
    assert finding.unverified and not finding.security
    assert 'no known vulnerabilities' not in capsys.readouterr().out


@pytest.mark.parametrize('severity', ['moderate', 'low'])
def test_all_vulnerability_severities_remain_security_findings(severity, monkeypatch, capsys):
    counts = dict.fromkeys(('critical','high','moderate','low','info'), 0)
    counts[severity] = 1
    data = json.dumps({'metadata': {'vulnerabilities': counts}})
    monkeypatch.setattr('hermes_constants.with_hermes_node_path', dict)
    issues = []
    with patch.object(doctor_tools.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, data, '')):
        doctor_tools._audit_one('npm', '.', 'tree', [], issues)
    assert issues and 'has 1 npm vulnerability' in issues[0]
    assert '✓' not in capsys.readouterr().out


def test_default_board_placeholder_does_not_mask_an_unreadable_canonical_db(tmp_path, monkeypatch, capsys):
    import sqlite3
    canonical = tmp_path / 'kanban.db'
    with sqlite3.connect(canonical) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT)')
    placeholder = tmp_path / 'kanban/boards/default/kanban.db'
    placeholder.parent.mkdir(parents=True)
    placeholder.touch()
    doctor_platform._report_database_journal_modes(tmp_path, (3, 53, 1))
    assert 'unused empty placeholder' in capsys.readouterr().out
    canonical.write_bytes(b'broken')
    doctor_platform._report_database_journal_modes(tmp_path, (3, 53, 1))
    output = capsys.readouterr().out
    assert 'unused empty placeholder' not in output
    assert 'Unverified:' in output
    assert placeholder.stat().st_size == 0


def test_large_live_db_probe_is_unverified_and_does_not_repair(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    path.touch()
    monkeypatch.setattr(doctor_state, '_WRITE_PROBE_SNAPSHOT_MAX_BYTES', -1)
    monkeypatch.setattr('hermes_state_repair._live_writer_holds_db', lambda path: True)
    def refuse(path):
        raise AssertionError('must not probe live data')
    monkeypatch.setattr('hermes_state_repair._db_opens_cleanly', refuse)
    @doctor_check()
    def check(fix, finding):
        assert doctor_state._write_health_reason(path, should_fix=False) is None
    finding = check(False)
    assert finding.unverified and not finding.issues


def test_late_probe_failure_retains_established_vulnerabilities(monkeypatch, capsys):
    @doctor_check()
    def check(fix, finding):
        finding.security.append('known vulnerable dependency')
        raise RuntimeError('later probe failed')
    monkeypatch.setattr(doctor, 'DOCTOR_CHECKS', ((None, check),))
    assert doctor.run_doctor(SimpleNamespace(fix=False, live=False)) == 1
    output = capsys.readouterr().out
    assert 'known vulnerable dependency' in output and 'later probe failed' in output
