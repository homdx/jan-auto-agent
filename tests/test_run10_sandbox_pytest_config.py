"""tests/test_run10_sandbox_pytest_config.py — RUN-10: a sandbox under examples/ is tested as a project of its own.

Live runbook runs, 2026-10-19 (gpt-oss-40k on profiles low, medium and high):
flow 1 asks for a pytest test of examples/hello-world's main(). The plain test
every profile wrote first — ``from main import main``, call it, compare stdout
through capsys — failed in the sandbox, because pytest found the repository's own
pytest.ini and conftest.py above it: the repository root became the rootdir,
``-n auto`` started eight workers, and the conftest's sys.path bootstrap put the
repository root first, so ``main`` was the pipeline's main.py. Its main() opened
the orchestration shell and waited for input (``OSError: pytest: reading from
stdin``, or the 120 s timeout once input was patched). Low spent ten rounds and
two and a half hours there and ended BLOCKED; Medium shipped a subprocess test
that check_runbook could not run.

examples/pytest.ini stops pytest's config search above every sandbox. These
tests build a sandbox where scripts/run_flows.sh puts one and run that plain test
the two ways the pipeline does: check_runbook's ``python -m pytest`` in the
sandbox, and the executor's acceptance check in .agent/workspace/<task>/.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tools.auto.executor import Executor

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# The test flow 1 asks for, as a model writes it first. The module and the config are
# checked before main() runs: with the repository's conftest loaded, main() is the
# pipeline's shell, and a regression would wait for input until the run's timeout.
_PROBE = '''\
import sys
from pathlib import Path

import main

SANDBOX = Path({sandbox!r})
REPO_INI = {repo_ini!r}
REPO_CONFTEST = {repo_conftest!r}


def test_main_prints_hello_world(capsys, pytestconfig):
    # The executor's workspace mirrors the sandbox, and its PYTHONPATH is the sandbox: either main.py is the right one.
    assert Path(main.__file__).resolve().parent in (SANDBOX, Path(__file__).resolve().parent.parent), main.__file__
    assert str(pytestconfig.inipath) != REPO_INI, pytestconfig.inipath
    assert getattr(sys.modules.get("conftest"), "__file__", None) != REPO_CONFTEST
    main.main()
    assert capsys.readouterr().out == "Hello world\\n"
'''


@pytest.fixture
def sandbox(monkeypatch):
    """examples/task-run10-<id>/hello-world: where run_flows.sh makes a sandbox, ignored by examples/task*/."""
    root = EXAMPLES / f"task-run10-{uuid.uuid4().hex[:8]}"
    box = root / "hello-world"
    (box / "tests").mkdir(parents=True)
    shutil.copy2(EXAMPLES / "hello-world" / "main.py", box / "main.py")
    (box / "tests" / "test_main.py").write_text(_PROBE.format(
        sandbox=str(box.resolve()),
        repo_ini=str((REPO_ROOT / "pytest.ini").resolve()),
        repo_conftest=str((REPO_ROOT / "conftest.py").resolve()),
    ), encoding="utf-8")
    # The inner runs write no .pytest_cache into examples/ (their rootdir now).
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")
    try:
        yield box
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_repository_config_the_sandboxes_must_not_see_is_there():
    """Without a root pytest.ini and conftest.py above examples/ the tests below would pass for nothing."""
    assert (REPO_ROOT / "pytest.ini").is_file()
    assert (REPO_ROOT / "conftest.py").is_file()


def test_examples_pytest_ini_sets_nothing():
    """It only ends the search: an option there would apply to every sandbox and to no real target."""
    ini = configparser.ConfigParser()
    ini.read(EXAMPLES / "pytest.ini", encoding="utf-8")
    assert ini.has_section("pytest")
    assert ini.options("pytest") == []


def test_check_runbooks_test_run_imports_the_sandboxs_main(sandbox):
    """check_runbook's "test suite passes": `python -m pytest -q <files>` with the sandbox as cwd."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_main.py"],
        cwd=str(sandbox), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("command", ["pytest tests/test_main.py -q", "python -m pytest tests/test_main.py -q"])
def test_the_executors_acceptance_check_imports_the_sandboxs_main(sandbox, command):
    """The acceptance check runs in <sandbox>/.agent/workspace/<task>/; the live runs used the bare `pytest` form."""
    if command.startswith("pytest") and shutil.which("pytest") is None:
        pytest.skip("no pytest script on PATH")
    result = Executor(base_dir=sandbox, timeout_sec=120).run({
        "id": "RUN10-T1", "acceptance_check": command, "target_files": ["tests/test_main.py"],
    })
    assert result.passed, result.stdout + result.stderr
    assert "1 passed" in result.stdout
