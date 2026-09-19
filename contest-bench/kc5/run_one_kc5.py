#!/usr/bin/env python3
"""KC-5 black-box bench: one scenario per invocation, run with cwd=<entry worktree>.

    python3 probe.py <scenario> <base_worktree>

Prints PASS / FAIL <reason> / ERROR <traceback tail>. Every scenario builds its own
sandbox git repo in a temp dir, so nothing touches the entry worktree itself.
"""
from __future__ import annotations

import csv
import dataclasses
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ENTRY = Path.cwd().resolve()
sys.path.insert(0, str(ENTRY))
BASE = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

TICKET_NAME = "44-kc5-test.md"
TICKET_BODY = """# KC-5 test ticket — the sandbox one

**Status:** open — round 44.
**Severity:** HIGH
**File:** `pkg/thing.py`
**Symbol:** `thing`
**Round:** 44
**Also touches:** `pkg/other.py`, `tests/test_thing.py` (new)

body
"""
TICKET_NO_ALSO = """# KC-5 test ticket B

**File:** `pkg/thing.py`
**Also touches:** —

body
"""
BRIDGE_OK = '''"""stub"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        out = raw.strip()
        return out[:10]

    def other(self) -> int:
        return 1
'''
BRIDGE_CHANGED = BRIDGE_OK.replace("out[:10]", "out[:20]")

ALLOWED_CODES = {
    "no_progress_row", "progress_not_done", "no_commit", "commit_not_on_branch",
    "commits_ne_1", "pushed", "no_test_file", "shrink_changed", "off_ticket_files",
    "tests_failed",
}


class Fail(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit_all(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class Sandbox:
    """A repo with a base commit and the agent branch `contest/44/agent` on it."""

    def __init__(self, root: Path, agent: str = "agent"):
        self.root = root
        self.agent = agent
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        self.tasks = root / "epic-tasks"
        self.tasks.mkdir()
        write(self.tasks / TICKET_NAME, TICKET_BODY)
        write(self.tasks / "45-kc6-other.md", TICKET_NO_ALSO.replace("ticket B", "ticket C"))
        r = self.repo
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "bench@example.invalid")
        git(r, "config", "user.name", "bench")
        write(r / "tools" / "auto" / "collect_bridge.py", BRIDGE_OK)
        write(r / "pkg" / "__init__.py", "")
        write(r / "pkg" / "thing.py", "def thing():\n    return 1\n")
        write(r / "pkg" / "other.py", "def other():\n    return 2\n")
        write(r / ".gitignore", "runs/\n__pycache__/\n.pytest_cache/\n")
        write(r / "tests" / "__init__.py", "")
        write(r / "tests" / "test_base.py", "def test_base():\n    assert True\n")
        shutil.copytree(self.tasks, r / "epic-tasks")
        (r / "scripts").mkdir()
        shutil.copy(BASE / "scripts" / "append_task.py", r / "scripts" / "append_task.py")
        self.base_sha = commit_all(r, "base")
        self.branch = f"contest/44/{agent}"
        git(r, "checkout", "-q", "-b", self.branch)

    # ── agent work ──────────────────────────────────────────────────────
    def good_change(self, *, test=True, off_ticket=False, shrink=False, msg="KC-5: thing"):
        write(self.repo / "pkg" / "thing.py", f"def thing():\n    return 42  # {msg}\n")
        if test:
            write(self.repo / "tests" / "test_thing.py",
                  "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
        if off_ticket:
            write(self.repo / "pkg" / "extra.py", "X = 1\n")
        if shrink:
            write(self.repo / "tools" / "auto" / "collect_bridge.py", BRIDGE_CHANGED)
        return commit_all(self.repo, msg)

    def failing_test(self):
        write(self.repo / "tests" / "test_thing.py",
              "def test_thing_fails():\n    assert 1 == 2, 'bench-marker-boom'\n")
        return commit_all(self.repo, "KC-5: failing test")

    # ── the claim ───────────────────────────────────────────────────────
    @property
    def progress_csv(self) -> Path:
        return self.repo / "runs" / self.agent / "PROGRESS.csv"

    def append_task(self, outcome: str, commit: str = "", ticket: str = TICKET_NAME, note=""):
        cmd = [sys.executable, str(self.repo / "scripts" / "append_task.py"),
               "--progress", str(self.progress_csv), "--ticket", ticket,
               "--outcome", outcome, "--allow-duplicate"]
        if commit:
            cmd += ["--commit", commit]
        if note:
            cmd += ["--note", note]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.repo))
        if r.returncode:
            raise RuntimeError(f"append_task failed: {r.stderr}")

    def write_rows(self, rows):
        self.progress_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.progress_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["ticket", "finding", "outcome", "commit", "note"])
            w.writeheader()
            for r in rows:
                w.writerow({"ticket": r[0], "finding": "", "outcome": r[1], "commit": r[2], "note": r[3] if len(r) > 3 else ""})

    def workspace(self, kind="worktree", path=None):
        from tools.contest.workspace import Workspace
        return Workspace(agent=self.agent, path=(path or self.repo).resolve(), branch=self.branch,
                         base_sha=self.base_sha, kind=kind)

    @property
    def ticket_path(self) -> Path:
        return self.tasks / TICKET_NAME


