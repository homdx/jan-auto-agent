"""tests_bugfix/test_bugfix_341_pipeline_state.py — 341 AGNES-2: `--auto` stops or
misjudges on ordinary repo state.

Five defects, one file:

  * AUTO-T53 — a repo whose only test file is vendor-installed
    (`venv/lib/site-packages/test_vendor.py`) read as "has tests", hiding the
    phantom-suite check on ordinary targets. Files under `venv`, `node_modules`
    and `site-packages`, at any depth, do not count; the walk is pruned, not
    filtered after the fact, and a project's own `tests/test_x.py` still
    counts with a `venv/` beside it.
  * AUTO-T60 — an `OSError` from a state write or the round bookkeeping in
    `OuterLoop.run_task` escaped out of the per-task body of the `--auto`
    loop and ended the whole run (the controller has no task-level catch).
    `run_task` now fails only that task — `OuterLoopResult(passed=False)` —
    logs it, and the next task runs. The budget ledger is opened before the
    guarded `_run_rounds`, so it is guarded of its own; and a raising inner
    loop is not what this covers (the inner loop already has its own guard).
  * AUTO-T61 — a cluster-hash file of undecodable bytes (`ff fe` + JSON)
    raised `UnicodeDecodeError` out of `PlanEmitter._load_cluster_hashes`,
    stopping planning. It now reads as no stored hash, with the same warning
    as a corrupt file.
  * AUTO-T56 — a named LLM profile with a blank `base_url` or `model` was
    accepted and failed only at request time. It now raises the documented
    `ValueError` naming the section and key; a blank `api_key` stays allowed.
    The contest's OpenRouter backend is the one documented exception — its
    profile's `model` is deliberately empty because each agent gets its own
    model id from the roster — and it opts in with `allow_blank_model=True`.
  * AUTO-T58 — in a linked worktree `.git` is a *file* (`gitdir: ...`), so
    `GitManager.ensure_repo` ran `git init` inside it and returned `True`.
    It now returns `False` and runs nothing; `HEAD` and the gitfile are
    untouched.
"""

from __future__ import annotations

import configparser
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.existence_validator import ExistenceValidator
from tools.auto.git_manager import GitManager
from tools.auto.llm_profile import LlmSettings, resolve_llm_profile
from tools.auto.outer_loop import OuterLoop
from tools.auto.plan_emitter import PlanEmitter
from tools.auto.state import (
    StateStore, make_task, STATUS_IN_PROGRESS,
)


# ── AUTO-T53: vendor tests are not the project's suite ───────────────────────

