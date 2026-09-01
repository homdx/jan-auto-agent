"""tests/test_bugfix_review.py — Regression tests for the auto-mode code review.

Each class below targets one bug found during a review of the "auto mode"
workflow (tools/auto/*) and pins down the FIXED behaviour so it can't
silently regress. See the accompanying summary for the full write-up of each
bug; the short version:

  1. TestRewritePersistsAcrossResume  — a task rewrite (LOOP-2) used to live
     only in a local variable; a resumed session reverted to the stale,
     already-failing original instruction even though impl_version correctly
     showed a rewrite had happened.
  2. TestMaxRewritesCapCrossResume    — max_rewrites was only ever enforced
     within a single process lifetime; restarting reset the count.
  3. TestBlockedResetRespectsRoundExhaustion — resetting BLOCKED -> TODO at
     the start of a run was a no-op for round-exhausted tasks (only genuine
     dependency-blocks benefit from it), silently defeating the restart an
     operator would use to give a stuck task another chance.
  4. TestArchitectCheckpointAtomicWrite — the architect's crash-recovery
     checkpoint was written with a plain, non-atomic write_text(), so a kill
     mid-write could corrupt it and silently discard every cached review.
  5. TestClusterHashContentAware — PlanEmitter's re-review skip check only
     fingerprinted a cluster's file *paths*, never content, so editing a
     file already in a cluster was invisible to it forever.
  6. TestTicketStorePathSanitization — ticket ids were not sanitized before
     being interpolated into a filesystem path (unlike task ids).
  7. TestWordBoundaryGrandfathering — the selfhost-pilot grandfathering added
     to _check_content_safety covered the general substring patterns and the
     subprocess+danger-token combo, but not the separate word-boundary check
     (sudo/shutdown/reboot). Since coder.py's own pattern tables contain the
     word "sudo" as data, this silently blocked EVERY edit to coder.py,
     including a no-op rewrite of its own unchanged content — defeating the
     self-editing feature the commit was written to enable.
  8. TestNewFileCreation — Gate1's existence check and the architect's
     cited_location schema had no way to represent "this file does not exist
     yet, and this task's purpose is to create it." Every candidate task
     targeting a genuinely new file was unconditionally rejected as a
     hallucinated path (discovered via a live end-to-end simulation using
     stub_codeapp_server.py's T1 "create app.py from nothing" scenario,
     which could never pass Gate 1). Adds an explicit, opt-in
     cited_location.new_file flag (default False — fully backward
     compatible) that lets a candidate skip the existence and problem-
     presence checks for a file that legitimately does not exist yet,
     mirroring the reasoning AUTO-CR-8 already applies to creative mode.
  9. TestStoryBibleAtomicWrite — a refactor removed the atomic_write_text
     import from story_bible.py entirely and downgraded all three of its
     persisted-state writes (the bible file itself, twice for the pending-
     corrections state) to plain, non-atomic write_text(), silently
     re-introducing the exact "kill mid-write corrupts everything" failure
     mode Bug 4 already fixed once for a different file — except this file
     holds the running story's entire tracked fact set, so a mid-write kill
     during a long creative session would lose established character facts
     the pipeline has no other record of. The explanatory comment for why
     the write must be atomic was deleted along with the atomic write
     itself, with no replacement rationale given.
  10. TestCanonValidatorPreservesNumbers — same "Tests not passed" commit
     that regressed Bug 9 also replaced CanonValidator._extract_claims'
     correctly-anchored marker-stripping regex with a bare
     str.lstrip("-*0123456789. \\t") — and deleted the comment explaining
     exactly why that's wrong. lstrip eats ANY leading characters in the
     set regardless of structure, so a claim starting with its own number
     ("1985 год рождения героя") had that number silently deleted before
     ever being compared against canon, not just a genuine bullet/numbered
     marker. Restored the regex. This same commit deleted FOUR test
     classes total (TestSearchAgentEmptyLLMResponseFailsOpen,
     TestCoderTargetFilesDotfileCollision, TestCanonValidatorPreservesNumbers,
     TestStoryBibleAtomicWrites) alongside reverting the two fixes above —
     the other two (SearchAgent's empty-list fail-open, Coder's dotfile-
     collision guard) were checked and their underlying code is still
     correct, so only their test coverage needed restoring, not the code.
  11. TestSubprocessDangerComboRequiresCooccurrence — a same-day upstream
     fix (b0aabde) correctly identified that the subprocess+danger-token
     grandfathering checked `_preexisting(danger)` alone, letting an
     unrelated mention of the danger token anywhere in the file (a plain-
     English warning comment, an unrelated test string) permanently
     exempt that file from the guard. Its fix — requiring
     `_preexisting(danger) and _preexisting("subprocess")` — is a real
     improvement but still incomplete: both checks only verify each token
     independently appears SOMEWHERE in the old file, not that they ever
     co-occurred as an actual combined usage. A file with "subprocess"
     mentioned in one unrelated line (e.g. a benign import comment) and a
     danger token mentioned in a completely different, unrelated line
     (e.g. a warning docstring) still satisfied both independent checks,
     so a brand-new, genuinely dangerous subprocess+rm-rf combination
     introduced by the same edit was still silently grandfathered in —
     reproduced and confirmed empirically. Fixed by requiring same-line
     co-occurrence (_preexisting_combo), a cheap, reasonable proxy for
     "this was already real combined usage" without needing a full parser.
"""

from __future__ import annotations

