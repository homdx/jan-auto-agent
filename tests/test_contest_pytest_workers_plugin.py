"""tests/test_contest_pytest_workers_plugin.py — KC-65: the reader half of the
agents' pytest worker count.

``tools/contest/pytest_plugin/contest_pytest_workers.py`` is what an agent's
own ``pytest`` reads: the round writes ``<out_dir>/pytest-workers``, the CLI
points ``CONTEST_PYTEST_WORKERS_FILE`` at it and puts this directory alone on
``PYTHONPATH``, and the plugin turns the number into ``-n``. It must never
break a run it has nothing to say to — no xdist, no file, an empty one — and
it must read the file at every pytest start, because the count moves as
agents finish.

Every test below runs a **nested** pytest in ``tmp_path``: the round's own
``addopts = -n auto``, a six-test file, and the four KC-65 variables set or
cleared by the test itself, in the child's env alone. The worker count is read
back out of xdist's own ``created: N/N workers`` line, which is the number
xdist actually scheduled — not what the plugin returned on paper. No provider
and no kilo server are involved, and ``os.environ`` is never touched: each
child gets a copy with the four variables cleared.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The one directory the round puts on the agents' ``PYTHONPATH``.
PLUGIN_DIR = REPO_ROOT / "tools" / "contest" / "pytest_plugin"

#: The four KC-65 variables: every child env below starts with none of them.
VARS = ("CONTEST_PYTEST_WORKERS_FILE", "PYTEST_PLUGINS",
        "PYTEST_XDIST_AUTO_NUM_WORKERS", "PYTHONPATH")

#: The round's own ``pytest.ini`` — an agent's clone carries this.
NESTED_INI = "[pytest]\naddopts = -n auto\n"

#: Six tests: enough that every worker xdist creates runs at least one.
NESTED_TESTS = "".join(f"def test_{name}(): pass\n" for name in "abcdef")


# ─────────────────────────────────────────────────────────────────────────────
# the nested run
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def nested(tmp_path):
    """A clone of the round's own pytest: the same ``addopts``, six tests."""
    (tmp_path / "pytest.ini").write_text(NESTED_INI, encoding="utf-8")
    (tmp_path / "test_workers.py").write_text(NESTED_TESTS, encoding="utf-8")
    return tmp_path


def _env(plugin: bool = True, workers_file: str | None = None,
         fallback: str | None = None) -> dict:
    """The child's env: the four KC-65 variables cleared, then only the ones
    named set. Nothing else of the test process's environment leaks through
    them."""
    env = {key: value for key, value in os.environ.items() if key not in VARS}
    if plugin:
        env["PYTEST_PLUGINS"] = "contest_pytest_workers"
        env["PYTHONPATH"] = str(PLUGIN_DIR)
    if workers_file is not None:
        env["CONTEST_PYTEST_WORKERS_FILE"] = workers_file
    if fallback is not None:
        env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = fallback
    return env


def _run(nested: Path, env: dict, argv: list, target: str = "test_workers.py"):
    """One nested pytest: the round's own ini, no cache, the test's own env."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(nested / "pytest.ini"),
         "-p", "no:cacheprovider", *argv, str(nested / target)],
        env=env, capture_output=True, text=True, timeout=180, cwd=str(nested))


def _workers(proc) -> int | None:
    """The count xdist scheduled, from its own ``created: N/N`` line; ``None``
    when xdist never started, because then there is no count to report."""
    match = re.search(r"created: (\d+)/", proc.stdout + proc.stderr)
    return int(match.group(1)) if match else None


def _combine(proc) -> str:
    return proc.stdout + proc.stderr


# ─────────────────────────────────────────────────────────────────────────────
# the count moves, so the file is read at every start
# ─────────────────────────────────────────────────────────────────────────────

def test_the_file_is_read_at_every_pytest_start(nested):
    """Two pytests, two counts: the round rewrote the file between them, and
    the second run starts with the second number, not the one of the first.
    That is the whole point — a run begun before the last agent finished must
    not keep the crowd's count."""
    workers = nested / "pytest-workers"
    workers.write_text("3\n", encoding="utf-8")
    env = _env(workers_file=str(workers))

    first = _run(nested, env, [])
    assert first.returncode == 0, _combine(first)
    assert _workers(first) == 3

    workers.write_text("2\n", encoding="utf-8")
    second = _run(nested, env, [])
    assert second.returncode == 0, _combine(second)
    assert _workers(second) == 2


