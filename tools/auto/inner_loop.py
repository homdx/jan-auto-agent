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
[auto]  validator_unavailable_retries — re-runs of ONLY the Gate-2 call when
        it came back without a verdict (transport/parse error) before the
        round is left unreviewed (default 2; RUN-7)
[auto]  coder_transport_retries — re-runs of ONLY the coder call when it died
        before the model could answer (``CoderResult.error_kind ==
        "transport"``) before the round is left unreviewed (default 2; RUN-8)
[collect] error_retry_wait_sec  — pause between those re-runs (default 60; RUN-7)
[validator_agent] temperature   — validator temperature (default 0.1)
[validator_agent] max_hints     — max hint items in rejection (default 3)
[executor] pytest_serial        — run workspace pytest in-process (default true; RUN-3)
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import time
from tools.auto.context_broker import ContextBroker
from tools.auto.gate_registry import (  # GATES-1 / GATES-2
    build_validators, resolve_gate_order, run_gates,
)
from tools.agent_trace import tracer   # AUTO-CR-27: per-stage decision tracing
from tools.auto.utils import is_pytest_command   # RUN-3
from tools.llm_stream import strip_json_fence     # HARDEN-1 A7: Gate 2's fence handling
# HARDEN-7: the shared readers of a reply field's type. Gate 2's "approved" is
# read fail-closed and its "feedback" as text, so a verdict a small local model
# spelled as a string can neither approve a rejection nor crash the review.
from tools.llm_stream import reply_is_yes, reply_text

from dataclasses import dataclass, field
from pathlib import Path
from tools.config_safe import safe_getboolean, safe_getint
from tools.auto.verdict_followup import STRICT_VERDICT_RULE   # VERDICT-ASK

logger = logging.getLogger(__name__)


_DEFAULT_MAX_ATTEMPTS = 5
# Validator re-runs when it returns no verdict (a transport or parse error); the count is validator_unavailable_retries (RUN-7).
_DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES  = 2
_DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC = 60.0
# Coder re-runs after a silent socket; the count is coder_transport_retries (RUN-8).
_DEFAULT_CODER_TRANSPORT_RETRIES   = 2
_DEFAULT_CODER_TRANSPORT_WAIT_SEC  = 60.0

# Data classes

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
    # Set when the validator never answered on any retry: a technical failure, not a rejection, and the coder is not told it was rejected (RUN-7).
    unavailable:   bool  = False
    # The last "validator unavailable" text, kept only for the outer loop's warning and never fed back.
    unavailable_reason: str = ""
    # Which stage left the round unavailable ("coder" or "gate2"), so the log names the real outage (RUN-8).
    unavailable_stage: str = ""
    # Seconds a dead coder call used, credited back to the task deadline once, so they are not charged twice (RUN-8).
    deadline_credit_s: float = 0.0


# LLM-backed Gate-2 validator (LOOP-1)

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
    # The example bullets are language-neutral English, and the language is injected explicitly, so a project is not critiqued in Russian (AUTO-FIX).
    "Example of the FORMAT ONLY. Its language carries no meaning — write your "
    "own reply in the chapter's language, whatever that is, and do not reuse "
    "this wording:\n"
    "REVISE:\n"
    "1. The character's line in paragraph 2 does not answer the task — turn it "
    "into a short exchange of direct speech on the task's subject, in your own "
    "words.\n"
    "2. The scene description repeats another chapter word for word — cut it to "
    "a single phrase or remove it.\n"
    "3. The character is called \"he\" although the facts say she is a woman — "
    "bring every pronoun and verb form into agreement.\n"
    + STRICT_VERDICT_RULE   # VERDICT-ASK
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


@dataclass
class Gate2Verdict:
    """Unified GateVerdict for Gate 2 (the LLM validator).

    Wraps the ``(approved, feedback)`` tuple :meth:`LLMGate2Validator.approve`
    has always returned so Gate 2 reports its verdict through the same
    :class:`~tools.auto.gate_verdict.GateVerdict` interface (``approved`` +
    ``feedback()``) as every Gate-3 validator. The existing ``approve()``
    tuple return is unchanged for the many call sites and tests that unpack
    it; :meth:`LLMGate2Validator.approve_verdict` returns this object.

    Attributes
    ----------
    approved:
        ``True`` when the validator accepts the attempt; ``False`` when it
        rejects (or fail-closed on an error — the validator never silently
        approves, it returns ``False`` with a ``validator unavailable: …``
        message).
    reason:
        The raw feedback string (``""`` on acceptance, a structured
        ``Reason: …`` / ``Hints: …`` block on rejection). Kept under its
        existing name for callers that read the tuple's second element
        directly.
    unavailable:
        RUN-7: ``True`` only when ``approved is False`` because the
        ``except Exception`` exit fired (a transport or parse error) —
        never for a model that actually answered ``{"approved": false}``.
        The inner loop uses this to tell a real rejection from a provider
        outage that happens to look like one; ``approve()``'s own
        two-tuple return does not carry this distinction, so it is only
        available through :meth:`LLMGate2Validator.approve_verdict`.
    """

    approved: bool
    reason: str = ""
    unavailable: bool = False

    def feedback(self) -> str:
        """Coder-facing message. Empty on acceptance, the reason on rejection."""
        return self.reason


class LLMGate2Validator:
    """Fail-closed LLM-based Gate-2 validator.

    Calls the model and parses ``{"approved": bool, "feedback": str, ...}``.
    Any network / parse error returns ``(False, "validator unavailable: …")``.
    """

    # Empty at class level: objects built without __init__ keep request_completion's default budget instead of an AttributeError (RUN-10).
    _retry_kwargs: dict = {}

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
        think: bool | None = None,
    ):
        self.base_url    = base_url
        self.model       = model
        self.api_key     = api_key
        self.api_format  = api_format
        self.temperature = temperature
        # The timeout is cast and clamped like max_hints, so a bad value cannot break the call (AUTO-FIX).
        try:
            self.timeout = max(1, int(timeout))
        except (TypeError, ValueError):
            self.timeout = 120
        self.max_hints   = max(1, int(max_hints))
        self.ssl_context = ssl_context
        self.base_dir    = Path(base_dir)
        self.last_missing_context: list[str] = []
        # Side channel: True only when the validator failed to answer (transport or parse error), never for a real rejection (RUN-7).
        self.last_unavailable: bool = False
        self.num_ctx     = int(num_ctx)
        self.max_tokens  = int(max_tokens)
        self.task_mode   = str(task_mode)
        self._config     = config
        # The validator's HTTP retry budget comes from the [loop] section; a directly built validator uses the defaults (RUN-10).
        from tools.llm_stream import retry_kwargs_from_config
        self._retry_kwargs = retry_kwargs_from_config(config)
        # Gate 2 needs strict JSON; a per-profile think override is honoured, and the base key is used only when none is given (BUGFIX).
        self._think = (
            think if think is not None
            else (safe_getboolean(config, "validator_agent", "think", fallback=False) if config is not None else False)
        )
        # System prompt: the mode-specific override first, then the legacy key in code mode, then the built-in (AUTO-DM-5, AUTO-CR-19-1).
        self._system = _resolve_validator_system(config, self.task_mode)

        # Task 3 — smart context additions
        from tools.search_agent import make_search_agent
        self._search_agent = make_search_agent(config, base_dir) if config else None
        self._context_probe_enabled = (
            safe_getboolean(config, "coder", "context_probe", fallback=True) if config else True
        )
        self._max_chars_per_dep = (
            safe_getint(config, "coder", "max_chars_per_dep", fallback=2000) if config else 2000
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
            # A non-string or empty entry gets its own visible note instead of aborting the prompt build (B8).
            if not isinstance(rel, str) or not rel.strip():
                blocks.append(
                    f"--- (not a usable path: {rel!r}) ---\n"
                    f"(the coder reported this entry in files_written, but it "
                    f"is not a path — expected a non-empty string, got "
                    f"{type(rel).__name__})"
                )
                continue
            try:
                content = (_base / rel).read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                # A file missing with a .coder.bak sibling is a deliberate deletion, not a read failure (AUTO-FIX).
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

    def _language_directive(self, coder_result, base_dir) -> str:
        """An explicit "reply in <language>" line for the critique, or "".

        AUTO-FIX (language-leak). The creative system prompt says to write the
        problem list "in the chapter's language", which asks the model to infer
        something this process already knows for certain — and the model got it
        wrong. On an entirely English project the whole critique came back in
        Russian, 61 rejections in a row, because the prompt's own example
        bullets happened to be written in Russian.

        Those examples are language-neutral now, but removing a nudge is not
        the same as giving an answer. This states it outright, using the same
        ``resolve_creative_language`` the coder's language lock uses, so the
        two can never disagree about what the chapter's language is.

        Returns "" whenever the language cannot be resolved: an unresolved
        language must not become a confidently wrong instruction, and the
        prompt's own "in the chapter's language" wording still applies.
        """
        if self.task_mode != "creative":
            return ""
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            from tools.auto.utils import resolve_creative_language  # noqa: PLC0415

            sample = ""
            for rel in (getattr(coder_result, "files_written", None) or []):
                try:
                    sample = (_Path(base_dir) / str(rel)).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if sample.strip():
                    break
            if not sample.strip():
                return ""
            language = resolve_creative_language(
                self._config, sample, task_mode="creative")
            if not language:
                return ""
            return (
                f"\n\nLANGUAGE: write your entire reply in {language}. "
                f"The verdict word (APPROVED / REVISE) stays in English."
            )
        except Exception as exc:  # noqa: BLE001 — advisory only, never blocks
            logger.debug("Gate2 language directive skipped: %s", exc)
            return ""

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

            # With no synopsis yet, the language is taken from the chapters already on disk (podrugi run, AUTO-FIX).
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
        self.last_unavailable = False   # RUN-7: reset every call, like last_missing_context

        # A cheap language check for creative mode: it only ever asks for a revision, never approves on its own (AUTO-FIX).
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

            _system_msg = self._system + self._language_directive(
                coder_result, base_dir)

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

            # On a re-review the model sees its own previous critique, so it checks what was fixed (AUTO-CR-30).
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
                            {"role": "system",  "content": _system_msg},
                            {"role": "user",    "content": um},
                        ],
                        "options": _val_opts,
                        "think": getattr(self, "_think", False),
                    }
                else:
                    _payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system",  "content": _system_msg},
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
                    **self._retry_kwargs,
                )
                return strip_think(_r or "")

            raw = _call_validator(user_msg)
            # An empty reply is reported clearly, not as a JSON decoding error.
            if not raw or not raw.strip():
                raise ValueError(
                    "validator model returned an empty response "
                    "(possible network error or silent refusal)"
                )

            # ── Creative mode: line-oriented soft verdict (AUTO-CR-2) ─────────
            if self.task_mode == "creative":
                # If the verdict is buried in prose, the validator is asked once more with a hard nudge (AUTO-CR-31);
                # an approval that goes on to name a problem is asked about the same way, its own reply quoted (VERDICT-ASK).
                from tools.auto.verdict_followup import settle_verdict  # noqa: PLC0415
                approved, reason, unparseable, raw = settle_verdict(
                    raw, lambda extra: _call_validator(user_msg + extra),
                    who="LLMGate2Validator [creative]",
                )
                if unparseable:
                    logger.warning(
                        "LLMGate2Validator [creative]: verdict still unparseable "
                        "after one retry — passing on fail-open. raw=%r", raw[:120]
                    )
                if approved:
                    return True, ""
                # The full critique is passed to the coder, since a numbered list would be cut by the line parser (AUTO-CR-29).
                critique = raw.strip()
                import re as _re  # local import — matches _parse_verdict_soft's convention; approve() has no module-level `re` import in scope
                # Verdict words are matched whole, so a past-tense reply such as "REVISED" is not read as a prefix (AUTO-FIX).
                critique = _re.sub(
                    r"^(?:revise[d]?|reject(?:ed)?)\s*:?\s*|^no\s*:\s*",
                    "",
                    critique,
                    count=1,
                    flags=_re.IGNORECASE,
                )
                if not critique:
                    critique = reason  # fall back to the parsed reason
                return False, f"Reason: {critique}"

            # ── Code / docs mode: strict JSON path (unchanged) ───────────────
            # HARDEN-1 A7: Gate 2 used to strip the fences with its own
            # `raw.split("```json")[1].split("```")[0]`, which cut a verdict at
            # the first fence *inside* one of its strings — a rejection whose
            # hint quotes a code sample lost the hint, and an approval became
            # "validator unavailable", so InnerLoop re-ran Gate 2 and then
            # discarded the passing candidate. `strip_json_fence` parses the
            # whole reply first and only then falls back to fence stripping,
            # getting fix 36's `_before_closing_fence` for the wrapped case too.
            raw = strip_json_fence(raw)

            # A reply that is only code fences is reported as empty, not as a JSON error.
            if not raw or not raw.strip():
                raise ValueError(
                    "validator model returned empty JSON content "
                    "(empty code fences or no body after stripping markdown)"
                )

            parsed   = json.loads(raw)
            # A valid JSON that is not an object gets a clear error instead of an attribute error; a one-item list is unwrapped.
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"validator returned {type(parsed).__name__}, expected JSON object"
                )
            # missing_context may come back as a string; it is type-checked so characters are not iterated (BUGFIX).
            raw_missing = parsed.get("missing_context")
            if isinstance(raw_missing, list):
                self.last_missing_context = [
                    str(x).strip() for x in raw_missing if str(x).strip()
                ]
            else:
                self.last_missing_context = []
            # HARDEN-7 A: read the verdict fail-closed. `bool(...)` made every
            # non-empty string true, so `{"approved": "false", "feedback":
            # "REJECT: ..."}` — a rejection in the model's own words — was
            # committed as an approval. Only an explicit yes approves now, and
            # the reply's feedback is kept for the rejection below.
            approved = reply_is_yes(parsed.get("approved"))
            if approved:
                return True, ""

            # Build LOOP-1 structured feedback string
            return False, _format_gate2_feedback(parsed, self.max_hints)

        except Exception as exc:
            logger.warning("LLMGate2Validator error: %s", exc)
            self.last_missing_context = []
            self.last_unavailable = True   # RUN-7: transport/parse error, not a verdict
            return False, f"validator unavailable: {exc}"

    def approve_verdict(
        self,
        task:         dict,
        exec_result,
        coder_result,
        *,
        base_dir=None,
        prior_critique: str = "",
    ) -> "Gate2Verdict":
        """Return a :class:`Gate2Verdict` (the unified GateVerdict interface).

        Thin wrapper over :meth:`approve` that keeps the existing tuple
        return contract intact for every existing caller/test while also
        exposing the verdict through the shared ``approved`` / ``feedback()``
        interface used by :mod:`tools.auto.gate_registry`'s runner. The
        ``last_missing_context`` side channel is populated identically to
        :meth:`approve` — this method calls it directly.
        """
        approved, reason = self.approve(
            task, exec_result, coder_result,
            base_dir=base_dir, prior_critique=prior_critique,
        )
        # last_unavailable is set only on the exception exit, never for a real rejection (RUN-7).
        return Gate2Verdict(approved=approved, reason=reason,
                            unavailable=self.last_unavailable)


