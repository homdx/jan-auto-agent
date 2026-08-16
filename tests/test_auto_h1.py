"""tests/test_auto_h1.py — AUTO-H1: Re-validate an existing plan.

Story ACs verified here
-----------------------
AUTO-H1 — Plan re-validation ("false positive" sweep)

  AC-REMOVE     — StateStore.remove_task() deletes a task from plan.json,
                  persists the change, and strips the removed id from any
                  other task's ``dependencies`` list.

  AC-CANDIDATE  — A plan.json task dict round-trips into the same
                  CandidateTask shape the Architect produces, including the
                  no-citation fallback.

  AC-VALIDATE   — validate_plan() re-runs Gate 1 (existence + LLM presence
                  check) against ONLY todo/in_progress tasks; confirmed
                  false positives (either stage) are removed from plan.json
                  and appended to IMPROVEMENTS-FALSE.md; confirmed-still-
                  needed tasks are left untouched; done/blocked tasks are
                  never sent through Gate 1 at all.

  AC-NOPLAN     — validate_plan() raises RuntimeError with an actionable
                  message when .agent/plan.json does not exist yet.

  AC-IDEMPOTENT — Appending the same task id to IMPROVEMENTS-FALSE.md twice
                  does not duplicate the entry.

  AC-CLI        — --validate-plan is parsed by argparse and main.py
                  dispatches to tools.auto.plan_validator.run_validate
                  without touching the --auto / --collect / --faq paths.

  AC-EXITCODE   — run_validate() returns 0 on success (including "nothing
                  to check") and 1 with an "Error: ..." message when no
                  plan exists.

No live LLM is required; all calls to request_completion are patched.
"""

from __future__ import annotations