# ─────────────────────────────────────────────────────────────────────────────
# nothing to say: xdist answers for itself
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["", "abc", "0", "-1"])
def test_an_unusable_file_gives_xdist_its_own_fallback(nested, text):
    """Empty, not a number, zero and negative all mean "no answer": the run is
    green and the workers are xdist's own ``PYTEST_XDIST_AUTO_NUM_WORKERS``."""
    workers = nested / "pytest-workers"
    workers.write_text(text, encoding="utf-8")

    proc = _run(nested, _env(workers_file=str(workers), fallback="2"), [])

    assert proc.returncode == 0, _combine(proc)
    assert _workers(proc) == 2


def test_a_missing_file_with_no_fallback_is_xdist_and_the_cores(nested):
    """No variable at all: xdist's ``auto`` is the core count, and the run is
    still green — the plugin refused to invent a number, and nobody broke."""
    proc = _run(nested, _env(), [])

    assert proc.returncode == 0, _combine(proc)
    assert _workers(proc) == _xdist_auto()


def _xdist_auto() -> int:
    """What xdist's own ``auto`` answers here with no variable set: the
    physical cores when psutil is installed, else the logical ones — so the
    test holds on a box with and without psutil."""
    from types import SimpleNamespace
    from xdist.plugin import pytest_xdist_auto_num_workers

    config = SimpleNamespace(option=SimpleNamespace(numprocesses="auto"))
    saved = os.environ.pop("PYTEST_XDIST_AUTO_NUM_WORKERS", None)
    try:
        return pytest_xdist_auto_num_workers(config)
    finally:
        if saved is not None:
            os.environ["PYTEST_XDIST_AUTO_NUM_WORKERS"] = saved


# ─────────────────────────────────────────────────────────────────────────────
# no xdist at all
# ─────────────────────────────────────────────────────────────────────────────

def test_the_plugin_needs_no_xdist_to_load(nested):
    """``-p no:xdist`` must not fail with an unknown hook: the plugin declares
    its hook optional, so a run that asks for no workers in parallel loads it
    and gets its tests. ``addopts`` is cleared for this run alone — ``-n`` is
    xdist's own option, so asking for both is a refusal that has nothing to do
    with the plugin."""
    proc = _run(nested, _env(workers_file=str(nested / "no-such-file")),
                ["-o", "addopts=", "-p", "no:xdist"])

    assert proc.returncode == 0, _combine(proc)
    assert _workers(proc) is None, "xdist never started, so there is no count"
    assert "unknown hook" not in _combine(proc).lower()


# ─────────────────────────────────────────────────────────────────────────────
# the four variables together, as the round sets them
# ─────────────────────────────────────────────────────────────────────────────

def test_all_four_variables_are_honoured_at_once(nested):
    """The round's own combination: the file says 2, the fallback says 8, the
    plugin is loaded from its one directory, and ``-n auto`` is the ini's. The
    file wins over the fallback, and the agent's own module imports from its
    own clone — never the runner's repo root."""
    (nested / "thing.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    (nested / "test_thing.py").write_text(
        "import thing\n\n"
        "def test_the_clone():\n    assert thing.thing() == 1\n", encoding="utf-8")
    workers = nested / "pytest-workers"
    workers.write_text("2\n", encoding="utf-8")
    env = _env(workers_file=str(workers), fallback="8")
    assert env["PYTHONPATH"] == str(PLUGIN_DIR)
    # one directory, and it is the plugin's own: the runner's repo root is not
    # there, so an agent's pytest imports the clone's code, not the runner's
    assert env["PYTHONPATH"].split(os.pathsep) == [str(PLUGIN_DIR)]

    proc = _run(nested, env, [], target="test_thing.py")

    assert proc.returncode == 0, _combine(proc)
    assert _workers(proc) == 2
    assert "failed" not in _combine(proc)
