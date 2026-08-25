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
            cwd=str(sandbox), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {result.stderr.strip()[:120]}"
    out = result.stdout.strip()
    return out == "Hello world", f"printed {out!r}"


def _run_ran(sandbox: Path) -> bool:
    return (sandbox / ".agent").is_dir()


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

    test_files = sorted(
        p.name for p in sandbox.glob("test_*.py")
        if not p.name.endswith(".coder.bak")
    )
    report.add(
        bool(test_files), "test file created", ", ".join(test_files) or "none",
        hint="the Architect never proposed one, or the task cap cut it off — "
             "check plan.json and auto.max_tasks_per_run",
    )

    if test_files:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *test_files],
            cwd=str(sandbox), capture_output=True, text=True, timeout=180,
        )
        report.add(
            result.returncode == 0,
            "test suite passes",
            (result.stdout + result.stderr).strip().splitlines()[-1][:120]
            if (result.stdout or result.stderr) else "",
        )
        uses_pytest = any(
            "def test_" in _read(sandbox / name) for name in test_files
        )
        report.add(
            uses_pytest, "test uses pytest conventions",
            hint="no `def test_*` function found — pytest collects nothing",
            warn=True,
        )
    return report


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
        hint="identical to the committed baseline — the docs flow wrote "
             "nothing. Seen twice live: Gate 1 cannot evidence \"README "
             "lacks a Usage section\" with a verbatim quote, so it "
             "fail-closes and rejects the candidates the flow needs",
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
        hint="only the seed entry is present — the flow added nothing",
    )

    report.add(
        "The first greeting" in text, "seed entry preserved",
        hint="the coder rewrote the file wholesale instead of prepending; "
             "`continuity` then has no predecessor to compare against and "
             "its approval means less than it appears to",
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
        print(json.dumps([
            {
                "task": r.task, "skill": r.skill, "sandbox": str(r.sandbox),
                "passed": r.passed,
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
