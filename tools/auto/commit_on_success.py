"""tools/auto/commit_on_success.py — AUTO-C5: commit validated task to git.

Called by the controller (or outer_loop caller) immediately after
``OuterLoop.run_task`` returns a *passed* result:

    from tools.auto.commit_on_success import CommitOnSuccess

    cos = CommitOnSuccess(git_manager, state_store)
    commit_hash = cos.commit(task, outer_result)
    # → "abc123def456…"  or  None if nothing was staged

Responsibilities
----------------
1. Call ``GitManager.commit_task(task_id, title)`` which stages all changes
   (``git add -u && git add .``) and commits with the conventional message
   ``auto(<task-id>): <title>``.
2. If the commit returns a hash (i.e. there were staged changes):
   a. Mark the task DONE in ``StateStore`` via ``set_task_status``,
      recording ``commit=<hash>`` in the task record.
   b. Log the event via the state store's ``log()`` helper.
3. If ``commit_task`` returns ``None`` (nothing staged — should be rare but
   possible if the coder wrote nothing new):
   a. Still mark the task DONE (the acceptance check passed, so the code is
      already in the desired state — perhaps a prior commit already covered it).
   b. Record ``commit=""`` to signal no new commit was made.
   c. Log a warning.
4. Never raises on a git failure; wraps ``GitError`` and returns ``None``,
   leaving the task status unchanged so the caller can decide what to do.

Public surface
--------------
    CommitOnSuccess(git_manager, state_store)
        .commit(task, outer_result)   -> Optional[str]   # commit hash or None

    make_commit_on_success(config, repo_dir, state_store) -> CommitOnSuccess

Spec reference: AUTO-C5
    AC: one commit per validated task; commit hash recorded in plan.json.
    AC: message format: auto(<task-id>): <title>
    Dep: AUTO-A3 (GitManager), AUTO-C3 (InnerLoop / pass signal).
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer
from tools.auto.git_manager import GitError, GitManager, make_git_manager
from tools.auto.state import StateStore, STATUS_DONE

# AUTO-CR-5: SummaryMemory is imported lazily inside commit() to keep the
# import graph acyclic and to avoid a hard dependency on the LLM stack for
# callers (tests) that don't need creative-mode synopsis updates.

logger = logging.getLogger(__name__)


class CommitOnSuccess:
    """Commits a validated task and updates persistent state.

    Parameters
    ----------
    git_manager:
        A ready-to-use :class:`~tools.auto.git_manager.GitManager` (repo
        already initialised, identity already configured).
    state_store:
        The run's :class:`~tools.auto.state.StateStore` instance.
    """

    def __init__(
        self,
        git_manager: GitManager,
        state_store: StateStore,
        *,
        summary_memory=None,
        task_mode: str = "code",
        base_dir=None,
    ) -> None:
        self._git = git_manager
        self._state = state_store
        self._summary_memory = summary_memory
        self._task_mode = task_mode
        self._base_dir = Path(base_dir) if base_dir is not None else None

    # ── Public API ───────────────────────────────────────────────────────────

    def commit(self, task: dict, outer_result=None) -> Optional[str]:
        """Stage all changes and commit for a validated *task*.

        Parameters
        ----------
        task:
            The task dict (must contain ``"id"`` and ``"title"``).
        outer_result:
            The ``OuterLoopResult`` returned by ``OuterLoop.run_task``.
            Unused at runtime but accepted so callers can pass it through
            for future extension (e.g. embedding round metadata in the
            commit message).  May be ``None``.

        Returns
        -------
        str or None
            The full SHA-1 commit hash on success, or ``None`` if the commit
            was skipped (nothing staged) or a :class:`GitError` was raised.
        """
        task_id = task.get("id", "UNKNOWN")
        title   = task.get("title", "")

        tracer.event(
            "controller", "commit_on_success", "commit_start",
            params={"task": task_id},
        )

        sha: Optional[str] = None
        try:
            sha = self._git.commit_task(task_id, title)
        except GitError as exc:
            logger.error(
                "CommitOnSuccess: git error for task %s — %s", task_id, exc
            )
            tracer.event(
                "commit_on_success", "controller", "commit_error",
                params={"task": task_id},
                content=str(exc),
            )
            return None

        if sha:
            logger.info(
                "CommitOnSuccess: committed task %s → %s", task_id, sha[:12]
            )
            self._state.set_task_status(task_id, STATUS_DONE, commit=sha)
            self._state.log(
                f"task {task_id} DONE — committed {sha[:12]} "
                f"(auto({task_id}): {title})"
            )
            tracer.event(
                "commit_on_success", "controller", "committed",
                params={"task": task_id, "sha": sha[:12]},
            )
            # AUTO-CR-5: after a successful creative commit, update synopsis.md.
            self._update_synopsis(task)
        else:
            # Nothing new to commit — acceptance check already passed, so
            # the working tree is in the correct state from a prior commit.
            logger.warning(
                "CommitOnSuccess: nothing staged for task %s; "
                "marking DONE with empty commit hash",
                task_id,
            )
            self._state.set_task_status(task_id, STATUS_DONE, commit="")
            self._state.log(
                f"task {task_id} DONE — no new commit (nothing staged)"
            )
            tracer.event(
                "commit_on_success", "controller", "nothing_staged",
                params={"task": task_id},
            )

        return sha

    # ── AUTO-CR-5: synopsis hook ─────────────────────────────────────────────

    def _update_synopsis(self, task: dict) -> None:
        """Call SummaryMemory.update() for a creative chapter task.

        Only runs when:
          - self._task_mode == "creative"
          - self._summary_memory is set (not None)
          - task["target_files"] contains at least one entry

        Fails silently so a synopsis error never disrupts the commit outcome.
        """
        if self._task_mode != "creative":
            return
        if self._summary_memory is None:
            return
        target_files = task.get("target_files") or []
        if not target_files:
            return
        chapter_file = target_files[0]
        base_dir = self._base_dir
        if base_dir is None:
            logger.warning(
                "CommitOnSuccess._update_synopsis: base_dir not set — "
                "cannot update synopsis for %s.", chapter_file,
            )
            return
        try:
            self._summary_memory.update(chapter_file, base_dir=base_dir)
        except Exception as exc:
            logger.error(
                "CommitOnSuccess: synopsis update failed for %s: %s — "                "commit outcome is unaffected.", chapter_file, exc,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_commit_on_success(
    config: configparser.ConfigParser,
    repo_dir: str | Path,
    state_store: StateStore,
) -> CommitOnSuccess:
    """Build a :class:`CommitOnSuccess` from config.

    Calls :func:`~tools.auto.git_manager.make_git_manager` which ensures the
    repo exists and configures the agent identity before returning.

    Parameters
    ----------
    config:
        A ``configparser.ConfigParser`` instance (reads ``[auto]`` section for
        ``git_user`` / ``git_email``).
    repo_dir:
        Root of the git repository to commit into.
    state_store:
        The active ``StateStore`` for this run.
    """
    task_mode = config.get("auto", "task_mode", fallback="code")
    gm = make_git_manager(repo_dir, config)

    # AUTO-CR-5: wire SummaryMemory for creative mode so synopsis.md is
    # updated after every accepted chapter commit.
    summary_memory = None
    if task_mode == "creative":
        try:
            from tools.auto.summary_memory import make_summary_memory
            summary_memory = make_summary_memory(config, base_dir=repo_dir, task_mode=task_mode)
        except Exception as exc:
            logger.warning(
                "make_commit_on_success: could not build SummaryMemory: %s — "                "synopsis updates will be skipped.", exc,
            )

    return CommitOnSuccess(
        gm, state_store,
        summary_memory=summary_memory,
        task_mode=task_mode,
        base_dir=repo_dir,
    )
