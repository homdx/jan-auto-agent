"""SLOW-1 — check_slow1.py tells which of the reference causes a commit removes.

Offline, on a tiny git repo shaped like the target: a `SLOW-1: ticket` commit
holding the two slow patterns, then an entry commit. The reference answer
(REFERENCE.md) stays out of the target: make_target.sh copies only the ticket.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SLOW1 = REPO_ROOT / "contest-bench" / "slow1"

_RUNNER = '''pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


def make_config(agents, **over):
    kw = dict(agents=agents, max_parallel=1{extra})
    return kw


def test_one():
    assert make_config([])
'''
_SAFE = '''def test_a():
    index = _real_repo_index()
    assert index
'''
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _target(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "slow1-target"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, {"tests/test_contest_runner.py": _RUNNER.format(extra=""),
                          "tests/test_collect_already_safe_query.py": _SAFE},
                   "SLOW-1: ticket")
    return repo, base


def _check(repo: Path, commit: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SLOW1 / "check_slow1.py"), str(repo), commit],
                          capture_output=True, text=True)


def test_both_causes_fixed_passes(tmp_path):
    repo, base = _target(tmp_path)
    runner = _RUNNER.format(extra=", error_retry_backoff_sec=0").split("\n", 1)[1]
    sha = _commit(repo, {"tests/test_contest_runner.py": runner},
                  f"SLOW-1: base {base[:7]}, 300 s -> 180 s")
    r = _check(repo, sha)
    assert r.returncode == 0, r.stdout
    assert "MISS  cause 3" in r.stdout


def test_one_cause_is_not_enough(tmp_path):
    repo, base = _target(tmp_path)
    sha = _commit(repo, {"tests/test_contest_runner.py":
                         _RUNNER.format(extra=", error_retry_backoff_sec=0")},
                  f"SLOW-1: base {base[:7]}, 300 s -> 240 s")
    r = _check(repo, sha)
    assert r.returncode == 1
    assert "PASS  cause 1" in r.stdout and "FAIL  cause 2" in r.stdout


def test_a_dropped_assert_and_a_bare_message_fail(tmp_path):
    repo, _base = _target(tmp_path)
    runner = _RUNNER.format(extra=", error_retry_backoff_sec=0").split("\n", 1)[1]
    runner = runner.replace("    assert make_config([])\n", "    make_config([])\n")
    sha = _commit(repo, {"tests/test_contest_runner.py": runner}, "faster")
    out = _check(repo, sha).stdout
    assert "FAIL  no touched test file lost an assert" in out
    assert "FAIL  the message names the base" in out
    assert "FAIL  the message gives times in seconds" in out


def test_the_reference_stays_out_of_the_target():
    script = (SLOW1 / "make_target.sh").read_text()
    assert "REFERENCE" not in script and "check_slow1" not in script
    ticket = next(SLOW1.glob("119-*.md")).read_text()
    for sha in ("b6464a1", "3144826", "4b5c8ee", "e8c6ad3", "share one session build"):
        assert sha not in ticket, sha
