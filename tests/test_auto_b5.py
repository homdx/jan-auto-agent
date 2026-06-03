"""tests/test_auto_b5.py — Tests for AUTO-B5: Emit & commit the plan.

Covers all ACs from the story:

  AC1 (IMPROVEMENTS.md committed by agent identity; re-run is cheap):
      - emit() writes IMPROVEMENTS.md to the repo root.
      - emit() calls git.commit() with the expected message format.
      - On re-run with unchanged clusters, changed_clusters() returns [].
      - On re-run with one changed cluster, only that cluster is returned.

  AC2 (tasks upserted into plan.json):
      - emit() calls state.upsert_task() once per auto task.
      - State tasks are schema-valid (validated via _validate_task_schema).

  Broader coverage:

  ImprovementsMdContent:
      - Written content matches to_improvements_md() output exactly.
      - File is UTF-8 encoded.

  CommitMessage:
      - Message follows "auto(AUTO-B5): emit plan — N task(s)".
      - When git.commit() returns None (nothing to stage), emit() returns None.

  ClusterHashes:
      - _cluster_hash() is deterministic for same input.
      - _cluster_hash() differs when file list differs.
      - _cluster_hash() differs when cluster name differs.
      - changed_clusters() returns all clusters when no hash file exists (first run).
      - changed_clusters() returns [] when all clusters unchanged.
      - changed_clusters() returns only stale clusters after a partial change.
      - Hash file is written under .agent/cluster_hashes.json.
      - Corrupted hash file treated as empty (graceful fallback).

  StateLogging:
      - emit() calls state.log() with a summary line.

  Integration:
      - Full end-to-end: real tmp dirs, real StateStore + GitManager,
        IMPROVEMENTS.md on disk, plan.json populated, commit created.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.backlog_prioritiser import PrioritisedBacklog, build_backlog, to_improvements_md
from tools.auto.plan_emitter import IMPROVEMENTS_FILENAME, PlanEmitter, _cluster_hash
from tools.auto.repo_ingest import RepoCluster
from tools.auto.state import StateStore, _validate_task_schema


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cand(
    *,
    title: str = "Fix something",
    instruction: str = "Improve the code.",
    file: str = "tools/utils.py",
    symbol: str = "my_func",
    line_start: int = 1,
    line_end: int = 5,
    acceptance_check: str = "python -m pytest tests/ -q",
    cluster: str = "agents",
) -> CandidateTask:
    return CandidateTask(
        title            = title,
        instruction      = instruction,
        target_files     = [file],
        acceptance_check = acceptance_check,
        cited_location   = CitedLocation(
            file=file, symbol=symbol, line_start=line_start, line_end=line_end
        ),
        cluster=cluster,
    )


def _cluster(name: str, files: list[str]) -> RepoCluster:
    return RepoCluster(name=name, files=files, patterns=[])


def _mock_git(commit_return: str | None = "abc1234567890abc") -> MagicMock:
    git = MagicMock()
    git.commit.return_value = commit_return
    return git


def _mock_state(agent_dir: Path) -> MagicMock:
    state = MagicMock()
    state.agent_dir = agent_dir
    return state


def _make_emitter(tmp_path: Path, git=None, state=None) -> PlanEmitter:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if state is None:
        state = _mock_state(agent_dir)
    if git is None:
        git = _mock_git()
    return PlanEmitter(base_dir=tmp_path, state=state, git=git)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — IMPROVEMENTS.md written and committed
# ─────────────────────────────────────────────────────────────────────────────

class TestImprovementsMdWritten:
    def test_file_created_in_base_dir(self, tmp_path: Path) -> None:
        backlog = build_backlog([_cand()])
        emitter = _make_emitter(tmp_path)
        emitter.emit(backlog)
        assert (tmp_path / IMPROVEMENTS_FILENAME).exists()

    def test_content_matches_to_improvements_md(self, tmp_path: Path) -> None:
        backlog = build_backlog([_cand()])
        emitter = _make_emitter(tmp_path)
        emitter.emit(backlog)
        expected = to_improvements_md(backlog)
        actual = (tmp_path / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert actual == expected

    def test_file_is_utf8(self, tmp_path: Path) -> None:
        backlog = build_backlog([_cand(title="Ünïcödé task")])
        emitter = _make_emitter(tmp_path)
        emitter.emit(backlog)
        content = (tmp_path / IMPROVEMENTS_FILENAME).read_bytes()
        # Should decode cleanly as UTF-8.
        assert content.decode("utf-8")

    def test_empty_backlog_still_writes_file(self, tmp_path: Path) -> None:
        backlog = build_backlog([])
        emitter = _make_emitter(tmp_path)
        emitter.emit(backlog)
        assert (tmp_path / IMPROVEMENTS_FILENAME).exists()


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — git.commit() called with correct message
# ─────────────────────────────────────────────────────────────────────────────

class TestCommitMessage:
    def test_commit_called_once(self, tmp_path: Path) -> None:
        git = _mock_git()
        emitter = _make_emitter(tmp_path, git=git)
        emitter.emit(build_backlog([_cand()]))
        git.commit.assert_called_once()

    def test_commit_message_format_one_task(self, tmp_path: Path) -> None:
        git = _mock_git()
        emitter = _make_emitter(tmp_path, git=git)
        emitter.emit(build_backlog([_cand()]))
        msg = git.commit.call_args[0][0]
        assert msg == "auto(AUTO-B5): emit plan — 1 task(s)"

    def test_commit_message_format_zero_tasks(self, tmp_path: Path) -> None:
        git = _mock_git()
        emitter = _make_emitter(tmp_path, git=git)
        emitter.emit(build_backlog([]))
        msg = git.commit.call_args[0][0]
        assert msg == "auto(AUTO-B5): emit plan — 0 task(s)"

    def test_commit_message_format_many_tasks(self, tmp_path: Path) -> None:
        git = _mock_git()
        candidates = [
            _cand(title=f"Task {i}", file=f"tools/f{i}.py", acceptance_check="pytest")
            for i in range(5)
        ]
        emitter = _make_emitter(tmp_path, git=git)
        emitter.emit(build_backlog(candidates))
        msg = git.commit.call_args[0][0]
        assert msg == "auto(AUTO-B5): emit plan — 5 task(s)"

    def test_emit_returns_commit_hash(self, tmp_path: Path) -> None:
        git = _mock_git(commit_return="deadbeef1234")
        emitter = _make_emitter(tmp_path, git=git)
        result = emitter.emit(build_backlog([_cand()]))
        assert result == "deadbeef1234"

    def test_emit_returns_none_when_nothing_staged(self, tmp_path: Path) -> None:
        git = _mock_git(commit_return=None)
        emitter = _make_emitter(tmp_path, git=git)
        result = emitter.emit(build_backlog([_cand()]))
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — tasks upserted into plan.json via StateStore
# ─────────────────────────────────────────────────────────────────────────────

class TestTasksUpserted:
    def test_upsert_called_per_auto_task(self, tmp_path: Path) -> None:
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        backlog = build_backlog([
            _cand(title="T1", file="a.py", acceptance_check="pytest"),
            _cand(title="T2", file="b.py", acceptance_check="pytest"),
        ])
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(backlog)
        assert state.upsert_task.call_count == 2

    def test_upsert_not_called_for_manual_tasks(self, tmp_path: Path) -> None:
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        backlog = build_backlog([_cand(acceptance_check="manual review")])
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(backlog)
        state.upsert_task.assert_not_called()

    def test_upserted_tasks_are_schema_valid(self, tmp_path: Path) -> None:
        upserted: list[dict] = []
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        state.upsert_task.side_effect = upserted.append

        backlog = build_backlog([_cand()])
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(backlog)

        for task in upserted:
            _validate_task_schema(task)  # raises if invalid

    def test_zero_auto_tasks_no_upsert(self, tmp_path: Path) -> None:
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        backlog = build_backlog([_cand(acceptance_check="none")])
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(backlog)
        state.upsert_task.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# StateLogging
# ─────────────────────────────────────────────────────────────────────────────

class TestStateLogging:
    def test_state_log_called(self, tmp_path: Path) -> None:
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(build_backlog([_cand()]))
        # state.log should be called at least once
        assert state.log.call_count >= 1

    def test_state_log_mentions_task_count(self, tmp_path: Path) -> None:
        state = _mock_state(tmp_path / ".agent")
        (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
        emitter = PlanEmitter(base_dir=tmp_path, state=state, git=_mock_git())
        emitter.emit(build_backlog([_cand()]))
        log_messages = " ".join(str(c) for c in state.log.call_args_list)
        assert "1" in log_messages


# ─────────────────────────────────────────────────────────────────────────────
# _cluster_hash unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestClusterHash:
    def test_deterministic(self) -> None:
        c = _cluster("agents", ["tools/a.py", "tools/b.py"])
        assert _cluster_hash(c) == _cluster_hash(c)

    def test_order_independent(self) -> None:
        c1 = _cluster("agents", ["tools/a.py", "tools/b.py"])
        c2 = _cluster("agents", ["tools/b.py", "tools/a.py"])
        assert _cluster_hash(c1) == _cluster_hash(c2)

    def test_different_files_produce_different_hash(self) -> None:
        c1 = _cluster("agents", ["tools/a.py"])
        c2 = _cluster("agents", ["tools/b.py"])
        assert _cluster_hash(c1) != _cluster_hash(c2)

    def test_different_name_produces_different_hash(self) -> None:
        c1 = _cluster("cluster-a", ["tools/a.py"])
        c2 = _cluster("cluster-b", ["tools/a.py"])
        assert _cluster_hash(c1) != _cluster_hash(c2)

    def test_extra_file_produces_different_hash(self) -> None:
        c1 = _cluster("agents", ["tools/a.py"])
        c2 = _cluster("agents", ["tools/a.py", "tools/b.py"])
        assert _cluster_hash(c1) != _cluster_hash(c2)

    def test_empty_files_has_hash(self) -> None:
        c = _cluster("empty", [])
        h = _cluster_hash(c)
        assert isinstance(h, str) and len(h) == 64  # SHA-256 hex


# ─────────────────────────────────────────────────────────────────────────────
# changed_clusters — re-run cheapness (AC1)
# ─────────────────────────────────────────────────────────────────────────────

class TestChangedClusters:
    def test_all_returned_on_first_run(self, tmp_path: Path) -> None:
        """No hash file → every cluster is considered stale."""
        emitter = _make_emitter(tmp_path)
        clusters = [
            _cluster("agents", ["tools/a.py"]),
            _cluster("io",     ["tools/b.py"]),
        ]
        stale = emitter.changed_clusters(clusters)
        assert stale == clusters

    def test_none_returned_after_emit_unchanged(self, tmp_path: Path) -> None:
        """After emit() with the same clusters, changed_clusters() returns []."""
        clusters = [
            _cluster("agents", ["tools/a.py"]),
            _cluster("io",     ["tools/b.py"]),
        ]
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]), clusters=clusters)
        stale = emitter.changed_clusters(clusters)
        assert stale == []

    def test_only_changed_cluster_returned(self, tmp_path: Path) -> None:
        cluster_a = _cluster("agents", ["tools/a.py"])
        cluster_b = _cluster("io",     ["tools/b.py"])
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]), clusters=[cluster_a, cluster_b])

        # Now cluster_b gets a new file.
        cluster_b_new = _cluster("io", ["tools/b.py", "tools/c.py"])
        stale = emitter.changed_clusters([cluster_a, cluster_b_new])
        assert len(stale) == 1
        assert stale[0].name == "io"

    def test_new_cluster_is_stale(self, tmp_path: Path) -> None:
        cluster_a = _cluster("agents", ["tools/a.py"])
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]), clusters=[cluster_a])

        cluster_b = _cluster("io", ["tools/b.py"])  # new cluster
        stale = emitter.changed_clusters([cluster_a, cluster_b])
        assert any(c.name == "io" for c in stale)
        assert all(c.name != "agents" for c in stale)

    def test_hash_file_written_to_agent_dir(self, tmp_path: Path) -> None:
        cluster_a = _cluster("agents", ["tools/a.py"])
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]), clusters=[cluster_a])

        hashes_path = tmp_path / ".agent" / "cluster_hashes.json"
        assert hashes_path.exists()
        data = json.loads(hashes_path.read_text())
        assert "agents" in data

    def test_corrupted_hash_file_treated_as_empty(self, tmp_path: Path) -> None:
        """A corrupted hash file causes all clusters to be reported stale."""
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "cluster_hashes.json").write_text("NOT JSON", encoding="utf-8")

        emitter = _make_emitter(tmp_path)
        clusters = [_cluster("agents", ["tools/a.py"])]
        stale = emitter.changed_clusters(clusters)
        assert stale == clusters

    def test_no_clusters_returns_empty(self, tmp_path: Path) -> None:
        emitter = _make_emitter(tmp_path)
        assert emitter.changed_clusters([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# emit() without clusters arg — hash not updated
# ─────────────────────────────────────────────────────────────────────────────

class TestEmitWithoutClusters:
    def test_no_hash_file_created_when_clusters_not_passed(self, tmp_path: Path) -> None:
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]))  # clusters=None
        hashes_path = tmp_path / ".agent" / "cluster_hashes.json"
        assert not hashes_path.exists()

    def test_changed_clusters_returns_all_when_no_hash(self, tmp_path: Path) -> None:
        emitter = _make_emitter(tmp_path)
        emitter.emit(build_backlog([]))  # no clusters → no hash written
        clusters = [_cluster("agents", ["tools/a.py"])]
        stale = emitter.changed_clusters(clusters)
        assert stale == clusters


# ─────────────────────────────────────────────────────────────────────────────
# Integration — real StateStore + GitManager + tmp repo
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    """End-to-end: real StateStore, real GitManager, real filesystem."""

    def test_end_to_end(self, tmp_path: Path) -> None:
        """
        Full pipeline:
        1. Init real StateStore + GitManager in a tmp dir.
        2. Build a backlog with two auto tasks and one manual.
        3. emit() → IMPROVEMENTS.md on disk, plan.json populated, git commit.
        4. Verify IMPROVEMENTS.md content and plan.json task list.
        5. Re-run with same clusters → changed_clusters() returns [].
        """
        import subprocess
        from tools.auto.git_manager import GitManager
        from tools.auto.state import StateStore

        repo = tmp_path / "repo"
        repo.mkdir()

        # Minimal git init
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo, check=True, capture_output=True,
        )
        # Need at least one commit so HEAD exists for rev-parse
        (repo / "README.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
        )

        state = StateStore(repo / ".agent")
        state.initialise("improve code", repo)

        git = GitManager(repo_dir=repo)
        git.configure_identity()

        candidates = [
            _cand(title="Fix alpha", file="tools/a.py", line_start=1, line_end=5),
            _cand(title="Fix beta",  file="tools/b.py", line_start=1, line_end=5),
            _cand(title="Manual refactor", acceptance_check="manual review"),
        ]
        backlog = build_backlog(candidates)

        clusters = [
            _cluster("tools", ["tools/a.py", "tools/b.py"]),
            _cluster("support", ["tools/c.py"]),
        ]

        emitter = PlanEmitter(base_dir=repo, state=state, git=git)
        commit_hash = emitter.emit(backlog, clusters=clusters)

        # IMPROVEMENTS.md must exist and have correct sections
        md = (repo / IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")
        assert "## Autonomous Tasks" in md
        assert "## Manual Suggestions" in md
        assert "Manual refactor" in md

        # plan.json must have 2 auto tasks
        plan = json.loads((repo / ".agent" / "plan.json").read_text())
        task_ids = {t["id"] for t in plan["tasks"]}
        assert len(task_ids) == 2

        # All tasks are schema-valid
        for t in plan["tasks"]:
            _validate_task_schema(t)

        # A commit was created
        assert commit_hash is not None
        assert len(commit_hash) >= 12

        # Re-run: changed_clusters should return [] (unchanged)
        stale = emitter.changed_clusters(clusters)
        assert stale == []

    def test_rerun_with_changed_cluster(self, tmp_path: Path) -> None:
        """Second emit with one modified cluster → only that cluster stale."""
        import subprocess
        from tools.auto.git_manager import GitManager
        from tools.auto.state import StateStore

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=repo, check=True, capture_output=True,
        )
        (repo / "README.md").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
        )

        state = StateStore(repo / ".agent")
        state.initialise("improve", repo)
        git = GitManager(repo_dir=repo)
        git.configure_identity()

        clusters_v1 = [
            _cluster("agents", ["tools/a.py"]),
            _cluster("io",     ["tools/b.py"]),
        ]
        emitter = PlanEmitter(base_dir=repo, state=state, git=git)
        emitter.emit(build_backlog([]), clusters=clusters_v1)

        # Modify io cluster
        clusters_v2 = [
            _cluster("agents", ["tools/a.py"]),
            _cluster("io",     ["tools/b.py", "tools/c.py"]),  # new file
        ]
        stale = emitter.changed_clusters(clusters_v2)
        assert len(stale) == 1
        assert stale[0].name == "io"