def load_old_judge():
    """The pre-move script from the base worktree, imported as a module."""
    spec = importlib.util.spec_from_file_location("old_judge", BASE / "scripts" / "judge_epic_round.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gates():
    import tools.contest.gates as g
    return g


def hv():
    import tools.contest.harvest as h
    return h


def codes(h):
    return [r.code for r in h.reasons]


def blocking(h):
    return [r.code for r in h.reasons if r.blocking]


def reason(h, code):
    for r in h.reasons:
        if r.code == code:
            return r
    raise Fail(f"reason {code!r} not in {codes(h)}")


def run_script(entry: Path, args, cwd=None):
    r = subprocess.run([sys.executable, str(entry / "scripts" / "judge_epic_round.py"), *args],
                       cwd=str(cwd or entry), capture_output=True, text=True)
    return r


# ═════════════════════════════════════════════════════════════════════════
# scenarios
# ═════════════════════════════════════════════════════════════════════════
SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def s01_api_symbols(tmp):
    g, h = gates(), hv()
    for n in ("judge_worktree", "extract_shrink", "ticket_for_round", "git", "run_tests", "declared_files"):
        check(callable(getattr(g, n, None)), f"gates.{n} missing")
    for n in ("Reason", "Harvest", "harvest", "rework_message"):
        check(getattr(h, n, None) is not None, f"harvest.{n} missing")
    check(dataclasses.is_dataclass(h.Reason) and h.Reason.__dataclass_params__.frozen, "Reason not frozen dataclass")
    check(dataclasses.is_dataclass(h.Harvest) and h.Harvest.__dataclass_params__.frozen, "Harvest not frozen dataclass")
    fields = {f.name for f in dataclasses.fields(h.Harvest)}
    check({"verdict", "reasons", "commit", "facts", "elapsed"} <= fields, f"Harvest fields {fields}")
    rf = {f.name for f in dataclasses.fields(h.Reason)}
    check({"code", "text", "blocking"} <= rf, f"Reason fields {rf}")


@scenario
def s02_script_is_thin_wrapper(tmp):
    src = (ENTRY / "scripts" / "judge_epic_round.py").read_text(encoding="utf-8")
    check("tools.contest.gates" in src, "script does not import tools.contest.gates")
    for n in ("def judge(", "def extract_shrink(", "def ticket_for_round(", "def judge_worktree("):
        check(n not in src, f"script still defines {n}")


def _two_repos(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    b = Sandbox(tmp / "B"); b.good_change(test=False, off_ticket=True, shrink=True); b.good_change(test=False, msg="second")
    return a, b


def _golden(tmp, extra=(), cwd_entry=None, cwd_base=None):
    a, b = _two_repos(tmp)
    args = ["--round", "44", "--tasks", str(a.tasks), "--base", "main",
            "--worktree", f"a={a.repo}", "--worktree", f"b={b.repo}", *extra]
    out_e = tmp / "e.csv"; out_b = tmp / "b.csv"
    re_ = run_script(ENTRY, [*args, "--csv", str(out_e)], cwd=cwd_entry)
    rb = run_script(BASE, [*args, "--csv", str(out_b)], cwd=cwd_base)
    check(rb.returncode == 0, f"base script failed?! {rb.stderr[-300:]}")
    check(re_.returncode == 0, f"entry script rc={re_.returncode}: {re_.stderr[-400:]}")
    so_e = re_.stdout.replace(str(out_e), "OUT"); so_b = rb.stdout.replace(str(out_b), "OUT")
    check(so_e == so_b, "stdout differs:\n" + "\n".join(_diff(so_b, so_e)))
    check(out_e.read_bytes() == out_b.read_bytes(), "csv differs:\n" + "\n".join(_diff(out_b.read_text(), out_e.read_text())))
    check(re_.stderr == rb.stderr, f"stderr differs: {re_.stderr[-300:]!r}")


def _diff(a, b):
    import difflib
    return list(difflib.unified_diff(a.splitlines(), b.splitlines(), "base", "entry", lineterm="", n=1))[:30]


@scenario
def s03_cli_golden(tmp):
    _golden(tmp)


@scenario
def s04_cli_golden_with_tests(tmp):
    _golden(tmp, extra=["--tests"])


@scenario
def s05_cli_from_other_cwd(tmp):
    other = tmp / "elsewhere"; other.mkdir()
    _golden(tmp, cwd_entry=other, cwd_base=BASE)


@scenario
def s06_cli_errors_identical(tmp):
    a = Sandbox(tmp / "A")
    for args in (["--round", "44", "--tasks", str(a.tasks)], ["--round", "99", "--tasks", str(a.tasks), "--worktree", f"a={a.repo}"]):
        re_ = run_script(ENTRY, args); rb = run_script(BASE, args)
        check(re_.returncode == rb.returncode == 1, f"rc {re_.returncode} vs {rb.returncode}")
        check(re_.stdout == rb.stdout and re_.stderr == rb.stderr, f"error output differs: {re_.stderr!r} vs {rb.stderr!r}")


@scenario
def s07_judge_worktree_equals_old_judge(tmp):
    a, b = _two_repos(tmp)
    old = load_old_judge(); g = gates()
    decl = ["pkg/thing.py", "pkg/other.py", "tests/test_thing.py"]
    for sb in (a, b):
        exp = old.judge("x", str(sb.repo), "main", decl, False)
        got = g.judge_worktree("x", str(sb.repo), "main", decl, False)
        check(got == exp, f"row differs:\n old={exp}\n new={got}")
    nog = tmp / "notgit"; nog.mkdir()
    check(g.judge_worktree("n", str(nog), "main", decl, False) == old.judge("n", str(nog), "main", decl, False), "non-git row differs")
    # want_tests=True on the good sandbox — same column text
    exp = old.judge("x", str(a.repo), "main", decl, True); got = g.judge_worktree("x", str(a.repo), "main", decl, True)
    check(got == exp, f"row with tests differs:\n old={exp}\n new={got}")


@scenario
def s08_moved_helpers_equal_old(tmp):
    a = Sandbox(tmp / "A"); a.good_change(shrink=True)
    old = load_old_judge(); g = gates()
    for rev in ("main", "HEAD", "nope"):
        check(g.extract_shrink(str(a.repo), rev) == old.extract_shrink(str(a.repo), rev), f"extract_shrink({rev}) differs")
    check(g.extract_shrink(str(a.repo), "main") != g.extract_shrink(str(a.repo), "HEAD"), "shrink change not seen")
    for n in (44, 45, 7):
        check(g.ticket_for_round(str(a.tasks), n) == old.ticket_for_round(str(a.tasks), n), f"ticket_for_round({n}) differs")
    check(g.ticket_for_round(str(a.tasks), 44)[2] == ["pkg/thing.py", "pkg/other.py", "tests/test_thing.py"], "declared list wrong")
    check(g.git(str(a.repo), "rev-parse", "HEAD") == old.git(str(a.repo), "rev-parse", "HEAD"), "git() differs")
    check(g.git(str(a.repo), "rev-parse", "--verify", "-q", "nope") == "", "git() should swallow errors without check")
    try:
        g.git(str(a.repo), "rev-parse", "--verify", "nope", check=True)
        raise Fail("git(check=True) did not raise")
    except RuntimeError:
        pass
    check(g.run_tests(str(a.repo)) == old.run_tests(str(a.repo)), "run_tests() differs")


@scenario
def s09_declared_files(tmp):
    a = Sandbox(tmp / "A"); g = gates()
    d = g.declared_files(a.ticket_path)
    check(isinstance(d, tuple), f"declared_files returned {type(d)}")
    check(d == ("pkg/thing.py", "pkg/other.py", "tests/test_thing.py"), f"declared_files -> {d}")
    write(tmp / "b.md", TICKET_NO_ALSO)
    check(g.declared_files(tmp / "b.md") == ("pkg/thing.py",), f"'—' handling: {g.declared_files(tmp / 'b.md')}")
    check(g.declared_files(str(tmp / "b.md")) == ("pkg/thing.py",), "str path not accepted")
    # same parse as ticket_for_round on every real ticket
    tasks = BASE / "epic-tasks"
    for name in sorted(os.listdir(tasks)):
        m = re.match(r"^0*(\d+)-.*\.md$", name)
        if not m:
            continue
        n = int(m.group(1))
        _, _, decl = g.ticket_for_round(str(tasks), n)
        got = g.declared_files(tasks / name)
        check(tuple(decl) == got, f"{name}: ticket_for_round={decl} declared_files={got}")


@scenario
def s10_no_progress_row(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK", f"verdict {h.verdict}")
    check("no_progress_row" in blocking(h), f"codes {codes(h)}")


@scenario
def s11_ready_via_append_task(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.append_task("DONE", sha)  # append_task.py rewrites DONE -> FIXED: the real contract
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"verdict {h.verdict}, codes {codes(h)}: {[r.text for r in h.reasons]}")
    check(blocking(h) == [], f"blocking {blocking(h)}")
    check(h.commit and sha.startswith(h.commit), f"commit {h.commit!r}")
    check(isinstance(h.facts, dict) and h.facts.get("commits") == 1 and h.facts.get("shrink") == "same"
          and h.facts.get("test_files") == 1 and h.facts.get("pushed") == "no", f"facts {h.facts}")
    check(isinstance(h.elapsed, float) and h.elapsed >= 0, f"elapsed {h.elapsed!r}")


@scenario
def s12_ready_literal_done(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.write_rows([(TICKET_NAME, "DONE", sha)])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"verdict {h.verdict}, codes {codes(h)}")


@scenario
def s13_short_sha_is_on_branch(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.append_task("DONE", sha[:7])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"verdict {h.verdict}, codes {codes(h)}: {[r.text for r in h.reasons]}")
    check(h.commit == sha[:7] or h.commit == sha, f"commit {h.commit!r}")


@scenario
def s14_progress_not_done(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    a.append_task("SKIPPED", note="deferred")
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "progress_not_done" in blocking(h), f"{h.verdict} {codes(h)}")
    check("no_progress_row" not in codes(h), "a row exists — no_progress_row must not fire")


@scenario
def s15_no_commit(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    a.write_rows([(TICKET_NAME, "DONE", "")])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "no_commit" in blocking(h), f"{h.verdict} {codes(h)}")


@scenario
def s16_commit_not_on_branch(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    git(a.repo, "checkout", "-q", "-b", "side", "main")
    write(a.repo / "side.txt", "x"); side = commit_all(a.repo, "side")
    git(a.repo, "checkout", "-q", a.branch)
    a.append_task("DONE", side)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "commit_not_on_branch" in blocking(h), f"real other-branch sha: {h.verdict} {codes(h)}")
    a.write_rows([(TICKET_NAME, "DONE", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "commit_not_on_branch" in blocking(h), f"bogus sha: {h.verdict} {codes(h)}")


@scenario
def s17_commits_ne_1(tmp):
    a = Sandbox(tmp / "A"); a.good_change(); sha = a.good_change(msg="second")
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "commits_ne_1" in blocking(h), f"{h.verdict} {codes(h)}")
    r = reason(h, "commits_ne_1"); check("2" in r.text, f"text does not name the number: {r.text!r}")


@scenario
def s18_no_test_file(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change(test=False)
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "no_test_file" in blocking(h), f"{h.verdict} {codes(h)}")
    check("test" in reason(h, "no_test_file").text.lower(), "text does not mention a test")


@scenario
def s19_shrink_changed(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change(shrink=True)
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "shrink_changed" in blocking(h), f"{h.verdict} {codes(h)}")
    check("_shrink" in reason(h, "shrink_changed").text, "text does not name _shrink")


@scenario
def s20_off_ticket_non_blocking(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change(off_ticket=True)
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"verdict {h.verdict} codes {codes(h)}")
    r = reason(h, "off_ticket_files")
    check(r.blocking is False, "off_ticket_files must be non-blocking")
    check("pkg/extra.py" in r.text, f"text does not list the file: {r.text!r}")
    check(blocking(h) == [], f"blocking {blocking(h)}")


@scenario
def s21_tests_failed(tmp):
    a = Sandbox(tmp / "A"); sha = a.failing_test()
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path, run_tests=True)
    check(h.verdict == "REWORK" and "tests_failed" in blocking(h), f"{h.verdict} {codes(h)}")
    t = reason(h, "tests_failed").text
    check("failed" in t or "bench-marker-boom" in t or "test_thing_fails" in t, f"text has no tail: {t!r}")
    check(h.elapsed > 0, "elapsed not measured")


@scenario
def s22_tests_pass_with_run_tests(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path, run_tests=True)
    check(h.verdict == "READY", f"verdict {h.verdict} codes {codes(h)}: {[r.text for r in h.reasons]}")
    check("tests_failed" not in codes(h), "tests_failed on a green suite")
    check("tests:PASS" in str(h.facts.get("tests_run", "")), f"facts.tests_run={h.facts.get('tests_run')!r}")


@scenario
def s23_last_row_for_ticket_wins(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.write_rows([(TICKET_NAME, "SKIPPED", "", "first try"), ("45-kc6-other.md", "SKIPPED", "", "x"), (TICKET_NAME, "FIXED", sha)])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"last row must win: {h.verdict} {codes(h)}")
    a.write_rows([("45-kc6-other.md", "FIXED", sha)])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check("no_progress_row" in blocking(h), f"other ticket's row must not count: {codes(h)}")
    a.write_rows([(TICKET_NAME, "FIXED", sha), (TICKET_NAME, "SKIPPED", "", "reverted")])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check("progress_not_done" in blocking(h), f"last row (SKIPPED) must win: {codes(h)}")


@scenario
def s24_pushed(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    bare = tmp / "remote.git"; git(tmp, "init", "-q", "--bare", str(bare))
    git(a.repo, "remote", "add", "origin", str(bare)); git(a.repo, "push", "-q", "origin", a.branch)
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "pushed" in blocking(h), f"{h.verdict} {codes(h)}")


@scenario
def s25_types_and_limits(tmp):
    a = Sandbox(tmp / "A"); a.good_change(test=False, off_ticket=True, shrink=True); sha = a.good_change(test=False, msg="2")
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(isinstance(h.reasons, tuple), f"reasons is {type(h.reasons)}")
    check(h.verdict in ("READY", "REWORK"), f"verdict {h.verdict!r}")
    for r in h.reasons:
        check(r.code in ALLOWED_CODES, f"unknown code {r.code!r}")
        check(isinstance(r.text, str) and r.text.strip(), f"empty text for {r.code}")
        check(len(r.text) <= 200, f"{r.code} text is {len(r.text)} chars: {r.text!r}")
        check(isinstance(r.blocking, bool), f"{r.code}.blocking is {type(r.blocking)}")
    try:
        h.reasons[0].text = "x"; raise Fail("Reason not frozen")
    except dataclasses.FrozenInstanceError:
        pass
    try:
        h.verdict = "READY"; raise Fail("Harvest not frozen")
    except dataclasses.FrozenInstanceError:
        pass


@scenario
def s26_everything_found_is_listed(tmp):
    a = Sandbox(tmp / "A"); a.good_change(test=False, off_ticket=True, shrink=True); sha = a.good_change(test=False, msg="2")
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    for c in ("commits_ne_1", "no_test_file", "shrink_changed"):
        check(c in blocking(h), f"{c} missing from {codes(h)}")
    check("off_ticket_files" in codes(h) and not reason(h, "off_ticket_files").blocking, f"off-ticket note missing/blocking: {codes(h)}")
    check(len(codes(h)) == len(set(codes(h))), f"duplicate reasons {codes(h)}")


@scenario
def s27_rework_message(tmp):
    a = Sandbox(tmp / "A"); a.good_change(test=False, off_ticket=True); sha = a.good_change(test=False, msg="2")
    a.append_task("DONE", sha)
    H = hv(); h = H.harvest(a.workspace(), a.ticket_path)
    msg = H.rework_message(h, 2, 3)
    check(isinstance(msg, str), "not a str")
    for r in h.reasons:
        check(r.text in msg, f"reason text missing from message: {r.text!r}")
    check("Attempt 2 of 3" in msg, "attempt counter missing")
    check("append_task.py" in msg, "append_task.py missing")
    check("Also noted" in msg, "'Also noted' section missing for the non-blocking reason")
    check("_shrink" in msg, "ground rule about _shrink missing")
    check("one" in msg.lower() and "commit" in msg, "ground rule about one commit missing")
    check("fails without" in msg, "ground rule 'a test that fails without the change' missing")
    check("Your ticket is not accepted yet" in msg, "fixed header missing")
    check("KC-5 test ticket" not in msg, "ticket title repeated in message")
    # order: blocking bullets before 'Also noted'
    check(msg.index(reason(h, "commits_ne_1").text) < msg.index("Also noted"), "blocking reasons must come before 'Also noted'")


@scenario
def s28_rework_message_only_blocking(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change(test=False)
    a.append_task("DONE", sha)
    H = hv(); h = H.harvest(a.workspace(), a.ticket_path)
    msg = H.rework_message(h, 1, 3)
    check("Attempt 1 of 3" in msg and reason(h, "no_test_file").text in msg, "basic content missing")
    check("Also noted" not in msg, "'Also noted' printed with nothing to note")


@scenario
def s29_clone_kind(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    clone = tmp / "clone"; git(tmp, "clone", "-q", "-b", a.branch, str(a.repo), str(clone))
    ws = a.workspace(kind="clone", path=clone)
    # the claim lives in the clone
    (clone / "runs" / a.agent).mkdir(parents=True)
    shutil.copy(BASE / "scripts" / "append_task.py", clone / "scripts" / "append_task.py")
    subprocess.run([sys.executable, str(clone / "scripts" / "append_task.py"), "--progress", str(ws.progress_csv),
                    "--ticket", TICKET_NAME, "--outcome", "DONE", "--commit", sha], check=True, capture_output=True)
    h = hv().harvest(ws, a.ticket_path)
    # a clone has origin/<branch> containing HEAD -> the mechanical 'pushed' gate fires; that is judge_worktree's rule, not ours.
    codes_ = codes(h)
    check("no_progress_row" not in codes_ and "commit_not_on_branch" not in codes_ and "commits_ne_1" not in codes_,
          f"clone: {h.verdict} {codes_}: {[r.text for r in h.reasons]}")


@scenario
def s30_real_git_worktree(tmp):
    a = Sandbox(tmp / "A")
    wt = tmp / "wt"; git(a.repo, "checkout", "-q", "main")
    git(a.repo, "worktree", "add", "-q", str(wt), a.branch)
    write(wt / "pkg" / "thing.py", "def thing():\n    return 42\n")
    write(wt / "tests" / "test_thing.py", "def test_thing():\n    assert True\n")
    sha = commit_all(wt, "KC-5 in worktree")
    ws = a.workspace(path=wt)
    subprocess.run([sys.executable, str(wt / "scripts" / "append_task.py"), "--progress", str(ws.progress_csv),
                    "--ticket", TICKET_NAME, "--outcome", "DONE", "--commit", sha], check=True, capture_output=True)
    h = hv().harvest(ws, a.ticket_path)
    check(h.verdict == "READY", f".git-file worktree: {h.verdict} {codes(h)}: {[r.text for r in h.reasons]}")


@scenario
def s31_no_progress_still_reports_facts(tmp):
    a = Sandbox(tmp / "A"); a.good_change(test=False); a.good_change(test=False, msg="2")
    h = hv().harvest(a.workspace(), a.ticket_path)
    check("no_progress_row" in blocking(h), f"{codes(h)}")
    check("commits_ne_1" in blocking(h) and "no_test_file" in blocking(h), f"facts not listed alongside the claim: {codes(h)}")
    check(isinstance(h.facts, dict) and h.facts.get("commits") == 2, f"facts {h.facts}")


@scenario
def s32_header_only_csv(tmp):
    a = Sandbox(tmp / "A"); a.good_change()
    a.write_rows([])
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "no_progress_row" in blocking(h), f"{h.verdict} {codes(h)}")
    a.progress_csv.write_text("", encoding="utf-8")
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "no_progress_row" in blocking(h), f"empty file: {h.verdict} {codes(h)}")


@scenario
def s33_no_commits_at_all(tmp):
    a = Sandbox(tmp / "A")
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "REWORK" and "no_progress_row" in blocking(h), f"{h.verdict} {codes(h)}")
    check("commits_ne_1" in codes(h), f"0 commits should be commits_ne_1: {codes(h)}")


@scenario
def s34_str_paths_accepted(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change()
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), str(a.ticket_path))
    check(h.verdict == "READY", f"str ticket_path: {h.verdict} {codes(h)}")


@scenario
def s35_declared_from_ticket_not_hardcoded(tmp):
    """A ticket that declares a different file: the same diff is now off-ticket."""
    a = Sandbox(tmp / "A"); sha = a.good_change()
    write(a.tasks / "46-kc7-x.md", "# T\n\n**File:** `pkg/other.py`  \n**Also touches:** —\n")
    a.append_task("DONE", sha, ticket="46-kc7-x.md")
    h = hv().harvest(a.workspace(), a.tasks / "46-kc7-x.md")
    check(h.verdict == "READY", f"{h.verdict} {codes(h)}")
    r = reason(h, "off_ticket_files")
    check("pkg/thing.py" in r.text and not r.blocking, f"{r}")


@scenario
def s36_tests_failed_tail_is_the_tail(tmp):
    a = Sandbox(tmp / "A"); sha = a.failing_test()
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path, run_tests=True)
    t = reason(h, "tests_failed").text
    check(re.search(r"1 failed", t), f"summary line '1 failed' not in text: {t[-300:]!r}")
    check("tests" in t, f"which root failed is not named: {t[-300:]!r}")


