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
import time
from tools.auto.context_broker import ContextBroker
from tools.agent_trace import tracer   # AUTO-CR-27: per-stage decision tracing

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
    "A change may deliberately DELETE a file (it appears as '(file DELETED by "
    "this change …)'); a deleted file is a valid part of a refactor, not an "
    "error — judge whether the deletion matches the task.\n"
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
    "You are a creative writing editor validating a chapter against its task. "
    "Check ALL of these and report every problem you find:\n"
    "  (a) TASK FULFILMENT — does the chapter actually do what the task asked "
    "(the requested action, scene, mood, or dialogue is present and on-topic)? "
    "Flag dialogue or passages that do not match the task.\n"
    "  (b) COHERENCE & COMPLETENESS as prose.\n"
    "  (c) REPETITION — scenes, lines, or descriptions already present earlier "
    "or in another chapter.\n"
    "  (d) CONTRADICTIONS / continuity errors (facts, names, gender, timeline).\n"
    "  (e) MISATTRIBUTION -- if the task assigns a specific problem, trait, or "
    "storyline to a NAMED character, check that the chapter keeps it with "
    "that same character. Flag it if another character is given that "
    "problem/storyline instead (e.g. the task says character A has a work "
    "problem and character B has a relationship problem, but the chapter "
    "has them talk about each other's problem as if it were their own).\n"
    "The FIRST token of your reply MUST be APPROVED or REVISE, written in "
    "English exactly (never translated). "
    "If everything is fine, reply with exactly: APPROVED\n"
    "If anything is wrong, reply with 'REVISE:' followed by a NUMBERED LIST of "
    "EVERY problem — do NOT stop at the first one. For each item give: the exact "
    "phrase or passage at fault (quote it), what is wrong, and the concrete fix "
    "to apply. Write the problem list in the chapter's language. "
    "Be specific and actionable, never vague. Do NOT return JSON, no preamble.\n"
    "VERY IMPORTANT — how to phrase a fix: for PROSE, DIALOGUE, or STYLE "
    "problems, describe the DESIRED OUTCOME in your own words (e.g. 'rewrite this "
    "narrated summary as a short direct-speech exchange between the characters "
    "involved'). Do NOT write the exact replacement sentence yourself — the "
    "author copies such sentences verbatim, and your terse phrasing then becomes "
    "flat 'telling' prose, which loops. ONLY for a factual value (a name, gender, "
    "age, date, place) may you give the exact corrected value.\n"
    "Example of the FORMAT (generic — describe the real problems in the chapter, "
    "do not reuse this wording):\n"
    "REVISE:\n"
    "1. Реплика персонажа в абзаце 2 не отвечает заданию — оживить как короткий "
    "диалог по теме задания (своими словами).\n"
    "2. Описание сцены дословно повторяет другую главу — сократить до одной "
    "фразы или убрать.\n"
    "3. Персонаж назван «он», хотя по фактам это женщина — привести местоимения "
    "и глаголы к женскому роду («она/стояла»)."
)

# Backward-compatibility alias
_GATE2_SYSTEM = _GATE2_SYSTEM_CODE

_GATE2_SYSTEMS: dict[str, str] = {
    "code":     _GATE2_SYSTEM_CODE,
    "docs":     _GATE2_SYSTEM_DOCS,
    "creative": _GATE2_SYSTEM_CREATIVE,
}


