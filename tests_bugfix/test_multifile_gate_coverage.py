"""tests/test_multifile_gate_coverage.py

Regression tests: all four post-Gate-2 creative-mode quality gates in
InnerLoop — canon (AUTO-CR-7), fact (AUTO-CR-20), continuity (AUTO-CR-23-3),
and prosody (AUTO-CR-21) — must check EVERY entry in a task's target_files,
not only target_files[0].

Multi-file creative tasks are a supported, tested shape at the architect
level (see tests/test_cr17_creative_acceptance.py, target_files=
["chapter_1.txt", "chapter_2.txt"]) — the natural example being a single
task that fixes a name/age/detail inconsistency across two chapters at once
(see tests/test_cr16_edit_mode_and_cleanup.py). Before this fix, all four
gates silently ignored every file past the first, so a genuine contradiction
introduced in a second or third file could be committed without ever being
checked. This mirrors the AUTO-CR-16 fix that made the coder load every
target file's content instead of only the first.

Each test uses a stub validator that flags a conflict ONLY on the second of
two target files, and asserts:
  1. the stub was actually invoked for the second file (proves it's checked
     at all, not just that the overall verdict happens to come out right),
  2. the attempt is rejected-with-feedback because of that second file.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tools.auto.inner_loop import InnerLoop
from tools.auto.canon_validator import CanonResult
from tools.auto.fact_validator import FactVerdict
from tools.auto.continuity_validator import ContinuityVerdict
from tools.auto.prosody import ProsodyVerdict


# ── Shared stubs ────────────────────────────────────────────────────────────

class _OkCoderMulti:
    """Writes every target file to disk, exactly as the real Coder does for
    a multi-file creative task (AUTO-CR-16/18)."""

    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        targets = task.get("target_files") or []
        for t in targets:
            (Path(base_dir) / t).write_text(f"Text for {t}.", encoding="utf-8")
        return SimpleNamespace(
            succeeded=True, files_written=list(targets), missing_context=[],
            context_satisfied=True, error="",
        )


class _OkExecutor:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="", traceback="")


class _OkValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return True, ""


TASK = {"id": "t1", "target_files": ["chapter_02.md", "chapter_03.md"]}
SECOND_FILE = "chapter_03.md"


# ── Canon gate (AUTO-CR-7) ──────────────────────────────────────────────────

class _CanonConflictOnSecondFile:
    max_canon_revisions = 1

    def __init__(self):
        self.checked_files: list[str] = []

    def should_check(self, chapter_file):
        return True

    def check(self, text, chapter_file, base_dir=None):
        self.checked_files.append(chapter_file)
        r = CanonResult(checked=True)
        if chapter_file == SECOND_FILE:
            r.conflicts.append("contradicts established canon")
        return r


def test_canon_gate_checks_second_target_file(tmp_path):
    canon = _CanonConflictOnSecondFile()
    loop = InnerLoop(
        _OkCoderMulti(), _OkExecutor(), _OkValidator(),
        max_attempts=5, canon_validator=canon, task_mode="creative",
    )
    result = loop.run_task(dict(TASK), tmp_path)

    assert SECOND_FILE in canon.checked_files
    # cap=1: attempt 1 rejects on the second file's conflict (spending the
    # cap), attempt 2 hits the cap and is accepted-with-warning. Two attempts
    # only happen if the conflict was actually detected and acted on.
    assert result.attempts_used == 2


# ── Fact gate (AUTO-CR-20) ──────────────────────────────────────────────────

class _FactConflictOnSecondFile:
    max_fact_revisions = 1

    def __init__(self):
        self.checked_files: list[str] = []

    def check(self, task, text):
        # text is per-file chapter content: "Text for <file>."
        chapter_file = text.replace("Text for ", "").rstrip(".")
        self.checked_files.append(chapter_file)
        if chapter_file == SECOND_FILE:
            return FactVerdict(approved=False, reason="contradicts task fact", unparseable=False)
        return FactVerdict(approved=True, reason="", unparseable=False)


def test_fact_gate_checks_second_target_file(tmp_path):
    fact = _FactConflictOnSecondFile()
    loop = InnerLoop(
        _OkCoderMulti(), _OkExecutor(), _OkValidator(),
        max_attempts=5, fact_validator=fact, task_mode="creative",
    )
    result = loop.run_task(dict(TASK), tmp_path)

    assert SECOND_FILE in fact.checked_files
    assert result.attempts_used == 2


# ── Continuity gate (AUTO-CR-23-3) ──────────────────────────────────────────

class _ContinuityConflictOnSecondFile:
    max_continuity_revisions = 1

    def __init__(self):
        self.checked_texts: list[str] = []

    def check(self, known_facts, text):
        self.checked_texts.append(text)
        if text == f"Text for {SECOND_FILE}.":
            return ContinuityVerdict(approved=False, reason="contradicts bible", unparseable=False)
        return ContinuityVerdict(approved=True, reason="", unparseable=False)


def test_continuity_gate_checks_second_target_file(tmp_path):
    continuity = _ContinuityConflictOnSecondFile()
    loop = InnerLoop(
        _OkCoderMulti(), _OkExecutor(), _OkValidator(),
        max_attempts=5, continuity_validator=continuity, task_mode="creative",
    )
    result = loop.run_task(dict(TASK), tmp_path)

    assert f"Text for {SECOND_FILE}." in continuity.checked_texts
    assert result.attempts_used == 2


# ── Prosody gate (AUTO-CR-21) ───────────────────────────────────────────────

class _ProsodyConflictOnSecondFile:
    max_prosody_revisions = 1

    def __init__(self):
        self.checked_texts: list[str] = []

    def check(self, task, text):
        self.checked_texts.append(text)
        if text == f"Text for {SECOND_FILE}.":
            return ProsodyVerdict(approved=False, reason="broken meter")
        return ProsodyVerdict(approved=True, reason="")


def test_prosody_gate_checks_second_target_file(tmp_path):
    prosody = _ProsodyConflictOnSecondFile()
    loop = InnerLoop(
        _OkCoderMulti(), _OkExecutor(), _OkValidator(),
        max_attempts=5, prosody_validator=prosody, task_mode="creative",
    )
    result = loop.run_task(dict(TASK), tmp_path)

    assert f"Text for {SECOND_FILE}." in prosody.checked_texts
    assert result.attempts_used == 2
