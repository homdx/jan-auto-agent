"""tools/auto/outer_loop.py — AUTO-C4: outer round loop + feedback files.

Wraps the AUTO-C3 inner loop (``InnerLoop.run_task``) in the outer *round* loop
for ONE task:

    for round in 1 .. max_rounds (default 10):
        prior = [compact summary of every PREVIOUS round]   # the only carry-over
        result = inner_loop.run_task(task, prior_feedback=prior)   # up to 5 attempts
        if result.passed:  → task done (AUTO-C5 commits)
        else:              → write a COMPACT feedback_round_<n>.md, start a fresh round

The key idea (and the thing that fixes the context-bloat / 500s seen earlier):
each new round starts with a **fresh context** seeded ONLY by the compact
per-round feedback files — never the full attempt-by-attempt transcript.  So the
context the model sees grows by *one short summary per round*, not by five
attempt logs per round.  Within a round, the inner loop keeps its own attempt
context; that context is discarded at round end and distilled into one file.

Counters (round / attempt) are persisted to the StateStore after every round, so
a run killed mid-task resumes from the next unfinished round rather than redoing
work.  Committing a passed task is AUTO-C5; turning an exhausted task into a
knowledge note + investigation ticket is AUTO-C6 — this module only drives the
rounds and produces the feedback files / result.

Public surface:

    from tools.auto.outer_loop import OuterLoop, OuterLoopResult, make_outer_loop

    outer = make_outer_loop(config, base_dir, state)        # builds InnerLoop too
    result = outer.run_task(task, base_dir)
    if result.passed:      ...   # AUTO-C5: git commit
    elif result.exhausted: ...   # AUTO-C6: knowledge + ticket

agents.ini [auto] keys
----------------------
max_rounds_per_task   — outer-loop cap (default 10)
max_attempts_per_task — inner-loop cap (default 5)   [used by make_inner_loop]
"""

from __future__ import annotations

import configparser
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.agent_trace import tracer
from tools.auto.state import StateStore, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED
from tools.auto.inner_loop import make_inner_loop
from tools.auto.utils import highest_completed_round

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROUNDS = 10
_FEEDBACK_GLOB = "feedback_round_*.md"
_FEEDBACK_RE = re.compile(r"feedback_round_(\d+)\.md$")
_MAX_FEEDBACK_CHARS = 800        # keep each round file compact

# LOOP-4: regex to extract impl version from file headers
_IMPL_HEADER_RE = re.compile(r"impl v(\d+)")


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OuterLoopResult:
    """Aggregate result of the outer round loop for one task."""
    task_id:             str
    passed:              bool
    rounds_used:         int
    exhausted:           bool
    feedback_files:      list[str] = field(default_factory=list)
    inner_results:       list = field(default_factory=list)   # list[InnerLoopResult]
    impl_versions_used:  list = field(default_factory=list)   # list[int] — LOOP-3

    def summary(self) -> str:
        if self.passed:
            return f"[{self.task_id}] DONE in {self.rounds_used} round(s)"
        return f"[{self.task_id}] EXHAUSTED after {self.rounds_used} round(s)"

    def knowledge(self) -> str:
        """Concatenated round feedback — the seed for AUTO-C6's knowledge note."""
        parts = []
        for path in self.feedback_files:
            try:
                parts.append(Path(path).read_text(encoding="utf-8"))
            except OSError:
                continue
        return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# OuterLoop
# ─────────────────────────────────────────────────────────────────────────────

