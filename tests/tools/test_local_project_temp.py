"""Assigned checkout owns terminal evidence and child temporary files."""
from pathlib import Path
from unittest.mock import patch


def test_project_temp_overrides_external_scratch_and_reaches_child(tmp_path):
    from tools.environments.local import LocalEnvironment
    (tmp_path / '.git').mkdir()
    env = LocalEnvironment.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    env.env = {'TMPDIR': '/external-fixture', 'TERMINAL_TEMP_DIR': '/external-fixture'}
    target = tmp_path / 'var' / 'hermes-terminal'
    assert Path(env.get_temp_dir()) == target
    with patch('tools.environments.local._make_run_env', return_value={}), \
         patch('tools.environments.local._find_bash', return_value='/bin/bash'), \
         patch.object(env, '_recover_cwd'), \
         patch('tools.environments.local.os.getpgid', side_effect=ProcessLookupError), \
         patch('tools.environments.local.subprocess.Popen') as spawn:
        env._run_bash('true')
    child_env = spawn.call_args.kwargs['env']
    assert all(child_env[key] == str(target) for key in ('TMPDIR', 'TMP', 'TEMP'))
    assert target.is_dir()
