"""tools/auto/git_manager.py — AUTO-A3: Git integration + agent identity.

Provides a thin, testable wrapper around git for the autonomous agent:

  * Ensures the target folder is a git repo (runs ``git init`` if absent).
  * Reads agent identity from ``agents.ini [auto]`` and configures
    ``user.name`` / ``user.email`` locally in the repo.
  * Stages all tracked + new files and commits them with a conventional
    commit message, returning the commit hash.
  * Skips empty commits (no staged changes) — never produces an empty commit.

Public surface consumed by controller.py and the coder loop:

    from tools.auto.git_manager import GitManager

    gm = GitManager(repo_dir, config)           # config is a ConfigParser
    gm.ensure_repo()                            # git init if needed
    gm.configure_identity()                     # apply [auto] git_user / git_email
    hash_ = gm.commit("AUTO-T1", "Fix logger") # None if nothing to commit
    hash_ = gm.commit_task(task_id, title)      # convenience: formats message

Convention
----------
Commit messages follow the pattern:  ``auto(<task-id>): <title>``
e.g.  ``auto(AUTO-T1): Fix off-by-one in retry loop``

agents.ini [auto] keys
----------------------
git_user   — git user.name for agent commits   (default: "auto-agent")
git_email  — git user.email for agent commits  (default: "auto-agent@localhost")
"""

from __future__ import annotations

import configparser
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
_DEFAULT_GIT_USER  = "auto-agent"
_DEFAULT_GIT_EMAIL = "auto-agent@localhost"


class GitError(RuntimeError):
    """Raised when a git command exits non-zero unexpectedly."""


class GitManager:
    """Manages git operations for an autonomous run.

    Parameters
    ----------
    repo_dir:
        The directory that must be (or become) a git repository.
    config:
        A ``configparser.ConfigParser`` instance.  The ``[auto]`` section
        is read for ``git_user`` and ``git_email``.  Pass ``None`` to use
        only defaults.
    """

    def __init__(
        self,
        repo_dir: str | Path,
        config: Optional[configparser.ConfigParser] = None,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self._config  = config or configparser.ConfigParser()

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def git_user(self) -> str:
        return self._config.get("auto", "git_user", fallback=_DEFAULT_GIT_USER)

    @property
    def git_email(self) -> str:
        return self._config.get("auto", "git_email", fallback=_DEFAULT_GIT_EMAIL)

    # ── Public API ───────────────────────────────────────────────────────────

    def ensure_repo(self) -> bool:
        """Ensure *repo_dir* is a git repository.

        Runs ``git init`` if no ``.git`` directory is found.

        Returns
        -------
        bool
            ``True`` if a new repo was initialised; ``False`` if one
            already existed.
        """
        if (self.repo_dir / ".git").is_dir():
            logger.debug("ensure_repo: repo already exists at %s", self.repo_dir)
            return False

        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self._run(["git", "init"], "git init failed")
        logger.info("ensure_repo: initialised new git repo at %s", self.repo_dir)
        return True

    def configure_identity(self) -> None:
        """Set ``user.name`` and ``user.email`` locally in the repo.

        Reads values from ``agents.ini [auto]``.  Always overwrites so
        that re-runs with a changed config take effect immediately.
        """
        self._run(
            ["git", "config", "user.name", self.git_user],
            "git config user.name failed",
        )
        self._run(
            ["git", "config", "user.email", self.git_email],
            "git config user.email failed",
        )
        logger.debug(
            "configure_identity: set %s <%s> in %s",
            self.git_user, self.git_email, self.repo_dir,
        )

    def stage_all(self) -> None:
        """Stage all tracked modifications and new (untracked) files.

        Equivalent to ``git add -u`` followed by ``git add .`` so that
        both modifications and brand-new files are included.
        """
        self._run(["git", "add", "-u"], "git add -u failed")
        self._run(["git", "add", "."], "git add . failed")

    def has_staged_changes(self) -> bool:
        """Return ``True`` if there is at least one staged change."""
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.repo_dir,
            capture_output=True,
        )
        # exit 0 → nothing staged; exit 1 → changes staged
        return result.returncode != 0

    def commit(self, message: str) -> Optional[str]:
        """Stage everything and commit with *message*.

        Skips the commit (and returns ``None``) if there are no staged
        changes after staging, so empty commits are never created.

        Returns
        -------
        str or None
            The full SHA-1 commit hash on success, or ``None`` if nothing
            was committed.
        """
        self.stage_all()
        if not self.has_staged_changes():
            logger.info("commit: nothing to commit — skipping")
            return None

        self._run(["git", "commit", "-m", message], "git commit failed")
        sha = self._run(
            ["git", "rev-parse", "HEAD"],
            "git rev-parse HEAD failed",
        ).strip()
        logger.info("commit: %s  (%s)", sha[:12], message)
        return sha

    def commit_task(self, task_id: str, title: str) -> Optional[str]:
        """Commit all staged changes with the conventional auto-task message.

        Message format: ``auto(<task-id>): <title>``

        Returns
        -------
        str or None
            Commit hash, or ``None`` if nothing was committed.
        """
        msg = f"auto({task_id}): {title}"
        return self.commit(msg)

    def get_current_hash(self) -> Optional[str]:
        """Return the current HEAD commit hash, or ``None`` if no commits yet."""
        try:
            return self._run(
                ["git", "rev-parse", "HEAD"],
                "git rev-parse HEAD failed",
            ).strip()
        except GitError:
            return None

    def get_commit_author(self, sha: str) -> dict:
        """Return ``{name, email}`` of the author of commit *sha*."""
        name = self._run(
            ["git", "log", "-1", "--format=%an", sha],
            "git log author name failed",
        ).strip()
        email = self._run(
            ["git", "log", "-1", "--format=%ae", sha],
            "git log author email failed",
        ).strip()
        return {"name": name, "email": email}

    def get_commit_message(self, sha: str) -> str:
        """Return the commit message (subject line) of commit *sha*."""
        return self._run(
            ["git", "log", "-1", "--format=%s", sha],
            "git log message failed",
        ).strip()

    # ── Private ──────────────────────────────────────────────────────────────

    def _run(self, cmd: list[str], error_msg: str) -> str:
        """Run *cmd* inside *repo_dir*, capture output, raise on failure."""
        result = subprocess.run(
            cmd,
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(
                f"{error_msg}\n"
                f"  cmd : {' '.join(cmd)}\n"
                f"  code: {result.returncode}\n"
                f"  out : {result.stdout.strip()}\n"
                f"  err : {result.stderr.strip()}"
            )
        return result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory — used by AutoController
# ─────────────────────────────────────────────────────────────────────────────

def make_git_manager(
    repo_dir: str | Path,
    config: Optional[configparser.ConfigParser] = None,
) -> GitManager:
    """Create a :class:`GitManager`, ensure the repo exists, and configure
    the agent identity in one call.

    This is the preferred entry-point for ``AutoController``.
    """
    gm = GitManager(repo_dir, config)
    gm.ensure_repo()
    gm.configure_identity()
    return gm