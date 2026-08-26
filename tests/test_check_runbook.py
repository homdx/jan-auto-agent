"""CHECK-1 — the post-run validator and the flow runner.

``pytest`` proves the machinery is wired. It cannot prove a *run* produced
the right artefact, because the artefact is written by a model at runtime.
``scripts/check_runbook.py`` closes that gap, and this file proves the
checker itself works — including, crucially, that it FAILS on the outputs it
is supposed to reject.

A checker that only ever passes is worse than no checker: it converts "we
did not look" into "we looked and it was fine". So most of what follows
builds a deliberately wrong sandbox and asserts the specific finding fires.

The motivating case is real. A docs run was judged working because it exited
0 and committed. It had written a Python test file and never touched
``README.md``. Nothing in the test suite could have caught it; the checker
catches it in four separate findings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_runbook.py"
RUNNER = REPO_ROOT / "scripts" / "run_flows.sh"
BASELINE = REPO_ROOT / "examples" / "hello-world"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — a sandbox that looks like a finished run
# ─────────────────────────────────────────────────────────────────────────────

def _make_sandbox(tmp_path: Path, task: int, *, plan_targets: list[str]) -> Path:
    """Build examples/task<N>/hello-world inside a fake repo root."""
    root = tmp_path / "repo"
    sandbox = root / "examples" / f"task{task}" / "hello-world"
    sandbox.mkdir(parents=True)
    # Seed from git HEAD, exactly as scripts/run_flows.sh does. Copying from
    # the WORKING TREE instead would silently diverge whenever a baseline file
    # has uncommitted edits, and the checker compares against HEAD — so the
    # sandbox would look "modified" before the flow had done anything.
    for name in ("main.py", "README.md", "CHANGELOG.md"):
        blob = subprocess.run(
            ["git", "show", f"HEAD:examples/hello-world/{name}"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        (sandbox / name).write_text(blob.stdout, encoding="utf-8")
    agent = sandbox / ".agent"
    agent.mkdir()
    (agent / "plan.json").write_text(
        json.dumps({"tasks": [{"id": "AUTO-T1", "target_files": plan_targets}]}),
        encoding="utf-8",
    )
    (agent / "progress.json").write_text(
        json.dumps({"status": "idle", "done_count": 1}), encoding="utf-8"
    )
    return sandbox


def _check(task: int, sandbox: Path) -> dict:
    """Run one check function directly and return its findings by label."""
    import check_runbook

    report = check_runbook.CHECKS[task](sandbox)
    return {f.label: f for f in report.findings}


def _report(task: int, sandbox: Path):
    import check_runbook

    return check_runbook.CHECKS[task](sandbox)


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_falls_back_to_the_shared_sandbox(tmp_path):
    import check_runbook

    root = tmp_path / "repo"
    (root / "examples" / "hello-world").mkdir(parents=True)
    assert check_runbook.sandbox_for(2, root).name == "hello-world"
    assert "task2" not in str(check_runbook.sandbox_for(2, root))


def test_prefers_the_per_task_sandbox(tmp_path):
    import check_runbook

    root = tmp_path / "repo"
    (root / "examples" / "hello-world").mkdir(parents=True)
    (root / "examples" / "task2" / "hello-world").mkdir(parents=True)
    assert "task2" in str(check_runbook.sandbox_for(2, root))


@pytest.mark.parametrize("task", [1, 2, 3, 4])
def test_each_task_resolves_independently(tmp_path, task):
    import check_runbook

    root = tmp_path / "repo"
    (root / "examples" / "hello-world").mkdir(parents=True)
    (root / "examples" / f"task{task}" / "hello-world").mkdir(parents=True)
    for other in (1, 2, 3, 4):
        resolved = str(check_runbook.sandbox_for(other, root))
        if other == task:
            assert f"task{task}" in resolved
        else:
            assert f"task{other}" not in resolved


# ─────────────────────────────────────────────────────────────────────────────
# "Was it even run?"
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("task", [1, 2, 3, 4])
def test_missing_agent_dir_is_a_failure(tmp_path, task):
    """A pristine sandbox must not read as a passing run."""
    root = tmp_path / "repo"
    sandbox = root / "examples" / "hello-world"
    sandbox.mkdir(parents=True)
    shutil.copy(BASELINE / "main.py", sandbox / "main.py")
    report = _report(task, sandbox)
    assert not report.passed
    assert any("flow was run" in f.label for f in report.failed)


@pytest.mark.parametrize("task", [1, 2, 3, 4])
def test_absent_sandbox_is_a_failure(tmp_path, task):
    assert not _report(task, tmp_path / "nope").passed


# ─────────────────────────────────────────────────────────────────────────────
# Task 2 — the real observed failure
# ─────────────────────────────────────────────────────────────────────────────

def test_docs_run_that_wrote_a_py_file_is_rejected(tmp_path):
    """Verbatim reproduction of the observed run.

    Gate 1 rejected every README candidate and kept 'Add test suite for
    main.py', so the docs flow committed a Python file and left README.md
    untouched — while exiting 0.
    """
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["test_main.py"])
    (sandbox / "test_main.py").write_text("def test_x(): pass\n", encoding="utf-8")

    report = _report(2, sandbox)
    labels = {f.label for f in report.failed}

    assert not report.passed
    assert "README.md was modified" in labels
    assert "no .py file was targeted" in labels


def test_unmodified_readme_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    assert _check(2, sandbox)["README.md was modified"].ok is False


def test_modified_readme_with_usage_passes(tmp_path):
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    (sandbox / "README.md").write_text(
        "# hello-world\n\nPrints a greeting.\n\n"
        "## Usage\n\nRun `main.py` and it prints `Hello world`.\n",
        encoding="utf-8",
    )
    report = _report(2, sandbox)
    assert report.passed, [f.label for f in report.failed]


def test_missing_usage_section_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    (sandbox / "README.md").write_text("# hello-world\n\nSomething else.\n", encoding="utf-8")
    assert _check(2, sandbox)["README has a Usage section"].ok is False


def test_invented_file_reference_is_detected(tmp_path):
    """The checker reuses the shipped existence gate rather than
    reimplementing it, so the two can never disagree about what counts."""
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    (sandbox / "README.md").write_text(
        "# hello-world\n\n## Usage\n\nRun `main.py`.\n\n"
        "## Development\n\nRun `python -m unittest test_main.py`.\n",
        encoding="utf-8",
    )
    finding = _check(2, sandbox)["no references to missing files"]
    assert finding.ok is False
    assert "test_main.py" in finding.detail


# ─────────────────────────────────────────────────────────────────────────────
# Task 3 — creative
# ─────────────────────────────────────────────────────────────────────────────

def _changelog(sandbox: Path, text: str) -> None:
    (sandbox / "CHANGELOG.md").write_text(text, encoding="utf-8")


_GOOD_CHANGELOG = """# Changelog

