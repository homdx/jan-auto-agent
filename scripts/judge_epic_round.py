#!/usr/bin/env python3
"""Score one epic round across several agent worktrees, on facts only.

An epic round hands every agent the same ticket and the same starting tree.
This collects what can be checked mechanically, so the judgement call that is
left is about the design, not about whether the suite passes.

    # every worktree under runs/, round 1, no test execution
    python3 scripts/judge_epic_round.py --round 1 --runs runs/ --base main

    # explicit worktrees, and actually run the four pytest roots
    python3 scripts/judge_epic_round.py --round 1 --base competition \
        --worktree opus=/tmp/wt-opus --worktree sonnet=/tmp/wt-sonnet --tests

    # and the cross phase: every entry's own tests, run on every implementation
    python3 scripts/judge_epic_round.py --round 158 --base competition \
        --worktree agnes=/tmp/wt-a --worktree glm=/tmp/wt-g --cross \
        --ideal 4c1f0e2 --jobs 4

    # and the claims phase: the ticket's own claims, judged at the base and at each entry
    python3 scripts/judge_epic_round.py --round 296 --base competition \
        --runs contest-out/296/wt --cross --claims --claims-profiles opus sonnet haiku

Hard gates (a `FAIL` here settles the round regardless of anything else):

  shrink       `CollectBridge._shrink` must be byte-identical to `--base`
  commits      exactly one commit for the ticket
  pushed       nothing may have reached a remote

Everything else is a column, not a verdict. The scorecard is in
`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`; this script fills in its
mechanical half and prints the questions it cannot answer.

The cross phase (`--cross`, round 158) is a second table the judge used to
make by hand, with a shell loop: each entry's own new or changed tests, run
against every other entry's code, the base, and a candidate ideal. An entry's
tests find what the bench misses — round 151's cache that outlived the drop,
its linked worktree left with Kilo's own `.gitignore`.

The claims phase (`--claims`, CC-14) is the third: the ticket's own claims,
each judged at the base and at every entry that passed the hard gates, by the
same voters and rule `claim_diff.py` uses (`claim_diff.judge_both` is the call,
not a copy). The base side is voted once per round — every entry starts from the
same tree — and reused for the others, refused when the claims, the base sha, the
voters, the runs or the rule differ. Like the cross phase's `behaviour` class it
is a lead: a mismatch sets no exit code and fails no round, so read the evidence
and reproduce it by hand before a ticket is filed.

Exit codes: 0 scored · 1 usage / nothing to score · 2 nothing was crossed or
the claims phase refused before a request.

KC-5: the scoring itself lives in `tools/contest/gates.py` (`judge_worktree`)
so the contest runner can import it; this file is the operator CLI over it and
keeps its CLI and its stdout/CSV bytes.
"""
import argparse
import configparser
import csv
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest.gates import (  # noqa: E402
    judge_worktree,
    ticket_for_round,
)

#: The scripts dir, so the claims phase reuses `claim_diff`'s and `claim_vote`'s own code
#: rather than a second copy of the voting, the tally and the rule.
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import claim_diff as cd  # noqa: E402  (CC-7/CC-14: prepare_sides, judge_both, requests_of, the reuse)
import claim_vote as cv  # noqa: E402  (CC-6: ask_v2, tally, the ini and the pacer)
from tools.claimcheck import compare as cc_compare  # noqa: E402  (CC-7: the deltas, the expect)
from tools.claimcheck import target as cc_target  # noqa: E402  (the base and the entry's head)

# The cross phase: P's test files run in a scratch copy of I's tree, deleted afterwards; no worktree is written.

#: The three kinds of failure a cell can hold, in the order the operator reads them.
API = "api"               # a name or signature P has and I does not have
BASE = "base"             # the test fails on the base too: no one asked for it
BEHAVIOUR = "behaviour"   # a candidate bug in I, to be reproduced by hand

#: A cross cell is P's test on I's code, named _xcross_<P>_<file> so pytest still collects it.
_CROSS_NAME = "_xcross_"

#: Test roots an entry may use: tests/ and tests_bugfix/ (a judge that looked in one only scored zero cells, bug 16).
TEST_ROOTS = ("tests/", "tests_bugfix/")

#: The first test root, kept as the fallback for paths that name neither root.
TEST_ROOT = TEST_ROOTS[0]

#: Text for a matrix with no cells, printed in place of "no cell failed" and behind the exit code.
NOTHING_CROSSED = (
    f"cross: no entry changed a test under {' or '.join(TEST_ROOTS)} — nothing was crossed"
)

#: `--cross` with no cell to run: the phase did not judge the round.
EXIT_NO_CROSS = 2

#: The cell's own suite runs with this many xdist workers, per the ticket.
CROSS_WORKERS = 4

#: Seconds of wall clock one cell may spend before it is ended by group.
CROSS_CELL_TIMEOUT = 300.0

#: Node id from pytest's short-summary line; the xdist group and the message are dropped.
_SUMMARY_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:@[^\s\[]+)?(?: - .*)?$")

#: pytest's section header, including the single-underscore form for long names.
_SECTION = re.compile(r"^_+ (.+?) _+\s*$")

#: The first `E ` line of a traceback is the failure; the rest is context.
_E_LINE = re.compile(r"^E\s")

#: …unless it is a frame (`E     File "x.py", line 3`): the failure comes last.
_E_FRAME = re.compile(r'^E\s+File "')

#: Import and signature errors (ImportError, AttributeError, TypeError) mean a different implementation, not a bug in I.
_API_ERR = re.compile(
    r"\b(?:ImportError|ModuleNotFoundError|AttributeError|TypeError)\b"
    # A test that runs another entry's CLI fails on argparse's usage banner, so the pattern is kept.
    r"|\busage: \S+ \[-h\]",
    re.I)

#: Pytest's stats line, the last line with a count and a duration.
_STATS = re.compile(r"(\d+) (passed|failed|errors?|skipped)")

#: The stats line ends in ====; the duration is searched for, never anchored.
_HAS_DURATION = re.compile(r"\d+(?:\.\d+)?\s*[smh]")


