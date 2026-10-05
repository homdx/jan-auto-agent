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

Exit codes: 0 scored · 1 usage / nothing to score.

KC-5: the scoring itself lives in `tools/contest/gates.py` (`judge_worktree`)
so the contest runner can import it; this file is the operator CLI over it and
keeps its CLI and its stdout/CSV bytes.
"""
import argparse
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
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest.gates import (  # noqa: E402
    judge_worktree,
    ticket_for_round,
)

# ─────────────────────────────────────────────────────────────────────────────
# The cross phase (158)
#
# P's changed test files are copied into a scratch copy of I's tree and run
# there, then the copy and its basetemp are deleted again. No entry's
# worktree is ever written to — `git archive` reads — and nothing is left on
# disk: round 151 ran the box out of inodes before the copies were cleaned up.
# ─────────────────────────────────────────────────────────────────────────────

#: The three kinds of failure a cell can hold, in the order the operator
#: reads them — the first two are not bugs in the code under test.
API = "api"               # a name or signature P has and I does not have
BASE = "base"             # the test fails on the base too: no one asked for it
BEHAVIOUR = "behaviour"   # a candidate bug in I, to be reproduced by hand

#: A cell is P's test, run on I's code: `tests/_xcross_<P>_<file>`. Named that
#: way so pytest still collects it when it is passed on the command line, and
#: so it can never be mistaken for a test of I's own tree.
_CROSS_NAME = "_xcross_"

#: The test root the ticket names: `git diff --name-only <base> -- tests/`.
TEST_ROOT = "tests/"

#: The cell's own suite runs with this many xdist workers, per the ticket.
CROSS_WORKERS = 4

#: Seconds of wall clock one cell may spend before it is ended by group.
CROSS_CELL_TIMEOUT = 300.0

#: pytest's short-summary line: the node id, then ` - message`. xdist's
#: `@group` suffix and the message are both dropped — the node id is enough.
_SUMMARY_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:@[^\s\[]+)?(?: - .*)?$")

#: pytest's section header between a run's sections: `_____ name _____` — and a
#: name too long for the line gets one `_` on each side, `_ ERROR at setup of
#: test_a_very_long_name _`, which still has to be a header (an indented source
#: line never starts with `_`; the spaces around the name are the format's own).
_SECTION = re.compile(r"^_+ (.+?) _+\s*$")

#: The first `E ` line of a traceback is the failure; the rest is context.
_E_LINE = re.compile(r"^E\s")

#: …unless it is a frame (`E     File "x.py", line 3`): the failure comes last.
_E_FRAME = re.compile(r'^E\s+File "')

#: ImportError / ModuleNotFoundError / AttributeError / TypeError, the ticket's own
#: four: P's code has a name or a signature that I's does not. That is a different
#: implementation, not a bug in I. Every TypeError counts, not only the ones that
#: name a keyword or an argument count: `'int' object is not iterable` is what a
#: call with another implementation's argument shape raises, and calling it a
#: `behaviour` lead sent the operator after a bug that was only a different API.
_API_ERR = re.compile(
    r"\b(?:ImportError|ModuleNotFoundError|AttributeError|TypeError)\b"
    # a test that runs another entry's CLI with its own flags: `assert rc == 0` fails
    # on argparse's banner, `AssertionError: usage: judge_epic_round.py [-h] --round ...`
    r"|\busage: \S+ \[-h\]",
    re.I)

#: pytest's stats line, the last line that carries a count and a duration:
#: `1 failed, 1 passed in 0.02s`.
_STATS = re.compile(r"(\d+) (passed|failed|errors?|skipped)")

#: pytest's stats line ends in `====================`, so the duration is only
#: searched for, never anchored: `1 failed in 0.55s ====`.
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


def _git(cwd, *args):
    """`git <args>` in *cwd* as `(returncode, stdout)`; never raises.

    The cross phase reads trees it does not own, so a tree git cannot read is
    a cell that says so — not an exception into the run.
    """
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout.strip()


def changed_tests(path, base):
    """The test files *path* added or changed against *base* — `[]` if unreadable.

    `git diff --name-only <base> -- tests/`: base against the worktree, so a
    test an agent left uncommitted is crossed too. Anything git cannot name is
    dropped, which is the fail-open side of the ticket.
    """
    try:
        rc, out = _git(path, "diff", "--name-only", "--no-renames", base, "--", TEST_ROOT)
    except Exception:
        return []
    if rc:
        return []
    return [f for f in out.splitlines()
            if f.startswith(TEST_ROOT) and f.lower().endswith(".py")]


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
    """P's changed test files into a scratch I, under `tests/_xcross_<P>_…`.

    Returns the relative paths copied — `[]` when none of them is readable,
    which is a cell that says so rather than an empty suite that says `0/0`.
    """
    out = []
    for rel in files:
        src = os.path.join(entry, rel)
        if not os.path.isfile(src):
            continue  # the diff named a file the worktree does not carry
        stem = rel[len(TEST_ROOT):] if rel.startswith(TEST_ROOT) else os.path.basename(rel)
        # a module name: pytest imports the file by its basename, and a `.` or
        # a `/` in it is a package path — `_xcross_agnes-2.5-flash_test_x` is
        # `ModuleNotFoundError: No module named '_xcross_agnes-2'`, which would
        # read as `api` for every cell of an entry named like a model or a
        # judge folder (`apertus-v1-5-8b.GAVE_UP`)
        dst = os.path.join(dest, TEST_ROOT,
                           f"{_CROSS_NAME}{_modname(label)}_{_modname(os.path.splitext(stem)[0])}.py")
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            out.append(os.path.join(TEST_ROOT, os.path.basename(dst)))
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
    # The cell's temp files live inside the cell: a test that makes a dir with
    # `tempfile` and leaves it, or sweeps `<tmp>/xcross-*` like round 158's
    # agnes-2-5-flash does at the end of one of its tests, touches nothing of
    # the judge's and nothing outlives the scratch copy.
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
        # the first `E ` line is the failure — except when the code under test did
        # not even import: a SyntaxError opens with `E   File "…", line N` and a
        # source line, and the error itself is the last `E ` line
        keep.append((n, lines[-1] if _E_FRAME.match(lines[0]) else lines[0]))
    return keep


#: What pytest prints before the name in the header of a section that is not a
#: plain test failure: `ERROR at setup of test_x`, `ERROR collecting tests/x.py`.
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
    # not `xcross-…`: that is the name a test of another entry may sweep (a cell
    # of `--jobs 3` lost its scratch dir to such a test, `rc 127`, `no tests ran`)
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
            # no verdict at all (`no tests ran`, rc 5): under load an xdist worker
            # can die before it collects. Once more, in a clean basetemp, before
            # the cell is called `n/a` — a ninety-cell matrix with one hole in it
            # is a matrix to run again by hand
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


def _render_markdown(providers, impls, by, base, ideal, ticket=None, round_no=None):
    """The same two views as `cross.md`."""
    rows = [p[0] for p in providers]
    cols = [i[0] for i in impls]
    lines = ["# Cross — every entry's own tests, run on every implementation", ""]
    if round_no is not None:
        lines.append(f"- round: {round_no}")
    if ticket:
        lines.append(f"- ticket: {ticket}")
    lines.append(f"- base: `{base}`")
    lines.append(f"- ideal: `{ideal}`" if ideal else "- ideal: —")
    lines.append("")
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
                "tests": [], "impls": [], "cells": []}


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
    # `setup_worktrees.py` makes a `base` and an `ideal` worktree of its own: a
    # tree with that name IS the column, and the ref is not archived a second
    # time under the same name (two `base` columns shared one cell dict)
    named = {name for name, _ in trees}
    anchor = _resolve(repos, base) if repos and base and "base" not in named else None
    if anchor:
        impls.append(("base", str(base), anchor))
    ideal_repo = _resolve(repos, ideal) if repos and ideal and "ideal" not in named else None
    if ideal_repo:
        impls.append(("ideal", str(ideal), ideal_repo))

    log(f"\nCross — every entry's own tests, run on every implementation "
        f"(cells: {len(providers)} × {len(impls)})")

    # one line per finished cell: a real round is some ninety cells and minutes
    # long, and a judge that says nothing until the end looks hung
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
            "tests": [p[0] for p in providers],
            "impls": [i[0] for i in impls],
            "leads": leads,
            "cells": [{"tests": pn, "code": in_, "passed": c["passed"], "total": c["total"],
                       "note": c["note"], "kinds": c["kinds"],
                       "failures": c["failures"]}
                      for pn, in_, c in cells]}

    md = _render_markdown(providers, impls, by, base, ideal, ticket, round_no)
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

    if a.cross:
        # zero-padded like `contest-out/<NN>` of the runner (`_round_out_dir`),
        # so cross.json / cross.md land next to SUMMARY.md also for round 7
        out_dir = a.cross_out or os.path.join("contest-out", f"{a.round:02d}")
        cross_tests(trees, a.base, ideal=a.ideal, jobs=a.jobs,
                    cell_timeout=a.cell_timeout, out_dir=out_dir,
                    round_no=a.round, ticket=name)

    print("\nWhat this script cannot score — read the diffs for these:")
    print("  1. Does it do what the ticket's Acceptance list says, item by item?")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