def _parse_verdict_soft(text: str) -> tuple[bool, str, bool]:
    """Parse a line-oriented Gate-2 verdict for creative mode (AUTO-CR-2 / CR-26-1).

    Protocol: the model is expected to reply with one line whose first token is
    ``APPROVED`` (or ``OK``) or ``REVISE`` / ``REJECT`` / ``NO`` / ``NOT``.  If a
    rejection token, the reason follows after it (``: `` or a space).

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
        * ``REVISE: <reason>`` / ``REJECT: …`` / ``NO: …`` /
          ``NOT <anything>`` / ``NOTHING <prose>`` / ``NONSENSE``  → approved=False
          (``NOT BAD`` / ``NOTHING TO ADD`` / ``NOTHING WRONG`` / ``NOTE:``
          are the exceptions — approvals, matched before the NOT family)
        * Russian APPROVED forms (нет противоречий, согласна, …)  → approved=True
        * Russian REVISE forms (не согласна, противоречие, …)     → approved=False
        * Any other content (rambling, JSON, prose, …)            → approved=True (fail-open)

    Order is load-bearing: negated-positive RU rules (нет противоречий, не против,
    не противоречит) and the «не соглас» rule are tested **before** the bare
    negative patterns (против, противоречи…, соглас) to avoid mis-classification,
    and the English approvals are tested **before** the English rejection tokens
    for the same reason: ``NOT BAD`` is an approval whose first token is ``NOT``.

    The English verdict tokens match as whole words only (HARDEN-1 A1, see
    ``_verdict_token_at_start``): ``Normally`` / ``NOPE`` / ``REVISED`` are not
    verdicts. HARDEN-5 A adds ``NOT`` / ``NOTHING`` / ``NONSENSE`` back as
    rejection tokens — after HARDEN-1 A1 a reply that began with ``NOT``
    matched nothing and fell through to the fail-open approval — and the three
    explicit NOT-family approvals listed above are matched first, so they keep
    failing open to the later rules as prose.
    """
    import re as _re

    # REP-VERDICT B: a verdict word the model wrapped in markdown or put behind a label —
    # "**REVISE**: …", "- REVISE: …", "1. REVISE", "[REVISE] …", "> REVISE", "`REVISE`",
    # "Verdict: REVISE", "**Verdict:** REJECT — …", "Answer: NO" — matched no token, was
    # unparseable, and the continuity, fact, theme and architect validators (no re-ask)
    # approved the chapter on fail-open. Such a line is read without its wrapper; a line
    # whose unwrapped text does not open with a verdict word keeps its own text.
    _DECOR_LEAD = _re.compile(r"^(?:\s*(?:#{1,6}\s+|>\s*|[-+•]\s+|\*\s+|\d{1,2}[.)]\s+))+")
    _DECOR_WRAP = _re.compile(r"^(\*\*|__|\*|_|`|\[|\()\s*")
    _DECOR_LABEL = _re.compile(
        r"^(?:FINAL\s+)?(?:VERDICT|DECISION|ANSWER|RESULT|STATUS|CONCLUSION)\s*:\s*", _re.I)
    _DECOR_VERDICT_WORD = _re.compile(
        r"^(?:APPROVED|OK|REVISE|REJECT(?:ED)?|NO|NOT|NOPE|NEEDS?)\b", _re.I)

    def _undecorate(line: str) -> str:
        s = line.strip()
        for _ in range(4):
            prev = s
            s = _DECOR_LEAD.sub("", s)
            w = _DECOR_WRAP.match(s)
            if w:
                closer = {"[": "]", "(": ")"}.get(w.group(1), w.group(1))
                s = s[w.end():]
                at = s.find(closer)
                if 0 < at <= 40:
                    s = s[:at] + s[at + len(closer):]
            s = _DECOR_LABEL.sub("", s).strip()
            if s == prev:
                break
        if s != line.strip() and _DECOR_VERDICT_WORD.match(s):
            return s
        return line

    text = "\n".join(_undecorate(ln) for ln in (text or "").splitlines())

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

    def _verdict_token_at_start(candidate: str, token: str) -> bool:
        """Whether *candidate* begins with the verdict word *token*, as a whole word.

        HARDEN-1 A1: ``str.startswith`` alone made every word that merely begins
        with a verdict token a verdict — ``NOT BAD``, ``Nothing to add``,
        ``NOTE:``, ``Normally``, ``NOPE`` were all read as a bare ``NO``
        rejection whose "reason" was the word's own tail (``'T BAD'``,
        ``'thing to add, looks good'``), and that garbage was handed to the
        Coder as the thing to fix: a whole round burned on an approval.
        ``REVISED`` / ``REJECTION`` were read the same way.

        A verdict token ends at a non-alphanumeric character (``NO: x``,
        ``NO, x``, ``No - x``, ``REVISE: x``) or at the end of the line
        (``NO``). Both loops below go through this one helper, so they cannot
        drift apart again.
        """
        if not candidate.startswith(token):
            return False
        rest = candidate[len(token):]
        return not rest or not rest[0].isalnum()

    # ── APPROVED patterns (Russian) — must be checked BEFORE bare negatives ──
    _RU_APPROVED = [
        # «нет противоречий» / «противоречий нет» / «не противоречит»
        _re.compile(r"\b(нет|без)\s+противоречий\b"),
        _re.compile(r"\bпротиворечий\s+нет\b"),
        _re.compile(r"\bне\s+противоречит\b"),
        # Russian negated-contradiction patterns: filler words are allowed between the noun and the verb, but only search verbs count (AUTO-BUG).
        _re.compile(
            r"противоречи\w*(?:\s+\S+){0,3}\s+"
            r"(?:отсутств\w*|не\s+(?:обнаружен\w*|выявлен\w*|найден\w*|"
            r"встречает\w*|замечен\w*|видн\w*))"
        ),
        # The negation must be followed by a search or perception verb; "not expected" or "not liked" do not approve (BUGFIX).
        _re.compile(
            r"\bне\s+(?:вижу|нахожу|нашёл|нашел|обнаружил|выявил|заметил|"
            r"встретил|встречаю|смог\s+найти)"
            r"(?:\s+\S+){0,3}\s+противоречи"
        ),
        # «не против» / «непротив»
        _re.compile(r"\bне\s+против\b"),
        _re.compile(r"\bнепротив\b"),
        # Approval words not preceded by a negation (one-word and spaced forms both checked).
        _re.compile(r"(?<!не)(?<!не )одобр"),
        # «принято / принимаю / можно принять» — NOT preceded by «не » or «не»
        _re.compile(r"(?<!не)(?<!не )принят"),
        _re.compile(r"\bможно\s+принять\b"),
        # "всё верно" / "все верно" count only when not preceded by "не " (the one-word form is blocked by \b).
        _re.compile(r"(?<!не )\b(всё|все)\s+верно\b"),
        # "соответствует" does not match after "не" or "не " (the negated one-word form).
        _re.compile(r"(?<!не)(?<!не )соответствует\b"),
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
        # A standalone "No," opener is a rejection, except the discourse filler "No, it's all fine" (AUTO-BUG).
        _re.compile(
            r"^нет(?:[,.]|$)"
            r"(?!\s*(?:всё|все)\s+(?:хорошо|нормально|в\s+порядке|ок(?:ей)?|отлично|супер)\b)"
        ),
    ]

    # English "NO, it's good" guard: an explicit subject is needed before the adjective; "NO PROBLEMS" is a clean approval (BUGFIX).
    _EN_DISCOURSE_OK = _re.compile(
        r"^NO[,.]?\s*(?:"
        r"(?:IT'?S|IT IS|THAT'?S|THAT IS|EVERYTHING'?S|EVERYTHING IS|ALL)"
        r"\s*GOOD"
        r"|(?:IT'?S|IT IS|THAT'?S|THAT IS|EVERYTHING'?S|EVERYTHING IS|ALL)?"
        r"\s*(?:FINE|PERFECT|GREAT|OK|OKAY|CORRECT|IN ORDER)"
        r"|PROBLEMS?|ISSUES?|CONCERNS?|COMPLAINTS?"
        r")\b"
    )

    # "NO REVISIONS NEEDED" is a clean approval, matched before the generic "NO" rejection (AUTO-BUG).
    _EN_NOTHING_NEEDED = _re.compile(
        r"^NO\s+(?:FURTHER\s+|ADDITIONAL\s+|MORE\s+)?"
        r"(?:REVISIONS?|CHANGES?|EDITS?|FIXES?|CORRECTIONS?|ADJUSTMENTS?)\s+"
        r"(?:NEEDED|REQUIRED|NECESSARY)\b"
    )

    # English "OK, but ..." caveat guard: an approval followed by a problem is not an approval (BUGFIX).
    # HARDEN-26 A: the contrast after "OK" also opens with a dash or an opening parenthesis —
    # "OK — but the seed entry is gone.", "OK (no issues)" — the same boundaries the clause
    # caveat below takes, so the two readers cannot drift apart.
    _EN_OK_CAVEAT = _re.compile(
        r"^OK(?:\s*[,.]\s*|\s*[—–-]\s*|\s*\(\s*|\s+)"
        r"(BUT|HOWEVER|SO)\b[,:]?\s*(.*)$"
    )

    def _ok_caveat_reason(s: str) -> "str | None":
        m = _EN_OK_CAVEAT.match(s.strip().upper())
        if not m:
            return None
        rest = s.strip()[m.start(2):].strip() if m.group(2) else ""
        return rest if rest else "validator rejected (no reason given)"

    # ── HARDEN-5 A: "NOT …" opens a rejection, but an explicit approval first ──
    # HARDEN-1 A1 made the tokens match as whole words, which took the rejection
    # away from every reply beginning with "NOT": "NOT APPROVED", "Not good
    # enough" and "Nothing works" matched nothing, were unparseable, and
    # unparseable means approve — a chapter the model rejected went through.
    # The NOT / NOTHING / NONSENSE rejections are added back below, and these
    # three approvals are checked *before* them, so "NOT BAD" and "NOTHING TO
    # ADD" stay approvals and "NOTE:" is read as prose (they fall through to
    # the fail-open rules on purpose now, the way A1 made them do it by
    # accident). "Normally", "NOPE", "NOT APPROVED" stay where they are: the
    # NOT token needs a whole word after "NOT", so "NOPE" is not a NOT.
    _EN_NOT_BAD = _re.compile(r"^NOT\s+BAD\b")
    #: The actions a validator can have "nothing of": "nothing to add", "nothing to fix".
    #: `_EN_NOTHING_OK`'s `TO` shape and the `NEEDS` / `TO BE` shapes of HARDEN-22 A both
    #: read this one vocabulary, so the two cannot disagree about what has to be done.
    _EN_NOTHING_ACTIONS = (
        r"ADD(?:S|ED|ING)?|FIX(?:ES|ED|ING)?|CHANGE(?:S|D|ING)?|"
        r"IMPROVE(?:S|D|MENT)?|CORRECT(?:S|ED|ION)?|"
        r"REPORT(?:S|ED|ING)?|FLAG(?:S|ED|ING)?|NOTE(?:S|D)?"
    )
    _EN_NOTHING_OK = _re.compile(
        r"^NOTHING\s+(?:TO\s+)?"
        r"(?:" + _EN_NOTHING_ACTIONS + r"|"
        r"COMPLETELY\s+WRONG|WRONG|"
        r"FOUND|ISSUE(?:S)?|PROBLEM(?:S)?|CONCERN(?:S)?|"
        r"COMPLAINT(?:S)?|CONTRADICT(?:ORY|S|ING|ION(?:S)?)?)\b"
    )
    _EN_NOTE = _re.compile(r"^NOTE\b")
    #: The verdict words a "NOT" negates into a rejection — the approval side (HARDEN-5 A).
    #: HARDEN-14 A: this set is no longer the *decision*: a "NOT …" reply is a rejection unless it
    #: is on the problem side (see `_EN_NOT_PROBLEM`), because every creative rejection that is not
    #: "NOT APPROVED"-shaped — "Not coherent", "NOT READY", "Nothing makes sense" — fell through
    #: to the fail-open rule and shipped the chapter. The set stays as the class of words the NOT
    #: family is *known* to negate, which is what the problem-side guard below is built against.
    _EN_NOT_NEGATES_APPROVAL = frozenset((
        "APPROVED", "APPROVABLE", "APPROVE", "ACCEPTED", "ACCEPTABLE", "ACCEPT", "AGREED",
        "OK", "OKAY", "GOOD", "VALID", "CONSISTENT", "CORRECT", "RIGHT", "TRUE", "EXACT",
        "FINE", "PERFECT", "GREAT", "SOUND", "COMPLETE", "COMPLETELY", "FULLY", "DONE",
        "RECOMMENDED", "ADEQUATE", "SATISFACTORY", "CLEAN", "CLEAR", "PROPER", "FITTING",
        "SUFFICIENT", "RESOLVED", "FIXED", "WORKS", "WORKING",
    ))
    #: The problem words a "NO" negates into an approval (HARDEN-10 C): "No contradictions
    #: found" is how the creative validator says there is nothing to fix. One vocabulary —
    #: it is built into both the "No <problem>" phrases and the "the rest of the reply
    #: names a problem" signal of HARDEN-17 A2 (`_EN_PROBLEM_NAMED`), so the approval rule
    #: and the caveat rule cannot disagree about what a problem is.
    _EN_PROBLEM_NOUNS = (
        r"CONTRADICTION(?:S)?|ERROR(?:S)?|INCONSISTENC(?:Y|IES)|CONFLICT(?:S)?"
        r"|VIOLATION(?:S)?|OBJECTION(?:S)?|ISSUE(?:S)?|PROBLEM(?:S)?|CONCERN(?:S)?"
        r"|FLAW(?:S)?|OMISSION(?:S)?|AMBIGUIT(?:Y|IES)|DEFECT(?:S)?"
        r"|DISCREPANC(?:Y|IES)|MISTAKE(?:S)?|TYPO(?:S)?"
    )
    #: AUDIT3-C13 (#49): one qualifier may stand before the noun ("No plot holes",
    #: "No logic errors"), nouns may be joined by OR/AND ("No contradictions or
    #: inconsistencies"), and "No changes are needed" / "No need for changes" deny
    #: the work itself. The qualifier list is closed, so "No time, errors remain"
    #: stays where it was.
    _EN_PROBLEM_QUALIFIER = (
        r"(?:PLOT|LOGIC(?:AL)?|CONTINUITY|FACTUAL|GRAMMATICAL|SPELLING|STYLE|STYLISTIC"
        r"|MAJOR|MINOR|OBVIOUS|REAL|SIGNIFICANT|FURTHER|REMAINING|OTHER|NEW|MORE)\s+"
    )
    _EN_PROBLEM_NOUN_EXTRA = r"|GAP(?:S)?|HOLE(?:S)?|RED\s+FLAGS?"
    _EN_NO_PROBLEM = _re.compile(
        r"^NO\b\s+(?:" + _EN_PROBLEM_QUALIFIER + r")?"
        r"(?:" + _EN_PROBLEM_NOUNS + _EN_PROBLEM_NOUN_EXTRA + r")\b"
        r"(?:\s+(?:OR|AND|NOR)\s+(?:" + _EN_PROBLEM_QUALIFIER + r")?"
        r"(?:" + _EN_PROBLEM_NOUNS + _EN_PROBLEM_NOUN_EXTRA + r")\b)*"
        r"(?:\s+(?:WERE\s+|WAS\s+)?(?:FOUND|REMAIN(?:ING)?|LEFT|DETECTED))?"
        r"|^NO\s+(?:FURTHER\s+|MORE\s+|OTHER\s+)?"
        r"(?:CHANGE|REVISION|EDIT|REWRITE|FIX|CORRECTION|ADJUSTMENT|MODIFICATION)(?:S|ES)?"
        r"\s+(?:ARE\s+|IS\s+)?(?:NEEDED|REQUIRED|NECESSARY)\b"
        r"|^NO\s+NEED\s+(?:FOR\s+(?:ANY\s+)?(?:CHANGE|REVISION|EDIT|REWRITE|FIX|CORRECTION)(?:S|ES)?"
        r"|TO\s+(?:CHANGE|REVISE|EDIT|REWRITE|FIX|CORRECT)\b(?:\s+ANYTHING)?)\b"
        # REL-49: the shape, not a closed noun list — "No rework needed.", "No faults
        # found.", "No hallucinations found." One to three plain words, no comma or stop,
        # so "No time, errors remain" and "No. Errors found." stay out.
        r"|^NO\s+(?:[A-Z][A-Z'-]*\s+){0,2}[A-Z][A-Z'-]*\s+(?:WERE\s+|WAS\s+|ARE\s+|IS\s+)?"
        r"(?:FOUND|DETECTED|SPOTTED|IDENTIFIED|NEEDED|REQUIRED|NECESSARY)\b"
    )

    #: The articles and qualifiers that stand between "NOT" and the verdict word
    #: ("NOT A PROBLEM", "NOTHING COMPLETELY WRONG", "NOTHING VERY BAD").
    _EN_NOT_FILLERS = frozenset(("A", "AN", "ANY", "VERY", "COMPLETELY", "QUITE", "ALL", "TOO", "AT ALL", "SINGLE",
                                 "OBVIOUSLY"))
    _EN_NOTHING_CALM = _re.compile(r"^NOTHING\s+(?:TO\s+WORRY\s+ABOUT|(?:JUMPS|STANDS|STICKS)\s+OUT(?:\s+AS\s+(?:A\s+)?(?:PROBLEM|ISSUE|WRONG))?)\b")

    def _explicit_en_approval(upper: str) -> bool:
        """Whether *upper* says the opposite of a NOT/NOTHING rejection."""
        return bool(
            _EN_NOT_BAD.match(upper)
            or _EN_NOTHING_OK.match(upper)
            or _EN_NOTE.match(upper)
        )

    #: A contrast that OPENS A CLAUSE is the way a "No <problem>" verdict turns into a
    #: complaint: it stands right after the phrase ("No errors except one: …") or after a
    #: stop, a comma or a semicolon ("No issues, but …", "No issues found. However, …",
    #: "No issues; however …"). HARDEN-17 A2: nowhere else. The old rule took any `;`,
    #: `ONLY` or `YET` anywhere after the phrase for a caveat, which sent eleven natural
    #: approvals ("No issues found; everything matches the bible.") back to the Coder with
    #: the approval sentence as the reason to fix. "YET ANOTHER" is not a contrast.
    #: CC review fixes 47: the blanks after the boundary are one run of spaces and tabs
    #: and nothing else may take them — HARDEN-26 A wrote `\s*` twice in a row, `-\s+`
    #: before a `\s*` and a `\s*` before the parenthesis, so "Looks good." and three
    #: thousand blank lines took 95 s. A newline is a boundary of its own, so the last
    #: one before the contrast still opens it: every match ends where it ended before.
    _EN_CLAUSE_CAVEAT = _re.compile(
        r"(?:^|[,;:.\!\?\n]|[—–]|(?<=\s)-\s|\()[^\S\n]*(?:"
        r"(?:APART|ASIDE)\s+FROM|OTHER\s+THAN|NOTWITHSTANDING|"
        r"BUT|HOWEVER|EXCEPT|ALTHOUGH|THOUGH|YET(?!\.?\s+ANOTHER\b)|ONLY|"
        # AUDIT3-C13 (#50): the other ways a clause names its exception, and a
        # request to change something right after the approval.
        # CC review fixes 50: each in its exception sense only — "Save the
        # file", "Bar none the best chapter", "Beyond that, great work" and
        # "Besides, it reads well" are not exceptions, and rejected an approval.
        # REL-61: "beyond reproach", "beyond a doubt", "besides being consistent"
        # are praise — only a determiner after the word names the exception.
        r"BESIDES(?=\s+(?:THE|THAT|THESE|THOSE)\b)|BEYOND(?=\s+THE\b)|BARRING|SAVE\s+FOR|"
        r"BAR(?!\s+NONE\b)|UNLESS|"
        r"WITH\s+THE\s+EXCEPTION\s+OF|"
        r"PLEASE\s+(?:FIX|CHANGE|CORRECT|UPDATE|REVISE|ADD|REMOVE|ADDRESS|RENAME|REWRITE))\b"
    )

    #: A contrast word anywhere after a conflict denial, with no stop before it (HARDEN-25 A).
    _EN_CONTRAST_ANYWHERE = _re.compile(r"\b(?:BUT|HOWEVER|EXCEPT|ALTHOUGH|THOUGH|APART\s+FROM|OTHER\s+THAN)\b")

    #: A problem word anywhere in a clause, for the "…; one problem: …" shape, where no
    #: contrast word stands between the approval and the complaint.
    _EN_PROBLEM_NAMED = _re.compile(r"\b(?:" + _EN_PROBLEM_NOUNS + r")\b")
    #: The negators that turn a named problem back into praise: "no problems", "no major
    #: issues", "nothing contradicts the bible".
    _EN_NEGATED_BY = frozenset((
        "NO", "NOT", "NOTHING", "NONE", "NEVER", "NOBODY", "NOR", "NEITHER", "WITHOUT",
        "LACK", "LACKS",
    ))

    def _names_a_problem(upper: str) -> bool:
        """Whether *upper* names a problem rather than denying one.

        The negation is looked up two words back, so "one problem" and "minor issues"
        name a problem while "no problems", "no major issues" and "nothing contradicts"
        deny one — the two read identically and only the first is a complaint.
        """
        for m in _EN_PROBLEM_NAMED.finditer(upper):
            before = upper[:m.start()].split()[-2:]
            if not any(w in _EN_NEGATED_BY for w in before):
                return True
        return False

    #: "Not approved" after an approval phrase is the verdict too ("No issues. Not approved.").
    _EN_NOT_APPROVED = _re.compile(r"^NOT\s+(?:APPROVED|ACCEPTABLE|ACCEPTED|OK|READY)\b")

    def _directive_reason(sent: str) -> "str | None":
        """The reason when the sentence *sent* opens with a rejection directive.

        HARDEN-22 B: one word list (`_EN_REJECT_HEAD`) decides whether a sentence that follows
        an approval phrase is the verdict — before, the scan knew only REVISE / REJECT /
        NEEDS REVISION, so "REJECTED: …", "Rejected - …" and "Needs revision: …" were missed.
        HARDEN-25 B adds FIX and CHANGE to that list, and reads a directive whose object is
        "nothing" / "none" / "no <noun>" as the withdrawal of the directive, not as one.
        """
        up = _re.sub(r"^PLEASE\s+", "", sent.strip().upper())
        hm = _EN_DIRECTIVE_HEAD.match(up)
        if hm is None:
            hm = _EN_NOT_APPROVED.match(up)
        if hm is None:
            return None
        if _EN_NOT_APPROVED.match(up):
            return sent.strip()
        if _EN_DIRECTIVE_DENIAL_OBJECT.match(up[hm.end():].strip()):
            # "REJECT NOTHING." / "REVISE NONE." / "FIX NO ISSUES.": the object denies the
            # directive, so the sentence is not the verdict — the line stays an approval.
            return None
        if _EN_REJECT_HEAD.match(up) is None:
            # FIX / CHANGE: a directive after an approval phrase only, so there is no
            # first-line token to hand to `_en_rejection_reason` — the reason is what follows.
            plain = _re.sub(r"^\s*PLEASE\s+", "", sent, flags=_re.I).strip()
            rest = plain[hm.end():].lstrip(" \t:;,.!?–—-").strip()
            return rest if rest else "validator rejected (no reason given)"
        return _en_rejection_reason(_re.sub(r"^\s*PLEASE\s+", "", sent, flags=_re.I), up)

    def _sentences_after(line: str, start: int, text: str) -> list[str]:
        """The sentences after offset *start* on *line*, then the reply's later lines.

        HARDEN-22 B and HARDEN-25 B walk the same two places a directive can stand after an
        approval phrase: a stop on the approval's own line (`.`, `!`, `?`, `;`) and a line
        of its own.
        """
        out: list[str] = []
        tail = line[start:]
        for sp in _EN_AFTER_STOP.finditer(tail.upper()):
            sent = tail[sp.end():]
            stop = _EN_SENTENCE_STOP.search(sent)
            if stop:
                sent = sent[:stop.end()]
            if sent.strip():
                out.append(sent)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        later = lines[lines.index(line.strip()) + 1:] if line.strip() in lines else []
        out.extend(later)
        return out

    def _en_no_problem_verdict(line: str, upper: str) -> "tuple[bool, str] | None":
        """The verdict of a "No <problem>" reply, or ``None`` when *upper* is not one.

        "No contradictions found", "No errors", "No inconsistencies" (HARDEN-10 C), "No major
        inconsistencies" (HARDEN-14 A), the NOTHING / NOT denials of HARDEN-22 A ("Nothing is
        wrong", "Nothing else to add", "Not seeing any issues") and the casual "No issues" /
        "No, it's good" are the way the creative validator says there is nothing to fix,
        matched the way the Russian «нет противоречий» already was, and before the standalone
        ``NO`` token, which took the first problem word away as the reason. Each is an approval
        only when it is the whole verdict: "No contradictions found, but Anna's age changes…",
        "No errors except one: …" and "No issues." followed by a `REVISE:` or `REJECT:` line
        reject, with the reply's own words as the reason.
        """
        # HARDEN-25 A is matched before the prose guard below: "nothing contradicts the
        # outline" is on `_EN_NOTHING_OK`'s side too, and there it stays prose — it is parsed
        # here instead, so the approval carries no `unparseable` flag.
        m = _EN_NOTHING_CONTRADICTION.match(upper)
        if m is None and _explicit_en_approval(upper):
            # The classic NOT-family approvals ("NOTHING TO ADD", "NOTHING WRONG", "NOTE: …")
            # stay prose: they are approved by the fail-open rule, never parsed here.
            return None
        if m is None:
            m = (_EN_NO_PROBLEM.match(upper) or _EN_NO_PROBLEM_ADJ.match(upper)
                 or _EN_NOTHING_DENIAL.match(upper) or _EN_DISCOURSE_OK.match(upper))
        if not m:
            return None
        rest = upper[m.end():]
        if _EN_CLAUSE_CAVEAT.search(rest):
            return False, line.strip()
        # HARDEN-25 A: "Nothing here contradicts the outline but the ending fails." — the contrast
        # stands after the object of the verb, not after a stop, so the clause caveat above cannot
        # see it; a denial that goes on with a contrast is the complaint (HARDEN-22 A).
        if _EN_NOTHING_CONTRADICTION.match(upper) is not None and _EN_CONTRAST_ANYWHERE.search(rest):
            return False, line.strip()
        # "No issues with tone; one problem: …": no contrast word separates the approval
        # from the complaint, but the second clause names a problem and that is the complaint.
        if _names_a_problem(rest):
            return False, line.strip()
        # HARDEN-22 B: a directive that opens a sentence on the approval's own line —
        # "No contradictions. REVISE: fix the timeline." — not only on a later one.
        for sent in _sentences_after(line.strip(), m.end(), text):
            reason = _directive_reason(sent)
            if reason is not None:
                return False, reason
        return True, ""

    # ── HARDEN-14 A: the problem side of the NOT family — the approvals ──────
    # A "NOT" that negates a *problem* says there is no problem: "NOT A PROBLEM",
    # "NOT A CONTRADICTION", "NOT REJECTED", "NOTHING WRONG". These are the exception
    # list now, so a *new* rejection phrase needs no new code — the default for a
    # NOT/NOTHING reply is the rejection (HARDEN-14 A). Deliberately *not* here:
    # "Not ready", "Not coherent", "Not believable", "Not a match", "Not enough",
    # "Not suitable" and "Not plausible" are live rejections — a reviewer's note
    # about fit, not a problem named and denied.
    _EN_NOT_PROBLEM_WORDS = frozenset((
        "PROBLEM", "PROBLEMS", "PROBLEMATIC", "AMISS", "ISSUE", "ISSUES", "CONTRADICTION", "CONTRADICTIONS",
        "CONTRADICTORY", "CONFLICT", "CONFLICTS", "ERROR", "ERRORS", "FLAW", "FLAWS",
        "INCONSISTENCY", "INCONSISTENCIES", "INCONSISTENT", "VIOLATION", "VIOLATIONS",
        "OBJECTION", "OBJECTIONS", "DEFECT", "DEFECTS", "AMBIGUITY", "AMBIGUITIES",
        "AMBIGUOUS", "OMISSION", "OMISSIONS", "MISTAKE", "MISTAKES", "BUG", "BUGS",
        "DISCREPANCY", "DISCREPANCIES", "MISMATCH", "WRONG", "INCORRECT", "REPORT",
        "REPORTED",
        # the rejection verdicts themselves: "NOT REJECTED", "NOT REVISE", "NOT DISAPPROVED"
        # the failure side of the same class: "NOT FAILED" denies the failure the way "NOT REJECTED" does
        "FAIL", "FAILS", "FAILED", "FAILING", "FAILURE", "FAILURES",
        "REJECTED", "REJECTION", "REJECTIONS", "REJECT", "REVISE", "REVISION",
        "REVISIONS", "DISAPPROVED", "DISAPPROVE", "DECLINED", "REFUSED", "DENIED",
    ))
    #: "NOT IN CONFLICT (WITH …)" names a conflict and denies it: an approval, matched on its
    #: own because the word after "NOT" is the preposition, not the problem.
    _EN_NOT_IN_CONFLICT = _re.compile(r"^NOT\s+IN\s+(?:CONFLICT|CONTRADICTION|VIOLATION|DISAGREEMENT)\b")
    #: A "NO" with adjectives between it and the problem word: "No major inconsistencies"
    #: is how the creative validator says there is nothing to fix, and every word between
    #: "No" and the problem word is an adjective, not the problem (HARDEN-14 A).
    _EN_NO_ADJECTIVES = (
        r"(?:MAJOR|MINOR|SIGNIFICANT|INSIGNIFICANT|SERIOUS|SEVERE|NOTICEABLE|NOTED|"
        r"OBSERVED|APPARENT|CLEAR|REAL|ACTUAL|SUCH|OTHER|ELSE|FURTHER|ADDITIONAL|ANY|"
        r"MORE|MOST|NEW|REMAINING|OUTSTANDING|OBVIOUS|GLARING|MATERIAL|NOTABLE|"
        r"FACTUAL|CONTINUITY|PLOT|TIMELINE)\s+"
    )
    _EN_NO_PROBLEM_ADJ = _re.compile(
        r"^NO\b\s+(?:" + _EN_NO_ADJECTIVES + r")*"
        r"(?:" + _EN_PROBLEM_NOUNS + r")\b"
        r"(?:\s+(?:FOUND|REMAIN(?:ING)?|LEFT|DETECTED))?"
    )

    #: The verbs the `NEEDS` / `TO BE` shapes admit that the bare `TO` shape of
    #: `_EN_NOTHING_OK` does not: "nothing to revise" is a denial, but "nothing revised"
    #: would read the other way, so these follow a connector only (HARDEN-22 A).
    _EN_NOTHING_ACTIONS_VERB = (
        r"REVISE(?:D|S|ING)?|REVISION(?:S)?|REWRITE(?:S|N|ING)?|REWRITING|MENTION(?:S|ED|ING)?|AMEND(?:S|ED|ING)?|"
        r"RAISE(?:D|S|ING)?|EDIT(?:S|ED|ING)?|WORK(?:S|ED|ING)?"
    )
    #: The participles a "NOTHING" denial can end on: "nothing major was found", "nothing
    #: serious detected". They stay out of `_EN_NOT_PROBLEM_WORDS` on purpose — "NOT FOUND"
    #: is a missing thing, not a denied problem.
    _EN_NOT_FOUND_PARTICIPLES = (
        r"FOUND|DETECTED|NOTED|NOTICED|RAISED|OBSERVED|SPOTTED|IDENTIFIED|LISTED|RECORDED|MISSING|LACKING|OFF"
    )
    #: A problem stated as a phrase rather than a word: "nothing is out of character",
    #: "nothing is out of line" — the denial is of the phrase, not of a single word.
    _EN_PROBLEM_PHRASES = r"OUT\s+OF\s+(?:CHARACTER|LINE|CONTEXT|PLACE|PLACEMENT)"

    #: HARDEN-25 A: the place a "NOTHING" denial locates the absence of conflict — "nothing
    #: here contradicts the outline", "nothing in the draft conflicts with the bible",
    #: "nothing anywhere breaks it". HARDEN-22 A knew adjectives, auxiliaries and gap words
    #: between "NOTHING" and the finding, but not a place, so a place phrase was read as the
    #: verdict word and the whole approval went back to the Coder as the thing to fix. The
    #: words stop at a stop of their own, so a contrast inside the place phrase
    #: ("nothing in the plan, but the date breaks it") is never swallowed by the denial.
    _EN_NOTHING_PLACE_WORD = r"[^\s.,;:!?()\[\]\"\']{1,24}"
    _EN_NOTHING_PLACE = (
        r"(?:HERE|THERE|ANYWHERE|EVERYWHERE)\s+|"
        r"IN\s+(?:IT|THEM|HERE|THERE)\s+|"
        r"IN\s+(?:THE|THIS|THAT|A|AN|MY|YOUR|SOME|ANY)\s+"
        r"(?:" + _EN_NOTHING_PLACE_WORD + r"\s+){0,2}" + _EN_NOTHING_PLACE_WORD + r"\s+"
    )
    #: The verbs of conflict a denial can deny. They are verbs, not the problem words of
    #: `_EN_NOT_PROBLEM_WORDS`, so "nothing here fits the established characters" stays the
    #: HARDEN-14 A rejection it is: only a verb of conflict is denied here.
    _EN_NOTHING_CONFLICT_VERBS = (
        r"CONTRADICT(?:S|ED|ING)?|CONFLICT(?:S|ED|ING)?\s+WITH|"
        r"BREAKS?|BROKE|BREAKING|VIOLAT(?:ES|ED|ING)|"
        r"DISAGREE(?:S|D|ING)?\s+WITH|CLASH(?:ES|ED|ING)?\s+WITH|UNDERMIN(?:ES|ED|ING)"
    )
    #: The nouns "NO" can deny over a verb of conflict: "no part contradicts the outline".
    _EN_NOTHING_DENY_NOUNS = (
        r"PART|SECTION|ITEM|LINE|SENTENCE|WORD|DETAIL|ELEMENT|ASPECT|FACTOR|"
        r"POINT|PLACE|NAME|DATE|AGE|ORDER|CHAPTER|STYLE|VOICE|PLOT|TIMELINE"
    )

    #: The denial an English validator writes when there is nothing to fix, in the shapes
    #: HARDEN-14 A's problem-word rule did not cover. An adjective stands between
    #: "NOTHING" and the finding ("Nothing major was found"), the verb is an auxiliary
    #: ("Nothing needs fixing", "Nothing to be corrected"), a copula links them
    #: ("Nothing is wrong", "Nothing seems inconsistent"), "ELSE" stands in for the problem
    #: ("Nothing else to add"), and "NOT SEEING ANY <problem>" denies the problem without
    #: the word "NOTHING". One decision for the whole family: the adjectives are
    #: `_EN_NO_ADJECTIVES`, the problem words `_EN_NOT_PROBLEM_WORDS`, the actions
    #: `_EN_NOTHING_ACTIONS` — no second word list beside them (HARDEN-22 A).
    _EN_NOTHING_DENIAL = _re.compile(
        r"(?:"
        r"(?:(?:NO\s*[,.:;]?\s+)?)?NOTHING\b\s*(?:"
        r"(?:" + _EN_NO_ADJECTIVES + r")*"
        r"(?:TO\s+(?:BE\s+)?|NEEDS?\s+(?:TO\s+(?:BE\s+)?|AN?\s+|ANY\s+)?|REQUIRES?\s+|REMAINS?\s+TO\s+(?:BE\s+)?)"
        r"(?:" + _EN_NOTHING_ACTIONS + r"|" + _EN_NOTHING_ACTIONS_VERB + r")\b"
        r"|"
        r"(?:" + _EN_NO_ADJECTIVES + r")*"
        r"(?:WAS\s+|WERE\s+|IS\s+|ARE\s+|SEEMS?\s+|APPEARS?\s+|LOOKS?\s+|FEELS?\s+|HOLDS?\s+)?"
        r"(?:(?:AT\s+ALL|COMPLETELY|ENTIRELY|FULLY|QUITE|VERY|TOO|MUCH|PARTIALLY|ESPECIALLY|JUST|ONLY)\s+)?"
        r"(?:" + "|".join(sorted(_EN_NOT_PROBLEM_WORDS)) + r"|"
        + _EN_NOT_FOUND_PARTICIPLES + r"|" + _EN_PROBLEM_PHRASES + r")\b"
        r")"
        r"|"
        r"(?:(?:NO\s*[,.:;]?\s+)?)?NOT\s+"
        r"(?:FIND|FINDS|FINDING|SEE|SEES|SEEN|SEEING|SEEM|SEEMS|SEEMED|NOTICE|NOTICES|NOTICED|NOTICING|"
        r"APPEAR|APPEARS|APPEARED|LOOK|LOOKS|LOOKED)\s+"
        r"(?:(?:ANYTHING|ANY|A|AN|THE|SUCH|ANY\s+OTHER)\s+)?"
        r"(?:" + "|".join(sorted(_EN_NOT_PROBLEM_WORDS)) + r"|"
        + _EN_NOT_FOUND_PARTICIPLES + r")\b"
        r")"
    )

    #: The denial HARDEN-22 A's shapes could not hold: "nothing here contradicts the outline",
    #: "nothing in the draft conflicts with the bible", "nothing anywhere breaks it", "no part
    #: contradicts the outline". It is a denial of a verb of conflict, matched on its own
    #: (HARDEN-25 A) because "nothing contradicts" is on `_EN_NOTHING_OK`'s side too, and
    #: there it stays prose — parsed here the approval carries no `unparseable` flag.
    _EN_NOTHING_CONTRADICTION = _re.compile(
        r"(?:(?:NO\s*[,.:;]?\s+)?)?"
        r"(?:NOTHING\b\s*(?:" + _EN_NOTHING_PLACE + r")?\s*|"
        r"NO\s+(?:" + _EN_NOTHING_DENY_NOUNS + r")\b\s*(?:" + _EN_NOTHING_PLACE + r")?\s*)"
        r"(?:" + _EN_NOTHING_CONFLICT_VERBS + r")\b"
    )

    #: The start of a sentence after an approval phrase: a stop on the same line, a
    #: semicolon, or a new line. HARDEN-22 B — "No contradictions. REVISE: fix the
    #: timeline." put the order on the approval's own line, so a later-line scan never saw
    #: it, and "No issues; REVISE: fix X." was approved the same way.
    _EN_AFTER_STOP = _re.compile(r"(?:^|[.!?;]\s+|\n)")
    _EN_SENTENCE_STOP = _re.compile(r"(?<![.!?])[.!?]+(?![.!?])(?=\s+\S)")  # a run is tried from its start only: linear on "…….…"

    #: The words a creative validator opens an approval with. HARDEN-17 A: the parser
    #: returned on `APPROVED` and `OK` and failed open for everything else, so "Looks good,
    #: but the ring is stolen" was approved on its first clause alone and the objection
    #: after the `but` was never read. The single-word forms come from
    #: `_EN_NOT_NEGATES_APPROVAL`, the approval side HARDEN-5 A already knew, so there is no
    #: second list of praise words beside it.
    _EN_APPROVAL_HEAD = _re.compile(
        r"^(?:LOOKS\s+(?:GOOD|FINE|OK|OKAY|GREAT|PERFECT|EXCELLENT|NICE|FANTASTIC|WONDERFUL|ACCEPTABLE|RIGHT|CORRECT|CONSISTENT)|GOOD\s+TO\s+GO|"
        r"ALL\s+(?:GOOD|FINE|RIGHT|OK|CLEAR)|THINGS?\s+(?:ARE|LOOK)\s+(?:GOOD|FINE|OK)|"
        r"LGTM|LGTVM|NO\s+(?:CHANGES?|ISSUES?|COMMENTS?)\s+NEEDED|PASS(?:ED)?|YES"
        r"|" + "|".join(sorted(_EN_NOT_NEGATES_APPROVAL)) + r")\b"
    )

    def _en_approval_caveat_reason(line: str, upper: str) -> "str | None":
        """The objection when *upper* praises and then turns against itself.

        HARDEN-17 A: "Looks good, but the ring is stolen" was approved on the first clause
        alone. The part after the conjunction decides, and the objection is the reason —
        handing the Coder the praise would send it to fix the good parts. A reply whose
        objection cannot be told from its praise is a not-approved with its own words kept,
        never an approval on the praise.
        """
        m = _EN_APPROVAL_HEAD.match(upper)
        if not m:
            return None
        c = _EN_CLAUSE_CAVEAT.search(upper[m.end():])
        if not c:
            return None
        s = line.strip()
        obj = s[m.end() + c.end():].lstrip(" \t:;,.!?–—-").strip()
        return obj if obj else s

    #: The answer a question-form approval head is not approved by (HARDEN-26 B). `NOT` only
    #: joins `REALLY` — "Not approved" is the rejection of `_EN_NOT_APPROVED` and stays where it
    #: is, so an approval that merely takes a negative form keeps the read of the NOT family.
    _EN_Q_NO_ANSWER = _re.compile(
        r"^\s*[?]+\s*(?:NOPE|NOT\s+REALLY|NO)\b"
        r"[,.\s:;!–—-]*(.*)$"
    )

    def _en_approval_question_rejection(line: str, upper: str) -> "str | None":
        """The reason when an approval head is a question answered in the negative.

        HARDEN-26 B: "Approved? No — the test is wrong." and "OK? Not really, the date is
        wrong." were read on their first word — the head approved and the answer that followed
        it was never looked at, so an unparseable `False` was never even logged. The answer to
        the question is the verdict, and the clause after it is the reason; "Approved? Yes." and
        "OK? Sure." keep the head's approval.

        CC review fixes 47: an answer that is the "No <problem>" approval on its own — "Approved?
        No issues.", "OK? No problem.", "Approved? No changes needed." — answers with that
        approval, not with a no: read alone, "No issues." approves (HARDEN-10 C), so after the
        question it is no rejection either, and the Coder is never told to fix "issues.". Only a
        `No` the next word follows with a blank is such an answer: "No, …", "No — …" and "No."
        stay the rejection they open, whatever comes after them.
        """
        m = _EN_APPROVAL_HEAD.match(upper)
        if m is None:
            return None
        a = _EN_Q_NO_ANSWER.match(upper[m.end():])
        if a is None:
            return None
        s = line.strip()
        answer_upper = upper[m.end():].lstrip().lstrip("?").lstrip()
        if _re.match(r"NO\s+\w", answer_upper):
            answer = s[m.end():].lstrip().lstrip("?").lstrip()
            if _EN_NOTHING_NEEDED.match(answer_upper):
                return None
            verdict = _en_no_problem_verdict(answer, answer_upper)
            if verdict is not None and verdict[0]:
                return None
        rest = s[m.end() + a.start(1):].strip()
        return rest if rest else line.strip()

    def _en_not_family_reason(line: str, upper: str) -> "str | None":
        """The reason when *upper* opens a NOT-family rejection, else ``None``.

        HARDEN-14 A: a reply whose first word is ``NOT`` or ``NOTHING`` is a
        **rejection by default**. HARDEN-5 A / HARDEN-10 C had it the other way
        round — the family rejected only when the word after the fillers
        negated something on the approval side, so "NOT READY", "Not coherent",
        "Nothing makes sense", "Not in character for Ana", "Nothing matches the
        bible" (41 live rejections, three of three through
        ``ContinuityValidator.check``) matched nothing, were unparseable, and
        the fail-open rule **approved the chapter** the model had thrown out.
        The continuity, fact and theme validators have no re-ask, so that
        approval was final.

        The approval side is now the exception list: the problem-side shapes
        (``_EN_NOT_PROBLEM_WORDS``) plus the explicit approvals already matched
        before this function (``NOT BAD``, ``NOTHING TO ADD``, ``NOTE:``). The
        reason is still the whole reply — the words after ``NOT`` alone would
        read as an approval ("NOT APPROVED" → "APPROVED"), which is exactly what
        the Coder would be told to fix.
        """
        m0 = _EN_NOT_BAD.match(upper) or _EN_NOTHING_OK.match(upper) or _EN_NOTE.match(upper)
        if m0 is not None:
            tail = upper[m0.end():]
            # "Nothing wrong, but the ring is stolen": the contrast is the verdict, not the
            # denial. These are approved by the fail-open rule, so without this the denial
            # that turned into a complaint would ship the chapter (HARDEN-22 B's caveat rule).
            if _EN_CLAUSE_CAVEAT.search(tail) or _names_a_problem(tail):
                return line.strip()
            # "NOT BAD", "NOTHING TO ADD", "NOTE: …" — prose that the fail-open rules
            # approve, never a rejection.
            return None
        if _verdict_token_at_start(upper, "NONSENSE"):
            return line.strip()
        if _verdict_token_at_start(upper, "NOTE"):
            # "NOTE:" opens a note, not a NOT-family rejection (HARDEN-1 A1).
            return None
        m = _re.match(r"^(?:NOT|NOTHING)\b[\s,:;]*(.*)$", upper)
        if not m:
            return None
        if _EN_NOT_IN_CONFLICT.match(upper):
            # "NOT IN CONFLICT (WITH …)": the preposition stands between NOT and the
            # problem word, so the shape is matched on its own.
            return None
        # REL-1: "Nothing to worry about." / "Nothing jumps out as a problem." deny any
        # problem the way "Nothing wrong" does; a contrast after them is still the complaint.
        m_calm = _EN_NOTHING_CALM.match(upper)
        if m_calm is not None:
            tail = upper[m_calm.end():]
            if _EN_CLAUSE_CAVEAT.search(tail) or _names_a_problem(tail):
                return line.strip()
            return None
        words = m.group(1).split()
        # "NOT A PROBLEM", "NOTHING COMPLETELY WRONG" — the article or the qualifier is not
        # the verdict word, so step past them to the word "NOT" really negates.
        # REL-1: "AT ALL" is two words, so it was never stepped past ("Not at all problematic").
        while words and (words[0] in _EN_NOT_FILLERS or words[:2] == ["AT", "ALL"]):
            words = words[2:] if words[:2] == ["AT", "ALL"] else words[1:]
        head = words[0] if words else ""
        if head.rstrip(".,;:") in _EN_NOT_PROBLEM_WORDS:
            # A negation of a problem: "nothing to fix", never a rejection.
            return None
        # A recognised approval-side word ("NOT APPROVED") rejects, and so does
        # every unknown one ("NOT READY", "Not coherent", "Nothing makes sense").
        return line.strip()

    #: The words a reply opens a rejection with (HARDEN-17 A): one list, shared with the
    #: NOT/NOTHING family in `_en_rejection_reason` below, so a reply is decided in one
    #: place. "REJECTED" is a word of its own — `_verdict_token_at_start(upper, "REJECT")`
    #: refused it as the tail of REJECT, and so did "REJECTED - …" and "Rejected" alone —
    #: so a reply that began with any of these matched nothing, was unparseable, and the
    #: fail-open rule approved a chapter the validator had thrown out.
    #: One word list, read by both heads below (HARDEN-22 B).
    _EN_REJECT_HEAD_WORDS = (
        r"REJECT(?:ED)?"
        r"|REVISE"
        r"|INCORRECT"
        r"|NEED(?:S|ING)?\s+(?:A\s+|ANOTHER\s+|ANY\s+|MORE\s+|SOME\s+|FURTHER\s+|TO\s+BE\s+)?(?:REVISIONS?|REWRITE|REWRITTEN|WORK|CHANGES?|FIX(?:ES|ING)?|EDITS?)"
        r"|REWRITE(?:N)?|REWROTE|FAIL(?:ED|S|ING|URE)?|DISAPPROVED|UNACCEPTABLE"
        # REL-4 B/E: the noun-first form and DENIED — "REVISION NEEDED.", "Changes required:
        # …", "Denied — plot hole." matched nothing and passed on fail-open.
        r"|(?:REVISIONS?|REWRITE|CHANGES?|FIX(?:ES)?|EDITS?)\s+(?:IS\s+|ARE\s+)?(?:NEEDED|REQUIRED|NECESSARY)"
        r"|DENIED"
    )
    _EN_REJECT_HEAD = _re.compile(
        r"^(?:" + _EN_REJECT_HEAD_WORDS + r")\b(?:[\s]*[:;,.!?\-–—]*)?"
    )

    #: HARDEN-25 B: the same heads plus FIX and CHANGE. A bare FIX / CHANGE is deliberately
    #: *not* a first-line verdict token — "FIXED" sits on the approval side of
    #: `_EN_NOT_NEGATES_APPROVAL`, and a reply of "Fixed." is praise — but after an approval
    #: phrase it is the instruction the validator just gave: "LGTM. Fix the date."
    _EN_DIRECTIVE_HEAD = _re.compile(
        r"^(?:" + _EN_REJECT_HEAD_WORDS + r"|FIX(?:ES|ED|ING)?|CHANGE(?:S|D|ING|MENT)?)\b"
        r"(?:[\s]*[:;,.!?\-–—]*)?"
    )

    #: A directive whose object denies — "reject nothing.", "revise none.", "fix no issues." —
    #: is the withdrawal of the directive, so it carries no verdict (HARDEN-25 B).
    _EN_DIRECTIVE_DENIAL_OBJECT = _re.compile(r"^(?:NOTHING|NONE|NO\s+\S+)\b")

    #: REL-4 C/D/E: the reviewed thing named as rejected in the middle of a sentence —
    #: "The chapter is REJECTED", "Changes are REJECTED", "Request DENIED". The subject is
    #: a closed list of what a validator reviews, so a story sentence ("the king's offer is
    #: rejected") is not read as the verdict; a NOT / NEVER / NO before the word denies it.
    _EN_BURIED_REJECTED = _re.compile(
        r"\b(?:CHAPTER|SCENE|DRAFT|TEXT|SUBMISSION|REQUEST|CHANGES?|EDITS?|PATCH|DIFF|REVISION"
        r"|VERSION|ANSWER|VERDICT|STORY|THIS|IT)\b[^\S\n]+"
        r"(?:(?:IS|ARE|WAS|WERE|HAS\s+BEEN|HAVE\s+BEEN)[^\S\n]+)?(?:HEREBY[^\S\n]+)?"
        r"(?P<word>REJECTED|DENIED|DISAPPROVED)\b"
    )
    _EN_SENTENCE_SPLIT = _re.compile(r"(?<=[.!?])[^\S\n]+|\n+")

    def _en_buried_rejection(text: str) -> "str | None":
        """The reason when a reply with no verdict at a line head rejects mid-text (REL-4).

        Called on the fail-open path only. A sentence after the first that opens with a
        rejection head ("…consistent. Needs revision for the date.") rejects, unless its
        object denies it ("Reject nothing."); so does a reviewed thing named as rejected or
        denied anywhere in the reply.
        """
        stripped = text.strip()
        sentences = [x for x in _EN_SENTENCE_SPLIT.split(stripped) if x.strip()]
        for sent in sentences[1:]:
            up = _re.sub(r"^PLEASE\s+", "", sent.strip().upper())
            hm = _EN_REJECT_HEAD.match(up)
            if hm is None or _EN_DIRECTIVE_DENIAL_OBJECT.match(up[hm.end():].strip()):
                continue
            rest = sent.strip()[hm.end():].lstrip(" \t:;,.!?–—-").strip()
            return rest if rest else sent.strip()
        upper = stripped.upper()
        for m in _EN_BURIED_REJECTED.finditer(upper):
            if _re.search(r"\b(?:NOT|NEVER|NO|N'T)\b[^\S\n]*$", upper[max(0, m.start("word") - 40):m.start("word")]):
                continue
            rest = stripped[m.end():].lstrip(" \t:;,.!?–—-").strip()
            return rest if rest else stripped
        return None

    #: HARDEN-25 C: the words that name the problem a "Nope," clause opens with. The problem
    #: nouns and phrases are the shared lists of HARDEN-22 A, checked through
    #: `_names_a_problem` so a negation is not read as a complaint; these are the adjectives a
    #: validator writes instead of a noun — "Nope, the date is wrong.", "Nope, it breaks it."
    _EN_PROBLEM_ADJECTIVES = (
        r"WRONG(?:LY)?|BROKEN|BREAKS?|BROKE|BREAKING|FAIL(?:S|ED|ING|URE)?|"
        r"MISS(?:ING|ES|ED|ING)|INCORRECT(?:LY)?|BAD|" + _EN_PROBLEM_PHRASES
    )
    #: Also what a "Nope," clause says without a noun: the conflict verbs of the denial ("Nope, this
    #: contradicts the outline."), an error, and the plain "not right / not correct".
    _EN_NOPE_PROBLEM = _re.compile(
        r"\b(?:" + _EN_PROBLEM_ADJECTIVES + r"|ERRORS?|MISTAKES?|FLAWED|"
        + _EN_NOTHING_CONFLICT_VERBS + r"|NOT\s+(?:RIGHT|CORRECT|VALID|ACCURATE|TRUE))\b")

    def _nope_names_a_problem(clause_upper: str) -> bool:
        """Whether a "Nope," clause names a problem that is not itself denied: "Nope, the date
        is wrong." does, "Nope, not bad." and "Nope, no problem at all." do not."""
        for hit in _EN_NOPE_PROBLEM.finditer(clause_upper):
            if hit.group(0).startswith("NOT "):
                return True
            before = clause_upper[:hit.start()].split()[-2:]
            if not any(w in _EN_NEGATED_BY for w in before):
                return True
        return False
    _EN_NOPE_CLAUSE = _re.compile(r"^\s*NOPE\b(?:[,.:;!?]|\s*[—–-]\s*)?\s*(.+)$")

    #: HARDEN-25 A: a subject that affirms, over the same verbs of conflict the denial above
    #: denies — "it contradicts the outline.", "this breaks the timeline." The denial needs
    #: `NOTHING` / `NO <noun>` (HARDEN-25 A), so anything else on these verbs is the complaint.
    _EN_CONFLICT_HEAD = _re.compile(
        r"^(?:IT|THIS|THAT|SHE|HE|THEY|"
        r"(?:THE|A|AN|MY|YOUR|ITS|HIS|HER|THEIR)\s+\S+|"
        r"EVERYTHING|ALL)\s+(?:" + _EN_NOTHING_CONFLICT_VERBS + r")\b"
    )

    def _en_rejection_reason(line: str, upper: str) -> "str | None":
        """The reason when *upper* opens an English rejection, else ``None``.

        Both the first-line pass and the per-line pass go through this one
        helper, so the whole-word boundary (HARDEN-1 A1), the rejection words of
        HARDEN-17 A and the NOT-family approval guard cannot drift apart again.

        ``REVISE`` / ``REJECT`` / ``NO`` are standalone tokens, so the reason is
        what follows them. The NOT family rejects by *negating* the clause that
        follows, so the reason is the whole reply — the words after ``NOT`` alone
        would read as an approval ("NOT APPROVED" → "APPROVED") and that is
        exactly what the Coder is told to fix.
        """
        m = _EN_REJECT_HEAD.match(upper)
        if m:
            # Strip the separators that stand between the verdict word and the reason
            # ("REJECTED - …", "NEEDS REVISION: …") and keep everything else, trailing
            # punctuation included: the reply's own words are the reason.
            rest = line.strip()[m.end():].lstrip(" \t:;,.!?–—-").strip()
            return rest if rest else "validator rejected (no reason given)"
        for token in ("REVISE", "REJECT", "NO"):
            if _verdict_token_at_start(upper, token):
                rest = line.strip()[len(token):].lstrip(":;, ").strip()
                return rest if rest else "validator rejected (no reason given)"
        # HARDEN-22 C: a reply that is the word "Nope" alone rejects, in any case; it has no
        # verdict token, so it used to fall open to an approval. "Nope, works" (HARDEN-10 C) is
        # not that reply: with words after it the "nope" may answer "any problems?", so it stays
        # prose, and so does any word that merely begins with NO (HARDEN-1 A1).
        if _normalise(line) == "nope":
            return line.strip()
        # HARDEN-25 C: a "Nope," that goes on to name the problem is the rejection it opens —
        # before it was prose and the fail-open rule approved the chapter the validator had
        # thrown out. "Nope, works" and "Nope, looks fine" name no problem, so they keep the
        # HARDEN-10 C reading and stay prose.
        m_nope = _EN_NOPE_CLAUSE.match(upper)
        if m_nope is not None:
            # the clause is read back from *line*, so the reason keeps the reply's own case
            clause = line[m_nope.start(1):].strip()
            if clause and (_names_a_problem(clause.upper()) or _nope_names_a_problem(clause.upper())):
                return clause
        # HARDEN-25 A: the denial needs the denying subject. The same sentence with a subject
        # that affirms — "It contradicts the outline." — is the complaint, not its denial.
        m_conflict = _EN_CONFLICT_HEAD.match(upper)
        if m_conflict is not None:
            return line.strip()
        return _en_not_family_reason(line, upper)

    def _en_approval_directive_verdict(line: str, upper: str) -> "tuple[bool, str] | None":
        """The verdict of a praise head followed by a directive sentence, else ``None``.

        HARDEN-25 B: the directive scan of HARDEN-22 B ran only on the "No <problem>"
        approvals, so a directive after a praise head was prose. "Looks good. Fix the date."
        was passed on fail-open as an approval, and "LGTM. Fix nothing." /
        "Looks good. Revise nothing." approved too, with the fail-open note as their reason
        instead of the empty reason a parsed approval carries. Only a sentence that opens
        with a directive head is read: the rest of the reply stays whatever it was before.
        """
        m = _EN_APPROVAL_HEAD.match(upper)
        if m is None:
            return None
        denied = False
        for sent in _sentences_after(line.strip(), m.end(), text):
            reason = _directive_reason(sent)
            if reason is not None:
                return False, reason
            up = _re.sub(r"^PLEASE\s+", "", sent.strip().upper())
            if _EN_DIRECTIVE_HEAD.match(up) is not None or _EN_NOT_APPROVED.match(up) is not None:
                denied = True
        return (True, "") if denied else None

    #: REP-VERDICT A: what can follow the APPROVED / OK token and take the approval back.
    _EN_APPROVED_TOKEN = _re.compile(r"^(?:APPROVED|OK\b)")
    _EN_APPROVED_NEGATED = _re.compile(r"^[^\S\n]*(?:[:=\-–—][^\S\n]*)?(NO|NOT|NOPE|FALSE|N)\b")
    _EN_APPROVED_CONDITION = _re.compile(
        r"^[^\S\n]*(?:[,;:.—–-][^\S\n]*)?(?:IF|ONCE|PROVIDED|UNLESS|PENDING|CONDITIONAL(?:LY)?|"
        r"ON\s+CONDITION)\b")
    _EN_TAKE_BACK = _re.compile(r"^(?:ACTUALLY|ON\s+SECOND\s+THOUGHT|WAIT)\b[\s,:;—–-]*(?:NO|NOT)\b")

    def _en_approved_token_verdict(line: str, upper: str) -> "tuple[bool, str] | None":
        """The verdict of a reply that opens with ``APPROVED`` / ``OK``, else ``None``.

        REP-VERDICT A: the token approved at once and nothing after it was read, so
        "Approved: No", "approved = false", "APPROVED if the date is fixed",
        "APPROVED. Actually no, …" and an APPROVED line followed by a ``REVISE: …`` line
        (VD-3) were clean approvals with no re-ask, and the revision request was lost. A
        negation right after the token, a condition, or a later sentence or line that opens
        with a rejection directive takes the approval back, with the reply's words as the
        reason. "Approved - no issues found", "APPROVED: not a problem", "APPROVED. Save the
        file." and "APPROVED. Fix nothing." keep the approval.
        """
        m = _EN_APPROVED_TOKEN.match(upper)
        if m is None:
            return None
        s = line.strip()
        rest_upper = upper[m.end():]
        neg = _EN_APPROVED_NEGATED.match(rest_upper)
        if neg is not None:
            word = neg.group(1)
            r_up = rest_upper[neg.start(1):].strip()
            r_line = s[m.end() + neg.start(1):].strip()
            after = r_line[len(word):].lstrip(" \t:;,.!?–—-").strip()
            reject = False
            if word in ("FALSE", "NOPE", "N"):
                reject = True
            elif word == "NO":
                if _re.match(r"NO\s+\w", r_up):
                    # "Approved - no issues found": the No <problem> approval, unless it turns.
                    if not _EN_NOTHING_NEEDED.match(r_up):
                        v = _en_no_problem_verdict(r_line, r_up)
                        if v is not None and not v[0]:
                            return False, v[1]
                else:
                    reject = True
            elif _en_not_family_reason(r_line, r_up) is not None:
                reject = True
            if reject:
                return False, after or s
        cond = _EN_APPROVED_CONDITION.match(rest_upper)
        if cond is not None:
            return False, s[m.end():].lstrip(" \t:;,.!?–—-").strip() or s
        for sent in _sentences_after(s, m.end(), text):
            reason = _directive_reason(sent)
            if reason is not None:
                return False, reason
            if _EN_TAKE_BACK.match(sent.strip().upper()):
                return False, sent.strip()
        return True, ""

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

        # An approval that turns on a contrast ("Looks good, but …") is not an approval at
        # all, and it must be checked before the approval tokens and the fail-open rule
        # (HARDEN-17 A).
        _approval_caveat = _en_approval_caveat_reason(raw, upper)
        if _approval_caveat is not None:
            return False, _approval_caveat, False

        # A caveat right after OK is checked before the plain OK approval (BUGFIX).
        _ok_caveat = _ok_caveat_reason(raw)
        if _ok_caveat is not None:
            return False, _ok_caveat, False

        # An approval head asked as a question and answered in the negative is the answer's
        # rejection, never the head's approval (HARDEN-26 B) — it must come before the token
        # check that approves on `APPROVED` and `OK`.
        _q_rejection = _en_approval_question_rejection(raw, upper)
        if _q_rejection is not None:
            return False, _q_rejection, False

        # ── English APPROVED ──────────────────────────────────────────────
        _approved = _en_approved_token_verdict(raw, upper)
        if _approved is not None:
            return _approved[0], _approved[1], False

        # A casual "No, it's perfect" is an approval (`_en_no_problem_verdict`); a "No" that names a real problem still rejects (AUTO-BUG).

        # ── English "NO <revisions/changes/…> NEEDED" approval guard ─────
        if _EN_NOTHING_NEEDED.match(upper):
            return True, "", False

        # "No contradictions found" is an approval, checked before the standalone NO token,
        # which would take the problem word away as the reason (HARDEN-10 C).
        _no_problem = _en_no_problem_verdict(raw, upper)
        if _no_problem is not None:
            return _no_problem[0], _no_problem[1], False

        # HARDEN-25 B: a directive after a praise head is the verdict too — "LGTM. Fix the
        # date." — and "Fix nothing." denies it, so the line stays an approval.
        _head_directive = _en_approval_directive_verdict(raw, upper)
        if _head_directive is not None:
            return _head_directive[0], _head_directive[1], False

        # ── English REVISE / REJECT / NO / NOT ───────────────────────────
        _reject_reason = _en_rejection_reason(raw, upper)
        if _reject_reason is not None:
            return False, _reject_reason, False

        # A verdict on a later line behind a preamble is found too, since the protocol is the first token of a line (AUTO-BUG).
        for _line in raw.splitlines():
            _lu = _line.strip().upper()
            if not _lu:
                continue
            # The same caveat checks, per line, so an approval that turns on a contrast is
            # caught behind a preamble too.
            _line_approval_caveat = _en_approval_caveat_reason(_line, _lu)
            if _line_approval_caveat is not None:
                return False, _line_approval_caveat, False
            _line_ok_caveat = _ok_caveat_reason(_line)
            if _line_ok_caveat is not None:
                return False, _line_ok_caveat, False
            _line_q_rejection = _en_approval_question_rejection(_line, _lu)
            if _line_q_rejection is not None:
                return False, _line_q_rejection, False
            _line_approved = _en_approved_token_verdict(_line, _lu)
            if _line_approved is not None:
                return _line_approved[0], _line_approved[1], False
            if _EN_NOTHING_NEEDED.match(_lu):
                return True, "", False
            _line_no_problem = _en_no_problem_verdict(_line, _lu)
            if _line_no_problem is not None:
                return _line_no_problem[0], _line_no_problem[1], False
            _line_head_directive = _en_approval_directive_verdict(_line, _lu)
            if _line_head_directive is not None:
                return _line_head_directive[0], _line_head_directive[1], False
            _line_reject_reason = _en_rejection_reason(_line, _lu)
            if _line_reject_reason is not None:
                return False, _line_reject_reason, False

        # ── Russian APPROVED (order matters — negated first) ──────────────
        for pat in _RU_APPROVED:
            if pat.search(norm):
                return True, "", False

        # ── Russian REVISE (order matters — negated first) ────────────────
        for pat in _RU_REVISE:
            if pat.search(norm):
                reason = _reason_from(raw.strip(), norm)
                return False, reason, False

        # The line-first pass uses only the first non-empty line.
        if raw == first_line:
            # did not match on first line — try whole text next iteration
            continue
        # whole-text pass also found nothing
        break

    # REL-4 A–E: a rejection in the middle of a one-line reply — "The story is consistent.
    # Needs revision for the date.", "The chapter is REJECTED — wrong character.", "Request
    # DENIED — plot hole." — matched no line head and passed on fail-open. Only the
    # fail-open path reads it, so no reply that was parsed before changes its verdict.
    _buried = _en_buried_rejection(text or "")
    if _buried is not None:
        return False, _buried, False

    # Empty response or no matching token found anywhere
    note = "verdict unparseable — passed on fail-open"
    return True, note, True