import configparser
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.backlog_prioritiser import PrioritisedBacklog, ReadyTask, to_improvements_md
from tools.auto.plan_emitter import IMPROVEMENTS_FILENAME
from tools.auto.state import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_TODO,
    StateStore,
    make_task,
)
from tools.auto.plan_validator import (
    IMPROVEMENTS_FALSE_FILENAME,
    RemovedTask,
    _already_recorded_ids,
    _append_false_positives,
    _presence_check_skip_reason,
    _remove_from_improvements_md,
    _task_to_candidate,
    run_validate,
    validate_plan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_PY = """\
def parse_config(raw):
    # TODO: validate input
    return raw


def stable_func():
    return 42
"""


def _git_init(path: Path) -> None:
    for cmd in [
        ["git", "init", str(path)],
        ["git", "-C", str(path), "config", "user.email", "agent@test"],
        ["git", "-C", str(path), "config", "user.name", "Agent"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)

    (path / "pkg").mkdir()
    (path / "pkg" / "sample.py").write_text(_SAMPLE_PY, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"],
                    check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                    check=True, capture_output=True)


def _write_ini(
    tmp: Path, *, task_mode: str = "code", skip_llm: bool = False, filename: str = "agents.ini",
) -> Path:
    ini = tmp / filename
    ini.write_text(f"""
[auto]
git_user = agent
git_email = agent@test
task_mode = {task_mode}

[api]
active = local
verify_ssl = false

[api_local]
base_url = http://localhost:11434/v1
api_key =
model = dummy
api_format = openai

[gate1]
skip_llm = {"true" if skip_llm else "false"}
""")
    return ini


def _git_log(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    return [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]


def _fake_gate1_llm(*args, **kwargs):
    """Route Gate 1's presence check by a marker string in the instruction.

    Accepts *args/**kwargs rather than a fixed signature so it survives
    however LLMClientBase happens to forward positional/keyword args to
    tools.llm_stream.request_completion.
    """
    payload = kwargs.get("payload")
    if payload is None:
        payload = next((a for a in args if isinstance(a, dict) and "messages" in a), {})
    messages = payload.get("messages", [])
    user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if "FALSE_POSITIVE_MARKER" in user:
        return json.dumps({"verdict": "rejected", "reason": "already fixed by a prior commit"})
    return json.dumps({"verdict": "confirmed", "reason": "problem still present"})


def _write_matching_improvements_md(repo: Path, tasks: list[dict]) -> str:
    """Render a realistic IMPROVEMENTS.md for *tasks* with the real
    backlog_prioritiser renderer, so section-removal tests exercise the
    exact format plan_emitter.emit() produces in production — including
    every seeded task (regardless of its current plan.json status), since
    real IMPROVEMENTS.md is a plan-time snapshot that never reflects later
    status changes.
    """
    ready = []
    for t in tasks:
        loc0 = (t.get("cited_locations") or [{}])[0]
        ready.append(ReadyTask(
            task_id=t["id"],
            candidate=CandidateTask(
                title=t["title"],
                instruction=t["instruction"],
                target_files=list(t.get("target_files") or []),
                acceptance_check=t.get("acceptance_check", ""),
                cited_location=CitedLocation(
                    file=loc0.get("file", ""),
                    symbol=loc0.get("symbol"),
                    line_start=loc0.get("line_start"),
                    line_end=loc0.get("line_end"),
                    new_file=bool(loc0.get("new_file", False)),
                ),
                cluster="pkg",
            ),
        ))
    backlog = PrioritisedBacklog(auto_tasks=ready, manual_suggestions=[])
    text = to_improvements_md(backlog)
    (repo / IMPROVEMENTS_FILENAME).write_text(text, encoding="utf-8")
    return text


def _seed_plan(agent_dir: Path, *, goal: str, base_dir: Path, tasks: list[dict]) -> None:
    """Write .agent/plan.json directly via StateStore, bypassing PlanEmitter."""
    store = StateStore(agent_dir)
    store.initialise(goal, base_dir)
    for t in tasks:
        store.upsert_task(make_task(**t))


def _task_kwargs(
    task_id: str,
    *,
    title: str,
    instruction: str,
    file: str = "pkg/sample.py",
    symbol: "str | None" = "parse_config",
    status: str = STATUS_TODO,
) -> dict:
    return dict(
        id=task_id,
        title=title,
        instruction=instruction,
        target_files=[file],
        acceptance_check="true",
        status=status,
        cited_locations=[{
            "file": file, "symbol": symbol, "line_start": None, "line_end": None,
            "new_file": False,
        }],
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC-REMOVE — StateStore.remove_task
# ─────────────────────────────────────────────────────────────────────────────

class TestRemoveTask:

    @pytest.fixture()
    def store(self, tmp_path: Path) -> StateStore:
        agent_dir = tmp_path / ".agent"
        s = StateStore(agent_dir)
        s.initialise("test goal", tmp_path)
        s.upsert_task(make_task(
            id="AUTO-T1", title="First", instruction="do a thing",
            target_files=["a.py"], acceptance_check="true",
        ))
        s.upsert_task(make_task(
            id="AUTO-T2", title="Second", instruction="do a thing that needs T1",
            target_files=["b.py"], acceptance_check="true",
            dependencies=["AUTO-T1"],
        ))
        return s

    def test_removes_task_from_plan(self, store: StateStore) -> None:
        assert store.remove_task("AUTO-T1") is True
        assert store.get_task("AUTO-T1") is None
        assert {t["id"] for t in store.all_tasks()} == {"AUTO-T2"}

    def test_missing_id_is_a_noop(self, store: StateStore) -> None:
        assert store.remove_task("AUTO-DOES-NOT-EXIST") is False
        assert {t["id"] for t in store.all_tasks()} == {"AUTO-T1", "AUTO-T2"}

    def test_clears_removed_id_from_dependents(self, store: StateStore) -> None:
        store.remove_task("AUTO-T1")
        dependent = store.get_task("AUTO-T2")
        assert dependent is not None
        assert dependent["dependencies"] == []

    def test_persists_across_reload(self, store: StateStore, tmp_path: Path) -> None:
        store.remove_task("AUTO-T1")
        reloaded = StateStore(tmp_path / ".agent")
        reloaded.initialise("test goal", tmp_path)
        assert reloaded.get_task("AUTO-T1") is None
        assert reloaded.get_task("AUTO-T2")["dependencies"] == []

    def test_refreshes_progress_counters(self, store: StateStore) -> None:
        store.set_task_status("AUTO-T1", STATUS_DONE)
        before = store.get_progress()["done_count"]
        assert before == 1
        store.remove_task("AUTO-T1")
        assert store.get_progress()["done_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC-CANDIDATE — _task_to_candidate
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskToCandidate:

    def test_full_citation_round_trips(self) -> None:
        task = make_task(
            id="AUTO-T1", title="Fix it", instruction="fix the thing",
            target_files=["pkg/sample.py"], acceptance_check="true",
            cited_locations=[{
                "file": "pkg/sample.py", "symbol": "parse_config",
                "line_start": None, "line_end": None, "new_file": False,
            }],
        )
        c = _task_to_candidate(task)
        assert c.title == "Fix it"
        assert c.instruction == "fix the thing"
        assert c.target_files == ["pkg/sample.py"]
        assert c.cited_location == CitedLocation(
            file="pkg/sample.py", symbol="parse_config",
            line_start=None, line_end=None, new_file=False,
        )

    def test_line_range_citation(self) -> None:
        task = make_task(
            id="AUTO-T1", title="Fix it", instruction="fix the thing",
            target_files=["pkg/sample.py"], acceptance_check="true",
            cited_locations=[{
                "file": "pkg/sample.py", "symbol": None,
                "line_start": 3, "line_end": 5, "new_file": False,
            }],
        )
        c = _task_to_candidate(task)
        assert c.cited_location.line_start == 3
        assert c.cited_location.line_end == 5
        assert c.cited_location.symbol is None

    def test_missing_citation_falls_back_to_target_file(self) -> None:
        task = make_task(
            id="AUTO-T1", title="Fix it", instruction="fix the thing",
            target_files=["pkg/sample.py"], acceptance_check="true",
        )
        c = _task_to_candidate(task)
        assert c.cited_location.file == "pkg/sample.py"
        assert c.cited_location.symbol is None
        assert c.cited_location.new_file is False

    def test_new_file_flag_preserved(self) -> None:
        task = make_task(
            id="AUTO-T1", title="Create it", instruction="create the thing",
            target_files=["pkg/new.py"], acceptance_check="true",
            cited_locations=[{
                "file": "pkg/new.py", "symbol": None,
                "line_start": None, "line_end": None, "new_file": True,
            }],
        )
        c = _task_to_candidate(task)
        assert c.cited_location.new_file is True


# ─────────────────────────────────────────────────────────────────────────────
# AC-STRIP — IMPROVEMENTS.md section removal
# ─────────────────────────────────────────────────────────────────────────────

class TestRemoveFromImprovementsMd:

    _TASKS = [
        _task_kwargs("AUTO-T1", title="Validate parse_config input",
                     instruction="parse_config accepts anything", symbol="parse_config"),
        _task_kwargs("AUTO-T2", title="Add docstring to stable_func",
                     instruction="## no docstring here\nplease add one",
                     symbol="stable_func"),
        _task_kwargs("AUTO-T3", title="Third real task",
                     instruction="keep me", file="pkg/other.py", symbol="foo"),
    ]

    def test_no_op_when_file_missing(self, tmp_path: Path) -> None:
        _remove_from_improvements_md(tmp_path, ["AUTO-T1"])
        assert not (tmp_path / IMPROVEMENTS_FILENAME).exists()

    def test_no_op_when_removed_ids_empty(self, tmp_path: Path) -> None:
        text = _write_matching_improvements_md(tmp_path, self._TASKS)
        _remove_from_improvements_md(tmp_path, [])
        assert (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8") == text

    def test_removes_only_the_targeted_section(self, tmp_path: Path) -> None:
        _write_matching_improvements_md(tmp_path, self._TASKS)
        _remove_from_improvements_md(tmp_path, ["AUTO-T2"])
        out = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T1:" in out
        assert "### AUTO-T2:" not in out
        assert "### AUTO-T3:" in out

    def test_survives_embedded_hash_hash_in_removed_section(self, tmp_path: Path) -> None:
        """AUTO-T2's own instruction starts a line with '## ' — the removal
        must not stop early there, and must not treat it as the Manual
        Suggestions boundary."""
        _write_matching_improvements_md(tmp_path, self._TASKS)
        _remove_from_improvements_md(tmp_path, ["AUTO-T2"])
        out = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "no docstring here" not in out
        assert "## Manual Suggestions" in out  # section itself still intact

    def test_untouched_sections_are_byte_identical(self, tmp_path: Path) -> None:
        original = _write_matching_improvements_md(tmp_path, self._TASKS)
        t1_section = original[original.index("### AUTO-T1:"):original.index("### AUTO-T2:")]
        _remove_from_improvements_md(tmp_path, ["AUTO-T2"])
        out = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert t1_section in out

    def test_removing_every_task_leaves_a_note(self, tmp_path: Path) -> None:
        _write_matching_improvements_md(tmp_path, self._TASKS)
        _remove_from_improvements_md(tmp_path, ["AUTO-T1", "AUTO-T2", "AUTO-T3"])
        out = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T1:" not in out
        assert "### AUTO-T2:" not in out
        assert "### AUTO-T3:" not in out
        assert "All autonomous tasks originally planned here" in out
        assert "## Manual Suggestions" in out

    def test_manual_suggestions_section_never_touched(self, tmp_path: Path) -> None:
        ready = [ReadyTask(
            task_id="AUTO-T1",
            candidate=CandidateTask(
                title="Real task", instruction="do it", target_files=["a.py"],
                acceptance_check="true",
                cited_location=CitedLocation(file="a.py", symbol="foo"),
            ),
        )]
        manual = [CandidateTask(
            title="A manual suggestion", instruction="think about this",
            target_files=["b.py"], acceptance_check="",
            cited_location=CitedLocation(file="b.py", symbol="bar"),
        )]
        backlog = PrioritisedBacklog(auto_tasks=ready, manual_suggestions=manual)
        (tmp_path / IMPROVEMENTS_FILENAME).write_text(to_improvements_md(backlog), encoding="utf-8")

        _remove_from_improvements_md(tmp_path, ["AUTO-T1"])
        out = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T1:" not in out
        assert "A manual suggestion" in out

    def test_unknown_id_is_a_noop(self, tmp_path: Path) -> None:
        text = _write_matching_improvements_md(tmp_path, self._TASKS)
        _remove_from_improvements_md(tmp_path, ["AUTO-T-DOES-NOT-EXIST"])
        assert (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8") == text


# ─────────────────────────────────────────────────────────────────────────────
# AC-IDEMPOTENT — IMPROVEMENTS-FALSE.md writer
# ─────────────────────────────────────────────────────────────────────────────

class TestAppendFalsePositives:

    def test_creates_file_with_header(self, tmp_path: Path) -> None:
        r = RemovedTask(task_id="AUTO-T1", title="X", instruction="do X",
                         target_files=["a.py"], stage="presence", reason="already fixed")
        _append_false_positives(tmp_path, [r])
        text = (tmp_path / IMPROVEMENTS_FALSE_FILENAME).read_text(encoding="utf-8")
        assert "IMPROVEMENTS-FALSE.md" in text
        assert "### AUTO-T1: X" in text
        assert "already fixed" in text

    def test_second_call_does_not_duplicate(self, tmp_path: Path) -> None:
        r = RemovedTask(task_id="AUTO-T1", title="X", instruction="do X",
                         target_files=["a.py"], stage="presence", reason="already fixed")
        _append_false_positives(tmp_path, [r])
        _append_false_positives(tmp_path, [r])
        text = (tmp_path / IMPROVEMENTS_FALSE_FILENAME).read_text(encoding="utf-8")
        assert text.count("### AUTO-T1: X") == 1

    def test_appends_new_ids_alongside_existing(self, tmp_path: Path) -> None:
        r1 = RemovedTask(task_id="AUTO-T1", title="X", instruction="do X",
                          target_files=["a.py"], stage="presence", reason="already fixed")
        r2 = RemovedTask(task_id="AUTO-T2", title="Y", instruction="do Y",
                          target_files=["b.py"], stage="existence", reason="symbol gone")
        _append_false_positives(tmp_path, [r1])
        _append_false_positives(tmp_path, [r2])
        text = (tmp_path / IMPROVEMENTS_FALSE_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T1: X" in text
        assert "### AUTO-T2: Y" in text
        assert _already_recorded_ids(text) == {"AUTO-T1", "AUTO-T2"}


# ─────────────────────────────────────────────────────────────────────────────
# AC-NOPLAN
# ─────────────────────────────────────────────────────────────────────────────

class TestNoPlanYet:

    def test_validate_plan_raises_runtime_error(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        with pytest.raises(RuntimeError, match="No plan found"):
            validate_plan(tmp_path)

    def test_run_validate_prints_error_and_returns_1(self, tmp_path: Path, capsys) -> None:
        rc = run_validate(base_dir=tmp_path)
        assert rc == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "No plan found" in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# AC-VALIDATE — full integration
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatePlanIntegration:

    @pytest.fixture()
    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        return repo

    @pytest.fixture()
    def ini(self, tmp_path: Path) -> Path:
        return _write_ini(tmp_path)

    @pytest.fixture()
    def seeded(self, repo: Path):
        """Seed a plan with one real, one false-positive, one stale, one
        done, and one blocked task; also seed a matching IMPROVEMENTS.md, the
        way plan_emitter.emit() would have at real plan time (i.e. covering
        ALL five tasks — a done/blocked task still has its original snapshot
        entry, since IMPROVEMENTS.md is never re-rendered after plan time).
        Return (agent_dir, repo)."""
        tasks = [
            _task_kwargs(
                "AUTO-T-KEEP", title="Validate parse_config input",
                instruction="parse_config accepts anything with no validation — add a type check",
                symbol="parse_config",
            ),
            _task_kwargs(
                "AUTO-T-FALSEPOS", title="Add docstring to stable_func",
                instruction="FALSE_POSITIVE_MARKER stable_func has no docstring",
                symbol="stable_func",
            ),
            _task_kwargs(
                "AUTO-T-GONE", title="Fix a symbol that no longer exists",
                instruction="this cites a symbol removed from the file already",
                symbol="this_symbol_does_not_exist",
            ),
            _task_kwargs(
                "AUTO-T-DONE", title="Already-completed task",
                instruction="this was already done",
                symbol="parse_config", status=STATUS_DONE,
            ),
            _task_kwargs(
                "AUTO-T-BLOCKED", title="Blocked task",
                instruction="this is blocked",
                symbol="parse_config", status=STATUS_BLOCKED,
            ),
        ]
        agent_dir = repo / ".agent"
        _seed_plan(agent_dir, goal="harden error handling", base_dir=repo, tasks=tasks)
        _write_matching_improvements_md(repo, tasks)
        return agent_dir, repo

    def test_checked_only_counts_todo_and_in_progress(
        self, seeded, ini: Path,
    ) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            report = validate_plan(repo, config_path=str(ini))
        # KEEP + FALSEPOS + GONE = 3; DONE and BLOCKED are excluded.
        assert report.checked == 3

    def test_confirmed_task_is_kept(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            report = validate_plan(repo, config_path=str(ini))
        assert "AUTO-T-KEEP" in report.kept
        store = StateStore(agent_dir)
        store.initialise("harden error handling", repo)
        assert store.get_task("AUTO-T-KEEP") is not None
        assert store.get_task("AUTO-T-KEEP")["status"] == STATUS_TODO

    def test_false_positive_and_stale_are_removed(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            report = validate_plan(repo, config_path=str(ini))
        removed_ids = {r.task_id for r in report.removed}
        assert removed_ids == {"AUTO-T-FALSEPOS", "AUTO-T-GONE"}

        store = StateStore(agent_dir)
        store.initialise("harden error handling", repo)
        assert store.get_task("AUTO-T-FALSEPOS") is None
        assert store.get_task("AUTO-T-GONE") is None

    def test_removed_reasons_have_the_right_stage(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            report = validate_plan(repo, config_path=str(ini))
        by_id = {r.task_id: r for r in report.removed}
        assert by_id["AUTO-T-FALSEPOS"].stage == "presence"
        assert by_id["AUTO-T-GONE"].stage == "existence"

    def test_done_and_blocked_tasks_untouched(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            validate_plan(repo, config_path=str(ini))
        store = StateStore(agent_dir)
        store.initialise("harden error handling", repo)
        assert store.get_task("AUTO-T-DONE")["status"] == STATUS_DONE
        assert store.get_task("AUTO-T-BLOCKED")["status"] == STATUS_BLOCKED

    def test_false_positives_written_to_markdown(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            validate_plan(repo, config_path=str(ini))
        text = (repo / IMPROVEMENTS_FALSE_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T-FALSEPOS:" in text
        assert "### AUTO-T-GONE:" in text
        assert "### AUTO-T-KEEP:" not in text

    def test_false_positives_removed_from_improvements_md(self, seeded, ini: Path) -> None:
        """The gap this class exists to catch: a false positive must
        disappear from IMPROVEMENTS.md itself, not just get logged
        elsewhere — the two files must agree on what's still planned."""
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            validate_plan(repo, config_path=str(ini))
        text = (repo / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T-FALSEPOS:" not in text
        assert "### AUTO-T-GONE:" not in text

    def test_kept_and_out_of_scope_sections_survive_in_improvements_md(
        self, seeded, ini: Path,
    ) -> None:
        """AUTO-T-KEEP (re-confirmed) and AUTO-T-DONE/AUTO-T-BLOCKED (never
        sent through Gate 1 at all) must keep their original snapshot
        entries in IMPROVEMENTS.md untouched."""
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            validate_plan(repo, config_path=str(ini))
        text = (repo / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "### AUTO-T-KEEP:" in text
        assert "### AUTO-T-DONE:" in text
        assert "### AUTO-T-BLOCKED:" in text

    def test_commit_created_for_false_positives(self, seeded, ini: Path) -> None:
        agent_dir, repo = seeded
        before = _git_log(repo)
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            validate_plan(repo, config_path=str(ini))
        after = _git_log(repo)
        # A fresh repo also picks up GitManager's own one-time ".gitignore"
        # safety commit (see git_manager.ensure_gitignore_committed), so
        # assert on the newest commit's message rather than an exact count.
        assert len(after) > len(before)
        assert "AUTO-H1" in after[0]

        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert status.strip() == ""  # IMPROVEMENTS.md's edit was committed too

    def test_llm_called_only_for_surviving_existence_checks(
        self, seeded, ini: Path,
    ) -> None:
        """AUTO-T-GONE fails existence before any LLM call is made — only
        AUTO-T-KEEP and AUTO-T-FALSEPOS should reach Stage B."""
        agent_dir, repo = seeded
        with patch("tools.llm_stream.request_completion",
                   side_effect=_fake_gate1_llm) as mock_llm:
            validate_plan(repo, config_path=str(ini))
        assert mock_llm.call_count == 2


class TestValidatePlanNoPendingTasks:

    def test_all_done_returns_empty_report_without_llm_call(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path)
        agent_dir = repo / ".agent"
        _seed_plan(agent_dir, goal="g", base_dir=repo, tasks=[
            _task_kwargs("AUTO-T1", title="Done already", instruction="x",
                         status=STATUS_DONE),
        ])
        with patch("tools.llm_stream.request_completion") as mock_llm:
            report = validate_plan(repo, config_path=str(ini))
        assert report == type(report)(checked=0, kept=[], removed=[])
        mock_llm.assert_not_called()
        assert not (repo / IMPROVEMENTS_FALSE_FILENAME).exists()


# ─────────────────────────────────────────────────────────────────────────────
# AC-SKIPWARN — Gate 1 presence-check-disabled warning
#
# Regression coverage for a real incident: agents_32k.ini / agents_64k.ini /
# agents_128k.ini / agents_stub.ini all ship with [auto] task_mode = creative
# by default (unlike agents.ini / agents_4k.ini's task_mode = code). Running
# --validate-plan with one of those configs against an ordinary code plan
# silently skips Gate 1's entire LLM presence check (AUTO-CR-8's rule:
# task_mode != "code" -> existence-only) and returns "N/N confirmed" in well
# under a second — a result that reads exactly like a real check completed
# and found nothing, when in fact nothing was semantically checked at all.
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceCheckSkipWarning:

    def test_reason_empty_for_normal_code_config(self) -> None:
        cfg = configparser.ConfigParser()
        cfg.read_dict({"auto": {"task_mode": "code"}, "gate1": {"skip_llm": "false"}})
        assert _presence_check_skip_reason(cfg, "code") == ""

    def test_reason_reports_skip_llm(self) -> None:
        cfg = configparser.ConfigParser()
        cfg.read_dict({"gate1": {"skip_llm": "true"}})
        assert "skip_llm=true" in _presence_check_skip_reason(cfg, "code")

    def test_reason_reports_non_code_task_mode(self) -> None:
        cfg = configparser.ConfigParser()
        cfg.read_dict({"gate1": {"skip_llm": "false"}})
        reason = _presence_check_skip_reason(cfg, "creative")
        assert "creative" in reason

    @pytest.fixture()
    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        return repo

    def _seed_one_false_positive(self, repo: Path) -> None:
        _seed_plan(repo / ".agent", goal="g", base_dir=repo, tasks=[
            _task_kwargs(
                "AUTO-T1", title="Add docstring to stable_func",
                instruction="FALSE_POSITIVE_MARKER stable_func has no docstring",
                symbol="stable_func",
            ),
        ])

    def test_creative_mode_config_skips_presence_check_and_keeps_everything(
        self, repo: Path, tmp_path: Path, capsys,
    ) -> None:
        """This is the exact scenario from the field report: a config whose
        task_mode is 'creative' (agents_128k.ini's real default) must never
        call the LLM and must report the task as confirmed, even though the
        instruction is marked (via _fake_gate1_llm) as a case that WOULD be
        rejected if the presence check actually ran."""
        self._seed_one_false_positive(repo)
        ini = _write_ini(tmp_path, task_mode="creative")

        with patch("tools.llm_stream.request_completion",
                   side_effect=_fake_gate1_llm) as mock_llm:
            report = validate_plan(repo, config_path=str(ini))

        mock_llm.assert_not_called()
        assert report.checked == 1
        assert report.kept == ["AUTO-T1"]
        assert report.removed == []
        assert report.presence_check_skipped is True
        assert "creative" in report.presence_check_skip_reason

        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "Gate 1's LLM presence check is disabled" in out

    def test_skip_llm_true_also_skips_and_warns(
        self, repo: Path, tmp_path: Path, capsys,
    ) -> None:
        self._seed_one_false_positive(repo)
        ini = _write_ini(tmp_path, task_mode="code", skip_llm=True)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_fake_gate1_llm) as mock_llm:
            report = validate_plan(repo, config_path=str(ini))

        mock_llm.assert_not_called()
        assert report.removed == []
        assert report.presence_check_skipped is True
        assert "skip_llm=true" in report.presence_check_skip_reason
        assert "⚠️" in capsys.readouterr().out

    def test_normal_code_config_does_not_warn_and_does_call_llm(
        self, repo: Path, tmp_path: Path, capsys,
    ) -> None:
        self._seed_one_false_positive(repo)
        ini = _write_ini(tmp_path, task_mode="code", skip_llm=False)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_fake_gate1_llm) as mock_llm:
            report = validate_plan(repo, config_path=str(ini))

        mock_llm.assert_called()
        assert report.presence_check_skipped is False
        assert report.presence_check_skip_reason == ""
        assert [r.task_id for r in report.removed] == ["AUTO-T1"]
        assert "⚠️" not in capsys.readouterr().out

    def test_run_validate_summary_repeats_the_warning(
        self, repo: Path, tmp_path: Path, capsys,
    ) -> None:
        """The warning must also land in run_validate()'s final summary, not
        just the early print, so it isn't missed if scrolled past."""
        self._seed_one_false_positive(repo)
        ini = _write_ini(tmp_path, task_mode="creative")

        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            rc = run_validate(base_dir=repo, config_path=str(ini))

        assert rc == 0
        out = capsys.readouterr().out
        assert out.count("⚠️") >= 2  # early warning + summary note
        assert "confirmed above" in out or "not that anything was" in out


# ─────────────────────────────────────────────────────────────────────────────
# AC-EXITCODE — run_validate() console summary
# ─────────────────────────────────────────────────────────────────────────────

class TestRunValidateExitCodes:

    def test_success_with_removals_returns_0_and_summarises(
        self, tmp_path: Path, capsys,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path)
        agent_dir = repo / ".agent"
        _seed_plan(agent_dir, goal="g", base_dir=repo, tasks=[
            _task_kwargs("AUTO-T1", title="Already fixed",
                         instruction="FALSE_POSITIVE_MARKER already fixed",
                         symbol="stable_func"),
        ])
        with patch("tools.llm_stream.request_completion", side_effect=_fake_gate1_llm):
            rc = run_validate(base_dir=repo, config_path=str(ini))
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 removed as false positive" in out
        assert IMPROVEMENTS_FALSE_FILENAME in out

    def test_nothing_to_check_returns_0(self, tmp_path: Path, capsys) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path)
        agent_dir = repo / ".agent"
        _seed_plan(agent_dir, goal="g", base_dir=repo, tasks=[
            _task_kwargs("AUTO-T1", title="Done", instruction="x", status=STATUS_DONE),
        ])
        rc = run_validate(base_dir=repo, config_path=str(ini))
        assert rc == 0
        assert "nothing to do" in capsys.readouterr().out.lower()


# ─────────────────────────────────────────────────────────────────────────────
# AC-CLI — argparse + main.py dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIWiring:

    def test_argparser_accepts_validate_plan(self) -> None:
        old_argv = sys.argv[:]
        try:
            sys.argv = ["main.py", "--validate-plan", "--base", "/tmp/x"]
            import main as main_mod
            args = main_mod._parse_args()
            assert args.validate_plan is True
            assert args.auto is None
        finally:
            sys.argv = old_argv

    def test_validate_plan_defaults_to_false(self) -> None:
        old_argv = sys.argv[:]
        try:
            sys.argv = ["main.py", "--auto", "improve", "--dry-run"]
            import main as main_mod
            args = main_mod._parse_args()
            assert args.validate_plan is False
        finally:
            sys.argv = old_argv

    def test_main_dispatches_to_run_validate(self, tmp_path: Path) -> None:
        import main as main_mod

        captured = {}

        def _fake_run_validate(*, base_dir, config_path):
            captured["base_dir"] = base_dir
            captured["config_path"] = config_path
            return 0

        argv = ["main.py", "--validate-plan", "--base", str(tmp_path)]
        with patch("tools.auto.plan_validator.run_validate", _fake_run_validate):
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()
                assert exc_info.value.code == 0
        assert captured["base_dir"] == str(tmp_path)

    def test_validate_plan_flag_short_circuits_before_auto(self, tmp_path: Path) -> None:
        """If both --validate-plan and --auto are somehow passed, validate
        wins and run_auto is never reached (mirrors --collect/--faq's
        early-exit style in main())."""
        import main as main_mod

        argv = ["main.py", "--validate-plan", "--auto", "improve", "--base", str(tmp_path)]
        with patch("tools.auto.plan_validator.run_validate", return_value=0) as mock_validate:
            with patch("tools.auto.controller.run_auto") as mock_auto:
                with patch.object(sys, "argv", argv):
                    with pytest.raises(SystemExit):
                        main_mod.main()
        mock_validate.assert_called_once()
        mock_auto.assert_not_called()
