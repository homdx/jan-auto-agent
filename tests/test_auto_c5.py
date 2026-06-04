"""tests/test_auto_c5.py — AUTO-C5: commit on success.

ACs (from the Jira story):
  * One commit per validated task; commit hash recorded in plan.json.
  * Commit message follows auto(<task-id>): <title>.
  * Task status is set to DONE with the commit hash in plan.json.
  * If GitManager.commit_task returns None (nothing staged), task is still
    marked DONE with commit="" (empty string sentinel).
  * A GitError does NOT raise out of CommitOnSuccess.commit(); it returns None
    and leaves the task status unchanged.
  * make_commit_on_success factory wires GitManager + StateStore correctly.
"""

import sys
from pathlib import Path
from typing import Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.commit_on_success import CommitOnSuccess, make_commit_on_success
from tools.auto.git_manager import GitError
from tools.auto.state import StateStore, make_task, STATUS_DONE, STATUS_TODO


# ── helpers / fakes ──────────────────────────────────────────────────────────

class FakeGitManager:
    """Minimal GitManager stand-in.  Returns scripted commit hashes."""

    def __init__(self, hashes: list[Optional[str]], *, raise_git_error: bool = False):
        self._hashes = list(hashes)
        self._raise  = raise_git_error
        self.commit_calls: list[tuple[str, str]] = []   # (task_id, title) pairs

    # replicate only the surface CommitOnSuccess uses
    def commit_task(self, task_id: str, title: str) -> Optional[str]:
        self.commit_calls.append((task_id, title))
        if self._raise:
            raise GitError("simulated git failure")
        return self._hashes.pop(0) if self._hashes else None

    # ensure_repo / configure_identity are called by make_git_manager, not C5
    def ensure_repo(self) -> bool:  # pragma: no cover
        return False

    def configure_identity(self) -> None:  # pragma: no cover
        pass