def _format_gate2_feedback(parsed: dict, max_hints: int) -> str:
    """Build a structured rejection string from a Gate-2 dict (LOOP-1).

    HARDEN-7 A: ``feedback`` and ``suggested_approach`` are read through
    :func:`tools.llm_stream.reply_text`, so a rejection whose critique the model
    sent as a list or a number still reaches the coder as text instead of
    ``Reason: ['a', 'b']``.
    """
    feedback = reply_text(parsed.get("feedback")) or "no reason given"
    # A hints string is wrapped as one hint, so it is not sliced character by character (BUGFIX).
    raw_hints = parsed.get("hints")
    if isinstance(raw_hints, list):
        # A hint of another type is text, and a null or empty one is no hint: `[null]` printed "1. None" to the coder.
        hints = [h for h in (reply_text(h) for h in raw_hints) if h]
    elif isinstance(raw_hints, str) and raw_hints.strip():
        hints = [raw_hints]
    else:
        hints = []
    hints = hints[:max_hints]
    approach = reply_text(parsed.get("suggested_approach"))

    lines = [f"Reason: {feedback}"]
    if hints:
        lines.append("Hints:")
        for i, h in enumerate(hints, 1):
            lines.append(f"  {i}. {h}")
    if approach:
        lines.append(f"Suggested approach: {approach}")
    return "\n".join(lines)


