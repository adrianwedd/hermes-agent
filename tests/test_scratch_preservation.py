"""Merely resolving runtime scratch must retain previously recorded artifacts."""
import os


def test_scratch_resolution_preserves_idle_evidence(tmp_path, monkeypatch):
    import hermes_constants as constants
    scratch = tmp_path / 'cache' / 'scratch'
    run = scratch / 'retained-run'
    run.mkdir(parents=True)
    evidence = run / 'receipt.json'
    evidence.write_text('{"complete": true}')
    for path in (evidence, run):
        os.utime(path, (1, 1))
    monkeypatch.setattr(constants, '_scratch_pruned_once', False)
    assert constants.get_scratch_dir(home=tmp_path) == scratch
    assert evidence.read_text() == '{"complete": true}'