def _state(tmp_path: Path) -> StateStore:
    """Return a StateStore with one todo task AUTO-T1."""
    st = StateStore(tmp_path / ".agent")
    st.initialise("test goal", tmp_path)
    st.upsert_task(
        make_task(id="AUTO-T1", title="Fix off-by-one", instruction="do it",
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


# ── happy path ───────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_returns_commit_hash(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager(["abc123def456" + "0" * 28])
        cos = CommitOnSuccess(gm, st)
        sha = cos.commit(TASK)
        assert sha == "abc123def456" + "0" * 28

    def test_task_marked_done(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager(["deadbeef" + "0" * 32])
        CommitOnSuccess(gm, st).commit(TASK)
        assert st.get_task("AUTO-T1")["status"] == STATUS_DONE

    def test_commit_hash_recorded_in_plan(self, tmp_path):
        sha_full = "cafebabe" + "f" * 32
        st  = _state(tmp_path)
        gm  = FakeGitManager([sha_full])
        CommitOnSuccess(gm, st).commit(TASK)
        assert st.get_task("AUTO-T1").get("commit") == sha_full

    def test_commit_message_format(self, tmp_path):
        """GitManager.commit_task must be called with (task_id, title)."""
        st  = _state(tmp_path)
        gm  = FakeGitManager(["1" * 40])
        CommitOnSuccess(gm, st).commit(TASK)
        assert gm.commit_calls == [("AUTO-T1", "Fix off-by-one")]

    def test_log_written_on_success(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager(["abcdef" + "0" * 34])
        CommitOnSuccess(gm, st).commit(TASK)
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "AUTO-T1" in log and "DONE" in log

    def test_outer_result_param_accepted(self, tmp_path):
        """outer_result may be None or any object; C5 must not crash."""
        st  = _state(tmp_path)
        gm  = FakeGitManager(["1" * 40])
        sha = CommitOnSuccess(gm, st).commit(TASK, outer_result=object())
        assert sha is not None


# ── nothing staged ────────────────────────────────────────────────────────────

class TestNothingStaged:
    def test_returns_none_when_nothing_staged(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager([None])   # commit_task returns None → nothing staged
        sha = CommitOnSuccess(gm, st).commit(TASK)
        assert sha is None

    def test_task_still_marked_done_when_nothing_staged(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager([None])
        CommitOnSuccess(gm, st).commit(TASK)
        assert st.get_task("AUTO-T1")["status"] == STATUS_DONE

    def test_commit_field_empty_string_when_nothing_staged(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager([None])
        CommitOnSuccess(gm, st).commit(TASK)
        assert st.get_task("AUTO-T1").get("commit") == ""

    def test_warning_logged_when_nothing_staged(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager([None])
        CommitOnSuccess(gm, st).commit(TASK)
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "nothing staged" in log.lower() or "no new commit" in log.lower()


# ── git error ────────────────────────────────────────────────────────────────

class TestGitError:
    def test_git_error_does_not_raise(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager([], raise_git_error=True)
        sha = CommitOnSuccess(gm, st).commit(TASK)   # must not raise
        assert sha is None

    def test_task_status_unchanged_on_git_error(self, tmp_path):
        """A GitError must leave the task in its prior status (not DONE)."""
        st  = _state(tmp_path)
        gm  = FakeGitManager([], raise_git_error=True)
        CommitOnSuccess(gm, st).commit(TASK)
        assert st.get_task("AUTO-T1")["status"] == STATUS_TODO


# ── one commit per task ───────────────────────────────────────────────────────

class TestOneCommitPerTask:
    def test_commit_called_exactly_once(self, tmp_path):
        st  = _state(tmp_path)
        gm  = FakeGitManager(["a" * 40])
        CommitOnSuccess(gm, st).commit(TASK)
        assert len(gm.commit_calls) == 1

    def test_two_tasks_two_commits(self, tmp_path):
        st = StateStore(tmp_path / ".agent")
        st.initialise("goal", tmp_path)
        for tid, ttitle in [("AUTO-T1", "Task one"), ("AUTO-T2", "Task two")]:
            st.upsert_task(make_task(id=tid, title=ttitle, instruction="x",
                                     target_files=["f.py"]))

        gm  = FakeGitManager(["a" * 40, "b" * 40])
        cos = CommitOnSuccess(gm, st)

        cos.commit({"id": "AUTO-T1", "title": "Task one"})
        cos.commit({"id": "AUTO-T2", "title": "Task two"})

        assert gm.commit_calls == [
            ("AUTO-T1", "Task one"),
            ("AUTO-T2", "Task two"),
        ]
        assert st.get_task("AUTO-T1")["status"] == STATUS_DONE
        assert st.get_task("AUTO-T2")["status"] == STATUS_DONE
        assert st.get_task("AUTO-T1")["commit"] == "a" * 40
        assert st.get_task("AUTO-T2")["commit"] == "b" * 40


# ── factory ───────────────────────────────────────────────────────────────────

class TestFactory:
    def test_make_commit_on_success_returns_instance(self, tmp_path):
        import configparser

        # Set up a bare git repo so make_git_manager can init/configure it
        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)

        st  = _state(tmp_path)
        cfg = configparser.ConfigParser()
        cfg["auto"] = {"git_user": "bot", "git_email": "bot@test.local"}

        cos = make_commit_on_success(cfg, repo, st)
        assert isinstance(cos, CommitOnSuccess)

    def test_factory_commits_correctly(self, tmp_path, monkeypatch):
        """Factory-built CommitOnSuccess uses GitManager to commit."""
        import configparser
        import tools.auto.commit_on_success as c5_mod

        captured: list[tuple[str, str]] = []

        class _FakeGM(FakeGitManager):
            def __init__(self):
                super().__init__(["deadf00d" + "0" * 32])
            def ensure_repo(self): return False
            def configure_identity(self): pass

        fake_gm = _FakeGM()
        monkeypatch.setattr(c5_mod, "make_git_manager", lambda *a, **kw: fake_gm)

        st  = _state(tmp_path)
        cfg = configparser.ConfigParser()
        cos = make_commit_on_success(cfg, tmp_path, st)
        sha = cos.commit(TASK)
        assert sha == "deadf00d" + "0" * 32
        assert st.get_task("AUTO-T1")["commit"] == sha


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
