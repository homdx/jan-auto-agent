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
from tools.auto.context_broker import ContextBroker

from dataclasses import dataclass, field
from pathlib import Path

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
    context_satisfied: bool = True   # pull-model: False ⇒ last attempt still needed context


# ─────────────────────────────────────────────────────────────────────────────
# LLM-backed Gate-2 validator  (LOOP-1)
# ─────────────────────────────────────────────────────────────────────────────

_GATE2_SYSTEM_CODE = (
    "You are a code-change validator. "
    "Given a task description, execution output, and the generated code, "
    "decide whether the implementation is complete and correct.\n"
    "Return ONLY a JSON object — no text before or after:\n"
    '{"approved": true|false, "feedback": "<one sentence reason>", '
    '"hints": ["<actionable hint 1>", ...], '
    '"suggested_approach": "<optional one-sentence alternative>", '
    '"missing_context": ["<symbol you needed to see but were not shown>", ...]}\n'
    "Use missing_context ONLY when you cannot verify correctness because a "
    "referenced symbol's definition was not provided; otherwise omit it.\n"
    "HINTS RULES:\n"
    "  - Each hint MUST point to a specific name, line, or pattern in the code.\n"
    "  - Good: 'import re is used on line 12 but not present in imports'.\n"
    "  - Bad: 'make sure the code is correct'.\n"
    "  - Omit the hints array (or use []) when approved=true.\n"
    "  - suggested_approach is optional — only fill it with a concrete alternative."
)

_GATE2_SYSTEM_DOCS = (
    "You are a documentation change validator. "
    "Given a task description and the revised file content, decide whether "
    "the documentation improvement is complete and accurate. "
    "Return ONLY a JSON object — no text before or after:\n"
    '{"approved": true|false, "feedback": "<one sentence>", '
    '"hints": ["<specific hint>", ...], "suggested_approach": "<optional>"}\n'
    "HINTS RULES:\n"
    "  - Each hint MUST point to a specific section, heading, or line.\n"
    "  - Good: 'The installation section on line 42 still uses the old command'.\n"
    "  - Bad: 'make sure the docs are clear'.\n"
    "  - Omit hints (or use []) when approved=true."
)

_GATE2_SYSTEM_CREATIVE = (
    "You are a creative writing editor validating a chapter. "
    "Given a task description and the generated chapter prose, decide whether "
    "the chapter fulfils the task and reads as coherent, complete prose. "
    "Reply with ONE line only. "
    "The first token must be APPROVED or REVISE. "
    "If REVISE, follow immediately with ': ' and one concrete reason that "
    "points at a specific passage, character, or continuity issue. "
    "Examples of valid replies:\n"
    "  APPROVED\n"
    "  REVISE: the duel in paragraph 3 contradicts the earlier truce in chapter_02\n"
    "  REVISE: Elena's eye colour changes from blue to green mid-scene\n"
    "Do NOT return JSON. Do NOT add preamble. One line, first token APPROVED or REVISE."
)

# Backward-compatibility alias
_GATE2_SYSTEM = _GATE2_SYSTEM_CODE