# Failure detail: the coder gets the tail of the output, where the one-line error sits (RUN-3).

# Head budget, unchanged, for non-pytest commands.
_EXEC_HEAD_BUDGET = 400
# Tail budget for a pytest run; 400 characters are too few for one collection error.
_EXEC_TAIL_BUDGET = 1500

# Pytest exit 5 means no tests were collected; the coder is told so in words (RUN-3).
_NO_TESTS_COLLECTED_MSG = (
    "no tests collected — the -k expression matched nothing "
    "or the file defines no test_* function"
)

# Report sections that hold the error line (ERRORS, FAILURES); the summary is not an anchor, since it comes last (RUN-3).
_PYTEST_BOX_HEADER_RE = re.compile(r"^=+ (?:ERRORS|FAILURES) =+\s*$", re.MULTILINE)
# xdist prints one of these per worker while the pool starts.
_XDIST_BANNER_RE = re.compile(r"^\s*bringing up nodes\.\.\.\s*$")
_TRIM_MARK = "... [trimmed]\n"


def _strip_pytest_noise(text: str) -> str:
    """Drop xdist's ``bringing up nodes...`` lines and runs of blank lines.

    Neither carries information, and stripping them *before* the budget is
    applied means the budget buys traceback lines instead of whitespace.
    """
    out: list[str] = []
    blank = False
    for line in text.splitlines():
        if _XDIST_BANNER_RE.match(line):
            continue
        if line.strip():
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip("\n")


