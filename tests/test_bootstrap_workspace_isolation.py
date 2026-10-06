"""Control-plane imports must not execute task modules; tools retain their cwd."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("collision", ["inspect", "pathlib", "socket"])
def test_workspace_stdlib_collision_cannot_break_bootstrap(tmp_path, collision):
    workspace = tmp_path / "task"
    workspace.mkdir()
    helper = workspace / f"{collision}.py"
    helper.write_text("raise RuntimeError('TASK_MODULE_EXECUTED')\n")
    (workspace / "tool-fixture.txt").write_text("workspace tools still work")
    source = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_KANBAN_WORKSPACE=str(workspace), TERMINAL_CWD=str(workspace),
               PYTHONPATH=os.pathsep.join((str(source), str(workspace))))
    probe = """
import sys
sys.argv = ['offline', 'pm', 'repair']
import hermes_bootstrap
from dataclasses import dataclass
import inspect, os
@dataclass
class Fixture:
    value: int = 1
assert inspect.signature(Fixture)
assert os.getcwd() == os.environ['TERMINAL_CWD']
assert open('tool-fixture.txt').read() == 'workspace tools still work'
print('CONTROL_AND_TOOL_CWD_OK')
"""
    result = subprocess.run([sys.executable, "-c", probe], cwd=workspace, env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "CONTROL_AND_TOOL_CWD_OK" in result.stdout
    assert helper.read_text() == "raise RuntimeError('TASK_MODULE_EXECUTED')\n"


def test_workspace_symlink_and_nested_pythonpath_entries_are_excluded(tmp_path):
    workspace = tmp_path / "task"
    nested = workspace / "helpers"
    nested.mkdir(parents=True)
    (nested / "inspect.py").write_text("raise RuntimeError('TASK_MODULE_EXECUTED')\n")
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    source = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_KANBAN_WORKSPACE=str(workspace),
               PYTHONPATH=os.pathsep.join((str(source), str(alias / "helpers"))))
    probe = "import sys; sys.argv=['offline','pm','repair']; import hermes_bootstrap; import inspect; assert inspect.signature(int.__new__); print('OK')"
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_launch_home_keeps_installed_dependencies_beneath_cwd(tmp_path):
    dependencies = tmp_path / "installs" / "site-packages"
    dependencies.mkdir(parents=True)
    (dependencies / "trusted_dependency.py").write_text("marker='installed dependency'\n")
    workspace = tmp_path / "kanban" / "task"
    workspace.mkdir(parents=True)
    (workspace / "inspect.py").write_text("raise RuntimeError('TASK_MODULE_EXECUTED')\n")
    source = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_KANBAN_WORKSPACE=str(workspace),
               PYTHONPATH=os.pathsep.join((str(source), str(dependencies), str(workspace))))
    probe = "import sys; sys.argv=['offline','pm','repair']; import hermes_bootstrap; import inspect, trusted_dependency; assert inspect.signature(int.__new__); assert trusted_dependency.marker == 'installed dependency'; print('OK')"
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