_GATE2_SYSTEMS: dict[str, str] = {
    "code":     _GATE2_SYSTEM_CODE,
    "docs":     _GATE2_SYSTEM_DOCS,
    "creative": _GATE2_SYSTEM_CREATIVE,
}


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
        base_dir:   str  = ".",
        num_ctx:    int  = 0,
        max_tokens: int  = 512,
        task_mode:  str  = "code",
        config = None,
    ):
        self.base_url    = base_url
        self.model       = model
        self.api_key     = api_key
        self.api_format  = api_format
        self.temperature = temperature
        self.timeout     = timeout
        self.max_hints   = max(1, int(max_hints))
        self.ssl_context = ssl_context
        self.base_dir    = Path(base_dir)
        self.last_missing_context: list[str] = []
        self.num_ctx     = int(num_ctx)
        self.max_tokens  = int(max_tokens)
        self.task_mode   = str(task_mode)
        # AUTO-DM-5: select system prompt — agents.ini override wins over built-in.
        # Priority (mirrors Coder/Architect): mode-specific key (system_docs /
        # system_creative) > legacy "system" key > built-in constant.
        # This allows independent per-mode validator prompt overrides without
        # clobbering the other modes, which a single "system" key cannot do.
        _builtin = _GATE2_SYSTEMS.get(self.task_mode, _GATE2_SYSTEM_CODE)
        if config is not None:
            _mode_key = f"system_{self.task_mode}" if self.task_mode != "code" else None
            if _mode_key and config.has_option("validator_agent", _mode_key):
                self._system = config.get("validator_agent", _mode_key).strip()
            else:
                self._system = config.get("validator_agent", "system", fallback=_builtin).strip()
        else:
            self._system = _builtin

        # Task 3 — smart context additions
        from tools.search_agent import make_search_agent
        self._search_agent = make_search_agent(config, base_dir) if config else None
        self._context_probe_enabled = (
            config.getboolean("coder", "context_probe", fallback=True) if config else True
        )
        self._max_chars_per_dep = (
            config.getint("coder", "max_chars_per_dep", fallback=2000) if config else 2000
        )

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    def _read_changed_content(self, coder_result, task: dict | None = None,
                              base_dir: "Path | None" = None) -> str:
        """Read the post-edit content of the files the coder wrote, so the
        validator judges the ACTUAL code (not just file names).  The Gate-2
        system prompt promises 'the generated code' and asks for line/pattern
        specific hints, so the code must be present in the prompt.

        ``base_dir`` overrides ``self.base_dir`` for this call, allowing the
        inner loop to pass the per-invocation working directory without
        requiring a construction-time match.
        """
        from pathlib import Path as _Path
        _base = Path(base_dir) if base_dir is not None else self.base_dir
        files = list(getattr(coder_result, "files_written", []) or [])
        if not files:
            return "(the coder reported NO files written — nothing changed)"
        budget = max(800, 6000 // max(len(files), 1))
        blocks = []
        for rel in files:
            try:
                content = (_base / rel).read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                blocks.append(f"--- {rel} ---\n(could not read {rel}: {exc})")
                continue

            if len(content) <= budget:
                blocks.append(f"--- {rel} ---\n{content}")
                continue

            ext = _Path(rel).suffix.lower()
            cited_symbol = None
            if task:
                locs = task.get("cited_locations") or []
                if locs and isinstance(locs[0], dict):
                    cited_symbol = locs[0].get("symbol")

            try:
                from tools.auto.coder import chunk_file, select_relevant_chunks
                chunks = chunk_file(content, ext, budget)
                content = select_relevant_chunks(chunks, cited_symbol, budget)
            except Exception as exc:
                logger.warning("validator: smart chunk failed for %s: %s", rel, exc)
                content = content[:budget] + f"\n… [+{len(content) - budget} chars truncated]"

            # blind dep fetch — no probe LLM call
            if self._context_probe_enabled and self._search_agent:
                try:
                    from tools.block_extractor import extract_imports
                    dep_ctx = self._fetch_needed_flat(
                        extract_imports(content, ext)[:4], budget_per=400
                    )
                    if dep_ctx:
                        content += "\n\n## Interfaces and callers\n" + dep_ctx
                except Exception as exc:
                    logger.debug("validator: dep fetch skipped for %s: %s", rel, exc)

            blocks.append(f"--- {rel} ---\n{content}")
        return "\n\n".join(blocks)

    def _fetch_needed_flat(self, symbols: list[str], budget_per: int) -> str:
        """Fetch short snippets for the given symbol names via the search agent."""
        if not self._search_agent or not symbols:
            return ""
        parts = []
        for sym in symbols:
            try:
                result = self._search_agent.run(references=[sym], base_dir=self.base_dir)
                found = result.get("found", {})
                if found:
                    block = next(iter(found.values())).get("code", "")[:budget_per]
                    parts.append(f"### {sym}\n{block}")
            except Exception:
                pass
        return "\n\n".join(parts)

    def approve(
        self,
        task:         dict,
        exec_result,
        coder_result,
        *,
        base_dir=None,
    ) -> tuple[bool, str]:
        """Return (approved, feedback_string).  Never raises — fail-closed.

        Side channel: ``self.last_missing_context`` is set to any symbol names
        the validator reported as needed (pull-model); InnerLoop reads it.
        """
        self.last_missing_context = []
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

            _exec_stderr = getattr(exec_result, 'stderr', '') or ''
            _exec_stdout = getattr(exec_result, 'stdout', '') or ''
            _stderr_section = f"stderr:\n{_exec_stderr[:2000]}\n\n" if _exec_stderr.strip() else ""
            user_msg = (
                f"Task: {task.get('instruction', '')}\n\n"
                f"Acceptance check exit code: {getattr(exec_result, 'exit_code', 0)}\n"
                f"stdout:\n{_exec_stdout[:2000]}\n\n"
                + _stderr_section
                + f"Generated files (CHANGED FILE CONTENT after the coder's edit):\n"
                + self._read_changed_content(coder_result, task=task, base_dir=base_dir)
            )

            if self.api_format == "ollama":
                _val_opts: dict = {"temperature": self.temperature, "num_predict": self.max_tokens}
                if self.num_ctx:
                    _val_opts["num_ctx"] = self.num_ctx
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system",  "content": self._system},
                        {"role": "user",    "content": user_msg},
                    ],
                    "options": _val_opts,
                }
            else:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system",  "content": self._system},
                        {"role": "user",    "content": user_msg},
                    ],
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }

            raw = request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self.timeout,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            )
            raw = strip_think(raw or "")
            # ── Guard: empty response ─────────────────────────────────────────
            # An empty body means the model returned nothing at all (network
            # timeout that still got a 200, or a model that refused silently).
            # json.loads("") raises the cryptic "Expecting value: line 1 column
            # 1 (char 0)" — we surface a clearer message instead.
            if not raw or not raw.strip():
                raise ValueError(
                    "validator model returned an empty response "
                    "(possible network error or silent refusal)"
                )

            # ── Creative mode: line-oriented soft verdict (AUTO-CR-2) ─────────
            if self.task_mode == "creative":
                approved, reason, unparseable = _parse_verdict_soft(raw)
                if unparseable:
                    logger.warning(
                        "LLMGate2Validator [creative]: verdict unparseable — "
                        "passing on fail-open. raw=%r", raw[:120]
                    )
                if approved:
                    return True, ""
                return False, f"Reason: {reason}"

            # ── Code / docs mode: strict JSON path (unchanged) ───────────────
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            parsed   = json.loads(raw)
            # Guard against valid-but-non-object JSON (list / string / null):
            # the .get(...) calls below would otherwise raise AttributeError
            # (caught, but with a cryptic message). Unwrap a single-element list;
            # otherwise raise a clear error → fail-closed in the except handler.
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"validator returned {type(parsed).__name__}, expected JSON object"
                )
            self.last_missing_context = [
                str(x).strip() for x in (parsed.get("missing_context") or [])
                if str(x).strip()
            ]
            approved = bool(parsed.get("approved", False))
            if approved:
                return True, ""

            # Build LOOP-1 structured feedback string
            return False, _format_gate2_feedback(parsed, self.max_hints)

        except Exception as exc:
            logger.warning("LLMGate2Validator error: %s", exc)
            self.last_missing_context = []
            return False, f"validator unavailable: {exc}"


