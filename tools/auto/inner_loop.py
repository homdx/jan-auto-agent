"""tools/auto/inner_loop.py — AUTO-C3: inner attempt loop + Gate 2.

Ties the Coder (AUTO-C2) and Executor (AUTO-C1) together into the bounded
inner loop for ONE task:

    for attempt in 1 .. max_attempts (default 5):
        1. Coder.generate(task, prior_feedback)   → writes files
        2. Executor.run(task)                      → runs the acceptance check
        3. Gate 2: acceptance check PASSED *and* the validator APPROVES
           → success, stop.
        otherwise: record what went wrong, feed it back, try again.

Gate 2 (completion gate) is deliberately two-part:
  * objective  — the acceptance check command must exit 0 (Executor.passed), and
  * judged     — a validator must approve the change.
Both must hold.  The validator is **fail-closed**: any infra/parse error counts
as "not approved", never a false pass.

The loop never raises on a coder/executor/validator failure — each becomes
feedback for the next attempt.  Committing the result is AUTO-C5's job; this
module only decides pass/fail and produces the feedback that AUTO-C4 turns into
a round-feedback file.

Public surface:

    from tools.auto.inner_loop import InnerLoop, InnerLoopResult, make_inner_loop

    loop = make_inner_loop(config, base_dir)        # real coder/executor/validator
    result = loop.run_task(task, base_dir, prior_feedback=[])
    if result.passed: ...                           # AUTO-C5 commits

agents.ini [auto] keys
----------------------
max_attempts_per_task — inner-loop cap (default 5)
"""

from __future__ import annotations

import configparser
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Tuple

from tools.agent_trace import tracer
from tools.llm_stream import request_completion, strip_think

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 5
_MAX_DETAIL_CHARS = 600


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AttemptRecord:
    """Outcome of a single inner-loop attempt."""
    attempt:    int
    coder_ok:   bool
    exec_passed: bool
    approved:   bool
    feedback:   str = ""

    @property
    def passed(self) -> bool:
        """Gate 2: acceptance check passed AND validator approved."""
        return self.coder_ok and self.exec_passed and self.approved


@dataclass
class InnerLoopResult:
    """Aggregate result of the inner loop for one task."""
    task_id:       str
    passed:        bool
    attempts_used: int
    records:       list[AttemptRecord] = field(default_factory=list)
    last_feedback: str = ""

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (f"[{self.task_id}] inner-loop {status} after "
                f"{self.attempts_used} attempt(s)")


# ─────────────────────────────────────────────────────────────────────────────
# Validator protocol + default LLM implementation (Gate 2, judged half)
# ─────────────────────────────────────────────────────────────────────────────

class Gate2Validator(Protocol):
    """Anything with this shape can serve as the Gate-2 judge."""
    def approve(self, task: dict, exec_result, coder_result) -> Tuple[bool, str]:
        ...