def _slug(name):
    """*name* as a filename fragment: `agnes-2.5-flash` → `agnes-2.5-flash`."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:40].strip("._")
    return s or "x"


def _say(*args):
    """`print` that flushes: the cross phase talks while it works."""
    print(*args, flush=True)


def _modname(text):
    """*text* as part of a Python module name: letters, digits and `_` only."""
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_")
    return s or "x"


def _root_tag(root):
    """The part of *root* that goes into a copied test's module name.

    `tests/` needs none: the file sits in the directory its name already
    stands for. Any other root does, because pytest imports a test file by its
    basename alone — two files called `_xcross_p_x.py`, one per root, make the
    second one `import file mismatch` and the whole cell is a collection error
    instead of two suites. `tests_bugfix/` → `tests_bugfix`.
    """
    return "" if root == TEST_ROOT else _modname(root)


def _git(cwd, *args):
    """`git <args>` in *cwd* as `(returncode, stdout)`; never raises.

    The cross phase reads trees it does not own, so a tree git cannot read is
    a cell that says so — not an exception into the run.
    """
    try:
        # core.quotepath=false: a non-ASCII test path came back C-quoted and was no test of a root
        p = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout.strip()


def changed_tests(path, base):
    """The test files *path* added or changed against *base* — `[]` if unreadable.

    `git diff --name-only <base> -- tests/ tests_bugfix/`: base against the
    worktree, so a test an agent left uncommitted is crossed too. A path in
    neither root is not a test, and one root missing from the tree is not an
    error — git ignores an unmatched pathspec — so a round whose whole suite
    is in `tests_bugfix/` is still seen. Anything git cannot name is dropped,
    which is the fail-open side of the ticket.
    """
    try:
        rc, out = _git(path, "diff", "--name-only", "--no-renames", base,
                       "--", *TEST_ROOTS)
    except Exception:
        return []
    if rc:
        return []
    return [f for f in out.splitlines()
            if f.lower().endswith(".py") and any(f.startswith(r) for r in TEST_ROOTS)]


def _resolve(repos, ref):
    """The first *repo* that resolves *ref* to a commit, else `None`.

    An entry's `--base` may be a branch only some checkouts know, so the ref
    is probed in every candidate rather than assumed.
    """
    for repo in repos:
        rc, out = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if not rc or out:
            return repo
    return None


def _materialize(repo, ref, dest):
    """`git archive <ref>` from *repo* into *dest*: `(ok, note)`.

    An archive, not a worktree: it reads the object store and writes only to
    *dest*, so no entry's checkout gains a `__pycache__` or a stray file. The
    tar is streamed, not buffered — a real round's archive is far bigger than
    the box's spare memory is worth spending on.
    """
    cmd = ["git", "-C", str(repo), "archive", "--format=tar", str(ref)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return False, f"git archive {ref} did not run: {type(e).__name__}"
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as t:
            t.extractall(dest)
        stderr = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
        rc = proc.wait(timeout=600)
    except (OSError, tarfile.TarError, ValueError, subprocess.SubprocessError) as e:
        _end_group(proc)
        return False, f"git archive {ref} failed: {type(e).__name__}: {e}"
    if rc:
        return False, f"git archive {ref} failed: {stderr[-1] if stderr else 'no message'}"
    return True, ""


def _copy_tests(entry, files, dest, label):
    """P's changed test files into a scratch I, keeping each one's own root.

    `tests/x.py` becomes `tests/_xcross_<P>_x.py`; `tests_bugfix/x.py` becomes
    `tests_bugfix/_xcross_<P>_tests_bugfix_x.py`. The root stays in the path,
    and it also goes into the module name, because pytest imports a test file
    by its basename alone — a basename that exists in both roots is two files
    and one collection error without it. The directory the file came from is
    kept for the same reason.

    Returns the relative paths copied — `[]` when none of them is readable,
    which is a cell that says so rather than an empty suite that says `0/0`.
    """
    out = []
    for rel in files:
        src = os.path.join(entry, rel)
        if not os.path.isfile(src):
            continue  # the diff named a file the worktree does not carry
        root, rest = next(((r, rel[len(r):]) for r in TEST_ROOTS if rel.startswith(r)),
                          (TEST_ROOT, rel))
        # Keep the path below the root in the module name, so tests/a/test_x.py and tests/b/test_x.py stay distinct.
        stem = rest
        tag = _root_tag(root)
        # Module names must be valid identifiers (no "." or "/"); the root is included when it is not the file's own.
        name = (f"{_CROSS_NAME}{_modname(label)}"
                + (f"_{tag}_" if tag else "_")
                + f"{_modname(os.path.splitext(stem)[0])}.py")
        dst = os.path.join(dest, root, name)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            out.append(os.path.join(root, name))
        except OSError:
            continue
    return out


def _run(cmd, cwd, timeout, env=None):
    """*cmd* in *cwd*, bounded to *timeout* s: `(rc, stdout, stderr)`.

    The cell's own suite gets its own session, so a suite that hangs is ended
    by process group — pytest and every xdist worker under it, not just the
    parent — and the cell reports `rc 124` instead of the judge hanging too.
    """
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env,
                                start_new_session=True)
    except (OSError, ValueError) as e:
        return 127, "", f"could not start pytest: {e}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _end_group(proc)
        return 124, "", f"the cell ran past {int(timeout)} s and was ended"
    except OSError as e:
        _end_group(proc)
        return 127, "", f"could not read pytest's output: {e}"
    return proc.returncode, out or "", err or ""


def _end_group(proc):
    """End the cell's process group, leader and xdist workers: TERM, a moment, KILL.

    The KILL goes out whatever became of the leader. A leader that exits on the
    TERM leaves its workers a little longer, and a worker that is mid-`sleep` in
    a hung test must not outlive its cell — "no process left" is part of "no
    file left". The leader has not been reaped yet, so its pid is still the
    group's id and cannot belong to anyone else. Fail-open on every race.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass  # the group is already gone, or is not ours to signal
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass


def _run_pytest(code_root, files, basetemp, timeout, workers=CROSS_WORKERS):
    """P's copies inside I's scratch: `(rc, stdout, stderr)`; never raises.

    `-n 4` per the ticket, with a private basetemp so the cell's temp files
    are short to name and delete with the scratch copy. Both plugins are
    looked up before their flags are passed: without xdist `-n` is a usage
    error, and without pytest-timeout `--timeout` exits 4.
    """
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=short",
           "-rfE", "--basetemp", basetemp]
    if workers > 0 and importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", str(workers)]
    if importlib.util.find_spec("pytest_timeout") is not None:
        cmd += ["--timeout", str(max(1, int(timeout)))]
    cmd += list(files)
    # The cell's temp files live inside the cell, so nothing outlives the scratch copy.
    cell_tmp = os.path.join(code_root, ".cell_tmp")
    os.makedirs(cell_tmp, exist_ok=True)
    return _run(cmd, code_root, timeout, env={**os.environ, "TMPDIR": cell_tmp})


def _counts(out):
    """`(passed, total)` from pytest's stats line, or `(None, None)`.

    `None` means pytest never reached its own summary — a usage error, an
    interrupted run — and the cell then says why instead of showing `0/0`.
    The last matching line wins: pytest prints it after its own summary.
    """
    for line in reversed((out or "").splitlines()):
        if _STATS.search(line) and _HAS_DURATION.search(line):
            c = {}
            for n, k in _STATS.findall(line):
                c[k] = c.get(k, 0) + int(n)
            return c.get("passed", 0), sum(c.values())
    return None, None


def _sections(out):
    """`[(test name, first E line)]` of every section that carries an `E ` line.

    pytest prints a `_ … name … _` header before each failure and each
    collection error; sections with no `E ` line are headings, not tests, and
    are dropped so the index still lines up with the short summary.
    """
    sections = []
    name, body = None, []
    for line in (out or "").splitlines():
        m = _SECTION.match(line)
        if m:
            if name is not None:
                sections.append((name, body))
            name, body = m.group(1).strip(), []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        sections.append((name, body))
    keep = []
    for n, b in sections:
        lines = [l.strip() for l in b if _E_LINE.match(l)]
        if not lines:
            continue
        # On an import failure the error is the last E line; otherwise it is the first.
        keep.append((n, lines[-1] if _E_FRAME.match(lines[0]) else lines[0]))
    return keep


#: Section header kinds that pytest prints before a test name, other than a plain failure.
_SECTION_KIND = re.compile(r"^ERROR (?:at (?:setup|teardown) of|collecting)\s+")


def _pair(ids, out):
    """`[(node id, first E line)]` — each failed test with the line that caused it.

    Matched by name: a section is headed by the test's own name (`test_x`,
    `test_x[param]`), by `ERROR at setup of test_x`, or — for a module that
    never imported — by `ERROR collecting <file>`, which is the whole node id.
    No match is no line. Nothing is matched by position: xdist prints the
    sections in the order its workers finish, not in the order of the short
    summary, and an E line borrowed from another test turns a fixture error
    into a false `behaviour` (a failure with no line cannot be `api`, so a
    wrong or missing line is exactly what makes a lead out of a non-lead). When
    the one section left is the one node left, they are the same failure.
    """
    secs = [(_SECTION_KIND.sub("", n).strip(), e) for n, e in _sections(out)]
    used = set()
    res = {}
    for node in ids:
        short = node.split("::")[-1]
        for k, (key, line) in enumerate(secs):
            if k in used:
                continue
            if key in (short, node) or key.endswith(("::" + short, "." + short)) or node.endswith("::" + key):
                used.add(k)
                res[node] = line
                break
    rest_nodes = [n for n in ids if n not in res]
    rest_secs = [s for k, s in enumerate(secs) if k not in used]
    if len(rest_nodes) == 1 and len(rest_secs) == 1:
        res[rest_nodes[0]] = rest_secs[0][1]
    return [(n, res.get(n, "")) for n in ids]