A narrative changelog: each entry is a few paragraphs of prose telling the
story of one change, newest first. New entries are appended by the
`hello-creative` skill.

### A newer entry

We taught it something new. It still prints Hello world.

### The first greeting

We taught this project to say something. Before there was anything else,
there was a single function called `main`, and all it did was print the
words `Hello world` to the terminal and then stop.

That is genuinely all of it. There are no arguments to pass, no
configuration to read, no failure path to worry about. Someone runs
`python main.py` and the machine answers back. We kept it that way on
purpose: a project this small is the only kind where you can hold the whole
thing in your head at once, and that turns out to be useful when what you
actually want to study is the machinery around the code rather than the
code itself.

A reader can now run the program and see it greet them.
"""
# ^ the seed entry's prose is copied VERBATIM from
# examples/hello-world/CHANGELOG.md@HEAD, not paraphrased — the new
# "seed entry text unchanged" check (added alongside the heading-only
# "seed entry preserved") diffs against that real baseline, so a fixture
# claiming to be "good" has to actually match it.


def test_good_changelog_passes(tmp_path):
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    report = _report(3, sandbox)
    assert report.passed, [f.label for f in report.failed]


def test_dropped_seed_entry_is_detected(tmp_path):
    """Observed: the coder rewrote the file wholesale and lost the seed entry,
    leaving `continuity` with no predecessor to compare against."""
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, "# Changelog\n\n### Only entry\n\nIt prints Hello world.\n")
    assert _check(3, sandbox)["seed entry preserved"].ok is False


def test_seed_entry_rewritten_in_place_is_detected(tmp_path):
    """Observed live (test-other-router run, task 3 and task 4 both): the
    architect's 'support' cluster call — the one that shows it
    CHANGELOG.md's real content — came back empty (upstream 429s), so its
    only candidate was built blind. The coder faithfully "recreated" the
    seed entry with the same heading but different prose, instead of
    leaving it alone and prepending something new. `len(entries) == 1`
    already catches this via "a new entry was added", but until now
    "seed entry preserved" read as a bare PASS right beside it, because it
    only checked for the heading text.
    """
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(
        sandbox,
        "# Changelog\n\n### The first greeting\n\n"
        "We taught this project to say something. It now prints a "
        "friendly hello to whoever runs it.\n",
    )
    finding = _check(3, sandbox)["seed entry text unchanged"]
    assert finding.ok is False


def test_untouched_seed_entry_passes_the_text_check(tmp_path):
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    finding = _check(3, sandbox)["seed entry text unchanged"]
    assert finding.ok is True
    assert finding.warn is False


def test_no_new_entry_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    assert _check(3, sandbox)["a new entry was added"].ok is False


def test_code_fence_in_an_entry_is_detected(tmp_path):
    """The skill says prose only — no code blocks inside an entry."""
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG + "\n```python\nprint('x')\n```\n")
    assert _check(3, sandbox)["prose only — no code fences"].ok is False


def test_creative_run_targeting_python_is_detected(tmp_path):
    """The failure mode that once burned 10 rounds without converging."""
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["main.py"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    assert _check(3, sandbox)["no .py file was targeted"].ok is False


def test_lost_canon_fact_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, "# Changelog\n\n### New\n\nprose\n\n### The first greeting\n\nprose\n")
    assert _check(3, sandbox)["canon fact intact"].ok is False


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — creative, canon/continuity on a second provider (GATE3-PROFILE-5)
# ─────────────────────────────────────────────────────────────────────────────

_SHARED_LINE = (
    "validator_agent.validator_llm_profile: provider = https://a.example/v1 "
    "(model-a) \u2014 shared provider (no validator_llm_profile configured)\n"
)


def _console_log(sandbox: Path, text: str) -> None:
    (sandbox.parent / "console-log.txt").write_text(text, encoding="utf-8")


def _split_log() -> str:
    return _SHARED_LINE + (
        "validator_agent.canon_llm_profile: provider = https://b.example/v1 "
        "(model-b) \u2014 via canon_llm_profile = [task4_provider_b]\n"
        "validator_agent.continuity_llm_profile: provider = https://b.example/v1 "
        "(model-b) \u2014 via continuity_llm_profile = [task4_provider_b]\n"
    )


def _same_provider_log() -> str:
    return _SHARED_LINE + (
        "validator_agent.canon_llm_profile: provider = https://a.example/v1 "
        "(model-a) \u2014 shared provider (no canon_llm_profile configured)\n"
        "validator_agent.continuity_llm_profile: provider = https://a.example/v1 "
        "(model-a) \u2014 shared provider (no continuity_llm_profile configured)\n"
    )


def test_task4_reuses_task3s_changelog_checks(tmp_path):
    """Same skill body, same base — task 4 must not relax task 3's checks."""
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, "# Changelog\n\n### Only entry\n\nno seed here.\n")
    _console_log(sandbox, _split_log())
    report = _report(4, sandbox)
    assert report.findings, "check_task4 produced no findings at all"
    assert not report.passed
    assert any("seed entry preserved" in f.label for f in report.failed)