def _pytest_tail(text: str, budget: int = _EXEC_TAIL_BUDGET) -> str:
    """The diagnostic slice of a pytest run's stdout — its tail (RUN-3).

    Strip the noise, then start at the last ``ERRORS`` / ``FAILURES`` header
    so the final box arrives whole (header, frames, ``E`` line, and the short
    summary that follows it); with no box, take the plain tail. Over budget,
    the box is cut from the FRONT — its header line is kept so it still reads
    as a box, the middle is marked as trimmed, and the last lines, where the
    diagnostic sits, always survive.
    """
    cleaned = _strip_pytest_noise(text or "")
    boxes = list(_PYTEST_BOX_HEADER_RE.finditer(cleaned))
    window = cleaned[boxes[-1].start():] if boxes else cleaned
    if len(window) <= budget:
        return window
    header, sep, rest = window.partition("\n")
    if boxes and sep and len(header) + len(sep) + len(_TRIM_MARK) < budget:
        keep = budget - len(header) - len(sep) - len(_TRIM_MARK)
        return header + sep + _TRIM_MARK + _whole_lines(rest[-keep:])
    return _TRIM_MARK + _whole_lines(window[-(budget - len(_TRIM_MARK)):])


def _whole_lines(tail: str) -> str:
    """Drop the partial first line a character-count cut leaves behind."""
    nl = tail.find("\n")
    return tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail


def _build_exec_detail(exec_result) -> str:
    """The detail block of one exec-failure feedback message (RUN-3).

    Stream priority is unchanged — traceback > stderr > stdout, the most
    diagnostic first — and so is the 400-character head for every command
    that is not a test runner (script output is head-interesting). For a
    pytest run the stdout slice is the tail instead, on the larger budget,
    and exit 5 is prefixed with the sentence its empty output is missing.
    """
    tb  = getattr(exec_result, "traceback", "") or ""
    out = getattr(exec_result, "stdout",    "") or ""
    err = getattr(exec_result, "stderr",    "") or ""
    ec  = getattr(exec_result, "exit_code", 1)
    cmd = getattr(exec_result, "command",   "") or ""
    pytest_run = is_pytest_command(cmd)
    if tb:
        detail = f"traceback:\n{tb}"
    elif err:
        detail = f"stderr:\n{err[:_EXEC_HEAD_BUDGET]}"
    elif pytest_run:
        detail = f"stdout:\n{_pytest_tail(out)}"
    else:
        detail = f"stdout:\n{out[:_EXEC_HEAD_BUDGET]}"
    if pytest_run and ec == 5:
        detail = f"{_NO_TESTS_COLLECTED_MSG}\n{detail}"
    return detail


# InnerLoop

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
        existence_validator=None,   # GATES-3: docs-mode file-reference check
        delta_validator=None,       # AUTO-CR-DELTA-1: unchanged-from-HEAD check
        require_tests: bool = False,
        task_mode: str = "code",
        max_task_seconds: int = 0,
        run_goal: str = "",
        collect_bridge=None,
        validator_unavailable_retries: int = _DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES,
        validator_unavailable_wait_sec: float = _DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC,
        coder_transport_retries: int = _DEFAULT_CODER_TRANSPORT_RETRIES,
        coder_transport_wait_sec: float = _DEFAULT_CODER_TRANSPORT_WAIT_SEC,
    ):
        self.coder        = coder
        self.executor     = executor
        self.validator    = validator
        self.max_attempts = max(1, int(max_attempts))
        self._broker      = context_broker or ContextBroker()
        # The collect bridge serves the static block and pull-model lookups (COLLECT-24).
        self._collect_bridge = collect_bridge
        # AUTO-CR-7: optional periodic canon/fact gate (creative mode only).
        self.canon_validator = canon_validator
        # AUTO-CR-20: optional per-task fact-compliance gate (creative mode only).
        self.fact_validator  = fact_validator
        # AUTO-CR-21: optional Russian rhythm/rhyme gate (creative mode only).
        self.prosody_validator = prosody_validator
        # Optional continuity gate against the bible and the previous chapter (creative mode only, AUTO-CR-23-3).
        self.continuity_validator = continuity_validator
        # The theme gate judges what the chapter says against the story guidelines (creative mode only).
        self.theme_validator = theme_validator
        self.existence_validator = existence_validator
        # selfhost pilot: deterministic tests-mandate gate for code mode.
        self.require_tests = bool(require_tests)
        self.task_mode    = str(task_mode)
        # AUTO-CR-21-4: hard wall-clock cap per task; 0 disables the guard.
        self.max_task_seconds = max(0, int(max_task_seconds))
        # The run goal is used by per-task gates when a task has no goal of its own (AUTO-CR-22-1).
        self._run_goal    = str(run_goal or "")
        # Gate-2 retry bounds are checked here too, so a bad value falls back to the default instead of raising (RUN-7).
        try:
            self._validator_unavailable_retries = max(0, int(validator_unavailable_retries))
        except (TypeError, ValueError):
            self._validator_unavailable_retries = _DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES
        try:
            self._validator_unavailable_wait_sec = max(0.0, float(validator_unavailable_wait_sec))
        except (TypeError, ValueError):
            self._validator_unavailable_wait_sec = _DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC
        # A bad value degrades to the default, not an error (RUN-8).
        try:
            self._coder_transport_retries = max(0, int(coder_transport_retries))
        except (TypeError, ValueError):
            self._coder_transport_retries = _DEFAULT_CODER_TRANSPORT_RETRIES
        try:
            self._coder_transport_wait_sec = max(0.0, float(coder_transport_wait_sec))
        except (TypeError, ValueError):
            self._coder_transport_wait_sec = _DEFAULT_CODER_TRANSPORT_WAIT_SEC

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
        # Only a validator with approve_verdict() can report unavailable; older validators keep the two-outcome behaviour (RUN-7).
        _validator_has_verdict = hasattr(self.validator, "approve_verdict")
        # Detect once whether the validator accepts prior_critique, so older validators are not broken (AUTO-CR-30, RUN-7).
        try:
            import inspect as _inspect
            _validator_method = (
                self.validator.approve_verdict if _validator_has_verdict
                else self.validator.approve
            )
            _validator_accepts_prior = (
                "prior_critique"
                in _inspect.signature(_validator_method).parameters
            )
        except (ValueError, TypeError, AttributeError):
            _validator_accepts_prior = False
        records:  list[AttemptRecord] = []
        # Pull-model state (carried across attempts within this round)
        prefetched_context: str = ""
        resolved_context: dict[str, str] = {}   # accumulates every symbol the validator has asked for
        _any_missing: bool = False   # Task 4: True if any attempt had unsatisfied context
        # One revision counter per gate, keyed by GateSpec.name; run_gates updates it in place (GATES-1).
        _gate_revisions: dict[str, int] = {}
        _tests_mandate_rejections: int = 0  # selfhost: code-without-tests rejections
        base_dir_path = Path(base_dir)
        target_files  = task.get("target_files", []) or []
        self._broker.reset_cache()  # clear per-task cache; Pass-2 hits re-accumulate fresh

        # Collect context is computed once per task and re-added after every context reassignment, so it is not lost (COLLECT-24).
        _collect_block = ""
        if self._collect_bridge is not None:
            _collect_block = self._collect_bridge.context_for_many(
                target_files, task_id=task_id) or ""
            if _collect_block:
                prefetched_context = _collect_block + "\n\n"

        def _with_collect_block(formatted: str) -> str:
            """Re-attach the per-task collect block to a rebuilt context string.

            Purely additive: returns *formatted* unchanged when no bridge was
            wired in, the model is absent or stale, or target_files have no
            collect record.
            """
            if not _collect_block:
                return formatted
            if not formatted:
                return _collect_block + "\n\n"
            return _collect_block + "\n\n" + formatted
        _start_time = time.monotonic()  # AUTO-CR-21-4: wall-clock guard reference point
        # A task-wide deadline is shared across rounds; standalone calls use a per-call budget (AUTO-CR-33).
        if deadline is not None:
            _eff_deadline = deadline
        elif self.max_task_seconds > 0:
            _eff_deadline = _start_time + self.max_task_seconds
        else:
            _eff_deadline = None
        # Seconds returned to the deadline by coder calls that died on the wire (RUN-8).
        _deadline_credit_s = 0.0

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

            # Hard wall-clock guard for the whole task, shared across rounds; None disables it (AUTO-CR-21-4, CR-33).
            if _eff_deadline is not None:
                if time.monotonic() >= _eff_deadline:
                    # The log names the real source of the deadline, the shared budget or max_task_seconds (BUGFIX).
                    if deadline is not None:
                        logger.warning(
                            "InnerLoop: task wall-clock limit reached (from the "
                            "outer-loop's shared task-wide deadline; this call "
                            "had run %.1f min) — stopping after %d attempts",
                            (time.monotonic() - _start_time) / 60.0, attempt - 1,
                        )
                    else:
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
                        deadline_credit_s=_deadline_credit_s,
                    )

            # 1. Coder: a call that died before any answer is re-run without charging an attempt or adding feedback (RUN-8).
            _coder_calls_allowed = 1 + self._coder_transport_retries
            _transport_exhausted = False
            _coder_raised: Exception | None = None
            for _c_call in range(_coder_calls_allowed):
                # Time spent on a dead socket is given back to the shared deadline, not charged to the task (RUN-8).
                _call_start = time.monotonic()
                try:
                    coder_result = self.coder.generate(
                        task, base_dir, prior_feedback=feedback,
                        prefetched_context=prefetched_context,
                    )
                except Exception as exc:
                    _coder_raised = exc
                    break

                if str(getattr(coder_result, "error_kind", "") or "") != "transport":
                    break   # a reply came back (parsed or not) — a charged attempt

                _elapsed = time.monotonic() - _call_start
                _t_budget = 0
                try:  # a malformed field must not raise into the run
                    _t_budget = int(getattr(coder_result, "max_tokens", 0) or 0)
                except (TypeError, ValueError):
                    _t_budget = 0
                _t_extra: dict = {"budget_raised": False}
                if _t_budget:
                    _t_extra["max_tokens"] = _t_budget
                _trace_stage(task_id, attempt, "coder", "TRANSPORT",
                             error=str(getattr(coder_result, "error", "") or ""),
                             calls=_coder_calls_allowed, **_t_extra)
                if _eff_deadline is not None:
                    _eff_deadline += _elapsed
                    _deadline_credit_s += _elapsed
                if _coder_calls_allowed - _c_call - 1 <= 0:
                    _transport_exhausted = True
                    break
                logger.warning(
                    "InnerLoop: task %s attempt %d coder transport failure "
                    "(call %d/%d, %.1fs credited back) — retrying in %.1fs: %s",
                    task_id, attempt, _c_call + 1, _coder_calls_allowed,
                    _elapsed, self._coder_transport_wait_sec,
                    str(getattr(coder_result, "error", "") or ""),
                )
                if self._coder_transport_wait_sec > 0:
                    time.sleep(self._coder_transport_wait_sec)

            if _coder_raised is not None:
                logger.error("InnerLoop: coder raised on attempt %d: %s",
                             attempt, _coder_raised)
                fb = f"attempt {attempt}: coder error — {_coder_raised}"
                _trace_stage(task_id, attempt, "coder", "ERROR", error=str(_coder_raised))
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            if _transport_exhausted:
                # Every re-run died before the model answered: the attempt is not charged and the coder gets no feedback line (RUN-8).
                logger.warning(
                    "InnerLoop: task %s attempt %d — coder transport failure "
                    "after %d call(s): %s",
                    task_id, attempt, _coder_calls_allowed,
                    str(getattr(coder_result, "error", "") or ""),
                )
                return InnerLoopResult(
                    task_id=task_id,
                    passed=False,
                    unavailable=True,
                    unavailable_reason=str(getattr(coder_result, "error", "") or ""),
                    unavailable_stage="coder",
                    attempts_used=attempt - 1,   # only attempts that reached a reply
                    last_feedback="",
                    records=records,
                    context_satisfied=not _any_missing,
                    deadline_credit_s=_deadline_credit_s,
                )

            # Pull-model: resolve any context the coder asked for, for the NEXT attempt.
            coder_missing = list(getattr(coder_result, "missing_context", []) or [])
            if coder_missing or not getattr(coder_result, "context_satisfied", True):
                _any_missing = True
            if coder_missing:
                # Coder pulls accumulate into the same context as validator pulls, so neither overwrites the other.
                newly = self._broker.resolve(coder_missing, target_files, base_dir_path)
                resolved_context.update(newly)
                prefetched_context = _with_collect_block(
                    self._broker.format_for_prompt(resolved_context))
                logger.info("InnerLoop: attempt %d coder requested context %s — accumulated (%d total)",
                            attempt, coder_missing, len(resolved_context))

            # Paths the coder was not allowed to touch are collected before the success check, so a rejected run can name them (RUN-1).
            files_skipped = list(getattr(coder_result, "files_skipped", []) or [])

            # The budget this attempt used, and whether its cut-off reply raised it, is recorded for the round trace (RUN-4).
            _budget = int(getattr(coder_result, "max_tokens", 0) or 0)
            _trace_extra: dict = {}
            if _budget:
                _trace_extra["max_tokens"] = _budget
                _trace_extra["budget_raised"] = bool(
                    getattr(coder_result, "budget_raised", False))

            if not getattr(coder_result, "succeeded", True):
                # Context is kept even when the coder failed: the next attempt benefits from symbols already resolved.
                fb = f"attempt {attempt}: coder failed — {getattr(coder_result, 'error', 'unknown error')}"
                _trace_stage(task_id, attempt, "coder", "REJECTED",
                             skipped=files_skipped, **_trace_extra)
                feedback.append(fb)
                records.append(AttemptRecord(attempt, False, False, False, fb))
                continue

            # Skipped paths are noted in the feedback so the model knows they never hit disk, without calling it a failure (RUN-1).
            if files_skipped:
                _trace_stage(task_id, attempt, "coder", "OK_WITH_SKIPS",
                             written=list(getattr(coder_result, "files_written", []) or []),
                             skipped=files_skipped, **_trace_extra)
                _n = len(files_skipped)
                feedback.append(
                    f"attempt {attempt}: note: {_n} file"
                    f"{'s' if _n != 1 else ''} outside target_files "
                    f"{'were' if _n != 1 else 'was'} not written "
                    f"({', '.join(str(p) for p in files_skipped)}) — "
                    f"only the listed target files are editable in this task"
                )

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
                ec  = getattr(exec_result, "exit_code", 1)
                cmd = getattr(exec_result, "command",   "") or ""
                # Stderr is included, and the pytest tail is kept, since the error line sits at the end (RUN-3).
                detail = _build_exec_detail(exec_result)
                fb  = (
                    f"attempt {attempt}: exec failed (exit {ec})"
                    + (f"  cmd={cmd!r}" if cmd else "")
                    + f"\n{detail}"
                )
                _trace_stage(task_id, attempt, "executor", "REJECTED", exit_code=ec)
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, False, False, fb))
                continue

            # 3. Validator: prior critique is passed only to validators that accept it (AUTO-CR-30).
            _ap_kwargs = {"base_dir": base_dir_path}
            if _validator_accepts_prior:
                _ap_kwargs["prior_critique"] = _prior_validator_critique

            # A validator outage is re-run alone, up to the retry limit; only an explicit answer counts as a rejection (RUN-7).
            approved = None
            vfb = ""
            _gate2_unavailable = False
            _gate2_raised: Exception | None = None
            _val_calls_allowed = 1 + self._validator_unavailable_retries
            for _val_call in range(_val_calls_allowed):
                try:
                    if _validator_has_verdict:
                        verdict = self.validator.approve_verdict(
                            task, exec_result, coder_result, **_ap_kwargs
                        )
                        approved = verdict.approved
                        vfb      = verdict.feedback()
                        _gate2_unavailable = bool(getattr(verdict, "unavailable", False))
                    else:
                        # An older validator keeps the two-outcome behaviour, since its result has no unavailable marker (RUN-7).
                        approved, vfb = self.validator.approve(
                            task, exec_result, coder_result, **_ap_kwargs
                        )
                        _gate2_unavailable = False
                except Exception as exc:
                    _gate2_raised = exc
                    break

                if not _gate2_unavailable:
                    break   # a real verdict (approved or rejected) — proceed normally

                _retries_left = _val_calls_allowed - _val_call - 1
                if _retries_left <= 0:
                    break
                logger.warning(
                    "InnerLoop: attempt %d gate2 validator unavailable "
                    "(call %d/%d) — retrying in %.1fs: %s",
                    attempt, _val_call + 1, _val_calls_allowed,
                    self._validator_unavailable_wait_sec, vfb,
                )
                if self._validator_unavailable_wait_sec > 0:
                    time.sleep(self._validator_unavailable_wait_sec)

            if _gate2_raised is not None:
                logger.error("InnerLoop: validator raised on attempt %d: %s",
                             attempt, _gate2_raised)
                fb = f"attempt {attempt}: validator error — {_gate2_raised}"
                _trace_stage(task_id, attempt, "gate2", "ERROR", error=str(_gate2_raised))
                feedback.append(fb)
                records.append(AttemptRecord(attempt, True, True, False, fb))
                continue

            if _gate2_unavailable:
                # A round where every retry was unavailable is not charged, and the coder gets no rejection feedback (RUN-7).
                logger.warning(
                    "InnerLoop: task %s attempt %d — gate2 validator still "
                    "unavailable after %d call(s): %s",
                    task_id, attempt, _val_calls_allowed, vfb,
                )
                _trace_stage(task_id, attempt, "gate2", "UNAVAILABLE",
                            calls=_val_calls_allowed)
                return InnerLoopResult(
                    task_id=task_id,
                    passed=False,
                    unavailable=True,
                    unavailable_reason=vfb,
                    unavailable_stage="gate2",
                    attempts_used=attempt - 1,   # only attempts that reached a verdict
                    last_feedback="",
                    records=records,
                    context_satisfied=not _any_missing,
                    deadline_credit_s=_deadline_credit_s,
                )

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
                    prefetched_context = _with_collect_block(
                        self._broker.format_for_prompt(resolved_context))
                    logger.info(
                        "InnerLoop: attempt %d validator requested context %s — accumulated (%d total)",
                        attempt, val_missing, len(resolved_context),
                    )
                continue

            # The Gate-3 gates run from one registry, so the fail-open and revision policy lives in one place (GATES-1).
            _rejection = run_gates(
                self,
                task=task,
                task_id=task_id,
                attempt=attempt,
                target_files=target_files,
                base_dir_path=base_dir_path,
                revisions=_gate_revisions,
                trace_stage=_trace_stage,
            )
            if _rejection is not None:
                feedback.append(f"attempt {attempt}: {_rejection.feedback}")
                records.append(
                    AttemptRecord(attempt, True, True, False, _rejection.record_text)
                )
                continue


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
                deadline_credit_s=_deadline_credit_s,
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
            deadline_credit_s=_deadline_credit_s,
        )


