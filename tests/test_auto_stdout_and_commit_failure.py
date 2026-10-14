"""tests/test_auto_stdout_and_commit_failure.py — HARDEN-2 (B1, B2, B3, B4).

One file, four defects, each with its own class:

B1 — a closed stdout turned a received answer into a "transport failure".
     ``Coder.generate``'s token callback and closing ``print`` sat inside the
     ``try`` whose ``except Exception`` means *"the LLM call failed"*, so a
     ``| head`` / cron closed pipe discarded a whole paid-for reply and the
     retry layer paid for it again. The Architect's streaming callback had the
     same shape, where one broken write dropped a batch out of the plan.

B2 — a task whose commit failed was counted done and its work stayed in the
     tree. ``commit`` returned ``None`` both for "nothing staged" (a quiet
     success) and for a ``GitError``; the controller then did ``tasks_done += 1``
     and ran the regression check on a tree that was never committed, while the
     status file still said the task was open.

B3 — the adaptive search blacklisted every file it scanned.
     ``already_searched`` accumulated *files*, so iteration 2 could only look in
     files it had never opened — a definition sitting beside an earlier one
     became unreachable and a reference found before regressed to missing.

B4 — a creative run's own ``synopsis.md`` / ``story_bible.md`` refused the next
     ``--auto``. They are written *after* the task commit and nothing committed
     them, so the next start's ``user_changes()`` reported them as the user's
     own uncommitted changes and raised ``DirtyTreeError``.

All offline: no live LLM, no provider config. ``sys.stdout`` is swapped for a
writer that raises, and git runs inside a tmp repo.
"""

from __future__ import annotations

import configparser
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import ClusterReviewer
from tools.auto.coder import Coder
from tools.auto.commit_on_success import CommitOnSuccess
from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import GitError, GitManager, make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import StateStore, STATUS_DONE, STATUS_TODO, make_task
from tools.search_agent import SearchAgent

# The (file, ref) blacklist normaliser (B3). Imported defensively so a missing
# change fails its assertions rather than the whole module's import.
try:
    from tools.search_agent import _normalise_already_searched
except ImportError:  # pragma: no cover — the change is absent
    def _normalise_already_searched(already_searched):
        return set(already_searched or []), set()

try:
    from tools.ui import safe_stdout_write
except ImportError:  # pragma: no cover — the change is absent
    def safe_stdout_write(t: str) -> None:
        sys.stdout.write(t)
        sys.stdout.flush()

# The B2 vocabulary. Imported defensively so that, without the change, these
# tests FAIL on their assertions instead of failing to import: a reader must see
# which behaviour is broken, not an ImportError.
try:
    from tools.auto.commit_on_success import (
        COMMIT_GIT_ERROR,
        COMMIT_NOTHING_TO_COMMIT,
        COMMIT_OK,
        CommitResult,
    )
except ImportError:  # pragma: no cover — the change is absent
    COMMIT_GIT_ERROR = "\0git_error"
    COMMIT_NOTHING_TO_COMMIT = "\0nothing_to_commit"
    COMMIT_OK = "\0committed"

    class CommitResult:  # type: ignore[no-redef]
        def __init__(self, sha=None, outcome="", error=""):
            self.sha = sha
            self.outcome = outcome
            self.error = error

_AGENT_USER  = "auto-agent-test"
_AGENT_EMAIL = "auto-agent-test@localhost"


# ── shared fakes ─────────────────────────────────────────────────────────────

class _BrokenStdout(io.StringIO):
    """A stdout whose every write raises BrokenPipeError — a closed pipe."""

    def write(self, s: str) -> int:
        raise BrokenPipeError("pipe closed")

    def flush(self) -> None:
        pass