class LLMGate2Validator:
    """Default Gate-2 validator: asks the model whether the change correctly
    implements the task.  Fail-closed — any error → (False, reason)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        api_format: str = "openai",
        verify_ssl: bool = True,
        timeout: float = 120,
    ) -> None:
        self._base_url   = base_url.rstrip("/")
        self._api_key    = api_key
        self._model      = model
        self._api_format = api_format
        
        import ssl
        self._ssl_context = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx
            
        self._timeout    = timeout

    def approve(self, task: dict, exec_result, coder_result) -> Tuple[bool, str]:
        system = (
            "You are a strict code-change validator. You are given a TASK, the "
            "files a coder wrote, and the ACCEPTANCE CHECK output (which already "
            "exited 0). Decide whether the change correctly and completely "
            "implements the task without introducing an obvious regression. "
            'Return STRICT JSON only: {"approved": true or false, '
            '"feedback": "what is wrong or missing; empty if approved"}'
        )
        files = ", ".join(getattr(coder_result, "files_written", []) or [])
        stdout = _truncate(getattr(exec_result, "stdout", "") or "", _MAX_DETAIL_CHARS)
        user = (
            f"TASK: {task.get('title','')}\n"
            f"INSTRUCTION: {task.get('instruction','')}\n"
            f"FILES WRITTEN: {files}\n"
            f"ACCEPTANCE CHECK: {task.get('acceptance_check','')}\n"
            f"ACCEPTANCE OUTPUT (exit 0):\n{stdout}\n"
        )
        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._api_key}"}
        payload = {"model": self._model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.1}
        tracer.event("inner_loop", "gate2_validator", "llm_request",
                     params={"task": task.get("id")}, content=user,
                     model=self._model, temperature=0.1)
        try:
            raw = request_completion(
                url=url, headers=headers, payload=payload, timeout=self._timeout,
                stream=False, api_format=self._api_format, ssl_context=self._ssl_context,
            )
            cleaned = strip_think(raw)
            tracer.event("gate2_validator", "inner_loop", "llm_response", content=cleaned)
            if "```json" in cleaned:
                cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
            data = json.loads(cleaned)
            return bool(data.get("approved", False)), str(data.get("feedback", "") or "")
        except Exception as exc:  # noqa: BLE001 — fail closed on ANY error
            logger.warning("Gate2 validator failed (fail-closed): %s", exc)
            return False, f"validator unavailable: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# InnerLoop
# ─────────────────────────────────────────────────────────────────────────────

class InnerLoop:
    """Runs the bounded inner attempt loop for one task (AUTO-C3)."""

    def __init__(
        self,
        coder,
        executor,
        validator: Gate2Validator,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.coder        = coder
        self.executor     = executor
        self.validator    = validator
        self.max_attempts = max(1, int(max_attempts))

    def run_task(
        self,
        task: dict,
        base_dir: str | Path,
        prior_feedback: Optional[list[str]] = None,
    ) -> InnerLoopResult:
        """Attempt to complete *task* in up to ``max_attempts`` tries.

        *prior_feedback* (from earlier AUTO-C4 rounds) seeds the first attempt's
        coder context.  Returns an :class:`InnerLoopResult`; never raises.
        """
        task_id = task.get("id", "?")
        feedback: list[str] = list(prior_feedback or [])
        records: list[AttemptRecord] = []

        tracer.event("controller", "inner_loop", "run_start",
                     params={"task": task_id, "max_attempts": self.max_attempts})

        for attempt in range(1, self.max_attempts + 1):
            # 1) Coder
            cr = self.coder.generate(task, base_dir, prior_feedback=feedback)
            if not getattr(cr, "succeeded", False):
                fb = f"attempt {attempt}: coder failed — {getattr(cr, 'error', '') or 'no files written'}"
                records.append(AttemptRecord(attempt, False, False, False, fb))
                feedback.append(fb)
                self._trace_attempt(task_id, attempt, "coder_failed", fb)
                continue

            # 2) Executor (objective half of Gate 2)
            er = self.executor.run(task)
            if not getattr(er, "passed", False):
                detail = (getattr(er, "traceback", "") or
                          getattr(er, "stderr", "") or
                          getattr(er, "stdout", ""))
                timeout = ", timeout" if getattr(er, "timed_out", False) else ""
                fb = (f"attempt {attempt}: acceptance check failed "
                      f"(rc={getattr(er, 'exit_code', '?')}{timeout}) — "
                      f"{_truncate(detail, _MAX_DETAIL_CHARS)}")
                records.append(AttemptRecord(attempt, True, False, False, fb))
                feedback.append(fb)
                self._trace_attempt(task_id, attempt, "exec_failed", fb)
                continue

            # 3) Validator (judged half of Gate 2) — fail-closed
            approved, vfb = self.validator.approve(task, er, cr)
            if approved:
                records.append(AttemptRecord(attempt, True, True, True, ""))
                self._trace_attempt(task_id, attempt, "passed", "")
                return InnerLoopResult(task_id, True, attempt, records, "")

            fb = f"attempt {attempt}: validator rejected — {vfb}"
            records.append(AttemptRecord(attempt, True, True, False, fb))
            feedback.append(fb)
            self._trace_attempt(task_id, attempt, "validator_rejected", fb)

        last = feedback[-1] if feedback else ""
        tracer.event("inner_loop", "controller", "result",
                     params={"task": task_id, "passed": False,
                             "attempts": self.max_attempts})
        return InnerLoopResult(task_id, False, self.max_attempts, records, last)

    # ── private ──────────────────────────────────────────────────────────────

    def _trace_attempt(self, task_id: str, attempt: int, outcome: str, fb: str) -> None:
        tracer.event("inner_loop", "controller", "decision",
                     params={"task": task_id, "attempt": attempt, "outcome": outcome},
                     content=fb)


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"… [+{len(text) - max_chars} chars]"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_inner_loop(
    config: configparser.ConfigParser,
    base_dir: str | Path,
    *,
    coder=None,
    executor=None,
    validator: Optional[Gate2Validator] = None,
) -> InnerLoop:
    """Build an :class:`InnerLoop` with real Coder/Executor/validator from config."""
    from tools.auto.coder import make_coder
    from tools.auto.executor import make_executor

    max_attempts = config.getint("auto", "max_attempts_per_task",
                                 fallback=_DEFAULT_MAX_ATTEMPTS)

    if coder is None:
        coder = make_coder(config)

    if executor is None:
        exec_timeout = config.getfloat("auto", "exec_timeout_sec", fallback=120)
        executor = make_executor(base_dir, timeout_sec=exec_timeout)

    if validator is None:
        active     = config.get("api", "active", fallback="local")
        section    = f"api_{active}"
        validator = LLMGate2Validator(
            base_url   = config.get(section, "base_url", fallback="http://localhost:1337/v1"),
            api_key    = config.get(section, "api_key", fallback=""),
            model      = config.get(section, "model", fallback=""),
            api_format = config.get(section, "api_format", fallback="openai"),
            verify_ssl = config.getboolean("api", "verify_ssl", fallback=True),
            timeout    = config.getfloat("auto", "llm_timeout_sec", fallback=120),
        )

    return InnerLoop(coder, executor, validator, max_attempts=max_attempts)