"""Continuity and review attribution survive ordinary ledger append/rollback metadata."""
import contextvars
import json


def test_external_change_is_explicit_and_history_is_not_rewritten(tmp_path, monkeypatch):
    from tools import skill_ledger as ledger
    monkeypatch.setattr(ledger, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(ledger, 'ledger_enabled', lambda: True)
    monkeypatch.setattr(ledger, '_maintain_size', lambda: None)
    path = str(tmp_path / 'skills' / 'sample' / 'SKILL.md')
    first = ledger.append_entry('patch', 'sample', before=[{'path': path, 'sha256': 'a'}],
                                after=[{'path': path, 'sha256': 'b'}])
    original = ledger.ledger_path().read_bytes()
    ledger.append_entry('patch', 'sample', before=[{'path': path, 'sha256': 'external'}],
                        after=[{'path': path, 'sha256': 'd'}])
    assert ledger.ledger_path().read_bytes().startswith(original)
    latest = json.loads(ledger.ledger_path().read_text().splitlines()[-1])
    assert latest['evidence']['continuity_discontinuities'] == [{
        'path': path, 'previous_sha256': 'b', 'observed_before_sha256': 'external',
        'previous_entry_id': first}]


def test_review_metadata_crosses_tool_context_and_resets(tmp_path, monkeypatch):
    from tools import skill_ledger as ledger
    from tools.skill_provenance import set_review_provenance, reset_review_provenance
    monkeypatch.setattr(ledger, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(ledger, 'ledger_enabled', lambda: True)
    monkeypatch.setattr(ledger, '_maintain_size', lambda: None)
    token = set_review_provenance({'mechanism': 'post_turn_background_review',
                                   'run_id': 'fixture', 'prompt_sha256': 'prompt-hash',
                                   'model': 'fixture-model'})
    try:
        contextvars.copy_context().run(ledger.append_entry, 'patch', 'sample')
    finally:
        reset_review_provenance(token)
    ledger.append_entry('patch', 'other')
    first, second = [json.loads(line) for line in ledger.ledger_path().read_text().splitlines()]
    assert first['evidence']['run_id'] == 'fixture'
    assert first['evidence']['prompt_sha256'] == 'prompt-hash'
    assert second['evidence']['mechanism'] == 'foreground'
    assert 'run_id' not in second['evidence']