def _write(path: Path, text: str = "def test_x():\n    assert True\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestVendorTestsNotCounted:
    def test_venv_only_repo_reports_no_tests(self, tmp_path):
        """The ticket's exact repro: only `venv/lib/site-packages/test_vendor.py`."""
        _write(tmp_path / "venv" / "lib" / "site-packages" / "test_vendor.py")
        assert ExistenceValidator._repo_has_tests(tmp_path) is False

    def test_project_tests_still_count_with_venv_beside(self, tmp_path):
        _write(tmp_path / "tests" / "test_x.py")
        _write(tmp_path / "venv" / "lib" / "site-packages" / "test_vendor.py")
        assert ExistenceValidator._repo_has_tests(tmp_path) is True

    def test_node_modules_pruned_at_any_depth(self, tmp_path):
        _write(tmp_path / "node_modules" / "pkg" / "node_modules" / "pkg2" / "test_b.py")
        assert ExistenceValidator._repo_has_tests(tmp_path) is False

    def test_root_site_packages_pruned(self, tmp_path):
        _write(tmp_path / "site-packages" / "conftest.py", "")
        assert ExistenceValidator._repo_has_tests(tmp_path) is False

    def test_venv_no_longer_masks_the_phantom_suite_check(self, tmp_path):
        """Consequence at the gate: "run pytest" in a vendor-only repo is flagged."""
        _write(tmp_path / "venv" / "lib" / "site-packages" / "test_vendor.py")
        verdict = ExistenceValidator().check(
            "Run `pytest` to check the suite.\n", base_dir=tmp_path,
        )
        assert verdict.approved is False
        assert verdict.phantom_tests is True


# ── AUTO-T60: run_task never raises out of a state write or bookkeeping ──────

@dataclass
class _InnerResult:
    task_id: str = "AUTO-T1"
    passed: bool = False
    attempts_used: int = 1
    last_feedback: str = "still broken"


class _RecordingInnerLoop:
    """Records which task ids reached the inner loop; every call returns a
    scripted result (a failed round by default, so the round bookkeeping
    after the call actually runs)."""
    max_task_seconds = 0

    def __init__(self, passed: bool = False):
        self._passed = passed
        self.calls: list[str] = []

    def run_task(self, task, base_dir, prior_feedback=None, prior_implementations=None):
        task_id = task.get("id", "?")
        self.calls.append(task_id)
        return _InnerResult(task_id=task_id, passed=self._passed)


def _two_task_state(tmp_path: Path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("goal", tmp_path)
    st.upsert_task(make_task(id="AUTO-T1", title="one", instruction="x",
                             target_files=["f.py"]))
    st.upsert_task(make_task(id="AUTO-T2", title="two", instruction="x",
                             target_files=["f.py"]))
    return st


def _task(task_id: str) -> dict:
    return {"id": task_id, "title": task_id, "instruction": "x",
            "target_files": ["f.py"], "impl_version": 1}


class TestRunTaskSurvivesStateWriteFailure:
    def test_state_write_error_fails_the_task_not_the_run(self, tmp_path):
        """An OSError from the IN_PROGRESS write — before the inner loop is
        even reached — must come back as a failed result, not out the top."""
        st = _two_task_state(tmp_path)
        inner = _RecordingInnerLoop()
        outer = OuterLoop(inner, st, max_rounds=2)

        real_set = st.set_task_status

        def flaky_set(task_id, status, **extra):
            if task_id == "AUTO-T1" and status == STATUS_IN_PROGRESS:
                raise OSError("simulated state write failure")
            return real_set(task_id, status, **extra)

        st.set_task_status = flaky_set

        result = outer.run_task(_task("AUTO-T1"), tmp_path)
        assert result.task_id == "AUTO-T1"
        assert result.passed is False
        assert "AUTO-T1" not in inner.calls, \
            "the inner loop must not be reached for a task whose bookkeeping failed"
        # The error is logged — in the run log, not just the logger.
        run_log = (st.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "AUTO-T1" in run_log and "bookkeeping" in run_log

    def test_next_task_still_runs(self, tmp_path):
        st = _two_task_state(tmp_path)
        inner = _RecordingInnerLoop(passed=True)
        outer = OuterLoop(inner, st, max_rounds=2)

        real_set = st.set_task_status
        calls = {"n": 0}

        def flaky_once(task_id, status, **extra):
            calls["n"] += 1
            if calls["n"] == 1:          # the first status write of the run
                raise OSError("simulated state write failure")
            return real_set(task_id, status, **extra)

        st.set_task_status = flaky_once

        first = outer.run_task(_task("AUTO-T1"), tmp_path)
        assert first.passed is False
        second = outer.run_task(_task("AUTO-T2"), tmp_path)
        assert second.passed is True
        assert "AUTO-T2" in inner.calls

    def test_round_bookkeeping_error_after_a_failed_round_does_not_escape(self, tmp_path):
        """The counter write right after a failed round is bookkeeping too."""
        st = StateStore(tmp_path / ".agent")
        st.initialise("goal", tmp_path)
        st.upsert_task(make_task(id="AUTO-T1", title="one", instruction="x",
                                 target_files=["f.py"]))
        inner = _RecordingInnerLoop(passed=False)
        outer = OuterLoop(inner, st, max_rounds=2)

        def flaky_inc(task_id, **kw):
            raise OSError("simulated counter write failure")

        st.increment_task_counters = flaky_inc

        result = outer.run_task(_task("AUTO-T1"), tmp_path)
        assert result.passed is False
        assert "AUTO-T1" in inner.calls, \
            "the inner loop ran; the error is in the bookkeeping after it"


# ── AUTO-T61: an undecodable hash file reads as no stored hash ──────────────

class _FakeCluster:
    def __init__(self, name: str, files: list[str] | None = None):
        self.name = name
        self.files = files or []


class TestUndecodableHashFile:
    def _emitter(self, tmp_path: Path) -> PlanEmitter:
        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)
        return PlanEmitter(tmp_path, state, None)

    def test_ff_fe_prefix_reads_as_absent(self, tmp_path, caplog):
        emitter = self._emitter(tmp_path)
        emitter._hashes_path.write_bytes(b"\xff\xfe" + b'{"cluster": "deadbeef"}')
        with caplog.at_level(logging.WARNING, logger="tools.auto.plan_emitter"):
            assert emitter._load_cluster_hashes() == {}
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        assert any("could not read hash file" in m for m in warnings)

    def test_same_warning_as_a_corrupt_file(self, tmp_path, caplog):
        emitter = self._emitter(tmp_path)
        emitter._hashes_path.write_text("not json at all", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tools.auto.plan_emitter"):
            assert emitter._load_cluster_hashes() == {}
        corrupt_warning = next(
            r.getMessage() for r in caplog.records
            if "could not read hash file" in r.getMessage()
        )
        caplog.clear()
        emitter._hashes_path.write_bytes(b"\xff\xfe" + b"{}")
        with caplog.at_level(logging.WARNING, logger="tools.auto.plan_emitter"):
            assert emitter._load_cluster_hashes() == {}
        undecodable_warning = next(
            r.getMessage() for r in caplog.records
            if "could not read hash file" in r.getMessage()
        )
        assert corrupt_warning.split(":")[0] == undecodable_warning.split(":")[0]

    def test_absent_hash_marks_every_cluster_stale(self, tmp_path):
        emitter = self._emitter(tmp_path)
        emitter._hashes_path.write_bytes(b"\xff\xfe" + b"{}")
        stale = emitter.changed_clusters([_FakeCluster("c1"), _FakeCluster("c2")])
        assert [c.name for c in stale] == ["c1", "c2"]


# ── AUTO-T56: a blank base_url or model in a named profile raises ────────────

_BLANK_DEFAULTS = LlmSettings(
    base_url="https://shared.example/v1", api_key="shared-key", model="shared-model",
)


def _profile_config(section: dict, section_name: str = "validator_agent",
                    key: str = "canon_llm_profile") -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
        section_name: {key: "strong"},
        "strong": section,
    })
    return c


class TestBlankProfileFields:
    @pytest.mark.parametrize("blank_field", ["base_url", "model"])
    def test_blank_required_field_raises_naming_section_and_key(self, blank_field):
        section = {"base_url": "https://a.example", "api_key": "k", "model": "m"}
        section[blank_field] = " "
        with pytest.raises(ValueError) as exc:
            resolve_llm_profile(
                _profile_config(section), "validator_agent",
                "canon_llm_profile", defaults=_BLANK_DEFAULTS,
            )
        msg = str(exc.value)
        assert "strong" in msg            # the section
        assert "canon_llm_profile" in msg  # the key
        assert blank_field in msg

    def test_blank_api_key_still_allowed(self):
        """A local server needs no credential: an empty api_key must resolve."""
        settings, name = resolve_llm_profile(
            _profile_config({"base_url": "http://localhost:11434",
                             "api_key": "", "model": "local-model"}),
            "validator_agent", "canon_llm_profile", defaults=_BLANK_DEFAULTS,
        )
        assert name == "strong"
        assert settings.api_key == ""
        assert settings.base_url == "http://localhost:11434"

    def test_allow_blank_model_keeps_a_deliberate_blank(self):
        """The contest's OpenRouter backend supplies the model per agent, so
        its resolver call opts in; a blank model then resolves as blank."""
        config = _profile_config(
            {"base_url": "https://openrouter.ai/api/v1", "api_key": "k", "model": ""},
            section_name="contest", key="openrouter_llm_profile")
        settings, name = resolve_llm_profile(
            config, "contest", "openrouter_llm_profile", defaults=_BLANK_DEFAULTS,
            allow_blank_model=True,
        )
        assert name == "strong"
        assert settings.model == ""

    def test_allow_blank_model_does_not_allow_a_blank_base_url(self):
        config = _profile_config(
            {"base_url": "", "api_key": "k", "model": "m"},
            section_name="contest", key="openrouter_llm_profile")
        with pytest.raises(ValueError):
            resolve_llm_profile(
                config, "contest", "openrouter_llm_profile", defaults=_BLANK_DEFAULTS,
                allow_blank_model=True,
            )


# ── AUTO-T58: a .git file (linked worktree) is not re-initialised ────────────

def _git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr.strip()}"