import configparser
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.coder import Coder
from tools.auto.gate1_filter import Gate1Filter
from tools.auto.state import (
    StateStore,
    make_task,
    STATUS_BLOCKED,
    STATUS_TODO,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fakes (mirrors the style already used in tests/test_auto_c4.py)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeInnerResult:
    task_id: str = "T1"
    passed: bool = False
    attempts_used: int = 1
    records: list = field(default_factory=list)
    last_feedback: str = "still broken"


class _ScriptedInnerLoop:
    """Fails a fixed number of calls, then passes. Records task["instruction"]
    seen on each call so tests can assert exactly what the coder was told to
    do."""

    def __init__(self, fail_count: int = 999):
        self.fail_count = fail_count
        self.calls = 0
        self.seen_instructions: list[str] = []

    def run_task(self, task, base_dir, prior_feedback=None,
                 prior_implementations=None, deadline=None):
        self.calls += 1
        self.seen_instructions.append(task.get("instruction"))
        if self.calls <= self.fail_count:
            return _FakeInnerResult(passed=False, attempts_used=1)
        return _FakeInnerResult(passed=True, attempts_used=1)


class _CountingRewriter:
    """Always produces a genuinely different task object (new instruction)."""

    def __init__(self):
        self.calls = 0

    def rewrite(self, task, failure_history):
        self.calls += 1
        new_task = dict(task)
        new_task["instruction"] = f"REWRITTEN v{self.calls + 1}"
        new_task["acceptance_check"] = task.get("acceptance_check", "")
        return new_task


def _make_state(tmp_path: Path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("goal", tmp_path)
    return st


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 — rewrite persists across a resumed session
# ─────────────────────────────────────────────────────────────────────────────

class TestRewritePersistsAcrossResume:
    def test_apply_rewrite_persists_instruction_and_original(self, tmp_path):
        from tools.auto.state import StateStore as SS
        st = _make_state(tmp_path)
        st.upsert_task(make_task(
            id="T1", title="t", instruction="ORIGINAL",
            target_files=["f.py"], acceptance_check="pytest -q",
        ))
        new_ver = st.apply_rewrite(
            "T1", instruction="REWRITTEN", acceptance_check="pytest -k x"
        )
        assert new_ver == 2
        task = st.get_task("T1")
        assert task["instruction"] == "REWRITTEN"
        assert task["acceptance_check"] == "pytest -k x"
        assert task["original_instruction"] == "ORIGINAL"
        assert task["impl_version"] == 2

        # A second rewrite must not clobber the preserved true original.
        st.apply_rewrite("T1", instruction="REWRITTEN AGAIN")
        task = st.get_task("T1")
        assert task["original_instruction"] == "ORIGINAL"
        assert task["impl_version"] == 3

    def test_resumed_outer_loop_uses_rewritten_instruction(self, tmp_path):
        from tools.auto.outer_loop import OuterLoop

        st = _make_state(tmp_path)
        st.upsert_task(make_task(
            id="T1", title="t", instruction="ORIGINAL_V1",
            target_files=["f.py"], acceptance_check="pytest -q",
        ))
        task0 = st.get_task("T1")

        # "Session 1": rounds 1-3 fail; round 3 triggers a rewrite
        # (rewrite_every_n_rounds=2 -> fires at rnd>=3 where (rnd-1)%2==0).
        # max_rounds=3 ends the session right after that rewrite takes
        # effect — the same on-disk state a process kill would leave.
        inner_1 = _ScriptedInnerLoop(fail_count=999)
        rewriter_1 = _CountingRewriter()
        loop_1 = OuterLoop(inner_1, st, max_rounds=3, rewrite_every_n_rounds=2,
                           max_rewrites=5, task_rewriter=rewriter_1)
        result_1 = loop_1.run_task(task0, tmp_path)
        assert rewriter_1.calls == 1
        assert result_1.exhausted is True

        # "Session 2": brand-new StateStore + OuterLoop (new process),
        # exactly like controller._run_task_loop rebuilds both every call.
        st2 = _make_state(tmp_path)
        st2.set_task_status("T1", STATUS_TODO)  # controller's BLOCKED->TODO reset
        resumed_task = st2.get_task("T1")
        assert resumed_task["impl_version"] == 2

        inner_2 = _ScriptedInnerLoop(fail_count=0)  # passes immediately
        loop_2 = OuterLoop(inner_2, st2, max_rounds=10, rewrite_every_n_rounds=2,
                           max_rewrites=5, task_rewriter=_CountingRewriter())
        result_2 = loop_2.run_task(resumed_task, tmp_path)

        assert result_2.passed is True
        assert inner_2.seen_instructions == ["REWRITTEN v2"]


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 — max_rewrites is a true per-task, cross-resume cap
# ─────────────────────────────────────────────────────────────────────────────

class TestMaxRewritesCapCrossResume:
    def test_cap_holds_across_a_resume(self, tmp_path):
        from tools.auto.outer_loop import OuterLoop

        st = _make_state(tmp_path)
        st.upsert_task(make_task(
            id="T1", title="t", instruction="original",
            target_files=["f.py"], acceptance_check="pytest -q",
        ))

        MAX_REWRITES = 1
        inner_1 = _ScriptedInnerLoop(fail_count=999)
        rewriter_1 = _CountingRewriter()
        loop_1 = OuterLoop(inner_1, st, max_rounds=3, rewrite_every_n_rounds=1,
                           max_rewrites=MAX_REWRITES, task_rewriter=rewriter_1)
        loop_1.run_task(st.get_task("T1"), tmp_path)
        assert rewriter_1.calls == 1  # used up the entire budget

        # Resume with a fresh OuterLoop/TaskRewriter, same config. Give it
        # plenty of extra rounds so a second rewrite opportunity would recur
        # if the cap didn't hold.
        st.set_task_status("T1", STATUS_TODO)
        inner_2 = _ScriptedInnerLoop(fail_count=999)
        rewriter_2 = _CountingRewriter()
        loop_2 = OuterLoop(inner_2, st, max_rounds=6, rewrite_every_n_rounds=1,
                           max_rewrites=MAX_REWRITES, task_rewriter=rewriter_2)
        loop_2.run_task(st.get_task("T1"), tmp_path)

        total_rewrites = rewriter_1.calls + rewriter_2.calls
        assert total_rewrites == MAX_REWRITES, (
            f"max_rewrites={MAX_REWRITES} must hold across a resume; "
            f"got {total_rewrites} total rewrites"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3 — BLOCKED-reset only resets tasks a reset can actually help
# ─────────────────────────────────────────────────────────────────────────────

class TestBlockedResetRespectsRoundExhaustion:
    def _controller(self, tmp_path, max_rounds_per_task=3):
        from tools.auto.controller import AutoController
        agents_ini = tmp_path / "agents.ini"
        agents_ini.write_text(
            f"[auto]\nmax_rounds_per_task = {max_rounds_per_task}\n",
            encoding="utf-8",
        )
        controller = AutoController(
            goal="improve code", base_dir=tmp_path, config_path=str(agents_ini)
        )
        controller.state.initialise("improve code", tmp_path)
        return controller

    def test_round_exhausted_task_stays_blocked(self, tmp_path):
        controller = self._controller(tmp_path, max_rounds_per_task=3)
        controller.state.upsert_task(make_task(
            id="T1", title="t", instruction="x", target_files=["f.py"],
            acceptance_check="pytest", status=STATUS_BLOCKED,
        ))
        task_dir = controller.state.task_dir("T1")
        for n in (1, 2, 3):  # matches max_rounds_per_task — fully exhausted
            (task_dir / f"feedback_round_{n}.md").write_text("failed", encoding="utf-8")

        controller._reset_resettable_blocked_tasks(controller._cfg())

        assert controller.state.get_task("T1")["status"] == STATUS_BLOCKED

    def test_dependency_blocked_task_is_reset_to_todo(self, tmp_path):
        controller = self._controller(tmp_path, max_rounds_per_task=3)
        controller.state.upsert_task(make_task(
            id="T2", title="t", instruction="x", target_files=["f.py"],
            acceptance_check="pytest", status=STATUS_BLOCKED,
        ))
        # No feedback_round_*.md files at all: this task never ran a round —
        # it's BLOCKED purely because of an unmet dependency.

        controller._reset_resettable_blocked_tasks(controller._cfg())

        assert controller.state.get_task("T2")["status"] == STATUS_TODO

    def test_partially_used_task_is_still_reset(self, tmp_path):
        """A task blocked on a dependency after using SOME (not all) rounds
        (e.g. it failed twice, then a later task it depends on got queued)
        must still be reset — only full exhaustion should block the reset."""
        controller = self._controller(tmp_path, max_rounds_per_task=3)
        controller.state.upsert_task(make_task(
            id="T3", title="t", instruction="x", target_files=["f.py"],
            acceptance_check="pytest", status=STATUS_BLOCKED,
        ))
        task_dir = controller.state.task_dir("T3")
        (task_dir / "feedback_round_1.md").write_text("failed", encoding="utf-8")

        controller._reset_resettable_blocked_tasks(controller._cfg())

        assert controller.state.get_task("T3")["status"] == STATUS_TODO


# ─────────────────────────────────────────────────────────────────────────────
# Bug 4 — architect checkpoint is written atomically
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitectCheckpointAtomicWrite:
    def test_atomic_write_text_never_corrupts_old_file_on_failure(
        self, tmp_path, monkeypatch
    ):
        from tools.auto.utils import atomic_write_text

        path = tmp_path / "architect_checkpoint.json"
        path.write_text('{"good": "old-content"}', encoding="utf-8")

        def boom(*a, **k):
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(os, "fsync", boom)
        with pytest.raises(OSError):
            atomic_write_text(path, '{"new": "content-that-should-never-land"}')

        # The OLD file must be completely untouched — never truncated,
        # never partially overwritten.
        assert path.read_text(encoding="utf-8") == '{"good": "old-content"}'
        # No leftover temp file either.
        assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []

    def test_review_clusters_checkpoint_save_goes_through_atomic_write(
        self, tmp_path, monkeypatch
    ):
        import configparser
        import json as _json
        from tools.auto.architect import ClusterReviewer
        from tools.auto.repo_ingest import RepoCluster
        import tools.auto.architect as architect_mod

        src = tmp_path / "a.py"
        src.write_text("def fn(): pass\n", encoding="utf-8")
        cluster = RepoCluster(name="agents", patterns=["*.py"], files=["a.py"])

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api":       {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1", "api_key": "test",
                "model": "test-model", "api_format": "openai",
            },
            "architect": {"temperature": "0.2", "max_tokens": "512"},
            "loop":      {"timeout_seconds": "10"},
        })
        reviewer = ClusterReviewer(
            config=cfg, base_url="http://localhost:1337/v1", api_key="test",
            model="test-model", api_format="openai", verify_ssl=False,
        )

        payload = _json.dumps([{
            "title": "Fix it", "instruction": "do it",
            "target_files": ["a.py"], "acceptance_check": "pytest",
            "cited_location": {"file": "a.py", "symbol": "fn",
                                "line_start": 1, "line_end": 1},
        }])

        calls = []
        original = architect_mod.atomic_write_text

        def spy(path, content):
            calls.append(Path(path))
            return original(path, content)

        monkeypatch.setattr(architect_mod, "atomic_write_text", spy)

        with monkeypatch.context() as m:
            m.setattr("tools.llm_stream.request_completion", lambda *a, **k: payload)
            ckpt = tmp_path / "arch_ckpt.json"
            reviewer.review_clusters([cluster], tmp_path, goal="improve",
                                     checkpoint_path=ckpt)

        assert calls, "checkpoint save must go through atomic_write_text, not a bare write_text"
        assert ckpt.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Bug 5 — PlanEmitter's staleness check is content-aware