def test_task4_reports_task_and_skill_as_its_own(tmp_path):
    """check_task4 delegates to check_task3 internally; the RETURNED report
    must carry task 4's own identity, not a leaked task 3."""
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _console_log(sandbox, _split_log())
    report = _report(4, sandbox)
    assert report.task == 4
    assert report.skill == "hello-creative-split"


def test_split_provider_evidence_passes_on_a_real_split(tmp_path):
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _console_log(sandbox, _split_log())
    report = _report(4, sandbox)
    finding = _check(4, sandbox)["canon/continuity used a second provider"]
    assert finding.ok is True
    assert finding.warn is False
    assert "b.example" in finding.detail
    assert report.passed


def test_split_provider_evidence_warns_not_fails_on_a_single_provider(tmp_path):
    """The environment-can't-verify-this case: one provider only. Must be a
    WARN (skip with an explicit reason), never a silent PASS and never a
    hard FAIL of a property the checker cannot actually observe."""
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _console_log(sandbox, _same_provider_log())
    finding = _check(4, sandbox)["canon/continuity used a second provider"]
    assert finding.ok is False
    assert finding.warn is True
    report = _report(4, sandbox)
    assert report.passed, "a WARN finding must not fail the report"


def test_split_provider_evidence_warns_when_console_log_is_absent(tmp_path):
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    # No console-log.txt written at all.
    finding = _check(4, sandbox)["canon/continuity used a second provider"]
    assert finding.ok is False
    assert finding.warn is True
    assert _report(4, sandbox).passed