class TestEnsureRepoLinkedWorktree:
    def test_worktree_gitfile_returns_false_and_runs_nothing(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        _git(["init", "-q", "-b", "main"], main)
        (main / "a.py").write_text("print('a')\n", encoding="utf-8")
        _git(["add", "a.py"], main)
        _git(["-c", "user.name=worktree-test", "-c", "user.email=w@example.com",
              "commit", "-qm", "first"], main)

        worktree = tmp_path / "wt"
        _git(["worktree", "add", "-q", str(worktree), "-b", "feature"], main)
        assert (worktree / ".git").is_file(), "sanity: a linked worktree's .git is a file"

        # The worktree's git dir is named after the worktree PATH's basename,
        # not the branch: main/.git/worktrees/wt.
        wt_gitdir = main / ".git" / "worktrees" / "wt"
        head_before = (wt_gitdir / "HEAD").read_text(encoding="utf-8")
        gitdir_files_before = sorted(p.name for p in wt_gitdir.iterdir())

        gm = GitManager(worktree)
        git_calls: list[list[str]] = []
        gm._run = lambda cmd, msg: git_calls.append(cmd) or ""
        assert gm.ensure_repo() is False
        assert git_calls == [], "ensure_repo must run nothing in a linked worktree"

        # HEAD and the gitfile are unchanged.
        assert (wt_gitdir / "HEAD").read_text(encoding="utf-8") == head_before
        assert (worktree / ".git").read_text(encoding="utf-8").startswith("gitdir: ")
        assert sorted(p.name for p in wt_gitdir.iterdir()) == gitdir_files_before


# ── What a narrow fix breaks: the contest's own config, and paths around the walk ───

_CONTEST_INI = Path(__file__).resolve().parent.parent / "contest.ini"
_CONTEST_KEYS = ("CONTEST_GATE_API_KEY", "CONTEST_DRAFT_API_KEY", "CONTEST_OPENROUTER_API_KEY")


def _contest_ini(tmp_path, monkeypatch, blank_section=""):
    """A copy of the shipped contest.ini, with one section's `model` blanked if asked."""
    for name in _CONTEST_KEYS:
        monkeypatch.setenv(name, "k")
    lines, section = [], ""
    for line in _CONTEST_INI.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            section = line.strip()
        if section == blank_section and line.startswith("model"):
            line = "model ="
        lines.append(line)
    path = tmp_path / "contest.ini"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestShippedContestConfig:
    def test_the_openrouter_profiles_deliberate_blank_model_still_loads(self, tmp_path, monkeypatch):
        from tools.contest.roster import load_roster
        assert load_roster(_contest_ini(tmp_path, monkeypatch), backend="openrouter") is not None

    def test_a_blank_gate_model_is_refused_at_load(self, tmp_path, monkeypatch):
        from tools.contest.roster import RosterError, load_roster
        path = _contest_ini(tmp_path, monkeypatch, blank_section="[contest_gate_llm]")
        with pytest.raises(RosterError, match="model"):
            load_roster(path)


class TestTheWalkReadsOnlyInsideTheBase:
    @pytest.mark.parametrize("parent", ["venv", "node_modules", "site-packages", ".cache"])
    def test_a_project_under_a_vendor_named_parent_still_has_tests(self, tmp_path, parent):
        base = tmp_path / parent / "proj"
        (base / "tests").mkdir(parents=True)
        (base / "tests" / "test_x.py").write_text("", encoding="utf-8")
        assert ExistenceValidator._repo_has_tests(base) is True

    def test_a_directory_merely_containing_the_name_is_not_vendor(self, tmp_path):
        (tmp_path / "my_venv").mkdir()
        (tmp_path / "my_venv" / "test_x.py").write_text("", encoding="utf-8")
        assert ExistenceValidator._repo_has_tests(tmp_path) is True


class TestTheGuardIsNotACatchAll:
    @pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
    def test_a_stop_is_not_swallowed(self, tmp_path, stop):
        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)
        state.upsert_task(make_task(id="T1", title="t", instruction="x",
                                    target_files=["a.py"], acceptance_check="pytest -q"))

        def raise_stop(*_a, **_k):
            raise stop()
        state.set_task_status = raise_stop
        with pytest.raises(stop):
            OuterLoop(_RecordingInnerLoop(), state, max_rounds=1).run_task(state.get_task("T1"), tmp_path)

    def test_the_budget_ledger_closes_after_a_failure(self, tmp_path):
        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)
        state.upsert_task(make_task(id="T1", title="t", instruction="x",
                                    target_files=["a.py"], acceptance_check="pytest -q"))
        written = []
        write = state.write_task_file

        def record(task_id, name, text, *a, **k):
            written.append(text)
            return write(task_id, name, text, *a, **k)
        state.write_task_file = record

        def fail(*_a, **_k):
            raise OSError("disk full")
        state.set_task_status = fail
        loop = OuterLoop(_RecordingInnerLoop(), state, max_rounds=1)
        loop._task_budget_seconds = lambda: 100.0
        result = loop.run_task(state.get_task("T1"), tmp_path)
        assert result.passed is False
        assert written and "session_started_at" not in written[-1]