# ─────────────────────────────────────────────────────────────────────────────

class TestClusterHashContentAware:
    def _emitter(self, tmp_path):
        from tools.auto.plan_emitter import PlanEmitter
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        state = MagicMock()
        state.agent_dir = agent_dir
        git = MagicMock()
        git.commit.return_value = "abc123"
        return PlanEmitter(base_dir=tmp_path, state=state, git=git)

    def test_editing_file_in_place_is_detected_as_stale(self, tmp_path):
        from tools.auto.repo_ingest import RepoCluster
        from tools.auto.backlog_prioritiser import build_backlog

        (tmp_path / "a.py").write_text("v1", encoding="utf-8")
        emitter = self._emitter(tmp_path)
        cluster = RepoCluster(name="agents", patterns=["*.py"], files=["a.py"])

        emitter.emit(build_backlog([]), clusters=[cluster])
        assert emitter.changed_clusters([cluster]) == []  # unchanged right after emit

        # Edit the SAME file (same path, same cluster) — no path-list change,
        # but a real content change (different size, so this can't flake on
        # coarse filesystem mtime granularity).
        (tmp_path / "a.py").write_text(
            "v2 - a completely different, much longer body than before",
            encoding="utf-8",
        )

        stale = emitter.changed_clusters([cluster])
        assert stale == [cluster], (
            "editing a file already in the cluster must be detected as a "
            "change, not just adding/removing/renaming files"
        )

    def test_cluster_hash_without_base_dir_is_unchanged(self):
        """Direct/legacy callers that don't pass base_dir keep the old,
        path-only, 64-char SHA-256 behaviour untouched."""
        from tools.auto.plan_emitter import _cluster_hash
        from tools.auto.repo_ingest import RepoCluster

        c1 = RepoCluster(name="agents", patterns=[], files=["a.py", "b.py"])
        c2 = RepoCluster(name="agents", patterns=[], files=["b.py", "a.py"])
        assert _cluster_hash(c1) == _cluster_hash(c2)
        assert len(_cluster_hash(c1)) == 64