@scenario
def s37_tiers_check(tmp):
    r = subprocess.run([sys.executable, str(ENTRY / "scripts" / "sync_test_tiers.py"), "--check"], cwd=str(ENTRY), capture_output=True, text=True)
    check(r.returncode == 0, f"sync_test_tiers --check: {r.stdout[-400:]}")


@scenario
def s38_own_tests_pass(tmp):
    new = subprocess.run(["git", "diff", "--name-only", "--diff-filter=A", "4ff7d14..HEAD", "--", "tests/"], cwd=str(ENTRY), capture_output=True, text=True).stdout.split()
    files = [f for f in new if os.path.basename(f).startswith("test_")]
    check(files, "no test file shipped")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout=300", *files],
                       cwd=str(ENTRY), capture_output=True, text=True)
    check(r.returncode == 0, "own tests: " + "\n".join((r.stdout or "").strip().splitlines()[-15:]))


@scenario
def s39_stdlib_only_no_print(tmp):
    bad = []
    for f in ("tools/contest/gates.py", "tools/contest/harvest.py"):
        src = (ENTRY / f).read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M):
            top = m.group(1).split(".")[0]
            if top in ("pytest", "yaml", "requests"):
                bad.append(f"{f}: {top}")
        if re.search(r"^\s*print\(", src, re.M):
            bad.append(f"{f}: print()")
        if re.search(r"sys\.exit\(", src):
            bad.append(f"{f}: sys.exit")
    check(not bad, "; ".join(bad))


