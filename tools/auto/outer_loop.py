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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer
from tools.auto.state import StateStore, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROUNDS = 10
_FEEDBACK_GLOB = "feedback_round_*.md"
_FEEDBACK_RE = re.compile(r"feedback_round_(\d+)\.md$")
_MAX_FEEDBACK_CHARS = 800        # keep each round file compact


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OuterLoopResult:
    """Aggregate result of the outer round loop for one task."""
    task_id:        str
    passed:         bool
    rounds_used:    int
    exhausted:      bool
    feedback_files: list[str] = field(default_factory=list)
    inner_results:  list = field(default_factory=list)   # list[InnerLoopResult]

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
    ) -> None:
        self.inner_loop = inner_loop
        self.state      = state
        self.max_rounds = max(1, int(max_rounds))

    def run_task(self, task: dict, base_dir: str | Path) -> OuterLoopResult:
        """Run the outer loop for *task*.  Resumes from the next unfinished
        round if feedback files already exist.  Never raises."""
        task_id = task.get("id", "?")

        # ── Resume: existing feedback files mean prior rounds already ran ──
        done_rounds = self._existing_rounds(task_id)
        start_round = done_rounds + 1
        feedback_files = [str(p) for p in self._feedback_paths(task_id)]
        inner_results: list = []

        if start_round > self.max_rounds:
            # Already exhausted in a prior session.
            self.state.set_task_status(task_id, STATUS_BLOCKED)
            return OuterLoopResult(task_id, False, self.max_rounds, True,
                                   feedback_files, inner_results)

        self.state.set_task_status(task_id, STATUS_IN_PROGRESS)
        tracer.event("controller", "outer_loop", "run_start",
                     params={"task": task_id, "start_round": start_round,
                             "max_rounds": self.max_rounds})

        for rnd in range(start_round, self.max_rounds + 1):
            # Fresh context: seed ONLY with the compact prior-round summaries.
            prior = self._read_round_feedback(task_id)
            self.state.set_task_status(task_id, STATUS_IN_PROGRESS, round=rnd)

            res = self.inner_loop.run_task(task, base_dir, prior_feedback=prior)
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
                             params={"task": task_id, "passed": True, "round": rnd})
                return OuterLoopResult(task_id, True, rnd, False,
                                       feedback_files, inner_results)

            # Failed round → write ONE compact feedback file, then fresh round.
            fpath = self._write_round_feedback(task_id, rnd, res)
            feedback_files.append(str(fpath))
            self.state.log(f"{task_id}: round {rnd} failed — wrote {fpath.name}")

        # All rounds exhausted → BLOCKED (AUTO-C6 will write knowledge + ticket).
        self.state.set_task_status(task_id, STATUS_BLOCKED)
        tracer.event("outer_loop", "controller", "result",
                     params={"task": task_id, "passed": False,
                             "rounds": self.max_rounds, "exhausted": True})
        return OuterLoopResult(task_id, False, self.max_rounds, True,
                               feedback_files, inner_results)

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
        paths = self._feedback_paths(task_id)
        return self._round_of(paths[-1].name) if paths else 0

    def _read_round_feedback(self, task_id: str) -> list[str]:
        """Return the compact summary text of each prior round, in order."""
        out: list[str] = []
        for p in self._feedback_paths(task_id):
            try:
                out.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
        return out

    def _write_round_feedback(self, task_id: str, rnd: int, res) -> Path:
        """Distil a failed round into ONE compact markdown file."""
        last = _truncate(getattr(res, "last_feedback", "") or "", _MAX_FEEDBACK_CHARS)
        attempts = getattr(res, "attempts_used", "?")
        body = (
            f"# Round {rnd} — task {task_id}\n"
            f"{attempts} attempt(s), all failed.\n\n"
            f"Final issue to fix next round:\n{last}\n"
        )
        return self.state.write_task_file(task_id, f"feedback_round_{rnd}.md", body)


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
) -> OuterLoop:
    """Build an :class:`OuterLoop`, constructing the inner loop from config
    unless one is injected (tests / the controller may supply their own)."""
    max_rounds = config.getint("auto", "max_rounds_per_task",
                               fallback=_DEFAULT_MAX_ROUNDS)
    if inner_loop is None:
        from tools.auto.inner_loop import make_inner_loop
        inner_loop = make_inner_loop(config, base_dir)
    return OuterLoop(inner_loop, state, max_rounds=max_rounds)
