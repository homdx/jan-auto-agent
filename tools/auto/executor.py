"""tools/auto/executor.py — AUTO-C1: Sandboxed task executor.

Provides a thin, testable runner that:

  1. Prepares a clean per-task workspace under ``.agent/workspace/<task_id>/``.
  2. Copies task target files into the workspace so the executed code runs
     against the version the Coder produced (not the live repo).
  3. Runs the task's ``acceptance_check`` shell command (or falls back to
     ``python <target>`` / ``pytest``) with a wall-clock timeout.
  4. Captures stdout, stderr, exit code, and a parsed traceback snippet.
  5. Returns a structured :class:`ExecutionResult` — never raises on
     subprocess failure, only on infra errors.

**No network** — the child process inherits an environment where
``http_proxy`` / ``https_proxy`` are cleared and ``PYTHONDONTWRITEBYTECODE``
is set, but otherwise the host environment is used.  A proper network
namespace would require root; the conservative approach here is to leave
full isolation to the CI environment and focus on reproducibility and
timeout enforcement.

Public surface consumed by the Coder loop (AUTO-C2/C3)::

    from tools.auto.executor import Executor, ExecutionResult

    executor = Executor(
        base_dir        = Path("."),          # repo root
        workspace_root  = Path(".agent/workspace"),
        timeout_sec     = 120,                # 0 = disabled (infinite hang risk — see below)
    )

    result: ExecutionResult = executor.run(task)
    # task is a dict with at least:
    #   acceptance_check  — shell command to run
    #   target_files      — list[str] of relative paths
    #   id                — task id (used as workspace sub-dir name)

    result.passed   # True  iff exit_code == 0
    result.exit_code
    result.stdout
    result.stderr
    result.timed_out
    result.traceback   # last traceback block extracted from stderr, or ""

Configuration (agents.ini [auto])
-----------------------------------
exec_timeout_sec   — per-execution wall-clock cap in seconds (default 120).
                     0 disables the timeout entirely — the subprocess runs
                     forever if it blocks (e.g. a script that prompts for
                     console input).  Only set to 0 intentionally.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer

logger = logging.getLogger(__name__)

# Environment variables that could give a subprocess network access that we
# want to suppress.  We clear these rather than block at the OS level.
_PROXY_VARS = (
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "ftp_proxy",  "FTP_PROXY",
    "all_proxy",  "ALL_PROXY",
    "no_proxy",   "NO_PROXY",
)

# Maximum number of characters captured from stdout / stderr before truncation.
# Prevents runaway output from filling memory.
_MAX_OUTPUT_CHARS = 64_000

# Maximum traceback snippet length returned in ExecutionResult.traceback.
_MAX_TRACEBACK_CHARS = 4_000

# Directories excluded when mirroring base_dir into a task workspace (see
# _prepare_workspace / AUTO-FIX-1): VCS metadata, the agent's own
# state/workspace tree — workspace_root defaults to base_dir/.agent/workspace,
# so excluding ".agent" also prevents shutil.copytree from recursing into its
# own destination — and common cache/dependency dirs that are large,
# irrelevant to acceptance checks, and safe to leave out (regenerable).
_MIRROR_IGNORE = shutil.ignore_patterns(
    ".git", ".agent", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".tox", "*.egg-info",
)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Structured result of one executor run.

    Attributes
    ----------
    exit_code:
        Return code of the child process.  ``-1`` when the process could
        not be started (infra error) or was killed by timeout.
    stdout:
        Captured standard output (may be truncated to ``_MAX_OUTPUT_CHARS``).
    stderr:
        Captured standard error (may be truncated).
    timed_out:
        ``True`` if the process was killed because it exceeded
        ``timeout_sec``.
    traceback:
        Last Python traceback block extracted from *stderr*, or ``""`` if
        none found.  Useful for feeding compact error context into the Coder.
    command:
        The shell command string that was executed (for logging/tracing).
    task_id:
        The task id this result belongs to.
    """

    exit_code: int = -1
    stdout:    str = ""
    stderr:    str = ""
    timed_out: bool = False
    traceback: str = ""
    command:   str = ""
    task_id:   str = ""

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def passed(self) -> bool:
        """``True`` iff the process exited with code 0 and did not time out."""
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        """One-line human-readable summary for logging / progress display."""
        if self.timed_out:
            return f"[{self.task_id}] TIMEOUT  cmd={self.command!r}"
        status = "PASS" if self.passed else f"FAIL(rc={self.exit_code})"
        return f"[{self.task_id}] {status}  cmd={self.command!r}"

    def to_dict(self) -> dict:
        """Serialisable dict — matches the schema the Coder loop expects."""
        return {
            "exit_code": self.exit_code,
            "stdout":    self.stdout,
            "stderr":    self.stderr,
            "timed_out": self.timed_out,
            "traceback": self.traceback,
            "command":   self.command,
            "task_id":   self.task_id,
            "passed":    self.passed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────────

class Executor:
    """Sandboxed runner for autonomous task acceptance checks.

    Parameters
    ----------
    base_dir:
        The repo root.  Target files are resolved relative to this path.
    workspace_root:
        Parent directory under which per-task subdirectories are created.
        Defaults to ``<base_dir>/.agent/workspace``.
    timeout_sec:
        Wall-clock limit for each subprocess invocation.  ``0`` disables
        the timeout — the child process can run forever (dangerous if the
        acceptance_check script blocks waiting for console input).  Any
        positive value is enforced; ``subprocess.TimeoutExpired`` is caught
        and mapped to ``ExecutionResult.timed_out = True``.
    python_bin:
        Python interpreter used when the acceptance check is a bare
        ``pytest`` or starts with ``python``.  Defaults to ``sys.executable``
        so the same interpreter that runs the agent is used.
    """

    def __init__(
        self,
        base_dir:       str | Path,
        workspace_root: Optional[str | Path] = None,
        timeout_sec:    float = 120,
        python_bin:     Optional[str] = None,
    ) -> None:
        self._base_dir      = Path(base_dir).resolve()
        self._workspace_root = (
            Path(workspace_root).resolve()
            if workspace_root is not None
            else self._base_dir / ".agent" / "workspace"
        )
        self._timeout_sec = max(0.0, float(timeout_sec))
        self._python_bin  = python_bin or sys.executable

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, task: dict) -> ExecutionResult:
        """Execute the acceptance check for *task* and return the result.

        The acceptance check is run inside a temporary per-task workspace
        directory.  If *task* has ``target_files``, those files are copied
        from ``base_dir`` into the workspace before the command runs so that
        the check exercises the files the Coder produced (not stale repo
        state).

        Parameters
        ----------
        task:
            A task dict (at minimum: ``id``, ``acceptance_check``,
            ``target_files``).

        Returns
        -------
        ExecutionResult
            Always returns — never raises on subprocess failures.

        Raises
        ------
        ValueError
            If *task* is missing the ``id`` field (programming error by
            the caller).
        """
        task_id = task.get("id", "").strip()
        if not task_id:
            raise ValueError("Executor.run(): task dict must have a non-empty 'id' field")

        acceptance_check = (task.get("acceptance_check") or "").strip()
        target_files     = task.get("target_files") or []

        # Build the workspace for this task.
        workspace = self._prepare_workspace(task_id, target_files)

        # Resolve the command to run.
        command = self._resolve_command(acceptance_check, target_files, workspace)

        # AUTO-CR-12: cross-platform no-op acceptance — creative/docs tasks
        # default acceptance_check to "true" (a Unix builtin), but on Windows
        # `true`/`false`/`:` aren't commands, so cmd.exe returns rc=1 and every
        # such task fails. Recognise only these Unix builtins and resolve them
        # without spawning a shell ("exit 0"/"exit 1" are valid everywhere and
        # run normally).
        _norm = (command or "").strip().lower().rstrip(";")
        if _norm in ("true", ":"):
            logger.info("executor run: task=%s no-op acceptance (%r) → pass", task_id, command)
            return ExecutionResult(exit_code=0, command=command, task_id=task_id)
        if _norm == "false":
            logger.info("executor run: task=%s no-op acceptance (%r) → fail", task_id, command)
            return ExecutionResult(exit_code=1, command=command, task_id=task_id)

        logger.info("executor run: task=%s cmd=%r cwd=%s", task_id, command, workspace)

        # Execute.
        result = self._execute(command, cwd=workspace, task_id=task_id)
        logger.info("executor result: %s", result.summary())
        return result

    def run_raw(self, command: str, cwd: Optional[Path] = None) -> ExecutionResult:
        """Run an arbitrary shell command, bypassing workspace setup.

        Useful for testing specific commands directly without a full task dict.

        Parameters
        ----------
        command:
            Shell command string to run.
        cwd:
            Working directory.  Defaults to ``base_dir``.

        Returns
        -------
        ExecutionResult
            task_id will be ``""`` since this is a raw run.
        """
        return self._execute(command, cwd=cwd or self._base_dir, task_id="")

    # ── Workspace setup ───────────────────────────────────────────────────────

    def _prepare_workspace(self, task_id: str, target_files: list[str]) -> Path:
        """Create the per-task workspace and populate it for acceptance_check.

        The workspace is ``<workspace_root>/<task_id>/``.  It is recreated
        fresh on each call so stale artefacts from previous attempts don't
        interfere.

        Population happens in two passes: first the whole repo (``base_dir``)
        is mirrored in, so files the acceptance_check needs but that aren't
        listed in ``target_files`` — pre-existing tests, conftest.py,
        fixtures, sibling modules — are present; then each entry in
        ``target_files`` is re-copied on top to guarantee the freshest
        on-disk version wins regardless of mirror timing.

        Parameters
        ----------
        task_id:
            Used as the subdirectory name.
        target_files:
            Repo-relative paths.  Files that don't exist in *base_dir* are
            skipped with a warning (they may be newly created by the Coder
            and not yet on disk).

        Returns
        -------
        Path
            The workspace directory (guaranteed to exist).
        """
        workspace = self._workspace_root / _safe_dir_name(task_id)
        # Wipe and recreate for a clean run.
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        # AUTO-FIX-1: mirror the whole repo into the workspace *before*
        # overlaying target_files. Previously only target_files were copied,
        # so an acceptance_check referencing a file that exists in base_dir
        # but wasn't listed in target_files (a pre-existing test file,
        # conftest.py, a fixture, a sibling module the target imports) would
        # fail with a spurious "file or directory not found" — a false
        # negative unrelated to whether the Coder's change was correct, and
        # one that (via TaskRewriter's failure-pattern handling) could lead
        # to acceptance_check being silently replaced with a no-op. Best
        # effort: mirroring failures are logged, not raised, so the run
        # continues with the pre-existing target-files-only behaviour below
        # rather than aborting the task outright.
        try:
            shutil.copytree(
                self._base_dir, workspace,
                ignore=_MIRROR_IGNORE, dirs_exist_ok=True, symlinks=True,
            )
        except (OSError, shutil.Error) as exc:
            logger.warning(
                "_prepare_workspace: could not fully mirror %s into %s (%s) — "
                "acceptance_check may still fail on files outside target_files",
                self._base_dir, workspace, exc,
            )

        tracer.event(
            source="executor",
            target="workspace",
            kind="phase_transition",
            params={
                "phase":      "files_preparing",
                "status":     "started",
                "task":       task_id,
                "file_count": len(target_files),
                "files":      target_files,
            },
        )

        copied: list[str] = []
        missing: list[str] = []
        for rel in target_files:
            src = self._base_dir / rel
            if not src.exists():
                logger.debug(
                    "_prepare_workspace: %r not found in base_dir — skipping copy", rel
                )
                missing.append(rel)
                continue
            dst = workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.debug("_prepare_workspace: copied %s → %s", src, dst)
            copied.append(rel)

        tracer.event(
            source="executor",
            target="workspace",
            kind="phase_transition",
            params={
                "phase":         "files_preparing",
                "status":        "done",
                "task":          task_id,
                "files_copied":  len(copied),
                "files_missing": len(missing),
                "copied":        copied,
                "missing":       missing,
            },
        )

        return workspace

    # ── Command resolution ────────────────────────────────────────────────────

    # Shell tokens that are never safe in an LLM-generated acceptance_check.
    # This is a defence-in-depth blocklist, not a complete sandbox — the check
    # catches obvious mistakes/injections from a hallucinating model.
    _BLOCKED_COMMAND_PATTERNS: tuple[str, ...] = (
        "rm ",  "rm\t",              # file deletion (rm -rf /, rm -rf ~, …)
        "rmdir",                     # directory removal
        "sudo",                      # privilege escalation
        "chmod", "chown",            # permission changes
        "dd ",  "dd\t",              # disk writes (dd if=/dev/zero …)
        "mkfs",                      # filesystem formatting
        "> /",  ">/",                # redirect to absolute path
        "curl ", "wget ",            # outbound network (proxy-bypass, exfil)
        "nc ", "netcat",             # raw network
        ":(){:|:&};:",               # fork bomb
        "shutdown", "reboot", "halt",# system control
        "systemctl",                 # service control
    )

    @staticmethod
    def _check_command_safety(command: str) -> tuple[bool, str]:
        """Return *(safe, reason)* — ``safe=False`` blocks execution.

        Scans for shell tokens that should never appear in an acceptance-check
        command.  This is a best-effort defence against an LLM-generated
        command that would delete files, escalate privileges, or exfiltrate data.
        It does NOT replace a proper sandbox; it catches common cases fast.
        """
        import re as _re
        lower = command.lower()
        for pattern in Executor._BLOCKED_COMMAND_PATTERNS:
            token = pattern.strip().lower()
            if token.isalpha():
                # Whole-word match (mirrors coder.py's _check_content_safety):
                # plain substring containment falsely flagged ordinary commands
                # — 'rm ' matched 'terrafo[rm ]', 'nc ' matched 'rsy[nc ]',
                # 'rm ' matched 'confi[rm ]'.  Python \b treats '_' as a word
                # char, so identifier-style checks stay safe too.
                if _re.search(r"\b" + _re.escape(token) + r"\b", lower):
                    return False, f"blocked pattern {pattern!r} in acceptance_check"
            elif pattern in lower:
                # Non-word tokens (shell metachars: '> /', '>/', fork bomb) have
                # no word boundary to anchor on — keep the substring check.
                return False, f"blocked pattern {pattern!r} in acceptance_check"
        return True, ""

    def _resolve_command(
        self,
        acceptance_check: str,
        target_files: list[str],
        workspace: Path,
    ) -> str:
        """Return the shell command string to execute.

        Rules (in priority order):

        1. If *acceptance_check* is non-empty, validate it and use it.
        2. If *target_files* has exactly one ``.py`` file, fall back to
           ``python <file>``.
        3. Otherwise fall back to ``pytest`` (runs the full test suite).

        The ``python`` token at the start of a command is rewritten to use
        ``self._python_bin`` so the same interpreter as the agent is used.

        Additionally, if the first argument of the acceptance_check is a bare
        filename (no directory separator) and that basename matches the basename
        of exactly one target file, the bare name is replaced with the full
        workspace-relative path.  This fixes the case where the Architect
        writes ``bash generateAllureReport.sh`` but the file was copied into a
        subdirectory (e.g. ``dockerfiles/allure-generator/generateAllureReport.sh``).
        """
        if acceptance_check:
            safe, reason = self._check_command_safety(acceptance_check)
            if not safe:
                logger.error(
                    "_resolve_command: [SAFETY] %s — falling back to pytest", reason
                )
                return "pytest"
            resolved = self._resolve_bare_filename(acceptance_check, target_files)
            return self._rewrite_python(resolved)

        py_files = [f for f in target_files if f.endswith(".py")]
        if len(py_files) == 1:
            return f"{self._python_bin} {py_files[0]}"

        return "pytest"

    @staticmethod
    def _resolve_bare_filename(command: str, target_files: list[str]) -> str:
        """Replace a bare filename argument with the matching target-file path.

        When the acceptance_check's *first non-flag token* is a plain filename
        (contains no ``/`` or ``\\``), look for a target file whose basename
        matches it.  If exactly one match is found, rewrite that token to the
        full relative path so the command resolves correctly inside the
        workspace (where files land at their repo-relative sub-paths, not at
        the workspace root).

        Example::

            command      = "bash generateAllureReport.sh"
            target_files = ["dockerfiles/allure-generator/generateAllureReport.sh"]
            → returns   "bash dockerfiles/allure-generator/generateAllureReport.sh"

        No rewrite is done when:
        - The token already contains a path separator (already qualified).
        - Zero or multiple target files share that basename (ambiguous).
        - The command has no recognisable bare-filename token.
        """
        try:
            parts = shlex.split(command)
        except ValueError:
            return command  # un-parseable shell quoting — leave untouched

        # Walk parts to find the first *argument* token that looks like a plain
        # filename (not a flag and not already path-qualified).  idx==0 is the
        # executable itself (bash, python, …) — always skip it.
        _SHELL_OPS = frozenset({"&&", "||", ";", "|", "&"})
        for idx, part in enumerate(parts):
            if idx == 0:
                continue  # executable — never a filename target
            if part.startswith("-"):
                continue  # flag — skip
            if "/" in part or "\\" in part:
                return command  # already has a path component — nothing to do
            if part in _SHELL_OPS:
                return command  # compound command — stop scanning; no safe rewrite
            # `part` is a bare argument token.  Check whether its basename
            # matches exactly one target file.
            basename = part
            matches = [tf for tf in target_files if Path(tf).name == basename]
            if len(matches) == 1:
                full_path = matches[0]
                if "&&" in command or "||" in command or ";" in command:
                    # The original command contains shell operators; shlex.join
                    # would misquote them.  Do a safe word-boundary string
                    # substitution on the original command string instead.
                    import re as _re_local
                    rewritten = _re_local.sub(
                        r"(?<!\S)" + _re_local.escape(basename) + r"(?!\S)",
                        full_path,
                        command,
                        count=1,
                    )
                else:
                    parts[idx] = full_path
                    rewritten = shlex.join(parts)
                logger.debug(
                    "_resolve_bare_filename: rewrote %r → %r (matched target %r)",
                    command, rewritten, full_path,
                )
                return rewritten
            # Zero or multiple matches — continue scanning subsequent tokens.

        return command

    def _rewrite_python(self, command: str) -> str:
        """Replace a leading ``python`` / ``python3`` token with the real interpreter path.

        This ensures ``python -m pytest`` runs under the agent's own venv
        rather than whatever ``python`` resolves to in the PATH.
        """
        stripped = command.lstrip()
        for token in ("python3 ", "python "):
            if stripped.startswith(token):
                return self._python_bin + " " + stripped[len(token):]
        if stripped in ("python", "python3"):
            return self._python_bin
        return command

    # ── Subprocess execution ──────────────────────────────────────────────────

    def _execute(self, command: str, *, cwd: Path, task_id: str) -> ExecutionResult:
        """Run *command* as a shell subprocess and return a structured result.

        Parameters
        ----------
        command:
            Shell command string.
        cwd:
            Working directory for the child process.
        task_id:
            Attached to the returned :class:`ExecutionResult`.

        Returns
        -------
        ExecutionResult
            Always returns — subprocess.TimeoutExpired and OSError are caught
            and mapped to the result fields.
        """
        env = self._build_env()
        # Use explicit comparison instead of `or None` — 0.0 is falsy in Python,
        # so `0.0 or None` would silently mean "no timeout" instead of
        # "immediate timeout," tricking anyone who sets exec_timeout_sec = 0
        # expecting one behavior into getting the other with no warning. The
        # explicit form below makes the intent unmistakable.
        timeout = self._timeout_sec if self._timeout_sec > 0 else None

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            stdout = _truncate(proc.stdout, _MAX_OUTPUT_CHARS)
            stderr = _truncate(proc.stderr, _MAX_OUTPUT_CHARS)
            return ExecutionResult(
                exit_code = proc.returncode,
                stdout    = stdout,
                stderr    = stderr,
                timed_out = False,
                traceback = _extract_traceback(stderr),
                command   = command,
                task_id   = task_id,
            )

        except subprocess.TimeoutExpired as exc:
            # The process was killed; collect whatever partial output we have.
            stdout = _truncate(
                (exc.stdout or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                _MAX_OUTPUT_CHARS,
            )
            stderr = _truncate(
                (exc.stderr or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                _MAX_OUTPUT_CHARS,
            )
            logger.warning(
                "_execute: TIMEOUT task=%s cmd=%r timeout=%.1fs",
                task_id, command, self._timeout_sec,
            )
            return ExecutionResult(
                exit_code = -1,
                stdout    = stdout,
                stderr    = stderr,
                timed_out = True,
                traceback = _extract_traceback(stderr),
                command   = command,
                task_id   = task_id,
            )

        except OSError as exc:
            # Shell not found, permission error, etc.
            logger.error(
                "_execute: OSError task=%s cmd=%r: %s", task_id, command, exc
            )
            return ExecutionResult(
                exit_code = -1,
                stdout    = "",
                stderr    = str(exc),
                timed_out = False,
                traceback = "",
                command   = command,
                task_id   = task_id,
            )

    def _build_env(self) -> dict[str, str]:
        """Return a child-process environment with proxy vars cleared."""
        env = os.environ.copy()
        for var in _PROXY_VARS:
            env.pop(var, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # Force UTF-8 for all child processes — Windows' default cp1252 codec
        # can't decode bytes like 0x81. PYTHONUTF8=1 enables UTF-8 mode (PEP
        # 540, Python >= 3.7); PYTHONIOENCODING is the legacy fallback for
        # older builds.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        
        base_dir_str = str(self._base_dir)
        existing_pythonpath = env.get("PYTHONPATH", "")
        if existing_pythonpath:
            env["PYTHONPATH"] = f"{base_dir_str}{os.pathsep}{existing_pythonpath}"
        else:
            env["PYTHONPATH"] = base_dir_str
            
        return env


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_dir_name(name: str) -> str:
    """Strip path-traversal characters from *name* before use as a directory component.

    Keeps only alphanumeric chars, hyphens, and underscores so a task id
    like ``"../../evil"`` cannot escape the workspace root.
    """
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_\-]", "_", name)
    return safe.strip("_") or "task"


def _truncate(text: str, max_chars: int) -> str:
    """Return *text* truncated to *max_chars*, appending a notice if cut."""
    if len(text) <= max_chars:
        return text
    notice = f"\n... [truncated — {len(text) - max_chars} chars omitted]"
    return text[:max_chars] + notice


# Match a Python traceback block: "Traceback (most recent call last):" through
# the final exception line.
_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE)


def _extract_traceback(stderr: str) -> str:
    """Return the *last* Python traceback block from *stderr*, or ``""``.

    Extracts from "Traceback (most recent call last):" to the end of the
    exception text.  When multiple tracebacks appear (e.g. chained exceptions)
    the last one is returned because it is most likely the root cause visible
    to the user.

    The result is capped at ``_MAX_TRACEBACK_CHARS`` to keep context compact.
    """
    if not stderr:
        return ""

    matches = list(_TRACEBACK_START.finditer(stderr))
    if not matches:
        return ""

    last_match = matches[-1]
    snippet = stderr[last_match.start():]
    return _truncate(snippet.rstrip(), _MAX_TRACEBACK_CHARS)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory — used by AutoController (AUTO-C3)
# ─────────────────────────────────────────────────────────────────────────────

def make_executor(
    base_dir:       str | Path,
    workspace_root: Optional[str | Path] = None,
    timeout_sec:    float = 120,
    python_bin:     Optional[str] = None,
) -> Executor:
    """Create and return an :class:`Executor` with the given configuration.

    Parameters
    ----------
    base_dir:
        Repo root.
    workspace_root:
        Defaults to ``<base_dir>/.agent/workspace``.
    timeout_sec:
        From ``RunLimits.exec_timeout_sec`` (``agents.ini [auto] exec_timeout_sec``).
    python_bin:
        Python interpreter binary path.  Defaults to ``sys.executable``.
    """
    return Executor(
        base_dir       = base_dir,
        workspace_root = workspace_root,
        timeout_sec    = timeout_sec,
        python_bin     = python_bin,
    )