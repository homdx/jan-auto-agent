"""tests/test_run3_exec_feedback.py — RUN-3: exec feedback shows the tail of pytest output.

epic-tasks/31: the coder's feedback after a failed acceptance check was the
first 400 characters of stdout. For a pytest run those are xdist's
"bringing up nodes..." banner and the top of the error box; the one line
that names the cause (``E   ModuleNotFoundError: …``) sits at the bottom
and was always cut, so the coder re-emitted the same file until the cap.

Acceptance (from the ticket):
  * exit 1 + a 900-char collection error → the ``E   ModuleNotFoundError``
    line is in the feedback verbatim;
  * exit 5 + banner-only stdout → the feedback starts with the
    "no tests collected" sentence;
  * a non-pytest command with long stdout keeps the 400-char head;
  * "bringing up nodes..." never appears in feedback;
  * item 4: the workspace pytest command carries the serial flag by default
    and does not when ``[executor] pytest_serial = false``.

The serial flag is ``-n 0``, not ``-p no:xdist``: disabling the plugin
leaves the project's ``addopts = -n auto --dist=loadgroup`` unrecognised
and pytest exits 4 without running a test — the last block below proves
both on a real subprocess.
"""

from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import Executor, make_executor, pytest_config_requests_pool
from tools.auto.inner_loop import (
    InnerLoop, _EXEC_TAIL_BUDGET, _NO_TESTS_COLLECTED_MSG,
    _build_exec_detail, _pytest_tail, _strip_pytest_noise, make_inner_loop,
)
from tools.auto.utils import is_pytest_command, is_xdist_flag


# ── the output shapes the live runs produced ─────────────────────────────────

BANNER = "bringing up nodes...\nbringing up nodes...\n\n\n"
E_LINE = "E   ModuleNotFoundError: No module named 'x'"
COLLECTION_ERROR = BANNER + (
    "==================================== ERRORS ====================================\n"
    "_____________________ ERROR collecting tests/test_state.py _____________________\n"
    "ImportError while importing test module '/home/u/.agent/workspace/AUTO-T11/tests/test_state.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "/usr/lib/python3.10/importlib/__init__.py:126: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_state.py:3: in <module>\n"
    "    from tools.state import x\n"
    f"{E_LINE}\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_state.py\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.42s\n"
)
PYTEST_CMD = "/usr/bin/python3 -m pytest tests/test_state.py -q"
SCRIPT_CMD = "/usr/bin/python3 script.py"


@dataclass
class ExecResult:
    passed: bool = False
    exit_code: int = 1
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False
    command: str = PYTEST_CMD


@dataclass
class CoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: ["tests/test_state.py"])
    files_skipped: list = field(default_factory=list)
    error: str = ""
    raw_response: str = ""


class RecordingCoder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def generate(self, task, base_dir, prior_feedback=None, **kwargs):
        self.calls.append(list(prior_feedback or []))
        return CoderResult()


class OneShotExecutor:
    def __init__(self, result):
        self._result = result

    def run(self, task):
        return self._result


class ApprovingValidator:
    def approve(self, task, exec_result, coder_result, *, base_dir=None):
        return True, ""


TASK = {"id": "AUTO-T11", "title": "state", "instruction": "fix the import",
        "target_files": ["tests/test_state.py"],
        "acceptance_check": "python3 -m pytest tests/test_state.py -q"}


def _feedback(result: ExecResult, tmp_path: Path) -> str:
    """Run one attempt end to end and return the feedback the coder would get next."""
    coder = RecordingCoder()
    loop = InnerLoop(coder, OneShotExecutor(result), ApprovingValidator(), max_attempts=2)
    loop.run_task(TASK, tmp_path)
    assert len(coder.calls) == 2
    return "\n".join(coder.calls[1])


# ── 1. what the coder sees ───────────────────────────────────────────────────

def test_collection_error_e_line_reaches_the_coder_verbatim(tmp_path):
    fb = _feedback(ExecResult(exit_code=1, stdout=COLLECTION_ERROR), tmp_path)
    assert E_LINE in fb
    assert "bringing up nodes" not in fb
    assert "ERROR collecting tests/test_state.py" in fb     # the box arrives whole


def test_exit_5_starts_with_the_no_tests_sentence(tmp_path):
    fb = _feedback(ExecResult(exit_code=5, stdout=BANNER), tmp_path)
    body = fb.split("\n", 1)[1]                             # after "attempt N: exec failed …"
    assert body.startswith(_NO_TESTS_COLLECTED_MSG)
    assert "bringing up nodes" not in fb


def test_exit_5_on_a_script_gets_no_pytest_sentence(tmp_path):
    fb = _feedback(ExecResult(exit_code=5, stdout="usage: script.py", command=SCRIPT_CMD), tmp_path)
    assert "no tests collected" not in fb