# ─────────────────────────────────────────────────────────────────────────────
# Minor fix — ticket ids are sanitized before touching the filesystem
# ─────────────────────────────────────────────────────────────────────────────

class TestTicketStorePathSanitization:
    def test_traversal_id_cannot_escape_tickets_dir(self, tmp_path):
        from tools.auto.ticket_store import TicketStore, make_ticket

        tickets_dir = tmp_path / ".agent" / "tickets"
        ts = TicketStore(tickets_dir)
        ticket = make_ticket(
            id="../../evil", type="bug", linked_task="", title="t", body="b"
        )
        ts.create(ticket)

        # Must NOT have escaped two levels above tickets_dir.
        assert not (tmp_path.parent.parent / "evil.json").exists()
        # Must land inside tickets_dir instead, under the sanitized name.
        written = list(tickets_dir.glob("*evil*"))
        assert len(written) == 1
        assert written[0].parent == tickets_dir


# ── selfhost pilot: tests-mandate gate ────────────────────────────────────────

class TestTestsMandateGate:
    class _Coder:
        def __init__(self, base, drafts):
            self.base, self.drafts, self.calls = base, drafts, 0

        def generate(self, task, base_dir, prior_feedback=None, **kw):
            pairs = self.drafts[min(self.calls, len(self.drafts) - 1)]
            self.calls += 1
            for rel, content in pairs:
                p = self.base / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")

            class R:
                ok = True
                files_written = [rel for rel, _ in pairs]
                response = ""
                missing_context = []
            return R()

    class _Exec:
        def run(self, task):
            class R:
                passed = True
                exit_code = 0
                stdout = ""
                stderr = ""
                timed_out = False
            return R()

    class _Val:
        task_mode = "code"
        last_missing_context = []

        def approve(self, task, exec_result, coder_result, *, base_dir=None,
                    prior_critique=""):
            return True, "approved"

    def _loop(self, tmp_path, drafts, require=True):
        from tools.auto.inner_loop import InnerLoop
        coder = self._Coder(tmp_path, drafts)
        loop = InnerLoop(coder, self._Exec(), self._Val(), max_attempts=5,
                         require_tests=require, task_mode="code")
        return loop, coder

    def _task(self):
        return {"id": "S1", "title": "t", "instruction": "x",
                "target_files": ["mod.py", "tests/test_mod.py"],
                "acceptance_check": "true"}

    def test_code_without_tests_rejected_then_accepted(self, tmp_path):
        loop, coder = self._loop(tmp_path, [
            [("mod.py", "def f():\n    return 1\n")],
            [("mod.py", "def f():\n    return 1\n"),
             ("tests/test_mod.py", "from mod import f\n\ndef test_f():\n    assert f() == 1\n")],
        ])
        r = loop.run_task(self._task(), str(tmp_path))
        assert r.passed and coder.calls == 2
        assert any("tests mandate" in rec.feedback for rec in r.records)

    def test_tests_only_change_passes_gate(self, tmp_path):
        loop, coder = self._loop(tmp_path, [
            [("tests/test_mod.py", "def test_x():\n    assert True\n")],
        ])
        assert loop.run_task(self._task(), str(tmp_path)).passed
        assert coder.calls == 1

    def test_cap_reached_proceeds_fail_open(self, tmp_path):
        loop, coder = self._loop(tmp_path, [
            [("mod.py", "def f():\n    return 1\n")],
        ])
        r = loop.run_task(self._task(), str(tmp_path))
        assert r.passed
        assert coder.calls == 3

    def test_gate_off_by_default(self, tmp_path):
        loop, coder = self._loop(tmp_path, [
            [("mod.py", "def f():\n    return 1\n")],
        ], require=False)
        assert loop.run_task(self._task(), str(tmp_path)).passed
        assert coder.calls == 1


# ── selfhost pilot: pre-existing-pattern grandfathering в safety-сканере ─────