def test_split_provider_evidence_warns_when_gate_lines_are_missing(tmp_path):
    """A console log that exists but predates GATE3-PROFILE-2, or came from
    a run of plain hello-creative rather than hello-creative-split."""
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _console_log(sandbox, "InnerLoop: Gate-3 order for creative mode \u2014 canon, continuity\n")
    finding = _check(4, sandbox)["canon/continuity used a second provider"]
    assert finding.ok is False
    assert finding.warn is True
    assert _report(4, sandbox).passed


def test_split_provider_evidence_skipped_when_flow_never_ran(tmp_path):
    """No .agent/ at all — check_task3's own 'flow was run' failure already
    explains everything; the provider finding must not also fire (there is
    nothing to read a console log's providers FROM if the flow never ran)."""
    root = tmp_path / "repo"
    sandbox = root / "examples" / "task4" / "hello-world"
    sandbox.mkdir(parents=True)
    report = _report(4, sandbox)
    assert not report.passed
    assert "canon/continuity used a second provider" not in {
        f.label for f in report.findings
    }


def test_task4_makes_no_network_call(monkeypatch, tmp_path):
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("check_runbook must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    sandbox = _make_sandbox(tmp_path, 4, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _console_log(sandbox, _split_log())
    _report(4, sandbox)


# ─────────────────────────────────────────────────────────────────────────────
# Task 1 — code
# ─────────────────────────────────────────────────────────────────────────────

_HARDENED = '''"""Print a greeting."""


def main() -> int:
    """Print "Hello world"."""
    print("Hello world")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
'''


def test_hardened_main_passes(tmp_path):
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py", "test_main.py"])
    (sandbox / "main.py").write_text(_HARDENED, encoding="utf-8")
    (sandbox / "test_main.py").write_text(
        "import main\n\n\ndef test_output(capsys):\n"
        "    main.main()\n"
        "    assert capsys.readouterr().out.strip() == 'Hello world'\n",
        encoding="utf-8",
    )
    report = _report(1, sandbox)
    assert report.passed, [f"{f.label}: {f.detail}" for f in report.failed]


def test_missing_test_file_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / "main.py").write_text(_HARDENED, encoding="utf-8")
    assert _check(1, sandbox)["test file created"].ok is False


def test_import_above_docstring_is_detected(tmp_path):
    """Observed live: `import sys` inserted above the module docstring
    silently demotes it to a plain string."""
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / "main.py").write_text("import sys\n" + _HARDENED, encoding="utf-8")
    assert _check(1, sandbox)["module docstring"].ok is False


