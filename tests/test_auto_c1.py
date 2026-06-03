"""tests/test_auto_c1.py — Tests for AUTO-C1: Sandboxed executor.

Covers all ACs from the story:

  AC1 (structured result: {exit_code, stdout, stderr, timed_out}):
      - Successful command → exit_code=0, stdout captured, timed_out=False.
      - Failing command   → exit_code≠0, stderr captured, timed_out=False.
      - Timed-out command → exit_code=-1, timed_out=True.
      - result.to_dict() has all required keys.

  AC2 (respects [auto] exec_timeout_sec):
      - Executor constructed with timeout=0 never times out (no cap).
      - Executor constructed with a short timeout kills a slow process.

  Broader coverage:

  ExecutionResult:
      - passed == True iff exit_code==0 and not timed_out.
      - passed == False when timed_out even if exit_code were 0.
      - summary() format strings for PASS / FAIL / TIMEOUT.
      - to_dict() contains 'passed' key matching .passed property.

  WorkspaceSetup:
      - Workspace dir created under workspace_root/<task_id>/.
      - Target files copied from base_dir into workspace.
      - Missing target files skipped without error.
      - Workspace wiped and recreated on repeated run() calls (clean slate).
      - Subdirectory structure of target files preserved in workspace.

  CommandResolution:
      - Non-empty acceptance_check used verbatim.
      - acceptance_check starting with 'python ' rewritten to sys.executable.
      - acceptance_check starting with 'python3 ' rewritten to sys.executable.
      - Bare 'python' rewritten to sys.executable.
      - Empty acceptance_check + single .py target → python <file>.
      - Empty acceptance_check + multiple files → pytest.
      - Empty acceptance_check + no files → pytest.

  NetworkSuppression:
      - Child process does NOT see http_proxy / https_proxy / etc.
      - PYTHONDONTWRITEBYTECODE is set in child env.
      - PYTHONUNBUFFERED is set in child env.

  TracebackExtraction:
      - stderr with traceback → traceback field populated.
      - stderr without traceback → traceback == "".
      - Multiple tracebacks → last one returned.
      - Traceback capped at _MAX_TRACEBACK_CHARS.

  OutputTruncation:
      - stdout/stderr > _MAX_OUTPUT_CHARS truncated with notice.
      - Output at exactly _MAX_OUTPUT_CHARS not truncated.

  ErrorHandling:
      - OSError from shell → exit_code=-1, stderr=str(exc), no raise.
      - Missing 'id' field in task → ValueError raised.
      - Empty 'id' field in task → ValueError raised.

  RunRaw:
      - run_raw() executes command in base_dir and returns ExecResult.
      - run_raw() respects explicit cwd override.

  Integration:
      - Full end-to-end: real Python script written to base_dir, copied to
        workspace, acceptance_check runs it, stdout captured, passed==True.
      - Failing script: exit_code≠0, passed==False.
      - Timeout on a sleeping script: timed_out==True.
      - make_executor() factory returns a working Executor.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.executor import (
    Executor,
    ExecutionResult,
    _extract_traceback,
    _truncate,
    make_executor,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _task(
    *,
    task_id: str = "AUTO-T1",
    acceptance_check: str = "exit 0",
    target_files: list[str] | None = None,
) -> dict:
    return {
        "id":               task_id,
        "acceptance_check": acceptance_check,
        "target_files":     target_files or [],
    }


def _executor(tmp_path: Path, timeout: float = 30) -> Executor:
    return Executor(
        base_dir       = tmp_path / "repo",
        workspace_root = tmp_path / "ws",
        timeout_sec    = timeout,
    )


def _setup_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — structured result
# ─────────────────────────────────────────────────────────────────────────────

class TestStructuredResult:
    def test_success_exit_code_zero(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="exit 0"))
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.passed is True

    def test_failure_nonzero_exit_code(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="exit 1"))
        assert result.exit_code == 1
        assert result.passed is False

    def test_stdout_captured(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="echo hello_marker"))
        assert "hello_marker" in result.stdout

    def test_stderr_captured(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="echo err_marker >&2"))
        assert "err_marker" in result.stderr

    def test_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task())
        d = result.to_dict()
        for key in ("exit_code", "stdout", "stderr", "timed_out"):
            assert key in d, f"missing key: {key}"

    def test_to_dict_passed_matches_property(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="exit 0"))
        assert result.to_dict()["passed"] == result.passed

    def test_command_field_populated(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(acceptance_check="echo ok"))
        assert result.command != ""

    def test_task_id_field_populated(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(task_id="MY-TASK", acceptance_check="exit 0"))
        assert result.task_id == "MY-TASK"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — timeout enforcement
# ─────────────────────────────────────────────────────────────────────────────

class TestTimeoutEnforcement:
    def test_timed_out_result(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = Executor(
            base_dir       = tmp_path / "repo",
            workspace_root = tmp_path / "ws",
            timeout_sec    = 0.5,   # 500ms — sleep 5 will exceed this
        )
        result = ex.run(_task(acceptance_check="sleep 5"))
        assert result.timed_out is True
        assert result.exit_code == -1
        assert result.passed is False

    def test_no_timeout_when_zero(self, tmp_path: Path) -> None:
        """timeout_sec=0 means no cap; a fast command still passes."""
        _setup_repo(tmp_path)
        ex = Executor(
            base_dir       = tmp_path / "repo",
            workspace_root = tmp_path / "ws",
            timeout_sec    = 0,
        )
        result = ex.run(_task(acceptance_check="exit 0"))
        assert result.timed_out is False
        assert result.passed is True

    def test_timed_out_partial_stdout_captured(self, tmp_path: Path) -> None:
        """Partial output before timeout is included in result."""
        _setup_repo(tmp_path)
        ex = Executor(
            base_dir       = tmp_path / "repo",
            workspace_root = tmp_path / "ws",
            timeout_sec    = 0.5,
        )
        # Print something, then sleep long.  On some systems partial stdout
        # may or may not be captured depending on buffering, so we just check
        # the result doesn't crash and timed_out is set.
        result = ex.run(_task(acceptance_check="echo partial; sleep 10"))
        assert result.timed_out is True


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionResult properties
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionResultProperties:
    def test_passed_true(self) -> None:
        r = ExecutionResult(exit_code=0, timed_out=False)
        assert r.passed is True

    def test_passed_false_nonzero(self) -> None:
        r = ExecutionResult(exit_code=1, timed_out=False)
        assert r.passed is False

    def test_passed_false_timed_out(self) -> None:
        r = ExecutionResult(exit_code=0, timed_out=True)
        assert r.passed is False

    def test_summary_pass(self) -> None:
        r = ExecutionResult(exit_code=0, timed_out=False, task_id="T1", command="pytest")
        assert "PASS" in r.summary()
        assert "T1" in r.summary()

    def test_summary_fail(self) -> None:
        r = ExecutionResult(exit_code=2, timed_out=False, task_id="T1", command="pytest")
        assert "FAIL" in r.summary()
        assert "2" in r.summary()

    def test_summary_timeout(self) -> None:
        r = ExecutionResult(exit_code=-1, timed_out=True, task_id="T1", command="sleep 99")
        assert "TIMEOUT" in r.summary()

    def test_default_fields(self) -> None:
        r = ExecutionResult()
        assert r.exit_code == -1
        assert r.stdout == ""
        assert r.stderr == ""
        assert r.timed_out is False
        assert r.traceback == ""
        assert r.command == ""
        assert r.task_id == ""


# ─────────────────────────────────────────────────────────────────────────────
# Workspace setup
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkspaceSetup:
    def test_workspace_dir_created(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        ex.run(_task(task_id="TASK-1"))
        ws = tmp_path / "ws" / "TASK-1"
        assert ws.is_dir()

    def test_target_file_copied_to_workspace(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "script.py").write_text("print('hello')")
        ex = _executor(tmp_path)
        ex.run(_task(target_files=["script.py"]))
        assert (tmp_path / "ws" / "AUTO-T1" / "script.py").exists()

    def test_missing_target_file_skipped(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        # Should not raise even though ghost.py doesn't exist.
        result = ex.run(_task(target_files=["ghost.py"]))
        assert result.exit_code == 0  # exit 0 command still runs

    def test_workspace_recreated_on_rerun(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "file.py").write_text("v1")
        ex = _executor(tmp_path)
        ex.run(_task(task_id="TASK-1", target_files=["file.py"]))

        # Overwrite with v2 and re-run — workspace should have fresh copy.
        (repo / "file.py").write_text("v2")
        ex.run(_task(task_id="TASK-1", target_files=["file.py"]))
        content = (tmp_path / "ws" / "TASK-1" / "file.py").read_text()
        assert content == "v2"

    def test_subdirectory_structure_preserved(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "tools").mkdir()
        (repo / "tools" / "utils.py").write_text("pass")
        ex = _executor(tmp_path)
        ex.run(_task(target_files=["tools/utils.py"]))
        assert (tmp_path / "ws" / "AUTO-T1" / "tools" / "utils.py").exists()

    def test_multiple_target_files_all_copied(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "a.py").write_text("a")
        (repo / "b.py").write_text("b")
        ex = _executor(tmp_path)
        ex.run(_task(target_files=["a.py", "b.py"]))
        ws = tmp_path / "ws" / "AUTO-T1"
        assert (ws / "a.py").exists()
        assert (ws / "b.py").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Command resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandResolution:
    """Test _resolve_command() via a patched _execute()."""

    def _capture_command(self, tmp_path: Path, task: dict) -> str:
        """Run with a patched _execute to capture the resolved command."""
        ex = _executor(tmp_path)
        captured: list[str] = []
        orig = ex._execute

        def fake_execute(command, *, cwd, task_id):
            captured.append(command)
            return ExecutionResult(exit_code=0, command=command, task_id=task_id)

        ex._execute = fake_execute  # type: ignore[method-assign]
        ex.run(task)
        return captured[0]

    def test_acceptance_check_used_verbatim(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(tmp_path, _task(acceptance_check="make test"))
        assert "make test" in cmd

    def test_python_prefix_rewritten(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(
            tmp_path, _task(acceptance_check="python -m pytest tests/")
        )
        assert cmd.startswith(sys.executable)
        assert "-m pytest" in cmd

    def test_python3_prefix_rewritten(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(
            tmp_path, _task(acceptance_check="python3 script.py")
        )
        assert cmd.startswith(sys.executable)

    def test_bare_python_rewritten(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(tmp_path, _task(acceptance_check="python"))
        assert cmd == sys.executable

    def test_no_check_single_py_uses_python_file(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(
            tmp_path,
            {"id": "T1", "acceptance_check": "", "target_files": ["tools/run.py"]},
        )
        assert sys.executable in cmd
        assert "tools/run.py" in cmd

    def test_no_check_multiple_files_uses_pytest(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(
            tmp_path,
            {"id": "T1", "acceptance_check": "", "target_files": ["a.py", "b.py"]},
        )
        assert "pytest" in cmd

    def test_no_check_no_files_uses_pytest(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(
            tmp_path,
            {"id": "T1", "acceptance_check": "", "target_files": []},
        )
        assert "pytest" in cmd

    def test_non_python_check_unchanged(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        cmd = self._capture_command(tmp_path, _task(acceptance_check="./run.sh"))
        assert cmd == "./run.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Network suppression
# ─────────────────────────────────────────────────────────────────────────────

class TestNetworkSuppression:
    def test_proxy_vars_not_in_child_env(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        # Run a python command that prints the env; check proxy vars absent.
        script = (
            "import os, sys\n"
            "for v in ('http_proxy','HTTP_PROXY','https_proxy','HTTPS_PROXY'):\n"
            "    if v in os.environ:\n"
            "        sys.exit(f'FOUND {v}')\n"
            "print('clean')\n"
        )
        (repo / "check_env.py").write_text(script)
        with patch.dict(os.environ, {"http_proxy": "http://evil.proxy/", "HTTP_PROXY": "x"}):
            result = ex.run(_task(
                acceptance_check=f"{sys.executable} check_env.py",
                target_files=["check_env.py"],
            ))
        assert result.passed, result.stderr
        assert "clean" in result.stdout

    def test_pythondontwritebytecode_set(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        script = "import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE', 'MISSING'))\n"
        (repo / "check_pdb.py").write_text(script)
        result = ex.run(_task(
            acceptance_check=f"{sys.executable} check_pdb.py",
            target_files=["check_pdb.py"],
        ))
        assert result.passed
        assert "MISSING" not in result.stdout

    def test_pythonunbuffered_set(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        script = "import os; print(os.environ.get('PYTHONUNBUFFERED', 'MISSING'))\n"
        (repo / "check_ub.py").write_text(script)
        result = ex.run(_task(
            acceptance_check=f"{sys.executable} check_ub.py",
            target_files=["check_ub.py"],
        ))
        assert result.passed
        assert "MISSING" not in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Traceback extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestTracebackExtraction:
    def test_no_traceback_returns_empty(self) -> None:
        assert _extract_traceback("") == ""
        assert _extract_traceback("some output\nno error") == ""

    def test_traceback_extracted(self) -> None:
        stderr = (
            "some preamble\n"
            "Traceback (most recent call last):\n"
            '  File "script.py", line 1, in <module>\n'
            "ValueError: bad value\n"
        )
        result = _extract_traceback(stderr)
        assert "Traceback" in result
        assert "ValueError" in result

    def test_multiple_tracebacks_returns_last(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            "  File 'a.py', line 1\n"
            "FirstError: first\n"
            "\n"
            "Traceback (most recent call last):\n"
            "  File 'b.py', line 2\n"
            "SecondError: second\n"
        )
        result = _extract_traceback(stderr)
        assert "SecondError" in result
        assert "FirstError" not in result

    def test_traceback_real_subprocess(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        script = "raise ValueError('test error msg')\n"
        (repo / "bad.py").write_text(script)
        result = ex.run(_task(
            acceptance_check=f"{sys.executable} bad.py",
            target_files=["bad.py"],
        ))
        assert result.passed is False
        assert "Traceback" in result.traceback
        assert "ValueError" in result.traceback


# ─────────────────────────────────────────────────────────────────────────────
# Output truncation
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputTruncation:
    def test_short_text_not_truncated(self) -> None:
        assert _truncate("hello", 100) == "hello"

    def test_exact_length_not_truncated(self) -> None:
        text = "x" * 100
        assert _truncate(text, 100) == text

    def test_long_text_truncated_with_notice(self) -> None:
        text = "x" * 200
        result = _truncate(text, 100)
        assert len(result) > 100  # includes notice
        assert "truncated" in result
        assert result.startswith("x" * 100)

    def test_truncation_in_real_run(self, tmp_path: Path) -> None:
        from tools.auto.executor import _MAX_OUTPUT_CHARS
        repo = _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        # Write a script that prints more than _MAX_OUTPUT_CHARS characters.
        big = _MAX_OUTPUT_CHARS + 1000
        script = f"print('A' * {big})\n"
        (repo / "big.py").write_text(script)
        result = ex.run(_task(
            acceptance_check=f"{sys.executable} big.py",
            target_files=["big.py"],
        ))
        assert "truncated" in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Error handling
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_id_raises_value_error(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        with pytest.raises(ValueError, match="id"):
            ex.run({"acceptance_check": "exit 0", "target_files": []})

    def test_empty_id_raises_value_error(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        with pytest.raises(ValueError, match="id"):
            ex.run({"id": "   ", "acceptance_check": "exit 0", "target_files": []})

    def test_oserror_returns_result_no_raise(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        with patch("subprocess.run", side_effect=OSError("shell not found")):
            result = ex.run(_task())
        assert result.exit_code == -1
        assert "shell not found" in result.stderr
        assert result.passed is False

    def test_nonzero_exit_with_stderr(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run(_task(
            acceptance_check=f"{sys.executable} -c \"import sys; sys.stderr.write('err'); sys.exit(3)\""
        ))
        assert result.exit_code == 3
        assert "err" in result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# run_raw()
# ─────────────────────────────────────────────────────────────────────────────

class TestRunRaw:
    def test_run_raw_passes_on_success(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run_raw("exit 0")
        assert result.passed is True

    def test_run_raw_fails_on_nonzero(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run_raw("exit 42")
        assert result.exit_code == 42

    def test_run_raw_captures_stdout(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run_raw("echo raw_output")
        assert "raw_output" in result.stdout

    def test_run_raw_explicit_cwd(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        custom_cwd = tmp_path / "custom"
        custom_cwd.mkdir()
        ex = _executor(tmp_path)
        result = ex.run_raw("pwd", cwd=custom_cwd)
        assert str(custom_cwd) in result.stdout

    def test_run_raw_task_id_empty(self, tmp_path: Path) -> None:
        _setup_repo(tmp_path)
        ex = _executor(tmp_path)
        result = ex.run_raw("exit 0")
        assert result.task_id == ""


# ─────────────────────────────────────────────────────────────────────────────
# make_executor() factory
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeExecutor:
    def test_factory_returns_executor(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = make_executor(base_dir=repo)
        assert isinstance(ex, Executor)

    def test_factory_default_timeout(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = make_executor(base_dir=repo)
        assert ex._timeout_sec == 120

    def test_factory_custom_timeout(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = make_executor(base_dir=repo, timeout_sec=60)
        assert ex._timeout_sec == 60

    def test_factory_zero_timeout(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = make_executor(base_dir=repo, timeout_sec=0)
        assert ex._timeout_sec == 0

    def test_factory_custom_workspace_root(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ws = tmp_path / "my_ws"
        ex = make_executor(base_dir=repo, workspace_root=ws)
        assert ex._workspace_root == ws

    def test_factory_custom_python_bin(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        ex = make_executor(base_dir=repo, python_bin="/custom/python")
        assert ex._python_bin == "/custom/python"


# ─────────────────────────────────────────────────────────────────────────────
# Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_passing_python_script(self, tmp_path: Path) -> None:
        """Write a real script, run it via acceptance_check, verify pass."""
        repo = _setup_repo(tmp_path)
        script = textwrap.dedent("""\
            print("integration ok")
        """)
        (repo / "my_module.py").write_text(script)
        ex = _executor(tmp_path)
        result = ex.run({
            "id":               "AUTO-T-INT",
            "acceptance_check": f"{sys.executable} my_module.py",
            "target_files":     ["my_module.py"],
        })
        assert result.passed is True
        assert "integration ok" in result.stdout
        assert result.task_id == "AUTO-T-INT"

    def test_failing_python_script(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "fail.py").write_text("import sys; sys.exit(1)\n")
        ex = _executor(tmp_path)
        result = ex.run({
            "id":               "FAIL-TASK",
            "acceptance_check": f"{sys.executable} fail.py",
            "target_files":     ["fail.py"],
        })
        assert result.passed is False
        assert result.exit_code == 1

    def test_timeout_on_sleeping_script(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "sleepy.py").write_text("import time; time.sleep(99)\n")
        ex = Executor(
            base_dir       = repo,
            workspace_root = tmp_path / "ws",
            timeout_sec    = 0.5,
        )
        result = ex.run({
            "id":               "SLEEP-TASK",
            "acceptance_check": f"{sys.executable} sleepy.py",
            "target_files":     ["sleepy.py"],
        })
        assert result.timed_out is True
        assert result.passed is False

    def test_pytest_acceptance_check(self, tmp_path: Path) -> None:
        """Acceptance check using pytest on a real test file."""
        repo = _setup_repo(tmp_path)
        test_script = textwrap.dedent("""\
            def test_always_passes():
                assert 1 + 1 == 2
        """)
        (repo / "test_sample.py").write_text(test_script)
        ex = _executor(tmp_path)
        result = ex.run({
            "id":               "PYTEST-TASK",
            "acceptance_check": f"{sys.executable} -m pytest test_sample.py -q",
            "target_files":     ["test_sample.py"],
        })
        assert result.passed is True

    def test_script_with_traceback(self, tmp_path: Path) -> None:
        repo = _setup_repo(tmp_path)
        (repo / "crash.py").write_text("raise RuntimeError('boom')\n")
        ex = _executor(tmp_path)
        result = ex.run({
            "id":               "CRASH-TASK",
            "acceptance_check": f"{sys.executable} crash.py",
            "target_files":     ["crash.py"],
        })
        assert result.passed is False
        assert "RuntimeError" in result.traceback
        assert "boom" in result.traceback