class TestPreexistingPatternGrandfathering:
    def _coder(self, allow=True):
        import configparser
        from tools.auto.coder import Coder
        cfg = configparser.ConfigParser()
        cfg.read_string(f"[coder]\nallow_preexisting_patterns = {'true' if allow else 'false'}\n"
                        "[loop]\ntimeout_seconds = 5\n[api]\nactive = local\n[api_local]\nnum_ctx = 0\n")
        return Coder(config=cfg, base_url="http://x", api_key="k", model="m",
                     api_format="ollama", verify_ssl=True, task_mode="code")

    UTILS_LIKE = ("import os, tempfile\n\n"
                  "def atomic_write_text(path, content):\n"
                  "    fd, tmp = tempfile.mkstemp()\n"
                  "    os.replace(tmp, path)\n"
                  "    os.unlink(tmp)\n")

    def test_editing_file_with_preexisting_pattern_allowed(self, tmp_path):
        c = self._coder()
        (tmp_path / "utils.py").write_text(self.UTILS_LIKE, encoding="utf-8")
        new = self.UTILS_LIKE + "\n\ndef human_duration(s):\n    return f'{s}s'\n"
        written, err = c._write_files(
            [{"path": "utils.py", "content": new}], tmp_path, "T",
            allowed_paths=frozenset({"utils.py"}))
        assert err == "" and written == ["utils.py"]

    def test_introducing_pattern_into_clean_file_still_blocked(self, tmp_path):
        c = self._coder()
        (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
        written, err = c._write_files(
            [{"path": "clean.py", "content": "import os\nos.unlink('x')\n"}],
            tmp_path, "T", allowed_paths=frozenset({"clean.py"}))
        assert written == [] and "blocked content pattern" in err

    def test_new_file_with_pattern_blocked(self, tmp_path):
        c = self._coder()
        written, err = c._write_files(
            [{"path": "fresh.py", "content": "import os\nos.unlink('x')\n"}],
            tmp_path, "T", allowed_paths=frozenset({"fresh.py"}))
        assert written == [] and "blocked" in err

    def test_flag_off_blocks_even_preexisting(self, tmp_path):
        c = self._coder(allow=False)
        (tmp_path / "utils.py").write_text(self.UTILS_LIKE, encoding="utf-8")
        written, err = c._write_files(
            [{"path": "utils.py", "content": self.UTILS_LIKE + "# edit\n"}],
            tmp_path, "T", allowed_paths=frozenset({"utils.py"}))
        assert written == [] and "blocked" in err


# ── Bug 7: word-boundary patterns (sudo/shutdown/reboot) were never given the
#    same pre-existing-pattern grandfathering as the other two checks in
#    _check_content_safety, so any file whose *legitimate* content contains
#    one of those words as data (coder.py's own pattern tables, a devops
#    README, ...) could never be written again — not even as a no-op rewrite.

class TestWordBoundaryGrandfathering:
    # Mirrors the real coder.py self-edit case: a file whose own content
    # necessarily contains "sudo" as data (defining what to block), not as an
    # actual invocation.
    SELF_REFERENTIAL = (
        "SUDO_TOKENS = ('sudo ', '\"sudo\"')  # patterns this scanner blocks\n"
        "def greet(name):\n"
        "    return f'hi {name}'\n"
    )

    def test_noop_rewrite_of_self_referential_file_allowed(self):
        # The exact real-world case: rewriting a file back to itself
        # unchanged must not be permanently blocked by its own data.
        safe, reason = Coder._check_content_safety(
            self.SELF_REFERENTIAL, "code", existing_content=self.SELF_REFERENTIAL)
        assert safe, reason

    def test_unrelated_edit_to_self_referential_file_allowed(self):
        edited = self.SELF_REFERENTIAL + "\ndef farewell(name):\n    return f'bye {name}'\n"
        safe, reason = Coder._check_content_safety(
            edited, "code", existing_content=self.SELF_REFERENTIAL)
        assert safe, reason

    def test_new_sudo_word_in_clean_file_still_blocked(self):
        clean = "def greet(name):\n    return f'hi {name}'\n"
        injected = clean + "\n# TODO: this needs sudo to install\n"
        safe, reason = Coder._check_content_safety(injected, "code", existing_content=clean)
        assert not safe and "sudo" in reason

    def test_new_shutdown_word_in_clean_file_still_blocked(self):
        clean = "def greet(name):\n    return f'hi {name}'\n"
        injected = clean + "\n# TODO: call shutdown when idle\n"
        safe, reason = Coder._check_content_safety(injected, "code", existing_content=clean)
        assert not safe and "shutdown" in reason

    def test_new_file_with_word_boundary_pattern_still_blocked(self):
        # No existing_content at all (brand-new file) — must never be
        # grandfathered regardless of what the pattern is.
        safe, reason = Coder._check_content_safety(
            "# reboot the host after install\n", "code", existing_content=None)
        assert not safe

    def test_identifier_names_still_exempt(self):
        # Unrelated pre-existing behaviour must be untouched: underscore-
        # joined identifiers embedding the keyword never match (\\b treats
        # '_' as a word character), independent of grandfathering.
        clean = "def test_reboot_gracefully():\n    assert True\n"
        safe, reason = Coder._check_content_safety(clean, "code", existing_content=None)
        assert safe, reason


# ── selfrun E2E: недостижимый rewriter при max_rounds < 3 ────────────────────

def test_unreachable_rewriter_config_warns(caplog, tmp_path):
    import configparser
    import logging
    from tools.auto.outer_loop import make_outer_loop
    cfg = configparser.ConfigParser()
    cfg.read_string("""
[api]
active = local
[api_local]
base_url = http://localhost:1
api_key = k
model = m
api_format = ollama
[auto]
max_rounds_per_task = 2
max_rewrites = 3
[loop]
timeout_seconds = 5
""")
    from tools.auto.state import StateStore
    with caplog.at_level(logging.WARNING):
        try:
            make_outer_loop(cfg, str(tmp_path), StateStore(tmp_path),
                            task_mode="code")
        except Exception:
            pass  # интересует только предупреждение о конфиге
    assert any("UNREACHABLE" in r.message for r in caplog.records)


def test_reachable_rewriter_config_silent(caplog, tmp_path):
    import configparser
    import logging
    from tools.auto.outer_loop import make_outer_loop
    cfg = configparser.ConfigParser()
    cfg.read_string("""
[api]
active = local
[api_local]
base_url = http://localhost:1
api_key = k
model = m
api_format = ollama
[auto]
max_rounds_per_task = 4
max_rewrites = 3
[loop]
timeout_seconds = 5
""")
    from tools.auto.state import StateStore
    with caplog.at_level(logging.WARNING):
        try:
            make_outer_loop(cfg, str(tmp_path), StateStore(tmp_path),
                            task_mode="code")
        except Exception:
            pass
    assert not any("UNREACHABLE" in r.message for r in caplog.records)


# ── Bug 8: Gate1 had no way to accept a task that creates a brand-new file —
#    the existence check unconditionally rejected any cited_location.file
#    that didn't already exist, treating legitimate "create X" tasks the
#    same as hallucinated paths. Fixed with an explicit, opt-in
#    cited_location.new_file flag.

def _gate1(task_mode="code"):
    cfg = configparser.ConfigParser()
    cfg.add_section("gate1"); cfg.add_section("api"); cfg.add_section("loop")
    return Gate1Filter(cfg, base_url="http://localhost:11434", api_key="x",
                        model="m", api_format="ollama", task_mode=task_mode)


def _candidate(new_file, file="app.py", symbol=None):
    return CandidateTask(
        title="Create app.py", instruction="Create app.py with a hello() function",
        target_files=[file], acceptance_check="true",
        cited_location=CitedLocation(file=file, symbol=symbol,
                                      line_start=None, line_end=None,
                                      new_file=new_file),
        cluster="support",
    )


class TestNewFileCreation:
    def test_is_valid_true_for_new_file_without_anchor(self):
        loc = CitedLocation(file="app.py", new_file=True)
        assert loc.is_valid("code")

    def test_is_valid_still_false_without_anchor_when_not_new_file(self):
        # Regression check: the pre-existing "must have symbol or line_start"
        # rule for code mode must be completely unchanged for the default case.
        loc = CitedLocation(file="app.py")
        assert not loc.is_valid("code")

    def test_existence_check_passes_for_declared_new_file(self, tmp_path):
        gate = _gate1()
        cand = _candidate(new_file=True, file="app.py")
        ok, reason, block = gate._check_existence(
            cand, tmp_path, cluster_files={"support": {"README.md"}})
        assert ok, reason
        assert "new file" in reason

    def test_existence_check_still_rejects_missing_file_by_default(self, tmp_path):
        # The critical regression-safety test: new_file defaults to False,
        # so an ordinary hallucinated path must be rejected exactly as
        # before this fix.
        gate = _gate1()
        cand = _candidate(new_file=False, file="app.py")
        ok, reason, block = gate._check_existence(
            cand, tmp_path, cluster_files={"support": {"README.md"}})
        assert not ok
        assert "hallucinated" in reason

    def test_new_file_cannot_escape_base_dir(self, tmp_path):
        gate = _gate1()
        cand = _candidate(new_file=True, file="../../etc/passwd")
        ok, reason, block = gate._check_existence(cand, tmp_path, cluster_files=None)
        assert not ok
        assert "escapes" in reason

    def test_new_file_rejected_if_path_is_existing_directory(self, tmp_path):
        (tmp_path / "app.py").mkdir()
        gate = _gate1()
        cand = _candidate(new_file=True, file="app.py")
        ok, reason, block = gate._check_existence(cand, tmp_path, cluster_files=None)
        assert not ok
        assert "directory" in reason

    def test_new_file_rejected_when_path_already_exists(self, tmp_path):
        # BUGFIX: new_file=True used to return early — "existence check
        # skipped" — for ANY path, including one that already exists as a
        # real file with real content. That handed the coder a target file
        # to overwrite with zero visibility into what was already there
        # (block was always ""), silently clobbering it. new_file's whole
        # premise is "this path does not exist yet"; if it does exist,
        # that premise is false and the candidate must be rejected, not
        # waved through.
        (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n")
        gate = _gate1()
        cand = _candidate(new_file=True, file="app.py")
        ok, reason, block = gate._check_existence(cand, tmp_path, cluster_files=None)
        assert not ok
        assert "already exists" in reason
        # And the file itself must be left completely untouched by the check.
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def hello():\n    return 'hi'\n"

    def test_checkpoint_roundtrip_preserves_new_file(self):
        from tools.auto.architect import _serialise_candidates, _deserialise_candidates
        cand = _candidate(new_file=True, file="app.py", symbol=None)
        data = _serialise_candidates([cand])
        assert data[0]["cited_location"]["new_file"] is True
        restored = _deserialise_candidates(data)
        assert restored[0].cited_location.new_file is True

    def test_presence_stage_skips_llm_for_new_file_candidate(self, tmp_path):
        # A new_file candidate must not trigger an LLM presence check —
        # mirrors the existing creative-mode exemption (AUTO-CR-8).
        gate = _gate1()
        cand = _candidate(new_file=True, file="app.py")
        with patch("tools.llm_stream.request_completion") as mock_call:
            accepted, rejected = gate.filter([cand], tmp_path,
                                              cluster_files={"support": {"README.md"}})
        mock_call.assert_not_called()
        assert len(accepted) == 1 and accepted[0].title == "Create app.py"


# ── novel14: bootstrap библии из seed-глав ────────────────────────────────────

class TestBibleBootstrapFromSeedChapters:
    def _cos(self, tmp_path, updates):
        from tools.auto.commit_on_success import CommitOnSuccess

        class _Bible:
            def __init__(self, path):
                self._path = path

            def update(self, text):
                updates.append(text)
                self._path.write_text(
                    (self._path.read_text(encoding="utf-8")
                     if self._path.exists() else "") + "• f\n",
                    encoding="utf-8")

        cos = CommitOnSuccess.__new__(CommitOnSuccess)
        cos._story_bible = _Bible(tmp_path / "story_bible.md")
        cos._base_dir = tmp_path
        cos._task_mode = "creative"
        return cos

    def test_seed_chapters_extracted_once_when_bible_empty(self, tmp_path):
        updates = []
        (tmp_path / "chapter_1.txt").write_text("Сид: виолончель и смены.",
                                                encoding="utf-8")
        (tmp_path / "chapter_2.txt").write_text("Новая глава.", encoding="utf-8")
        cos = self._cos(tmp_path, updates)
        cos._update_story_bible({"target_files": ["chapter_2.txt"]})
        # первым апдейтом — сид, затем новая глава
        assert updates[0].startswith("Сид") and updates[-1].startswith("Новая")
        assert len(updates) == 2

    def test_no_bootstrap_when_bible_already_has_content(self, tmp_path):
        updates = []
        (tmp_path / "chapter_1.txt").write_text("Сид.", encoding="utf-8")
        (tmp_path / "chapter_2.txt").write_text("Новая.", encoding="utf-8")
        cos = self._cos(tmp_path, updates)
        cos._story_bible._path.write_text("• уже есть\n", encoding="utf-8")
        cos._update_story_bible({"target_files": ["chapter_2.txt"]})
        assert len(updates) == 1 and updates[0].startswith("Новая")

    def test_written_and_reserved_files_excluded_from_seeds(self, tmp_path):
        updates = []
        (tmp_path / "chapter_2.txt").write_text("Свежезаписанная.",
                                                encoding="utf-8")
        cos = self._cos(tmp_path, updates)
        cos._update_story_bible({"target_files": ["chapter_2.txt"]})
        # сидов нет — только сама глава, без самозацикливания
        assert len(updates) == 1


# ── Bug 9: story_bible.py's writes were downgraded from atomic_write_text to
#    plain write_text() with the import removed entirely - a kill mid-write
#    could corrupt the persisted fact set (or pending-corrections state)
#    with no read-side detection, re-introducing exactly what Bug 4 already
#    fixed once for a different file.

class TestStoryBibleAtomicWrite:
    def _bible(self, tmp_path):
        from tools.auto.story_bible import StoryBible
        sb = StoryBible.__new__(StoryBible)
        sb._path = tmp_path / "story_bible.md"
        return sb

    def test_write_goes_through_atomic_write_text(self, tmp_path, monkeypatch):
        import tools.auto.story_bible as sb_mod
        calls = []
        monkeypatch.setattr(
            sb_mod, "atomic_write_text",
            lambda path, content: calls.append((path, content)))
        sb = self._bible(tmp_path)
        sb._write("• fact one\n• fact two\n")
        assert len(calls) == 1
        assert calls[0][0] == sb._path
        assert calls[0][1] == "• fact one\n• fact two\n"

    def test_write_does_not_call_plain_write_text_directly(self, tmp_path, monkeypatch):
        # Belt-and-suspenders: even if atomic_write_text were left importable
        # but bypassed, catch a direct Path.write_text call on the target path.
        from pathlib import Path
        sb = self._bible(tmp_path)
        original = Path.write_text
        def guard(self, *a, **k):
            if self == sb._path:
                raise AssertionError("bible file written via plain write_text(), not atomic_write_text")
            return original(self, *a, **k)
        monkeypatch.setattr(Path, "write_text", guard)
        sb._write("• fact\n")
        assert sb._path.read_text(encoding="utf-8") == "• fact\n"

    def test_register_correction_attempt_goes_through_atomic_write_text(
        self, tmp_path, monkeypatch
    ):
        import tools.auto.story_bible as sb_mod
        calls = []
        monkeypatch.setattr(
            sb_mod, "atomic_write_text",
            lambda path, content: calls.append((path, content)))
        sb = self._bible(tmp_path)
        result = sb._register_correction_attempt("new fact", "old fact")
        assert result is False  # first observation, below threshold
        assert len(calls) == 1
        assert calls[0][0] == sb._pending_corrections_path()

    def test_bible_write_survives_simulated_crash_without_corrupting_old_content(
        self, tmp_path, monkeypatch
    ):
        # End-to-end version of the Bug-4-style crash test, but through
        # StoryBible._write specifically, so this class regresses loudly if
        # _write is ever pointed at a non-atomic path again.
        sb = self._bible(tmp_path)
        sb._path.write_text("• old established fact\n", encoding="utf-8")

        def boom(*a, **k):
            raise OSError("simulated crash mid-write")
        monkeypatch.setattr(os, "fsync", boom)

        sb._write("• new fact that should never land\n")  # _write catches OSError, logs, returns

        # Old content must be completely untouched - never truncated.
        assert sb._path.read_text(encoding="utf-8") == "• old established fact\n"
        assert list(tmp_path.glob(".story_bible.md.*.tmp")) == []


# ── Bug 10 (and test-coverage restoration): the same "Tests not passed"
#    commit that regressed story_bible.py's atomic writes also deleted these
#    three test classes. Two of the three still have correct underlying
#    code (verified independently) and just needed their coverage restored;
#    CanonValidator's number-eating regex regression above needed an actual
#    code fix.

class TestSearchAgentEmptyLLMResponseFailsOpen:
    """SearchAgent._evaluate_with_llm's documented contract is fail-open on
    ANY filter failure. An empty JSON list ("[]") is syntactically valid so
    it skipped the except-block's fail-open path and silently rejected every
    reference instead — verified against a real, reproducing test case."""

    def test_empty_list_triggers_fail_open(self):
        from unittest.mock import patch
        from tools.search_agent import SearchAgent

        agent = SearchAgent(
            model="test-model", base_url="http://fake-host", api_key="x", timeout=5,
        )
        found_refs = {
            "ref1": {"code": "def ref1(): pass"},
            "ref2": {"code": "def ref2(): pass"},
        }
        with patch("tools.search_agent._request_completion", return_value="[]"):
            result = agent._evaluate_with_llm(found_refs)

        assert result == ["ref1", "ref2"], (
            "an empty LLM verdict must fail-open (approve all), not silently "
            "reject every reference"
        )

    def test_nonempty_list_still_filters_normally(self):
        from unittest.mock import patch
        from tools.search_agent import SearchAgent

        agent = SearchAgent(
            model="test-model", base_url="http://fake-host", api_key="x", timeout=5,
        )
        found_refs = {
            "ref1": {"code": "real dependency"},
            "ref2": {"code": "stdlib wrapper"},
            "ref3": {"code": "another real dep"},
        }
        with patch("tools.search_agent._request_completion", return_value='["ref1", "ref3"]'):
            result = agent._evaluate_with_llm(found_refs)

        assert result == ["ref1", "ref3"]


class TestCoderTargetFilesDotfileCollision:
    """Coder._write_files' target_files allow-list guard must not let a
    disallowed dotfile slip through by colliding, after normalisation, with
    an allowed non-dotfile name (or vice versa)."""

    def _coder(self):
        import configparser
        from tools.auto.coder import Coder
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api":       {"active": "local", "verify_ssl": "true"},
            "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
            "coder":     {"temperature": "0.2", "max_tokens": "1024"},
            "loop":      {"timeout_seconds": "60"},
        })
        return Coder(cfg, "http://localhost:9999", "", "x")

    def test_dotfile_not_authorised_is_rejected(self, tmp_path):
        coder = self._coder()
        allowed = frozenset({"notes.md"})
        written, _ = coder._write_files(
            [{"path": ".notes.md", "content": "sneaky"}],
            tmp_path, "T1", allowed_paths=allowed,
        )
        assert written == [], (
            ".notes.md must NOT be treated as the same target as the "
            "approved notes.md — the allow-list guard must not collapse "
            "distinct filenames that merely share a suffix after a leading "
            "dot/slash is stripped"
        )
        assert not (tmp_path / ".notes.md").exists()

    def test_leading_dot_slash_is_still_normalised(self, tmp_path):
        """A genuine "./" prefix (not a bare leading dot) must still be
        stripped so the intended normalisation keeps working."""
        coder = self._coder()
        allowed = frozenset({"notes.md"})
        written, _ = coder._write_files(
            [{"path": "./notes.md", "content": "hello"}],
            tmp_path, "T1", allowed_paths=allowed,
        )
        assert written == ["./notes.md"]
        assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello"