def _parse_verdict_soft(text: str) -> tuple[bool, str, bool]:
    """Parse a line-oriented Gate-2 verdict for creative mode (AUTO-CR-2).

    Protocol: the model is expected to reply with one line whose first token is
    ``APPROVED`` (or ``OK``) or ``REVISE`` / ``REJECT`` / ``NO``.  If ``REVISE``,
    the reason follows after ``: ``.

    Returns
    -------
    (approved, reason, unparseable)
        ``approved``    — True when the verdict is positive.
        ``reason``      — Non-empty string on rejection; note string on fail-open.
        ``unparseable`` — True when no recognised verdict token was found.
                          The caller should log a warning; the verdict is treated
                          as approved (fail-open) so a rambling 8B response cannot
                          hard-block a chapter.

    Acceptance criteria (spec AUTO-CR-2):
        * ``APPROVED`` / ``approved`` / ``OK …``         → approved=True
        * ``REVISE: <reason>``                           → approved=False, reason captured
        * ``REJECT: <reason>`` / ``NO: <reason>``        → approved=False, reason captured
        * Any other content (rambling, JSON, prose, …)  → approved=True (fail-open)
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if upper.startswith("APPROVED") or upper.startswith("OK"):
            return True, "", False

        for token in ("REVISE", "REJECT", "NO"):
            if upper.startswith(token):
                # Extract reason after the token + optional ': '
                rest = stripped[len(token):].lstrip(": ").strip()
                reason = rest if rest else "validator rejected (no reason given)"
                return False, reason, False

        # First non-empty line matched no verdict token → fail-open
        break

    # Empty response or no matching line
    note = "verdict unparseable — passed on fail-open"
    return True, note, True


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
        context_broker=None,
        canon_validator=None,
        task_mode: str = "code",
    ):
        self.coder        = coder
        self.executor     = executor
        self.validator    = validator
        self.max_attempts = max(1, int(max_attempts))
        self._broker      = context_broker or ContextBroker()
        # AUTO-CR-7: optional periodic canon/fact gate (creative mode only).
        self.canon_validator = canon_validator
        self.task_mode    = str(task_mode)

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
        # Pull-model state (carried across attempts within this round)
        prefetched_context: str = ""
        resolved_context: dict[str, str] = {}   # accumulates every symbol the validator has asked for
        _any_missing: bool = False   # Task 4: True if any attempt had unsatisfied context
        _canon_revisions: int = 0     # AUTO-CR-7: canon-driven rejections used so far
        base_dir_path = Path(base_dir)
        target_files  = task.get("target_files", []) or []
        self._broker.reset_cache()  # clear per-task cache; Pass-2 hits re-accumulate fresh

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
                    task, base_dir, prior_feedback=feedback,
                    prefetched_context=prefetched_context,
                )
            except Exception as exc:
                logger.error("InnerLoop: coder raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: coder error — {exc}"
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            # Pull-model: resolve any context the coder asked for, for the NEXT attempt.
            coder_missing = list(getattr(coder_result, "missing_context", []) or [])
            if coder_missing or not getattr(coder_result, "context_satisfied", True):
                _any_missing = True
            if coder_missing:
                # Accumulate into the SAME running context as the validator path
                # so neither side clobbers the other's pulls (was: overwrite via
                # fetch(), which dropped reviewer-accumulated context when the
                # next rejection carried no missing_context of its own).
                newly = self._broker.resolve(coder_missing, target_files, base_dir_path)
                resolved_context.update(newly)
                prefetched_context = self._broker.format_for_prompt(resolved_context)
                logger.info("InnerLoop: attempt %d coder requested context %s — accumulated (%d total)",
                            attempt, coder_missing, len(resolved_context))

            if not getattr(coder_result, "succeeded", True):
                # Context is accumulated above even on coder failure: the next
                # attempt benefits from symbols already resolved, regardless of
                # whether the current attempt produced valid code.
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
                err = getattr(exec_result, "stderr",    "") or ""
                ec  = getattr(exec_result, "exit_code", 1)
                cmd = getattr(exec_result, "command",   "") or ""
                # Include stderr so argparse / runtime error messages reach the coder.
                # Priority: traceback > stderr > stdout (most diagnostic first).
                if tb:
                    detail = f"traceback:\n{tb}"
                elif err:
                    detail = f"stderr:\n{err[:400]}"
                else:
                    detail = f"stdout:\n{out[:400]}"
                fb  = (
                    f"attempt {attempt}: exec failed (exit {ec})"
                    + (f"  cmd={cmd!r}" if cmd else "")
                    + f"\n{detail}"
                )
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, False, False, fb))
                continue

            # ── 3. Validator (subjective half of Gate 2) ─────────────────────
            try:
                approved, vfb = self.validator.approve(task, exec_result, coder_result,
                                                       base_dir=base_dir_path)
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
                val_missing = list(getattr(self.validator, "last_missing_context", []) or [])
                if val_missing:
                    newly = self._broker.resolve(val_missing, target_files, base_dir_path)
                    resolved_context.update(newly)
                    prefetched_context = self._broker.format_for_prompt(resolved_context)
                    logger.info(
                        "InnerLoop: attempt %d validator requested context %s — accumulated (%d total)",
                        attempt, val_missing, len(resolved_context),
                    )
                continue

            # ── APPROVED ──────────────────────────────────────────────────────
            # AUTO-CR-7: before committing a creative chapter, run the periodic
            # canon/fact gate. A real contradiction with earlier chapters turns
            # this approval back into a rejection-with-feedback — but only up to
            # ``max_canon_revisions`` times, after which we accept-with-warning
            # so the gate can never ping-pong the loop.
            if (
                self.task_mode == "creative"
                and self.canon_validator is not None
                and target_files
            ):
                chapter_file = target_files[0]
                cap = getattr(self.canon_validator, "max_canon_revisions", 1)
                if (
                    _canon_revisions < cap
                    and self.canon_validator.should_check(chapter_file)
                ):
                    try:
                        chapter_text = (base_dir_path / chapter_file).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        canon_res = self.canon_validator.check(
                            chapter_text, chapter_file, base_dir=base_dir_path
                        )
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning("InnerLoop: canon check raised — %s; approving.", exc)
                        canon_res = None

                    if canon_res is not None and canon_res.has_conflict:
                        _canon_revisions += 1
                        cfb = canon_res.feedback()
                        logger.info(
                            "InnerLoop: attempt %d canon REJECT (%d/%d) — %s",
                            attempt, _canon_revisions, cap,
                            cfb.replace("\n", " ")[:120],
                        )
                        feedback.append(f"attempt {attempt}: canon rejected\n{cfb}")
                        records.append(AttemptRecord(attempt, True, True, False, cfb))
                        continue
                elif _canon_revisions >= cap and self.canon_validator.should_check(chapter_file):
                    logger.warning(
                        "InnerLoop: canon revision cap (%d) reached for %s — "
                        "accepting chapter with possible unresolved canon issues.",
                        cap, chapter_file,
                    )

            logger.info("InnerLoop: attempt %d APPROVED", attempt)
            records.append(AttemptRecord(attempt, True, True, True, ""))
            return InnerLoopResult(
                task_id=task_id,
                passed=True,
                attempts_used=attempt,
                last_feedback="",
                records=records,
                context_satisfied=True,
            )

        # All attempts exhausted
        last = feedback[-1] if feedback else ""
        return InnerLoopResult(
            task_id=task_id,
            passed=False,
            attempts_used=self.max_attempts,
            last_feedback=last,
            records=records,
            context_satisfied=not _any_missing,
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
    task_mode: str = "code",
) -> InnerLoop:
    """Construct an :class:`InnerLoop` with real agents from *config*.

    Any agent may be injected (useful for tests); omitted agents are
    constructed from the config's API / model settings.

    AUTO-DM-1: ``task_mode`` is accepted and stored for forwarding to
    ``LLMGate2Validator`` (DM-5 will use it to select domain-appropriate
    system prompts).  Defaults to ``"code"`` — no behavioural change for
    existing call sites.
    """
    # AUTO-CR-16: creative editing/review benefits from more coder→review→
    # revise cycles than code. Prefer a creative-specific cap when set.
    from tools.auto.utils import _cfg_mode
    max_attempts = int(_cfg_mode(
        config, "auto", "max_attempts_per_task", task_mode,
        fallback=str(_DEFAULT_MAX_ATTEMPTS),
    ))

    # ── API settings ─────────────────────────────────────────────────────────
    active_profile = config.get("api", "active", fallback="local")
    api_section    = f"api_{active_profile}"

    base_url   = config.get(api_section, "base_url",   fallback="http://localhost:1337/v1")
    api_key    = config.get(api_section, "api_key",    fallback="jan")
    model      = config.get(api_section, "model",      fallback="qwen2.5-14b-instruct")
    api_format = config.get(api_section, "api_format", fallback="openai")
    num_ctx    = config.getint(api_section, "num_ctx",  fallback=0)

    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

    import ssl
    ssl_context: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode    = ssl.CERT_NONE

    max_hints    = config.getint("validator_agent", "max_hints",        fallback=3)
    val_temp     = config.getfloat("validator_agent", "temperature",    fallback=0.1)
    val_timeout  = config.getint("loop",              "timeout_seconds", fallback=300)
    exec_timeout = config.getint("auto",              "exec_timeout_sec", fallback=120)

    # ── Coder ─────────────────────────────────────────────────────────────────
    if coder is None:
        try:
            from tools.auto.coder import make_coder  # type: ignore
            coder = make_coder(config, task_mode=task_mode)
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
            timeout=val_timeout,
            max_hints=max_hints,
            ssl_context=ssl_context,
            base_dir=str(base_dir),
            num_ctx=num_ctx,
            max_tokens=config.getint("validator_agent", "max_tokens", fallback=512),
            task_mode=task_mode,
            config=config,  # AUTO-DM-5: for system prompt override lookup
        )

    # ── ContextBroker ─────────────────────────────────────────────────────────
    broker = ContextBroker(
        max_symbols=config.getint("context_broker", "max_symbols", fallback=20),
    )

    # ── AUTO-CR-7: periodic canon/fact gate (creative mode only) ──────────────
    canon_validator = None
    if task_mode == "creative":
        try:
            from tools.auto.canon_validator import make_canon_validator
            canon_validator = make_canon_validator(
                config, base_dir, task_mode=task_mode, broker=broker,
            )
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning("make_inner_loop: canon validator unavailable — %s", exc)
            canon_validator = None

    return InnerLoop(coder, executor, validator, max_attempts=max_attempts,
                     context_broker=broker, canon_validator=canon_validator,
                     task_mode=task_mode)


# ── Stubs for environments without real agents (unit tests) ──────────────────

class _StubCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        from types import SimpleNamespace
        return SimpleNamespace(succeeded=False, files_written=[], error="stub coder — no real coder available")


class _StubExecutor:
    def run(self, task):
        from types import SimpleNamespace
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="", traceback="stub executor", timed_out=False)