# Factory

def resolve_validator_llm_profile(config, *, base_url, api_key, model,
                                  api_format, num_ctx, verify_ssl,
                                  temperature, max_tokens, think):
    """Resolve the Gate-2 critic's own LLM settings. GATE2-PROFILE-1.

    Mirrors ``[gate1] presence_llm_profile`` exactly, for the same reason it
    exists there: the critic is a different job from the author, and wanting a
    different model for it is normal — a cheap fast model writes the prose
    while a stronger one judges it, or vice versa.

    Until now the critic silently reused ``[api_{active}]``. A run hit that
    provider's free-tier rate limit (HTTP 429, "free_quota_rpm") on the
    critique call specifically, and there was no way to point the critique
    somewhere else without moving every other agent with it.

    Two modes, chosen by ``[validator_agent] validator_llm_profile``:

    * **Unset (default)** — every value is the one passed in, byte for byte.
      A config without this key behaves exactly as before.
    * **Set to a section name** — ``base_url``/``api_key``/``model`` come from
      THAT section and are REQUIRED. A missing one raises rather than falling
      back, because sending one provider's credential to another provider's
      URL is a worse failure than a loud stop at startup. ``api_format``,
      ``verify_ssl``, ``temperature``, ``max_tokens``, ``num_ctx`` and
      ``think`` are optional and each falls back to the value passed in —
      never to a hardcoded default, and never to another profile's value.

    Returns a dict of keyword arguments for :class:`LLMGate2Validator`.

    GATE3-PROFILE-6: the field-by-field resolution below used to be a
    second, independent copy of the same logic in
    ``Gate1Filter._init_presence_provider``. Both now delegate to the
    shared ``tools.auto.llm_profile.resolve_llm_profile`` (see that
    module for the authoritative field-resolution rules); this function's
    job is only to adapt its ``LlmSettings`` result to the
    (dict, verify_ssl, think) shape this call site has always returned,
    and to keep this function's own established "LLMGate2Validator:
    critique provider" log wording, unchanged for anyone grepping
    existing logs.
    """
    from tools.auto.llm_profile import LlmSettings, resolve_llm_profile  # noqa: PLC0415

    defaults = LlmSettings(
        base_url=base_url, api_key=api_key, model=model,
        api_format=api_format, verify_ssl=verify_ssl,
        think=think, temperature=temperature,
        max_tokens=max_tokens, num_ctx=num_ctx,
    )
    settings, profile_name = resolve_llm_profile(
        config, "validator_agent", "validator_llm_profile", defaults=defaults)

    resolved = dict(
        base_url=settings.base_url, api_key=settings.api_key,
        model=settings.model, api_format=settings.api_format,
        num_ctx=settings.num_ctx, temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )

    if profile_name is None:
        logger.info(
            "LLMGate2Validator: critique provider = %s (%s) \u2014 shared provider "
            "(no validator_llm_profile configured)",
            settings.base_url, settings.model,
        )
    else:
        logger.info(
            "LLMGate2Validator: critique provider = %s (%s) \u2014 via "
            "validator_llm_profile = [%s]",
            settings.base_url, settings.model, profile_name,
        )
    return resolved, settings.verify_ssl, settings.think