class TestCanonValidatorPreservesNumbers:
    """CanonValidator._extract_claims must not eat a claim's own leading
    digits (ages, years, counts) while stripping list-marker prefixes."""

    def _extractor(self, llm_reply):
        from tools.auto.canon_validator import CanonValidator
        cv = CanonValidator.__new__(CanonValidator)
        cv._llm = lambda system, user: llm_reply
        return cv

    def test_leading_number_in_claim_is_preserved(self):
        cv = self._extractor("3.5 million people lived there")
        claims = cv._extract_claims("chapter text")
        assert claims == ["3.5 million people lived there"]

    def test_numbered_marker_is_still_stripped(self):
        cv = self._extractor("1. She was born in 1990")
        claims = cv._extract_claims("chapter text")
        assert claims == ["She was born in 1990"]

    def test_bulleted_marker_is_still_stripped(self):
        cv = self._extractor("- Anna is 42 years old")
        claims = cv._extract_claims("chapter text")
        assert claims == ["Anna is 42 years old"]


# ── Bug 11: the upstream fix for subprocess+danger-token grandfathering
#    (b0aabde) required both tokens to independently appear somewhere in the
#    old file, but never verified they actually co-occurred as real combined
#    usage - so two unrelated, separate mentions still grandfathered in a
#    brand-new dangerous combination.