def test_broken_output_is_detected(tmp_path):
    """The one thing every flow must never change."""
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / "main.py").write_text("print('Goodbye')\n", encoding="utf-8")
    assert _check(1, sandbox)["main.py still prints Hello world"].ok is False


def test_failing_test_suite_is_detected(tmp_path):
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py", "test_main.py"])
    (sandbox / "main.py").write_text(_HARDENED, encoding="utf-8")
    (sandbox / "test_main.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    assert _check(1, sandbox)["test suite passes"].ok is False


def test_syntax_error_is_reported_not_raised(tmp_path):
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / "main.py").write_text("def main(:\n", encoding="utf-8")
    report = _report(1, sandbox)
    assert not report.passed
    assert any(f.label == "main.py parses" for f in report.failed)


# ─────────────────────────────────────────────────────────────────────────────
# CLI surface
# ─────────────────────────────────────────────────────────────────────────────

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )


def test_checker_requires_a_target():
    assert _cli().returncode != 0


def test_checker_json_output_is_parseable():
    result = _cli("--all", "--json")
    payload = json.loads(result.stdout)
    assert [entry["task"] for entry in payload] == [1, 2, 3, 4]
    assert all("findings" in entry for entry in payload)


def test_checker_exits_non_zero_when_a_task_fails():
    """On a repository with no runs, everything must fail."""
    assert _cli("--all", "--no-colour").returncode == 1


def test_checker_names_the_skill_per_task():
    payload = json.loads(_cli("--all", "--json").stdout)
    assert [e["skill"] for e in payload] == [
        "hello-code", "hello-docs", "hello-creative", "hello-creative-split",
    ]


