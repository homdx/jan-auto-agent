"""tools/auto/controller.py — AUTO-A1 / AUTO-A2 / AUTO-A4: Autonomous mode controller.

Entry point for the autonomous improvement mode.  Provides the public surface
that main.py imports:

    from tools.auto.controller import AutoController, run_auto
  * Resumable: a subsequent run reloads state and continues from the last
    unfinished task, respecting caps anew.

agents.ini [auto] keys (AUTO-A4)
---------------------------------
max_runtime_min   — wall-clock cap in minutes (float; 0 = disabled)
max_tasks_per_run — maximum tasks to execute this session (int; 0 = disabled)

-----------------------------------
task_mode — domain mode: code (default) | docs | creative
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
import time
from tools.auto.utils import _ts
from pathlib import Path
from typing import Callable, Optional

from tools.auto.state import StateStore, STATUS_DONE, STATUS_BLOCKED, STATUS_TODO
from tools.auto.git_manager import make_git_manager, DirtyTreeError, GitError, GitManager
from tools.auto.commit_on_success import (
    COMMIT_GIT_ERROR,
    COMMIT_NOTHING_TO_COMMIT,
    COMMIT_OK,
    CommitResult,
)
from tools.auto.utils import highest_completed_round, safe_filename_component, atomic_write_text
# make_outer_loop is imported inside _run_task_loop, so tests can patch it at its source module.

# Epic G Integrations
from tools.auto.run_trace import setup_run_trace
from tools.auto.progress_display import ProgressDisplay, make_progress_display
from tools.auto.auto_metrics import AutoMetricsStream
from tools.auto.auto_tuner import AutoTuner, make_auto_tuner
from tools.config_safe import safe_getboolean

logger = logging.getLogger(__name__)

# ── Stop-reason constants (written to progress.json) ────────────────────────
STOP_RUNTIME_CAP = "runtime_cap"
STOP_TASK_CAP    = "task_cap"


# RunLimits: the two caps

class RunLimits:
    """Carries the wall-clock and task caps for one autonomous session.

    Parameters
    ----------
    max_runtime_sec:
        Maximum wall-clock seconds for this run.  ``0`` (or negative) means
        no cap.
    max_tasks_per_run:
        Maximum number of tasks to execute in this session.  ``0`` (or
        negative) means no cap.
    exec_timeout_sec:
        Per-execution wall-clock timeout (seconds) handed to the executor
        (AUTO-C1) when it runs generated code / acceptance checks.  This is an
        *execution constraint* (how long a single ``python``/test run may take),
        distinct from the whole-session ``max_runtime_sec`` cap.  ``0`` (or
        negative) means no per-execution timeout.  Default is 120s.
    workspace_retain_count:
        Max number of per-task ``.agent/workspace/<task_id>/`` mirrors (each
        a full repo copy, see executor.py AUTO-FIX-1) kept on disk at once.
        Without this bound, a long auto-mode run (AUTO-T1, AUTO-T2, ... plus
        AUTO-G5 regression re-checks) fills the disk with one full repo copy
        per task.  ``0`` disables pruning.  Default is 5.
    """

    def __init__(
        self,
        max_runtime_sec: float = 0,
        max_tasks_per_run: int = 0,
        exec_timeout_sec: float = 120,
        workspace_retain_count: int = 5,
    ) -> None:
        self.max_runtime_sec   = max(0.0, float(max_runtime_sec))
        self.max_tasks_per_run = max(0, int(max_tasks_per_run))
        self.exec_timeout_sec  = max(0.0, float(exec_timeout_sec))
        self.workspace_retain_count = max(0, int(workspace_retain_count))

    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> "RunLimits":
        """Read limits from a ``ConfigParser`` instance ([auto] section).

        AUTO-FIX (medium-priority audit): a present-but-malformed value
        (e.g. ``max_runtime_min = abc``) used to raise a bare ValueError
        out of configparser — fallback= only covers an absent key, not a
        malformed one. Wrapped per-call (not delegated to a shared helper)
        so `tools.collect.ast_facts.extract_config_reads` (COLLECT-5)
        still recognizes each literal `config.getX(section, key,
        fallback=...)` call site — that AST-shape matching is how the
        `collect` static-analysis pass builds its config map, and a
        wrapper function call would have made these reads invisible to it.
        """
        try:
            max_min = config.getfloat("auto", "max_runtime_min", fallback=0)
        except ValueError as exc:
            logger.warning("config [auto] max_runtime_min invalid (%s) — using 0", exc)
            max_min = 0
        try:
            max_tasks = config.getint("auto", "max_tasks_per_run", fallback=0)
        except ValueError as exc:
            logger.warning("config [auto] max_tasks_per_run invalid (%s) — using 0", exc)
            max_tasks = 0
        try:
            exec_to = config.getfloat("auto", "exec_timeout_sec", fallback=120)
        except ValueError as exc:
            logger.warning("config [auto] exec_timeout_sec invalid (%s) — using 120", exc)
            exec_to = 120
        try:
            ws_retain = config.getint("auto", "workspace_retain_count", fallback=5)
        except ValueError as exc:
            logger.warning("config [auto] workspace_retain_count invalid (%s) — using 5", exc)
            ws_retain = 5
        return cls(
            max_runtime_sec   = max_min * 60,
            max_tasks_per_run = max_tasks,
            exec_timeout_sec  = exec_to,
            workspace_retain_count = ws_retain,
        )

    @property
    def runtime_capped(self) -> bool:
        """``True`` if a wall-clock cap is active (non-zero)."""
        return self.max_runtime_sec > 0

    @property
    def task_capped(self) -> bool:
        """``True`` if a task cap is active (non-zero)."""
        return self.max_tasks_per_run > 0

    @property
    def exec_timeout_active(self) -> bool:
        """``True`` if a per-execution timeout is active (non-zero)."""
        return self.exec_timeout_sec > 0

    def __repr__(self) -> str:
        return (
            f"RunLimits(max_runtime_sec={self.max_runtime_sec}, "
            f"max_tasks_per_run={self.max_tasks_per_run}, "
            f"exec_timeout_sec={self.exec_timeout_sec})"
        )


# Startup config lint: guards AUTO-CR-19-1 from regressing

# Legacy system-prompt keys that need a mode-specific override form (AUTO-CR-19-1).
_CR19_LEGACY_SYSTEM_KEYS = (
    ("validator_agent", "system"),
    ("coder", "system"),
    ("architect", "system"),
)

# Below this base num_ctx the 4k-era prompt budgets apply (see agents_4k.ini).
_CR19_SMALL_NUM_CTX_THRESHOLD = 8192


def _lint_mode_config(config: "configparser.ConfigParser | None", task_mode: str) -> list[str]:
    """AUTO-CR-19-4: warn at startup about config traps for non-code modes.

    Specifically guards against the exact regression AUTO-CR-19-1 fixed: a
    code-specific legacy ``system`` override (``[validator_agent]``,
    ``[coder]``, ``[architect]``) silently winning over the builtin prompt in
    a non-code ``task_mode`` because no matching ``system_{task_mode}``
    override was added alongside it. Also flags an ``agents_4k.ini``-style
    small ``num_ctx`` left in place for a ``creative`` run with no
    ``[coder] num_ctx_creative`` override to compensate.

    Pure logging — returns the list of warning strings it logged (for
    testability) and never raises or changes behaviour. No-op for
    ``task_mode == "code"`` since the legacy keys are exactly the ones code
    mode is supposed to use.
    """
    warnings: list[str] = []
    if config is None or task_mode == "code":
        return warnings

    mode_key = f"system_{task_mode}"
    for section, key in _CR19_LEGACY_SYSTEM_KEYS:
        legacy_val = config.get(section, key, fallback="").strip()
        if not legacy_val:
            continue
        override_val = config.get(section, mode_key, fallback="").strip()
        if override_val:
            continue
        warnings.append(
            f"[{section}] {key} is code-specific but task_mode={task_mode}; "
            f"set {mode_key} or rely on the builtin — the bare key is ignored "
            f"in {task_mode} mode (AUTO-CR-19-1)."
        )

    if task_mode == "creative":
        active_profile = config.get("api", "active", fallback="local")
        try:
            base_num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        except ValueError:
            base_num_ctx = 0
        num_ctx_creative = config.get("coder", "num_ctx_creative", fallback="").strip()
        if not num_ctx_creative and 0 < base_num_ctx < _CR19_SMALL_NUM_CTX_THRESHOLD:
            warnings.append(
                f"api_{active_profile} num_ctx={base_num_ctx} looks like an "
                "agents_4k.ini-style small context window for task_mode=creative, "
                "and no [coder] num_ctx_creative override is set; creative prompts "
                "(architect + coder + prefetched context) tend to need more room — "
                "consider agents_32k.ini or setting num_ctx_creative explicitly."
            )

    for w in warnings:
        logger.warning("controller: %s", w)
    return warnings


def _lint_probe_config(config: "configparser.ConfigParser | None") -> list[str]:
    """AUTO-P3: warn at startup when [architect] probe_enabled is on but the
    run cannot actually use it.

    Deliberately separate from ``_lint_mode_config`` rather than folded into
    it: that function returns early for ``task_mode == "code"`` (its own
    checks are about non-code modes only), and the probe traps apply to code
    mode most of all, since that is where the Architect does the bulk of its
    planning.

    Pure logging — returns the list of warning strings it logged (for
    testability) and never raises or changes behaviour.
    """
    warnings: list[str] = []
    if config is None:
        return warnings
    if not safe_getboolean(config, "architect", "probe_enabled", fallback=False):
        return warnings

    if not safe_getboolean(config, "collect", "use_in_auto", fallback=False):
        warnings.append(
            "[architect] probe_enabled = true but [collect] use_in_auto is "
            "false — the probe resolves facts from the collect model, so with "
            "no artifact to read it can never answer and every probe will "
            "fall straight through to the forced final call. Enable "
            "[collect] use_in_auto or turn probe_enabled back off."
        )

    active_profile = config.get("api", "active", fallback="local")
    try:
        base_num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
    except ValueError:
        base_num_ctx = 0
    if 0 < base_num_ctx < _CR19_SMALL_NUM_CTX_THRESHOLD:
        warnings.append(
            f"[architect] probe_enabled = true with api_{active_profile} "
            f"num_ctx={base_num_ctx} — a probe round appends a digest to an "
            "already-full architect prompt, and an agents_4k.ini-sized window "
            "has no room for it. Use a larger profile or leave probing off."
        )

    for w in warnings:
        logger.warning("controller: %s", w)
    return warnings


# AutoController

class AutoController:
    """Orchestrates an autonomous improvement run.

    Parameters
    ----------
    goal:
        A non-empty string describing what the agent should achieve,
        e.g. ``"improve current code"``.
    base_dir:
        Absolute (or relative) path to the project root that will be
        reviewed and modified.  Must exist.
    config_path:
        Path to ``agents.ini`` (default ``"agents.ini"``).
    _time_fn:
        Callable returning the current monotonic time in seconds.
        Defaults to ``time.monotonic``.  Intended for testing only — allows
        tests to fake elapsed time without sleeping.

    Raises
    ------
    ValueError
        If *goal* is empty.
    FileNotFoundError
        If *base_dir* does not exist.
    """

    def __init__(
        self,
        goal: str,
        base_dir: str | os.PathLike = ".",
        config_path: str = "agents.ini",
        dry_run: bool = False,
        skill: Optional[str] = None,
        _time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        goal = goal.strip() if goal else ""
        if not goal:
            raise ValueError("AutoController requires a non-empty goal string.")

        base_path = Path(base_dir).resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_dir does not exist: {base_path}")

        self.goal        = goal
        self.base_dir    = base_path
        self.config_path = config_path
        self.dry_run     = dry_run
        self.agent_dir   = base_path / ".agent"

        # task_mode and config are read once here; a nonexistent config path means defaults (tests rely on it); main.py catches real typos.
        self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        # A config file that exists but does not parse falls back to defaults, like a missing one (AUTO-FIX).
        if Path(config_path).exists():
            try:
                self.config.read(config_path, encoding="utf-8")
            except configparser.Error as exc:
                logger.warning(
                    "AutoController: %s is malformed (%s) — proceeding "
                    "with default settings", config_path, exc,
                )
        # Normalise task_mode (typo-tolerant), then apply the skill overlay: a skill is a config overlay, so it must come first (SKILLS-1).
        self.skill = None
        if skill:
            from tools.skills import SkillError, apply_skill
            try:
                self.skill = apply_skill(self.config, skill, base_dir=self.base_dir)
            except SkillError as exc:
                # A skill named explicitly must not silently fall back to running without it: fail loud.
                raise ValueError(f"--skill {skill!r}: {exc}") from exc

        from tools.auto.utils import normalize_task_mode
        _raw_mode = self.config.get("auto", "task_mode", fallback="code")
        self.task_mode, _mode_warn = normalize_task_mode(_raw_mode)
        if _mode_warn:
            logger.warning("controller: %s", _mode_warn)
        # Lint for a code-specific legacy system override left in a non-code mode (AUTO-CR-19-4); logging only.
        _lint_mode_config(self.config, self.task_mode)
        _lint_probe_config(self.config)
        # Always-on startup line stating whether probing is enabled, so its absence later reads as a real negative (AUTO-DEBUG-1).
        _probe_enabled = safe_getboolean(self.config, "architect", "probe_enabled", fallback=False)
        _use_in_auto = safe_getboolean(self.config, "collect", "use_in_auto", fallback=False)
        logger.info(
            "controller: run flags — task_mode=%s dry_run=%s "
            "[architect] probe_enabled=%s  [collect] use_in_auto=%s",
            self.task_mode, self.dry_run, _probe_enabled, _use_in_auto,
        )
        # AUTO-A4: execution working dir (executor/AUTO-C1 runs code here)
        self.workspace_dir = self.agent_dir / "workspace"

        # AUTO-A4: monotonic clock — injectable for unit tests
        self._time_fn: Callable[[], float] = _time_fn or time.monotonic

        # AUTO-A4: load run limits from agents.ini (reuse the already-parsed config)
        self.limits = RunLimits.from_config(self.config)

        # AUTO-A2: StateStore owns all .agent/ I/O
        self.state = StateStore(self.agent_dir)

        # AUTO-A3: git manager wired at run start (None until run())
        self.git = None

        # Set at the start of run(); used by is_runtime_exceeded()
        self._start_time: float = 0.0
        
        # Epic G sub-systems: Initialized as None to prevent test crashes
        self.run_trace = None
        self.progress_display: Optional[ProgressDisplay] = None
        self.metrics_stream: Optional[AutoMetricsStream] = None
        self.auto_tuner: Optional[AutoTuner] = None

    # ── Run limits API (AUTO-A4) ─────────────────────────────────────────────

    def is_runtime_exceeded(self) -> bool:
        """Return ``True`` if the wall-clock cap has been reached.

        Always returns ``False`` when no cap is configured
        (``limits.max_runtime_sec == 0``).
        """
        if not self.limits.runtime_capped:
            return False
        elapsed = self._time_fn() - self._start_time
        return elapsed >= self.limits.max_runtime_sec

    def is_task_cap_reached(self, tasks_done: int) -> bool:
        """Return ``True`` if *tasks_done* has reached the per-run task cap.

        Always returns ``False`` when no cap is configured
        (``limits.max_tasks_per_run == 0``).
        """
        if not self.limits.task_capped:
            return False
        return tasks_done >= self.limits.max_tasks_per_run

    def check_caps(self, tasks_done: int) -> Optional[str]:
        """Return the stop-reason string if any cap is exceeded, else ``None``.

        Evaluates runtime cap first (lowest-cost check).

        Parameters
        ----------
        tasks_done:
            Number of tasks fully executed so far in this session.

        Returns
        -------
        str or None
            ``"runtime_cap"``, ``"task_cap"``, or ``None``.
        """
        if self.is_runtime_exceeded():
            return STOP_RUNTIME_CAP
        if self.is_task_cap_reached(tasks_done):
            return STOP_TASK_CAP
        return None

    def elapsed_seconds(self) -> float:
        """Return seconds elapsed since :meth:`run` was called."""
        return self._time_fn() - self._start_time

    # Public run API

    def run(self) -> int:
        """Execute the autonomous run and return an exit code (0 = success).

        Lifecycle
        ---------
        1. Print start banner.
        2. Initialise (fresh) or resume (existing) state.
        3. Enter task loop — check caps before every task.
        4. On cap: persist stop_reason, log, exit 0 (graceful stop).
        5. On normal completion: set status "idle", exit 0.
        """
        self._start_time = self._time_fn()
        self._print_banner()

        # AUTO-A2: MUST initialise state first so .agent/ exists
        is_fresh = self.state.initialise(self.goal, self.base_dir)

        cfg = self._cfg()

        # ── Epic G Initialization ─────────────────────────────────────────

        # AUTO-F2: Configure the run tracer singleton
        self.run_trace = setup_run_trace(self.state, cfg)
        self.run_trace.log_run_start(self.goal, self.base_dir)

        # AUTO-F1: Setup progress display
        self.progress_display = make_progress_display(self.state, cfg, task_mode=self.task_mode)
        self.progress_display.code_total = len(self.state.all_tasks())

        # Metrics are optional: a failure to set them up is logged and the run continues without them (JAN-BUG-04).
        try:
            self.metrics_stream = AutoMetricsStream(self.agent_dir)
        except Exception as exc:
            logger.warning(
                "AutoMetricsStream unavailable (%s) — continuing this run "
                "with metrics recording disabled", exc,
            )
            self.metrics_stream = None
        self.auto_tuner = make_auto_tuner(cfg, self.agent_dir)

        # ──────────────────────────────────────────────────────────────────

        # AUTO-A3: ensure the target folder is a git repo with agent identity.
        try:
            self._setup_git()
        except DirtyTreeError as exc:
            print(f"⛔ --auto refused: {exc}", flush=True)
            self.state.log(f"refused: {exc}")
            return 2

        # Blocked tasks from a prior session go back to TODO so their dependencies are re-checked this session.
        self._reset_resettable_blocked_tasks(cfg)

        resume_info = self.state.resume_info()
        if not is_fresh:
            self._print_resume_summary(resume_info)

        # AUTO-A4: log active limits at run start
        self._log_limits()

        # Update progress to "running"
        self.state.update_progress(status="running")

        # Delegates to pipeline.run_pipeline (plan, then execute); the controller stays thin and testable (AUTO-G0).
        from tools.auto.pipeline import run_pipeline
        stop_reason, session_tasks_done = run_pipeline(self)

        # ── Finalise ──────────────────────────────────────────────────────
        self._log_auto_prompts()
        if stop_reason:
            self._handle_cap(stop_reason, session_tasks_done)
        else:
            self.state.update_progress(status="idle")
            self.state.log("run finished cleanly")
            if self.run_trace:
                self.run_trace.log_run_finished()

        return 0

    # Private helpers

    def _run_task_loop(
        self,
        *,
        task_mode: str = "code",
        cfg: Optional[configparser.ConfigParser] = None,
    ) -> tuple[Optional[str], int]:
        """Iterate pending tasks, check caps, execute via outer_loop, return stop reason.

        AUTO-G2: replaces the inner_loop skeleton with the real pipeline:
            outer_loop.run_task(task) → passed   → commit_on_success  (G3)
                                      → exhausted → exhaustion_handler (G4)
        A4 caps, dependency guard, progress display, run_trace, and auto_tuner
        wiring are all preserved unchanged.

        AUTO-DM-1: ``task_mode`` is forwarded to ``make_outer_loop`` so all
        downstream agents (coder, inner_loop, validator) receive the mode.

        Returns
        -------
        (stop_reason, tasks_done)
            stop_reason is ``"runtime_cap"`` / ``"task_cap"`` if a cap fired,
            or ``None`` if all pending tasks were processed.
            tasks_done is the count of tasks completed in *this session only*.
        """
        pending = self.state.resume_info()["pending"]
        tasks_done = 0
        # tasks_processed counts every task run (passed or exhausted) and feeds the cap check; tasks_done keeps its passing-only meaning (audit #1).
        tasks_processed = 0

        # A repeat run on a finished plan says why it exits, instead of looking like it did nothing (podrugi sim).
        if not pending:
            _total = len(self.state.all_tasks())
            if _total > 0:
                print(f"[controller] plan.json in {self.base_dir} has no "
                      f"pending tasks — all {_total} task(s) in the existing "
                      f"plan are already done. Nothing to execute this run. "
                      f"To plan NEW work toward the goal, remove the .agent "
                      f"state directory (or start with a fresh --base) and "
                      f"re-run.")
                logger.info("execute_phase: resumed plan has 0 pending of %d "
                            "task(s) — exiting without work; stale plan.json "
                            "is the likely cause on a repeat --auto "
                            "invocation.", _total)
            # No total is reported here, since the plan-phase logs already say the architect produced nothing.

        # ── Build execution helpers once per loop ──────────────────────────
        if cfg is None:
            cfg = self._cfg()

        # make_outer_loop is imported per call, not at module scope, so tests can patch it at its source.
        from tools.auto.outer_loop import make_outer_loop
        from tools.auto.commit_on_success import CommitOnSuccess
        from tools.auto.exhaustion_handler import make_exhaustion_handler
        from tools.auto.executor import make_executor
        from tools.auto.bug_fix_loop import make_bug_fix_loop

        # The collect bridge is built once per run, not per task (COLLECT-24).
        collect_bridge = self._get_collect_bridge(task_mode)

        outer_loop = make_outer_loop(cfg, self.base_dir, self.state,
                                       task_mode=task_mode, run_goal=self.goal,
                                       collect_bridge=collect_bridge)
        # The synopsis hook is wired with the commit helper so the creative continuity runs (AUTO-CR-14).
        _summary_memory = None
        if task_mode == "creative" and self.git is not None:
            try:
                from tools.auto.summary_memory import make_summary_memory
                _summary_memory = make_summary_memory(
                    cfg, base_dir=self.base_dir, task_mode=task_mode,
                )
            except ValueError:
                # A misconfigured [summary] profile is an operator error: raised at construction, not swallowed.
                raise
            except Exception as exc:  # noqa: BLE001 — never block commits on setup
                logger.warning(
                    "controller: could not build SummaryMemory — synopsis "
                    "updates will be skipped: %s", exc,
                )
        # The story bible is built like SummaryMemory, so its injection and continuity anchor actually run (AUTO-CR-23-1).
        _story_bible = None
        if task_mode == "creative":
            try:
                from tools.auto.story_bible import make_story_bible
                _active  = cfg.get("api", "active", fallback="local")
                _api_sec = f"api_{_active}"
                _story_bible = make_story_bible(
                    cfg,
                    base_url=cfg.get(_api_sec, "base_url", fallback="http://localhost:11434"),
                    api_key=cfg.get(_api_sec, "api_key", fallback="ollama"),
                    model=cfg.get(_api_sec, "model", fallback="llama3.1:8b"),
                    api_format=cfg.get(_api_sec, "api_format", fallback="ollama"),
                    base_dir=self.base_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never block commits on setup
                logger.warning(
                    "controller: could not build StoryBible — durable-fact "
                    "updates will be skipped: %s", exc,
                )

        commit_helper = (
            CommitOnSuccess(
                self.git, self.state,
                summary_memory=_summary_memory,
                task_mode=task_mode,
                base_dir=self.base_dir,
                story_bible=_story_bible,
            )
            if self.git is not None else None
        )
        exhaustion_handler = make_exhaustion_handler(self.state)

        # AUTO-G5: executor + bug_fix_loop for post-commit regression checks
        executor = make_executor(
            self.base_dir,
            timeout_sec=self.limits.exec_timeout_sec,
            max_retained_workspaces=self.limits.workspace_retain_count,
            # Reads the same [executor] pytest_serial as the task loop, so there is one setting (RUN-3).
            pytest_serial=safe_getboolean(cfg, "executor", "pytest_serial", fallback=True),
        )
        bug_fix_loop = make_bug_fix_loop(
            cfg, self.base_dir, self.state,
            outer_loop=outer_loop,
            commit_on_success=commit_helper,
        )

        for task in pending:
            # AUTO-A4: check caps BEFORE executing each task
            reason = self.check_caps(tasks_processed)
            if reason:
                logger.info(
                    "_run_task_loop: cap fired (%s) after %d task(s) "
                    "processed (%d passed) — stopping",
                    reason, tasks_processed, tasks_done,
                )
                self._log_collect_summary(collect_bridge)
                return reason, tasks_done

            failed_deps = []
            for dep_id in task.get("dependencies", []):
                dep = self.state.get_task(dep_id)
                if not dep or dep["status"] != STATUS_DONE:
                    failed_deps.append(dep_id)

            if failed_deps:
                reason_str = f"dependency not done: {', '.join(failed_deps)}"
                logger.info("Task %s blocked by incomplete dependencies: %s",
                            task["id"], failed_deps)
                self.state.set_task_status(task["id"], STATUS_BLOCKED)
                self.state.log(f"task {task['id']} blocked ({reason_str})")
                if self.run_trace:
                    self.run_trace.log_task_blocked(task["id"], reason_str)
                continue

            if self.run_trace:
                self.run_trace.log_task_start(task["id"], task.get("title", ""))

            # AUTO-F1: Update display for task start
            if self.progress_display:
                self.progress_display.set_task(
                    task_num=tasks_done + 1,
                    attempt=task.get("attempt", 1) or 1,
                    round_num=task.get("round", 1) or 1,
                    task_id=task.get("id", ""),
                    title=task.get("title", ""),
                )

            # ── AUTO-G2: outer_loop execution ──────────────────────────────
            result = outer_loop.run_task(task, self.base_dir)

            if result.passed:
                # ── AUTO-G3: commit on success ─────────────────────────────
                commit_hash: Optional[str] = None
                commit_result = None
                if commit_helper is not None:
                    # B2: the classified outcome, not just the sha — a task whose
                    # commit FAILED is not a task that was done, and its work must
                    # be kept before anything resets the tree.
                    commit_result = self._commit_outcome(commit_helper, task, result)
                    commit_hash = commit_result.sha
                else:
                    # No git: mark DONE manually
                    self.state.set_task_status(task["id"], STATUS_DONE)

                if commit_result is not None and commit_result.git_error:
                    # git failed: the task is NOT counted done, its status stays
                    # open-to-a-resume, and the received work is kept as a patch
                    # before a later task's commit can sweep it in or a reset
                    # can delete it. The regression check is skipped — there is
                    # no commit to check a tree against.
                    patch_path = self._save_uncommitted_patch(task, result)
                    self.state.log(
                        f"task {task['id']} NOT counted done — commit failed "
                        f"({commit_result.error})"
                        + (f"; work saved to {patch_path}" if patch_path
                           else "; could not save the work as a patch")
                    )
                    logger.error(
                        "task %s: commit failed after a passing acceptance "
                        "check (%s) — not counted done, work %s",
                        task["id"], commit_result.error,
                        f"saved to {patch_path}" if patch_path
                        else "left in the working tree (patch failed)",
                    )
                    if self.run_trace:
                        self.run_trace.log_task_failed(
                            task["id"], commit_result.error or "commit failed")
                    # The edit IS in the tree, uncommitted: the collect model
                    # built before it describes a tree that no longer exists,
                    # so its paths are marked dirty here too (V9). With no sha,
                    # _invalidate_collect falls back to the task's target_files.
                    self._invalidate_collect(task["id"], collect_bridge, None)
                else:
                    tasks_done += 1
                    self.state.log(
                        f"task {task['id']} completed — "
                        f"rounds={result.rounds_used} "
                        f"commit={commit_hash[:12] if commit_hash else 'none'}"
                    )
                    if self.run_trace:
                        self.run_trace.log_task_done(task["id"], commit_hash)

                    # Post-commit regression check: guarded, so a failure cannot cost the tasks still pending (AUTO-G5, bugfix).
                    try:
                        self._check_regressions(task["id"], executor, bug_fix_loop)
                    except Exception as exc:  # noqa: BLE001 — never abort the run
                        logger.warning(
                            "_check_regressions raised after commit of %s — %s; "
                            "continuing with remaining tasks.",
                            task["id"], exc,
                        )
                        self.state.log(
                            f"regression checks failed after commit of "
                            f"{task['id']} ({type(exc).__name__}: {exc}) — "
                            f"continuing with remaining tasks"
                        )

                    # The collect artifact predates this commit: mark the paths dirty after the regression fixes, since HEAD moved (V9).
                    self._invalidate_collect(task["id"], collect_bridge, commit_hash)

            elif getattr(result, "unavailable", False) is True:
                # A Gate-2 outage (or a coder call that died) is not an exhaustion: no knowledge note or ticket; the candidate is discarded (RUN-7, RUN-8).
                _u_stage = ""
                if result.inner_results:
                    _u_stage = str(
                        getattr(result.inner_results[-1], "unavailable_stage", "") or "")
                _u_label = ("coder transport failure" if _u_stage == "coder"
                            else "validator unavailable")
                self.state.log(
                    f"task {task['id']} left todo — {_u_label} "
                    f"(no feedback, no knowledge note, no ticket)"
                )
                self._discard_exhausted_residue(task["id"], reason=_u_label)

            else:
                # ── AUTO-G4: exhaustion → knowledge note + ticket ──────────
                ex_outcome = exhaustion_handler.handle(task, result)
                # An exhausted task's uncommitted edit is discarded now, so the next commit does not sweep it in (Bug 2).
                self._discard_exhausted_residue(task["id"])
                self.state.log(
                    f"task {task['id']} exhausted — "
                    f"rounds={result.rounds_used} "
                    f"ticket={ex_outcome.ticket_id}"
                )
                if self.run_trace:
                    feedback_snippet = result.knowledge()[:200] if result.feedback_files else ""
                    self.run_trace.log_task_blocked(task["id"], feedback_snippet)

            tasks_processed += 1

            # AUTO-F1: Tick progress upon task completion / exhaustion
            if self.progress_display:
                self.progress_display.record_result(passed=result.passed)
                self.progress_display.tick_code()

            # AUTO-E1/E2: Record metric and maybe tune
            if self.metrics_stream and self.auto_tuner:
                total_attempts = sum(
                    getattr(r, "attempts_used", 0) for r in result.inner_results
                )
                last_feedback = ""
                if result.inner_results:
                    last_feedback = getattr(result.inner_results[-1], "last_feedback", "")
                self.metrics_stream.record_gate2(
                    task["id"],
                    approved=result.passed,
                    feedback=last_feedback,
                    attempts=total_attempts,
                    prompt_store=self.auto_tuner.prompt_store,
                    # An outage is neither approval nor rejection, so the auto-tuner does not demote a prompt on it (RUN-7).
                    unavailable=getattr(result, "unavailable", False) is True,
                )
                tune_outcome = self.auto_tuner.maybe_tune()
                if tune_outcome.promoted:
                    # The full record is also in agent_trace.jsonl; the run log gets the promotion (AC4).
                    self.state.log(
                        f"[AUTO-E1] auto_tuner promoted validator prompt: "
                        f"score={tune_outcome.new_prompt_score:.2f}"
                    )
                elif getattr(tune_outcome, "triggered", False):
                    # A denied candidate is printed only: run.log stays silent for non-promoted outcomes (AC4).
                    print(
                        f"[AUTO-E1] auto_tuner denied candidate prompt: "
                        f"score={tune_outcome.new_prompt_score:.4f} — "
                        f"{getattr(tune_outcome, 'reason', '')}"
                    )

        self._log_collect_summary(collect_bridge)
        return None, tasks_done  # all tasks done / no tasks

    def _save_uncommitted_patch(self, task: dict, result) -> str:
        """Write the task's uncommitted work to ``.agent/uncommitted-<id>.patch``
        (B2), returning its path, or ``""`` when it could not be written.

        Called when a task passed its acceptance check but its commit failed.
        The edits are then still in the working tree, where the next task's
        ``stage_all()`` would sweep them in under another id — or an exhausted
        task's ``discard_working_changes()`` would delete them outright. A
        ``git diff`` of the whole tree is the only copy that survives both.

        The patch is written into ``.agent/`` (the run's own state directory,
        already gitignored) so it can never itself show up as a user change.
        Best-effort by design: a failure to save is logged, never raised — the
        run must keep going.
        """
        task_id = safe_filename_component(task.get("id") or "unknown")
        patch_path = self.agent_dir / f"uncommitted-{task_id}.patch"
        try:
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            diff = self.git.diff_working_tree()
        except (GitError, OSError) as exc:
            logger.error(
                "task %s: could not capture the uncommitted work as a patch "
                "(%s) — it stays only in the working tree.",
                task_id, exc,
            )
            return ""
        try:
            atomic_write_text(patch_path, diff)
        except OSError as exc:
            logger.error(
                "task %s: could not write %s (%s) — the uncommitted work "
                "stays only in the working tree.", task_id, patch_path, exc,
            )
            return ""
        if diff.strip():
            logger.warning(
                "task %s: commit failed — uncommitted work saved to %s",
                task_id, patch_path,
            )
        return str(patch_path)

    def _commit_outcome(self, commit_helper, task: dict, result) -> "CommitResult":
        """Commit *task* and return the classified :class:`CommitResult` (B2).

        Falls back to a plain outcome for a commit helper that predates the
        classification (a test double, or an older build): its ``commit()`` sha
        is all there is, and treating a missing classification as a failure
        would silently stop counting successful tasks as done. The check is
        ``isinstance``, not ``getattr``: a ``MagicMock`` auto-creates any
        attribute asked of it, so only a real :class:`CommitResult` is trusted.
        """
        commit_result = getattr(commit_helper, "commit_result", None)
        if callable(commit_result):
            try:
                classified = commit_result(task, result)
            except Exception as exc:  # noqa: BLE001 — a broken helper must not lose a passed task
                logger.warning(
                    "task %s: commit_result raised (%s) — falling back to "
                    "commit()'s sha.", task.get("id"), exc,
                )
            else:
                if isinstance(classified, CommitResult):
                    return classified
        sha = commit_helper.commit(task, result)
        return CommitResult(sha, COMMIT_OK if sha else COMMIT_NOTHING_TO_COMMIT)

    def _discard_exhausted_residue(
        self, task_id: str, reason: str = "exhausted",
    ) -> None:
        """Discard the uncommitted edits left behind by a task that did not commit.

        ``reason`` names the run.log line: ``exhausted`` (the AUTO-G4 path) or
        ``validator unavailable`` (RUN-7 — the task went back to todo).

        The coder writes its candidate into base_dir before validation, so an
        exhausted task leaves that edit dirty -- and commit() stages
        everything (git add -u/.), which would sweep it into the next
        successful task's commit. No-op when git is disabled.

        B9: this call used to be unguarded.
        ``discard_working_changes()`` goes through ``GitManager._run()``,
        which raises ``GitError`` on a timeout (a stale index.lock, a hung
        hook), a missing git binary, or a non-zero ``git reset --hard``. That
        one cleanup failure aborted the entire multi-task run at the worst
        possible moment: the task had already been fully accounted for
        (exhaustion note, ticket, BLOCKED status), so every task still
        pending in plan.json was dropped with it.

        Worst case if the discard fails is that the residue is swept into the
        next commit -- bad, but recoverable, and strictly better than losing
        the run. ``GitError`` is caught specifically rather than ``Exception``
        so a real bug on this path still surfaces.

        The failure is written to ``state.log`` as well as ``logger``: the
        run log is what gets read during a postmortem, and ``logger`` may be
        configured to a level that drops the warning entirely.
        """
        if self.git is None:
            return
        try:
            self.git.discard_working_changes()
        except GitError as exc:
            logger.warning(
                "task %s: could not discard uncommitted edits (%s) — "
                "continuing (residue may be swept into the next commit): %s",
                task_id, reason, exc,
            )
            self.state.log(
                f"task {task_id} uncommitted edits NOT discarded "
                f"(git discard failed: {exc}) — residue may be swept into "
                f"the next commit"
            )
            return
        self.state.log(
            f"task {task_id} uncommitted edits discarded "
            f"({reason}, not committed)"
        )

    def _check_regressions(
        self,
        just_committed_id: str,
        executor,
        bug_fix_loop,
    ) -> None:
        """AUTO-G5: re-run acceptance checks for all previously-DONE tasks.

        Called immediately after a task is committed.  Any task whose check
        now fails is treated as a regression and routed through BugFixLoop.

        Parameters
        ----------
        just_committed_id:
            The task that was just committed — excluded from re-checking
            because its check was validated moments ago by outer_loop.
        executor:
            Ready :class:`~tools.auto.executor.Executor` instance.
        bug_fix_loop:
            Ready :class:`~tools.auto.bug_fix_loop.BugFixLoop` instance.
        """
        from tools.auto.bug_fix_loop import _FIX_PREFIX, _root_task_id

        all_tasks = self.state.all_tasks()
        by_id     = {t["id"]: t for t in all_tasks}

        def _is_redundant_fix_task(t: dict) -> bool:
            """True for a synthetic BUG-FIX-* task that duplicates its root.

            BugFixLoop registers ``BUG-FIX-<id>`` carrying a *copy* of the
            root task's acceptance_check, so once committed it appears here
            as a second DONE task running the identical command.  Checking
            both doubles the executor work for zero extra coverage, and it
            was the entry point for the runaway id cascade: a fix task that
            "regressed" fed its own id back into BugFixLoop, generating
            BUG-FIX-BUG-FIX-... until the ticket filename blew past NAME_MAX.
            Only skip when the root really is present with the same check —
            otherwise the fix task is the only thing covering it, and
            dropping it would lose real coverage.
            """
            tid = t["id"]
            if not tid.startswith(_FIX_PREFIX):
                return False
            root = by_id.get(_root_task_id(tid))
            # The root must be a task this filter keeps; otherwise its check would run nowhere and coverage is lost.
            return (
                root is not None
                and root.get("status") == STATUS_DONE
                and root["id"] != just_committed_id
                and root.get("acceptance_check", "") == t.get("acceptance_check", "")
            )

        done_tasks = [
            t for t in all_tasks
            if t["status"] == STATUS_DONE
            and t["id"] != just_committed_id
            and t.get("acceptance_check", "").strip()
            and not _is_redundant_fix_task(t)
        ]
        for done_task in done_tasks:
            # Respect the run's time budget here too, since regression fixes can cascade (Bug 6).
            if self.is_runtime_exceeded():
                logger.info(
                    "_check_regressions: runtime cap reached — stopping "
                    "regression checks after commit of %s", just_committed_id,
                )
                self.state.log(
                    f"regression checks halted by runtime cap "
                    f"(after commit of {just_committed_id})"
                )
                break

            # One flaky check must not skip the regression check for the other done tasks (check-regressions-guard).
            try:
                exec_result = executor.run(done_task)
            except Exception as exc:  # noqa: BLE001 — fail-open, keep checking siblings
                logger.warning(
                    "_check_regressions: check for %s raised — %s; "
                    "continuing with remaining done task(s).",
                    done_task["id"], exc,
                )
                self.state.log(
                    f"regression check for {done_task['id']} after commit "
                    f"of {just_committed_id} failed "
                    f"({type(exc).__name__}: {exc}) — continuing"
                )
                continue

            if not exec_result.passed:
                logger.warning(
                    "_check_regressions: task %s regressed (rc=%s) — "
                    "running bug fix loop",
                    done_task["id"], exec_result.exit_code,
                )
                self.state.log(
                    f"regression detected in task {done_task['id']} "
                    f"after commit of {just_committed_id} "
                    f"(rc={exec_result.exit_code})"
                )
                # A failing bug-fix loop is attributed to its stage and the pass moves on (check-regressions-guard).
                try:
                    bfl_result = bug_fix_loop.handle_regression(
                        done_task, exec_result, self.base_dir
                    )
                except Exception as exc:  # noqa: BLE001 — fail-open, keep checking siblings
                    logger.warning(
                        "_check_regressions: bug fix loop for %s raised — "
                        "%s; continuing with remaining done task(s).",
                        done_task["id"], exc,
                    )
                    self.state.log(
                        f"bug fix loop for {done_task['id']} after commit "
                        f"of {just_committed_id} failed "
                        f"({type(exc).__name__}: {exc}) — continuing"
                    )
                    continue
                self.state.log(
                    f"bug fix loop: {bfl_result.summary()}"
                )

    def _handle_cap(self, stop_reason: str, tasks_done: int) -> None:
        """Persist cap state, print user-facing notice, write to log."""
        elapsed = self.elapsed_seconds()

        if self.run_trace:
            self.run_trace.log_run_capped(stop_reason)
        self.state.update_progress(status="capped", stop_reason=stop_reason)
        self.state.log(
            f"run capped: reason={stop_reason} "
            f"elapsed={elapsed:.1f}s tasks_done={tasks_done}"
        )

        ts = _ts()
        if stop_reason == STOP_RUNTIME_CAP:
            mins = self.limits.max_runtime_sec / 60
            print(
                f"[{ts}] ⏱  Runtime cap reached "
                f"({mins:.2g} min / {elapsed:.1f}s elapsed) — "
                f"state saved, run is resumable."
            )
        else:  # STOP_TASK_CAP
            print(
                f"[{ts}] 🔢 Task cap reached "
                f"({self.limits.max_tasks_per_run} tasks/session, "
                f"{tasks_done} completed) — "
                f"state saved, run is resumable."
            )

    def _cfg(self) -> configparser.ConfigParser:
        """Return the parsed agents.ini, loading it once and caching on self.

        Real runs set ``self.config`` in ``__init__`` so this returns it with no
        disk read.  Test harnesses that build the controller via ``__new__``
        (bypassing ``__init__``) hit the lazy path, which reads ``config_path``
        once and caches it — so the single-parse guarantee holds either way.
        """
        cfg = getattr(self, "config", None)
        if cfg is None:
            cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
            if Path(self.config_path).exists():
                cfg.read(self.config_path, encoding="utf-8")
            self.config = cfg
        return cfg

    def _reset_resettable_blocked_tasks(self, cfg: configparser.ConfigParser) -> None:
        """Reset BLOCKED tasks back to TODO — but only when that can help.

        A task can be BLOCKED for two very different reasons:

        1. An unmet dependency (set in ``_run_task_loop``). Resetting this to
           TODO is exactly right: the dependency check runs again immediately,
           so the task either runs now (dependency done) or is re-blocked
           straight away (dependency still pending) — harmless either way.
        2. OuterLoop used up every one of its ``max_rounds_per_task``
           attempts (round-exhaustion; see ``OuterLoop.run_task``'s
           "already exhausted" and "all rounds exhausted" paths).

        Bugfix: this used to reset BOTH cases unconditionally, on the theory
        (per the original comment, which only ever described case 1) that a
        fresh session should give every BLOCKED task another look. For case 2
        that reset was a no-op in disguise: OuterLoop decides where to resume
        from ``feedback_round_*.md`` files on disk, which a bare status reset
        never touches, so the task immediately re-exhausted and flipped
        straight back to BLOCKED without a single new attempt — silently
        defeating the exact restart an operator would reach for to give a
        stuck task another chance.

        This detects case 2 directly — via the same file-based round count
        OuterLoop itself uses (``highest_completed_round``) compared against
        the configured cap — and leaves those tasks BLOCKED so the status
        honestly reflects reality. Only genuinely-resettable tasks (case 1,
        or anything that hasn't used up its rounds) are reset.

        The same reset also unlinks the task's ``deadline_started_at.txt``
        budget ledger. Bugfix: it used to clear only the STATUS half of "give
        this task a fresh start", so on resume OuterLoop re-read the persisted
        consumed time, the remaining budget was 0, and the task was re-blocked
        immediately — the reset was structurally incapable of granting the
        attempt it exists to grant, and every resume burned a cycle and
        re-parked the task. The unlink is scoped to the task that was actually
        reset: tasks left BLOCKED keep their ledgers untouched.
        """
        from tools.auto.bug_fix_loop import _FIX_PREFIX

        # Guarded per call, like the other config reads; a malformed value does not abort the run (config-crash audit).
        try:
            max_rounds_cfg = cfg.getint("auto", "max_rounds_per_task", fallback=10)
        except ValueError as exc:
            logger.warning(
                "config [auto] max_rounds_per_task is malformed (%s) — using default 10",
                exc,
            )
            max_rounds_cfg = 10
        for task in self.state.all_tasks():
            if task["status"] != STATUS_BLOCKED:
                continue
            # Parked BUG-FIX tasks stay BLOCKED: resetting them on startup would bypass the fix-attempt bound.
            if task["id"].startswith(_FIX_PREFIX):
                continue
            if highest_completed_round(self.state.task_dir(task["id"])) >= max_rounds_cfg:
                continue  # round-exhausted — resetting would not help
            self.state.set_task_status(task["id"], STATUS_TODO)
            # A retry gets a fresh wall-clock budget; otherwise the parked task's leftover budget stops it before round 1.
            try:
                self.state.clear_task_deadline(task["id"])
            except OSError as exc:
                logger.warning(
                    "controller: could not clear the deadline start time for "
                    "%s — the reset attempt may inherit a stale wall-clock "
                    "budget: %s", task["id"], exc,
                )

    def _setup_git(self) -> None:
        """AUTO-A3: ensure the base dir is a git repo and apply agent identity.

        Guarded: a git failure (e.g. git not installed) is logged but does NOT
        abort the run — Epic A stays usable without git, and commits (AUTO-C5)
        simply won't happen until git is available.
        """
        try:
            cfg = self._cfg()
            probe = GitManager(self.base_dir, cfg) if (self.base_dir / ".git").exists() else None
            had_commit = probe is not None and probe.get_current_hash() is not None
            if had_commit:
                changed = probe.user_changes()
                if changed:
                    raise DirtyTreeError(changed)
            self.git = make_git_manager(self.base_dir, cfg)
            if not had_commit:
                self.git.commit_existing_files()
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            self.state.log(
                f"git ready (user={self.git.git_user} <{self.git.git_email}>)"
            )
        except (GitError, OSError) as exc:
            self.git = None
            logger.warning("git setup failed (continuing without git): %s", exc)
            self.state.log(f"git setup failed: {exc}")


    def _log_auto_prompts(self) -> None:
        """If auto_prompts.json exists, print its contents to console and log it."""
        prompts_path = self.agent_dir / "auto_prompts.json"
        if not prompts_path.exists():
            return
        import json as _json
        try:
            data = _json.loads(prompts_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[{_ts()}] ⚠️  Could not read auto_prompts.json: {exc}")
            return
        ts = _ts()
        text = _json.dumps(data, indent=2, ensure_ascii=False)
        print(f"[{ts}] ✨ Validator prompts were tuned this run — auto_prompts.json:")
        print(text)
        self.state.log(f"auto_prompts.json contents:\n{text}")

    def _print_banner(self) -> None:
        ts = _ts()
        print(f"[{ts}] 🤖 Autonomous mode starting")
        print(f"[{ts}]    goal      : {self.goal}")
        print(f"[{ts}]    base_dir  : {self.base_dir}")
        print(f"[{ts}]    config    : {self.config_path}")
        print(f"[{ts}]    task_mode : {getattr(self, 'task_mode', 'code')}")

    def _print_resume_summary(self, info: dict) -> None:
        done    = len(info["done_ids"])
        pending = len(info["pending"])
        ts = _ts()
        print(f"[{ts}] ♻️  Resuming existing run — "
              f"{done} already done, {pending} pending")
        if info["done_ids"]:
            print(f"[{ts}]    skipping: {', '.join(sorted(info['done_ids']))}")

    def _log_limits(self) -> None:
        parts = []
        if self.limits.runtime_capped:
            parts.append(f"max_runtime={self.limits.max_runtime_sec:.1f}s")
        if self.limits.task_capped:
            parts.append(f"max_tasks={self.limits.max_tasks_per_run}")
        if parts:
            self.state.log(f"run limits active: {', '.join(parts)}")
        else:
            self.state.log("run limits: none configured")

    # ── COLLECT-23: opt-in collect-context injection ────────────────────

    def _collect_use_flag(self) -> bool:
        """`[collect] use_in_doc` for `task_mode == "docs"`, else
        `[collect] use_in_auto` — both default `false`, matching
        `agents.ini`'s own documented default (turning collect on never
        changes behaviour until this flag is also explicitly set)."""
        key = "use_in_doc" if self.task_mode == "docs" else "use_in_auto"
        return safe_getboolean(self.config, "collect", key, fallback=False)

    def _get_collect_bridge(self, task_mode: str):
        """COLLECT-24: lazily build (and cache for the lifetime of this
        Controller / this `--auto` run) the `CollectBridge` used to inject
        collect context into every task in `_run_task_loop`.

        Cached per `task_mode` since `use_in_auto` vs `use_in_doc` and the
        summarizer's own mode-aware settings can legitimately differ; in
        practice a single `Controller` only ever runs one `task_mode` per
        `run()`, so this is a single load in normal operation — the
        per-task-loop-call cache is what `test_collect_model_loaded_once_per_run`
        actually pins down (`tools.collect.loader.load` called exactly once
        for N tasks in one `_run_task_loop` invocation).
        """
        cache = getattr(self, "_collect_bridge_cache", None)
        if cache is None:
            cache = {}
            self._collect_bridge_cache = cache
        if task_mode in cache:
            return cache[task_mode]
        # Tests may build the controller without __init__; then there is no config and the bridge stays disabled.
        cfg = getattr(self, "config", None)
        if cfg is None:
            cache[task_mode] = None
            return None
        from tools.auto.collect_bridge import make_collect_bridge
        bridge = make_collect_bridge(
            self.base_dir, cfg, getattr(self, "config_path", None), task_mode=task_mode,
        )
        cache[task_mode] = bridge
        return bridge

    def _invalidate_collect(self, task_id: str, bridge, commit_hash: Optional[str]) -> None:
        """V9: mark the paths this task wrote as dirty on the collect bridge.

        The model was built once, at `load()`, before this commit happened,
        so `status` still reads `"fresh"` while its facts describe a tree
        that no longer exists. The paths the commit actually touched are the
        authoritative answer to "what did this task write" — more accurate
        than the task's declared `target_files`, which can both
        under-report (the coder fixed a helper too) and over-report (a task
        that staged nothing). Without git, or when git cannot be asked, the
        task's declared `target_files` are the fallback — the safer guess:
        blinding a path that turned out clean costs a block, serving stale
        facts costs a wrong one. With no bridge this is a no-op.

        Called after the post-commit regression loop, so the bug-fix
        commits it produces are dirty as well: `paths_changed_between`
        covers `commit_hash..HEAD` rather than just the one commit. Never
        raises — a collect bookkeeping hiccup must not cost the remaining
        tasks in plan.json.
        """
        if bridge is None:
            return
        # No hash after a commit: if git had nothing to commit the facts are still valid; if git failed after staging, use target_files.
        if not commit_hash and self.git is not None and not self._tree_has_uncommitted_changes():
            logger.info(
                "_invalidate_collect: task %s committed nothing and the tree "
                "is clean — nothing was written, collect blocks stay valid",
                task_id,
            )
            return
        paths: Optional[list] = None
        if commit_hash and self.git is not None:
            try:
                paths = list(self.git.paths_changed_in(commit_hash))
                tip = self.git.get_current_hash()
                if tip and tip != commit_hash:
                    paths = sorted(set(paths) | set(self.git.paths_changed_between(commit_hash, tip)))
            except Exception as exc:  # noqa: BLE001 — never abort the run
                logger.warning(
                    "_invalidate_collect: could not list the paths committed "
                    "for task %s (%s) — falling back to target_files",
                    task_id, exc,
                )
                paths = None
        if paths is None:
            # No git: fall back to the task's target files; an empty diff-tree answer means nothing changed, so nothing is stale.
            paths = list(self._task_target_files(task_id))
        try:
            bridge.invalidate(paths)
        except Exception as exc:  # noqa: BLE001 — never abort the run
            logger.warning(
                "_invalidate_collect: bridge.invalidate failed for task %s "
                "(%s) — collect blocks may stay stale for the paths this "
                "task wrote", task_id, exc,
            )

    def _task_target_files(self, task_id: str) -> list:
        """A task's declared `target_files`, for the no-git invalidation
        fallback. Returns `[]` when the task record is gone or malformed
        rather than raising."""
        try:
            task = self.state.get_task(task_id)
            if not isinstance(task, dict):
                return []
            files = task.get("target_files")
            if not isinstance(files, (list, tuple)):
                return []
            return [str(f) for f in files]
        except Exception:  # noqa: BLE001 — never abort the run
            return []

    def _tree_has_uncommitted_changes(self) -> bool:
        """V9: does the tree still hold a change git could not commit?

        `GitManager.commit()` stages everything before it decides whether
        there is anything to commit, so a `None` commit hash is ambiguous:
        the coder may have written nothing, or git may have failed after
        staging a real edit. Staged changes are the answer — `True` means
        the edit exists and the collect facts for it are stale, `False`
        means nothing was written and the facts are still correct.

        Fail-open to `True`: if git cannot be asked, assuming a write
        happened is the safer guess — blinding a path that turned out
        clean costs one block, serving stale facts costs a wrong one.
        Never raises.
        """
        git = getattr(self, "git", None)
        if git is None:
            return True
        try:
            return bool(git.has_staged_changes())
        except Exception:  # noqa: BLE001 — never abort the run
            return True

    def _log_collect_summary(self, bridge) -> None:
        """V9 item 4: one run.log line for how much invalidate-on-write
        withheld this run.

        `collect_miss(reason="dirty")` is counted on the bridge; this is the
        reader. Called once, at the end of the task loop (both exit paths),
        so the line is the whole run's picture rather than a per-task echo.
        A bridge that withheld nothing and has nothing still dirty reports
        nothing — a run with no edits adds no noise. Never raises.
        """
        if bridge is None:
            return
        try:
            # The trace event goes first; the run.log line is the human reader. Both are written once per run (M4).
            bridge.emit_collect_summary()
        except Exception:  # noqa: BLE001 — never abort the run
            pass
        try:
            line = bridge.summary()
        except Exception:  # noqa: BLE001 — never abort the run
            return
        if not line:
            return
        try:
            self.state.log(f"collect summary ({self.task_mode}): {line}")
        except Exception:  # noqa: BLE001 — a broken log sink is not a blocker
            pass

    def collect_context_for(self, target_file: str) -> str:
        """The opt-in COLLECT-23 context block for `target_file` — kept for
        backward compatibility with anything calling this directly (e.g.
        `/doc` mode, older tests). `_run_task_loop` no longer calls this;
        it uses `_get_collect_bridge()` + `CollectBridge.context_for()`
        instead (COLLECT-24), which adds budget-aware shrinking and a
        run-scoped cache this method never had.

        Returns `""` when the feature is off, the artifact is unavailable
        or stale, or there is nothing to say about this file.
        """
        bridge = self._get_collect_bridge(self.task_mode)
        if bridge is None:
            return ""
        return bridge.context_for(target_file)


# Convenience wrapper used by main.py

def run_auto(
    goal: str,
    base_dir: str | os.PathLike = ".",
    config_path: str = "agents.ini",
    dry_run: bool = False,
    skill: Optional[str] = None,
) -> int:
    """Create an :class:`AutoController` and run it.

    Returns the integer exit code (0 on success, non-zero on error).
    """
    try:
        controller = AutoController(
            goal=goal,
            base_dir=base_dir,
            config_path=config_path,
            dry_run=dry_run,
            skill=skill,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        return controller.run()
    except RuntimeError as exc:
        # The plan-goal safety refusal is a deliberate error: printed cleanly, without a traceback or a fatal banner (BUGFIX).
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error in autonomous run: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