def _resolve_validator_system(config, task_mode: str) -> str:
    """Resolve the Gate-2 validator system prompt for *task_mode*.

    Priority:
      1. Mode-specific override ``[validator_agent] system_{task_mode}``
         (e.g. ``system_creative``, ``system_docs``) — wins whenever it is
         explicitly set, for any mode.
      2. The legacy bare ``[validator_agent] system`` key — but **only when
         ``task_mode == "code"``**.
      3. The built-in constant for *task_mode* (``_GATE2_SYSTEMS``), falling
         back to ``_GATE2_SYSTEM_CODE`` for an unrecognised mode.

    AUTO-CR-19-1: previously the bare ``system`` key was consulted as the
    fallback for *every* mode. ``agents.ini`` / ``agents_32k.ini`` ship a
    code-specific legacy ``system`` prompt ("You are a code completeness
    validator…"), so a creative or docs run with no matching ``system_creative``
    / ``system_docs`` override silently inherited that code prompt — the
    model was asked to judge prose against code-completeness criteria
    ("is the function body complete", "reply in JSON"), which the soft
    parser then either fail-opened to APPROVED or treated as noisy feedback.
    The bare key now only applies in code mode; every other mode falls
    through to its built-in (or an explicit mode-specific override).
    """
    builtin = _GATE2_SYSTEMS.get(task_mode, _GATE2_SYSTEM_CODE)
    if config is None:
        return builtin
    mode_key = f"system_{task_mode}" if task_mode != "code" else None
    if mode_key and config.has_option("validator_agent", mode_key):
        return config.get("validator_agent", mode_key).strip()
    if task_mode == "code":
        return config.get("validator_agent", "system", fallback=builtin).strip()
    return builtin


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
        self._config     = config
        # AUTO-FIX (fable follow-up): Gate-2 in code/docs mode requires
        # strict JSON with no soft-parse fallback (see AUTO-BUG-10) — a
        # thinking model truncated mid-<think> here fails closed exactly
        # like architect/coder did before the think=false fix.
        self._think      = config.getboolean("validator_agent", "think", fallback=False) if config is not None else False
        # AUTO-DM-5 / AUTO-CR-19-1: select system prompt — mode-specific
        # override > (code-mode-only) legacy "system" key > built-in.
        # See _resolve_validator_system for the full priority rationale.
        self._system = _resolve_validator_system(config, self.task_mode)

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
                # Final-polish: a file absent WITH a .coder.bak sibling is a
                # deliberate deletion (coder's delete:true), not a read
                # failure — say so, or the validator may treat the feature
                # as an error.
                if not (_base / rel).exists() and (
                    _base / (rel + ".coder.bak")
                ).exists():
                    blocks.append(
                        f"--- {rel} ---\n(file DELETED by this change — "
                        f"original backed up at {rel}.coder.bak)"
                    )
                else:
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

    def _creative_language_mismatch(self, coder_result, base_dir) -> "str | None":
        """Deterministic (non-LLM) language pre-gate for creative mode.

        Detects the story's established language from ``synopsis.md`` /
        ``story_bible.md`` (or ``[coder] creative_language`` in config) and
        compares it against the dominant script of the chapter the coder
        just wrote. On a clear mismatch (e.g. story is Russian, the new
        chapter drifted into English — a known failure mode for small
        models mid-chapter), returns a REVISE reason string so the caller
        can reject WITHOUT spending an LLM call on it — cheaper and instant
        compared to catching the same problem via the Gate-2 LLM review.

        Fail-open: returns ``None`` (no mismatch — proceed to the real
        Gate-2 LLM check as normal) whenever there's nothing to compare
        against, or detection itself fails for any reason. This is a fast
        path, not a substitute for the LLM review, so it never blocks a
        chapter it isn't confident about.
        """
        if self.task_mode != "creative":
            return None
        try:
            from tools.auto.utils import resolve_creative_language, detect_language

            _base = Path(base_dir) if base_dir is not None else self.base_dir

            sample = ""
            for fname in ("synopsis.md", "story_bible.md"):
                p = _base / fname
                if p.exists():
                    text = p.read_text(encoding="utf-8", errors="replace")
                    if text.strip():
                        sample = text
                        break

            # AUTO-FIX (podrugi run): when the run starts from user-provided
            # chapters (the documented Creative.MD workflow: hand-written
            # chapter_1.txt + goal "продолжи рассказ"), synopsis.md /
            # story_bible.md do not exist yet — they are only written AFTER
            # the first generated chapter is accepted. That left this gate
            # blind exactly on the first generated chapter: an all-English
            # chapter_2 sailed through and then poisoned the synopsis.
            # Fallback: establish the language from the chapters already on
            # disk (excluding the files the coder just wrote and reserved
            # meta files).
            if not sample:
                _written = set(getattr(coder_result, "files_written", []) or [])
                _reserved = {"synopsis.md", "story_bible.md", "IMPROVEMENTS.md",
                             "plan.json", "progress.json"}
                _parts: list[str] = []
                for prev in sorted(_base.glob("*.txt")) + sorted(_base.glob("*.md")):
                    if prev.name in _reserved or prev.name in _written:
                        continue
                    try:
                        t = prev.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        continue
                    if t:
                        _parts.append(t[:2000])
                    if len(_parts) >= 3:
                        break
                sample = "\n".join(_parts)

            expected = resolve_creative_language(self._config, sample, task_mode="creative")
            if not expected:
                return None  # nothing established yet (e.g. chapter 1) — skip

            files = list(getattr(coder_result, "files_written", []) or [])
            if not files:
                return None
            chapter_text = "\n".join(
                (_base / rel).read_text(encoding="utf-8", errors="replace")
                for rel in files
                if (_base / rel).exists()
            )
            actual = detect_language(chapter_text)
            if actual and actual != expected:
                return (
                    f"The chapter appears to be written in {actual}, but the "
                    f"story so far is in {expected}. Rewrite the ENTIRE "
                    f"chapter in {expected} — do not mix languages."
                )
        except Exception as exc:  # noqa: BLE001 — fast path must never raise
            logger.debug("Gate2 language pre-gate skipped: %s", exc)
        return None

    def approve(
        self,
        task:         dict,
        exec_result,
        coder_result,
        *,
        base_dir=None,
        prior_critique: str = "",
    ) -> tuple[bool, str]:
        """Return (approved, feedback_string).  Never raises — fail-closed.

        Side channel: ``self.last_missing_context`` is set to any symbol names
        the validator reported as needed (pull-model); InnerLoop reads it.

        AUTO-CR-30: ``prior_critique`` is the validator's OWN feedback from the
        previous attempt (empty on the first). When present, it is shown to the
        model so it can check, item by item, which of its earlier points the new
        draft actually addressed, re-read the task, and write a fresh, more
        detailed verdict instead of repeating itself.
        """
        self.last_missing_context = []

        # AUTO-FIX: cheap deterministic language pre-gate for creative mode —
        # catches a chapter that drifted into the wrong language WITHOUT
        # spending an LLM call on it. Only ever short-circuits to REVISE; it
        # never approves on its own, so a false trigger still gets caught by
        # the real LLM review below on the next attempt.
        _lang_reason = self._creative_language_mismatch(coder_result, base_dir)
        if _lang_reason:
            logger.info(
                "LLMGate2Validator [creative]: language pre-gate REVISE "
                "(no LLM call) — %s", _lang_reason,
            )
            return False, f"Reason: {_lang_reason}"

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
                + "Generated files (CHANGED FILE CONTENT after the coder's edit):\n"
                + self._read_changed_content(coder_result, task=task, base_dir=base_dir)
            )

            # AUTO-CR-30: on a re-review, show the model its OWN previous critique
            # so it verifies what was actually fixed rather than repeating itself.
            if prior_critique and prior_critique.strip():
                user_msg += (
                    "\n\n--- YOUR PREVIOUS REVIEW (of the earlier draft) ---\n"
                    + prior_critique.strip()
                    + "\n\nThe chapter above is the author's NEW revision after that "
                    "review. Go through your previous points ONE BY ONE: state for "
                    "each whether the new draft fixed it or not. Re-read the Task. "
                    "Then give a FRESH verdict: APPROVED only if every point is "
                    "resolved and the task is met; otherwise REVISE with an UPDATED, "
                    "MORE DETAILED numbered list — keep the still-unfixed points "
                    "(say what is still wrong) and add any new problems you now see."
                )

            def _call_validator(um: str) -> str:
                if self.api_format == "ollama":
                    _val_opts: dict = {"temperature": self.temperature, "num_predict": self.max_tokens}
                    if self.num_ctx:
                        _val_opts["num_ctx"] = self.num_ctx
                    _payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system",  "content": self._system},
                            {"role": "user",    "content": um},
                        ],
                        "options": _val_opts,
                        "think": getattr(self, "_think", False),
                    }
                else:
                    _payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system",  "content": self._system},
                            {"role": "user",    "content": um},
                        ],
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                    }
                _r = request_completion(
                    url=url,
                    headers=headers,
                    payload=_payload,
                    timeout=self.timeout,
                    api_format=self.api_format,
                    ssl_context=self.ssl_context,
                )
                return strip_think(_r or "")

            raw = _call_validator(user_msg)
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
                # AUTO-CR-31: if the model buried the verdict (e.g. it started
                # with "Let's go through each point:" instead of APPROVED/REVISE),
                # re-ask ONCE with a hard nudge before falling open. Capped at a
                # single retry — no loop, max two validator calls total.
                if unparseable:
                    logger.info(
                        "LLMGate2Validator [creative]: verdict unparseable — "
                        "re-asking once. raw=%r", raw[:120]
                    )
                    _nudge = (
                        "\n\nIMPORTANT: your previous reply did not begin with a "
                        "verdict. Reply AGAIN and make the VERY FIRST token exactly "
                        "APPROVED or REVISE (in English, uppercase). If REVISE, "
                        "follow it with ':' and the numbered list of problems."
                    )
                    raw2 = _call_validator(user_msg + _nudge)
                    if raw2 and raw2.strip():
                        a2, r2, u2 = _parse_verdict_soft(raw2)
                        if not u2:        # second answer was clear — use it
                            raw, approved, reason, unparseable = raw2, a2, r2, u2
                if unparseable:
                    logger.warning(
                        "LLMGate2Validator [creative]: verdict still unparseable "
                        "after one retry — passing on fail-open. raw=%r", raw[:120]
                    )
                if approved:
                    return True, ""
                # AUTO-CR-29: the prompt now asks for a NUMBERED LIST of every
                # problem, but _parse_verdict_soft is line-oriented and would
                # lose a multi-line critique. Feed the coder the FULL reviewer
                # reply minus the leading verdict token instead.
                critique = raw.strip()
                _low = critique.lower()
                for _tok in ("revise:", "revise", "reject:", "reject", "no:"):
                    if _low.startswith(_tok):
                        critique = critique[len(_tok):].lstrip(" :\n\t")
                        break
                if not critique:
                    critique = reason  # fall back to the parsed reason
                return False, f"Reason: {critique}"

            # ── Code / docs mode: strict JSON path (unchanged) ───────────────
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            # ── Guard: empty AFTER fence-stripping ────────────────────────
            # The pre-strip empty check above only catches a fully-empty raw
            # reply. A reply like "```json\n```" (fences with nothing inside)
            # passes that check but becomes "" once the fences are stripped,
            # so json.loads("") would raise the same cryptic
            # "Expecting value: line 1 column 1 (char 0)" error. Surface a
            # clear message instead.
            if not raw or not raw.strip():
                raise ValueError(
                    "validator model returned empty JSON content "
                    "(empty code fences or no body after stripping markdown)"
                )

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
    """Parse a line-oriented Gate-2 verdict for creative mode (AUTO-CR-2 / CR-26-1).

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

    Acceptance criteria (spec AUTO-CR-2 + AUTO-CR-26-1):
        * ``APPROVED`` / ``approved`` / ``OK …``                  → approved=True
        * ``REVISE: <reason>`` / ``REJECT: …`` / ``NO: …``        → approved=False
        * Russian APPROVED forms (нет противоречий, согласна, …)  → approved=True
        * Russian REVISE forms (не согласна, противоречие, …)     → approved=False
        * Any other content (rambling, JSON, prose, …)            → approved=True (fail-open)

    Order is load-bearing: negated-positive RU rules (нет противоречий, не против,
    не противоречит) and the «не соглас» rule are tested **before** the bare
    negative patterns (против, противоречи…, соглас) to avoid mis-classification.
    """
    import re as _re

    def _normalise(s: str) -> str:
        """Lowercase, collapse whitespace, strip surrounding punctuation."""
        s = s.lower().strip()
        s = _re.sub(r"[\s\u00a0]+", " ", s)   # collapse all whitespace
        s = s.strip(".,!?;:—–-")
        return s

    def _reason_from(raw: str, norm: str) -> str:
        """Extract human-readable reason: text after first ':' or the whole norm."""
        if ":" in raw:
            after = raw.split(":", 1)[1].strip()
            if after:
                return after
        return norm if norm else "validator rejected (no reason given)"

    # ── APPROVED patterns (Russian) — must be checked BEFORE bare negatives ──
    _RU_APPROVED = [
        # «нет противоречий» / «противоречий нет» / «не противоречит»
        _re.compile(r"\b(нет|без)\s+противоречий\b"),
        _re.compile(r"\bпротиворечий\s+нет\b"),
        _re.compile(r"\bне\s+противоречит\b"),
        # negated / absent contradictions expressed without «нет/без»:
        #   «противоречий не обнаружено / не выявлено», «противоречия отсутствуют»
        # AUTO-BUG: real LLM replies routinely insert filler words between the
        # noun and the negated verb — «противоречий В ТЕКСТЕ не обнаружено»,
        # «противоречий С ТЕКСТОМ не обнаружено» — which the old
        # immediately-adjacent ``\s+`` pattern missed, falling through to the
        # bare «противоречи» REVISE pattern below and flipping a genuine
        # APPROVED into a false REVISE. Allow up to 3 filler words (bounded so
        # it can't jump across an unrelated sentence) in between.
        #
        # BUGFIX: the filler-gap fix above first shipped as `не\s+\w+` — ANY
        # "не + word" — which also matched the critic negating their OWN
        # reaction/expectation rather than the existence of a contradiction:
        # «противоречий мне не понравилось», «противоречий я здесь не
        # одобрю», «противоречий не ожидал найти» all wrongly APPROVED.
        # Restrict the negated verb to ones that actually mean "not found /
        # not detected" instead of accepting any verb.
        _re.compile(
            r"противоречи\w*(?:\s+\S+){0,3}\s+"
            r"(?:отсутств\w*|не\s+(?:обнаружен\w*|выявлен\w*|найден\w*|"
            r"встречает\w*|замечен\w*|видн\w*))"
        ),
        #   «не вижу / не обнаружил / не нашёл … противоречий» — same filler-word
        #   gap allowed on this side (e.g. «не смог найти каких-либо противоречий»).
        # BUGFIX: the original `\bне(?:\s+\S+){0,3}\s+противоречи` allowed ANY
        # gap after «не», so «не ожидал встретить противоречия» / «не
        # нравятся противоречия» — negating the critic's expectation/liking,
        # not the presence of a contradiction — wrongly APPROVED too. Require
        # «не» to be followed by a search/perception verb (вижу, нахожу,
        # обнаружил, ...) before the filler-word gap kicks in.
        _re.compile(
            r"\bне\s+(?:вижу|нахожу|нашёл|нашел|обнаружил|выявил|заметил|"
            r"встретил|встречаю|смог\s+найти)"
            r"(?:\s+\S+){0,3}\s+противоречи"
        ),
        # «не против» / «непротив»
        _re.compile(r"\bне\s+против\b"),
        _re.compile(r"\bнепротив\b"),
        # «одобрен* / одобряю» — NOT preceded by «не »
        _re.compile(r"(?<!не )одобр"),
        # «принято / принимаю / можно принять» — NOT preceded by «не »
        _re.compile(r"(?<!не )принят"),
        _re.compile(r"\bможно\s+принять\b"),
        # «всё верно» / «все верно»
        _re.compile(r"\b(всё|все)\s+верно\b"),
        # «соответствует» — NOT preceded by «не »
        _re.compile(r"(?<!не )соответствует\b"),
        # «соглас*» (согласен/согласна/…) — NOT preceded by «не»/«нет»
        _re.compile(r"(?<!не)(?<!нет)(?<!не )(?<!нет )соглас"),
    ]

    # ── REVISE patterns (Russian) — negated forms first, then bare ──
    _RU_REVISE = [
        # «не соглас*» / «несоглас*»
        _re.compile(r"\b(не|нет)\s+соглас"),
        _re.compile(r"\bнесоглас"),
        # «не соответствует»
        _re.compile(r"\bне\s+соответствует\b"),
        # bare «противоречи*» (un-negated — the нет/не rules above returned already)
        _re.compile(r"\bпротиворечи"),
        # bare «против» (un-negated — «не против» / «непротив» returned above)
        _re.compile(r"\bпротив\b"),
        # error / correction vocabulary
        _re.compile(r"\bисправ"),
        _re.compile(r"\bошибк"),
        _re.compile(r"\bневерн"),
        # «доработать / переписать / переделать»
        _re.compile(r"\bдоработ"),
        _re.compile(r"\bперепис"),
        _re.compile(r"\bпередел"),
        # «нужно / надо / следует изменить|поправить|переписать|исправить|доработать»
        _re.compile(r"\b(нужно|надо|следует)\s+(изменить|поправить|переписать|исправить|доработать|переделать)"),
        # «(так) нельзя оставлять»
        _re.compile(r"нельзя\s+оставля"),
        # standalone negative answer «Нет, …» / «Нет.» — comma/period only,
        # so approvals like «нет проблем» / «нет замечаний» (followed by a
        # space) are NOT caught here. «нет противоречий» already returned
        # APPROVED above.
        # AUTO-BUG: this used to fire unconditionally on ANY "Нет, ..."
        # opener, which also caught casual-Russian approvals where "Нет" is
        # a discourse filler rather than a substantive rejection — "Нет,
        # всё хорошо." / "Нет, всё нормально." / "Нет, всё в порядке."
        # ("nah, it's all fine") were confidently misclassified as REVISE
        # even though nothing else in the reply objects to anything. A
        # negative lookahead excludes exactly that "Нет, <all-good phrase>"
        # shape while leaving every other "Нет, ..." opener — including the
        # test-locked "Нет, так нельзя оставлять" / "Нет, это не подходит"
        # — classified as REVISE exactly as before.
        _re.compile(
            r"^нет(?:[,.]|$)"
            r"(?!\s*(?:всё|все)\s+(?:хорошо|нормально|в\s+порядке|ок(?:ей)?|отлично|супер)\b)"
        ),
    ]

    # ── Scan: first try the first non-empty line; fall back to whole text ──
    candidates: list[str] = []
    first_line: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s:
            if first_line is None:
                first_line = s
                candidates.append(s)   # first non-empty line tried first
            # collect whole text as second candidate
    if text.strip():
        candidates.append(text.strip())   # whole text as fallback

    for raw in candidates:
        norm = _normalise(raw)
        upper = raw.strip().upper()

        # ── English APPROVED ──────────────────────────────────────────────
        if upper.startswith("APPROVED") or _re.match(r"^OK\b", upper):
            return True, "", False

        # ── English REVISE / REJECT / NO ─────────────────────────────────
        for token in ("REVISE", "REJECT", "NO"):
            if upper.startswith(token):
                rest = raw.strip()[len(token):].lstrip(": ").strip()
                reason = rest if rest else "validator rejected (no reason given)"
                return False, reason, False

        # ── English verdict on a LATER line (behind a preamble) ──────────
        # AUTO-BUG: the two English checks above only fire when the token is
        # the FIRST token of the whole candidate string. The candidate list
        # is [first_non_empty_line, whole_text] precisely so a verdict that
        # isn't on line 1 can still be found via the whole-text fallback —
        # but that fallback only ever helped RUSSIAN verdicts, because the
        # Russian rules below use substring .search() over the whole text
        # while the English rules use .startswith(). Result: a validator that
        # emits a perfectly clear English "REVISE:/REJECT:/NO:" but prepends
        # ANY preamble line ("Here is my verdict:\nREVISE: fix the ending")
        # had its rejection silently dropped and the chapter fail-opened to
        # APPROVED — while the identical reply in Russian was correctly
        # REVISE. The protocol is "first token of a LINE", so scan each line
        # rather than only the candidate's own start. Kept AFTER the two
        # startswith checks and BEFORE the Russian heuristics: every input
        # that already returned a definite verdict is byte-for-byte
        # unchanged, and an explicit English protocol token outranks the
        # fuzzy Russian keyword match (a bare «одобряю» elsewhere in the text
        # must not override an explicit "REVISE:" line).
        for _line in raw.splitlines():
            _lu = _line.strip().upper()
            if not _lu:
                continue
            if _lu.startswith("APPROVED") or _re.match(r"^OK\b", _lu):
                return True, "", False
            for token in ("REVISE", "REJECT", "NO"):
                if _lu.startswith(token):
                    rest = _line.strip()[len(token):].lstrip(": ").strip()
                    reason = rest if rest else "validator rejected (no reason given)"
                    return False, reason, False

        # ── Russian APPROVED (order matters — negated first) ──────────────
        for pat in _RU_APPROVED:
            if pat.search(norm):
                return True, "", False

        # ── Russian REVISE (order matters — negated first) ────────────────
        for pat in _RU_REVISE:
            if pat.search(norm):
                reason = _reason_from(raw.strip(), norm)
                return False, reason, False

        # Only the *first* non-empty line is tried for the line-first pass.
        # Break after processing the first candidate so that the second
        # candidate (whole text) is only reached when the first line had no match.
        if raw == first_line:
            # did not match on first line — try whole text next iteration
            continue
        # whole-text pass also found nothing
        break

    # Empty response or no matching token found anywhere
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