def test_non_pytest_command_keeps_the_400_char_head(tmp_path):
    out = "line 1\nline 2\n" + "x" * 2000 + "\nLAST LINE"
    fb = _feedback(ExecResult(exit_code=1, stdout=out, command=SCRIPT_CMD), tmp_path)
    assert "line 1" in fb and "LAST LINE" not in fb
    assert "x" * 380 in fb and "x" * 401 not in fb


def test_stream_priority_is_unchanged():
    # traceback > stderr > stdout, as before — only the stdout window moved.
    r = ExecResult(stdout=COLLECTION_ERROR, stderr="ERROR: usage: pytest [options]\n")
    assert _build_exec_detail(r).startswith("stderr:\nERROR: usage")
    r = ExecResult(stdout=COLLECTION_ERROR, stderr="x", traceback="Traceback (most recent call last):\nRuntimeError: boom")
    assert _build_exec_detail(r).startswith("traceback:\nTraceback")
    assert "boom" in _build_exec_detail(r)


def test_command_attribute_absent_means_head_as_before():
    class Legacy:                       # older fakes have no .command
        passed, exit_code, stdout, stderr, traceback = False, 1, "a" * 900, "", ""
    assert _build_exec_detail(Legacy()) == "stdout:\n" + "a" * 400


# ── 2. the tail slice ────────────────────────────────────────────────────────

def test_strip_noise_drops_banners_and_collapses_blank_runs():
    cleaned = _strip_pytest_noise("bringing up nodes...\n  bringing up nodes...  \n\n\n\nA\n\n\n\nB\n")
    assert cleaned == "A\n\nB"


def test_tail_starts_at_the_last_error_box_not_the_summary():
    two_boxes = COLLECTION_ERROR.replace("=== ERRORS ===", "=== ERRORS ===", 1)
    tail = _pytest_tail("noise\n" * 50 + two_boxes)
    assert tail.startswith("==================================== ERRORS ====")
    assert E_LINE in tail and "short test summary info" in tail
    assert "noise" not in tail


def test_tail_over_budget_keeps_header_and_the_last_lines():
    long_box = COLLECTION_ERROR.replace("Traceback:\n", "Traceback:\n" + "  frame line of noise\n" * 200)
    tail = _pytest_tail(long_box)
    assert len(tail) <= _EXEC_TAIL_BUDGET
    assert tail.startswith("==================================== ERRORS ====")
    assert "... [trimmed]" in tail
    assert E_LINE in tail and "1 error in 0.42s" in tail
    assert not tail.split("[trimmed]\n", 1)[1].startswith("me line")   # cut on a line boundary


def test_tail_without_a_box_is_the_plain_tail():
    out = BANNER + "\n".join(f"line {i}" for i in range(400))
    tail = _pytest_tail(out)
    assert len(tail) <= _EXEC_TAIL_BUDGET
    assert tail.endswith("line 399") and "bringing up nodes" not in tail


def test_short_output_arrives_whole():
    assert _pytest_tail(BANNER + "no tests ran in 0.01s\n") == "no tests ran in 0.01s"
    assert _pytest_tail("") == ""


# ── 3. what counts as a pytest run ───────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "pytest", "pytest -q tests/test_x.py", PYTEST_CMD, "python -m pytest",
    "/venv/bin/python3.11 -m pytest tests -k foo", "py.test -q",
    "cd sub && pytest -q", r"C:\venv\Scripts\python.exe -m pytest x",
])
def test_pytest_commands_are_recognised(cmd):
    assert is_pytest_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "", SCRIPT_CMD, "echo pytest", "bash pytest_runner.sh", "grep pytest log.txt",
    "python -m unittest", "true",
])
def test_non_pytest_commands_are_not(cmd):
    assert is_pytest_command(cmd) is False


def test_xdist_flags():
    assert all(is_xdist_flag(t) for t in ("-n", "-n4", "-nauto", "--numprocesses=4", "--dist=loadgroup"))
    assert not any(is_xdist_flag(t) for t in ("-q", "--no-header", "-k", "--numbers"))


# ── 4. item 4: the workspace pytest command ──────────────────────────────────

