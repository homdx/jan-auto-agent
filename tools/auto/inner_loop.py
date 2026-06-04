"""tools/auto/inner_loop.py — AUTO-C3: per-round attempt loop (Gate 2).

Runs up to ``max_attempts`` coder → executor → validator cycles for one task
within a single outer round.  Each attempt:

  1. Calls the coder agent to produce / fix the target code.
  2. Calls the executor to run the acceptance check (objective half of Gate 2).
  3. If exec passes, calls the validator (subjective half of Gate 2).
  4. Both halves must pass → InnerLoopResult(passed=True).
  5. Either half fails → build structured feedback (LOOP-1), add to context,
     continue to the next attempt.

LOOP-1 — Structured validator feedback
---------------------------------------
When the LLMGate2Validator rejects, it returns a feedback string that already
contains Reason / Hints / Suggested approach.  The coder sees this on the next
attempt, making feedback prescriptive rather than just diagnostic.

Public surface::

    from tools.auto.inner_loop import (
        InnerLoop, InnerLoopResult, AttemptRecord,
        LLMGate2Validator, make_inner_loop,
    )

    inner = make_inner_loop(config, base_dir)
    result = inner.run_task(task, base_dir, prior_feedback=[...])

agents.ini keys consumed
------------------------
[auto]  max_attempts_per_task   — attempt cap per round (default 5)
[validator_agent] temperature   — validator temperature (default 0.1)
[validator_agent] max_hints     — max hint items in rejection (default 3)
"""

from __future__ import annotations

import configparser
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 5

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AttemptRecord:
    """Record of a single coder → executor → validator attempt."""
    attempt_num:   int
    coder_ok:      bool
    exec_ok:       bool
    validator_ok:  bool
    feedback:      str

    @property
    def passed(self) -> bool:
        return self.coder_ok and self.exec_ok and self.validator_ok


@dataclass
class InnerLoopResult:
    """Result of one inner-loop run (one outer round)."""
    task_id:       str   = ""
    passed:        bool  = False
    attempts_used: int   = 0
    last_feedback: str   = ""
    records:       list  = field(default_factory=list)   # list[AttemptRecord]
    hint_history:  list  = field(default_factory=list)   # list[str] — LOOP-4


# ─────────────────────────────────────────────────────────────────────────────
# LLM-backed Gate-2 validator  (LOOP-1)
# ─────────────────────────────────────────────────────────────────────────────

_GATE2_SYSTEM = (
    "You are a code-change validator. "
    "Given a task description, execution output, and the generated code, "
    "decide whether the implementation is complete and correct.\n"
    "Return ONLY a JSON object — no text before or after:\n"
    '{"approved": true|false, "feedback": "<one sentence reason>", '
    '"hints": ["<actionable hint 1>", ...], '
    '"suggested_approach": "<optional one-sentence alternative>"}\n'
    "HINTS RULES:\n"
    "  - Each hint MUST point to a specific name, line, or pattern in the code.\n"
    "  - Good: 'import re is used on line 12 but not present in imports'.\n"
    "  - Bad: 'make sure the code is correct'.\n"
    "  - Omit the hints array (or use []) when approved=true.\n"
    "  - suggested_approach is optional — only fill it with a concrete alternative."
)


class LLMGate2Validator:
    """Fail-closed LLM-based Gate-2 validator.

    Calls the model and parses ``{"approved": bool, "feedback": str, ...}``.
    Any network / parse error returns ``(False, "validator unavailable: …")``.
    """

    def __init__(
        self,
        base_url:   str  = "http://localhost:1337/v1",
        model:      str  = "qwen2.5-14b-instruct",
        api_key:    str  = "jan",
        api_format: str  = "openai",
        temperature: float = 0.1,
        timeout:    int  = 120,
        max_hints:  int  = 3,
        ssl_context = None,
    ):
        self.base_url    = base_url
        self.model       = model
        self.api_key     = api_key
        self.api_format  = api_format
        self.temperature = temperature
        self.timeout     = timeout
        self.max_hints   = max(1, int(max_hints))
        self.ssl_context = ssl_context

    # ------------------------------------------------------------------

    def approve(
        self,
        task:         dict,
        exec_result,
        coder_result,
    ) -> tuple[bool, str]:
        """Return (approved, feedback_string).  Never raises — fail-closed."""
        try:
            from tools.llm_stream import request_completion, strip_think

            if self.api_format == "ollama":
                from tools.llm_stream import ollama_chat_url
                url = ollama_chat_url(self.base_url)
            else:
                url = f"{self.base_url.rstrip('/')}/chat/completions"

            headers = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

            user_msg = (
                f"Task: {task.get('instruction', '')}\n\n"
                f"Acceptance check exit code: {getattr(exec_result, 'exit_code', 0)}\n"
                f"stdout:\n{getattr(exec_result, 'stdout', '')[:2000]}\n\n"
                f"Generated files:\n"
                + "\n".join(
                    getattr(coder_result, "files_written", []) or []
                )
            )

            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system",  "content": _GATE2_SYSTEM},
                    {"role": "user",    "content": user_msg},
                ],
                "temperature": self.temperature,
            }

            raw = request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self.timeout,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            )
            raw = strip_think(raw)
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            parsed   = json.loads(raw)
            approved = bool(parsed.get("approved", False))
            if approved:
                return True, ""

            # Build LOOP-1 structured feedback string
            return False, _format_gate2_feedback(parsed, self.max_hints)

        except Exception as exc:
            logger.warning("LLMGate2Validator error: %s", exc)
            return False, f"validator unavailable: {exc}"


