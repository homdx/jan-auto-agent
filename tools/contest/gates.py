"""tools/contest/gates.py — KC-5: the mechanical round scorecard, as importable code.

Before this module the scorecard lived inside a script under `scripts/`, so `tools/`
could not use it without a `sys.path` hack, and its output was a table row for the
operator after the round — not a decision the runner could send back mid-round.

The scoring logic moved here verbatim (`judge` renamed `judge_worktree`);
`scripts/judge_epic_round.py` is now a thin CLI wrapper over it and keeps its CLI
and its stdout/CSV bytes.

Hard gates (a `FAIL` here settles the round regardless of anything else):

  shrink       `CollectBridge._shrink` must be byte-identical to the base
  commits      exactly one commit for the ticket
  pushed       nothing may have reached a remote

Everything else is a column, not a verdict. `tools/contest/harvest.py` turns a row
into a `READY`/`REWORK` verdict with sentences an agent can act on.

Standard library only; the git call below goes through this repo's own
`tools.git_run` helper rather than a third-party dependency.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.git_run import run_git

__all__ = [
    "BRIDGE",
    "TEST_ROOTS",
    "declared_files",
    "extract_shrink",
    "git",
    "judge_worktree",
    "run_tests",
    "run_tests_detail",
    "ticket_for_round",
]

#: The gate-1 file the round is scored on — `_shrink` must stay byte-identical.
BRIDGE = "tools/auto/collect_bridge.py"

#: Four separate invocations — combining the roots collides in conftest.
TEST_ROOTS = ["tests", "tests_bugfix", ".smoke_tests", ".regression_tests"]

#: How many lines of pytest output `run_tests_detail` keeps for a failure.
FAIL_TAIL_LINES = 20


def git(cwd, *args, check=False):
    """`git <args>` in *cwd*, stdout stripped. Raise `RuntimeError` when *check*.

    Through `tools.git_run.run_git`, so a transient held index is waited out
    before the answer is read (FL-2). `check=False` callers still get `""` when
    git cannot answer after the ladder — that is their contract, and
    `judge_worktree` is written on it.
    """
    r = run_git(["git", *args], cwd=cwd)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def extract_shrink(cwd, rev):
    """The text of `def _shrink` at *rev*, or None if the file/def is absent."""
    src = subprocess.run(["git", "show", f"{rev}:{BRIDGE}"], cwd=cwd,
                         capture_output=True, text=True)
    if src.returncode:
        return None
    lines = src.stdout.splitlines()
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^\s*def _shrink\b", l):
            start = i
            break
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        body.append(l)
    return "\n".join(body).rstrip()


#: What a backticked span on a `**File:**` / `**Also touches:**` line must
#: look like to count as a path — the lines also carry symbols
#: (`` (`_is_ancestor`, `harvest`) ``) and config keys, which are not.
_PATH_ENDINGS = (".py", ".md", ".ini", ".sh", ".json", ".txt", ".gitignore", "/")


def _looks_like_path(span: str) -> bool:
    return "/" in span or span.endswith(_PATH_ENDINGS)


def _declared_paths(body: str) -> tuple[str, ...]:
    """The paths a ticket declares: the `**File:**` line plus `**Also touches:**`.

    Both lines are read the same way (KC-17): every backticked span that looks
    like a path, in order, `**File:**` first. A `**File:**` line without any
    backtick is one path, the whole value; a `—` placeholder declares nothing.
    """
    declared: list[str] = []
    for label in ("File", "Also touches"):
        m = re.search(rf"^\*\*{label}:\*\*\s*(.+?)\s*$", body, re.M)
        if not m:
            continue
        value = m.group(1)
        spans = re.findall(r"`([^`]+)`", value)
        if spans:
            declared += [s for s in spans if _looks_like_path(s)]
        elif label == "File" and value != "—":
            declared.append(value)
    return tuple(declared)


def declared_files(ticket_path) -> tuple[str, ...]:
    """The paths a ticket file declares — so the runner never re-parses tickets.

    The `**File:**` line first, then every backticked path on the
    `**Also touches:**` line; a `—` placeholder is dropped. Reading fails loud
    (`FileNotFoundError`) — a runner holding a bad ticket path is a bug, not a
    harvest result.
    """
    return _declared_paths(Path(ticket_path).read_text(encoding="utf-8"))


def ticket_for_round(tasks_dir, n):
    """`(filename, title, declared paths)` for the ticket numbered *n*, or None."""
    for name in sorted(os.listdir(tasks_dir)):
        m = re.match(r"^0*(\d+)-.*\.md$", name)
        if m and int(m.group(1)) == n:
            body = Path(os.path.join(tasks_dir, name)).read_text(encoding="utf-8")
            title = body.splitlines()[0].lstrip("# ").strip()
            return name, title, list(_declared_paths(body))
    return None, None, []


def run_tests(cwd):
    """One summary token per pytest root: `tests:PASS` / `tests:2✗` / `tests:absent`."""
    return run_tests_detail(cwd)[0]


#: A `FAILED` / `ERROR` line of pytest's short test summary: the node id, with
#: xdist's `@group` suffix (`--dist=loadgroup`) and the ` - message` dropped.
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:@[^\s\[]+)?(?: - .*)?$")


def _failures(lines: list[str]) -> tuple[int, list[str]]:
    """`(count, node ids)` of the failed and errored tests in a pytest run.

    The count is the stats line's (`3 failed, 1 error, 40 passed in 2s`) when
    pytest printed one; under `-qq` — an ini `-q` on top of a `-q` on the
    command line — it prints none, and the count is the number of short-summary
    lines instead, which `-rfE` (the default) always prints.
    """
    ids = []
    for line in lines:
        m = _SUMMARY_LINE.match(line)
        if m:
            ids.append(m.group(1))
    for line in reversed(lines):
        nfail = re.search(r"(\d+) failed", line)
        nerr = re.search(r"(\d+) error", line)
        if (nfail or nerr) and re.search(r" in [\d.]+s", line):
            return int(nfail.group(1) if nfail else 0) + int(nerr.group(1) if nerr else 0), ids
    return len(ids), ids


def _pytest(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", *args, "--timeout=180"],
                          cwd=cwd, capture_output=True, text=True)


def run_tests_detail(cwd) -> tuple[str, list[str]]:
    """Same four roots as `run_tests`, plus the tail of the failing output.

    Returns `(summary, tail_lines)`. The summary is one token per root:
    `PASS`, `absent`, `3✗` (failed + errored), `rc4✗` (pytest exited non-zero
    without a failed test — a usage or collection error, an interrupt), or
    `PASS*2`: two tests failed under the full run and passed when rerun alone
    (`-n0`, by node id) — a timing test that flakes under round load, not a
    failure of the tree. The rerun happens once per failing root and only for
    the tests that failed; a real failure stays `✗`.

    `tail_lines` carries, per root that did not pass, a `--- <root>` line and
    the last `FAIL_TAIL_LINES` lines of that root's output — the cause is at
    the tail, not the head, so that is what an agent gets — and, for a root
    that passed on rerun, one line naming the flaky tests.
    """
    out = []
    tail: list[str] = []
    serial = ["-n0"] if importlib.util.find_spec("xdist") else []
    for d in TEST_ROOTS:
        if not os.path.isdir(os.path.join(cwd, d)):
            out.append(f"{d}:absent")
            continue
        r = _pytest(cwd, d)
        if r.returncode == 0:
            out.append(f"{d}:PASS")
            continue
        lines = (r.stdout or "").strip().splitlines()
        bad, ids = _failures(lines)
        if bad and ids and _pytest(cwd, *ids, *serial).returncode == 0:
            out.append(f"{d}:PASS*{bad}")
            tail.append(f"--- {d}: {bad} test(s) failed under the full run and passed "
                        f"alone on rerun (flaky, not counted): {' '.join(ids)}")
            continue
        out.append(f"{d}:{bad}✗" if bad else f"{d}:rc{r.returncode}✗")
        tail.append(f"--- {d}")
        tail.extend(lines[-FAIL_TAIL_LINES:] or (r.stderr or "").strip().splitlines()[-FAIL_TAIL_LINES:])
    return " ".join(out), tail


def judge_worktree(name, path, base, declared, want_tests):
    """The mechanical scorecard row for one agent worktree — facts only.

    *name* is the agent label, *path* its checkout, *base* the ref every agent
    started from, *declared* the ticket's declared paths, *want_tests* whether to
    run the four pytest roots. A path that is not a git worktree yields a short row
    with only `gate`/`notes` — the caller must not index `commits` then.
    """
    row = {"agent": name, "path": path}
    if not os.path.isdir(os.path.join(path, ".git")) and not os.path.exists(os.path.join(path, ".git")):
        row["gate"] = "FAIL"
        row["notes"] = "not a git worktree"
        return row

    merge_base = git(path, "merge-base", base, "HEAD") or base
    commits = [l for l in git(path, "log", "--oneline", f"{merge_base}..HEAD").splitlines() if l]
    row["commits"] = len(commits)
    row["sha"] = commits[0].split()[0] if commits else "—"

    # ── hard gate 1: _shrink byte-identical ──────────────────────────────
    before, after = extract_shrink(path, merge_base), extract_shrink(path, "HEAD")
    if before is None or after is None:
        row["shrink"] = "?" if before is None else "GONE"
    else:
        row["shrink"] = "same" if before == after else "CHANGED"

    # ── hard gate 2: nothing pushed ──────────────────────────────────────
    # KC-20: with no agent commit HEAD *is* the base, and the base is on
    # `origin` in every real round — only the agent's own commits can have
    # been pushed, so a zero-commit worktree is never "pushed".
    if not commits:
        row["pushed"] = "no"
    else:
        remotes = git(path, "branch", "-r", "--contains", "HEAD")
        row["pushed"] = "yes" if remotes.strip() else "no"

    # ── diff shape ───────────────────────────────────────────────────────
    stat = git(path, "diff", "--numstat", f"{merge_base}..HEAD")
    files, add, dele = [], 0, 0
    for l in stat.splitlines():
        parts = l.split("\t")
        if len(parts) != 3:
            continue
        a, d, f = parts
        files.append(f)
        add += int(a) if a.isdigit() else 0
        dele += int(d) if d.isdigit() else 0
    row["files"] = len(files)
    row["+/-"] = f"+{add}/-{dele}"

    tests = [f for f in files if re.search(r"(^|/)tests?[_/]|/test_|^\.smoke_tests/|^\.regression_tests/", f)]
    row["test_files"] = len(tests)
    new_tests = 0
    for f in tests:
        blob = subprocess.run(["git", "show", f"HEAD:{f}"], cwd=path,
                              capture_output=True, text=True)
        if blob.returncode == 0:
            new_tests += len(re.findall(r"^\s*def test_", blob.stdout, re.M))
    row["test_funcs"] = new_tests

    if declared:
        outside = [f for f in files if f not in declared and not tests.count(f)]
        row["off_ticket"] = len(outside)
        row["off_ticket_files"] = ";".join(outside[:4])
    else:
        row["off_ticket"] = 0
        row["off_ticket_files"] = ""

    row["tests_run"] = run_tests(path) if want_tests else "—"

    gates = []
    if row["shrink"] == "CHANGED":
        gates.append("_shrink modified")
    if row["commits"] != 1:
        gates.append(f"{row['commits']} commits, expected 1")
    if row["pushed"] == "yes":
        gates.append("reached a remote")
    if row["test_files"] == 0:
        gates.append("no test shipped")
    row["gate"] = "FAIL" if gates else "ok"
    row["notes"] = "; ".join(gates)
    return row