class TestSubprocessDangerComboRequiresCooccurrence:
    def test_separate_unrelated_mentions_do_not_grandfather_new_combo(self):
        existing = (
            'import subprocess  # used elsewhere for git commands, nothing dangerous\n'
            '\n'
            'def warn_user():\n'
            '    print("WARNING: never manually run rm -rf on the data directory")\n'
        )
        malicious = existing + (
            '\n'
            'def sneaky_cleanup():\n'
            '    subprocess.call("rm -rf /", shell=True)\n'
        )
        safe, reason = Coder._check_content_safety(
            malicious, "code", existing_content=existing)
        assert not safe, (
            "subprocess and the danger token were only ever mentioned in "
            "separate, unrelated lines in the old file - a brand-new "
            "combined usage must still be blocked"
        )

    def test_genuinely_combined_usage_still_grandfathered(self):
        existing = (
            'def cleanup_old_data():\n'
            '    subprocess.run(["rm", "-rf", tmp_dir], check=False)\n'
        )
        # No-op rewrite of the file's own genuinely pre-existing usage.
        safe, reason = Coder._check_content_safety(
            existing, "code", existing_content=existing)
        assert safe, reason

    def test_new_file_with_combo_still_blocked(self):
        malicious = (
            'def sneaky_cleanup():\n'
            '    subprocess.call("rm -rf /", shell=True)\n'
        )
        safe, reason = Coder._check_content_safety(
            malicious, "code", existing_content=None)
        assert not safe