class OuterLoop:
    """Drives up to ``max_rounds`` fresh-context rounds for one task (AUTO-C4)."""

    def __init__(
        self,
        inner_loop,
        state: StateStore,
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        rewrite_every_n_rounds: int = 2,
        max_rewrites: int = 5,
        task_rewriter=None,
    ) -> None:
        self.inner_loop             = inner_loop
        self.state                  = state
        self.max_rounds             = max(1, int(max_rounds))
        self.rewrite_every_n_rounds = max(1, int(rewrite_every_n_rounds))
        self.max_rewrites           = max(0, int(max_rewrites))
        self.task_rewriter          = task_rewriter  # optional; None disables rewriting

    def run_task(self, task: dict, base_dir: str | Path) -> OuterLoopResult:
        """Run the outer loop for *task*.  Resumes from the next unfinished
        round if feedback files already exist.  Never raises."""
        task_id = task.get("id", "?")

        # AUTO-CR-33: one wall-clock budget for the whole task (all rounds) —
        # previously each round re-entered InnerLoop.run_task and reset its own
        # start time, so the effective cap was max_rounds × max_task_seconds
        # (10 × 30 min ≈ 5h observed). Compute the deadline once here and both
        # gate the round loop and hand it to the inner loop.
        try:
            _mts = int(getattr(self.inner_loop, "max_task_seconds", 0) or 0)
        except (TypeError, ValueError):
            _mts = 0   # non-numeric (e.g. a test mock) → guard disabled
        _task_deadline = (time.monotonic() + _mts) if _mts > 0 else None
        # AUTO-CR-33: only hand the deadline to inner loops that accept it, so
        # fakes/older InnerLoop signatures are not broken.
        try:
            import inspect as _inspect
            _inner_accepts_deadline = (
                "deadline"
                in _inspect.signature(self.inner_loop.run_task).parameters
            )
        except (ValueError, TypeError):
            _inner_accepts_deadline = False

        # LOOP-4: the TRUE v1 baseline instruction — used so _build_impl_history
        # can correctly label version 1 even after a later rewrite overwrites
        # task["instruction"]. Bugfix: this used to just read task["instruction"]
        # unconditionally, which was safe only within a single continuous run
        # (before any rewrite happened yet). Now that a rewrite's text is
        # persisted back into task["instruction"] (see StateStore.apply_rewrite,
        # LOOP-3 below), a *resumed* session loads a task whose "instruction"
        # already holds the latest rewrite, not v1's — so prefer the explicitly
        # preserved "original_instruction" when one exists, falling back to
        # "instruction" for a task that has never been rewritten (where
        # "instruction" still IS v1).
        original_instruction: str = task.get("original_instruction") or task.get("instruction", "")

        # ── Resume: existing feedback files mean prior rounds already ran ──
        done_rounds = self._existing_rounds(task_id)
        start_round = done_rounds + 1
        feedback_files = [str(p) for p in self._feedback_paths(task_id)]
        inner_results: list = []

        # LOOP-2: rewrite tracking. Bugfix: this used to always start at 0, so
        # max_rewrites was only ever enforced within a single process's
        # lifetime — restarting the process (a crash, or just stopping and
        # re-running the CLI) reset the count and allowed unlimited further
        # rewrites across enough restarts. impl_version is persisted and
        # already tracks exactly this (starts at 1, +1 per rewrite — see
        # apply_rewrite), so seed the local counter from it to make the cap a
        # true per-task, cross-resume limit.
        # LOOP-3: impl_version tracking — starts at 1, bumped on each rewrite
        impl_version = task.get("impl_version", 1)
        rewrites_done = max(0, int(impl_version or 1) - 1)
        impl_versions_used: list[int] = []

        if start_round > self.max_rounds:
            # Already exhausted in a prior session.
            self.state.set_task_status(task_id, STATUS_BLOCKED)
            return OuterLoopResult(task_id, False, self.max_rounds, True,
                                   feedback_files, inner_results)

        self.state.set_task_status(task_id, STATUS_IN_PROGRESS)
        tracer.event("controller", "outer_loop", "run_start",
                     params={"task": task_id, "start_round": start_round,
                             "max_rounds": self.max_rounds,
                             "impl_version": impl_version})

        for rnd in range(start_round, self.max_rounds + 1):
            # AUTO-CR-33: enforce the task-wide wall-clock budget BEFORE starting
            # another round (this is what previously ran away for ~5 h).
            if _task_deadline is not None and time.monotonic() >= _task_deadline:
                logger.warning(
                    "OuterLoop: task %s wall-clock budget (%ds = %.1f min) "
                    "exhausted across rounds — stopping before round %d.",
                    task_id, _mts, _mts / 60.0, rnd,
                )
                self.state.set_task_status(task_id, STATUS_BLOCKED)
                return OuterLoopResult(task_id, False, rnd - 1, True,
                                       feedback_files, inner_results)

            # Fresh context: seed ONLY with the compact prior-round summaries.
            prior = self._read_round_feedback(task_id)
            self.state.set_task_status(task_id, STATUS_IN_PROGRESS, round=rnd)
            impl_versions_used.append(impl_version)

            # LOOP-4: build prior implementation history so the coder knows
            # which strategies already failed and must not be repeated.
            prior_impls = self._build_impl_history(
                task_id, impl_version, original_instruction
            )

            _rt_kwargs = dict(
                prior_feedback=prior,
                prior_implementations=prior_impls or None,
            )
            if _inner_accepts_deadline:
                _rt_kwargs["deadline"] = _task_deadline   # AUTO-CR-33: shared budget
            res = self.inner_loop.run_task(task, base_dir, **_rt_kwargs)
            inner_results.append(res)
            # round is set authoritatively above via set_task_status(round=rnd);
            # here we only accumulate the attempt count.
            self.state.increment_task_counters(
                task_id, attempt_delta=getattr(res, "attempts_used", 0),
            )

            if getattr(res, "passed", False):
                self.state.set_task_status(task_id, STATUS_DONE)
                self.state.log(f"{task_id}: passed in round {rnd} "
                               f"({getattr(res, 'attempts_used', '?')} attempts)")
                tracer.event("outer_loop", "controller", "result",
                             params={"task": task_id, "passed": True, "round": rnd,
                                     "impl_version": impl_version})
                return OuterLoopResult(task_id, True, rnd, False,
                                       feedback_files, inner_results,
                                       impl_versions_used)

            # Failed round → write ONE compact feedback file, then fresh round.
            fpath = self._write_round_feedback(task_id, rnd, res, impl_version)
            feedback_files.append(str(fpath))
            self.state.log(f"{task_id}: round {rnd} failed — wrote {fpath.name}")

            # LOOP-2: check whether a rewrite is due (rnd >= 3, (rnd-1) %
            # rewrite_every_n_rounds == 0, rewrites_done < max_rewrites, and a
            # rewriter wired up). Pull-model gate: only rewrite when the inner
            # loop's context was satisfied — if the last attempt was still
            # REQUESTING context, the failure is "missing information," not
            # "bad framing," so skip the rewrite and let the prefetched
            # context flow instead.
            if (
                self.task_rewriter is not None
                and self.max_rewrites > 0
                and rnd >= 3
                and (rnd - 1) % self.rewrite_every_n_rounds == 0
                and rewrites_done < self.max_rewrites
                and getattr(res, "context_satisfied", True)
            ):
                failure_history = self._read_round_feedback(task_id)
                impl_num = rewrites_done + 2  # v1 → first rewrite → v2, etc.
                logger.info(
                    "round %d failed — architect rewriting task (impl v%d)",
                    rnd, impl_num,
                )
                self.state.log(
                    f"{task_id}: round {rnd} failed — architect rewriting task "
                    f"(impl v{impl_num})"
                )

                new_task = self.task_rewriter.rewrite(task, failure_history)
                if new_task is not task:
                    # A genuine rewrite was produced — record it on disk and
                    # persist it in state (LOOP-3). Bugfix: this used to call
                    # bare increment_impl_version(), which only persisted the
                    # version *number* — the rewritten instruction itself lived
                    # only in the local `task` variable below and was lost the
                    # moment the process restarted. apply_rewrite() persists
                    # the rewritten instruction/acceptance_check/title in the
                    # SAME call that bumps impl_version, so a resumed session
                    # picks up the latest rewrite instead of silently reverting
                    # to the original (already-failing) instruction.
                    try:
                        impl_version = self.state.apply_rewrite(
                            task_id,
                            instruction=new_task.get("instruction", ""),
                            acceptance_check=new_task.get("acceptance_check", ""),
                            title=new_task.get("title"),
                        )
                    except Exception:
                        impl_version = impl_num   # fallback: derive from rewrites_done

                    rewrite_body = (
                        f"# Rewrite after round {rnd} — impl v{impl_version} — "
                        f"task {task_id}\n\n"
                        f"## New instruction\n{new_task.get('instruction', '')}\n\n"
                        f"## Acceptance check\n{new_task.get('acceptance_check', '')}\n"
                    )
                    self.state.write_task_file(
                        task_id,
                        f"rewrite_round_{rnd}.md",
                        rewrite_body,
                    )
                    task = new_task
                    rewrites_done += 1
                    tracer.event(
                        "outer_loop", "task_rewriter", "rewrite",
                        content=new_task.get("instruction", ""),
                        params={
                            "task": task_id,
                            "round": rnd,
                            "impl_version": impl_version,
                            "rewrites_done": rewrites_done,
                        },
                    )
                else:
                    logger.warning(
                        "TaskRewriter returned original task unchanged for %r "
                        "(parse/network error) — continuing with current strategy",
                        task_id,
                    )

        # All rounds exhausted → BLOCKED (AUTO-C6 will write knowledge + ticket).
        self.state.set_task_status(task_id, STATUS_BLOCKED)
        tracer.event("outer_loop", "controller", "result",
                     params={"task": task_id, "passed": False,
                             "rounds": self.max_rounds, "exhausted": True,
                             "impl_version": impl_version})
        return OuterLoopResult(task_id, False, self.max_rounds, True,
                               feedback_files, inner_results, impl_versions_used)

    # ── private ──────────────────────────────────────────────────────────────

    def _feedback_paths(self, task_id: str) -> list[Path]:
        """Existing feedback files, sorted by round number (numeric)."""
        d = self.state.task_dir(task_id)
        paths = list(d.glob(_FEEDBACK_GLOB))
        return sorted(paths, key=lambda p: self._round_of(p.name))

    @staticmethod
    def _round_of(name: str) -> int:
        m = _FEEDBACK_RE.search(name)
        return int(m.group(1)) if m else 0

    def _existing_rounds(self, task_id: str) -> int:
        # Delegates to the shared helper (tools.auto.utils) also used by
        # AutoController's BLOCKED-reset check, so the two can never disagree
        # about how many rounds a task has actually used (see the
        # round-exhaustion bugfix in controller.py's run()).
        return highest_completed_round(self.state.task_dir(task_id))

    def _read_round_feedback(self, task_id: str) -> list[str]:
        """Return the compact summary text of each prior round, in order."""
        out: list[str] = []
        for p in self._feedback_paths(task_id):
            try:
                out.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
        return out

    def _write_round_feedback(
        self, task_id: str, rnd: int, res, impl_version: int = 1
    ) -> Path:
        """Distil a failed round into ONE compact markdown file."""
        last = _truncate(getattr(res, "last_feedback", "") or "", _MAX_FEEDBACK_CHARS)
        attempts = getattr(res, "attempts_used", "?")
        body = (
            f"# Round {rnd} — impl v{impl_version} — task {task_id}\n"
            f"{attempts} attempt(s), all failed.\n\n"
            f"Final issue to fix next round:\n{last}\n"
        )
        return self.state.write_task_file(task_id, f"feedback_round_{rnd}.md", body)

    def _build_impl_history(
        self,
        task_id: str,
        current_impl_version: int,
        original_instruction: str,
    ) -> list[dict]:
        """Return one entry per impl version < current_impl_version (LOOP-4).

        Each entry has keys:
          version          — int, e.g. 1
          strategy_summary — first line of the instruction used for that version
          why_failed       — first line of the last failure for that version

        Reads rewrite_round_*.md for instructions and feedback_round_*.md for
        failures so it works correctly on resumed runs too.
        """
        if current_impl_version <= 1:
            return []

        d = self.state.task_dir(task_id)

        # ── instruction per impl version ─────────────────────────────────────
        impl_instruction: dict[int, str] = {1: original_instruction}
        for rpath in sorted(d.glob("rewrite_round_*.md")):
            try:
                text = rpath.read_text(encoding="utf-8")
                first_line = text.splitlines()[0] if text else ""
                m = _IMPL_HEADER_RE.search(first_line)
                if not m:
                    continue
                ver = int(m.group(1))
                if "## New instruction\n" in text:
                    instr = text.split("## New instruction\n", 1)[1]
                    if "## Acceptance check" in instr:
                        instr = instr.split("## Acceptance check")[0]
                    impl_instruction[ver] = instr.strip()
            except (OSError, ValueError):
                continue

        # ── last failure per impl version ─────────────────────────────────────
        impl_last_failure: dict[int, str] = {}
        for fpath in self._feedback_paths(task_id):
            try:
                text = fpath.read_text(encoding="utf-8")
                first_line = text.splitlines()[0] if text else ""
                m = _IMPL_HEADER_RE.search(first_line)
                if not m:
                    continue
                ver = int(m.group(1))
                if "Final issue to fix next round:\n" in text:
                    issue = text.split("Final issue to fix next round:\n", 1)[1].strip()
                    impl_last_failure[ver] = issue   # last file wins → highest round
            except (OSError, ValueError):
                continue

        # ── assemble one entry per previous version ───────────────────────────
        result: list[dict] = []
        for ver in range(1, current_impl_version):
            raw_instr   = impl_instruction.get(ver, "(unknown strategy)")
            raw_failure = impl_last_failure.get(ver, "(reason not recorded)")
            # Keep each entry to one compact line so coder context stays short.
            summary = raw_instr.splitlines()[0][:160] if raw_instr else ""
            failure = raw_failure.splitlines()[0][:200] if raw_failure else ""
            result.append({
                "version":          ver,
                "strategy_summary": summary,
                "why_failed":       failure,
            })
        return result


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    return text if len(text) <= max_chars else text[:max_chars] + f"… [+{len(text) - max_chars} chars]"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_outer_loop(
    config: configparser.ConfigParser,
    base_dir: str | Path,
    state: StateStore,
    *,
    inner_loop=None,
    task_mode: str = "code",
    run_goal: str = "",
) -> OuterLoop:
    """Build an :class:`OuterLoop`, constructing the inner loop from config
    unless one is injected (tests / the controller may supply their own).

    AUTO-DM-1: ``task_mode`` is forwarded to ``make_inner_loop`` and stored
    on the constructed inner loop's validator so domain-aware prompts are used.
    Defaults to ``"code"`` — no behavioural change for existing call sites.

    AUTO-CR-22-1: ``run_goal`` is forwarded to ``make_inner_loop`` so the
    fact/prosody gates see the run goal even when the architect didn't echo
    it into a task's own ``instruction``.
    """
    max_rounds             = config.getint("auto", "max_rounds_per_task",
                                           fallback=_DEFAULT_MAX_ROUNDS)
    rewrite_every_n_rounds = config.getint("auto", "rewrite_every_n_rounds", fallback=2)
    max_rewrites           = config.getint("auto", "max_rewrites",           fallback=5)

    if inner_loop is None:
        inner_loop = make_inner_loop(config, base_dir, task_mode=task_mode,
                                      run_goal=run_goal)  # AUTO-DM-1 / AUTO-CR-22-1

    # LOOP-2: build a TaskRewriter only if rewrite keys + max_rewrites > 0 are
    # configured. AUTO-CR-27: skip it in creative mode — its code-test-framed
    # prompt is meaningless there and previously wasted a call while nudging
    # the model toward emitting code mid-story.
    task_rewriter = None
    if max_rewrites > 0 and task_mode != "creative":
        try:
            from tools.auto.architect import TaskRewriter

            active     = config.get("api", "active", fallback="local")
            section    = f"api_{active}"
            base_url   = config.get(section, "base_url")
            api_key    = config.get(section, "api_key",    fallback="")
            model      = config.get(section, "model")
            api_fmt    = config.get(section, "api_format", fallback="openai")
            verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

            task_rewriter = TaskRewriter(
                config=config,
                base_url=base_url,
                api_key=api_key,
                model=model,
                api_format=api_fmt,
                verify_ssl=verify_ssl,
            )
        except Exception as exc:
            logger.warning(
                "make_outer_loop: could not build TaskRewriter — rewriting disabled: %s",
                exc,
            )

    return OuterLoop(
        inner_loop,
        state,
        max_rounds=max_rounds,
        rewrite_every_n_rounds=rewrite_every_n_rounds,
        max_rewrites=max_rewrites,
        task_rewriter=task_rewriter,
    )