CONFTEST_COUNTER = """import os
def pytest_sessionstart(session):
    with open(os.path.join(os.path.dirname(__file__), "..", "sessions.log"), "a") as fh:
        fh.write("session\\n")
"""


def _sessions(sb):
    p = sb.repo / "sessions.log"
    return p.read_text().count("session") if p.exists() else 0


@scenario
def s40_failing_suite_runs_pytest_once(tmp):
    a = Sandbox(tmp / "A")
    write(a.repo / "tests" / "conftest.py", CONFTEST_COUNTER)
    sha = a.failing_test()
    a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path, run_tests=True)
    check("tests_failed" in blocking(h), f"{codes(h)}")
    n = _sessions(a)
    check(n == 1, f"pytest started {n} times for one failing root — the tail must come from the same run")


@scenario
def s41_no_pytest_unless_asked(tmp):
    a = Sandbox(tmp / "A")
    write(a.repo / "tests" / "conftest.py", CONFTEST_COUNTER)
    sha = a.good_change(); a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.verdict == "READY", f"{h.verdict} {codes(h)}")
    check(_sessions(a) == 0, "pytest ran although run_tests=False")


@scenario
def s42_commit_is_the_claimed_sha(tmp):
    a = Sandbox(tmp / "A"); sha = a.good_change(); a.append_task("DONE", sha)
    h = hv().harvest(a.workspace(), a.ticket_path)
    check(h.commit == sha, f"commit {h.commit!r} != claimed {sha!r}")


@scenario
def s45_declared_files_missing_ticket_fails_loud(tmp):
    g = gates()
    try:
        out = g.declared_files(tmp / "nope.md")
    except (FileNotFoundError, OSError):
        return
    raise Fail(f"missing ticket returned {out!r} instead of raising")


def main():
    name = sys.argv[1]
    fn = SCENARIOS[name]
    tmp = Path(tempfile.mkdtemp(prefix=f"kc5-{name}-"))
    try:
        fn(tmp)
        print("PASS")
    except Fail as e:
        print(f"FAIL {e}")
    except Exception:
        tb = traceback.format_exc().strip().splitlines()
        print("ERROR " + " | ".join(tb[-3:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1] == "--list":
        print("\n".join(SCENARIOS))
    else:
        main()