def test_checker_makes_no_network_call(monkeypatch, tmp_path):
    """Determinism is the point — a checker that can hallucinate is not one."""
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("check_runbook must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    sandbox = _make_sandbox(tmp_path, 3, plan_targets=["CHANGELOG.md"])
    _changelog(sandbox, _GOOD_CHANGELOG)
    _report(3, sandbox)


# ─────────────────────────────────────────────────────────────────────────────
# run_flows.sh
# ─────────────────────────────────────────────────────────────────────────────

def test_runner_exists_and_is_executable():
    assert RUNNER.is_file()
    assert os.access(RUNNER, os.X_OK), "scripts/run_flows.sh is not executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_runner_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(RUNNER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_runner_refuses_a_profile_below_the_skill_floor():
    """agents.ini is 8192; the skills need 16384. Catching it here costs a
    second, catching it inside a run costs the whole run."""
    result = subprocess.run(
        ["bash", str(RUNNER), "--config", "agents.ini", "--check-only"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 2
    assert "16384" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_runner_rejects_a_missing_config():
    result = subprocess.run(
        ["bash", str(RUNNER), "--config", "nope.ini", "--check-only"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 2


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_runner_check_only_does_not_claim_a_run_happened():
    """Reporting run=ok for a run that never started would be a lie."""
    result = subprocess.run(
        ["bash", str(RUNNER), "--check-only", "--config", "agents_32k.ini"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert "run=skipped" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_runner_rejects_unknown_arguments():
    result = subprocess.run(
        ["bash", str(RUNNER), "--wat"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2


def test_generated_sandboxes_are_gitignored():
    """They are run output; committing them would make every run a diff."""
    assert "examples/task*/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_runner_documents_the_isolation_rationale():
    text = RUNNER.read_text(encoding="utf-8")
    assert "OWN sandbox" in text or "own sandbox" in text


# ─────────────────────────────────────────────────────────────────────────────
# Reporting defects found by the first full live run
# ─────────────────────────────────────────────────────────────────────────────
# All three were in the CHECKER, not the flows, and all three made a failing
# run read as a healthier one than it was.

def test_hint_is_hidden_on_passing_findings():
    """The worst of the three: a failure explanation printed beside PASS.

    Real output from that run:
        [PASS] seed entry preserved — the coder rewrote the file wholesale
        [PASS] main.py untouched   — the creative flow modified source code
        [PASS] module docstring    — absent
    Each reads as a flat self-contradiction.
    """
    import check_runbook

    report = check_runbook.Report(3, "hello-creative", Path("/tmp"))
    report.add(True, "some check", hint="this explains a failure")
    rendered = check_runbook.render(report, colour=False)
    assert "some check" in rendered
    assert "this explains a failure" not in rendered


def test_hint_is_shown_on_failing_findings():
    import check_runbook

    report = check_runbook.Report(3, "hello-creative", Path("/tmp"))
    report.add(False, "some check", hint="here is why it matters")
    assert "here is why it matters" in check_runbook.render(report, colour=False)


def test_detail_is_shown_on_both():
    import check_runbook

    report = check_runbook.Report(1, "hello-code", Path("/tmp"))
    report.add(True, "passing", detail="observed value")
    report.add(False, "failing", detail="observed value")
    rendered = check_runbook.render(report, colour=False)
    assert rendered.count("observed value") == 2


def test_empty_detail_leaves_no_dangling_dash():
    """`[PASS] no .py file was targeted  — targeted  — ...` was real output."""
    import check_runbook

    report = check_runbook.Report(2, "hello-docs", Path("/tmp"))
    report.add(True, "clean label", detail="")
    line = [l for l in check_runbook.render(report, colour=False).splitlines()
            if "clean label" in l][0]
    assert "—" not in line


def test_zero_completed_tasks_is_a_failure(tmp_path):
    """A 436-second run that completed nothing passed this check.

    `done_count: 0` with no stop_reason meant "finished cleanly", so the
    single most important signal — the flow did no work — was reported green.
    """
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    (sandbox / ".agent" / "progress.json").write_text(
        json.dumps({"status": "idle", "done_count": 0}), encoding="utf-8"
    )
    assert _check(2, sandbox)["run completed"].ok is False


def test_completed_tasks_still_pass(tmp_path):
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    assert _check(2, sandbox)["run completed"].ok is True


def test_task_cap_with_work_done_is_a_warning_not_a_failure(tmp_path):
    """The cap doing its job should not read the same as a broken run."""
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / ".agent" / "progress.json").write_text(
        json.dumps({"status": "capped", "done_count": 3, "stop_reason": "task_cap"}),
        encoding="utf-8",
    )
    finding = _check(1, sandbox)["run completed"]
    assert finding.warn is True


def test_task_cap_with_no_work_done_is_a_failure(tmp_path):
    sandbox = _make_sandbox(tmp_path, 1, plan_targets=["main.py"])
    (sandbox / ".agent" / "progress.json").write_text(
        json.dumps({"status": "capped", "done_count": 0, "stop_reason": "task_cap"}),
        encoding="utf-8",
    )
    assert _check(1, sandbox)["run completed"].warn is False


def test_duplicate_plan_targets_are_reported_once(tmp_path):
    """`plan has target files — README.md, README.md` hid that two tasks
    were competing for one file."""
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md", "README.md"])
    detail = _check(2, sandbox)["plan has target files"].detail
    assert detail.count("README.md") == 1
    assert "2 task(s), 1 distinct file(s)" in detail


def test_console_log_is_surfaced_when_present(tmp_path):
    """Every 'the flow did nothing' diagnosis needs the log, and no on-disk
    check can explain WHY nothing happened."""
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    (sandbox.parent / "console-log.txt").write_text("log\n", encoding="utf-8")
    finding = _check(2, sandbox)["console log"]
    assert finding.warn is True
    assert "console-log.txt" in finding.detail


def test_console_log_absence_is_not_a_failure(tmp_path):
    sandbox = _make_sandbox(tmp_path, 2, plan_targets=["README.md"])
    assert "console log" not in _check(2, sandbox)


def test_json_output_carries_the_hint():
    payload = json.loads(_cli("--all", "--json").stdout)
    assert all("hint" in f for e in payload for f in e["findings"])
