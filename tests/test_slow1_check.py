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

import pytest

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


def _judge_update_source(initial, patch_value, unreachable=False):
    if unreachable:
        body = (f"    kw = dict(error_retry_backoff_sec={initial})\n"
                f"    if False:\n        kw.update(error_retry_backoff_sec={patch_value})\n"
                "    return Cfg(**kw)\n")
    else:
        body = (f"    patch = dict(error_retry_backoff_sec={patch_value})\n"
                f"    kw = dict(error_retry_backoff_sec={initial})\n"
                "    kw.update(patch)\n    return Cfg(**kw)\n")
    return ("class Cfg:\n    def __init__(self, error_retry_backoff_sec):\n"
            "        self.error_retry_backoff_sec = error_retry_backoff_sec\n\n"
            "def make_config():\n" + body + "\ndef test_one():\n    assert make_config()\n")


def _judge_source(tmp_path, source):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, base = _target(tmp_path)
    _commit(repo, {"tests/test_contest_runner.py": source},
            f"SLOW-1: base {base[:7]}, 300 s -> 180 s")
    result = _check(repo, "HEAD")
    return result.returncode, result.stdout


def test_named_update_uses_call_position_through_public_judge(tmp_path):
    zero_source = _judge_update_source(initial=1, patch_value=0)
    namespace = {"pytest": pytest}
    exec(compile(zero_source, "fixture.py", "exec"), namespace)
    assert namespace["make_config"]().error_retry_backoff_sec == 0
    code, output = _judge_source(tmp_path / "update-zero", zero_source)
    assert code == 0, output
    assert "PASS  cause 1" in output

    one_source = _judge_update_source(initial=0, patch_value=1)
    namespace = {"pytest": pytest}
    exec(compile(one_source, "fixture.py", "exec"), namespace)
    assert namespace["make_config"]().error_retry_backoff_sec == 1
    code, output = _judge_source(tmp_path / "update-one", one_source)
    assert code == 1
    assert "FAIL  cause 1" in output


def test_unreachable_updates_are_excluded_through_public_judge(tmp_path):
    one_source = _judge_update_source(initial=1, patch_value=0, unreachable=True)
    namespace = {"pytest": pytest}
    exec(compile(one_source, "fixture.py", "exec"), namespace)
    assert namespace["make_config"]().error_retry_backoff_sec == 1
    code, output = _judge_source(tmp_path / "unreachable-one", one_source)
    assert code == 1
    assert "FAIL  cause 1" in output

    zero_source = _judge_update_source(initial=0, patch_value=1, unreachable=True)
    namespace = {"pytest": pytest}
    exec(compile(zero_source, "fixture.py", "exec"), namespace)
    assert namespace["make_config"]().error_retry_backoff_sec == 0
    code, output = _judge_source(tmp_path / "unreachable-zero", zero_source)
    assert code == 0, output
    assert "PASS  cause 1" in output


@pytest.mark.parametrize("latest", [0, 1])
@pytest.mark.parametrize("via_keyword", [False, True])
def test_update_uses_the_mapping_final_value_and_keyword_precedence(tmp_path, latest, via_keyword):
    argument = "patch, **override" if via_keyword else "patch"
    final_patch = 1 - latest if via_keyword else latest
    source = f"""def make_config():
    kw = dict(error_retry_backoff_sec={1 - latest})
    patch = dict(error_retry_backoff_sec={1 - final_patch})
    patch["error_retry_backoff_sec"] = {final_patch}
    override = dict(error_retry_backoff_sec={latest})
    kw.update({argument})
    return dict(**kw)

def test_one():
    assert make_config()
"""
    namespace = {}
    exec(compile(source, "fixture.py", "exec"), namespace)
    assert namespace["make_config"]()["error_retry_backoff_sec"] == latest
    code, output = _judge_source(tmp_path, source)
    assert code == (0 if latest == 0 else 1), output


def test_deep_expression_does_not_break_the_public_judge(tmp_path):
    expression = "+".join(["1"] * 1200)
    source = (f"def make_config():\n    _noise = {expression}\n"
              "    return dict(error_retry_backoff_sec=0)\n"
              "\ndef test_one():\n    assert make_config()\n")
    code, output = _judge_source(tmp_path, source)
    assert code == 0, output


def test_static_judge_keeps_supported_non_update_controls():
    from importlib.util import module_from_spec, spec_from_file_location

    path = SLOW1 / "check_slow1.py"
    spec = spec_from_file_location("slow1_checker_controls", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    controls = [
        "def make_config():\n    kw = dict(error_retry_backoff_sec=1)\n"
        "    kw.error_retry_backoff_sec = 0\n    return Cfg(**kw)",
        "def make_config():\n    kw: dict = dict(error_retry_backoff_sec=0)\n"
        "    return Cfg(**kw)",
        "async def make_config():\n    return Cfg(error_retry_backoff_sec=0)",
    ]
    for source in controls:
        assert module.backoff_zero(source)


def test_a_negated_constant_branch_is_decided_statically():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("slow1_checker_not", SLOW1 / "check_slow1.py")
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    template = ("def make_config():\n    kw = dict(error_retry_backoff_sec=1)\n    if {test}:\n"
                "        kw.update(error_retry_backoff_sec=0)\n    return Cfg(**kw)")
    assert not module.backoff_zero(template.format(test="not True"))
    assert not module.backoff_zero(template.format(test="not 1"))
    assert module.backoff_zero(template.format(test="not False"))
    assert module.backoff_zero(template.format(test="True"))


@pytest.mark.parametrize("statement, count, expected_exit", [
    ("if", 1200, 1),
    ("while", 1201, 0),
])
def test_deeply_negated_literal_keeps_the_judge_running(tmp_path, statement, count, expected_exit):
    condition = "not " * count + "True"
    source = ("def make_config():\n    kw = dict(error_retry_backoff_sec=0)\n"
              f"    {statement} {condition}:\n"
              "        kw.update(error_retry_backoff_sec=1)\n")
    if statement == "while":
        source += "        break\n"
    source += "    return dict(**kw)\n\ndef test_one():\n    assert make_config()\n"
    namespace = {}
    exec(compile(source, "fixture.py", "exec"), namespace)
    actual = namespace["make_config"]()["error_retry_backoff_sec"]
    assert (actual != 0) == bool(expected_exit)
    code, output = _judge_source(tmp_path, source)
    assert code == expected_exit, output
    assert ("PASS  cause 1" if expected_exit == 0 else "FAIL  cause 1") in output