def _fails(out):
    """The failed and errored node ids, in pytest's own order."""
    return [m.group(1) for line in (out or "").splitlines()
            if (m := _SUMMARY_FAIL.match(line))]


def _classify(node, line, base_ids):
    """`api`, `base`, or `behaviour` for one failure.

    `api` first: a test that cannot even import on I is a different
    implementation, whatever the base does. Then `base` — the test fails
    before anyone was asked to write the code. Everything left is a candidate
    bug, and stays a lead until it is reproduced by hand.
    """
    if _API_ERR.search(line or ""):
        return API
    if node in base_ids:
        return BASE
    return BEHAVIOUR


def _cell_str(cell):
    """A cell's `passed/total`, or `n/a` when the cell never ran."""
    if cell.get("total") is None:
        return "n/a"
    return f"{cell['passed']}/{cell['total']}"


def _cross_cell(p_name, p_path, files, i_name, i_ref, i_repo, cell_timeout, workers):
    """One cell, P's tests on I's code. A dict; never raises.

    `total` is `None` for a cell that could not be run at all — a ref git
    cannot archive, a test file that is not readable — and the `note` says
    which. Those read as `n/a` and are not classified, because there was no
    run to classify.
    """
    # The scratch dir name avoids xcross-, which a test of another entry may sweep (rc 127).
    scratch = tempfile.mkdtemp(prefix=f"judgecell-{_slug(p_name)}-{_slug(i_name)}-")
    basetemp = os.path.join(scratch, ".pytest_basetemp")
    try:
        ok, note = _materialize(i_repo, i_ref, scratch)
        if not ok:
            return {"passed": None, "total": None, "failures": [], "note": note}
        copied = _copy_tests(p_path, files, scratch, _slug(p_name))
        if not copied:
            return {"passed": None, "total": None, "failures": [],
                    "note": "none of P's changed test files was readable"}
        rc, out, err = _run_pytest(scratch, copied, basetemp, cell_timeout, workers)
        passed, total = _counts(out)
        fails = _pair(_fails(out), out)
        if passed is None and not fails and rc not in (124, 127):
            # No verdict (e.g. a worker died before collecting): retry once in a clean basetemp before calling the cell n/a.
            shutil.rmtree(basetemp, ignore_errors=True)
            rc, out, err = _run_pytest(scratch, copied, basetemp, cell_timeout, workers)
            passed, total = _counts(out)
            fails = _pair(_fails(out), out)
        if passed is None and not fails:
            why = (err or out or "").strip().splitlines()
            return {"passed": None, "total": None, "failures": [],
                    "note": f"pytest rc {rc}: {(why[-1] if why else 'no output')[:200]}"}
        return {"passed": passed, "total": total if total is not None else len(fails),
                "failures": [{"node": n, "line": l} for n, l in fails],
                "note": ""}
    except Exception as e:  # a cell that dies still reports, the run keeps going
        return {"passed": None, "total": None, "failures": [],
                "note": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _render_matrix(providers, impls, by):
    """The matrix as console lines: rows are whose tests, columns whose code."""
    rows = [p[0] for p in providers]
    cols = [i[0] for i in impls]
    head = "tests \\ code"
    w0 = max(len(head), *(len(r) for r in rows)) if rows else len(head)
    wc = [max(len(c), *(len(_cell_str(by[(r, c)])) for r in rows))
          if rows else len(c) for c in cols]
    lines = ["  ".join([head.ljust(w0)] + [c.ljust(wc[i]) for i, c in enumerate(cols)])]
    for r in rows:
        lines.append("  ".join([r.ljust(w0)]
                               + [_cell_str(by[(r, c)]).ljust(wc[i])
                                  for i, c in enumerate(cols)]))
    return lines


def _cells_in_order(providers, impls, by):
    """`(name, code, cell)` for every cell, in matrix order; the absent are skipped."""
    for p in providers:
        for i in impls:
            cell = by.get((p[0], i[0]))
            if cell is not None:
                yield p[0], i[0], cell


def _leads(providers, impls, by):
    """What the operator reads first, out of the cells.

    `behaviour` — a test the base passes that fails on this code: a candidate
    bug, reproduced by hand before a ticket. `discriminating` — a test that
    fails on the base but is satisfied by some code and not by other code: not a
    finding in the code under test (the base fails it too, so the class is
    `base`), but it tells who did what the test asked for and who did not; the
    cells that never got as far as the test (a module that does not import,
    a cell that did not run) are neither.
    """
    behaviour, discriminating = [], []
    for p in providers:
        pn = p[0]
        for i in impls:
            cell = by.get((pn, i[0]))
            for f in (cell or {}).get("failures", []):
                if f.get("kind") == "behaviour":
                    behaviour.append({"tests": pn, "code": i[0], "node": f["node"], "line": f["line"]})
        base_cell = by.get((pn, "base"))
        if not base_cell or base_cell["total"] is None:
            continue
        for f in base_cell["failures"]:
            if f.get("kind") != "base" or "::" not in f["node"]:
                continue
            fails_on, passes_on = [], []
            for i in impls:
                if i[0] == "base":
                    continue
                cell = by.get((pn, i[0]))
                if not cell or cell["total"] is None:
                    continue
                nodes = {x["node"] for x in cell["failures"]}
                if any("::" not in n for n in nodes):
                    continue            # the module did not import: the test never ran there
                (fails_on if f["node"] in nodes else passes_on).append(i[0])
            if fails_on and passes_on:
                discriminating.append({"tests": pn, "node": f["node"], "line": f["line"],
                                       "fails_on": fails_on, "passes_on": passes_on})
    return {"behaviour": behaviour, "discriminating": discriminating}


def _render_leads(leads, md=False):
    """The leads block: `behaviour` first, then the tests that tell the entries apart."""
    out = []
    head = (lambda s: f"### {s}") if md else (lambda s: f"  {s}")
    ind = "" if md else "  "
    item = (lambda s: f"- {s}") if md else (lambda s: f"    {s}")
    out.append(head("behaviour — a test the base passes, failing on this code"))
    if not leads["behaviour"]:
        out.append(item("none"))
    for b in leads["behaviour"]:
        out.append(item(f"{b['tests']} on {b['code']}: {b['node']}"))
        if b["line"]:
            out.append(ind + ("  " if md else "      ") + b["line"])
    out.append(head("discriminating — fails on the base, passes on some code, fails on other"))
    if not leads["discriminating"]:
        out.append(item("none"))
    for d in leads["discriminating"]:
        out.append(item(f"{d['tests']}: {d['node']}"))
        out.append(ind + ("  " if md else "      ") + f"fails on: {', '.join(d['fails_on'])}")
        out.append(ind + ("  " if md else "      ") + f"passes on: {', '.join(d['passes_on'])}")
        if d["line"]:
            out.append(ind + ("  " if md else "      ") + d["line"])
    return out


def _render_failures(providers, impls, by):
    """Under the matrix: the cells that failed or never ran, and why.

    A cell that never ran has no failing tests to name, only its note — so it
    shows up here too, or the `n/a` would have no explanation next to it.
    """
    lines = []
    for name, code, cell in _cells_in_order(providers, impls, by):
        if not cell.get("failures") and not cell.get("note"):
            continue
        lines.append(f"  {name} on {code}: {_cell_str(cell)}")
        for f in cell["failures"]:
            lines.append(f"    {f['kind']:<11} {f['node']}")
            if f["line"]:
                lines.append(f"                {f['line']}")
        if cell.get("note"):
            lines.append(f"    note        {cell['note']}")
    return lines


def _render_failures_md(providers, impls, by):
    """The same block as a markdown list; the `E ` line is the operator's read."""
    lines = []
    for name, code, cell in _cells_in_order(providers, impls, by):
        if not cell.get("failures") and not cell.get("note"):
            continue
        lines.append(f"- **{name} on {code}: {_cell_str(cell)}**")
        for f in cell["failures"]:
            lines.append(f"  - `{f['kind']}` `{f['node']}`")
            if f["line"]:
                lines.append(f"      {f['line']}")
        if cell.get("note"):
            lines.append(f"  - note: {cell['note']}")
    return lines


def _render_markdown(providers, impls, by, base, ideal, ticket=None, round_no=None,
                     skipped=None):
    """The same two views as `cross.md`."""
    rows = [p[0] for p in providers]
    cols = [i[0] for i in impls]
    lines = ["# Cross — every entry's own tests, run on every implementation", ""]
    if round_no is not None:
        lines.append(f"- round: {round_no}")
    if ticket:
        lines.append(f"- ticket: {ticket}")
    skipped = skipped or {}
    gone = " — resolves in no checkout, no column"
    lines.append(f"- base: `{base}`" + (gone if "base" in skipped else ""))
    lines.append((f"- ideal: `{ideal}`" + (gone if "ideal" in skipped else ""))
                 if ideal else "- ideal: —")
    lines.append("")
    if not rows:
        # A matrix with no rows is not a quiet one; nothing ran, so it says so.
        lines.append(NOTHING_CROSSED)
        lines.append("")
        return "\n".join(lines)
    lines.append("Rows are whose tests, columns are whose code; a cell is `passed/total`,")
    lines.append("`n/a` when the cell never ran. `api` is a name or signature only one")
    lines.append("implementation has, `base` also fails on the base, `behaviour` is a")
    lines.append("candidate bug: reproduce it by hand before a ticket is filed.")
    lines.append("")
    lines.append("| tests \\ code | " + " | ".join(cols) + " |")
    lines.append("|" + "|".join([" --- "] * len(cols)) + "|")
    for r in rows:
        lines.append("| " + r + " | "
                     + " | ".join(_cell_str(by[(r, c)]) for c in cols) + " |")
    lines.append("")
    lines.append("## Leads")
    lines.append("")
    lines.extend(_render_leads(_leads(providers, impls, by), md=True))
    lines.append("")
    lines.append("## Failures")
    lines.append("")
    detail = _render_failures_md(providers, impls, by)
    if detail:
        lines.extend(detail)
    else:
        lines.append("None — no cell failed, and none went unrun.")
    lines.append("")
    return "\n".join(lines)


def cross_tests(trees, base, ideal=None, jobs=1, cell_timeout=CROSS_CELL_TIMEOUT,
                workers=CROSS_WORKERS, out_dir=None, round_no=None, ticket=None,
                log=_say):
    """Run every entry's own tests against every implementation, and print the matrix.

    *trees* is `[(name, path)]`, *base* the ref every entry started from and
    *ideal* an optional candidate-ideal ref. Rows are the entries that added
    or changed test files, columns are every entry plus the base plus the
    ideal. Each cell runs in its own scratch copy of the code, with its own
    basetemp, and both are deleted before the next cell: nothing lands in an
    entry's worktree and nothing is left behind.

    Cells run one at a time — the box runs one suite at a time — unless
    *jobs* says otherwise. Writes `<out_dir>/cross.json` and `cross.md` when
    *out_dir* is given; a directory that cannot be created only loses the
    artifacts, never the run. Returns the data it wrote, and never raises.
    """
    try:
        return _cross_tests(trees, base, ideal=ideal, jobs=jobs,
                            cell_timeout=cell_timeout, workers=workers,
                            out_dir=out_dir, round_no=round_no, ticket=ticket, log=log)
    except Exception as e:  # the cross phase never takes the run down with it
        log(f"cross: the phase did not finish — {type(e).__name__}: {e}")
        return {"round": round_no, "ticket": ticket, "base": base, "ideal": ideal,
                "skipped": {}, "tests": [], "impls": [], "cells": []}


def _append_summary(out_dir):
    """Add the one line to `SUMMARY.md` in *out_dir*, when one is there: `""` otherwise.

    The round's runner owns that file, so it is only ever appended to, never
    rewritten; the line is not repeated if it is already there. A file that
    cannot be read or written is left alone — the line still stands in
    cross.md and on stdout, and the run never dies over a summary.
    """
    path = os.path.join(out_dir, "SUMMARY.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if NOTHING_CROSSED in text:
            return ""
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + NOTHING_CROSSED + "\n")
        return ", SUMMARY.md"
    except OSError:
        return ""


def _cross_empty(impls, skipped, base, ideal, out_dir, round_no, ticket, log):
    """No entry changed a test the judge can see: nothing was crossed.

    Not a clean matrix — a matrix that ran and failed nothing prints "no cell
    failed", and a judge reading only the last line took a zero-cell run for a
    judged round. The line goes to stdout twice, into cross.md and, when one is
    there, into the round's SUMMARY.md; the exit code is 2.
    """
    log(NOTHING_CROSSED)
    data = {"round": round_no, "ticket": ticket, "base": base, "ideal": ideal,
            "skipped": skipped,
            "tests": [], "impls": [i[0] for i in impls],
            "leads": {"behaviour": [], "discriminating": []}, "leads_count": 0,
            "nothing_crossed": True, "note": NOTHING_CROSSED, "cells": []}
    md = _render_markdown([], impls, {}, base, ideal, ticket, round_no, skipped)
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "cross.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
            with open(os.path.join(out_dir, "cross.md"), "w", encoding="utf-8") as fh:
                fh.write(md + "\n")
            log(f"\ncross -> {os.path.join(out_dir, 'cross.json')}, "
                f"{os.path.join(out_dir, 'cross.md')}{_append_summary(out_dir)}")
        except OSError as e:
            log(f"cross: could not write {out_dir} — {e} (the matrix above stands)")
    data["markdown"] = md
    log("")
    log(NOTHING_CROSSED)
    return data


def _cross_tests(trees, base, ideal=None, jobs=1, cell_timeout=CROSS_CELL_TIMEOUT,
                 workers=CROSS_WORKERS, out_dir=None, round_no=None, ticket=None,
                 log=_say):
    repos = [p for _, p in trees if os.path.isdir(os.path.join(str(p), ".git"))
             or os.path.exists(os.path.join(str(p), ".git"))]

    providers = []
    for name, path in trees:
        files = changed_tests(path, base)
        if files:
            providers.append((name, str(path), files))

    impls = [(name, "HEAD", str(path)) for name, path in trees]
    # A worktree with the same name is the column itself; the ref is not archived again under that name.
    named = {name for name, _ in trees}
    # Refs are looked up in the entries' checkouts, then the judge's own repo; a ref found nowhere is reported, not dropped.
    homes = list(repos)
    if os.path.isdir(REPO_ROOT) and not any(
            os.path.realpath(str(r)) == os.path.realpath(REPO_ROOT) for r in homes):
        homes.append(REPO_ROOT)
    skipped = {}
    for column, ref in (("base", base), ("ideal", ideal)):
        if not ref or column in named:
            continue
        home = _resolve(homes, ref)
        if home:
            impls.append((column, str(ref), home))
        else:
            skipped[column] = str(ref)
            log(f"cross: --{column} {ref} resolves in no checkout (the entries' or "
                f"{REPO_ROOT}) — no {column} column")

    log(f"\nCross — every entry's own tests, run on every implementation "
        f"(cells: {len(providers)} × {len(impls)})")
    if not providers:
        return _cross_empty(impls, skipped, base, ideal, out_dir, round_no, ticket, log)

    # One line per finished cell, so a long run does not look hung.
    total = len(providers) * len(impls)
    cells = []

    def _done(pn, in_, cell):
        cells.append((pn, in_, cell))
        log(f"  [{len(cells)}/{total}] {pn} on {in_}: {_cell_str(cell)}")

    if jobs and jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [(p[0], i[0], ex.submit(_cross_cell, p[0], p[1], p[2], i[0], i[1], i[2],
                                           cell_timeout, workers))
                    for p in providers for i in impls]
            for pn, in_, f in futs:
                _done(pn, in_, f.result())
    else:
        for p in providers:
            for i in impls:
                _done(p[0], i[0], _cross_cell(p[0], p[1], p[2], i[0], i[1], i[2],
                                              cell_timeout, workers))

    by = {(pn, in_): c for pn, in_, c in cells}

    # `base` classification reads the same row's cell on the base column.
    on_base = {pn: {f["node"] for f in by.get((pn, "base"), {}).get("failures", [])}
               for pn, _, _ in cells}
    for pn, in_, c in cells:
        kinds = set()
        for f in c["failures"]:
            f["kind"] = _classify(f["node"], f["line"], on_base.get(pn, ()))
            kinds.add(f["kind"])
        c["kinds"], c["tests"], c["code"] = sorted(kinds), pn, in_

    matrix = _render_matrix(providers, impls, by)
    log("\n".join(matrix))
    leads = _leads(providers, impls, by)
    log("")
    log("Leads — read these first:")
    log("\n".join(_render_leads(leads)))
    detail = _render_failures(providers, impls, by)
    if detail:
        log("")
        log("\n".join(detail))
    else:
        log("\nno cell failed")

    data = {"round": round_no, "ticket": ticket, "base": base, "ideal": ideal,
            "skipped": skipped,
            "tests": [p[0] for p in providers],
            "impls": [i[0] for i in impls],
            "leads": leads,
            # The counts in cross.json are the lengths of the lists, so they always agree.
            "leads_count": len(leads["behaviour"]) + len(leads["discriminating"]),
            "nothing_crossed": False,
            "cells": [{"tests": pn, "code": in_, "passed": c["passed"], "total": c["total"],
                       "note": c["note"], "kinds": c["kinds"],
                       "failures": c["failures"]}
                      for pn, in_, c in cells]}

    md = _render_markdown(providers, impls, by, base, ideal, ticket, round_no, skipped)
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "cross.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
            with open(os.path.join(out_dir, "cross.md"), "w", encoding="utf-8") as fh:
                fh.write(md + "\n")
            log(f"\ncross -> {os.path.join(out_dir, 'cross.json')}, "
                f"{os.path.join(out_dir, 'cross.md')}")
        except OSError as e:
            log(f"cross: could not write {out_dir} — {e} (the matrix above stands)")
    data["markdown"] = md
    return data


# ── the claims phase (CC-14) ─────────────────────────────────────────────────

#: `--claims` with no FILE: the claims come from the round's own ticket.
CLAIMS_FROM_TICKET = object()

#: The four words a `## Claims` bullet names: `claim_diff`'s expects.
CLAIM_WORDS = ("new", "fixed", "still", "gone")

#: The changes the claims table counts, in the order the operator reads them.
CLAIM_COUNTS = ("FIXED", "STILL", "NEW", "GONE", "UNCLEAR", "n/a")

#: Seconds one voter call may spend — the claims phase does not outwait `claim_diff`.
CLAIMS_TIMEOUT = 240

#: Voter calls in flight at once, both sides together.
CLAIMS_PARALLEL = 4

#: One line instead of an empty table: every entry failed a hard gate.
CLAIMS_NOTHING = "claims: no entry passed the hard gates — nothing was judged"

#: What the table is worth: a column next to the cross phase, never a hard gate.
CLAIMS_LEAD = ("claims: a lead, not a gate — a mismatch is a model's reading, so read the evidence "
               "and reproduce it by hand before a ticket")

#: `## Claims` — the optional section that carries the round's claims, after `## Acceptance`.
#: A heading that adds a parenthetical is still the section, so the match stops at the word.
_CLAIMS_HEADING = re.compile(r"^##\s+Claims(?:\s+.*)?$", re.I)

#: The heading that ends any section body.
_H2 = re.compile(r"^##\s+")

#: `- [new] text` — the bracketed word is the claim's `expect`, the rest is the claim itself.
#: The token is taken as written, so a mangled one like `- [ne]w` reports `[ne]w`, not `ne`.
_CLAIM_BULLET = re.compile(r"^\s*[-*+]\s+(\S+)\s+(.*)$")

#: A list item at all: a claim bullet that names no word is a refusal, not a silent skip.
_LIST_ITEM = re.compile(r"^\s*[-*+]\s+")


def _claims_section(text):
    """`[(line number, line)]` of a ticket's `## Claims` section — `[]` when it has none.

    The section is optional and may be the ticket's last, so the body runs to the next `## `
    heading or to the end of the file. Line numbers start at 1, the way an editor numbers them:
    a refusal names the line, not a position in a list."""
    lines = (text or "").split("\n")
    start = None
    for i, line in enumerate(lines):
        if _CLAIMS_HEADING.match(line):
            start = i + 1
            break
    if start is None:
        return []
    body = []
    for i in range(start, len(lines)):
        if _H2.match(lines[i]):
            break
        body.append((i + 1, lines[i]))
    return body


def ticket_claims(text, name=""):
    """The claims of a ticket's `## Claims` section as `[{"claim", "expect"}]`, `[]` when it has none.

    The word in brackets is the claim's `expect`, one of the four: `new` states the behaviour the
    ticket adds, `fixed` states the defect (true at the base, false after the fix), `still` what
    must not change and `gone` what must not be there. Any other word is a refusal naming the line
    — a typo would otherwise leave claims that can never be met, and an `expect` that never matches
    is the gate that looks like it checked. A bullet whose first word carries no brackets is refused
    too: the section is a claims list, not prose. An indented line after a bullet is its
    continuation, so a long claim may wrap without becoming a second claim.
    """
    where = name or "ticket"
    claims = []
    for lineno, line in _claims_section(text):
        bullet = _CLAIM_BULLET.match(line)
        if bullet:
            token, claim = bullet.group(1), bullet.group(2).strip()
            if not token.startswith("["):
                raise ValueError(f"{where}:{lineno}: a claim names its word in brackets — "
                                 f"{', '.join('[' + w + ']' for w in CLAIM_WORDS)}")
            word = token.strip("[]").strip().lower()
            if word not in CLAIM_WORDS:
                raise ValueError(f"{where}:{lineno}: '{token}' is not one of "
                                 f"{', '.join(CLAIM_WORDS)} — the word in brackets is the claim's expect")
            claims.append({"claim": claim, "expect": word})
        elif _LIST_ITEM.match(line) and line.strip():
            raise ValueError(f"{where}:{lineno}: a claim names its word in brackets — "
                             f"{', '.join('[' + w + ']' for w in CLAIM_WORDS)}")
        elif line.strip() and claims:
            claims[-1]["claim"] = (claims[-1]["claim"] + " " + line.strip()).strip()
    return claims


def _ini_fail_open(repo_root):
    """`[claim_vote]` of *repo_root*, or a parser with the section and nothing else.

    A missing, unreadable or malformed ini is no claims data — `claim_diff`'s own defaults —
    never an exception into a run that has already scored a round."""
    try:
        return cv.read_ini(Path(repo_root))
    except Exception:   # noqa: BLE001 — a broken config degrades to no collect data, never a crash
        parser = configparser.ConfigParser()
        parser.add_section(cv.SECTION)
        return parser


def _claims_config(repo_root, parser, accept=None, batch=None, code_batch=None, runs=1):
    """`(accept, batch, code_batch, budget, why-refused)`.

    `[claim_vote] accept` is the rule both sides are tallied under (CC-11) and the ini's
    `batch` / `code_batch` are the ones `claim_diff` builds its packs with. A key that is absent
    or malformed is the default; a count under 1 or a rule that is not one of the two is a refusal
    naming it, so a round never starts on a misread config."""
    if parser is None:
        parser = _ini_fail_open(repo_root)
    rule, why = cv.accept_rule(accept, parser)
    if why:
        return None, None, None, None, why

    def get(key, fallback):
        try:
            return int(str(parser.get(cv.SECTION, key, fallback=str(fallback))).strip())
        except (TypeError, ValueError):
            return fallback
    batch = cv.given_or(batch, get("batch", 10))
    code_batch = cv.given_or(code_batch, get("code_batch", 2))
    for label, value in (("batch", batch), ("code_batch", code_batch), ("runs", runs)):
        if value is not None and value < 1:
            return None, None, None, None, f"{label} {value}: must be 1 or more"
    return rule, batch, code_batch, cd.PackBudget.from_config(parser), ""


def _claims_skip(row):
    """The hard gate that keeps *row* out of the claims phase, `""` when it passed.

    The three hard gates of the scorecard: `_shrink` byte-identical, exactly one commit, nothing
    pushed. An entry that failed one is not worth the requests, and it is listed as skipped so the
    table still accounts for every agent. `no test shipped` is a column of the scorecard, not a
    hard gate, so it does not skip an entry here.
    """
    commits = row.get("commits")
    if not isinstance(commits, int):
        return row.get("notes") or "not a git worktree"
    if row.get("shrink") == "CHANGED":
        return "shrink is not byte-identical to the base"
    if commits != 1:
        return f"{commits} commits, expected 1"
    if row.get("pushed") == "yes":
        return "reached a remote"
    return ""


def _claims_costs(sides_list, voters, runs, batch, code_batch):
    """`(base requests once, head requests per entry, total)` — `claim_diff`'s own `requests_of`.

    Every entry starts from the same base, so its base side is the same question asked again: the
    first entry pays for it and the rest pay for their head only. Adding up per claim would bill
    the base once per entry, which is most of the requests of a round of eight."""
    base = cd.requests_of(sides_list[0]["items_b"], len(voters), runs, batch, code_batch)
    heads = [cd.requests_of(s["items_h"], len(voters), runs, batch, code_batch) for s in sides_list]
    return base, heads, base + sum(heads)


def _render_claims_table(entries, skipped, mismatches):
    """The console table: one row per entry, then the claims that did not meet their `expect`.

    `expect` is `met/total` over the claims that carry one — a claim without an `expect` is never
    a mismatch, so it is not in either number. Skipped entries are listed under the table, with
    the gate that kept them out, so the table accounts for every agent it was given."""
    head = ["agent"] + list(CLAIM_COUNTS) + ["expect"]
    body = [[e["agent"]] + [str(e["counts"].get(c, 0)) for c in CLAIM_COUNTS]
            + [f"{e['met']}/{e['total']} met"] for e in entries]
    width = [max(len(head[i]), *(len(r[i]) for r in body)) if body else len(head[i])
             for i in range(len(head))]
    lines = ["  ".join(h.ljust(width[i]) for i, h in enumerate(head))]
    for row in body:
        lines.append("  ".join(str(c).ljust(width[i]) for i, c in enumerate(row)))
    for name, reason in skipped.items():
        lines.append(f"  skipped: {name} — {reason}")
    if mismatches:
        lines.append("")
        lines.append("Mismatches — expect is the ticket, got is the model's reading:")
        for name, line in mismatches:
            lines.append(f"  {name}: {line}")
    else:
        lines.append("")
        lines.append("Every claim with an expect met it.")
    return lines


def _render_claims_markdown(entries, skipped, mismatches, *, round_no=None, ticket=None,
                            base_ref=None, n_claims=0, voters=(), runs=1, requests=0):
    """The same table as `claims.md`: the round's facts, the counts, the skipped entries,
    the mismatches with both sides' verdicts and the evidence ids."""
    head = ["agent"] + list(CLAIM_COUNTS) + ["expect"]
    lines = ["# Claims — the ticket's claims at the base and at every entry", ""]
    if round_no is not None:
        lines.append(f"- round: {round_no}")
    if ticket:
        lines.append(f"- ticket: {ticket}")
    if base_ref:
        lines.append(f"- base: `{base_ref}`")
    lines += [f"- claims: {n_claims}", f"- voters: {', '.join(voters) or '—'}",
              f"- runs: {runs}",
              f"- requests: {requests} (the base side once, then one head side per entry)"]
    if entries:
        lines += ["", "| " + " | ".join(head) + " |",
                  "|" + "|".join([" --- "] * len(head)) + "|"]
        for e in entries:
            row = [e["agent"]] + [str(e["counts"].get(c, 0)) for c in CLAIM_COUNTS] \
                  + [f"{e['met']}/{e['total']} met"]
            lines.append("| " + " | ".join(row) + " |")
    lines += ["", "A mismatch is a lead, not a gate: read the evidence and reproduce it by hand "
                "before a ticket is filed."]
    if skipped:
        lines.append("")
        lines.append("## Not judged")
        lines.append("")
        for name, reason in skipped.items():
            lines.append(f"- {name}: {reason}")
    lines.append("")
    lines.append("## Mismatches")
    lines.append("")
    if mismatches:
        for name, line in mismatches:
            lines.append(f"- **{name}** `{line}`")
    else:
        lines.append("None — every claim with an expect met it.")
    lines.append("")
    return "\n".join(lines)


def _write_claims_artifacts(out_dir, base, head, got, entry, *, budget, collect, parser,
                            voters, log):
    """`<out_dir>/claims/<entry>/` — `delta.json`, `votes_base.json` and `votes_head.json`, in
    the shape `claim_diff --out` writes (so `score_cc.py` and `claimcheck_truth` read them as they
    read a diff run). `votes_base.json` is the base side judged once for the round, written
    unchanged into every entry's folder. Returns the delta table, or `""` when the artifacts
    could not be written — a directory that fails only loses the files, never the phase."""
    try:
        dest = os.path.join(out_dir, "claims", _slug(entry))
        return cd.write_outputs(Path(dest), base, head, got, budget=budget, collect=collect,
                                models=cv.voter_models(voters, parser))
    except (OSError, ValueError) as e:   # noqa: BLE001 — a model that does not resolve is no model row
        log(f"claims: could not write {dest} — {type(e).__name__}: {e} (the table above stands)")
        return ""


def _claims_input(tasks_dir, name, source):
    """`(claims, where, refusal)` — the round's ticket's `## Claims` when no FILE was given, else
    the FILE, which may be anything `claim_diff.py` reads. An unknown word in the section and an
    unreadable FILE are a refusal naming it; a ticket without the section is no phase, not a refusal."""
    if source is CLAIMS_FROM_TICKET:
        text = ""
        try:
            text = Path(tasks_dir, name).read_text(encoding="utf-8")
        except OSError:
            pass   # ticket_for_round read it already: an unreadable ticket is no claims, not a crash
        try:
            return ticket_claims(text, name), f"{name} ## Claims", ""
        except ValueError as e:
            return [], f"{name} ## Claims", str(e)
    try:
        return cd.load_claims(Path(source)), str(source), ""
    except (OSError, ValueError) as e:   # json.JSONDecodeError is a ValueError
        return [], str(source), f"{getattr(e, 'strerror', None) or e}"


def _claims_refused(data, reason):
    """A phase that refused before a request: the data the caller needs to report and exit 2."""
    data["status"] = "refused"
    data["refusal"] = reason
    return data


def claims_phase(rows, base_ref, claims, *, voters, runs=1, repo_root=REPO_ROOT, parser=None,
                 accept=None, batch=None, code_batch=None, timeout=CLAIMS_TIMEOUT,
                 collect=True, out_dir=None, round_no=None, ticket=None, log=_say, vote=None):
    """Judge the ticket's claims at the base and at every entry that passed the hard gates.

    *rows* are the scorecard rows of `judge_worktree`, in order: an entry whose `shrink`,
    `commits` or `pushed` gate failed is listed as skipped and costs no request. For each entry
    left, `claim_diff.judge_both` with the target the entry's worktree, base `base_ref` and head
    its `HEAD`, the voters of `[claim_vote] llm_profiles`, one acceptance rule for both sides and
    packs with the collect facts — the vote seam of `judge_both`, so a test fills it with scripted
    voters and no request leaves the machine.

    The base side is judged once: the first entry's record is passed to every later entry as
    `base_side`, and `judge_both` reuses it only when the claims, the base sha, the voters, the
    runs and the rule are all this run's — a difference is a refusal naming the field, never a
    stale base judged as this round's. One line with the cost is printed before the first request.

    Returns `{"status": "ok" | "refused" | "nothing", "refusal", "requests", "entries",
    "skipped", "mismatches", "markdown"}`. A mismatch never sets the exit code; a refusal does
    (the caller exits 2). Never raises: a target that will not open, an unreadable ini, a broken
    artifact are a refusal or an `n/a`, never an exception into a run that has scored a round.
    """
    data = {"status": "nothing", "refusal": "", "requests": 0, "entries": [], "skipped": {},
            "mismatches": [], "markdown": ""}
    try:
        return _claims_phase(rows, base_ref, claims, voters=voters, runs=runs, repo_root=repo_root,
                             parser=parser, accept=accept, batch=batch, code_batch=code_batch,
                             timeout=timeout, collect=collect, out_dir=out_dir, round_no=round_no,
                             ticket=ticket, log=log, vote=vote)
    except Exception as e:   # noqa: BLE001 — the claims phase never takes the run down with it
        return _claims_refused(data, f"{type(e).__name__}: {e}")


def _claims_phase(rows, base_ref, claims, *, voters, runs=1, repo_root=REPO_ROOT, parser=None,
                  accept=None, batch=None, code_batch=None, timeout=CLAIMS_TIMEOUT, collect=True,
                  out_dir=None, round_no=None, ticket=None, log=_say, vote=None):
    data = {"status": "ok", "refusal": "", "requests": 0, "entries": [], "skipped": {},
            "mismatches": [], "markdown": ""}
    if not claims:
        return _claims_refused(data, "no claims to judge: an empty file, or a section with no claims")
    voters = [str(v) for v in (voters or []) if str(v).strip()]
    if not voters:
        return _claims_refused(data, f"no voters: pass --claims-profiles or set "
                                     f"[{cv.SECTION}] llm_profiles")
    # main passes no parser: the voters' sections are read from the ini here, once, and the same parser is the
    # one every vote asks (a None reached claim_vote.voter_settings and every vote died with an AttributeError)
    parser = parser if parser is not None else _ini_fail_open(repo_root)
    # A voter whose section is missing is a refusal before the first request, not a vote that errors for
    # every claim: a ``provider/model`` ref is Kilo's, read from its files, so only a section name is checked.
    absent = [v for v in voters if "/" not in v and not parser.has_section(v)]
    if absent:
        return _claims_refused(data, "no ini section for the voter(s) "
                                     + ", ".join(f"[{v}]" for v in absent)
                                     + " (contest.local.ini); add it or pick another --claims-profiles")
    accept, batch, code_batch, budget, why = _claims_config(
        repo_root, parser, accept, batch, code_batch, runs)
    if why:
        return _claims_refused(data, why)

    # The three hard gates decide who is judged; a skipped entry is still accounted for in the table.
    skipped, trees = data["skipped"], []
    for row in rows:
        name = row.get("agent") or "entry"
        reason = _claims_skip(row)
        if reason:
            skipped[name] = reason
        else:
            trees.append((name, row.get("path")))
    if not trees:
        data["status"] = "nothing"
        log(CLAIMS_NOTHING)
        return data

    # The base is looked up in the entries' checkouts, then the judge's own repo — a ref found
    # nowhere is a refusal naming it, not a target that fails halfway through a run.
    homes = [p for _, p in trees if os.path.isdir(os.path.join(str(p), ".git"))
             or os.path.exists(os.path.join(str(p), ".git"))]
    if os.path.isdir(REPO_ROOT) and not any(
            os.path.realpath(h) == os.path.realpath(REPO_ROOT) for h in homes):
        homes.append(REPO_ROOT)
    home = _resolve(homes, base_ref)
    if not home:
        return _claims_refused(data, f"--base {base_ref} resolves in no checkout (the entries' "
                                     f"or {REPO_ROOT}) — no base side to judge")

    scratch = tempfile.mkdtemp(prefix="judgeclaims-")
    try:
        with ExitStack() as stack:
            try:
                base = stack.enter_context(cc_target.Target.open(home, base_ref, scratch=scratch))
                heads = {}
                for name, path in trees:
                    heads[name] = stack.enter_context(
                        cc_target.Target.open(path, "HEAD", scratch=scratch))
            except cc_target.TargetError as e:
                return _claims_refused(data, f"refused: {e}")
            data["base_sha"] = base.sha

            prepared = []
            for name, _path in trees:
                try:
                    sides = cd.prepare_sides(claims, base, heads[name], budget, collect=collect)
                except Exception as e:   # noqa: BLE001 — a broken artifact is no claims data
                    return _claims_refused(data, f"{name}: the packs did not build — "
                                                 f"{type(e).__name__}: {e}")
                for i, c in enumerate(claims):
                    if c.get("expect") and sides["world"][i]:
                        return _claims_refused(data, f"{name}: expect on a world claim "
                                                     f"({c.get('id') or i + 1}): it is judged once, "
                                                     f"at head, so its expect can never be met")
                prepared.append((name, heads[name], sides))

            base_req, head_reqs, total_req = _claims_costs(
                [p[2] for p in prepared], voters, runs, batch, code_batch)
            data["requests"] = total_req

            # The bill, printed before the first request: a round of eight is not started blind.
            log(f"\nClaims — {len(claims)} claims, {len(voters)} voters, {runs} run(s), "
                f"{len(prepared)} entries: {total_req} requests (the base side once, then one head "
                f"side per entry)" + (f"\nskipped: " + ", ".join(f"{n} ({why})"
                                                                  for n, why in skipped.items())
                                      if skipped else ""))

            def ask(ref, items, run):
                return cv.ask_v2(ref, items, run, 1, parser, timeout, batch, code_batch, False)

            with ThreadPoolExecutor(max(1, CLAIMS_PARALLEL)) as pool:
                voter = vote if vote is not None else cd.make_vote(voters, runs, pool, ask)
                record = None
                entries = data["entries"]
                for (name, head, sides), head_req in zip(prepared, head_reqs):
                    try:
                        got = cd.judge_both(claims, base, head, voter, budget, collect=collect,
                                            accept=accept, base_side=record, voters=voters,
                                            runs=runs, sides=sides)
                    except ValueError as e:
                        return _claims_refused(data, f"{name}: {e}")
                    if record is None:
                        record = cd.base_side_record(claims, base, voters, runs, got)
                    counts = Counter(d.change for d in got["deltas"])
                    deltas = [d for d in got["deltas"] if d.expect]
                    lines = [cc_compare.mismatch_line(d) for d in cc_compare.mismatches(got["deltas"])]
                    met = sum(1 for d in deltas if cc_compare.EXPECTS[d.expect] == d.change)
                    entries.append({"agent": name, "path": head.repo, "head": head.sha,
                                    "counts": dict(counts), "met": met, "total": len(deltas),
                                    "mismatches": lines})
                    data["mismatches"].extend((name, line) for line in lines)
                    row = entries[-1]
                    log(f"  [{len(entries)}/{len(prepared)}] {name}: "
                        + "  ".join(f"{c}={counts.get(c, 0)}" for c in CLAIM_COUNTS)
                        + f" | expect {row['met']}/{row['total']} met ({head_req} requests)"
                        + (f"\n{CLAIMS_LEAD}" if lines else ""))
                    if out_dir:
                        _write_claims_artifacts(out_dir, base, head, got, name, budget=budget,
                                                collect=collect, parser=parser, voters=voters, log=log)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    log("\n".join(_render_claims_table(data["entries"], skipped, data["mismatches"])))
    data["markdown"] = _render_claims_markdown(
        data["entries"], skipped, data["mismatches"], round_no=round_no, ticket=ticket,
        base_ref=base.sha, n_claims=len(claims), voters=voters, runs=runs,
        requests=data["requests"])
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
            md_path = os.path.join(out_dir, "claims.md")
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(data["markdown"] + "\n")
            with open(os.path.join(out_dir, "claims.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
            log(f"\nclaims -> {md_path}, {os.path.join(out_dir, 'claims.json')}")
        except OSError as e:
            log(f"claims: could not write {out_dir} — {e} (the table above stands)")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True, help="round number, matches NN- prefix")
    ap.add_argument("--tasks", default="epic-tasks", help="folder of NN-*.md tickets")
    ap.add_argument("--runs", default=None, help="folder whose subdirs are one worktree per agent")
    ap.add_argument("--worktree", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--base", default="competition", help="the tree every agent started from")
    ap.add_argument("--tests", action="store_true", help="run the four pytest roots in each worktree")
    ap.add_argument("--csv", default=None, help="also write the scorecard here")
    ap.add_argument("--cross", action="store_true",
                    help="run every entry's own tests against every implementation")
    ap.add_argument("--ideal", default=None, metavar="REF",
                    help="a candidate-ideal ref the cross matrix also scores against")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="cross cells at once; the default 1 is sequential")
    ap.add_argument("--cell-timeout", type=float, default=CROSS_CELL_TIMEOUT, metavar="SEC",
                    help="wall clock one cross cell may spend")
    ap.add_argument("--cross-out", default=None, metavar="DIR",
                    help="write cross.json / cross.md there (default contest-out/NN)")
    ap.add_argument("--claims", nargs="?", const=CLAIMS_FROM_TICKET, default=None, metavar="FILE",
                    help="judge the ticket's claims at the base and at every entry that passed "
                         "the hard gates; FILE is anything claim_diff.py reads, and with no FILE "
                         "the claims come from the round's ticket ## Claims")
    ap.add_argument("--claims-profiles", nargs="+", default=None, metavar="PROFILE",
                    help="the voters of the claims phase ([claim_vote] llm_profiles when unset)")
    ap.add_argument("--claims-runs", type=int, default=1, metavar="N",
                    help="voter runs per side of the claims phase (the default 1)")
    a = ap.parse_args()

    name, title, declared = ticket_for_round(a.tasks, a.round)
    if not name:
        print(f"no ticket numbered {a.round} in {a.tasks}/", file=sys.stderr)
        return 1

    trees = []
    for spec in a.worktree:
        n, _, p = spec.partition("=")
        trees.append((n, p))
    if a.runs and os.path.isdir(a.runs):
        for d in sorted(os.listdir(a.runs)):
            p = os.path.join(a.runs, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, ".git")):
                trees.append((d, p))
    if not trees:
        print("nothing to score — pass --runs or --worktree", file=sys.stderr)
        return 1

    print(f"\nRound {a.round}: {title}")
    print(f"ticket: {a.tasks}/{name}")
    print(f"declared files: {', '.join(f'`{d}`' for d in declared) or '—'}")
    print(f"base: {a.base}\n")

    rows = [judge_worktree(n, p, a.base, declared, a.tests) for n, p in trees]

    cols = ["agent", "gate", "shrink", "commits", "files", "+/-", "test_files",
            "test_funcs", "off_ticket", "pushed", "sha"]
    if a.tests:
        cols.append("tests_run")
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))
    for r in rows:
        if r.get("notes"):
            print(f"\n  {r['agent']}: {r['notes']}")
        if r.get("off_ticket_files"):
            print(f"  {r['agent']} touched off-ticket: {r['off_ticket_files']}")

    crossed = True
    if a.cross:
        # Zero-padded like contest-out/<NN>, so cross.json sits next to SUMMARY.md.
        out_dir = a.cross_out or os.path.join("contest-out", f"{a.round:02d}")
        data = cross_tests(trees, a.base, ideal=a.ideal, jobs=a.jobs,
                           cell_timeout=a.cell_timeout, out_dir=out_dir,
                           round_no=a.round, ticket=name)
        crossed = not data.get("nothing_crossed", False)

    claims_status = "off"
    if a.claims is not None:
        claims, where, refuse = _claims_input(a.tasks, name, a.claims)
        if refuse:
            print(f"claims: {where}: {refuse}", file=sys.stderr)
            return 2
        if not claims:
            if a.claims is CLAIMS_FROM_TICKET:
                # Optional section: a ticket without it is judged as today, so the exit code is untouched.
                print("\nclaims: the ticket has no ## Claims section, and no FILE was given — "
                      "nothing was judged")
                claims_status = "none"
            else:
                print(f"claims: {where}: no claims to judge", file=sys.stderr)
                return 2
        else:
            # The base side of the claims is voted once, so the same --cross-out that holds cross.md
            # holds claims.md: one folder to read when the round ends.
            out_dir = a.cross_out or (os.path.join("contest-out", f"{a.round:02d}") if a.cross else None)
            data = claims_phase(rows, a.base, claims, voters=a.claims_profiles,
                                runs=a.claims_runs, repo_root=Path.cwd(), out_dir=out_dir,
                                round_no=a.round, ticket=name)
            if data.get("status") == "refused":
                print(f"claims: {data['refusal']}", file=sys.stderr)
                return 2
            claims_status = data["status"]

    first = ("  1. Does it do what the ticket's Acceptance list says, item by item?"
             if claims_status != "ok" else
             "  1. The claims phase scored the ticket's claims: read its mismatches, and the diff\n"
             "     of any code it did not judge")
    print("\nWhat this script cannot score — read the diffs for these:")
    print(first)
    print("  2. Is it the simplest thing that does it, or is there a new abstraction")
    print("     the ticket did not ask for?")
    print("  3. Does the test fail without the change? (delete the change, re-run it)")
    print("  4. Does it stay fail-open — absent model, malformed config, broken artifact?")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols + ["notes", "off_ticket_files", "path"])
            wr.writeheader()
            for r in rows:
                wr.writerow({k: r.get(k, "") for k in wr.fieldnames})
        print(f"\nscorecard -> {a.csv}")
    # A mismatch never sets the code; only a refused or unjudged claims phase does, like --cross.
    return EXIT_NO_CROSS if (not crossed or claims_status == "nothing") else 0


if __name__ == "__main__":
    sys.exit(main())