class _BrokenPipeStream:
    """A fake ``request_completion`` that streams tokens into *on_token* and
    then raises, mimicking a transport that fails *after* the first token."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        on_token = kwargs.get("on_token")
        if on_token is not None:
            for tok in self._tokens:
                on_token(tok)
        raise RuntimeError("simulated transport failure after streaming")


def _stream_into_broken_stdout(answer: str):
    """A fake ``request_completion`` that closes stdout *during* the stream.

    Mirrors the real failure mode (the repo's own
    ``tests_bugfix/test_bugfix_brokenpipe_improvement_validator.py`` does the
    same): the pipe closes while tokens are being delivered, so the callback's
    write raises. The banners before the call still print normally.
    """
    def _fake(*args, **kwargs):
        on_token = kwargs.get("on_token")
        if on_token is not None:
            real = sys.stdout
            sys.stdout = _BrokenStdout()
            try:
                on_token(answer)   # BrokenPipeError fires here
            finally:
                sys.stdout = real
        return answer
    return _fake


# ── B1 ───────────────────────────────────────────────────────────────────────

class TestB1ClosedStdout:
    """A write failure must never be classified as a transport failure."""

    def _config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
                "api_format": "openai",
            },
            "loop": {"timeout_seconds": "300"},
            "coder": {"temperature": "0.2", "max_tokens": "4096"},
        })
        return cfg

    def _coder(self) -> Coder:
        return Coder(
            config=self._config(),
            base_url="http://localhost:1337/v1",
            api_key="test",
            model="test-model",
        )

    def _task(self, base_dir: Path) -> dict:
        target = base_dir / "tools" / "module.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def parse(x): return None\n")
        return {
            "id": "AUTO-B1",
            "title": "Fix parse",
            "instruction": "Make parse return x.",
            "target_files": ["tools/module.py"],
            "acceptance_check": "true",
            "cited_locations": [
                {"file": "tools/module.py", "symbol": "parse",
                 "line_start": 1, "line_end": 1}
            ],
        }

    def _streaming_response(self) -> str:
        return json.dumps(
            {"files": [{"path": "tools/module.py",
                        "content": "def parse(x): return x\n"}]}
        )

    def test_coder_generate_survives_closed_stdout(self, tmp_path: Path):
        """The core B1 case: stdout is a closed pipe for the WHOLE call.

        The token callback and the closing ``print`` both raise, yet the reply
        must still be parsed and written, with no transport error recorded.
        """
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = self._coder()
        task = self._task(base_dir)

        response = json.dumps(
            {"files": [{"path": "tools/module.py", "content": "x = 1\n"}]}
        )

        with patch("tools.llm_stream.request_completion",
                   side_effect=_stream_into_broken_stdout(response)):
            r = coder.generate(task, base_dir)

        assert r.error_kind != "transport", (
            f"a closed stdout must not be a transport failure: {r.error!r}"
        )
        assert not r.error
        assert r.files_written == ["tools/module.py"]
        assert (base_dir / "tools" / "module.py").read_text() == "x = 1\n"

    def test_coder_generate_survives_whole_call_closed_stdout(self, tmp_path: Path):
        """The harder case: stdout is closed for the ENTIRE call, banners
        included. Every write inside the try must still be guarded."""
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = self._coder()
        task = self._task(base_dir)

        response = json.dumps(
            {"files": [{"path": "tools/module.py", "content": "x = 1\n"}]}
        )

        with patch("tools.llm_stream.request_completion",
                   return_value=response), \
             patch("tools.auto.coder.sys.stdout", _BrokenStdout()):
            r = coder.generate(task, base_dir)

        assert r.error_kind != "transport"
        assert not r.error
        assert r.files_written == ["tools/module.py"]

    def test_coder_transport_failure_still_reported(self, tmp_path: Path):
        """The guard must not swallow a REAL transport failure.

        A stream that dies mid-reply is still ``error_kind == "transport"``:
        the fix removes the false positive, not the true one.
        """
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = self._coder()
        task = self._task(base_dir)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_BrokenPipeStream(['{"files":'])), \
             patch("tools.auto.coder.sys.stdout", _BrokenStdout()):
            r = coder.generate(task, base_dir)

        assert r.error_kind == "transport"
        assert "simulated transport failure" in r.error
        assert r.files_written == []

    def test_architect_callback_survives_closed_stdout(self, tmp_path: Path):
        """Same defect in the Architect: a closed stdout must not drop a batch."""
        base = tmp_path / "repo"
        (base / "tools").mkdir(parents=True)
        (base / "tools" / "module.py").write_text("def parse(x): return None\n")

        cfg = self._config()
        cfg["architect"] = {"retry_delays_sec": "0"}
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost:1337/v1",
            api_key="test",
            model="test-model",
        )
        from tools.auto.repo_ingest import RepoCluster
        cluster = RepoCluster(
            name="cluster-a", patterns=["*.py"], files=["tools/module.py"],
        )

        candidates_json = json.dumps([
            {"title": "Fix parse", "instruction": "return x",
             "cited_location": {"file": "tools/module.py",
                                "symbol": "parse",
                                "line_start": 1, "line_end": 1},
             "target_files": ["tools/module.py"],
             "acceptance_check": "true"}
        ])

        def fake_completion(*args, **kwargs):
            on_token = kwargs.get("on_token")
            if on_token is not None:
                on_token("[")
                on_token(candidates_json)
                on_token("]")
            return candidates_json

        with patch("tools.llm_stream.request_completion",
                   side_effect=_stream_into_broken_stdout(candidates_json)):
            candidates = reviewer.review_clusters([cluster], str(base))

        assert len(candidates) == 1, (
            "a closed stdout must not drop the batch out of the plan"
        )
        assert candidates[0].title == "Fix parse"

    def test_safe_stdout_write_swallows_broken_pipe(self):
        """The shared helper itself: neither BrokenPipeError nor OSError escapes."""
        real = sys.stdout
        sys.stdout = _BrokenStdout()
        try:
            safe_stdout_write("token")          # must not raise
        finally:
            sys.stdout = real

        class _OSErrStdout(io.StringIO):
            def write(self, s: str) -> int:
                raise OSError("errno 32")

            def flush(self) -> None:
                pass

        real = sys.stdout
        sys.stdout = _OSErrStdout()
        try:
            safe_stdout_write("token")          # must not raise
        finally:
            sys.stdout = real

    def test_agents_share_one_helper(self):
        """The three agents use ONE implementation, not three copies."""
        import tools.improvement_agent as ia
        import tools.validator_agent as va

        assert ia._safe_stdout_write is safe_stdout_write
        assert va._safe_stdout_write is safe_stdout_write


# ── B2 ───────────────────────────────────────────────────────────────────────

class _FakeCommitGitManager:
    """A GitManager stand-in whose ``commit_task`` is scripted (B2)."""

    def __init__(self, *, sha: str | None = None, raise_git_error: bool = False):
        self._sha = sha
        self._raise = raise_git_error
        self.commit_calls: list[tuple[str, str]] = []

    def commit_task(self, task_id: str, title: str) -> str | None:
        self.commit_calls.append((task_id, title))
        if self._raise:
            raise GitError("simulated git failure")
        return self._sha

    def diff_working_tree(self) -> str:
        return "diff --git a/x.py b/x.py\n"

    def discard_working_changes(self) -> None:
        pass


def _state(tmp_path: Path, task_ids: list[str]) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("test goal", tmp_path)
    for tid in task_ids:
        st.upsert_task(
            make_task(id=tid, title=f"Task {tid}", instruction="do it",
                      target_files=["main.py"])
        )
    return st


TASK = {
    "id": "AUTO-T1",
    "title": "Fix off-by-one",
    "instruction": "do it",
    "target_files": ["main.py"],
    "acceptance_check": "pytest -q",
}


class TestB2CommitFailureAccounting:
    def test_git_error_reports_git_error_outcome(self, tmp_path):
        """The two Nones are now told apart."""
        st = _state(tmp_path, ["AUTO-T1"])
        gm = _FakeCommitGitManager(raise_git_error=True)
        cos = CommitOnSuccess(gm, st)

        res = cos.commit_result(TASK)
        assert res.sha is None
        assert res.outcome == COMMIT_GIT_ERROR
        assert res.git_error is True

    def test_nothing_staged_reports_its_own_outcome(self, tmp_path):
        """'Nothing staged' stays a quiet success — and is now distinguishable."""
        st = _state(tmp_path, ["AUTO-T1"])
        gm = _FakeCommitGitManager(sha=None)
        cos = CommitOnSuccess(gm, st)

        res = cos.commit_result(TASK)
        assert res.sha is None
        assert res.outcome == COMMIT_NOTHING_TO_COMMIT
        assert res.git_error is False

    def test_committed_reports_ok(self, tmp_path):
        st = _state(tmp_path, ["AUTO-T1"])
        gm = _FakeCommitGitManager(sha="a" * 40)
        res = CommitOnSuccess(gm, st).commit_result(TASK)
        assert res.sha == "a" * 40
        assert res.outcome == COMMIT_OK
        assert res.committed is True

    def test_commit_sha_contract_unchanged(self, tmp_path):
        """The existing ``commit()`` surface is untouched for every caller."""
        st = _state(tmp_path, ["AUTO-T1"])
        gm = _FakeCommitGitManager(raise_git_error=True)
        cos = CommitOnSuccess(gm, st)
        assert cos.commit(TASK) is None            # must not raise, must be None
        assert st.get_task("AUTO-T1")["status"] == STATUS_TODO


class TestB2ControllerAccounting:
    """The controller's own accounting on a failed commit."""

    def _controller(self, tmp_path: Path, task_ids: list[str]) -> AutoController:
        base = tmp_path / "repo"
        base.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=base, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", _AGENT_USER],
                       cwd=base, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", _AGENT_EMAIL],
                       cwd=base, check=True, capture_output=True)
        (base / "README.md").write_text("# repo\n")
        subprocess.run(["git", "add", "."], cwd=base, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=base,
                       check=True, capture_output=True)

        ctrl = AutoController.__new__(AutoController)
        ctrl.goal = "test"
        ctrl.base_dir = base
        ctrl.config_path = "agents.ini"
        ctrl.agent_dir = base / ".agent"
        ctrl.workspace_dir = ctrl.agent_dir / "workspace"
        import time
        ctrl._time_fn = time.monotonic
        ctrl._start_time = time.monotonic()
        ctrl.limits = RunLimits()
        ctrl.state = _state(base, task_ids)
        ctrl.git = make_git_manager(base, _git_config())
        ctrl.run_trace = MagicMock()
        ctrl.progress_display = MagicMock()
        ctrl.metrics_stream = MagicMock()
        ctrl.auto_tuner = MagicMock()
        ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
            promoted=False, new_prompt_score=0.0
        )
        return ctrl

    def _passed_result(self, task_id: str) -> OuterLoopResult:
        return OuterLoopResult(
            task_id=task_id, passed=True, rounds_used=1, exhausted=False,
            feedback_files=[],
            inner_results=[SimpleNamespace(attempts_used=1, last_feedback="")],
        )

    def test_failed_commit_not_counted_done_and_patch_saved(self, tmp_path):
        """B2's core: tasks_done must not grow, the status must not be DONE, and
        the task's diff must be on disk before anything resets the tree."""
        ctrl = self._controller(tmp_path, ["T-COMMITFAIL"])
        task = ctrl.state.get_task("T-COMMITFAIL")

        def run_task_writes(t, base_dir):
            # Real uncommitted work, so the patch has content to capture.
            (Path(base_dir) / "work.py").write_text("x = 1\n")
            return self._passed_result("T-COMMITFAIL")

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_writes

        with patch("tools.auto.outer_loop.make_outer_loop",
                   return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
             patch.object(ctrl.git, "commit_task",
                          side_effect=GitError("simulated git failure")):
            _stop_reason, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 0, (
            "a task whose commit failed must not be counted done"
        )
        t = ctrl.state.get_task("T-COMMITFAIL")
        assert t["status"] != STATUS_DONE
        assert t["status"] == STATUS_TODO

        patch_path = ctrl.agent_dir / "uncommitted-T-COMMITFAIL.patch"
        assert patch_path.exists(), "the task's diff must be saved as a patch"
        content = patch_path.read_text()
        assert "work.py" in content and "x = 1" in content

        # A patch that does not apply is not a saved patch: verify it against a
        # pristine clone of the repo.
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(ctrl.base_dir), str(clone)],
                       check=True, capture_output=True)
        applied = subprocess.run(["git", "apply", str(patch_path)], cwd=clone,
                                 capture_output=True, text=True)
        assert applied.returncode == 0, (
            f"the saved patch must apply to a clean tree: {applied.stderr}"
        )
        assert (clone / "work.py").read_text() == "x = 1\n"

    def test_nothing_staged_still_counted_done(self, tmp_path):
        """'Nothing staged' keeps today's quiet success — unchanged behaviour."""
        ctrl = self._controller(tmp_path, ["T-NOSTAGE"])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = self._passed_result("T-NOSTAGE")

        with patch("tools.auto.outer_loop.make_outer_loop",
                   return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            _stop_reason, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 1
        assert ctrl.state.get_task("T-NOSTAGE")["status"] == STATUS_DONE

    def test_successful_commit_still_counted_done(self, tmp_path):
        """The guard must not cost a task its DONE when the commit works."""
        ctrl = self._controller(tmp_path, ["T-OK"])

        def run_task_writes(t, base_dir):
            (Path(base_dir) / "ok.py").write_text("y = 2\n")
            return self._passed_result("T-OK")

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_writes

        with patch("tools.auto.outer_loop.make_outer_loop",
                   return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            _stop_reason, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 1
        assert ctrl.state.get_task("T-OK")["status"] == STATUS_DONE

    def test_legacy_commit_helper_without_commit_result(self, tmp_path):
        """Fail-open: a commit helper that predates the classification (a test
        double) still counts its task done rather than dropping it."""
        ctrl = self._controller(tmp_path, ["T-LEGACY"])

        class _LegacyHelper:
            def __init__(self):
                self.calls = 0

            def commit(self, task, result):
                self.calls += 1
                return "b" * 40

        helper = _LegacyHelper()
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = self._passed_result("T-LEGACY")

        with patch("tools.auto.outer_loop.make_outer_loop",
                   return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=helper):
            _stop_reason, tasks_done = ctrl._run_task_loop()

        assert helper.calls == 1
        assert tasks_done == 1


def _git_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": _AGENT_USER, "git_email": _AGENT_EMAIL}
    return cfg


# ── B3 ───────────────────────────────────────────────────────────────────────

def _search_agent() -> SearchAgent:
    """A SearchAgent with no LLM filter, so every hit is approved."""
    return SearchAgent()


class TestB3SearchBlacklist:
    def test_normalise_accepts_both_shapes(self):
        files, pairs = _normalise_already_searched([
            "legacy_file.py",                       # legacy: whole file
            ("f.py", "a"),                          # new: one (file, ref)
            ["g.py", "b"],                          # JSON round-trip shape
            ("only-one",),                          # malformed: ignored
            42,                                     # malformed: ignored
        ])
        assert files == {"legacy_file.py"}
        assert pairs == {("f.py", "a"), ("g.py", "b")}

    def test_normalise_never_raises(self):
        assert _normalise_already_searched(None) == (set(), set())
        assert _normalise_already_searched("not-a-list") == (set(), set())

    def test_second_iteration_finds_new_name_in_a_scanned_file(self, tmp_path):
        """B3's core: ref ``a`` is unfound in f.py on iteration 1; ref ``b`` is
        defined in f.py and must still be found on iteration 2."""
        base = tmp_path / "repo"
        base.mkdir()
        (base / "f.py").write_text("def a():\n    pass\n\ndef b():\n    pass\n")

        agent = _search_agent()

        # Iteration 1: ask for "a", which IS in f.py, then remove it so the
        # second iteration genuinely has to search a different name.
        first = agent.run(references=["a"], base_dir=str(base))
        assert "a" in first["found"]

        # What the loop now records: (file, ref) pairs, not files.
        assert first["searched_files"] == [(str(base / "f.py"), "a")]

        # Iteration 2: a NEW name, defined in the same file.
        second = agent.run(
            references=["b"], base_dir=str(base),
            already_searched=first["searched_files"],
        )
        assert "b" in second["found"], (
            "a name the validator asks for next must still be found in a file "
            "the previous iteration scanned for a different name"
        )

    def test_second_iteration_does_not_re_scan_same_pair(self, tmp_path):
        """The (file, ref) pair is still skipped, so the blacklist keeps its job."""
        base = tmp_path / "repo"
        base.mkdir()
        (base / "f.py").write_text("def a():\n    pass\n")

        agent = _search_agent()
        first = agent.run(references=["a"], base_dir=str(base))
        assert "a" in first["found"]

        second = agent.run(
            references=["a"], base_dir=str(base),
            already_searched=first["searched_files"],
        )
        # The pair is blacklisted, so nothing was looked at again.
        assert second["searched_files"] == []
        assert "a" in second["not_found"]

    def test_legacy_string_entries_keep_whole_file_meaning(self, tmp_path):
        """A resume checkpoint from an older run holds bare file names; those
        must keep skipping the whole file, exactly as before."""
        base = tmp_path / "repo"
        base.mkdir()
        (base / "f.py").write_text("def a():\n    pass\n")

        agent = _search_agent()
        res = agent.run(
            references=["a"], base_dir=str(base),
            already_searched=[str(base / "f.py")],
        )
        assert res["searched_files"] == []
        assert "a" in res["not_found"]

    def test_malformed_entry_does_not_break_the_search(self, tmp_path):
        """Fail-open: a junk entry costs a redundant read, never a failure."""
        base = tmp_path / "repo"
        base.mkdir()
        (base / "f.py").write_text("def a():\n    pass\n")

        agent = _search_agent()
        res = agent.run(
            references=["a"], base_dir=str(base),
            already_searched=[("incomplete",), 42, None],
        )
        assert "a" in res["found"]


# ── B4 ───────────────────────────────────────────────────────────────────────

class TestB4CreativeOwnOutputs:
    def _repo(self, tmp_path: Path) -> Path:
        base = tmp_path / "repo"
        base.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=base, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", _AGENT_USER],
                       cwd=base, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", _AGENT_EMAIL],
                       cwd=base, check=True, capture_output=True)
        (base / "README.md").write_text("# repo\n")
        subprocess.run(["git", "add", "."], cwd=base, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=base,
                       check=True, capture_output=True)
        return base

    def _creative_config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        cfg.read_dict({
            "auto": {"task_mode": "creative", "git_user": _AGENT_USER,
                     "git_email": _AGENT_EMAIL},
        })
        return cfg

    def test_own_outputs_are_named_from_the_objects(self, tmp_path):
        """The names come from SummaryMemory/StoryBible, not from a literal."""
        base = self._repo(tmp_path)
        gm = GitManager(base, self._creative_config())
        outputs = gm._creative_outputs()
        assert "synopsis.md" in outputs
        assert "story_bible.md" in outputs
        assert "story_bible.md.pending.json" in outputs, (
            "the pending-corrections sidecar is written by the same run"
        )

    def test_user_changes_excludes_own_outputs(self, tmp_path):
        """The ticket's own repro: after a creative run the three files are
        untracked; user_changes() must not report them."""
        base = self._repo(tmp_path)
        for name in ("synopsis.md", "story_bible.md",
                     "story_bible.md.pending.json"):
            (base / name).write_text("content\n")
        (base / "notes.md").write_text("the user's own file\n")

        gm = GitManager(base, self._creative_config())
        assert gm.user_changes() == ["notes.md"], (
            "only the user's own file must be reported; the agent's own "
            "continuity files are its output, not the user's changes"
        )

    def test_code_mode_reports_them(self, tmp_path):
        """Outside creative mode nothing is written, so the names must NOT be
        excluded — a user's own synopsis.md is their change."""
        base = self._repo(tmp_path)
        (base / "synopsis.md").write_text("mine\n")

        gm = GitManager(base, configparser.ConfigParser())
        assert "synopsis.md" in gm.user_changes()

    def test_second_auto_start_is_not_refused(self, tmp_path):
        """The end-to-end consequence: _setup_git must not raise DirtyTreeError."""
        base = self._repo(tmp_path)
        for name in ("synopsis.md", "story_bible.md",
                     "story_bible.md.pending.json"):
            (base / name).write_text("content\n")

        ctrl = AutoController.__new__(AutoController)
        ctrl.base_dir = base
        ctrl.agent_dir = base / ".agent"
        ctrl.workspace_dir = ctrl.agent_dir / "workspace"
        ctrl.config_path = "agents.ini"
        ctrl.config = self._creative_config()
        ctrl.state = _state(base, ["T-1"])
        ctrl.git = None

        # _setup_git raises DirtyTreeError when user_changes() is non-empty.
        ctrl._setup_git()

        assert ctrl.git is not None, "the second run must start, not refuse"

    def test_malformed_task_mode_degrades_safely(self, tmp_path):
        """A malformed [auto] task_mode must not raise out of _setup_git."""
        base = self._repo(tmp_path)
        cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        cfg.read_dict({"auto": {"task_mode": "creativy"}})
        gm = GitManager(base, cfg)
        assert gm.task_mode == "creative"     # normalised, not a crash

        cfg2 = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        cfg2.read_dict({"auto": {"task_mode": "???"}})
        gm2 = GitManager(base, cfg2)
        assert gm2.task_mode == "code"
        assert gm2._creative_outputs() == ()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