def make_inner_loop(
    config:   configparser.ConfigParser,
    base_dir: str | Path,
    *,
    coder=None,
    executor=None,
    validator=None,
    task_mode: str = "code",
    run_goal: str = "",
    collect_bridge=None,
) -> InnerLoop:
    """Construct an :class:`InnerLoop` with real agents from *config*.

    Any agent may be injected (useful for tests); omitted agents are
    constructed from the config's API / model settings.

    AUTO-DM-1: ``task_mode`` is accepted and stored for forwarding to
    ``LLMGate2Validator`` (DM-5 will use it to select domain-appropriate
    system prompts).  Defaults to ``"code"`` — no behavioural change for
    existing call sites.
    """
    # Creative editing gets a larger cap on coder, review and revise cycles than code (AUTO-CR-16).
    from tools.auto.utils import _cfg_mode
    # A malformed retry count falls back to the default instead of crashing (config-crash audit).
    try:
        max_attempts = int(_cfg_mode(
            config, "auto", "max_attempts_per_task", task_mode,
            fallback=str(_DEFAULT_MAX_ATTEMPTS),
        ))
    except ValueError as exc:
        logger.warning(
            "config [auto] max_attempts_per_task is malformed (%s) — using default %r",
            exc, _DEFAULT_MAX_ATTEMPTS,
        )
        max_attempts = _DEFAULT_MAX_ATTEMPTS

    # ── API settings ─────────────────────────────────────────────────────────
    active_profile = config.get("api", "active", fallback="local")
    api_section    = f"api_{active_profile}"

    # Config reads are guarded per call, so one bad value cannot crash construction (AUTO-FIX).
    base_url   = config.get(api_section, "base_url",   fallback="http://localhost:1337/v1")
    api_key    = config.get(api_section, "api_key",    fallback="jan")
    model      = config.get(api_section, "model",      fallback="qwen2.5-14b-instruct")
    api_format = config.get(api_section, "api_format", fallback="openai")
    try:
        num_ctx = config.getint(api_section, "num_ctx", fallback=0)
    except ValueError as exc:
        logger.warning("config [%s] num_ctx invalid (%s) — using 0", api_section, exc)
        num_ctx = 0

    try:
        verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)
    except ValueError as exc:
        logger.warning("config [api] verify_ssl invalid (%s) — using True", exc)
        verify_ssl = True

    import ssl
    from tools.llm_stream import make_unverified_context
    ssl_context: ssl.SSLContext | None = make_unverified_context() if not verify_ssl else None

    try:
        max_hints = config.getint("validator_agent", "max_hints", fallback=3)
    except ValueError as exc:
        logger.warning("config [validator_agent] max_hints invalid (%s) — using 3", exc)
        max_hints = 3
    try:
        val_temp = config.getfloat("validator_agent", "temperature", fallback=0.1)
    except ValueError as exc:
        logger.warning("config [validator_agent] temperature invalid (%s) — using 0.1", exc)
        val_temp = 0.1
    try:
        val_timeout = config.getint("loop", "timeout_seconds", fallback=300)
    except ValueError as exc:
        logger.warning("config [loop] timeout_seconds invalid (%s) — using 300", exc)
        val_timeout = 300
    try:
        exec_timeout = config.getint("auto", "exec_timeout_sec", fallback=120)
    except ValueError as exc:
        logger.warning("config [auto] exec_timeout_sec invalid (%s) — using 120", exc)
        exec_timeout = 120
    try:
        ws_retain = config.getint("auto", "workspace_retain_count", fallback=5)
    except ValueError as exc:
        logger.warning("config [auto] workspace_retain_count invalid (%s) — using 5", exc)
        ws_retain = 5
    # Serial pytest is read with a safe fallback for a missing section, key or bad value (RUN-3).
    pytest_serial = safe_getboolean(config, "executor", "pytest_serial", fallback=True)

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
                pytest_serial=pytest_serial,
            )
        except ImportError:
            logger.warning("Executor not found — using _StubExecutor (tests only)")
            executor = _StubExecutor()

    # ── Validator ─────────────────────────────────────────────────────────────
    try:
        val_max_tokens = config.getint("validator_agent", "max_tokens", fallback=512)
    except ValueError as exc:
        logger.warning("config [validator_agent] max_tokens invalid (%s) — using 512", exc)
        val_max_tokens = 512
    if validator is None:
        # GATE2-PROFILE-1: the critic may run on its own provider/model.
        try:
            _val_think = config.getboolean("validator_agent", "think", fallback=False)
        except ValueError:
            _val_think = False
        _val_kwargs, _val_verify_ssl, _val_think = resolve_validator_llm_profile(
            config,
            base_url=base_url, api_key=api_key, model=model,
            api_format=api_format, num_ctx=num_ctx, verify_ssl=verify_ssl,
            temperature=val_temp, max_tokens=val_max_tokens, think=_val_think,
        )
        from tools.llm_stream import (  # noqa: PLC0415
            make_unverified_context as _make_unverified_context,
        )
        _val_ssl_context = (
            ssl_context if _val_verify_ssl == verify_ssl
            else (None if _val_verify_ssl else _make_unverified_context())
        )
        validator = LLMGate2Validator(
            timeout=val_timeout,
            max_hints=max_hints,
            ssl_context=_val_ssl_context,
            base_dir=str(base_dir),
            task_mode=task_mode,
            config=config,  # AUTO-DM-5: for system prompt override lookup
            think=_val_think,  # BUGFIX (audit): profile override was dropped
            **_val_kwargs,
        )

    # The collect bridge is passed to the context broker, so pull-model lookups can use collect facts (COLLECT-24).
    try:
        _max_symbols = config.getint("context_broker", "max_symbols", fallback=20)
    except ValueError as exc:
        logger.warning("config [context_broker] max_symbols invalid (%s) — using 20", exc)
        _max_symbols = 20
    broker = ContextBroker(
        max_symbols=_max_symbols,
        collect_bridge=collect_bridge,
    )

    # The Gate-3 validators are built from the registry; a factory that fails yields None, which means the gate is off.
    _gate_validators = build_validators(
        config, base_dir, task_mode=task_mode, broker=broker,
    )
    # The gate order is resolved once here, so run_gates honours the configured [gates] order (GATES-2).
    _gate_order = resolve_gate_order(config, task_mode)


    # The per-task wall-clock guard reads are guarded per call, so a bad value falls back to the default (AUTO-CR-21-4).
    try:
        max_task_seconds = config.getint("auto", "max_task_seconds", fallback=1800)
    except ValueError as exc:
        logger.warning(
            "config [auto] max_task_seconds is malformed (%s) — using default 1800",
            exc,
        )
        max_task_seconds = 1800
    try:
        require_tests = config.getboolean("inner_loop", "require_tests_code",
                                          fallback=False)
    except ValueError as exc:
        logger.warning(
            "config [inner_loop] require_tests_code is malformed (%s) — using default False",
            exc,
        )
        require_tests = False

    # Validator re-runs and their pause come from the [collect] outage-wait knob, so one setting tunes both (RUN-7).
    try:
        validator_unavailable_retries = config.getint(
            "auto", "validator_unavailable_retries",
            fallback=_DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES,
        )
    except ValueError as exc:
        logger.warning(
            "config [auto] validator_unavailable_retries is malformed (%s) — using default %d",
            exc, _DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES,
        )
        validator_unavailable_retries = _DEFAULT_VALIDATOR_UNAVAILABLE_RETRIES
    try:
        validator_unavailable_wait_sec = config.getfloat(
            "collect", "error_retry_wait_sec",
            fallback=_DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC,
        )
    except ValueError as exc:
        logger.warning(
            "config [collect] error_retry_wait_sec is malformed (%s) — using default %.0f",
            exc, _DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC,
        )
        validator_unavailable_wait_sec = _DEFAULT_VALIDATOR_UNAVAILABLE_WAIT_SEC

    # Coder re-runs on a dead call, with the same outage-wait knob as the validator (RUN-8).
    try:
        coder_transport_retries = config.getint(
            "auto", "coder_transport_retries",
            fallback=_DEFAULT_CODER_TRANSPORT_RETRIES,
        )
    except ValueError as exc:
        logger.warning(
            "config [auto] coder_transport_retries is malformed (%s) — using default %d",
            exc, _DEFAULT_CODER_TRANSPORT_RETRIES,
        )
        coder_transport_retries = _DEFAULT_CODER_TRANSPORT_RETRIES

    loop = InnerLoop(coder, executor, validator, max_attempts=max_attempts,
                     context_broker=broker,
                     **_gate_validators,
                     require_tests=require_tests,
                     task_mode=task_mode, max_task_seconds=max_task_seconds,
                     run_goal=run_goal, collect_bridge=collect_bridge,
                     validator_unavailable_retries=validator_unavailable_retries,
                     validator_unavailable_wait_sec=validator_unavailable_wait_sec,
                     coder_transport_retries=coder_transport_retries,
                     coder_transport_wait_sec=validator_unavailable_wait_sec)
    loop.gate_order = _gate_order
    logger.info(
        "InnerLoop: Gate-3 order for %s mode — %s",
        task_mode, ", ".join(s.name for s in _gate_order) or "(none)",
    )
    return loop


# ── Stubs for environments without real agents (unit tests) ──────────────────

class _StubCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        from types import SimpleNamespace
        return SimpleNamespace(succeeded=False, files_written=[], error="stub coder — no real coder available")


class _StubExecutor:
    def run(self, task):
        from types import SimpleNamespace
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="", traceback="stub executor", timed_out=False)