# ── Bug 11b: _preexisting_combo's own comment-stripper (_strip_py_line_
#    comment, added alongside Bug 11) tracks quote state naively, with no
#    concept of a backslash-escaped quote. A line with an ODD number of
#    quote characters preceding the real "#" — e.g. an escaped quote inside
#    a string literal — desyncs that tracking so the scanner still believes
#    it is "inside a string" right when the actual comment starts, and
#    returns the line completely unstripped. That silently reopens the
#    exact hole Bug 11 exists to close: a comment on such a line can once
#    again make an unrelated same-line mention count as genuine co-
#    occurrence, letting a brand-new dangerous combo be grandfathered in.

class TestPreexistingComboCommentStripHandlesEscapedQuotes:
    def test_escaped_quote_before_comment_does_not_grandfather(self):
        # Both dangerous tokens appear ONLY inside this line's comment text,
        # never in real code — but an escaped quote earlier on the SAME
        # line (leaving an ODD count of quote characters before the "#")
        # is what desyncs the naive tracker. If comment-stripping works,
        # this line contributes nothing to the co-occurrence check; if it
        # desyncs, the whole line (comment included) counts.
        existing = (
            'y = "she said \\"hi"'
            '  # never combine subprocess with rm -rf\n'
        )
        malicious = existing + (
            '\n'
            'def sneaky_cleanup():\n'
            '    subprocess.call("rm -rf /", shell=True)\n'
        )
        safe, reason = Coder._check_content_safety(
            malicious, "code", existing_content=existing)
        assert not safe, (
            "a comment reachable only via a backslash-escaped-quote line "
            "must not count toward same-line co-occurrence — the escaped "
            "quote must not desync the comment stripper into leaving the "
            "comment text in scope"
        )

    def test_genuine_combined_usage_after_escaped_quote_line_still_grandfathered(self):
        existing = (
            'x = "he said \\"hello\\""  # just a greeting\n'
            'def cleanup_old_data():\n'
            '    subprocess.run(["rm", "-rf", tmp_dir], check=False)\n'
        )
        safe, reason = Coder._check_content_safety(
            existing, "code", existing_content=existing)
        assert safe, reason


# ── Bug 12: a blocked acceptance_check used to fall back to bare `pytest`,
#    which _prepare_workspace's whole-repo mirror (AUTO-FIX-1) means picks
#    up and runs every OTHER pre-existing test too - so in any repo with a
#    healthy existing test suite, a task whose real check was blocked for
#    safety reasons could get marked passed and committed without its
#    actual goal ever being verified.

class TestBlockedAcceptanceCheckCannotSpuriouslyPass:
    def test_blocked_check_falls_back_to_guaranteed_failure(self, tmp_path):
        from tools.auto.executor import Executor
        executor = Executor(base_dir=tmp_path, timeout_sec=10)
        cmd = executor._resolve_command("rm -rf /", [], tmp_path)
        assert cmd == "false"

    def test_blocked_check_does_not_spuriously_pass_via_unrelated_tests(self, tmp_path):
        from tools.auto.executor import Executor
        # A repo with its own healthy, completely unrelated test suite -
        # exactly the self-hosting scenario this codebase's own pilots
        # exercise (this repo included).
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_unrelated.py").write_text(
            "def test_something_unrelated():\n    assert 1 + 1 == 2\n"
        )
        (tmp_path / "app.py").write_text(
            "def broken():\n    raise NotImplementedError\n"
        )
        executor = Executor(base_dir=tmp_path, timeout_sec=30)
        task = {
            "id": "T1",
            "target_files": ["app.py"],
            # A real check for the task's actual goal, but flagged unsafe -
            # must never be silently replaced by something that can pass
            # for unrelated reasons.
            "acceptance_check": "python app.py && rm -rf /tmp/marker",
        }
        result = executor.run(task)
        assert not result.passed, (
            "a task whose acceptance_check was blocked for safety must "
            "never be marked passed via an unrelated pre-existing test "
            "in the mirrored workspace"
        )