def _repo(tmp_path: Path, addopts: str | None = "-n auto -q --dist=loadgroup") -> Path:
    if addopts is not None:
        (tmp_path / "pytest.ini").write_text(f"[pytest]\naddopts = {addopts}\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "tests" / "test_none.py").write_text("x = 1\n")
    return tmp_path


def _command(exe: Executor, check: str, files=("tests/test_ok.py",)) -> str:
    ws = exe._prepare_workspace("AUTO-T1", list(files))
    return exe._serialise_pytest(exe._resolve_command(check, list(files), ws))


def test_serial_flag_is_appended_by_default(tmp_path):
    exe = make_executor(_repo(tmp_path))
    assert _command(exe, "python -m pytest tests/test_ok.py -q").endswith("tests/test_ok.py -q -n 0")
    assert _command(exe, "", files=("tests/test_ok.py", "tests/test_none.py")) == "pytest -n 0"


def test_serial_flag_is_not_appended_when_off(tmp_path):
    exe = make_executor(_repo(tmp_path), pytest_serial=False)
    assert "-n 0" not in _command(exe, "python -m pytest tests/test_ok.py -q")


def test_serial_flag_leaves_scripts_and_explicit_worker_counts_alone(tmp_path):
    exe = make_executor(_repo(tmp_path))
    assert _command(exe, "python tests/test_ok.py").endswith("tests/test_ok.py")
    assert _command(exe, "python -m pytest -n 4 tests/test_ok.py").endswith("-n 4 tests/test_ok.py")
    assert _command(exe, "pytest -nauto tests") == "pytest -nauto tests"


def test_serial_flag_needs_a_pool_to_suppress(tmp_path):
    # A project whose config asks for no workers never gets an unknown flag —
    # on a machine without pytest-xdist that flag would fail every check.
    exe = make_executor(_repo(tmp_path, addopts="-q --strict-markers"))
    assert "-n 0" not in _command(exe, "python -m pytest tests/test_ok.py -q")
    (tmp_path / "bare").mkdir()
    exe = make_executor(_repo(tmp_path / "bare", addopts=None))
    assert "-n 0" not in _command(exe, "python -m pytest tests/test_ok.py -q")


def test_pool_detection_reads_the_first_config_file_only(tmp_path):
    root = tmp_path
    (root / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    (root / "setup.cfg").write_text("[tool:pytest]\naddopts = -n auto\n")
    assert pytest_config_requests_pool(root) is False          # pytest.ini wins, no pool
    (root / "pytest.ini").unlink()
    assert pytest_config_requests_pool(root) is True
    (root / "setup.cfg").unlink()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ["-q", "--numprocesses=2"]\n')
    assert pytest_config_requests_pool(root) is True


def test_pool_detection_honours_pytest_addopts_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n auto")
    assert pytest_config_requests_pool(tmp_path) is True
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    assert pytest_config_requests_pool(tmp_path) is False


def _loop_from(ini: str, base_dir: Path) -> InnerLoop:
    cfg = configparser.ConfigParser()
    cfg.read_string(ini)
    return make_inner_loop(cfg, base_dir, coder=RecordingCoder(), validator=ApprovingValidator())


def test_config_key_reaches_the_task_loop_executor(tmp_path):
    assert _loop_from("[executor]\npytest_serial = false\n", tmp_path).executor._pytest_serial is False
    assert _loop_from("[executor]\npytest_serial = maybe\n", tmp_path).executor._pytest_serial is True
    assert _loop_from("[auto]\nmax_attempts_per_task = 2\n", tmp_path).executor._pytest_serial is True


def test_controller_passes_the_same_key_to_the_regression_executor():
    # AUTO-G5 builds its own executor in controller._run_task_loop; it must
    # read the same [executor] key, or `false` would only reach half the runs.
    src = (PROJECT_ROOT / "tools" / "auto" / "controller.py").read_text(encoding="utf-8")
    g5 = src[src.index("# AUTO-G5: executor + bug_fix_loop"):]
    call = g5[:g5.index("bug_fix_loop = ")]
    assert 'safe_getboolean(cfg, "executor", "pytest_serial", fallback=True)' in call


# ── 5. on a real subprocess: -n 0 works, -p no:xdist would not ──────────────

pytest.importorskip("xdist", reason="pytest-xdist not installed — nothing to serialise")


def _run(exe: Executor, check: str):
    return exe.run({"id": "AUTO-T1", "acceptance_check": check, "target_files": ["tests/test_ok.py"]})


def test_real_workspace_run_is_serial_and_green(tmp_path):
    r = _run(make_executor(_repo(tmp_path)), "python -m pytest tests/test_ok.py -q")
    assert r.passed, (r.command, r.stdout[-300:], r.stderr[-300:])
    assert r.command.endswith("-n 0")
    assert "bringing up nodes" not in r.stdout


def test_real_workspace_run_with_serial_off_keeps_the_pool(tmp_path):
    r = _run(make_executor(_repo(tmp_path), pytest_serial=False), "python -m pytest tests/test_ok.py -q")
    assert r.passed
    assert "bringing up nodes" in r.stdout


def test_real_exit_5_feedback_end_to_end(tmp_path):
    exe = make_executor(_repo(tmp_path))
    r = _run(exe, "python -m pytest tests/test_none.py -q")
    assert r.exit_code == 5, (r.exit_code, r.stderr[-200:])
    detail = _build_exec_detail(r)
    assert detail.startswith(_NO_TESTS_COLLECTED_MSG)
    assert "bringing up nodes" not in detail


def test_p_no_xdist_would_have_broken_every_workspace_run(tmp_path):
    # The ticket's other option. With addopts = -n auto in force, disabling
    # the plugin makes pytest exit 4 ("unrecognized arguments: -n") before a
    # single test runs — which is why the flag is -n 0.
    r = _run(make_executor(_repo(tmp_path), pytest_serial=False),
             "python -m pytest -p no:xdist tests/test_ok.py -q")
    assert r.exit_code == 4 and "unrecognized arguments" in r.stderr
