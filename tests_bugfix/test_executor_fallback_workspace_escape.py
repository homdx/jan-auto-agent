"""tests_bugfix/test_executor_fallback_workspace_escape.py

_resolve_command's single-.py fallback (used when a task carries no
acceptance_check) built ``python <target_files[0]>`` from an unvalidated path
and handed it to subprocess.run(shell=True, cwd=workspace).

_prepare_workspace already refuses to COPY a target_files entry that escapes
base_dir/workspace, but that guard only stops the copy — the fallback command
was still constructed from the same raw entry, so "../../x.py" or an absolute
path executed a file outside the workspace. The fallback now fails closed with
"false", matching what a blocked acceptance_check already does.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import Executor


def _executor(tmp_path: Path) -> Executor:
    return Executor(base_dir=tmp_path)


def test_contained_py_file_still_resolves(tmp_path):
    ex = _executor(tmp_path)
    cmd = ex._resolve_command("", ["pkg/mod.py"], tmp_path)
    assert cmd.endswith("pkg/mod.py")
    assert cmd != "false"


def test_parent_traversal_fails_closed(tmp_path):
    ex = _executor(tmp_path)
    assert ex._resolve_command("", ["../../evil.py"], tmp_path) == "false"


def test_absolute_path_fails_closed(tmp_path):
    ex = _executor(tmp_path)
    outside = tmp_path.parent / "outside.py"
    assert ex._resolve_command("", [str(outside)], tmp_path) == "false"


def test_escape_is_not_reached_when_acceptance_check_present(tmp_path):
    """The guard is scoped to the fallback; a real acceptance_check is
    unaffected and still goes through _check_command_safety."""
    ex = _executor(tmp_path)
    cmd = ex._resolve_command("pytest tests/test_x.py", ["../../evil.py"], tmp_path)
    assert "pytest" in cmd