def _format_gate2_feedback(parsed: dict, max_hints: int) -> str:
    """Build a structured rejection string from a Gate-2 dict (LOOP-1)."""
    feedback = parsed.get("feedback", "no reason given")
    hints    = (parsed.get("hints") or [])[:max_hints]
    approach = parsed.get("suggested_approach", "")

    lines = [f"Reason: {feedback}"]
    if hints:
        lines.append("Hints:")
        for i, h in enumerate(hints, 1):
            lines.append(f"  {i}. {h}")
    if approach:
        lines.append(f"Suggested approach: {approach}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# InnerLoop
# ─────────────────────────────────────────────────────────────────────────────

class InnerLoop:
    """Runs up to ``max_attempts`` coder → executor → validator cycles.

    Agents are injected so this class stays unit-testable without live LLMs.
    ``make_inner_loop`` constructs real agents from config for production.

    Gate 2 requires BOTH halves:
      * executor.run(task) must return a result with passed=True, AND
      * validator.approve(task, exec_result, coder_result) must return True.
    If the exec fails the validator is not called at all.
    """

    def __init__(
        self,
        coder,
        executor,
        validator,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ):
        self.coder        = coder
        self.executor     = executor
        self.validator    = validator
        self.max_attempts = max(1, int(max_attempts))

    # ------------------------------------------------------------------

    def run_task(
        self,
        task:             dict,
        base_dir:         str | Path,
        *,
        prior_feedback:   list[str] | None = None,
        prior_implementations: list[dict] | None = None,   # LOOP-4
    ) -> InnerLoopResult:
        """Run up to ``max_attempts`` Gate-2 cycles for *task*.

        Returns:
            :class:`InnerLoopResult` with ``passed`` flag, attempt count,
            last feedback string, and per-attempt records.
        """
        task_id = task.get("id", "")
        feedback: list[str] = list(prior_feedback or [])
        records:  list[AttemptRecord] = []

        # LOOP-4: prepend prior implementation history
        if prior_implementations:
            history_lines = [
                "PREVIOUS IMPLEMENTATION STRATEGIES — do not repeat these approaches:"
            ]
            for entry in prior_implementations:
                v       = entry.get("version", "?")
                summary = entry.get("strategy_summary", "")
                why     = entry.get("why_failed", "")
                history_lines.append(f"  v{v}: tried {summary} — failed because {why}")
            feedback.insert(0, "\n".join(history_lines))

        for attempt in range(1, self.max_attempts + 1):

            # ── 1. Coder ──────────────────────────────────────────────────────
            try:
                coder_result = self.coder.generate(
                    task, base_dir, prior_feedback=feedback
                )
            except Exception as exc:
                logger.error("InnerLoop: coder raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: coder error — {exc}"
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            if not getattr(coder_result, "succeeded", True):
                fb = f"attempt {attempt}: coder failed — {getattr(coder_result, 'error', 'unknown error')}"
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            # ── 2. Executor (objective half of Gate 2) ────────────────────────
            try:
                exec_result = self.executor.run(task)
            except Exception as exc:
                logger.error("InnerLoop: executor raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: executor error — {exc}"
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, False, False, fb))
                continue

            if not getattr(exec_result, "passed", False):
                tb  = getattr(exec_result, "traceback", "") or ""
                out = getattr(exec_result, "stdout",    "") or ""
                ec  = getattr(exec_result, "exit_code", 1)
                fb  = (
                    f"attempt {attempt}: exec failed (exit {ec})\n"
                    + (f"traceback:\n{tb}" if tb else f"stdout:\n{out[:400]}")
                )
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, False, False, fb))
                continue

            # ── 3. Validator (subjective half of Gate 2) ─────────────────────
            try:
                approved, vfb = self.validator.approve(task, exec_result, coder_result)
            except Exception as exc:
                logger.error("InnerLoop: validator raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: validator error — {exc}"
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, True, False, fb))
                continue

            if not approved:
                fb = f"attempt {attempt}: validator rejected\n{vfb}"
                logger.info("InnerLoop: attempt %d rejected — %s", attempt, vfb[:80])
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, True, False, fb))
                continue

            # ── APPROVED ──────────────────────────────────────────────────────
            logger.info("InnerLoop: attempt %d APPROVED", attempt)
            records.append(AttemptRecord(attempt, True, True, True, ""))
            return InnerLoopResult(
                task_id=task_id,
                passed=True,
                attempts_used=attempt,
                last_feedback="",
                records=records,
            )

        # All attempts exhausted
        last = feedback[-1] if feedback else ""
        return InnerLoopResult(
            task_id=task_id,
            passed=False,
            attempts_used=self.max_attempts,
            last_feedback=last,
            records=records,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_inner_loop(
    config:   configparser.ConfigParser,
    base_dir: str | Path,
    *,
    coder=None,
    executor=None,
    validator=None,
) -> InnerLoop:
    """Construct an :class:`InnerLoop` with real agents from *config*.

    Any agent may be injected (useful for tests); omitted agents are
    constructed from the config's API / model settings.
    """
    max_attempts = config.getint("auto", "max_attempts_per_task",
                                 fallback=_DEFAULT_MAX_ATTEMPTS)

    # ── API settings ─────────────────────────────────────────────────────────
    active_profile = config.get("api", "active", fallback="local")
    api_section    = f"api_{active_profile}"

    base_url   = config.get(api_section, "base_url",   fallback="http://localhost:1337/v1")
    api_key    = config.get(api_section, "api_key",    fallback="jan")
    model      = config.get(api_section, "model",      fallback="qwen2.5-14b-instruct")
    api_format = config.get(api_section, "api_format", fallback="openai")
    num_ctx    = config.getint(api_section, "num_ctx",  fallback=0)

    verify_ssl_raw = config.get("api", "verify_ssl", fallback="true")
    verify_ssl     = verify_ssl_raw.strip().lower() not in ("false", "0", "no")

    import ssl
    ssl_context: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode    = ssl.CERT_NONE

    stream     = config.getboolean("output", "stream_agents", fallback=False)
    max_hints  = config.getint("validator_agent", "max_hints", fallback=3)
    val_temp   = config.getfloat("validator_agent", "temperature", fallback=0.1)
    exec_timeout = config.getint("auto", "exec_timeout_sec", fallback=120)

    # ── Coder ─────────────────────────────────────────────────────────────────
    if coder is None:
        try:
            from tools.auto.coder import make_coder  # type: ignore
            coder = make_coder(config)
        except ImportError:
            logger.warning("Coder not found — using _StubCoder (tests only)")
            coder = _StubCoder()

    # ── Executor ──────────────────────────────────────────────────────────────
    if executor is None:
        try:
            from tools.auto.executor import make_executor  # type: ignore
            executor = make_executor(base_dir=base_dir, timeout_sec=exec_timeout)
        except ImportError:
            logger.warning("Executor not found — using _StubExecutor (tests only)")
            executor = _StubExecutor()

    # ── Validator ─────────────────────────────────────────────────────────────
    if validator is None:
        validator = LLMGate2Validator(
            base_url=base_url,
            model=model,
            api_key=api_key,
            api_format=api_format,
            temperature=val_temp,
            timeout=120,
            max_hints=max_hints,
            ssl_context=ssl_context,
        )

    return InnerLoop(coder, executor, validator, max_attempts=max_attempts)


# ── Stubs for environments without real agents (unit tests) ──────────────────

class _StubCoder:
    def generate(self, task, base_dir, prior_feedback=None):
        from types import SimpleNamespace
        return SimpleNamespace(succeeded=False, files_written=[], error="stub coder — no real coder available")


class _StubExecutor:
    def run(self, task):
        from types import SimpleNamespace
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="", traceback="stub executor", timed_out=False)
