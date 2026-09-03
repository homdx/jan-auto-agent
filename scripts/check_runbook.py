#!/usr/bin/env python3
"""scripts/check_runbook.py — CHECK-1: did each flow actually do its job?

``pytest`` proves the machinery is wired correctly. It cannot prove that a
run produced the right artefact, because the artefact is written by a model
at runtime. This script closes that gap: it inspects what a flow left on
disk and reports whether it matches what the flow was supposed to do.

Every check is **deterministic** — no LLM, no network. "Does ``test_main.py``
exist", "did ``README.md`` change", "is the seed changelog entry still there"
all have exact answers on disk. That matters twice over: the checks cost
nothing, and they cannot themselves hallucinate a pass. The one place a model
would be needed — judging prose *quality* — is deliberately out of scope; the
gates do that during the run.

Why this exists: the docs flow was once judged "working" because it exited 0
and committed. It had in fact written a Python test file and never touched
``README.md`` at all. Nothing in the test suite could have caught that.

Layout
------
Each task is checked against its own sandbox if one exists, otherwise the
shared one::

    examples/task1/hello-world/   → used for task 1 if present
    examples/task2/hello-world/   → used for task 2 if present
    examples/task3/hello-world/   → used for task 3 if present
    examples/task4/hello-world/   → used for task 4 if present
    examples/hello-world/         → fallback for any task without its own

Separate sandboxes are strongly preferred: the flows commit into their base
directory, so running several against one directory means a later task
inspects an earlier task's output and the results are not independent.

Task 4 (GATE3-PROFILE-5) reuses every check from task 3 — same skill body,
same base — plus one more: evidence from the console log that canon and
continuity actually resolved to a DIFFERENT provider than the shared one the
coder and Gate 2 used. That extra finding is a WARN, not a FAIL, when the
console log shows every validator on the same provider — task 4 needs a
second real, reachable LLM endpoint ([task4_provider_b]) that is not always
available (a fresh clone, most CI), and a check that FAILS whenever that
endpoint is absent would just be a second, noisier way of saying so. See
check_task4's docstring.

Usage
-----
    python3 scripts/check_runbook.py --task 1
    python3 scripts/check_runbook.py --all
    python3 scripts/check_runbook.py --all --json

Exit code is 0 when every requested check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_SANDBOX = REPO_ROOT / "examples" / "hello-world"

TASKS = (1, 2, 3, 4)
TASK_SKILL = {
    1: "hello-code", 2: "hello-docs", 3: "hello-creative",
    4: "hello-creative-split",
}

_GREEN, _RED, _YELLOW, _DIM, _RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)


@dataclass
class Finding:
    ok: bool
    label: str
    #: Factual observation — what was actually seen. Always shown.
    detail: str = ""
    #: Why it matters and what to do. Shown ONLY when the finding fails.
    #: Separate from `detail` because a message written to explain a failure
    #: reads as a flat contradiction beside PASS. Real output from the first
    #: live run: "[PASS] seed entry preserved — the coder rewrote the file
    #: wholesale instead of prepending".
    hint: str = ""
    #: A finding that is informational rather than pass/fail.
    warn: bool = False


@dataclass
class Report:
    task: int
    skill: str
    sandbox: Path
    findings: list[Finding] = field(default_factory=list)

    def add(
        self, ok: bool, label: str, detail: str = "",
        hint: str = "", warn: bool = False,
    ) -> None:
        self.findings.append(Finding(ok, label, detail, hint, warn))

    @property
    def failed(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and not f.warn]

    @property
    def passed(self) -> bool:
        return not self.failed


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox resolution
# ─────────────────────────────────────────────────────────────────────────────

def sandbox_for(task: int, repo_root: Path = REPO_ROOT) -> Path:
    """Per-task sandbox if it exists, else the shared one."""
    specific = repo_root / "examples" / f"task{task}" / "hello-world"
    if specific.is_dir():
        return specific
    return repo_root / "examples" / "hello-world"


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _baseline(name: str) -> str:
    """The committed baseline for *name*, read from git rather than assumed."""
    result = subprocess.run(
        ["git", "show", f"HEAD:examples/hello-world/{name}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _prints_hello_world(sandbox: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, "main.py"],
            cwd=str(sandbox), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {result.stderr.strip()[:120]}"
    out = result.stdout.strip()
    return out == "Hello world", f"printed {out!r}"


def _run_ran(sandbox: Path) -> bool:
    return (sandbox / ".agent").is_dir()


#: Directories that are not part of the sandbox's real working tree: the
#: per-task workspace mirrors under .agent/workspace/<task_id>/ each copy
#: the WHOLE repo (see AUTO-T*/AUTO-G* mirroring in executor.py), so an
#: unfiltered recursive search finds every test file once per mirror on
#: top of the real one — or, on a run where the real file never landed,
#: finds only a stale copy inside a mirror and reports a false PASS.
_NON_WORKTREE_DIRS = {".agent", ".git", "__pycache__", ".venv", "venv", "node_modules"}


def _find_test_files(sandbox: Path) -> list[Path]:
    """Every ``test_*.py`` in the sandbox's real working tree, at any depth.

    CHECK-1 bug (found via a live run): the original version only checked
    ``sandbox.glob("test_*.py")`` — the sandbox's TOP LEVEL only. A run
    where the Architect filed the test at ``tests/test_main.py`` (a
    perfectly normal, arguably more conventional choice) was reported as
    "no test file created" even though the file existed and its own
    ``acceptance_check`` (``pytest tests/test_main.py -q``) had already
    passed during the run. Returns paths relative to *sandbox*.
    """
    found: list[Path] = []
    for p in sorted(sandbox.rglob("test_*.py")):
        if p.name.endswith(".coder.bak"):
            continue
        rel = p.relative_to(sandbox)
        if any(part in _NON_WORKTREE_DIRS for part in rel.parts):
            continue
        found.append(rel)
    return found


def _plan_targets(sandbox: Path) -> list[str]:
    plan = sandbox / ".agent" / "plan.json"
    if not plan.is_file():
        return []
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for task in data.get("tasks", []):
        out.extend(task.get("target_files") or [])
    return out


def _add_run_context(report: Report, sandbox: Path) -> bool:
    """Common preamble. Returns False when there is nothing to check."""
    if not sandbox.is_dir():
        report.add(False, "sandbox exists", f"{sandbox} not found")
        return False
    if not _run_ran(sandbox):
        report.add(False, "flow was run", f"no .agent/ in {sandbox} — run the flow first")
        return False
    report.add(True, "flow was run", str(sandbox))

    # run_flows.sh writes the console log next to the sandbox. Naming it here
    # saves hunting for it: every "the flow did nothing" diagnosis needs it,
    # and none of the on-disk checks can explain WHY nothing happened.
    log = sandbox.parent / "console-log.txt"
    if log.is_file():
        report.add(True, "console log", str(log), warn=True)

    targets = _plan_targets(sandbox)
    unique = sorted(set(targets))
    detail = ", ".join(unique) or "none"
    if len(targets) != len(unique):
        detail += f" ({len(targets)} task(s), {len(unique)} distinct file(s))"
    report.add(
        bool(targets), "plan has target files", detail,
        hint="the Architect proposed nothing, or Gate 1 rejected every "
             "candidate — check the console log for 'gate1 accepted='",
    )

    progress = sandbox / ".agent" / "progress.json"
    if progress.is_file():
        try:
            data = json.loads(progress.read_text(encoding="utf-8"))
            done = data.get("done_count", 0) or 0
            stop = data.get("stop_reason")
            if stop:
                report.add(
                    False, "run completed", f"stop_reason={stop}, {done} task(s) done",
                    hint="task_cap means auto.max_tasks_per_run stopped it",
                    warn=(stop == "task_cap" and done > 0),
                )
            else:
                # A run that completed ZERO tasks is not a completed run. This
                # passed in the first live run and made a 436-second no-op look
                # like a success with a couple of cosmetic complaints.
                report.add(
                    done > 0, "run completed", f"{done} task(s) done",
                    hint="the run finished without doing any work at all",
                )
        except (OSError, json.JSONDecodeError):
            pass
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Task 1 — code hardening
# ─────────────────────────────────────────────────────────────────────────────

def check_task1(sandbox: Path) -> Report:
    report = Report(1, TASK_SKILL[1], sandbox)
    if not _add_run_context(report, sandbox):
        return report

    main_py = sandbox / "main.py"
    source = _read(main_py)
    report.add(bool(source), "main.py present")

    ok, detail = _prints_hello_world(sandbox)
    report.add(ok, "main.py still prints Hello world", detail)

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.add(False, "main.py parses", str(exc))
        return report
    report.add(True, "main.py parses")

    report.add(
        ast.get_docstring(tree) is not None,
        "module docstring",
        hint="check that no import was inserted above it — that silently "
             "demotes the docstring to a plain string",
    )

    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    main_fn = next((f for f in funcs if f.name == "main"), None)
    report.add(main_fn is not None, "main() defined")
    if main_fn is not None:
        report.add(ast.get_docstring(main_fn) is not None, "main() docstring")
        report.add(main_fn.returns is not None, "main() return annotation")

    test_files = _find_test_files(sandbox)
    report.add(
        bool(test_files), "test file created",
        ", ".join(str(p) for p in test_files) or "none",
        hint=_diagnose_no_candidates(sandbox) or (
            "the Architect never proposed one, or the task cap cut it off — "
            "check plan.json and auto.max_tasks_per_run"
        ),
    )

    if test_files:
        rel = [str(p) for p in test_files]
        # Bugfix: unguarded — a hung test raised TimeoutExpired and
        # crashed the whole checker instead of reporting the failure, unlike
        # _prints_hello_world's guard above. The static "uses pytest
        # conventions" check below still runs after a timeout.
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *rel],
                cwd=str(sandbox), capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            report.add(
                False, "test suite passes",
                f"timed out after {exc.timeout}s — "
                "test suite may hang or be too slow",
            )
        else:
            report.add(
                result.returncode == 0,
                "test suite passes",
                (result.stdout + result.stderr).strip().splitlines()[-1][:120]
                if (result.stdout or result.stderr) else "",
            )
        uses_pytest = any(
            "def test_" in _read(sandbox / p) for p in test_files
        )
        report.add(
            uses_pytest, "test uses pytest conventions",
            hint="no `def test_*` function found — pytest collects nothing",
            warn=True,
        )
    return report


#: Matches Gate 1's own rejection line, e.g.:
#:   Gate1[existence] REJECTED 'Create README.md ...' — new_file='README.md'
#:   but this path already exists — not a new file; drop new_file or cite
#:   the real content
_GATE1_REJECT_RE = re.compile(r"Gate1\[[a-z_]+\] REJECTED '([^']*)' — (.+)")
#: Matches the architect fully giving up on a cluster after exhausting its
#: empty/non-JSON retry budget (see [architect] empty_response_retry_max).
_ARCHITECT_GIVEUP_RE = re.compile(
    r"review_one_cluster \[([^\]]+)\]: still unsalvageable after \d+ .*?"
    r"giving up on this batch with 0 candidates"
)


def _diagnose_no_candidates(sandbox: Path) -> str:
    """Read console-log.txt for the ACTUAL reason nothing landed, instead of
    asserting one fixed hypothesis for every occurrence of this failure.

    CHECK-1 bug (found via a live run): the previous version of this check
    hard-coded a single historical root cause ("Gate 1 cannot evidence a
    missing Usage section with a verbatim quote") into every FAIL of
    "README.md was modified". A subsequent run failed the same finding for
    two entirely different reasons — Gate 1 rejecting a candidate that
    mislabelled an existing file as new, and the architect giving up on the
    'support' cluster after six consecutive empty/non-JSON responses over
    ~19 minutes — neither of which matches that hard-coded story. Repeating
    a specific-sounding but wrong diagnosis is worse than a generic pointer:
    it sends the next person chasing the wrong fix. This is deterministic
    (reads a log file already on disk) and degrades to "" — the caller's
    generic fallback — when nothing recognisable is found, e.g. no console
    log at all.
    """
    log = _read(sandbox.parent / "console-log.txt")
    if not log:
        return ""
    reasons: list[str] = []
    for m in _GATE1_REJECT_RE.finditer(log):
        reasons.append(f"Gate 1 rejected {m.group(1)!r} — {m.group(2).strip()}")
    for m in _ARCHITECT_GIVEUP_RE.finditer(log):
        reasons.append(
            f"architect gave up on cluster {m.group(1)!r} after repeated "
            "empty/non-JSON responses"
        )
    seen: set[str] = set()
    unique = [r for r in reasons if not (r in seen or seen.add(r))]
    return "; ".join(unique)


# ─────────────────────────────────────────────────────────────────────────────
# Task 2 — documentation
# ─────────────────────────────────────────────────────────────────────────────

def check_task2(sandbox: Path) -> Report:
    report = Report(2, TASK_SKILL[2], sandbox)
    if not _add_run_context(report, sandbox):
        return report

    readme = sandbox / "README.md"
    text = _read(readme)
    report.add(bool(text), "README.md present")

    baseline = _baseline("README.md")
    changed = bool(baseline) and text.strip() != baseline.strip()
    report.add(
        changed, "README.md was modified",
        hint=_diagnose_no_candidates(sandbox) or (
            "identical to the committed baseline — the docs flow wrote "
            "nothing. Check the console log for 'Gate1[' rejections or "
            "'giving up on this batch'"
        ),
    )

    report.add(
        bool(re.search(r"^##\s+Usage", text, re.MULTILINE)),
        "README has a Usage section",
    )

    targets = _plan_targets(sandbox)
    py_targets = [t for t in targets if t.endswith(".py")]
    report.add(
        not py_targets, "no .py file was targeted",
        ", ".join(py_targets),
        hint="the docs skill forbids modifying .py files",
    )

    # Reuse the shipped existence gate rather than reimplementing it, so this
    # check and the in-run gate can never disagree about what counts.
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tools.auto.existence_validator import ExistenceValidator

        verdict = ExistenceValidator().check(text, sandbox, rel_path="README.md")
        report.add(
            verdict.approved, "no references to missing files",
            verdict.feedback().replace("\n", " ")[:200],
        )
    except Exception as exc:  # noqa: BLE001
        report.add(True, "existence check", f"skipped: {exc}", warn=True)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Task 3 — narrative changelog
# ─────────────────────────────────────────────────────────────────────────────

_ENTRY_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)


def _entry_text(markdown: str, heading: str) -> str:
    """The full text of one ``### <heading>`` entry — heading line through
    the next ``### `` heading or end of file — or ``""`` if absent."""
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\s*$.*?(?=\n###\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(markdown)
    return m.group(0).strip() if m else ""


def check_task3(sandbox: Path) -> Report:
    report = Report(3, TASK_SKILL[3], sandbox)
    if not _add_run_context(report, sandbox):
        return report

    changelog = sandbox / "CHANGELOG.md"
    text = _read(changelog)
    report.add(bool(text), "CHANGELOG.md present")

    entries = _ENTRY_RE.findall(text)
    report.add(
        len(entries) >= 2, "a new entry was added", f"{len(entries)} entry/entries",
        hint=_diagnose_no_candidates(sandbox) or (
            "only the seed entry is present — the flow added nothing"
        ),
    )

    report.add(
        "The first greeting" in text, "seed entry preserved",
        hint="the coder rewrote the file wholesale instead of prepending; "
             "`continuity` then has no predecessor to compare against and "
             "its approval means less than it appears to",
    )

    # CHECK-1 bug (found via a live run): the check above only looks for the
    # HEADING string. A run where the architect never saw the real
    # CHANGELOG.md (its cluster review came back empty) reconstructed the
    # seed entry from a guess and the coder overwrote its prose wholesale —
    # same heading, different words — and "seed entry preserved" read as a
    # bare PASS right next to "a new entry was added" correctly failing.
    # This compares the actual committed seed entry (not just its heading)
    # against what is on disk now. Skips (warn) rather than fails when there
    # is no baseline to diff against, e.g. a repo without the usual
    # examples/hello-world/CHANGELOG.md history.
    baseline_changelog = _baseline("CHANGELOG.md")
    seed_entry = _entry_text(baseline_changelog, "The first greeting")
    if seed_entry:
        report.add(
            seed_entry in text, "seed entry text unchanged",
            hint="the heading survived but the coder rewrote the seed "
                 "entry's own prose instead of leaving it untouched and "
                 "prepending a new entry above it — check whether the "
                 "architect's cluster review that shows CHANGELOG.md's "
                 "real content actually returned candidates, or came back "
                 "empty/rate-limited and left the coder guessing",
        )
    else:
        report.add(
            True, "seed entry text unchanged",
            "skipped: no baseline CHANGELOG.md to diff against", warn=True,
        )

    report.add("Hello world" in text, "canon fact intact")

    body = text.split("### ", 1)[-1] if "### " in text else ""
    report.add("```" not in body, "prose only — no code fences")

    baseline_main = _baseline("main.py")
    if baseline_main:
        report.add(
            _read(sandbox / "main.py").strip() == baseline_main.strip(),
            "main.py untouched",
            hint="the creative flow modified source code",
        )

    targets = _plan_targets(sandbox)
    py_targets = [t for t in targets if t.endswith(".py")]
    report.add(
        not py_targets, "no .py file was targeted",
        ", ".join(py_targets),
        hint="a creative task pointed at source code is the failure mode "
             "that once burned 10 feedback rounds without converging",
    )
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — narrative changelog, canon/continuity on a second provider
# ─────────────────────────────────────────────────────────────────────────────
# GATE3-PROFILE-5: proves the GATE3-PROFILE-2 per-validator profile keys work
# on a real run, not only in unit tests. hello-creative-split is the same
# skill body as hello-creative (same base, same artefact contract), with
# [validator_agent] canon_llm_profile / continuity_llm_profile pointed at
# [task4_provider_b] via its [skill.overlay] — see skills/hello-creative-split
# .skill.ini. So this reuses check_task3 wholesale for the artefact criteria,
# then adds one more finding this task alone can make: did canon/continuity
# actually go to a DIFFERENT provider than the coder and Gate 2 did.

#: Matches GATE3-PROFILE-2's per-gate startup line, e.g.:
#:   validator_agent.canon_llm_profile: provider = https://b.example/v1 (m) — via ...
_GATE_PROVIDER_RE = re.compile(
    r"validator_agent\.(canon|continuity)_llm_profile: provider = (\S+) \("
)
#: Matches the shared-provider step every enabled gate resolves through
#: first (GATE3-PROFILE-2's `validator_llm_profile` link in the chain),
#: which is what the coder and Gate 2 also use — the baseline "provider A".
_SHARED_PROVIDER_RE = re.compile(
    r"validator_agent\.validator_llm_profile: provider = (\S+) \("
)


def _add_provider_split_evidence(report: Report, sandbox: Path) -> None:
    """Did canon/continuity resolve to a different provider than the coder.

    A WARN (not a FAIL) whenever the evidence can't be found or shows only
    one provider in play: this needs a second real, reachable LLM endpoint
    ([task4_provider_b]) that the operator configures locally and that is
    not always available. `Report.failed` excludes warned findings (see its
    property above), so `check_runbook.py --task 4` still exits 0 on an
    otherwise-healthy single-provider run rather than failing a check it
    structurally cannot perform — "skip with an explicit reason" rather than
    a silent, vacuous pass on a property that was never actually verified.
    """
    log = _read(sandbox.parent / "console-log.txt")
    if not log:
        report.add(
            False, "canon/continuity used a second provider",
            "no console-log.txt found next to the sandbox",
            hint="run_flows.sh writes it automatically — run the flow "
                 "through run_flows.sh rather than main.py directly, or "
                 "copy your own log to examples/task4/console-log.txt",
            warn=True,
        )
        return

    resolved = dict(_GATE_PROVIDER_RE.findall(log))  # {"canon": url, ...}
    if not resolved:
        report.add(
            False, "canon/continuity used a second provider",
            "no canon_llm_profile/continuity_llm_profile startup line in "
            "the console log",
            hint="GATE3-PROFILE-2 logs one INFO line per gate naming its "
                 "resolved provider — check the flow actually used "
                 "skills/hello-creative-split.skill.ini",
            warn=True,
        )
        return

    shared_match = _SHARED_PROVIDER_RE.search(log)
    shared_url = shared_match.group(1) if shared_match else None
    split = {
        gate: url for gate, url in resolved.items()
        if shared_url is None or url != shared_url
    }

    if not split:
        report.add(
            False, "canon/continuity used a second provider",
            f"canon/continuity resolved to {sorted(set(resolved.values()))}, "
            f"same as the shared provider — [task4_provider_b] is not "
            f"configured (or is identical to [api_{{active}}]) for this run",
            hint="add a [task4_provider_b] section with a second real "
                 "base_url/api_key/model to your config before running "
                 "task 4 — see RUNBOOK.md 'Flow 4'",
            warn=True,
        )
        return

    report.add(
        True, "canon/continuity used a second provider",
        ", ".join(f"{gate}={url}" for gate, url in sorted(split.items())),
    )


def check_task4(sandbox: Path) -> Report:
    report = check_task3(sandbox)
    report.task = 4
    report.skill = TASK_SKILL[4]
    if _run_ran(sandbox):
        _add_provider_split_evidence(report, sandbox)
    return report


CHECKS = {1: check_task1, 2: check_task2, 3: check_task3, 4: check_task4}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Weighted scoring — a percentage on top of the pass/fail findings above
# ─────────────────────────────────────────────────────────────────────────────
#
# check_task1/2 report binary findings: a run either satisfies "test suite
# passes" or it doesn't. That's the right tool for "should CI go green", but
# it throws away information a human reviewing a run actually wants — a run
# that hardened main.py but forgot one docstring is not the same outcome as
# a run that left main.py untouched, and PASS/FAIL alone can't tell them
# apart. WEIGHTS turns the SAME findings check_task1/2 already computed into
# a 0-100 score instead of a second, independent judgement — this can never
# disagree with the PASS/FAIL verdict about WHAT happened, only about how
# much a given gap should cost.
#
# GATES are findings whose failure caps the score outright, no matter how
# many other points were earned: points for "wrote docstrings" mean nothing
# if the docs run edited main.py instead of README.md, or if the code run's
# one required behaviour (prints "Hello world") broke. A capped run can
# still land above 0% — a gate failure means "this run violated a hard
# constraint", not "this run did nothing".
#
# Only tasks 1 and 2 are weighted below. Task 3/4 (creative) are deliberately
# left out — RUNBOOK.md is explicit that judging prose quality is out of
# scope for this deterministic checker; a creative rubric needs a different
# kind of criterion (canon/continuity) that belongs to the Gate-3 validators
# at runtime, not to a percentage computed after the fact.

@dataclass(frozen=True)
class ScoreBand:
    floor: int
    label: str
    meaning: str


BANDS: tuple[ScoreBand, ...] = (
    ScoreBand(100, "Excellent", "every weighted check passed — nothing left to fix"),
    ScoreBand(85, "Solid", "the deliverable is there and working; only cosmetic gaps remain"),
    ScoreBand(65, "Partial", "the deliverable landed but is noticeably incomplete"),
    ScoreBand(40, "Weak", "the deliverable is mostly missing or badly broken"),
    ScoreBand(0, "Failed", "a hard constraint was violated or nothing usable landed"),
)


def band_for(pct: float) -> ScoreBand:
    for b in BANDS:
        if pct >= b.floor:
            return b
    return BANDS[-1]  # pragma: no cover — 0 is always in range


#: label -> points earned when that finding is present and OK. Each task's
#: points sum to 100 so the raw total doubles as the percentage.
WEIGHTS: dict[int, dict[str, int]] = {
    1: {  # hello-code
        "main.py present": 5,
        "main.py still prints Hello world": 20,
        "main.py parses": 5,
        "main() defined": 10,
        "test file created": 15,
        "test suite passes": 25,
        "module docstring": 7,
        "main() docstring": 7,
        "main() return annotation": 6,
    },
    2: {  # hello-docs
        "README.md present": 5,
        "README.md was modified": 30,
        "README has a Usage section": 25,
        "no .py file was targeted": 20,
        "no references to missing files": 20,
    },
}

#: label -> cap. If that finding fails, the score cannot exceed cap even
#: though other points were earned.
GATES: dict[int, dict[str, int]] = {
    1: {
        "main.py parses": 10,                    # broken syntax — nothing else here is trustworthy
        "main.py still prints Hello world": 15,   # regressed the one behaviour that must survive
    },
    2: {
        "no .py file was targeted": 30,           # a docs run touching code violates the skill contract
    },
}


@dataclass
class Score:
    applicable: bool  # False when the flow never ran — nothing to grade
    pct: float = 0.0
    band: ScoreBand = BANDS[-1]
    capped_by: tuple[str, ...] = ()


def compute_score(report: Report) -> Score | None:
    """None when task has no rubric yet (3/4). Score.applicable is False
    when the flow never ran (score of 0 would misleadingly read as 'tried
    and failed everything' rather than 'nothing to grade')."""
    weights = WEIGHTS.get(report.task)
    if weights is None:
        return None
    by_label = {f.label: f for f in report.findings}
    ran = by_label.get("flow was run")
    if ran is not None and not ran.ok:
        return Score(applicable=False)

    total = sum(weights.values())
    earned = sum(
        points for label, points in weights.items()
        if (f := by_label.get(label)) is not None and f.ok
    )
    pct = (earned / total * 100) if total else 0.0

    cap = 100
    capped_by: list[str] = []
    for label, cap_value in GATES.get(report.task, {}).items():
        f = by_label.get(label)
        if f is not None and not f.ok:
            cap = min(cap, cap_value)
            capped_by.append(label)
    pct = min(pct, cap)

    return Score(applicable=True, pct=pct, band=band_for(pct), capped_by=tuple(capped_by))


def render(report: Report, colour: bool = True) -> str:
    def paint(code: str, s: str) -> str:
        return f"{code}{s}{_RESET}" if colour else s

    head = f"Task {report.task} ({report.skill}) — {report.sandbox}"
    lines = [head, "-" * len(head)]
    for f in report.findings:
        if f.ok:
            mark, code = "PASS", _GREEN
        elif f.warn:
            mark, code = "WARN", _YELLOW
        else:
            mark, code = "FAIL", _RED
        line = f"  [{paint(code, mark)}] {f.label}"
        if f.detail.strip():
            line += paint(_DIM, f"  — {f.detail.strip()}")
        if not f.ok and f.hint.strip():
            line += paint(_DIM, f"  — {f.hint.strip()}")
        lines.append(line)
    verdict = "OK" if report.passed else f"{len(report.failed)} failure(s)"
    lines.append(f"  => {paint(_GREEN if report.passed else _RED, verdict)}")

    score = compute_score(report)
    if score is not None:
        if not score.applicable:
            lines.append(paint(_DIM, "  => score: n/a — flow never ran"))
        else:
            band_code = _GREEN if score.pct >= 85 else _YELLOW if score.pct >= 40 else _RED
            score_line = f"  => score: {score.pct:.0f}% ({score.band.label})"
            if score.capped_by:
                score_line += f" — capped by: {', '.join(score.capped_by)}"
            lines.append(paint(band_code, score_line))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=int, choices=TASKS, help="check one task")
    parser.add_argument("--all", action="store_true", help="check every task")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-colour", action="store_true")
    args = parser.parse_args()

    if not args.task and not args.all:
        parser.error("pass --task N or --all")

    tasks = TASKS if args.all else (args.task,)
    reports = [CHECKS[t](sandbox_for(t)) for t in tasks]

    if args.json:
        def score_json(r: Report) -> dict | None:
            s = compute_score(r)
            if s is None:
                return None
            if not s.applicable:
                return {"applicable": False}
            return {
                "applicable": True, "pct": round(s.pct, 1), "band": s.band.label,
                "capped_by": list(s.capped_by),
            }

        print(json.dumps([
            {
                "task": r.task, "skill": r.skill, "sandbox": str(r.sandbox),
                "passed": r.passed, "score": score_json(r),
                "findings": [
                    {
                        "ok": f.ok, "warn": f.warn, "label": f.label,
                        "detail": f.detail, "hint": f.hint,
                    }
                    for f in r.findings
                ],
            }
            for r in reports
        ], indent=2))
    else:
        for report in reports:
            print(render(report, colour=not args.no_colour))
            print()
        failed = [r.task for r in reports if not r.passed]
        if failed:
            print(f"FAILED: task(s) {', '.join(map(str, failed))}")
        else:
            print(f"All {len(reports)} task(s) passed.")

    return 0 if all(r.passed for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
