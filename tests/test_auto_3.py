"""tests/test_auto_a3.py — Tests for AUTO-A3: Git integration + agent identity.

Covers all ACs from the story:

  AC1: [auto] git_user / git_email read from agents.ini; commits show the
       agent author (verified via get_commit_author).
  AC2: commit only when there is a staged change; empty commits never created
       (commit() returns None when nothing is staged).
  AC3: each task commit message follows auto(<task-id>): <title>
       (verified via get_commit_message).

Also exercises the full GitManager / make_git_manager public surface:
  - ensure_repo (fresh + idempotent)
  - configure_identity (custom config + defaults)
  - stage_all / has_staged_changes
  - commit (with and without changes)
  - commit_task (message format)
  - get_current_hash (no commits + after commit)
  - get_commit_author / get_commit_message
  - make_git_manager factory
  - GitError on bad git commands
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

# Make project root importable regardless of where pytest is invoked.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.git_manager import GitError, GitManager, make_git_manager


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_config(git_user: str, git_email: str) -> configparser.ConfigParser:
    """Return a ConfigParser with an [auto] section pre-populated."""
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": git_user, "git_email": git_email}
    return cfg


def _seed_file(repo_dir: Path, name: str = "hello.txt", content: str = "hello") -> Path:
    """Write a file inside repo_dir so there is something to commit."""
    p = repo_dir / name
    p.write_text(content, encoding="utf-8")
    return p


def _fresh_gm(tmp_path: Path, cfg: configparser.ConfigParser | None = None) -> GitManager:
    """Return a GitManager with repo initialised and identity configured."""
    gm = GitManager(tmp_path, cfg)
    gm.ensure_repo()
    gm.configure_identity()
    return gm


# ─────────────────────────────────────────────────────────────────────────────
# ensure_repo
# ─────────────────────────────────────────────────────────────────────────────

class TestEnsureRepo:
    def test_creates_git_dir(self, tmp_path):
        gm = GitManager(tmp_path)
        gm.ensure_repo()
        assert (tmp_path / ".git").is_dir()

    def test_returns_true_on_fresh_init(self, tmp_path):
        gm = GitManager(tmp_path)
        assert gm.ensure_repo() is True

    def test_returns_false_when_repo_exists(self, tmp_path):
        gm = GitManager(tmp_path)
        gm.ensure_repo()
        # Second call — already a repo
        assert gm.ensure_repo() is False

    def test_idempotent_does_not_raise(self, tmp_path):
        gm = GitManager(tmp_path)
        gm.ensure_repo()
        gm.ensure_repo()  # must not raise
        assert (tmp_path / ".git").is_dir()

    def test_creates_parent_dirs_if_missing(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        gm = GitManager(deep)
        gm.ensure_repo()
        assert (deep / ".git").is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Identity from agents.ini [auto]
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigureIdentity:
    def test_git_user_read_from_config(self, tmp_path):
        cfg = _make_config("my-bot", "my-bot@example.com")
        gm = _fresh_gm(tmp_path, cfg)
        assert gm.git_user == "my-bot"

    def test_git_email_read_from_config(self, tmp_path):
        cfg = _make_config("my-bot", "my-bot@example.com")
        gm = _fresh_gm(tmp_path, cfg)
        assert gm.git_email == "my-bot@example.com"

    def test_defaults_when_no_config(self, tmp_path):
        gm = _fresh_gm(tmp_path, cfg=None)
        assert gm.git_user == "auto-agent"
        assert gm.git_email == "auto-agent@localhost"

    def test_defaults_when_auto_section_absent(self, tmp_path):
        cfg = configparser.ConfigParser()  # no [auto] section
        gm = _fresh_gm(tmp_path, cfg)
        assert gm.git_user == "auto-agent"
        assert gm.git_email == "auto-agent@localhost"

    def test_identity_applied_to_repo(self, tmp_path):
        """configure_identity must set local git config so commits carry the name."""
        cfg = _make_config("testbot", "testbot@ci.local")
        gm = _fresh_gm(tmp_path, cfg)
        _seed_file(tmp_path)
        sha = gm.commit("initial")
        assert sha is not None
        author = gm.get_commit_author(sha)
        assert author["name"] == "testbot"
        assert author["email"] == "testbot@ci.local"

    def test_reconfigure_with_new_identity(self, tmp_path):
        """Re-calling configure_identity with different config takes effect."""
        gm = _fresh_gm(tmp_path, _make_config("bot-v1", "v1@x.com"))
        # change config
        gm._config = _make_config("bot-v2", "v2@x.com")
        gm.configure_identity()
        _seed_file(tmp_path)
        sha = gm.commit("test")
        assert sha is not None
        author = gm.get_commit_author(sha)
        assert author["name"] == "bot-v2"

    def test_configure_requires_existing_repo(self, tmp_path):
        """configure_identity on a non-repo dir must raise GitError."""
        gm = GitManager(tmp_path)
        # no ensure_repo call
        with pytest.raises(GitError):
            gm.configure_identity()


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — No empty commits
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyCommitGuard:
    def test_commit_returns_none_when_nothing_staged(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        result = gm.commit("should be skipped")
        assert result is None

    def test_has_staged_changes_false_on_clean_repo(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        assert gm.has_staged_changes() is False

    def test_has_staged_changes_true_after_add(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        gm.stage_all()
        assert gm.has_staged_changes() is True

    def test_commit_clears_staged_changes(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        gm.commit("add file")
        assert gm.has_staged_changes() is False

    def test_second_commit_no_changes_returns_none(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        gm.commit("first")
        # no new changes
        result = gm.commit("second — should be skipped")
        assert result is None

    def test_no_empty_commit_after_delete(self, tmp_path):
        """Deleting a file is a change; after committing it, a second commit is empty."""
        gm = _fresh_gm(tmp_path)
        f = _seed_file(tmp_path)
        gm.commit("add")
        f.unlink()
        sha = gm.commit("delete")
        assert sha is not None  # deletion is a real change
        result = gm.commit("nothing left to commit")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# commit() — returns SHA
# ─────────────────────────────────────────────────────────────────────────────

class TestCommit:
    def test_commit_returns_sha_string(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit("initial commit")
        assert isinstance(sha, str)
        assert len(sha) == 40  # full SHA-1

    def test_commit_sha_matches_head(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit("initial commit")
        assert sha == gm.get_current_hash()

    def test_multiple_commits_return_different_shas(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path, "a.txt", "a")
        sha1 = gm.commit("first")
        _seed_file(tmp_path, "b.txt", "b")
        sha2 = gm.commit("second")
        assert sha1 != sha2

    def test_new_file_is_staged_and_committed(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path, "new.txt", "content")
        sha = gm.commit("add new.txt")
        assert sha is not None

    def test_modified_file_is_staged_and_committed(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        f = _seed_file(tmp_path)
        gm.commit("initial")
        f.write_text("modified", encoding="utf-8")
        sha = gm.commit("modify")
        assert sha is not None


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — commit_task message format
# ─────────────────────────────────────────────────────────────────────────────

class TestCommitTask:
    def test_message_follows_convention(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit_task("AUTO-T1", "Fix off-by-one in retry loop")
        assert sha is not None
        msg = gm.get_commit_message(sha)
        assert msg == "auto(AUTO-T1): Fix off-by-one in retry loop"

    def test_message_contains_task_id(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit_task("AUTO-B3", "Grounding filter")
        msg = gm.get_commit_message(sha)
        assert "AUTO-B3" in msg

    def test_message_contains_title(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit_task("AUTO-C2", "Generate patch for coder")
        msg = gm.get_commit_message(sha)
        assert "Generate patch for coder" in msg

    def test_commit_task_returns_none_when_nothing_staged(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        result = gm.commit_task("AUTO-T1", "nothing here")
        assert result is None

    def test_commit_task_sha_is_40_chars(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit_task("AUTO-A3", "Git integration")
        assert sha is not None and len(sha) == 40

    def test_multiple_task_commits_unique_shas(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path, "f1.txt", "x")
        sha1 = gm.commit_task("AUTO-T1", "first task")
        _seed_file(tmp_path, "f2.txt", "y")
        sha2 = gm.commit_task("AUTO-T2", "second task")
        assert sha1 != sha2


# ─────────────────────────────────────────────────────────────────────────────
# get_current_hash
# ─────────────────────────────────────────────────────────────────────────────

class TestGetCurrentHash:
    def test_returns_none_before_any_commit(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        assert gm.get_current_hash() is None

    def test_returns_sha_after_commit(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        gm.commit("init")
        h = gm.get_current_hash()
        assert h is not None and len(h) == 40

    def test_advances_with_each_commit(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path, "a.txt")
        gm.commit("first")
        h1 = gm.get_current_hash()
        _seed_file(tmp_path, "b.txt")
        gm.commit("second")
        h2 = gm.get_current_hash()
        assert h1 != h2


# ─────────────────────────────────────────────────────────────────────────────
# get_commit_author / get_commit_message
# ─────────────────────────────────────────────────────────────────────────────

class TestCommitInspection:
    def test_get_commit_author_name(self, tmp_path):
        cfg = _make_config("inspector-bot", "bot@inspect.io")
        gm = _fresh_gm(tmp_path, cfg)
        _seed_file(tmp_path)
        sha = gm.commit("test")
        assert gm.get_commit_author(sha)["name"] == "inspector-bot"

    def test_get_commit_author_email(self, tmp_path):
        cfg = _make_config("inspector-bot", "bot@inspect.io")
        gm = _fresh_gm(tmp_path, cfg)
        _seed_file(tmp_path)
        sha = gm.commit("test")
        assert gm.get_commit_author(sha)["email"] == "bot@inspect.io"

    def test_get_commit_message_returns_subject(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit("my subject line")
        assert gm.get_commit_message(sha) == "my subject line"

    def test_get_commit_message_for_task(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        sha = gm.commit_task("AUTO-A3", "Git integration")
        assert gm.get_commit_message(sha) == "auto(AUTO-A3): Git integration"


# ─────────────────────────────────────────────────────────────────────────────
# make_git_manager factory
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeGitManager:
    def test_returns_git_manager_instance(self, tmp_path):
        gm = make_git_manager(tmp_path)
        assert isinstance(gm, GitManager)

    def test_repo_is_initialised(self, tmp_path):
        make_git_manager(tmp_path)
        assert (tmp_path / ".git").is_dir()

    def test_identity_is_configured(self, tmp_path):
        cfg = _make_config("factory-bot", "factory@bot.io")
        gm = make_git_manager(tmp_path, cfg)
        _seed_file(tmp_path)
        sha = gm.commit("via factory")
        assert sha is not None
        author = gm.get_commit_author(sha)
        assert author["name"] == "factory-bot"

    def test_factory_with_no_config_uses_defaults(self, tmp_path):
        gm = make_git_manager(tmp_path)
        assert gm.git_user == "auto-agent"
        assert gm.git_email == "auto-agent@localhost"

    def test_factory_idempotent_on_existing_repo(self, tmp_path):
        make_git_manager(tmp_path)
        gm2 = make_git_manager(tmp_path)  # must not raise
        assert isinstance(gm2, GitManager)


# ─────────────────────────────────────────────────────────────────────────────
# GitError
# ─────────────────────────────────────────────────────────────────────────────

class TestGitError:
    def test_bad_command_raises_git_error(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        with pytest.raises(GitError):
            gm._run(["git", "no-such-subcommand"], "expected failure")

    def test_git_error_message_contains_command(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        with pytest.raises(GitError, match="no-such-subcommand"):
            gm._run(["git", "no-such-subcommand"], "cmd failed")


# ─────────────────────────────────────────────────────────────────────────────
# stage_all
# ─────────────────────────────────────────────────────────────────────────────

class TestStageAll:
    def test_stages_new_file(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path, "new.txt")
        gm.stage_all()
        assert gm.has_staged_changes() is True

    def test_stages_modified_tracked_file(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        f = _seed_file(tmp_path)
        gm.commit("initial")
        f.write_text("changed", encoding="utf-8")
        gm.stage_all()
        assert gm.has_staged_changes() is True

    def test_stage_all_on_clean_repo_no_changes(self, tmp_path):
        gm = _fresh_gm(tmp_path)
        _seed_file(tmp_path)
        gm.commit("initial")
        gm.stage_all()  # nothing new
        assert gm.has_staged_changes() is False


if __name__ == "__main__":

    tests_funcs = []  # pytest handles discovery; use `pytest test_auto_3.py -v`
    print("Run with: pytest test_auto_3.py -v")