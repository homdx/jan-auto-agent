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
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from tools.git_run import (
    LOCK_BACKOFF_S,
    LOCK_CONTENTION_RE,
    LOCK_RETRIES,
    run_git,
)

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
_DEFAULT_GIT_USER  = "auto-agent"
_DEFAULT_GIT_EMAIL = "auto-agent@localhost"


def _normalised_task_mode(config: configparser.ConfigParser) -> str:
    """The run's task mode, or ``"code"`` when unset/unreadable (B4).

    Only ``creative`` mode writes the continuity files ``user_changes`` must
    ignore, so anything that is not exactly ``creative`` — including a
    malformed ``[auto] task_mode`` — is treated as ``code``.
    """
    try:
        from tools.auto.utils import normalize_task_mode
        mode, _warning = normalize_task_mode(
            config.get("auto", "task_mode", fallback="code")
        )
    except Exception:   # noqa: BLE001 — an unreadable config degrades to code
        return "code"
    return mode if mode in ("code", "creative", "docs") else "code"


class DirtyTreeError(RuntimeError):
    """The target has uncommitted changes that are not the agent's: a failed task's reset would take them."""

    def __init__(self, paths: "list[str]") -> None:
        self.paths = list(paths)
        shown = ", ".join(self.paths[:5]) + (f" and {len(self.paths) - 5} more" if len(self.paths) > 5 else "")
        super().__init__(f"uncommitted changes in the target: {shown} — commit or stash them first "
                         "(a task that fails resets the tree to HEAD)")


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
        self._config  = config or configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        # Normalised [auto] task_mode, read once (B4): it decides whether the
        # agent's own creative continuity files count as its own output. A
        # malformed value degrades to "code", which is the safe default — the
        # extra files are then never written, so they need no exclusion.
        self.task_mode = _normalised_task_mode(self._config)

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def git_user(self) -> str:
        return self._config.get("auto", "git_user", fallback=_DEFAULT_GIT_USER)

    @property
    def git_email(self) -> str:
        return self._config.get("auto", "git_email", fallback=_DEFAULT_GIT_EMAIL)

    # ── Public API ───────────────────────────────────────────────────────────

    # Entries the repo's .gitignore must contain, so internal state and backups are never committed.
    _GITIGNORE_ENTRIES: tuple[str, ...] = (
        ".agent/",       # all agent state (plan.json, run.log, trace files, …)
        "*.coder.bak",   # per-file backups written by Coder before each edit
    )

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

    def ensure_gitignore_committed(self) -> None:
        """Add missing safety entries to .gitignore and commit the change.

        Call this *after* :meth:`configure_identity` so git has a user name
        for the commit.  Only writes and commits lines that are not already
        present, so existing ``.gitignore`` content is preserved.  Skips the
        commit if nothing new was added (idempotent across runs).
        """
        gi_path = self.repo_dir / ".gitignore"
        existing_lines: set[str] = set()
        if gi_path.exists():
            try:
                existing_lines = set(gi_path.read_text(encoding="utf-8").splitlines())
            except OSError as exc:
                logger.warning("ensure_gitignore_committed: cannot read .gitignore: %s", exc)

        missing = [e for e in self._GITIGNORE_ENTRIES if e not in existing_lines]
        if not missing:
            logger.debug("ensure_gitignore_committed: all entries already present")
            return

        try:
            with gi_path.open("a", encoding="utf-8") as fh:
                fh.write("\n# auto-agent safety entries\n")
                for entry in missing:
                    fh.write(entry + "\n")
        except OSError as exc:
            logger.warning("ensure_gitignore_committed: cannot write .gitignore: %s", exc)
            return

        # Stage only .gitignore and commit it at once, so stage_all() never sees it as a pending change.
        try:
            self._run(["git", "add", ".gitignore"], "git add .gitignore failed")
            self._run(
                ["git", "commit", "-m", "chore: add auto-agent safety .gitignore", "--", ".gitignore"],
                "git commit .gitignore failed",
            )
            logger.info(
                "ensure_gitignore_committed: committed .gitignore with entries: %s", missing
            )
        except GitError as exc:
            # Non-fatal: the file is written and the entries are present for git to honour.
            logger.warning("ensure_gitignore_committed: commit failed: %s", exc)

    #: what the agent itself leaves in the target outside .agent/: not the user's changes
    _OWN_OUTPUTS: tuple[str, ...] = (".agent/", "IMPROVEMENTS.md", "IMPROVEMENTS-FALSE.md")

    def _collect_dir(self) -> Optional[str]:
        """``[collect] dir`` relative to the repository (``.collect/``), or None when it lies outside it."""
        try:
            from tools.collect.cli import resolve_collect_dir
            rel = resolve_collect_dir(self.repo_dir, self._config).resolve().relative_to(self.repo_dir)
        except Exception:   # noqa: BLE001 — no collect dir to keep
            return None
        return rel.as_posix().rstrip("/") + "/" if rel.parts else None

    def _creative_outputs(self) -> tuple[str, ...]:
        """The creative-mode continuity files the agent itself writes into the
        repo — the synopsis, the story bible, and the bible's pending-corrections
        sidecar.

        B4: ``CommitOnSuccess`` calls ``_update_synopsis`` / ``_update_story_bible``
        *after* the task commit, so both files are left untracked when the run
        ends. The next ``--auto`` start runs ``user_changes()`` before it commits
        anything, saw them as the user's own uncommitted changes, and raised
        ``DirtyTreeError`` — a second run on a creative folder was refused for
        changes the agent itself made.

        The names come from the objects that write them, exactly as
        ``story_bible.py:562`` builds the sidecar
        (``self._path.with_suffix(self._path.suffix + ".pending.json")``), and
        fall back to the documented defaults: a renamed path must not silently
        reappear in ``user_changes()``. Fail-open throughout — anything
        unexpected returns ``()``, which is the pre-B4 behaviour.

        Returns ``()`` outside creative mode: these files are never written
        there, so listing them would be noise (and a user's own file of that
        name would be wrongly ignored).
        """
        # task_mode is set in __init__; a subclass that does not call it simply
        # has no creative outputs, which is the safe direction.
        if getattr(self, "task_mode", "code") != "creative":
            return ()
        names: list[str] = ["synopsis.md", "story_bible.md"]
        try:
            synopsis = self._get_synopsis_path()
            bible = self._get_bible_path()
            pending = self._get_bible_pending_path(bible)
            names = [
                self._own_output_name(synopsis, "synopsis.md"),
                self._own_output_name(bible, "story_bible.md"),
                self._own_output_name(pending, "story_bible.md.pending.json"),
            ]
        except Exception:   # noqa: BLE001 — never let a config read break _setup_git
            return ()
        return tuple(
            n for n in dict.fromkeys(names)
            if n and not n.startswith(("/", "..")) and "/" not in n
        )

    def _own_output_name(self, path: "Optional[Path]", default: str) -> str:
        """The repo-relative name of one creative output path (B4).

        Read from the object that writes it, so a path moved in config is still
        excluded. A path outside the repo cannot appear in ``git status`` at
        all, so it falls back to *default* — harmless, since nothing in the
        tree matches it.
        """
        if path is None:
            return default
        try:
            return path.resolve().relative_to(self.repo_dir).as_posix()
        except ValueError:
            return default

    # ── creative output paths, read from the objects that write them ───────────

    def _get_synopsis_path(self) -> "Optional[Path]":
        """``SummaryMemory``'s ``synopsis.md`` path, or ``None``."""
        try:
            from tools.auto.summary_memory import make_summary_memory
            sm = make_summary_memory(self._config, self.repo_dir, task_mode="creative")
            path = getattr(sm, "_synopsis_path", None)
            return path if isinstance(path, Path) else None
        except Exception:   # noqa: BLE001 — constructor can raise on a bad profile
            return None

    def _get_bible_path(self) -> "Optional[Path]":
        """``StoryBible``'s ``_path`` (``story_bible.md``), or ``None``."""
        try:
            from tools.auto.story_bible import make_story_bible
            bible = make_story_bible(
                self._config, base_url="http://localhost/", api_key="",
                model="", api_format="openai", base_dir=self.repo_dir,
            )
            if bible is None:
                return None
            path = getattr(bible, "_path", None)
            return path if isinstance(path, Path) else None
        except Exception:   # noqa: BLE001 — never let a config read break _setup_git
            return None

    @staticmethod
    def _get_bible_pending_path(bible_path: "Optional[Path]") -> "Optional[Path]":
        """The bible's pending-corrections sidecar, named the way
        ``StoryBible._pending_corrections_path`` names it."""
        if bible_path is None:
            return None
        return bible_path.with_suffix(bible_path.suffix + ".pending.json")

    def user_changes(self) -> list[str]:
        """The paths ``git status`` reports that are not the agent's own outputs (the collect artifact and the creative continuity files included)."""
        collect = self._collect_dir()
        out = self._run(["git", "-c", "core.quotepath=false", "--no-optional-locks", "status",
                         "--porcelain", "-z", "--untracked-files=all"], "git status failed")
        paths, entries = [], out.split("\0")
        own = frozenset(self._OWN_OUTPUTS) | frozenset(self._creative_outputs())
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC":
                i += 1          # -z: a rename's source is the next entry
            path = entry[3:]
            if path.startswith(".agent/") or path in own or path.endswith(".coder.bak") or \
                    (collect is not None and path.startswith(collect)):
                continue
            paths.append(path)
        return paths

    def commit_existing_files(self) -> Optional[str]:
        """Commit the files a repository without a commit already holds, .agent/ aside, so a reset to HEAD
        has them to go back to; ``None`` when there is nothing to commit."""
        # an ignored path named in an exclude pathspec is an error of `git add`: exclude only what is not ignored
        keep_out = [] if self._agent_state_is_ignored() else [":(exclude).agent"]
        collect = self._collect_dir()
        if collect is not None and (self.repo_dir / collect).exists() and not self._is_ignored(collect):
            keep_out.append(f":(exclude){collect}")
        self._run(["git", "add", "-A", *(["--", ".", *keep_out] if keep_out else [])], "git add failed")
        if not self.has_staged_changes():
            return None
        self._run(["git", "commit", "-m", "chore: the files as they were before the auto-agent"],
                  "git commit of the existing files failed")
        return self.get_current_hash()

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

    def discard_working_changes(self) -> None:
        """Discard all uncommitted working-tree changes, restoring to HEAD.

        Resets tracked files to HEAD (``git reset --hard``) and removes
        untracked files (``git clean -fd``).  ``git clean`` without ``-x``
        respects ``.gitignore``, so internal agent state (``.agent/``) and
        coder backups (``*.coder.bak``) are preserved.

        Used to clean up after a task that failed/exhausted without a commit:
        without this, its half-finished edits stay dirty in the repo and get
        swept into the *next* successful task's ``stage_all()`` commit, since
        ``commit()`` stages everything (``git add -u && git add .``).  Between
        task commits the working tree is HEAD plus only the current task's
        edits, so resetting to HEAD discards exactly that task's residue and
        nothing already committed.

        No-op before the first commit (no HEAD to reset to).
        """
        if not self._has_head():
            return
        self._run(["git", "reset", "--hard", "HEAD"], "git reset --hard failed")

        # `git clean -fd` deletes untracked files, so .agent/ must be ignored; if the .gitignore write failed, this clean would delete the whole run.
        if not self._agent_state_is_ignored():
            logger.warning(
                "discard_working_changes: .agent/ is not covered by .gitignore "
                "— skipping `git clean -fd` to avoid deleting run state. "
                "Untracked residue is left in place; check that .gitignore is "
                "writable."
            )
            return
        collect = self._collect_dir()
        # an untracked collect artifact is an hour of Pass B, not a task's residue
        keep = ["-e", f"/{collect}"] if collect is not None else []
        self._run(["git", "clean", "-fd", *keep], "git clean failed")

    def _has_head(self) -> bool:
        """False only for a repository without a commit; any other failure raises ``GitError`` (a None from
        get_current_hash skipped the reset, and the failed task's edits went into the next commit)."""
        cmd = ["git", "rev-parse", "--verify", "-q", "HEAD"]
        try:
            proc = run_git(cmd, cwd=self.repo_dir, timeout=60, encoding="utf-8", errors="replace", retries=1)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise self._raise(cmd, "git rev-parse HEAD failed", exc) from exc
        if proc.returncode == 1 and not proc.stdout.strip():
            return False
        self._decode(cmd, "git rev-parse HEAD failed", proc)
        return True

    def _is_ignored(self, rel: str) -> bool:
        try:
            return run_git(["git", "check-ignore", "-q", rel], cwd=self.repo_dir, timeout=30, encoding="utf-8",
                           errors="replace", retries=1).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _agent_state_is_ignored(self) -> bool:
        """Return True only if git will actually ignore ``.agent/``.

        Uses ``git check-ignore``, which consults every ignore source (repo
        .gitignore, .git/info/exclude, global core.excludesFile) rather than
        trusting that ensure_gitignore_committed() succeeded.  Fails CLOSED:
        anything unexpected returns False, so a clean is skipped rather than
        risked.
        """
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", ".agent"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "_agent_state_is_ignored: check-ignore failed (%s) — assuming "
                "NOT ignored", exc,
            )
            return False

    def has_staged_changes(self) -> bool:
        """Return ``True`` if there is at least one staged change."""
        # Raise GitError on failure, not False: a false answer would silently skip the commit instead of showing that git is broken.
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                "git diff --cached --quiet timed out after 60s "
                "(possible stale .git/index.lock or hung hook)"
            ) from exc
        except OSError as exc:
            raise GitError(
                f"git diff --cached --quiet failed to run ({exc}) — is git "
                "installed and on PATH?"
            ) from exc
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

    def commit_paths(self, message: str, paths) -> Optional[str]:
        """Commit only *paths* (repo-relative or absolute under the repo) with
        *message*; ``None`` when none of them has a change (AUDIT-8).

        Nothing else in the tree or the index is touched: ``git commit --
        <paths>`` commits exactly those paths. Used for the files the agent
        itself rewrites right after a task's commit (synopsis.md, the story
        bible), so they land under that task's id instead of being swept into
        whichever task commits next.
        """
        repo = Path(self.repo_dir).resolve()
        rels: list[str] = []
        for raw in paths or ():
            p = Path(raw)
            if p.is_absolute():
                try:
                    p = p.resolve().relative_to(repo)
                except (ValueError, OSError):
                    continue
            rel = p.as_posix()
            if rel and rel not in rels and not rel.startswith(".agent/"):
                rels.append(rel)
        if not rels:
            return None
        changed = [
            rel for rel in rels
            if self._run(["git", "status", "--porcelain", "--", rel],
                         "git status failed").strip()
        ]
        if not changed:
            return None
        self._run(["git", "add", "-A", "--", *changed], "git add failed")
        self._run(["git", "commit", "-m", message, "--", *changed],
                  "git commit failed")
        sha = self._run(["git", "rev-parse", "HEAD"],
                        "git rev-parse HEAD failed").strip()
        logger.info("commit_paths: %s  (%s)", sha[:12], message)
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

    def diff_working_tree(self) -> str:
        """Every uncommitted change in the repo as one patch (B2).

        Used to keep a task's received work when its commit fails: a patch is
        the only copy that survives both the next task's ``stage_all()``
        (which would sweep the work in under another id) and an exhausted
        task's ``discard_working_changes()`` (which would delete it).

        Covers tracked edits, staged changes AND untracked new files — the
        coder's work is usually a new file, and a patch without it would save
        nothing. Untracked paths are diffed against ``/dev/null`` with
        ``--no-index``, whose non-zero exit is a normal "files differ" result,
        not an error; the path is passed *relative* so the headers come out as
        ``a/<path>`` / ``b/<path>`` and the patch applies from the repo root
        like any other.

        HARDEN-7 D: git's output is taken as BYTES and decoded with
        ``surrogateescape``, so every byte git printed survives into the
        returned string and back out of it. Decoding it as UTF-8 text instead
        turned each ``\\r\\n`` line end into ``\\n`` and each non-UTF-8 byte into
        U+FFFD, and ``git apply`` then refused the whole patch — one CRLF or
        latin-1 file anywhere in the tree sank the recovery of every other file
        too. The return type is still ``str`` for the existing callers; the
        surrogate escapes are the one encoding that round-trips losslessly
        through a ``str``.

        ``GitError`` propagates only when git itself could not be run: the
        caller treats a failed capture as "the work stays only in the working
        tree", never as an empty patch.
        """
        # No HEAD yet (a repo with no commit): the index diff is the whole patch.
        base = "HEAD" if self._has_head() else "--cached"
        parts = [self._run_bytes(["git", "diff", base, "--binary", "--"],
                                 "git diff failed").decode("utf-8", "surrogateescape")]
        collect = self._collect_dir()
        for path in self.untracked_paths():
            if collect is not None and path.startswith(collect):
                continue   # the discard keeps the collect artifact, and a patch that adds it again does not apply
            # --no-index exits 1 when the files differ, which is the normal case.
            proc = run_git(
                ["git", "diff", "--no-index", "--binary", "--",
                 os.devnull, path],
                cwd=self.repo_dir, timeout=120, text=False, retries=1,
            )
            if proc.returncode in (0, 1) and proc.stdout:
                parts.append(proc.stdout.decode("utf-8", "surrogateescape"))
        return "".join(parts)

    def untracked_paths(self) -> list[str]:
        """The untracked (and not ignored) paths in the repo (B2)."""
        out = self._run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            "git ls-files failed",
        )
        return [p for p in out.split("\0") if p.strip()]

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

    def paths_changed_in(self, sha: str) -> list[str]:
        """Return the repo-relative paths committed in *sha*.

        V9: ``commit()`` stages everything, so the paths a commit actually
        touched are the authoritative answer to "what did this task write" —
        more accurate than the task's declared ``target_files``, which can
        both under-report (the coder also fixed a helper) and over-report
        (a task that committed nothing staged).

        Fail-open: any git failure returns ``[]`` so a caller that marks
        these paths dirty never blinds more of a collect model than the
        commit really changed, and never raises.
        """
        if not sha:
            return []
        try:
            # -z: names as written. Without it git C-quotes non-ASCII names, so their collect facts were never marked stale.
            out = self._run(
                ["git", "diff-tree", "-z", "--root", "--no-commit-id", "--name-only", "-r", sha],
                "git diff-tree failed",
            )
        except GitError:
            return []
        return [name for name in out.split("\0") if name.strip()]

    def paths_changed_between(self, base: str, tip: str) -> list[str]:
        """Repo-relative paths changed in the ``base..tip`` range.

        V9: ``commit()`` returns the sha of a single commit, but the
        controller's post-commit regression loop can commit further fixes on
        top of it, so the tree state a later task sees is ``tip`` and not
        the original commit. Any git failure returns ``[]`` — see
        :meth:`paths_changed_in`.
        """
        if not base or not tip or base == tip:
            return []
        try:
            out = self._run(
                ["git", "diff", "-z", "--name-only", f"{base}..{tip}"],
                "git diff base..tip failed",
            )
        except GitError:
            return []
        return [name for name in out.split("\0") if name.strip()]       # -z: see paths_changed_in

    # ── Private ──────────────────────────────────────────────────────────────

    # Lock retries are shared with the other git callers through tools.git_run.run_git (see there for why a held lock is transient).
    _LOCK_CONTENTION_RE = LOCK_CONTENTION_RE
    _LOCK_RETRIES = LOCK_RETRIES
    _LOCK_BACKOFF_S = LOCK_BACKOFF_S

    def _run_bytes(self, cmd: list[str], error_msg: str) -> bytes:
        """One attempt at *cmd*, returning git's stdout as raw ``bytes``.

        HARDEN-7 D: the one caller that must reproduce git's output byte for
        byte — ``diff_working_tree``'s patch — cannot go through ``_run``,
        which decodes as UTF-8 with ``errors="replace"`` and so loses every
        ``\\r`` and every non-UTF-8 byte. Everything else about the call is
        ``_run``'s: the same held-index ladder, the same 60 s bound, the same
        ``GitError`` on a failure.
        """
        try:
            proc = run_git(
                cmd,
                cwd=self.repo_dir,
                timeout=60,
                text=False,
                retries=self._LOCK_RETRIES,
                backoff_s=self._LOCK_BACKOFF_S,
                sleep=self._backoff,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise self._raise(cmd, error_msg, exc) from exc
        if proc.returncode != 0:
            # Decoded for the message only: an error text is never the bytes
            # the caller was going to write out.
            shown = (proc.stderr or b"").decode("utf-8", "replace")
            raise GitError(
                f"{error_msg}\n"
                f"  cmd : {' '.join(cmd)}\n"
                f"  code: {proc.returncode}\n"
                f"  err : {shown.strip()}"
            )
        return proc.stdout or b""

    def _run(self, cmd: list[str], error_msg: str) -> str:
        """Run *cmd* inside *repo_dir*, capture output, raise on failure.

        The held-index retry is `tools.git_run.run_git` — see
        `_LOCK_CONTENTION_RE` above; this method only adds the `GitError` text.
        The class's own count and backoff are passed through rather than
        relying on `run_git`'s defaults, so overriding them here still works.
        """
        try:
            proc = run_git(
                cmd,
                cwd=self.repo_dir,
                timeout=60,
                encoding="utf-8",
                errors="replace",
                retries=self._LOCK_RETRIES,
                backoff_s=self._LOCK_BACKOFF_S,
                sleep=self._backoff,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise self._raise(cmd, error_msg, exc) from exc
        return self._decode(cmd, error_msg, proc)

    def _backoff(self, seconds: float) -> None:
        """Wait between two lock retries.

        A named seam rather than a bare ``time.sleep`` so a test can stand in
        for the wait without patching the ``time`` module for its whole
        process — which is both too broad to be safe and, under ``pytest -n``,
        a way to change behaviour a long way from the test doing it.
        `run_git` calls it through its *sleep* argument (FL-2).
        """
        time.sleep(seconds)

    def _run_once(self, cmd: list[str], error_msg: str) -> str:
        """One attempt at *cmd*, no waiting; `_run` is what decides to repeat."""
        try:
            proc = run_git(
                cmd,
                cwd=self.repo_dir,
                timeout=60,
                encoding="utf-8",
                errors="replace",
                retries=1,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise self._raise(cmd, error_msg, exc) from exc
        return self._decode(cmd, error_msg, proc)

    def _raise(self, cmd: list[str], error_msg: str, exc: Exception) -> GitError:
        """The `GitError` for an attempt that could not be made or did not finish.

        Timeout first: its message already names a stale `.git/index.lock` and a
        hung hook as the likely cause, which is how the operator finds it. A
        missing git binary or a vanished *repo_dir* must never escape as a bare
        OSError past callers that catch `GitError` only.
        """
        if isinstance(exc, subprocess.TimeoutExpired):
            return GitError(
                f"{error_msg} (timed out after 60s)\n"
                f"  cmd : {' '.join(cmd)}\n"
                "  Possible causes: stale .git/index.lock, a hung git hook, "
                "or an unresponsive remote."
            )
        return GitError(
            f"{error_msg} ({exc})\n"
            f"  cmd : {' '.join(cmd)}\n"
            "  Possible causes: git is not installed / not on PATH, or "
            f"repo_dir ({self.repo_dir!r}) does not exist or is not "
            "accessible."
        )

    def _decode(self, cmd: list[str], error_msg: str,
                proc: subprocess.CompletedProcess) -> str:
        """The stdout of a successful git, or the `GitError` for a failed one."""
        if proc.returncode != 0:
            raise GitError(
                f"{error_msg}\n"
                f"  cmd : {' '.join(cmd)}\n"
                f"  code: {proc.returncode}\n"
                f"  out : {proc.stdout.strip()}\n"
                f"  err : {proc.stderr.strip()}"
            )
        return proc.stdout


# Convenience factory, used by AutoController

def make_git_manager(
    repo_dir: str | Path,
    config: Optional[configparser.ConfigParser] = None,
) -> GitManager:
    """Create a :class:`GitManager`, ensure the repo exists, configure the
    agent identity, and guarantee a safety ``.gitignore`` is committed.

    This is the preferred entry-point for ``AutoController``.
    """
    gm = GitManager(repo_dir, config)
    gm.ensure_repo()
    gm.configure_identity()
    gm.ensure_gitignore_committed()
    return gm