def _trace_stage(task_id: str, attempt: int, stage: str, content: str, **extra) -> None:
    """AUTO-CR-27: emit one stage-decision trace event. Never raises — tracing
    must never affect pipeline behaviour even if the tracer is misconfigured."""
    try:
        params = {"task": task_id, "attempt": attempt, "stage": stage}
        params.update(extra)
        tracer.event(source=stage, target="inner_loop", kind="decision",
                     content=content, params=params)
    except Exception:  # noqa: BLE001 — tracing is best-effort
        pass


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
        fact_validator=None,
        prosody_validator=None,
        continuity_validator=None,
        theme_validator=None,
        require_tests: bool = False,
        task_mode: str = "code",
        max_task_seconds: int = 0,
        run_goal: str = "",
    ):
        self.coder        = coder
        self.executor     = executor
        self.validator    = validator
        self.max_attempts = max(1, int(max_attempts))
        self._broker      = context_broker or ContextBroker()
        # AUTO-CR-7: optional periodic canon/fact gate (creative mode only).
        self.canon_validator = canon_validator
        # AUTO-CR-20: optional per-task fact-compliance gate (creative mode only).
        self.fact_validator  = fact_validator
        # AUTO-CR-21: optional Russian rhythm/rhyme gate (creative mode only).
        self.prosody_validator = prosody_validator
        # AUTO-CR-23-3: optional continuity gate vs bible + previous chapter
        # (creative mode only).
        self.continuity_validator = continuity_validator
        # podrugi-3: optional theme/content gate vs story-level guidelines
        # (creative mode only) — the one gate that judges WHAT the chapter
        # says, not whether it is consistent.
        self.theme_validator = theme_validator
        # selfhost pilot: deterministic tests-mandate gate for code mode.
        self.require_tests = bool(require_tests)
        self.task_mode    = str(task_mode)
        # AUTO-CR-21-4: hard wall-clock cap per task; 0 disables the guard.
        self.max_task_seconds = max(0, int(max_task_seconds))
        # AUTO-CR-22-1: run-level goal, propagated into per-task gate checks
        # whenever a task's own "goal" key is absent — the architect emits
        # only title/instruction per task, so the gates (fact/prosody) would
        # otherwise only see the keyword if it happened to be echoed into the
        # per-task instruction text.
        self._run_goal    = str(run_goal or "")

    # ------------------------------------------------------------------

    def _task_with_goal(self, task: dict) -> dict:
        """Shallow copy of *task* with the run goal injected when absent.

        AUTO-CR-22-1: the architect only emits ``title``/``instruction`` per
        task — ``task["goal"]`` is never populated in production. The fact
        and prosody gates key their keyword/fact detection off
        ``task.get("goal", "")``, so without this they only activate when
        the architect happens to echo the keyword/fact into the per-task
        instruction. This builds a copy (never mutates the stored task dict)
        carrying the run-level goal whenever the task doesn't already have
        its own.
        """
        if task.get("goal"):
            return task
        return {**task, "goal": self._run_goal}

    # ------------------------------------------------------------------

    def run_task(
        self,
        task:             dict,
        base_dir:         str | Path,
        *,
        prior_feedback:   list[str] | None = None,
        prior_implementations: list[dict] | None = None,   # LOOP-4
        deadline:         float | None = None,              # AUTO-CR-33
    ) -> InnerLoopResult:
        """Run up to ``max_attempts`` Gate-2 cycles for *task*.

        ``deadline`` (monotonic seconds) is a TASK-WIDE wall-clock budget shared
        across all outer-loop rounds. When omitted, the limit is computed per
        call from ``max_task_seconds`` (legacy/standalone behaviour). This is the
        AUTO-CR-33 fix: previously ``_start_time`` reset every round, so the real
        cap was ``max_rounds × max_task_seconds`` (e.g. 10 × 30 min ≈ 5 h).

        Returns:
            :class:`InnerLoopResult` with ``passed`` flag, attempt count,
            last feedback string, and per-attempt records.
        """
        task_id = task.get("id", "")
        feedback: list[str] = list(prior_feedback or [])
        _prior_validator_critique: str = ""   # AUTO-CR-30: last Gate-2 critique
        # AUTO-CR-30: detect (once) whether this validator's approve() accepts
        # prior_critique, so we never break fakes/older validators that don't.
        try:
            import inspect as _inspect
            _validator_accepts_prior = (
                "prior_critique"
                in _inspect.signature(self.validator.approve).parameters
            )
        except (ValueError, TypeError):
            _validator_accepts_prior = False
        records:  list[AttemptRecord] = []
        # Pull-model state (carried across attempts within this round)
        prefetched_context: str = ""
        resolved_context: dict[str, str] = {}   # accumulates every symbol the validator has asked for
        _any_missing: bool = False   # Task 4: True if any attempt had unsatisfied context
        _canon_revisions: int = 0     # AUTO-CR-7: canon-driven rejections used so far
        _fact_revisions:  int = 0     # AUTO-CR-20: Gate-3 fact-driven rejections used so far
        _prosody_revisions: int = 0   # AUTO-CR-21: prosody-gate-driven rejections used so far
        _continuity_revisions: int = 0  # AUTO-CR-23-3: continuity-gate-driven rejections used so far
        _theme_revisions: int = 0     # podrugi-3: theme-gate-driven rejections used so far
        _tests_mandate_rejections: int = 0  # selfhost: code-without-tests rejections
        base_dir_path = Path(base_dir)
        target_files  = task.get("target_files", []) or []
        self._broker.reset_cache()  # clear per-task cache; Pass-2 hits re-accumulate fresh
        _start_time = time.monotonic()  # AUTO-CR-21-4: wall-clock guard reference point
        # AUTO-CR-33: prefer a task-wide deadline shared across rounds; fall back
        # to a per-call budget when called standalone (no deadline passed).
        if deadline is not None:
            _eff_deadline = deadline
        elif self.max_task_seconds > 0:
            _eff_deadline = _start_time + self.max_task_seconds
        else:
            _eff_deadline = None

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

            # ── AUTO-CR-21-4 / CR-33: hard wall-clock guard (task-wide) ──────
            # Independent safety valve for pathological tasks (e.g. the CR-21
            # rhythm/rhyme runaway), with the deadline shared across outer-loop
            # rounds (CR-33) so the cap is the real per-task budget, not budget
            # × rounds. None disables the guard.
            if _eff_deadline is not None:
                if time.monotonic() >= _eff_deadline:
                    logger.warning(
                        "InnerLoop: task wall-clock limit (%ds = %.1f min, "
                        "from max_task_seconds) reached — stopping after %d attempts",
                        self.max_task_seconds, self.max_task_seconds / 60.0,
                        attempt - 1,
                    )
                    _trace_stage(task_id, attempt - 1, "overall", "EXHAUSTED", reason="wall_clock")
                    last = feedback[-1] if feedback else ""
                    return InnerLoopResult(
                        task_id=task_id,
                        passed=False,
                        attempts_used=attempt - 1,
                        last_feedback=last,
                        records=records,
                        context_satisfied=not _any_missing,
                    )

            # ── 1. Coder ──────────────────────────────────────────────────────
            try:
                coder_result = self.coder.generate(
                    task, base_dir, prior_feedback=feedback,
                    prefetched_context=prefetched_context,
                )
            except Exception as exc:
                logger.error("InnerLoop: coder raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: coder error — {exc}"
                _trace_stage(task_id, attempt, "coder", "ERROR", error=str(exc))
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
                _trace_stage(task_id, attempt, "coder", "REJECTED")
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            # ── 2. Executor (objective half of Gate 2) ────────────────────────
            try:
                # ── selfhost pilot: deterministic tests-mandate gate ─────
                if self.require_tests and self.task_mode == "code":
                    _written = list(getattr(coder_result, "files_written", []) or [])
                    _py = [f for f in _written if f.endswith(".py")]
                    def _is_test(f: str) -> bool:
                        import os as _os
                        base = _os.path.basename(f)
                        return base.startswith("test_") or base.endswith("_test.py") \
                            or "/tests/" in f.replace("\\", "/") or f.replace("\\", "/").startswith("tests/")
                    _code_py = [f for f in _py if not _is_test(f)]
                    _test_py = [f for f in _py if _is_test(f)]
                    if _code_py and not _test_py:
                        _tests_mandate_rejections += 1
                        _cap = 2
                        _msg = ("tests mandate: code files written without any "
                                "test file (" + ", ".join(_code_py[:4]) + ") — every "
                                "code change must include new or updated tests "
                                "(tests/test_*.py) covering it; resubmit code AND tests.")
                        if _tests_mandate_rejections <= _cap:
                            logger.info("InnerLoop: attempt %d rejected by tests "
                                        "mandate (%d/%d)", attempt,
                                        _tests_mandate_rejections, _cap)
                            feedback.append(f"attempt {attempt}: {_msg}")
                            records.append(AttemptRecord(attempt, True, False, False, _msg))
                            _trace_stage(task_id, attempt, "tests_mandate", "REJECTED",
                                        revisions_used=_tests_mandate_rejections, cap=_cap)
                            continue
                        logger.warning("InnerLoop: tests-mandate cap (%d) reached — "
                                       "proceeding to execution without tests.", _cap)
                        _trace_stage(task_id, attempt, "tests_mandate",
                                    "ACCEPTED_AT_CAP", cap=_cap)

                exec_result = self.executor.run(task)
            except Exception as exc:
                logger.error("InnerLoop: executor raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: executor error — {exc}"
                _trace_stage(task_id, attempt, "executor", "ERROR", error=str(exc))
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
                _trace_stage(task_id, attempt, "executor", "REJECTED", exit_code=ec)
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, False, False, fb))
                continue

            # ── 3. Validator (subjective half of Gate 2) ─────────────────────
            try:
                # AUTO-CR-30: only pass prior_critique to validators that accept
                # it (real LLMGate2Validator); fakes/older validators are unaffected.
                _ap_kwargs = {"base_dir": base_dir_path}
                if _validator_accepts_prior:
                    _ap_kwargs["prior_critique"] = _prior_validator_critique
                approved, vfb = self.validator.approve(
                    task, exec_result, coder_result, **_ap_kwargs
                )
            except Exception as exc:
                logger.error("InnerLoop: validator raised on attempt %d: %s", attempt, exc)
                fb = f"attempt {attempt}: validator error — {exc}"
                _trace_stage(task_id, attempt, "gate2", "ERROR", error=str(exc))
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, True, False, fb))
                continue

            if not approved:
                fb = f"attempt {attempt}: validator rejected\n{vfb}"
                logger.info(
                    "InnerLoop: attempt %d rejected — full critique below:\n"
                    "──────── validator critique (attempt %d) ────────\n%s\n"
                    "─────────────────────────────────────────────────",
                    attempt, attempt, vfb,
                )
                _trace_stage(task_id, attempt, "gate2", "REJECTED")
                feedback.append(fb)
                _prior_validator_critique = vfb   # AUTO-CR-30: carry into next review
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
            #
            # Checks EVERY target file, not just target_files[0]: a multi-file
            # creative task (e.g. a cross-chapter consistency fix — see
            # tests/test_cr17_creative_acceptance.py) can touch several
            # chapters in one attempt, and a contradiction introduced in any
            # one of them is just as real as one in the first. Conflicts are
            # aggregated into a single combined feedback so the coder sees
            # every problem at once, and the revision cap is still spent once
            # per REJECTED attempt (not once per file), matching the existing
            # cap semantics.
            if (
                self.task_mode == "creative"
                and self.canon_validator is not None
                and target_files
            ):
                cap = getattr(self.canon_validator, "max_canon_revisions", 1)
                checkable_files = [
                    tf for tf in target_files if self.canon_validator.should_check(tf)
                ]
                if _canon_revisions < cap and checkable_files:
                    conflict_blocks: list[str] = []
                    for chapter_file in checkable_files:
                        try:
                            chapter_text = (base_dir_path / chapter_file).read_text(
                                encoding="utf-8", errors="replace"
                            )
                            canon_res = self.canon_validator.check(
                                chapter_text, chapter_file, base_dir=base_dir_path
                            )
                        except Exception as exc:  # noqa: BLE001 — fail-open
                            logger.warning(
                                "InnerLoop: canon check raised for %s — %s; approving.",
                                chapter_file, exc,
                            )
                            canon_res = None

                        if canon_res is not None and canon_res.has_conflict:
                            conflict_blocks.append(f"{chapter_file}:\n{canon_res.feedback()}")

                    if conflict_blocks:
                        _canon_revisions += 1
                        cfb = "\n\n".join(conflict_blocks)
                        logger.info(
                            "InnerLoop: attempt %d canon REJECT (%d/%d) — %s",
                            attempt, _canon_revisions, cap,
                            cfb.replace("\n", " ")[:120],
                        )
                        feedback.append(f"attempt {attempt}: canon rejected\n{cfb}")
                        records.append(AttemptRecord(attempt, True, True, False, cfb))
                        _trace_stage(task_id, attempt, "canon", "REJECTED",
                                    revisions_used=_canon_revisions, cap=cap)
                        continue
                elif _canon_revisions >= cap and checkable_files:
                    logger.warning(
                        "InnerLoop: canon revision cap (%d) reached for %s — "
                        "accepting chapter with possible unresolved canon issues.",
                        cap, checkable_files,
                    )
                    _trace_stage(task_id, attempt, "canon", "ACCEPTED_AT_CAP", cap=cap)

            # ── AUTO-CR-20: Gate-3 per-task fact-compliance check ─────────────
            # Runs after Gate-2 APPROVED (and the canon gate) in creative mode,
            # checking only whether the generated text contradicts an explicit
            # fact in the task. Bounded by max_fact_revisions; fail-open on
            # any error.
            #
            # Checks EVERY target file (see the canon gate above for why a
            # multi-file creative task needs every file checked, not just
            # target_files[0]) and aggregates any contradictions into one
            # combined feedback.
            if (
                self.task_mode == "creative"
                and self.fact_validator is not None
                and target_files
            ):
                fact_cap = getattr(self.fact_validator, "max_fact_revisions", 1)
                # Always run the check — the cap only gates *rejection*, not the
                # check itself.  On a post-cap attempt the check still runs so a
                # now-passing text is accepted cleanly instead of warned through.
                fact_problem_blocks: list[str] = []
                _fact_task = self._task_with_goal(task)
                for _fact_file in target_files:
                    try:
                        # Read the chapter text from disk (same strategy as canon gate).
                        _fact_text = (base_dir_path / _fact_file).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        fact_verdict = self.fact_validator.check(_fact_task, _fact_text)
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning(
                            "InnerLoop: Gate-3 fact check raised for %s — %s; approving.",
                            _fact_file, exc,
                        )
                        fact_verdict = None

                    if fact_verdict is not None and not fact_verdict.approved:
                        fact_problem_blocks.append(f"{_fact_file}:\n{fact_verdict.feedback()}")

                if fact_problem_blocks:
                    ffb = "\n\n".join(fact_problem_blocks)
                    if _fact_revisions < fact_cap:
                        _fact_revisions += 1
                        logger.info(
                            "InnerLoop: attempt %d fact-check rejected (%d/%d) — %s",
                            attempt, _fact_revisions, fact_cap,
                            ffb.replace("\n", " ")[:120],
                        )
                        full_ffb = f"fact-check rejected\n{ffb}"
                        feedback.append(f"attempt {attempt}: {full_ffb}")
                        records.append(AttemptRecord(attempt, True, True, False, full_ffb))
                        _trace_stage(task_id, attempt, "fact", "REJECTED",
                                    revisions_used=_fact_revisions, cap=fact_cap)
                        continue
                    else:
                        logger.warning(
                            "InnerLoop: fact revision cap (%d) reached — "
                            "accepting chapter with possible unresolved fact contradiction.",
                            fact_cap,
                        )
                        _trace_stage(task_id, attempt, "fact", "ACCEPTED_AT_CAP", cap=fact_cap)

            # ── AUTO-CR-23-3: continuity gate vs bible + previous chapter ─────
            # Runs after Gate-2 APPROVED, canon, and fact checks; the catch-net
            # that doesn't rely on the model knowing it was wrong, checking the
            # new chapter against (story bible + previous chapter) and returning
            # a concrete "replace X with Y" instruction on a genuine
            # contradiction. Bounded by max_continuity_revisions; fail-open on
            # any error.
            #
            # Checks EVERY target file (see the canon gate above for why a
            # multi-file creative task needs every file checked). Each file's
            # own previous-chapter text is looked up independently — for a
            # task touching chapter_02 and chapter_03 together, chapter_03's
            # "previous chapter" naturally resolves to chapter_02 as this
            # same attempt just wrote it to disk, since files are written
            # before this gate runs.
            if (
                self.task_mode == "creative"
                and self.continuity_validator is not None
                and target_files
            ):
                continuity_cap = getattr(
                    self.continuity_validator, "max_continuity_revisions", 1
                )
                continuity_problem_blocks: list[str] = []
                from tools.auto.continuity_validator import (
                    find_previous_chapter_text, read_story_bible,
                )
                for chapter_file in target_files:
                    try:
                        _continuity_text = (base_dir_path / chapter_file).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        _bible_text = read_story_bible(base_dir_path)
                        _prev_text = find_previous_chapter_text(chapter_file, base_dir_path)
                        known_facts = (
                            _bible_text + "\n\n--- previous chapter ---\n" + _prev_text
                        )
                        continuity_verdict = self.continuity_validator.check(
                            known_facts, _continuity_text
                        )
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning(
                            "InnerLoop: continuity check raised for %s — %s; approving.",
                            chapter_file, exc,
                        )
                        continuity_verdict = None

                    if continuity_verdict is not None and not continuity_verdict.approved:
                        continuity_problem_blocks.append(
                            f"{chapter_file}:\n{continuity_verdict.feedback()}"
                        )

                if continuity_problem_blocks:
                    cfb = "\n\n".join(continuity_problem_blocks)
                    if _continuity_revisions < continuity_cap:
                        _continuity_revisions += 1
                        logger.info(
                            "InnerLoop: attempt %d continuity rejected (%d/%d) — %s",
                            attempt, _continuity_revisions, continuity_cap,
                            cfb.replace("\n", " ")[:120],
                        )
                        full_cfb = f"continuity rejected\n{cfb}"
                        feedback.append(f"attempt {attempt}: {full_cfb}")
                        records.append(AttemptRecord(attempt, True, True, False, full_cfb))
                        _trace_stage(task_id, attempt, "continuity", "REJECTED",
                                    revisions_used=_continuity_revisions, cap=continuity_cap)
                        continue
                    else:
                        logger.warning(
                            "InnerLoop: continuity revision cap (%d) reached — "
                            "accepting chapter with possible unresolved continuity issues.",
                            continuity_cap,
                        )
                        _trace_stage(task_id, attempt, "continuity", "ACCEPTED_AT_CAP",
                                    cap=continuity_cap)

            # ── podrugi-3: theme/content gate vs story-level guidelines ───────
            # Runs after the continuity gate. Every other gate checks
            # consistency; this one checks CONTENT against the author's
            # configured guidelines (e.g. "the story must not glamorize the
            # addiction it depicts"). A chapter can be consistent, complete,
            # and in the right language — and still violate the story's
            # theme contract; nothing else in the chain would ever notice.
            # Bounded by max_theme_revisions; fail-open on any error.
            if (
                self.task_mode == "creative"
                and self.theme_validator is not None
                and target_files
            ):
                theme_cap = getattr(self.theme_validator, "max_theme_revisions", 2)
                theme_problem_blocks: list[str] = []
                for _theme_file in target_files:
                    try:
                        _theme_text = (base_dir_path / _theme_file).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        theme_verdict = self.theme_validator.check(_theme_text)
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning(
                            "InnerLoop: theme check raised for %s — %s; approving.",
                            _theme_file, exc,
                        )
                        theme_verdict = None

                    if theme_verdict is not None and not theme_verdict.approved:
                        theme_problem_blocks.append(
                            f"{_theme_file}:\n{theme_verdict.feedback()}"
                        )

                if theme_problem_blocks:
                    tfb = "\n\n".join(theme_problem_blocks)
                    if _theme_revisions < theme_cap:
                        _theme_revisions += 1
                        logger.info(
                            "InnerLoop: attempt %d theme rejected (%d/%d) — %s",
                            attempt, _theme_revisions, theme_cap,
                            tfb.replace("\n", " ")[:120],
                        )
                        full_tfb = f"theme rejected\n{tfb}"
                        feedback.append(f"attempt {attempt}: {full_tfb}")
                        records.append(AttemptRecord(attempt, True, True, False, full_tfb))
                        _trace_stage(task_id, attempt, "theme", "REJECTED",
                                    revisions_used=_theme_revisions, cap=theme_cap)
                        continue
                    else:
                        logger.warning(
                            "InnerLoop: theme revision cap (%d) reached — "
                            "accepting chapter with possible unresolved theme issues.",
                            theme_cap,
                        )
                        _trace_stage(task_id, attempt, "theme", "ACCEPTED_AT_CAP",
                                    cap=theme_cap)

            # ── AUTO-CR-21: Gate-3 Russian rhythm/rhyme (prosody) gate ────────
            # Runs after Gate-2 APPROVED, canon, and fact checks in creative
            # mode; no-op unless the task is a verse task (ритм/рифм keyword).
            # Bounded by max_prosody_revisions; fail-open on any error.
            #
            # Checks EVERY target file (see the canon gate above for why a
            # multi-file creative task needs every file checked). Cheap when
            # not applicable since .check() itself no-ops on non-verse text.
            if (
                self.task_mode == "creative"
                and self.prosody_validator is not None
                and target_files
            ):
                prosody_cap = getattr(self.prosody_validator, "max_prosody_revisions", 2)
                prosody_problem_blocks: list[str] = []
                _prosody_task = self._task_with_goal(task)
                for _prosody_file in target_files:
                    try:
                        _prosody_text = (base_dir_path / _prosody_file).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        prosody_verdict = self.prosody_validator.check(
                            _prosody_task, _prosody_text
                        )
                    except Exception as exc:  # noqa: BLE001 — fail-open
                        logger.warning(
                            "InnerLoop: prosody check raised for %s — %s; approving.",
                            _prosody_file, exc,
                        )
                        prosody_verdict = None

                    if prosody_verdict is not None and not prosody_verdict.approved:
                        prosody_problem_blocks.append(
                            f"{_prosody_file}:\n{prosody_verdict.feedback()}"
                        )

                if prosody_problem_blocks:
                    pfb = "\n\n".join(prosody_problem_blocks)
                    if _prosody_revisions < prosody_cap:
                        _prosody_revisions += 1
                        logger.info(
                            "InnerLoop: attempt %d prosody rejected (%d/%d) — %s",
                            attempt, _prosody_revisions, prosody_cap,
                            pfb.replace("\n", " ")[:120],
                        )
                        full_pfb = f"prosody rejected\n{pfb}"
                        feedback.append(f"attempt {attempt}: {full_pfb}")
                        records.append(AttemptRecord(attempt, True, True, False, full_pfb))
                        _trace_stage(task_id, attempt, "prosody", "REJECTED",
                                    revisions_used=_prosody_revisions, cap=prosody_cap)
                        continue
                    else:
                        logger.warning(
                            "InnerLoop: prosody revision cap (%d) reached — "
                            "accepting poem with possible unresolved rhythm/rhyme issues.",
                            prosody_cap,
                        )
                        _trace_stage(task_id, attempt, "prosody", "ACCEPTED_AT_CAP",
                                    cap=prosody_cap)

            logger.info("InnerLoop: attempt %d APPROVED", attempt)
            records.append(AttemptRecord(attempt, True, True, True, ""))
            _trace_stage(task_id, attempt, "overall", "APPROVED")
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
        _trace_stage(task_id, self.max_attempts, "overall", "EXHAUSTED")
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
    run_goal: str = "",
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
    from tools.llm_stream import make_unverified_context
    ssl_context: ssl.SSLContext | None = make_unverified_context() if not verify_ssl else None

    max_hints    = config.getint("validator_agent", "max_hints",        fallback=3)
    val_temp     = config.getfloat("validator_agent", "temperature",    fallback=0.1)
    val_timeout  = config.getint("loop",              "timeout_seconds", fallback=300)
    exec_timeout = config.getint("auto",              "exec_timeout_sec", fallback=120)
    ws_retain    = config.getint("auto",              "workspace_retain_count", fallback=5)

    # ── Coder ─────────────────────────────────────────────────────────────────
    if coder is None:
        try:
            from tools.auto.coder import make_coder  # type: ignore
            coder = make_coder(config, task_mode=task_mode, run_goal=run_goal)
        except ImportError:
            logger.warning("Coder not found — using _StubCoder (tests only)")
            coder = _StubCoder()

    # ── Executor ──────────────────────────────────────────────────────────────
    if executor is None:
        try:
            from tools.auto.executor import make_executor  # type: ignore
            executor = make_executor(
                base_dir=base_dir, timeout_sec=exec_timeout,
                max_retained_workspaces=ws_retain,
            )
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

    # ── AUTO-CR-20: Gate-3 per-task fact-compliance gate (creative mode only) ─
    fact_validator = None
    if task_mode == "creative":
        try:
            from tools.auto.fact_validator import make_fact_validator
            fact_validator = make_fact_validator(
                config,
                base_url=base_url,
                api_key=api_key,
                model=model,
                api_format=api_format,
            )
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning("make_inner_loop: fact validator unavailable — %s", exc)
            fact_validator = None

    # ── AUTO-CR-21: Gate-3 Russian rhythm/rhyme (prosody) gate (creative only) ─
    prosody_validator = None
    if task_mode == "creative":
        try:
            from tools.auto.prosody import make_prosody_validator
            prosody_validator = make_prosody_validator(config)
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning("make_inner_loop: prosody validator unavailable — %s", exc)
            prosody_validator = None

    # ── AUTO-CR-23-3: continuity gate vs bible + previous chapter (creative only) ─
    continuity_validator = None
    if task_mode == "creative":
        try:
            from tools.auto.continuity_validator import make_continuity_validator
            continuity_validator = make_continuity_validator(config)
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning("make_inner_loop: continuity validator unavailable — %s", exc)
            continuity_validator = None

    # ── podrugi-3: optional theme/content gate (creative mode only) ────────────
    theme_validator = None
    if task_mode == "creative":
        try:
            from tools.auto.theme_validator import make_theme_validator
            theme_validator = make_theme_validator(config)
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning("make_inner_loop: theme validator unavailable — %s", exc)
            theme_validator = None

    # ── AUTO-CR-21-4: hard per-task wall-clock guard ───────────────────────────
    max_task_seconds = config.getint("auto", "max_task_seconds", fallback=1800)
    require_tests = config.getboolean("inner_loop", "require_tests_code",
                                      fallback=False)

    return InnerLoop(coder, executor, validator, max_attempts=max_attempts,
                     context_broker=broker, canon_validator=canon_validator,
                     fact_validator=fact_validator, prosody_validator=prosody_validator,
                     continuity_validator=continuity_validator,
                     theme_validator=theme_validator,
                     require_tests=require_tests,
                     task_mode=task_mode, max_task_seconds=max_task_seconds,
                     run_goal=run_goal)


# ── Stubs for environments without real agents (unit tests) ──────────────────

class _StubCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        from types import SimpleNamespace
        return SimpleNamespace(succeeded=False, files_written=[], error="stub coder — no real coder available")


class _StubExecutor:
    def run(self, task):
        from types import SimpleNamespace
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="", traceback="stub executor", timed_out=False)
