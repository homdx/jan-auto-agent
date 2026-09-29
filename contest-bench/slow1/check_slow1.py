#!/usr/bin/env python3
"""contest-bench/slow1/check_slow1.py — SLOW-1: what does an entry's commit fix?

The mechanical half of judging round 119 against REFERENCE.md — no LLM, no
network, no timing (time the suite yourself, on one quiet machine):

* cause 1 — the runner harness sets ``error_retry_backoff_sec=0``;
* cause 2 — ``tests/test_contest_runner.py`` has no file-wide
  ``port_bound_http_servers`` mark;
* cause 3 (bonus) — the two collect test files build their index once;
* no test file the commit touches lost an ``assert``;
* the commit message names the base sha and seconds.

The base is the ticket commit (``SLOW-1: ticket``) below COMMIT.

Usage:  python3 contest-bench/slow1/check_slow1.py <TARGET> <COMMIT>
Exit code 0 when the regression (causes 1 and 2) is gone and the hard rules
hold, 1 otherwise. Cause 3 is reported, never required.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RUNNER_TESTS = "tests/test_contest_runner.py"
COLLECT_TESTS = ("tests/test_collect_already_safe_query.py", "tests/test_collect_gates.py")
TICKET_SUBJECT = "SLOW-1: ticket"


def _git(target: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=target, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _show(target: Path, rev: str, path: str) -> str:
    return _git(target, "show", f"{rev}:{path}")


def _make_config(src: str) -> str:
    m = re.search(r"^def make_config\(.*?(?=^def |\Z)", src, re.M | re.S)
    return m.group(0) if m else ""


def backoff_zero(src: str) -> bool:
    return re.search(r"error_retry_backoff_sec\s*=\s*0\b", _make_config(src)) is not None


def runner_off_port_group(src: str) -> bool:
    return re.search(r"^pytestmark\s*=.*port_bound_http_servers", src, re.M) is None


def collect_shared(target: Path, rev: str) -> bool:
    safe = _show(target, rev, COLLECT_TESTS[0])
    # the per-test rebuild is `index = _real_repo_index()` inside a test body
    return bool(safe) and "index = _real_repo_index()" not in safe


def _asserts(src: str) -> int:
    return len(re.findall(r"^\s*assert\b", src, re.M))


def _base(target: Path, commit: str) -> str:
    for line in _git(target, "log", "--format=%H %s", commit).splitlines():
        sha, _, subject = line.partition(" ")
        if subject == TICKET_SUBJECT:
            return sha
    return ""


def checks(target: Path, commit: str) -> list[tuple[bool, bool, str]]:
    """(ok, required, message) per check."""
    res: list[tuple[bool, bool, str]] = []
    sha = _git(target, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    if not sha:
        return [(False, True, f"{commit} is not a commit in {target}")]
    base = _base(target, sha)
    if not base:
        return [(False, True, f"no '{TICKET_SUBJECT}' commit below {sha[:12]}")]

    runner = _show(target, sha, RUNNER_TESTS)
    res.append((backoff_zero(runner), True,
                "cause 1: make_config sets error_retry_backoff_sec=0"))
    res.append((runner_off_port_group(runner), True,
                f"cause 2: {RUNNER_TESTS} has no file-wide port_bound_http_servers mark"))
    res.append((collect_shared(target, sha), False,
                "cause 3 (bonus): the collect index is built once, not per test"))

    touched = [p for p in _git(target, "diff", "--name-only", base, sha).splitlines()
               if p.startswith(("tests/", "tests_bugfix/")) and p.endswith(".py")]
    lost = []
    for p in touched:
        before, after = _show(target, base, p), _show(target, sha, p)
        if before and _asserts(after) < _asserts(before):
            lost.append(f"{p} {_asserts(before)}->{_asserts(after)}")
    res.append((not lost, True, "no touched test file lost an assert"
                + (f" — {', '.join(lost)}" if lost else "")))

    msg = _git(target, "log", "--format=%B", f"{base}..{sha}")
    res.append((base[:7] in msg, True, f"the message names the base {base[:7]}"))
    res.append((re.search(r"\d+(\.\d+)?\s*s\b", msg) is not None, True,
                "the message gives times in seconds"))
    return res


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("Usage:")[1].split("\n")[0].strip(), file=sys.stderr)
        return 2
    results = checks(Path(argv[0]).resolve(), argv[1])
    for ok, required, msg in results:
        print(f"{'PASS' if ok else ('FAIL' if required else 'MISS')}  {msg}")
    failed = sum(required and not ok for ok, required, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
