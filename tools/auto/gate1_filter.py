"""tools/auto/gate1_filter.py — AUTO-B3: Gate 1 false-positive filter.

For each candidate produced by AUTO-B2 (ClusterReviewer), this module
performs a two-stage static grounding check *before* the task ever enters
the Coder loop:

Stage A — Existence check (no LLM, instant):
  1. The cited file exists under base_dir.
  2. If a symbol is cited, ``block_extractor.extract_block`` finds it.
  3. If only a line range is cited, those line numbers are within the file.

Stage A0 — Unindexed location (no LLM, instant, L1):
  A candidate that survived Stage A whose cited location the collect model
  does not index (an ``.ini``/``.md``/``.yaml`` the architect was handed
  verbatim) is rejected before its presence call is ever made — 74 of 834
  live Stage-B calls were spent judging non-code files. It asks the model
  "do you know this path" (``CollectBridge.module_symbols``), never "is this
  extension allowed", so a file the artifact indexes is never rejected
  (``.py`` *and* ``.java``). Code mode only — docs/creative runs are about
  the prose the model does not index — and off entirely when there is no
  usable collect model or ``[gate1] skip_llm_for_unindexed = false``.

Stage B — Problem-presence check (one LLM call per surviving candidate):
  An LLM reads the exact code block (or line range) and answers whether
  the problem described in the candidate's instruction is actually present
  and not already fixed.  The response is a small JSON object:

      {"verdict": "confirmed" | "rejected", "reason": "<one sentence>"}

  Fail-closed: an unparseable response, network error, or missing "verdict"
  key is treated as a *rejection* so a faulty LLM cannot sneak bad tasks
  through Gate 1.

Stage C — Deduplication:
  Candidates sharing the same (cited_file, cited_symbol OR line range, title)
  fingerprint are deduplicated; the first occurrence is kept.

Public surface consumed by controller.py / the Architect stage::

    from tools.auto.gate1_filter import Gate1Filter, FilterResult

    filt = Gate1Filter(config, base_url, api_key, model)
    accepted, rejected = filt.filter(candidates, base_dir)
    # accepted : list[CandidateTask] — ready for AUTO-B4
    # rejected : list[FilterResult]  — logged, not propagated

Configuration (agents.ini [gate1])
------------------------------------
temperature   — sampling temperature (default 0.0 — deterministic)
max_tokens    — token cap for the presence-check call (default 512; thinking
                models like qwen3 spend part of this budget on an internal
                <think> block before the JSON verdict, so this needs more
                headroom than a plain JSON-only estimate)
think         — Ollama "think" toggle for reasoning models (default false —
                Gate 1 wants a tiny deterministic verdict, not reasoning in
                the reply; set true to re-enable a model's thinking mode)
presence_empty_retries — RUN-9: how many times an EMPTY reply whose stream
                metadata shows no sign the budget was spent (a degraded
                provider, not an exhausted model) is re-issued unchanged
                before the re-ask ladder runs (default 2; 0 = today's
                behaviour, every empty reply goes straight into the ladder)
system        — override the built-in system prompt (optional)
skip_llm      — "true" to run existence checks only, skip LLM stage (testing)
skip_llm_for_unindexed — "false" to disable Stage A0 and let a candidate
                 whose cited location the collect model does not index
                 still reach the presence check (default true; L1)

agents.ini [api] / [api_local] / [api_remote] supply the same connection
keys used everywhere else in this codebase.
"""

from __future__ import annotations

import configparser
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.agent_trace import tracer
from tools.auto.architect import CandidateTask
from tools.auto.llm_profile import LlmSettings, resolve_llm_profile
from tools.auto import gate1_vote as _gate1_vote
from tools.auto.gate1_grounding import (
    callee_context, config_fallback_note, target_file_context,
    instruction_file_context,
    collect_contract_note, existing_test_coverage_note, truncation_safety_note,
    intentional_design_note, test_helper_note,
)
from tools.claimcheck.anchors import PathRepoView
from tools.block_extractor import extract_block, extract_module_docstring
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think
# HARDEN-7 B: the shared reader of a reply field's type. One presence reply the
# model sent with `evidence` as a list used to raise AttributeError out of
# `filter()`, which `pipeline.py` and `plan_validator.py` call without a guard —
# one malformed reply ended the planning phase and lost every other candidate.
from tools.llm_stream import reply_text
from tools.config_safe import safe_getboolean

logger = logging.getLogger(__name__)

# ── Gate 1 system prompts ─────────────────────────────────────────────────────

_SYSTEM_PROMPT_CODE = (
    "You are a static code reviewer performing a false-positive check. "
    "You will be shown a code excerpt and a description of a claimed problem. "
    "Your ONLY job is to verify whether the described problem is actually present "
    "in the code shown, and has NOT already been fixed. "
    "GATE1-CTX-4: look specifically for existing try/except blocks, "
    "error-handling comments (e.g. 'fail-open', 'noqa: BLE001'), or tests "
    "already covering the claim, in the code shown and any grounding notes "
    "below it — a claim about missing error handling or missing test "
    "coverage is REJECTED if that handling/coverage already exists. "
    "A confident, specific-sounding claim is not evidence — plan-generating "
    "models regularly reference the wrong function, describe a bug a nearby "
    "comment already documents as fixed, or restate what a docstring/comment "
    "says instead of what the code does. Trust only what you can quote "
    "verbatim from the code block. "
    "Do NOT suggest improvements. Do NOT run the code. "
    "Return ONLY a JSON object — no prose, no markdown fences, no preamble."
)

_SYSTEM_PROMPT_DOCS = (
    "You are a documentation reviewer performing a false-positive check. "
    "You will be shown a prose excerpt and a description of a claimed documentation problem. "
    "Your ONLY job is to verify whether the described problem is actually present "
    "in the text shown, and has NOT already been fixed. "
    "Do NOT suggest improvements. Treat the content as documentation, not code. "
    "Trust only what you can quote verbatim from the text shown, not how "
    "confident the claim sounds. "
    "Return ONLY a JSON object — no prose, no markdown fences, no preamble."
)

_SYSTEM_PROMPT_CREATIVE = (
    "You are a creative writing editor performing a quality check. "
    "You will be shown a text excerpt and a description of a claimed writing issue. "
    "Your ONLY job is to verify whether the described issue is actually present "
    "in the text shown, and has NOT already been addressed. "
    "Do NOT suggest improvements. Treat the content as creative writing, not code. "
    "Trust only what you can quote verbatim from the text shown, not how "
    "confident the claim sounds. "
    "Return ONLY a JSON object — no prose, no markdown fences, no preamble."
)

# Backward-compat alias.
_SYSTEM_PROMPT = _SYSTEM_PROMPT_CODE

_SYSTEM_PROMPTS: dict[str, str] = {
    "code":     _SYSTEM_PROMPT_CODE,
    "docs":     _SYSTEM_PROMPT_DOCS,
    "creative": _SYSTEM_PROMPT_CREATIVE,
}

# Placeholders of the user prompt template: instruction, location and code block.
_USER_PROMPT_TMPL = """\
Claimed problem: {instruction}

Location: {location}

Code at that location:
```
{code_block}
```
{grounding_notes}
Is the claimed problem actually present in the code shown above, \
and NOT already fixed?

Before answering, find the SPECIFIC line(s) in the code above that the \
claim depends on. If you cannot point to an actual line that supports the \
claim, the claim is not present — reject it, even if the instruction \
sounds confident, cites a bug-tracker ID, or references a comment near \
the code (a comment describing a bug is not evidence the bug is still \
unfixed — check the code the comment sits next to, not just the comment).

Return ONLY this JSON (no extra keys). "evidence" is REQUIRED whenever \
verdict is "confirmed" — a substring copied verbatim from the code block \
above that proves the claim; use "" only when rejecting:
{{"verdict": "confirmed" | "rejected", "evidence": "<exact verbatim \
substring from the code block above, or "" if rejecting>", \
"reason": "<one sentence explaining your decision>"}}
"""

# Context-size defaults, used only when [gate1] lacks max_context_lines and max_block_chars.
_DEFAULT_MAX_CONTEXT_LINES = 60
_DEFAULT_MAX_BLOCK_CHARS   = 4000


# Result types

@dataclass
class FilterResult:
    """Outcome record for one candidate after Gate 1.

    Implements the unified :class:`~tools.auto.gate_verdict.GateVerdict`
    interface: ``approved`` is the inverse of ``accepted`` (so a candidate
    the Gate-3 runner would want to reject reads the same way it reads
    every other gate), and :meth:`feedback` returns the human-readable
    ``reason`` on a rejection or ``""`` on acceptance — matching the
    ``feedback()`` contract of every Gate-3 verdict. The native
    ``accepted`` field is kept unchanged for the many call sites
    (:mod:`tools.auto.plan_validator`, the ``filter`` return tuple, the
    ``all_results`` loop below) that read it directly.

    Attributes
    ----------
    candidate:
        The :class:`~tools.auto.architect.CandidateTask` that was evaluated.
    accepted:
        ``True`` if the candidate passed both existence and problem-presence
        checks; ``False`` if it was rejected at any stage.
    stage:
        Which stage produced the final verdict: ``"existence"``,
        ``"presence"``, or ``"duplicate"``.
    reason:
        Human-readable explanation of why the candidate was accepted or
        rejected.  For accepted candidates this is the LLM's confirmation
        sentence (or ``"existence check passed"`` when LLM is skipped).
    """

    candidate: CandidateTask
    accepted: bool
    stage: str
    reason: str

    @property
    def approved(self) -> bool:
        """Unified GateVerdict field — the inverse of :attr:`accepted`.

        Gate 1's native field is ``accepted`` (a candidate that *passed*
        the filter is ``True``); the unified interface uses ``approved``
        with the same sense (``True`` = the gate accepts the candidate).
        The two are exact inverses only in name — here they mean the
        same thing, so this property is a plain alias.
        """
        return self.accepted

    def feedback(self) -> str:
        """Coder-facing message. Empty on acceptance, the ``reason`` on rejection."""
        return "" if self.accepted else self.reason


# Gate1Filter

class Gate1Filter(_llm_stream.LLMClientBase):
    """Runs the two-stage Gate 1 filter over a list of :class:`CandidateTask`.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    base_url:
        API endpoint (e.g. ``http://localhost:1337/v1``).
    api_key:
        Authentication token.
    model:
        Model name string.
    api_format:
        ``"openai"`` or ``"ollama"`` — forwarded to ``request_completion``.
    verify_ssl:
        Whether to verify the server's TLS certificate.
    """

    # Retries for an unparseable presence verdict: six, an even number so each token tier tries both temperatures (AUTO-RETRY-TEMP-1).
    _UNPARSEABLE_MAX_RETRIES = 6

    # Token budget floor and ceiling for empty replies: not from max_tokens (the small value causing the cut); the ceiling tracks num_ctx.
    _UNPARSEABLE_TOKENS_FLOOR = 4096
    _UNPARSEABLE_TOKENS_STEP_MULT = 2.0
    _UNPARSEABLE_TOKENS_DEFAULT_CEILING = 32768
    _UNPARSEABLE_TOKENS_CTX_FRACTION = 0.5
    # Fixed absolute temperatures per token tier, not relative to the base (which would stay at 0.0); see AUTO-RETRY-TEMP-1.
    _UNPARSEABLE_TEMPERATURES = (0.0, 0.1)

    def __init__(
        self,
        config: configparser.ConfigParser,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "openai",
        verify_ssl: bool = True,
        task_mode: str = "code",
        collect_bridge=None,
    ) -> None:
        super().__init__(config, base_url, api_key, model, api_format, verify_ssl)
        self._task_mode  = task_mode
        # The collect bridge (optional) adds contract and coverage notes to the grounding (GATE1-CTX).
        self._collect_bridge = collect_bridge

        sec = "gate1"
        # Config reads are guarded per call (config-crash audit): one malformed value falls back instead of crashing.
        try:
            self._temperature = float(config.get(sec, "temperature", fallback="0.0"))
        except ValueError as exc:
            logger.warning("config [%s] temperature is malformed (%s) — using default 0.0", sec, exc)
            self._temperature = 0.0
        try:
            self._max_tokens = int(config.get(sec, "max_tokens", fallback="512"))
        except ValueError as exc:
            logger.warning("config [%s] max_tokens is malformed (%s) — using default 512", sec, exc)
            self._max_tokens = 512
        # Learn the budget that worked: the next candidate starts at the median of recent successes (GATE1-LEARN-1).
        try:
            self._unparseable_learn = config.getboolean(sec, "unparseable_learn", fallback=True)
        except ValueError as exc:
            logger.warning("config [%s] unparseable_learn is malformed (%s) — using default True", sec, exc)
            self._unparseable_learn = True
        try:
            self._unparseable_learn_window = max(1, int(config.get(
                sec, "unparseable_learn_window", fallback="8")))
        except ValueError as exc:
            logger.warning("config [%s] unparseable_learn_window is malformed (%s) — using default 8", sec, exc)
            self._unparseable_learn_window = 8
        # Opt-in retry knobs, defaults keep the old behaviour: retry mode, a max_tokens cap and the ladder length (GATE1-LEARN-2).
        try:
            _mode = config.get(sec, "unparseable_retry_mode", fallback="strict").strip().lower()
            if _mode not in ("strict", "fast"):
                raise ValueError(f"expected strict|fast, got {_mode!r}")
            self._unparseable_retry_mode = _mode
        except ValueError as exc:
            logger.warning("config [%s] unparseable_retry_mode is malformed (%s) — using default strict", sec, exc)
            self._unparseable_retry_mode = "strict"
        try:
            self._unparseable_max_tokens_cap = max(0, int(config.get(
                sec, "unparseable_max_tokens_cap", fallback="0")))
        except ValueError as exc:
            logger.warning("config [%s] unparseable_max_tokens_cap is malformed (%s) — using default 0", sec, exc)
            self._unparseable_max_tokens_cap = 0
        try:
            self._unparseable_max_retries = max(0, int(config.get(
                sec, "unparseable_max_retries",
                fallback=str(self._UNPARSEABLE_MAX_RETRIES))))
        except ValueError as exc:
            logger.warning("config [%s] unparseable_max_retries is malformed (%s) — using default %d", sec, exc, self._UNPARSEABLE_MAX_RETRIES)
            self._unparseable_max_retries = self._UNPARSEABLE_MAX_RETRIES
        # Re-issue the same request when an empty reply came from a degraded provider, not an exhausted budget (RUN-9).
        try:
            self._presence_empty_retries = max(0, int(config.get(
                sec, "presence_empty_retries", fallback="2")))
        except ValueError as exc:
            logger.warning("config [%s] presence_empty_retries is malformed (%s) — using default 2", sec, exc)
            self._presence_empty_retries = 2
        # What to do with a presence check the provider never answered: keep, reject or retry (RUN-5).
        try:
            _unknown_policy = config.get(sec, "presence_unknown", fallback="keep").strip().lower()
            if _unknown_policy not in ("keep", "reject"):
                raise ValueError(f"expected keep|reject, got {_unknown_policy!r}")
            self._presence_unknown_policy = _unknown_policy
        except ValueError as exc:
            logger.warning("config [%s] presence_unknown is malformed (%s) — using default keep", sec, exc)
            self._presence_unknown_policy = "keep"
        # Learned budgets are kept per presence model, since budgets from different models are not comparable.
        self._unparseable_samples: dict[str, list[int]] = {}
        # GATE1-PAR-1: the learned window is shared by presence workers, so one lock guards it.
        self._learn_lock = threading.Lock()
        # Stage-split counters that no result record carries (reask, non-.py requests, unknown); updated under a lock when workers run in parallel (M4).
        self.presence_reask: int = 0
        self.presence_unknown: int = 0
        # Why empty replies were empty: transport (retried) or exhausted budget (empty-pinned); no-think ignored per provider (RUN-9).
        self.presence_empty_transport: int = 0
        self.presence_empty_exhausted: int = 0
        self.presence_nothink_ignored: int = 0
        self.non_py_requests: int = 0
        self._counter_lock = threading.Lock()
        # One warning per (base_url, model) per run, not one per candidate, so a fleet of empty replies stays one line (RUN-9).
        self._nothink_warned: set = set()
        # Presence checks run in a thread pool when presence_workers > 1; the default 1 keeps the sequential loop (GATE1-PAR-1).
        try:
            self._presence_workers = max(1, int(config.get(
                sec, "presence_workers", fallback="1")))
        except ValueError as exc:
            logger.warning("config [%s] presence_workers is malformed (%s) — using default 1", sec, exc)
            self._presence_workers = 1
        try:
            self._skip_llm = config.getboolean(sec, "skip_llm", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] skip_llm is malformed (%s) — using default False", sec, exc)
            self._skip_llm = False
        # Stage A0: a candidate whose cited file the collect model does not index costs no presence call; code mode only (L1).
        try:
            self._skip_llm_for_unindexed = config.getboolean(
                sec, "skip_llm_for_unindexed", fallback=True)
        except ValueError as exc:
            logger.warning(
                "config [%s] skip_llm_for_unindexed is malformed (%s) — using default True",
                sec, exc,
            )
            self._skip_llm_for_unindexed = True
        # CC-10: three voters with a quote shortcut decide when sure; claim_vote off builds nothing.
        try:
            self._vote = _gate1_vote.build(config)
        except Exception as exc:  # noqa: BLE001 — a wrong vote setting must not stop Gate 1
            logger.warning("config [gate1] claim_vote: the vote stays off (%s)", exc)
            self._vote = None
        try:
            self._timeout = float(config.get("loop", "timeout_seconds", fallback="300"))
        except ValueError as exc:
            logger.warning("config [loop] timeout_seconds is malformed (%s) — using default 300.0", exc)
            self._timeout = 300.0
        # One presence check, with every retry included, is bounded by the presence deadline; past it the check is unknown (CC-10).
        try:
            self._presence_deadline_sec = float(config.get(
                sec, "presence_deadline_sec", fallback=str(DEFAULT_PRESENCE_DEADLINE_SEC)) or 0)
            if self._presence_deadline_sec < 0:
                raise ValueError("below 0")
            if not math.isfinite(self._presence_deadline_sec):
                raise ValueError("not a number of seconds (0 = no bound)")   # nan was no bound at all
        except ValueError as exc:
            logger.warning("config [%s] presence_deadline_sec is malformed (%s) — using default %.0f",
                           sec, exc, DEFAULT_PRESENCE_DEADLINE_SEC)
            self._presence_deadline_sec = DEFAULT_PRESENCE_DEADLINE_SEC
        # One view per target directory per run, so the anchor index is built once and not per candidate (CC-10).
        self._vote_views: dict = {}
        self._vote_views_lock = threading.Lock()
        # Extra attempts for a plain LLM exception before the candidate fails closed as a technical failure (AUTO-RETRY-BACKOFF-1).
        try:
            self._llm_call_retry_max = max(0, config.getint(
                sec, "llm_call_retry_max", fallback=3))
        except ValueError as exc:
            logger.warning("config [%s] llm_call_retry_max is malformed (%s) — using default 3", sec, exc)
            self._llm_call_retry_max = 3
        try:
            self._llm_call_retry_wait_sec = max(0.0, float(config.get(
                sec, "llm_call_retry_wait_sec", fallback="60")))
        except ValueError as exc:
            logger.warning("config [%s] llm_call_retry_wait_sec is malformed (%s) — using default 60.0", sec, exc)
            self._llm_call_retry_wait_sec = 60.0
        try:
            self._max_context_lines = int(config.get(
                sec, "max_context_lines", fallback=str(_DEFAULT_MAX_CONTEXT_LINES)))
        except ValueError as exc:
            logger.warning(
                "config [%s] max_context_lines is malformed (%s) — using default %r",
                sec, exc, _DEFAULT_MAX_CONTEXT_LINES,
            )
            self._max_context_lines = _DEFAULT_MAX_CONTEXT_LINES
        try:
            self._max_block_chars = int(config.get(
                sec, "max_block_chars", fallback=str(_DEFAULT_MAX_BLOCK_CHARS)))
        except ValueError as exc:
            logger.warning(
                "config [%s] max_block_chars is malformed (%s) — using default %r",
                sec, exc, _DEFAULT_MAX_BLOCK_CHARS,
            )
            self._max_block_chars = _DEFAULT_MAX_BLOCK_CHARS
        # The presence verdict is a tiny JSON: reasoning is stripped so a thinking model cannot use up the budget (AUTO-FIX).
        try:
            self._think = config.getboolean(sec, "think", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] think is malformed (%s) — using default False", sec, exc)
            self._think = False
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        _active = config.get("api", "active", fallback="local")
        try:
            self._num_ctx = config.getint(f"api_{_active}", "num_ctx", fallback=0)
        except ValueError as exc:
            logger.warning("config [api_%s] num_ctx is malformed (%s) — using default 0", _active, exc)
            self._num_ctx = 0
        # JSON mode is one global switch in [api], default off; if the endpoint rejects it, the call falls back for the rest of the run (AUTO-JSONMODE-1).
        try:
            self._response_format = config.getboolean("api", "response_format", fallback=False)
        except ValueError as exc:
            logger.warning("config [api] response_format is malformed (%s) — using default False", exc)
            self._response_format = False
        # Reasoning depth is one global switch in [api], default off; an endpoint that rejects the value falls back to plain think (AUTO-THINKDEPTH-1).
        try:
            _think_effort_enabled = config.getboolean("api", "think_effort_enabled", fallback=False)
        except ValueError as exc:
            logger.warning("config [api] think_effort_enabled is malformed (%s) — using default False", exc)
            _think_effort_enabled = False
        self._think_effort = (
            config.get("api", "think_effort", fallback="").strip()
            if _think_effort_enabled
            else None
        ) or None

        # System prompt by task_mode: mode key, legacy key, then built-in constant (DM-3).
        mode_ini_key = f"system_{task_mode}" if task_mode != "code" else None
        if mode_ini_key and config.has_option(sec, mode_ini_key):
            self._system = config.get(sec, mode_ini_key).strip()
        else:
            built_in = _SYSTEM_PROMPTS.get(task_mode, _SYSTEM_PROMPT_CODE)
            self._system = config.get(sec, "system", fallback=built_in).strip()

        self._init_presence_provider(config, sec, base_url, api_key, model,
                                      api_format, verify_ssl)

    # Presence-provider selection is split out of __init__, so the "which section wins" logic is in one place (GATE1-PROVIDER-2).
    def _init_presence_provider(
        self, config: configparser.ConfigParser, sec: str,
        base_url: str, api_key: str, model: str, api_format: str,
        verify_ssl: bool,
    ) -> None:
        """Resolve which provider/model the presence check (`_check_presence`,
        the only LLM call Gate1Filter makes — existence checks are pure
        filesystem/AST and never touch an LLM) actually talks to, and populate
        ``self._presence_*``.

        Two modes, selected by ``[gate1] presence_llm_profile``:

        - **Unset (default)** — ``self._presence_*`` are exact copies of the
          constructor-passed ``base_url``/``api_key``/``model``/``api_format``
          and this instance's own ``think``/``temperature``/``max_tokens``/
          ``num_ctx`` ([gate1]) and ``response_format``/``think_effort``
          (global [api], AUTO-JSONMODE-1 / AUTO-THINKDEPTH-1). Byte-for-byte
          the pre-existing behaviour; nothing about a config file without
          this key changes.

        - **Set to a section name** (e.g. ``presence_llm_profile = gate1_llm``)
          — presence-check calls instead use THAT section's own
          ``base_url``/``api_key``/``model`` (required — raises a clear
          ``ValueError`` naming the missing key(s) rather than silently
          falling back to the shared provider, since silently reusing a
          different provider's credential against a URL it was never meant
          for is a worse failure than a loud one at startup) and
          ``api_format``/``verify_ssl`` (optional, default ``"openai"`` /
          ``True``).

          ``think``/``temperature``/``max_tokens``/``num_ctx``/
          ``response_format``/``think_effort_enabled``+``think_effort`` are
          each read from the PROFILE section first; any one the profile
          doesn't set falls back to THIS instance's own resolved value
          (``self._think`` etc., ``self._response_format``,
          ``self._think_effort``) — never to a hardcoded default and never
          to a different profile's value. This is the crux of the "don't
          conflict with another model's saved settings" requirement: a
          profile tuned for a thinking-capable model (``think = true``) and
          the default non-thinking ``[gate1]`` setting coexist without
          either bleeding into the other; a profile for a provider that
          doesn't support enforced JSON mode can set
          ``response_format = false`` even while the global ``[api]
          response_format = true`` switch is on for every other caller; and
          two DIFFERENT profiles never share settings with each other
          either — each is read independently, from its own section, every
          time.

        The module-level ``(url, model)``-keyed "does this endpoint accept
        field X" memories (see ``tools.llm_stream.
        mark_reasoning_field_unsupported`` and its three siblings for
        response_format / think_effort / think_depth) already give the
        presence profile's own (possibly different) url+model an
        independent verdict from whatever the shared provider's url+model
        has recorded — no extra wiring needed here for that part.
        """
        # Field resolution is shared with the validator profiles: an unset profile copies the shared provider, a named one overrides it (GATE3-PROFILE-6).
        defaults = LlmSettings(
            base_url=base_url, api_key=api_key, model=model,
            api_format=api_format, verify_ssl=verify_ssl,
            think=self._think, temperature=self._temperature,
            max_tokens=self._max_tokens, num_ctx=self._num_ctx,
            response_format=self._response_format,
            think_effort_enabled=self._think_effort is not None,
            think_effort=self._think_effort,
        )
        settings, profile_name = resolve_llm_profile(
            config, sec, "presence_llm_profile", defaults=defaults)

        self._presence_base_url        = settings.base_url
        self._presence_api_key         = settings.api_key
        self._presence_model           = settings.model
        self._presence_api_format      = settings.api_format
        # No profile: reuse this instance's ssl_context object as it is, not a rebuilt equivalent.
        self._presence_ssl_context = (
            self._ssl_context if profile_name is None
            else (_llm_stream.make_unverified_context()
                  if not settings.verify_ssl else None)
        )
        self._presence_think           = settings.think
        self._presence_temperature     = settings.temperature
        self._presence_max_tokens      = settings.max_tokens
        self._presence_num_ctx         = settings.num_ctx
        self._presence_response_format = settings.response_format
        self._presence_think_effort    = settings.think_effort

        # One startup line says which presence provider the run uses, so it need not be reverse-engineered from URLs (AUTO-PRESENCE-LOG-1).
        if profile_name is None:
            logger.info(
                "Gate1Filter: presence-check provider = %s (%s) — shared "
                "provider (no presence_llm_profile configured)",
                self._presence_base_url, self._presence_model,
            )
        else:
            logger.info(
                "Gate1Filter: presence-check provider = %s (%s) — via "
                "presence_llm_profile = [%s]",
                self._presence_base_url, self._presence_model, profile_name,
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def filter(
        self,
        candidates: list[CandidateTask],
        base_dir: str | Path,
        cluster_files: "dict[str, set[str]] | None" = None,
        counters: "dict | None" = None,
    ) -> tuple[list[CandidateTask], list[FilterResult]]:
        """Run Gate 1 over every candidate and split into accepted / rejected.

        Parameters
        ----------
        candidates:
            Output of :func:`tools.auto.architect.ClusterReviewer.review_clusters`.
        base_dir:
            Root directory of the repository; all cited file paths are resolved
            relative to this.
        cluster_files:
            Optional mapping of cluster name → set of known relative file paths
            produced by the ingestor.  When provided, a candidate whose
            ``cited_location.file`` is not in its cluster's file set is rejected
            immediately with a clear "hallucinated path" message — before any
            filesystem I/O.  Pass ``None`` to skip this check (e.g. in tests).
        counters:
            M4. An optional dict filled with the per-stage split
            (`existence`, `presence_confirmed`, `presence_rejected`,
            `presence_fail_closed`, `presence_unknown`, `presence_reask`,
            `duplicate`, `non_py`, plus `_uncounted`) before returning.
            `None` (the default, and what every caller has always used)
            changes nothing — the return tuple is byte-for-byte the same as
            before this parameter existed.

        Returns
        -------
        accepted : list[CandidateTask]
            Candidates that passed existence + presence checks and are unique.
        rejected : list[FilterResult]
            Every candidate that was dropped, with the stage and reason logged.
        """
        base_dir = Path(base_dir)
        # One line per run says whether the collect model was present and who got it; the artifact is read once per run, not cached across runs (CC-13).
        _gate1_vote.clear_collect_reads()
        print(self._collect_line(base_dir), flush=True)
        all_results: list[FilterResult] = []

        # ── Stage A: existence checks ─────────────────────────────────────────
        existence_passed: list[tuple[CandidateTask, str]] = []  # (task, code_block)

        n_candidates = len(candidates)
        for i, c in enumerate(candidates, 1):
            print(f"  [{i}/{n_candidates}] existence check: {c.title}")
            ok, reason, block = self._check_existence(c, base_dir, cluster_files)
            if ok:
                existence_passed.append((c, block))
            else:
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="existence", reason=reason,
                ))
                # An existence rejection is Gate 1 doing its job, not an anomaly, so it is logged at info (AUTO-LOG-1).
                logger.info(
                    "Gate1[existence] REJECTED %r — %s", c.title, reason,
                )

        print(
            f"\n🔎 Gate 1 existence: "
            f"{len(existence_passed)}/{len(candidates)} candidate(s) passed"
        )

        # Stage A0: a location the collect model does not index costs no LLM call; code mode only, and new files are exempt (L1).
        if (
            self._skip_llm_for_unindexed
            and self._task_mode == "code"
            and getattr(self, "_collect_bridge", None) is not None
        ):
            a0_passed: list[tuple[CandidateTask, str]] = []
            a0_removed = 0
            for c, block in existence_passed:
                if getattr(c.cited_location, "new_file", False) or self._location_is_indexed(c):
                    a0_passed.append((c, block))
                    continue
                a0_removed += 1
                ext = Path(c.cited_location.file).suffix or "no extension"
                # Recorded under the existence stage with its own prefix, so it is counted and still separable in the log.
                reason = (
                    f"location is not an indexed source file ({ext}) — the "
                    f"collect model does not index {c.cited_location.file!r}, "
                    f"so a presence check cannot ground a claim against it "
                    f"(set [gate1] skip_llm_for_unindexed = false to check "
                    f"it anyway)"
                )
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="existence", reason=reason,
                ))
                logger.info(
                    "Gate1[existence] REJECTED %r — %s", c.title, reason,
                )
            if a0_removed:
                print(
                    f"🔎 Gate 1 unindexed: {a0_removed} candidate(s) skipped "
                    f"the presence check (location not in the collect model)"
                )
                logger.info(
                    "Gate1[a0] skipped the presence check for %d candidate(s) "
                    "whose cited location the collect model does not index",
                    a0_removed,
                )
            existence_passed = a0_passed

        # ── Stage B: LLM problem-presence check ───────────────────────────────
        presence_passed: list[tuple[CandidateTask, str]] = []  # (task, reason)

        if self._skip_llm or self._task_mode == "creative":
            # Creative generation has no existing text to check, so existence is enough here; Gate 2 and the canon gate judge quality (AUTO-CR-8).
            _why = "LLM skipped" if self._skip_llm else "creative mode — existence only"
            presence_passed = [(c, f"existence check passed ({_why})") for c, _ in existence_passed]
        else:
            n_presence = len(existence_passed)

            def _presence_one(i: int, c: CandidateTask, block: str) -> tuple[bool, str]:
                print(f"  [{i}/{n_presence}] presence check: {c.title}")
                module_docstring = self._module_docstring_for(c, base_dir)
                voted = self._vote_on(c, base_dir)
                if voted is not None and voted.verdict == "confirmed":
                    # CC-10: three families, three verified quotes — the reader is not asked
                    ok, reason, verdict = True, voted.reason, "confirmed"
                    self._vote.note_decided()
                elif (voted is not None and voted.verdict == "rejected"
                      and not self._vote.cfg.veto_by_reader):
                    ok, reason, verdict = False, voted.reason, "rejected"
                    self._vote.note_decided()
                else:
                    ok, reason, verdict = self._check_presence(
                        c, block, module_docstring=module_docstring, base_dir=base_dir,
                    )
                    if voted is not None and voted.verdict == "rejected":
                        if ok and verdict == "confirmed":
                            # the veto: the reader read the code and disagrees with a unanimous FALSE
                            self._vote.note_overridden()
                        else:
                            self._vote.note_decided()
                            # a reader with no answer is not a "confirmed": the vote's reason stands alone
                            no_answer = verdict == _PRESENCE_UNKNOWN
                            ok, verdict = False, "rejected"
                            reason = voted.reason + ("" if no_answer else f"; single reader: {reason}")
                # Logged at completion, so verdict lines appear as each check finishes even with parallel workers.
                if verdict == _PRESENCE_UNKNOWN:
                    # No verdict at all is a warning, never REJECTED: that word stays reserved for what the model actually rejected (RUN-5).
                    logger.warning(
                        "Gate1[presence] UNKNOWN %r — %s", c.title, reason,
                    )
                    if self._presence_unknown_policy == "reject":
                        # Today's behaviour, for operators who prefer a small, certain plan.
                        return False, reason
                    logger.info(
                        "Gate1[presence] KEPT (presence_unknown = keep) %r — %s",
                        c.title, reason,
                    )
                    return True, reason
                if ok:
                    # A confirmation is logged at info like a rejection, so passing candidates are visible too (AUTO-LOG-1).
                    logger.info(
                        "Gate1[presence] CONFIRMED %r — %s", c.title, reason,
                    )
                else:
                    # A model's rejection; technical failures never reach this line (see the unknown branch above).
                    logger.info(
                        "Gate1[presence] REJECTED %r — %s", c.title, reason,
                    )
                return ok, reason

            # With several presence workers the LLM calls overlap, but outcomes are read in order (GATE1-PAR-1).
            outcomes: list[tuple[bool, str] | None] = [None] * n_presence
            llm_slots = [
                (i, c, block) for i, (c, block) in enumerate(existence_passed, 1)
                if not c.cited_location.new_file
            ]
            n_workers = min(self._presence_workers, len(llm_slots))
            if n_workers > 1:
                print(f"  presence checks: {len(llm_slots)} call(s) across {n_workers} worker(s)")
                with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="gate1-presence") as pool:
                    futures = {i: pool.submit(_presence_one, i, c, block) for i, c, block in llm_slots}
                    for i, fut in futures.items():
                        outcomes[i - 1] = fut.result()
            else:
                for i, c, block in llm_slots:
                    outcomes[i - 1] = _presence_one(i, c, block)

            for i, (c, block) in enumerate(existence_passed, 1):
                if c.cited_location.new_file:
                    # A new file has no existing content, so there is nothing to check its problem against (AUTO-BUG).
                    presence_passed.append(
                        (c, "new file — existence check sufficient"))
                    continue
                ok, reason = outcomes[i - 1]
                if ok:
                    if reason.startswith(UNKNOWN_PRESENCE_REASON):
                        # The reason travels with the task, so the coder reads that the model never verified it (RUN-5).
                        c.instruction = _with_presence_unknown_note(c.instruction, reason)
                    presence_passed.append((c, reason))
                else:
                    all_results.append(FilterResult(
                        candidate=c, accepted=False, stage="presence", reason=reason,
                    ))

        # Counts how many confirmed tasks were never verified by the model, so the total is not read as all verified (RUN-5).
        _unknown_kept = sum(
            1 for _, r in presence_passed if r.startswith(UNKNOWN_PRESENCE_REASON)
        )
        print(
            f"🔎 Gate 1 presence: "
            f"{len(presence_passed)}/{len(existence_passed)} candidate(s) confirmed"
            + (
                f" ({_unknown_kept} without a verdict — presence_unknown = keep)"
                if _unknown_kept else ""
            )
        )

        # Stage C dedup: in creative mode a grounded candidate replaces an ungrounded one with the same target (AUTO-FIX).
        accepted: list[CandidateTask] = []
        seen_fingerprints: set[str] = set()
        seen_target_fingerprints: set[str] = set()
        target_fp_index: dict[str, int] = {}

        def _grounded_in_own_target(c: CandidateTask) -> bool:
            return c.cited_location.file in c.target_files

        for c, reason in presence_passed:
            fp = _fingerprint(c)
            tfp = _target_fingerprint(c) if self._task_mode == "creative" else None
            is_fp_dup = fp in seen_fingerprints
            is_tfp_dup = tfp is not None and tfp in seen_target_fingerprints
            if is_fp_dup or is_tfp_dup:
                dup_key = fp if is_fp_dup else tfp
                if (
                    not is_fp_dup and tfp in target_fp_index
                    and _grounded_in_own_target(c)
                    and not _grounded_in_own_target(accepted[target_fp_index[tfp]])
                ):
                    superseded = accepted[target_fp_index[tfp]]
                    accepted[target_fp_index[tfp]] = c
                    # The superseding candidate's full fingerprint is registered too, so a later duplicate is still caught (BUGFIX).
                    seen_fingerprints.add(fp)
                    all_results.append(FilterResult(
                        candidate=superseded, accepted=False, stage="duplicate",
                        reason=(
                            f"superseded by a later candidate with fingerprint "
                            f"{dup_key!r} that is grounded in its own target "
                            f"file ({superseded.cited_location.file!r} is not "
                            f"in {superseded.target_files!r})"
                        ),
                    ))
                    all_results.append(FilterResult(
                        candidate=c, accepted=True, stage="presence", reason=reason,
                    ))
                    logger.info(
                        "Gate1[dedup] replaced ungrounded duplicate %r with "
                        "grounded %r", superseded.title, c.title,
                    )
                    continue
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="duplicate",
                    reason=f"duplicate of an earlier candidate with fingerprint {dup_key!r}",
                ))
                logger.info("Gate1[dedup] merged duplicate %r", c.title)
                continue
            seen_fingerprints.add(fp)
            if tfp is not None:
                seen_target_fingerprints.add(tfp)
                target_fp_index[tfp] = len(accepted)
            accepted.append(c)
            all_results.append(FilterResult(
                candidate=c, accepted=True, stage="presence", reason=reason,
            ))

        rejected = [r for r in all_results if not r.accepted]
        if getattr(self, "_vote", None) is not None:
            c = self._vote.counters
            print(f"🗳 Gate 1 vote: asked {c['votes_asked']}, decided {c['votes_decided']}, "
                  f"overridden by the reader {c['votes_overridden']}, requests {c['votes_requests']}, "
                  f"skipped {c.get('votes_skipped', 0)}"
                  # Also says how many collect facts the voters were shown, not only that the pack was used (CC-13).
                  + (f", collect chunks {c.get(_gate1_vote.COUNTER_COLLECT_CHUNKS, 0)}"
                     if getattr(self._vote.cfg, "vote_collect", "off") == "auto" else "")
                  + (f" — stopped: {self._vote.stopped}" if self._vote.stopped else ""))
        print(
            f"✅ Gate 1 done — {len(accepted)} accepted, "
            f"{len(rejected)} rejected ({len([r for r in rejected if r.stage == 'duplicate'])} duplicate(s))\n"
        )
        # The stage split is computed from all results; a caller without counters gets the pre-M4 return value (M4).
        if counters is not None:
            try:
                counters.update(
                    split_gate1_results(
                        all_results,
                        reask=getattr(self, "presence_reask", 0),
                        unknown=getattr(self, "presence_unknown", 0),
                        non_py=getattr(self, "non_py_requests", 0),
                        empty_transport=getattr(self, "presence_empty_transport", 0),
                        empty_exhausted=getattr(self, "presence_empty_exhausted", 0),
                        nothink_ignored=getattr(self, "presence_nothink_ignored", 0),
                    )
                )
            except Exception:  # noqa: BLE001 — never sink a run on a counter
                pass
            if getattr(self, "_vote", None) is not None:
                # only with the vote on: with it off the dict is what it was
                counters.update(self._vote.counters)
        return accepted, rejected

    # ── Stage A helpers ───────────────────────────────────────────────────────

    def _collect_line(self, base_dir: Path) -> str:
        """CC-13: the one line at the start of Gate 1 that says what the judges had.

        ``collect: fresh | stale (N modules changed, collector version, skip_dirs changed) | stale | present |
        absent — reader: used/not used, vote pack: used/not used`` (a stale line names what of the three the read
        found; a bare ``stale`` is the reader's bridge, which counts nothing). The reader's side is its collect bridge (GATE1-CTX-1/-2,
        Stage A0) and the vote's side is the CC-10 vote's own read of the same artifact
        (`[gate1] vote_collect`), so one line settles "did the judges pull from collect"
        for both of them. Printed by every variant, the vote off included, and built before
        Stage A so the run's log says it at the start rather than never. Nothing here raises:
        a missing artifact, a malformed ``[collect]`` key or a broken read is an ``absent``
        line, never a run that stops.
        """
        state = ""
        changed: "int | None" = None      # unknown until a read says how many files moved
        causes: tuple = ()                # what else made it stale: "collector version", "skip_dirs changed"
        reader_used = False
        try:
            bridge = getattr(self, "_collect_bridge", None)
            reader_used = bool(getattr(bridge, "usable", False))
            state = str(getattr(bridge, "status", "") or "")
        except Exception:   # noqa: BLE001 — the bridge's own state is a line, not a traceback
            state, reader_used = "", False
        vote_used = False
        try:
            vote = getattr(self, "_vote", None)
            read = vote.collect_read(base_dir) if vote is not None else None
            if read is not None:
                vote_used = bool(getattr(read, "used", False))
                if getattr(read, "status", ""):
                    state = str(read.status)
                changed = int(getattr(read, "changed", 0) or 0)
                causes = tuple(str(c) for c in (getattr(read, "causes", ()) or ()))
        except Exception:   # noqa: BLE001 — a read that fails is "not used"
            vote_used = False
        if not state:
            # Reports whether a collect artifact is on disk, without reading it: a read is a full scan of the tree (label only).
            try:
                cl = _gate1_vote._collect_loader()
                state = "present" if cl is not None and cl.artifact_present(base_dir, config=self._config) else "absent"
            except Exception:   # noqa: BLE001
                state = "absent"
        state = state or "absent"
        if state == "stale" and not changed and not causes:
            # The state came from the reader's own bridge, which counts nothing; its model still
            # says why it is stale, and a module Pass B left without a summary (CC-15) used to print
            # as a bare `stale` (HARDEN-6 E).
            try:
                import re as _re
                model = getattr(getattr(self, "_collect_bridge", None), "_model", None)
                reason = str(getattr(model, "reason", "") or "")
                if "unsummarized" in tuple(getattr(model, "stale_causes", ()) or ()):
                    m_files = _re.search(r"(\d+) tracked file", reason)
                    m_sum = _re.search(r"\d+ modules? without a summary", reason)
                    changed = int(m_files.group(1)) if m_files else changed
                    causes = (m_sum.group(0),) if m_sum else causes
            except Exception:   # noqa: BLE001 — a diagnostic line, never a run blocker
                pass
        # A version or skip_dirs change is stale with no file moved, so the line says so (CC-13).
        parts = ([f"{changed} module{'s' if changed != 1 else ''} changed"] if changed else []) + list(causes)
        head = f"stale ({', '.join(parts)})" if state == "stale" and parts else state
        return (f"📦 collect: {head} — reader: {'used' if reader_used else 'not used'}, "
                f"vote pack: {'used' if vote_used else 'not used'}")

    def _vote_on(self, candidate: CandidateTask, base_dir: Path):
        """CC-10: the claim vote on *candidate*, or None when there is no vote or it has no
        decision to offer. Never raises (the vote itself does not)."""
        vote = getattr(self, "_vote", None)
        if vote is None:
            return None
        key = str(Path(base_dir).resolve())
        with self._vote_views_lock:
            view = self._vote_views.get(key)
            if view is None:
                view = self._vote_views[key] = PathRepoView(base_dir)
        out = vote.presence_by_vote(candidate, view)
        return out if out.verdict else None

    def _location_is_indexed(self, candidate: CandidateTask) -> bool:
        """L1 (Stage A0): does the collect model know the file this candidate
        cites?

        MEMBERSHIP, not an extension allowlist: the artifact indexes whatever
        the collector scans (``.py``, ``.java``), so "does the model know this
        path" is the honest question, and a language the collector learns
        tomorrow needs no allowlist change here. ``module_symbols`` answers it
        exactly — no suffix or bare-name fallback — so an ``agents.ini`` that
        genuinely sits in the tree still misses, which is the point.

        V9 wrinkle: a path a task of this run already edited is *indexed*,
        just stale — ``module_symbols`` withholds it, and reading that empty
        answer as "unindexed" would start rejecting ``.py`` files mid-run.
        Dirty is known.

        Fail-open by construction: a missing bridge, an unusable model, a path
        the candidate does not name, or ANY exception means "proceed", never a
        rejection — this gate's savings must never cost a candidate its Stage
        B check.
        """
        bridge = self._collect_bridge
        if bridge is None:
            return True
        try:
            if not bool(getattr(bridge, "usable", False)):
                # A stale or missing learned set asks no membership question, so every candidate reaches Stage B.
                return True
            path = str(getattr(candidate.cited_location, "file", "") or "")
            if not path:
                return True
            dirty = getattr(bridge, "dirty_paths", None)
            if dirty is not None and path in dirty:
                return True
            return bool(bridge.module_symbols(path))
        except Exception:  # noqa: BLE001 — fail open, never block a candidate
            return True

    def _check_existence(
        self,
        candidate: CandidateTask,
        base_dir: Path,
        cluster_files: "dict[str, set[str]] | None" = None,
    ) -> tuple[bool, str, str]:
        """Return (ok, reason, code_block).

        *code_block* is the extracted snippet to send to Stage B (empty string
        on failure).
        """
        loc = candidate.cited_location

        # A new_file path is exempt from the existence checks but must still resolve inside base_dir and not be a directory (AUTO-BUG).
        if loc.new_file:
            candidate_path = (base_dir / loc.file).resolve()
            try:
                candidate_path.relative_to(base_dir.resolve())
            except ValueError:
                return False, f"new_file path escapes base_dir: {loc.file!r}", ""
            if candidate_path.is_dir():
                return False, f"new_file path is an existing directory: {loc.file!r}", ""
            # A new_file path that already exists as a file is rejected, so the coder cannot silently overwrite real content (BUGFIX).
            if candidate_path.is_file():
                return (
                    False,
                    f"new_file={loc.file!r} but this path already exists — "
                    f"not a new file; drop new_file or cite the real content",
                    "",
                )
            return (
                True,
                f"new file (not yet created): {loc.file!r} — existence check skipped",
                "",
            )

        # 0. Cluster membership check — catch hallucinated paths before filesystem I/O.
        if cluster_files is not None:
            known = cluster_files.get(candidate.cluster, set())
            if known and loc.file not in known:
                return (
                    False,
                    f"cited file {loc.file!r} was not in the ingested file list "
                    f"for cluster {candidate.cluster!r} (likely a hallucinated path). "
                    f"Known files: {sorted(known)}",
                    "",
                )

        abs_path = base_dir / loc.file
        # The normal read is contained in base_dir too, so an escaping cited path cannot leak outside content (BUGFIX).
        try:
            abs_path.resolve().relative_to(base_dir.resolve())
        except ValueError:
            return False, f"cited path escapes base_dir: {loc.file!r}", ""

        # 1. File must exist.
        if not abs_path.is_file():
            return False, f"cited file not found: {loc.file!r}", ""

        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read {loc.file!r}: {exc}", ""

        # An extensionless file is not treated as Python; its block is read with the language-neutral search (FIX-2 #13).
        file_ext = Path(loc.file).suffix

        # Docs and creative mode: a file alone counts as grounding, since line numbers are often wrong or missing (AUTO-CR-8).
        if self._task_mode != "code":
            lines = source.splitlines()
            block = "\n".join(lines[: self._max_context_lines])
            return (
                True,
                "file-only citation (creative/docs — line anchors ignored)",
                _truncate(block, self._max_block_chars),
            )

        # 2. Symbol anchor: must be locatable by block_extractor.
        if loc.symbol:
            block = extract_block(source, loc.symbol, file_ext)
            if not block:
                return (
                    False,
                    f"symbol {loc.symbol!r} not found in {loc.file!r}",
                    "",
                )
            return True, "symbol found", _truncate(block, self._max_block_chars)

        # 3. Line range anchor: lines must exist in the file.
        lines = source.splitlines()
        total = len(lines)
        start = loc.line_start  # may be None in docs/creative mode (file-only citation)
        end   = loc.line_end if loc.line_end is not None else start

        # docs and creative files cite only a file, so a truncated head of it is returned.
        if start is None:
            block = "\n".join(lines[: self._max_context_lines])
            return True, "file-only citation (no line range)", _truncate(block, self._max_block_chars)

        if start < 1 or start > total:
            return (
                False,
                f"line_start={start} is out of range (file has {total} lines)",
                "",
            )
        if end < start:
            # An inverted line range is handled as an empty range instead of slicing wrongly.
            return (
                False,
                f"line_end={end} is before line_start={start}",
                "",
            )
        if end > total:
            # A too-large end is clamped rather than rejected, since a slightly-off line is still useful.
            end = total

        # Include some context around the cited range, capped to max_context_lines.
        ctx_start = max(0, start - 1)
        ctx_end   = min(total, end)  # use clamped end, then cap at max_context_lines
        ctx_end   = min(ctx_end, ctx_start + self._max_context_lines)
        block = "\n".join(lines[ctx_start:ctx_end])
        return True, "line range found", _truncate(block, self._max_block_chars)

    # ── Stage B helpers ───────────────────────────────────────────────────────

    def _full_source_for(self, candidate: CandidateTask, base_dir: "Path | None") -> str:
        """AUTO-H2-7 helper. Same read-it-again pattern as
        ``_module_docstring_for`` and for the same reason: keeps
        ``_check_existence``'s tuple contract untouched. Full file text
        (not just the extracted symbol block) so ``config_fallback_note``
        can resolve a one-hop same-file wrapper method's body. Never
        raises: a failure here just means no wrapper resolution, not a
        broken run.
        """
        if base_dir is None:
            return ""
        loc = candidate.cited_location
        if loc.new_file:
            return ""
        try:
            return (base_dir / loc.file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _module_docstring_for(self, candidate: CandidateTask, base_dir: Path) -> str:
        """AUTO-H2-2 helper. Deliberately re-reads the cited file rather
        than threading a return value through ``_check_existence`` — that
        method's 3-tuple ``(ok, reason, code_block)`` signature is used
        directly by existing tests (tests/test_bugfix_review.py) and by
        design should not need to change shape just because Stage B wants
        one more piece of (cheap, optional) context. Never raises: any
        failure here just means no docstring context, not a broken run.
        """
        loc = candidate.cited_location
        if loc.new_file:
            return ""
        try:
            source = (base_dir / loc.file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        # An extensionless file is not treated as Python; its docstring is not parsed as Python (FIX-2 #13).
        file_ext = Path(loc.file).suffix
        try:
            return extract_module_docstring(source, file_ext)
        except Exception:  # pragma: no cover - defensive, see docstring
            return ""

    def _build_grounding_notes(
        self,
        candidate: CandidateTask,
        code_block: str,
        module_docstring: str,
        base_dir: "Path | None",
    ) -> str:
        """AUTO-H2-1/-2/-3/-6: assemble Stage A2's deterministic evidence into
        the text block injected into Stage B's prompt via
        ``{grounding_notes}``.

        Every piece here is *evidence for the LLM to weigh*, never a
        decision by itself — Stage A2 does not reject candidates. See
        ``gate1_grounding.py``'s module docstring for the reasoning: a
        heuristic that misfires and auto-rejects trades a false positive
        for a false negative, which is not obviously a win. Returns ""
        when nothing fires, which renders as a harmless blank line in the
        template — no behavior change for candidates none of this applies to.
        """
        notes: list[str] = []

        if module_docstring:
            notes.append(
                f"Module docstring for this file (context the code block above "
                f"doesn't repeat):\n\"\"\"\n{module_docstring}\n\"\"\"\n"
                f"If that docstring states the code was written on purpose as "
                f"an incorrect/uncovered example for testing some OTHER part "
                f"of the system (not as production logic meant to work "
                f"correctly), that is strong evidence the claimed problem "
                f"should NOT be fixed here even if it is technically present "
                f"— reject in that case. Otherwise this note is just "
                f"background."
            )

        fb_note = config_fallback_note(candidate.instruction, code_block, self._full_source_for(candidate, base_dir))
        if fb_note:
            notes.append(fb_note)

        id_note = intentional_design_note(code_block)
        if id_note:
            notes.append(id_note)

        loc = candidate.cited_location

        th_note = test_helper_note(loc.file, loc.symbol)
        if th_note:
            notes.append(th_note)

        if base_dir is not None and not loc.new_file:
            try:
                tf_note = target_file_context(
                    candidate.target_files, loc.file, loc.symbol,
                    candidate.instruction, base_dir,
                )
            except Exception as exc:  # pragma: no cover - defensive, see docstring
                # Grounding notes are best-effort: a bug in the repo-wide search must not stop Stage B (AUTO-H2-6).
                logger.warning("Gate1._build_grounding_notes: target_file_context failed (%s) — skipping", exc)
                tf_note = None
            if tf_note:
                notes.append(tf_note)

            # A second grounding note for an instruction about a symbol outside the cited file (AUTO-H2-6b).
            try:
                if_note = instruction_file_context(
                    candidate.instruction, loc.file, base_dir,
                    already_noted=bool(tf_note),
                )
            except Exception as exc:  # pragma: no cover - defensive, see docstring
                logger.warning("Gate1._build_grounding_notes: instruction_file_context failed (%s) — skipping", exc)
                if_note = None
            if if_note:
                notes.append(if_note)

        if base_dir is not None:
            try:
                cc_note = callee_context(
                    candidate.instruction, code_block,
                    candidate.cited_location.file, base_dir,
                )
            except Exception as exc:  # pragma: no cover - defensive, see docstring
                # Callee context is best-effort too: Stage B is the check that matters (AUTO-H2-3).
                logger.warning("Gate1._build_grounding_notes: callee_context failed (%s) — skipping", exc)
                cc_note = None
            if cc_note:
                notes.append(cc_note)

        # GATE1-CTX-1: collect-derived static contract for the cited symbol.
        try:
            contract_note = collect_contract_note(self._collect_bridge, loc.symbol)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            logger.warning("Gate1._build_grounding_notes: collect_contract_note failed (%s) — skipping", exc)
            contract_note = None
        if contract_note:
            notes.append(contract_note)

        # Existing test coverage for the cited module, even when the citation is the source file (GATE1-CTX-2).
        try:
            coverage_note = existing_test_coverage_note(self._collect_bridge, loc.file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gate1._build_grounding_notes: existing_test_coverage_note failed (%s) — skipping", exc)
            coverage_note = None
        if coverage_note:
            notes.append(coverage_note)

        # A truncated code block is flagged explicitly, not left to the model (GATE1-CTX-3).
        trunc_note = truncation_safety_note(code_block)
        if trunc_note:
            notes.append(trunc_note)

        if not notes:
            return ""
        return "\n" + "\n\n".join(notes) + "\n"

    def _count(self, name: str, n: int = 1) -> None:
        """M4: bump one of the two stage-split counters. Never raises.

        Thread-safe for the same reason the learned budget window is: with
        `presence_workers > 1` the presence checks run in a pool, where
        `x += 1` loses increments.
        """
        try:
            lock = getattr(self, "_counter_lock", None)
            if lock is None:
                setattr(self, name, int(getattr(self, name, 0)) + n)
                return
            with lock:
                setattr(self, name, int(getattr(self, name, 0)) + n)
        except Exception:  # noqa: BLE001 — a counter must never sink a run
            pass

    def _finish_presence(
        self, confirmed: bool, reason: str, *, reasks: int = 0
    ) -> tuple[bool, str, str]:
        """RUN-5: turn ``confirmed`` + ``reason`` into the three-outcome form
        ``(confirmed, reason, verdict)`` with
        ``verdict ∈ {"confirmed", "rejected", "unknown"}``.

        ``unknown`` is produced only when the ladder (or the outer call retry)
        ended with ``_is_technical_failure(reason)`` true — the provider gave
        no verdict at all, so there is nothing to count as an answer. A model
        that answered ``rejected``, even with an empty ``reason`` field, is
        still ``rejected``; so is a ``confirmed`` verdict the evidence check
        (AUTO-H3) downgraded to rejection for wanting a fabricated quote.

        On ``unknown`` the reason is rewritten to ``UNKNOWN_PRESENCE_REASON``
        with the underlying technical cause and the number of re-asks behind
        it, so the string that travels with the candidate says what happened
        instead of looking like a verdict the model gave. The technical cause
        is preserved verbatim after the prefix, and ``_is_technical_failure``
        recognises the prefix, so plan_validator's AUTO-REMOVE-GUARD-1 still
        treats the result as "no verdict" rather than "already fixed".

        Increments the M4 ``presence_unknown`` counter; ``_count`` never
        raises, so a broken counter cannot sink a run.
        """
        if confirmed:
            return True, reason, _PRESENCE_CONFIRMED
        if not _is_technical_failure(reason):
            return False, reason, _PRESENCE_REJECTED
        self._count("presence_unknown")
        return (
            False,
            f"{UNKNOWN_PRESENCE_REASON} after {reasks} re-ask(s): {reason}",
            _PRESENCE_UNKNOWN,
        )

    def _log_empty_reply(self, title: str, kind: str, meta) -> None:
        """RUN-9: one line per empty reply, with the numbers it was classified
        from. Never raises — a log line must not sink a run."""
        try:
            logger.warning(
                "Gate1._check_presence [%s]: empty reply — kind=%s "
                "finish_reason=%s completion_tokens=%s reasoning_chars=%d "
                "elapsed=%.1fs",
                title, kind,
                getattr(meta, "finish_reason", None),
                getattr(meta, "completion_tokens", None),
                int(getattr(meta, "reasoning_chars", 0) or 0),
                float(getattr(meta, "elapsed", 0.0) or 0.0),
            )
        except Exception:  # noqa: BLE001
            pass

    def _note_reasoning_despite_no_think(self, think: "bool | None", meta) -> None:
        """RUN-9: the no-think request was not honoured by this provider.

        ``reasoning_chars > 0`` on a call that went out with ``think=False``
        means ``reasoning.exclude`` was ignored: the provider thinks
        regardless, an empty reply from it is exhaustion (the budget went
        into the chain-of-thought), and the no-think rung has nothing left to
        switch off. Logged at WARNING once per (base_url, model) per run and
        counted in ``presence_nothink_ignored`` on the same key — that number
        is what decides whether a per-URL cascade of vendor no-think fields
        (``enable_thinking``, ``chat_template_kwargs``, …) is worth writing;
        none is sent from here, that is the HTTP-400 minefield
        AUTO-THINKDEPTH-2 documents.
        """
        if meta is None or think is not False:
            return
        if int(getattr(meta, "reasoning_chars", 0) or 0) <= 0:
            return
        key = (self._presence_base_url, self._presence_model)
        try:
            with self._counter_lock:
                if key in self._nothink_warned:
                    return
                self._nothink_warned.add(key)
                self.presence_nothink_ignored += 1
        except Exception:  # noqa: BLE001 — a counter must never sink a run
            return
        logger.warning(
            "provider streams reasoning_content despite think=false — the "
            "no-think request is not honoured by %s/%s; empty replies from "
            "this model are exhaustion, not transport",
            self._presence_base_url, self._presence_model,
        )

    def _check_presence(
        self,
        candidate: CandidateTask,
        code_block: str,
        *,
        module_docstring: str = "",
        base_dir: "Path | None" = None,
        _sleep_fn=None,
    ) -> tuple[bool, str, str]:
        """Call the LLM to confirm the claimed problem is present.

        Parameters
        ----------
        module_docstring:
            AUTO-H2-2. Top-of-module docstring for the cited file, if any.
            Injected as extra context — see ``block_extractor.extract_module_docstring``.
        base_dir:
            AUTO-H2-3. Project root, used for best-effort one-hop callee
            lookup when the instruction claims a downstream crash. ``None``
            (the default) skips this lookup entirely — every existing
            caller that doesn't pass it gets identical behavior to before
            this parameter existed.
        _sleep_fn:
            Injected in tests to replace ``time.sleep`` with something
            that doesn't actually block — see AUTO-RETRY-BACKOFF-1 below.
            ``None`` (the default) uses the real ``time.sleep``.

        Returns ``(confirmed: bool, reason: str, verdict: str)`` where
        ``verdict ∈ {"confirmed", "rejected", "unknown"}`` (RUN-5).

        Fail-closed: any error → ``(False, reason, verdict)`` after
        ``self._llm_call_retry_max`` retries of the LLM call itself (see
        AUTO-RETRY-BACKOFF-1 below), each separated by a real
        ``self._llm_call_retry_wait_sec``-second pause — a transient
        network/provider outage gets a real chance to clear before the
        candidate is treated as resolved. Since RUN-5 such a failure is a
        third outcome, ``unknown``, not a rejection: the ladder ended
        without the model ever answering, so ``filter()`` decides what to
        do with it via ``[gate1] presence_unknown``.
        """
        loc = candidate.cited_location
        location_str = _location_str(loc)

        # Counts each presence request that cites a non-module file, so the count can be driven to zero (M4).
        try:
            if not str(getattr(loc, "file", "") or "").endswith(".py"):
                self._count("non_py_requests")
        except Exception:  # noqa: BLE001 — a counter must never sink a run
            pass

        grounding_notes = self._build_grounding_notes(
            candidate, code_block, module_docstring, base_dir,
        )
        # presence_deadline_sec bounds the whole check; it is passed only when set, for old stubs.
        _deadline = (time.monotonic() + self._presence_deadline_sec
                     if getattr(self, "_presence_deadline_sec", 0) else None)
        _until = {"deadline": _deadline} if _deadline is not None else {}

        user_msg = _USER_PROMPT_TMPL.format(
            instruction=candidate.instruction,
            location=location_str,
            code_block=code_block,
            grounding_notes=grounding_notes,
        )

        def _call(
            msg: str,
            *,
            max_tokens: "int | None" = None,
            temperature: "float | None" = None,
            think_off: bool = False,
        ) -> "tuple[str, object | None]":
            """One presence call. Returns ``(cleaned_text, meta)`` where
            ``meta`` is the reply's ``CompletionMeta`` — or ``None`` when the
            transport reported none (a test stub): then there is no evidence
            to classify an empty reply with, and it is left to the ladder
            exactly as before RUN-9. ``think_off=True`` is the no-think rung:
            this one call goes out with ``think=False`` whatever the config
            says."""
            _think = False if think_off else self._presence_think
            url, headers, payload = _llm_stream.build_chat_request(
                base_url=self._presence_base_url, api_key=self._presence_api_key,
                model=self._presence_model,
                api_format=self._presence_api_format,
                temperature=self._presence_temperature if temperature is None else temperature,
                max_tokens=self._presence_max_tokens if max_tokens is None else max_tokens,
                system=self._system, user_msg=msg,
                num_ctx=self._presence_num_ctx, think=_think,
                response_format=self._presence_response_format,
                think_effort=self._presence_think_effort,
                # Ask for the trailing usage chunk, which classifies an empty reply; a gateway that rejects it is retried without (RUN-9).
                stream=True,
            )
            _meta_box: list = []
            tracer.event(
                source="gate1",
                target="llm",
                kind="llm_request",
                content=msg, model=self._presence_model,
                params={"model": self._presence_model, "candidate": candidate.title},
            )
            text = _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self._timeout,
                stream=True,
                api_format=self._presence_api_format,
                ssl_context=self._presence_ssl_context,
                on_meta=_meta_box.append,
                **self._retry_kwargs,
                **_until,
            )
            cleaned = strip_think(text)
            meta = _meta_box[0] if _meta_box else None
            self._note_reasoning_despite_no_think(_think, meta)
            tracer.event(
                source="llm",
                target="gate1",
                kind="llm_response",
                content=cleaned, model=self._presence_model,
                params={"candidate": candidate.title},
            )
            return cleaned, meta

        # Retry a plain provider exception with a wait, since brief provider outages outlast one instant retry (AUTO-RETRY-BACKOFF-1).
        sleep = _sleep_fn or time.sleep
        last_exc: "Exception | None" = None
        cleaned = ""
        # The first call starts at the budget that has been answering, not the configured floor (GATE1-LEARN-1).
        _learned = self._learned_max_tokens()
        _initial_tokens = (
            self._presence_max_tokens if _learned is None else _learned
        )
        if _learned is not None and _learned > self._presence_max_tokens:
            logger.info(
                "Gate1._check_presence [%s]: starting at learned "
                "max_tokens=%d (median of %d parseable verdict(s); "
                "configured %d).",
                candidate.title, _learned,
                len(self._unparseable_samples.get(self._presence_model, ())),
                self._presence_max_tokens,
            )
        # An empty reply without budget exhaustion is a transport failure: the same request again after a wait, not the nudge (RUN-9).
        meta = None
        empty_kind: "str | None" = None
        first_empty_kind: "str | None" = None
        _exc_attempt = 0
        _empty_retry = 0
        _initial_call_tokens = None if _learned is None else _initial_tokens
        while True:
            try:
                cleaned, meta = _call(user_msg, max_tokens=_initial_call_tokens)
            except Exception as exc:
                last_exc = exc
                if _exc_attempt >= self._llm_call_retry_max:
                    break
                if _deadline is not None and time.monotonic() >= _deadline:
                    break     # [gate1] presence_deadline_sec: no retry past the bound
                _exc_attempt += 1
                logger.warning(
                    "Gate1._check_presence [%s]: LLM call failed (%s) — "
                    "retrying in %.0fs (attempt %d/%d).",
                    candidate.title, last_exc, self._llm_call_retry_wait_sec,
                    _exc_attempt, self._llm_call_retry_max,
                )
                sleep(self._llm_call_retry_wait_sec)
                continue
            last_exc = None
            empty_kind = None
            if cleaned.strip() == "" and meta is not None:
                empty_kind = _classify_empty_reply(meta, _initial_tokens)
                self._log_empty_reply(candidate.title, empty_kind, meta)
                if first_empty_kind is None:
                    first_empty_kind = empty_kind
                    self._count(
                        "presence_empty_transport"
                        if empty_kind == _EMPTY_KIND_TRANSPORT
                        else "presence_empty_exhausted"
                    )
                if (empty_kind == _EMPTY_KIND_TRANSPORT
                        and _empty_retry < self._presence_empty_retries
                        and (_deadline is None or time.monotonic() < _deadline)):
                    _empty_retry += 1
                    logger.info(
                        "Gate1._check_presence [%s]: empty (transport) — "
                        "re-issuing the same request in %.0fs (retry %d/%d, "
                        "max_tokens=%d, temperature=%.2f, no nudge).",
                        candidate.title, self._llm_call_retry_wait_sec,
                        _empty_retry, self._presence_empty_retries,
                        _initial_tokens, float(self._presence_temperature),
                    )
                    sleep(self._llm_call_retry_wait_sec)
                    continue
            break
        if last_exc is not None:
            # Keep the "LLM call failed:" prefix so this logs as a warning and is recognised as a technical failure (see _is_technical_failure).
            reason = f"LLM call failed: {last_exc} (after {self._llm_call_retry_max} retries)"
            logger.warning("Gate1._check_presence: %s — no verdict obtained", reason)
            tracer.event(
                source="gate1", target="llm", kind="llm_response",
                content=f"[ERROR] {last_exc}", model=self._presence_model,
                params={"candidate": candidate.title},
            )
            # No verdict was received, so the candidate is unknown, not rejected (RUN-5).
            return self._finish_presence(False, reason)

        if empty_kind is not None:
            # An empty reply is described by its stream metadata, not run through the JSON decoder (RUN-9).
            confirmed, reason, unparseable = False, _empty_cause(empty_kind), True
        else:
            confirmed, reason, unparseable = self._parse_presence_response(
                cleaned, candidate.title, code_block=code_block,
            )
        if not unparseable:
            self._record_parseable_budget(_initial_tokens)

        # An unparseable verdict is often a thinking model cut off mid-think: retry with a nudge before failing closed (AUTO-CR-31).
        if unparseable:
            # Whether the ladder started from an empty reply decides the think-off rung; read before the ladder overwrites it (RUN-9).
            _ladder_entered_empty = empty_kind is not None
            nudge = (
                "\n\nIMPORTANT: your previous reply was not valid JSON with a "
                "\"verdict\" field. Reply AGAIN with ONLY a JSON object of the "
                "exact form {\"verdict\": \"confirmed\" or \"rejected\", "
                "\"evidence\": \"...\" or \"\", \"reason\": \"...\"} — no "
                "reasoning, no markdown fences, no other text."
            )
            last_confirmed, last_reason, last_cleaned = confirmed, reason, cleaned
            # The retry ceiling follows the model's real context size, not the configured max_tokens (GATE1-LEARN).
            ctx_ceiling = (
                int(self._presence_num_ctx * self._UNPARSEABLE_TOKENS_CTX_FRACTION)
                if self._presence_num_ctx
                else self._UNPARSEABLE_TOKENS_DEFAULT_CEILING
            )
            _n_temps = len(self._UNPARSEABLE_TEMPERATURES)
            # The learned-budget ladder starts at the first tier not below the budget that already failed (GATE1-LEARN-1).
            _start_tier = 0
            while (
                self._UNPARSEABLE_TOKENS_FLOOR
                * int(self._UNPARSEABLE_TOKENS_STEP_MULT ** _start_tier)
                < _initial_tokens
                and self._UNPARSEABLE_TOKENS_FLOOR
                * int(self._UNPARSEABLE_TOKENS_STEP_MULT ** _start_tier)
                < ctx_ceiling
            ):
                _start_tier += 1
            _max_retries = self._unparseable_max_retries
            _fast = self._unparseable_retry_mode == "fast"
            _hard_cap = self._unparseable_max_tokens_cap or None
            _initial_temp = float(self._presence_temperature)
            _tried: set = {(int(_initial_tokens), round(_initial_temp, 4))}
            # An empty reply pins the tier, since more budget only gives more silence; a pin below the floor is not honoured (GATE1-LEARN-2).
            _pin_floor = self._UNPARSEABLE_TOKENS_FLOOR
            _empty_pin: "int | None" = (
                _initial_tokens
                if _fast and cleaned.strip() == "" and _initial_tokens >= _pin_floor
                else None
            )
            _made = 0
            for attempt in range(1, _max_retries + 1):
                # Every temperature is tried at a token tier before the tier doubles, from one attempt counter (AUTO-RETRY-TEMP-1).
                tier_index = _start_tier + (attempt - 1) // _n_temps
                temp_index = (attempt - 1) % _n_temps
                _uncapped_tokens = (
                    self._UNPARSEABLE_TOKENS_FLOOR
                    * int(self._UNPARSEABLE_TOKENS_STEP_MULT ** tier_index)
                )
                attempt_tokens = min(_uncapped_tokens, ctx_ceiling)
                _capped_by_provider = _hard_cap is not None and attempt_tokens > _hard_cap
                if _capped_by_provider:
                    attempt_tokens = _hard_cap
                attempt_temp = self._UNPARSEABLE_TEMPERATURES[temp_index]
                if _fast:
                    if _empty_pin is not None and attempt_tokens > _empty_pin:
                        attempt_tokens = _empty_pin
                    _key = (int(attempt_tokens), round(float(attempt_temp), 4))
                    if _key in _tried:
                        logger.info(
                            "Gate1._check_presence [%s]: fast mode — "
                            "skipping re-ask %d/%d (max_tokens=%d, "
                            "temperature=%.2f already tried).",
                            candidate.title, attempt, _max_retries,
                            attempt_tokens, attempt_temp,
                        )
                        continue
                    _tried.add(_key)
                if _capped_by_provider and attempt_tokens == _hard_cap:
                    # Logged after the clamps, so the line names the budget actually sent (RUN-9).
                    logger.info(
                        "Gate1._check_presence [%s]: re-ask max_tokens=%d "
                        "capped at unparseable_max_tokens_cap=%d.",
                        candidate.title, attempt_tokens, _hard_cap,
                    )
                if attempt_tokens < _uncapped_tokens and attempt_tokens == ctx_ceiling:
                    # A small num_ctx that silently caps escalation is reported as a warning (AUTO-CTX-CAP-WARN-1).
                    logger.warning(
                        "Gate1._check_presence [%s]: escalation wants "
                        "max_tokens=%d but is capped at %d by "
                        "num_ctx=%s (ceiling = num_ctx * %.1f). If this "
                        "model's real context window is bigger, raise "
                        "num_ctx in the active [api_*] section (or this "
                        "candidate's presence_llm_profile) to let "
                        "escalation actually use it.",
                        candidate.title, _uncapped_tokens, attempt_tokens,
                        self._presence_num_ctx or "unset",
                        self._UNPARSEABLE_TOKENS_CTX_FRACTION,
                    )
                _made += 1
                logger.info(
                    "Gate1._check_presence [%s]: verdict unparseable — "
                    "re-asking (attempt %d/%d, max_tokens=%d, temperature=%.2f). "
                    "raw=%r",
                    candidate.title, attempt, _max_retries,
                    attempt_tokens, attempt_temp, last_cleaned[:120],
                )
                try:
                    cleaned_n, meta = _call(
                        # The "not valid JSON" nudge goes out only after a non-empty reply; a silent model has nothing to correct (RUN-9).
                        user_msg if last_cleaned.strip() == "" else user_msg + nudge,
                        max_tokens=attempt_tokens,
                        temperature=attempt_temp,
                    )
                except Exception as exc:
                    logger.warning(
                        "Gate1._check_presence [%s]: re-ask attempt %d/%d "
                        "failed (%s) — keeping last fail-closed result.",
                        candidate.title, attempt, _max_retries, exc,
                    )
                    return self._finish_presence(
                        last_confirmed, last_reason, reasks=_made,
                    )
                confirmed_n, reason_n, unparseable_n = self._parse_presence_response(
                    cleaned_n, candidate.title, code_block=code_block,
                )
                if not unparseable_n:  # this answer was clear — use it
                    self._record_parseable_budget(attempt_tokens)
                    # Counts a candidate whose answer came from the ladder, whatever the verdict (M4).
                    self._count("presence_reask")
                    return self._finish_presence(confirmed_n, reason_n, reasks=_made)
                last_confirmed, last_reason, last_cleaned = confirmed_n, reason_n, cleaned_n
                if _fast and cleaned_n.strip() == "" and attempt_tokens >= _pin_floor:
                    _empty_pin = attempt_tokens
                empty_kind = None
                if cleaned_n.strip() == "" and meta is not None:
                    # Every empty reply is classified, including those of the ladder, at its own budget.
                    empty_kind = _classify_empty_reply(meta, attempt_tokens)
                    self._log_empty_reply(candidate.title, empty_kind, meta)
                    last_reason = _empty_cause(empty_kind)

            # One more re-ask with thinking off at the pinned budget, when the ladder started on an empty reply (RUN-9).
            if (_ladder_entered_empty and _max_retries > 0
                    and self._presence_think is True):
                _rung_tokens = _empty_pin if _empty_pin is not None else _initial_tokens
                _made += 1
                logger.info(
                    "Gate1._check_presence [%s]: verdict unparseable — "
                    "re-asking (final rung, max_tokens=%d, temperature=%.2f, "
                    "think=off). raw=%r",
                    candidate.title, _rung_tokens, _initial_temp, last_cleaned[:120],
                )
                try:
                    cleaned_n, meta = _call(
                        user_msg if last_cleaned.strip() == "" else user_msg + nudge,
                        max_tokens=_rung_tokens,
                        temperature=_initial_temp,
                        think_off=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "Gate1._check_presence [%s]: final think=off re-ask "
                        "failed (%s) — keeping last fail-closed result.",
                        candidate.title, exc,
                    )
                    return self._finish_presence(
                        last_confirmed, last_reason, reasks=_made,
                    )
                confirmed_n, reason_n, unparseable_n = self._parse_presence_response(
                    cleaned_n, candidate.title, code_block=code_block,
                )
                if not unparseable_n:
                    self._record_parseable_budget(_rung_tokens)
                    self._count("presence_reask")
                    return self._finish_presence(confirmed_n, reason_n, reasks=_made)
                last_confirmed, last_reason, last_cleaned = confirmed_n, reason_n, cleaned_n
                empty_kind = None
                if cleaned_n.strip() == "" and meta is not None:
                    empty_kind = _classify_empty_reply(meta, _rung_tokens)
                    self._log_empty_reply(candidate.title, empty_kind, meta)
                    last_reason = _empty_cause(empty_kind)
            elif _ladder_entered_empty and _max_retries > 0:
                logger.info(
                    "Gate1._check_presence [%s]: no think=off rung — the "
                    "presence call already went out with think=false, "
                    "nothing to switch off.",
                    candidate.title,
                )

            # The cause names the reply's shape (empty by transport, empty by exhaustion, garbled), not the JSON decoder's view (RUN-9).
            _cause = _reply_cause(last_cleaned, last_reason, empty_kind)
            logger.warning(
                "Gate1._check_presence [%s]: verdict still unparseable after "
                "%d re-ask(s) (mode=%s) — no verdict, ending unknown: %s. raw=%r",
                candidate.title, _made, self._unparseable_retry_mode, _cause,
                last_cleaned[:120],
            )
            return self._finish_presence(last_confirmed, _cause, reasks=_made)

        return self._finish_presence(confirmed, reason)

    # ── GATE1-LEARN-1: learned start budget ──────────────────────────────
    def _learned_max_tokens(self) -> "int | None":
        """Median max_tokens of recent parseable verdicts for the current
        presence model, or ``None`` while nothing has been learned (or the
        feature is off). Never below the configured max_tokens."""
        if not self._unparseable_learn:
            return None
        with self._learn_lock:
            samples = list(self._unparseable_samples.get(self._presence_model) or [])
        if not samples:
            return None
        costs = sorted(samples)
        mid = len(costs) // 2
        median = (costs[mid] if len(costs) % 2
                  else (costs[mid - 1] + costs[mid]) // 2)
        return max(int(median), int(self._presence_max_tokens))

    def _record_parseable_budget(self, max_tokens: int) -> None:
        """A verdict parsed at *max_tokens* — remember it (bounded window)."""
        if not self._unparseable_learn:
            return
        with self._learn_lock:
            samples = self._unparseable_samples.setdefault(self._presence_model, [])
            samples.append(int(max_tokens))
            del samples[:-self._unparseable_learn_window]

    def _parse_presence_response(
        self,
        text: str,
        candidate_title: str,
        code_block: str = "",
    ) -> tuple[bool, str, bool]:
        """Parse the LLM's JSON verdict.  Fail-closed on any error.

        Returns ``(confirmed, reason, unparseable)``. ``unparseable`` is
        ``True`` only when the response couldn't be read as a verdict at
        all (bad JSON, wrong shape, unrecognised verdict word) — as opposed
        to a genuine ``"rejected"`` verdict, which is a real answer, not a
        parse failure. Callers use this distinction to re-ask once on
        ``unparseable`` (mirroring AUTO-CR-31's Gate-2 retry) without
        biasing genuinely-rejected candidates toward acceptance.

        AUTO-H3 (evidence check): whenever *code_block* is non-empty (every
        production call site always passes it — the only caller that
        doesn't is a direct-unit-test call with no code to check against),
        a "confirmed" verdict is additionally required to supply a
        non-empty ``"evidence"`` string that actually occurs in
        *code_block* (whitespace-normalised substring match). This check is
        unconditional on the key being present at all — the prompt template
        in this module always asks for "evidence" in its JSON schema, so a
        model that omits the key entirely is exhibiting the exact same
        failure to ground its answer as one that fills it with a fabricated
        quote, and both are treated identically as a downgrade to
        rejection rather than as a formatting nuance eligible for the
        unparseable/re-ask path — the JSON shape was fine, the model just
        couldn't back up its own answer, which is itself the informative
        outcome.
        """
        stripped = text.strip()
        # Strip optional markdown fences.
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            stripped = "\n".join(inner).strip()

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # A raw newline inside a JSON string is accepted by a non-strict parse; the evidence check below still applies (AUTO-CTRLCHAR-1).
            try:
                data = json.loads(stripped, strict=False)
            except json.JSONDecodeError:
                reason = f"JSON decode failed ({exc}) — failing closed"
                logger.warning(
                    "Gate1._parse_presence_response [%s]: %s  raw=%.200s",
                    candidate_title, reason, text,
                )
                return False, reason, True
            else:
                logger.info(
                    "Gate1._parse_presence_response [%s]: strict JSON "
                    "decode failed (%s) but recovered with strict=False "
                    "(literal control character inside a string value, "
                    "e.g. an unescaped newline) — treating as parsed.",
                    candidate_title, exc,
                )

        if not isinstance(data, dict):
            reason = f"expected JSON object, got {type(data).__name__} — failing closed"
            logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, reason)
            return False, reason, True

        # HARDEN-7 B: each field is read as text, whatever JSON type the model
        # sent for it. `(data.get("verdict") or "").strip()` assumed every
        # truthy field was already a string, so `"evidence": ["a", "b"]` raised
        # `AttributeError: 'list' object has no attribute 'strip'` out of
        # `filter()` — and with it the planning phase, every other candidate's
        # verdict and every LLM call already paid for. A field of another type
        # is now text (a list one item per line, a number as its text, None as
        # empty), and a `verdict` that is not a recognisable word falls through
        # to the unparseable path below — the existing re-ask, not an exception.
        # A verdict is a word: a list or a number is no verdict in any spelling (`["confirmed"]` is not a
        # confirmation), so it is the unparseable reply and is asked again, never read as a yes.
        raw_verdict = data.get("verdict")
        verdict  = raw_verdict.strip().lower() if isinstance(raw_verdict, str) else ""
        reason   = reply_text(data.get("reason"))
        evidence = reply_text(data.get("evidence"))

        if verdict == "confirmed":
            if code_block and not _evidence_found(evidence, code_block):
                msg = (
                    "verdict was 'confirmed' but no matching evidence quote "
                    f"was supplied ({evidence!r}) — treating as unsupported, "
                    "failing closed"
                )
                logger.info(
                    "Gate1._parse_presence_response [%s]: %s", candidate_title, msg,
                )
                return False, msg, False
            return True, reason or "LLM confirmed problem is present", False
        if verdict == "rejected":
            return False, reason or "LLM found problem absent or already fixed", False

        # An unrecognised verdict fails closed as a parse failure, so it may be asked again.
        msg = f"unrecognised verdict {verdict!r} — failing closed"
        logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, msg)
        return False, msg, True


# Convenience factory (as in architect.review_clusters)

def filter_candidates(
    candidates: list[CandidateTask],
    base_dir: str | Path,
    config: configparser.ConfigParser,
    cluster_files: "dict[str, set[str]] | None" = None,
    task_mode: str = "code",
    model_override: "str | None" = None,
    active_override: "str | None" = None,
    collect_bridge=None,
    counters: "dict | None" = None,
) -> tuple[list[CandidateTask], list[FilterResult]]:
    """One-call entry point for ``AutoController`` (and ``plan_validator``).

    Reads API settings from *config* (same ``[api]`` / ``[api_local]`` /
    ``[api_remote]`` convention) and delegates to :class:`Gate1Filter`.

    Parameters
    ----------
    candidates:
        Output of :func:`tools.auto.architect.review_clusters`.
    base_dir:
        Root of the project being reviewed.
    config:
        Parsed ``agents.ini``.
    cluster_files:
        Optional mapping of cluster name → set of known relative file paths.
        Built from the ingestor clusters and passed through to Gate1Filter so
        hallucinated paths are caught before any filesystem I/O.
    model_override, active_override:
        AUTO-H2-4. When set, use this model / this ``[api_*]`` profile
        instead of ``[api] active`` and that profile's ``model`` key. Both
        default to ``None`` — the live ``--auto`` call site
        (``tools/auto/pipeline.py``) never passes these, so its behavior is
        completely unchanged by this parameter existing. ``plan_validator``
        is the only caller that ever sets them, and only when
        ``[gate1_validate]`` is present in the config it was given — see
        that module's docstring for why re-verifying with the *same* model
        that proposed a candidate is weaker evidence than an independent
        model would be.
    collect_bridge:
        GATE1-CTX-1/-2. A ``tools.auto.collect_bridge.CollectBridge`` (or
        ``None``, the default) — feeds two additive grounding notes into
        Stage B (collect-derived contracts, existing test coverage). Build
        it once per run (same rule as COLLECT-24's own caller in
        ``AutoController._get_collect_bridge``); never build one per
        candidate.
    counters:
        M4. Filled with the per-stage split of this pass before returning —
        see :meth:`Gate1Filter.filter` for the field list. Forwarded as-is;
        `None` keeps the call exactly as it has always been.

    Returns
    -------
    accepted : list[CandidateTask]
        Candidates that passed Gate 1.
    rejected : list[FilterResult]
        Rejected candidates with stage and reason.
    """
    active    = active_override or config.get("api", "active", fallback="local")
    section   = f"api_{active}"
    base_url  = config.get(section, "base_url")
    api_key   = config.get(section, "api_key",    fallback="")
    model     = model_override or config.get(section, "model")
    api_fmt   = config.get(section, "api_format", fallback="openai")
    verify_ssl = safe_getboolean(config, "api", "verify_ssl", fallback=True)

    filt = Gate1Filter(
        config=config,
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=api_fmt,
        verify_ssl=verify_ssl,
        task_mode=task_mode,
        collect_bridge=collect_bridge,
    )
    return filt.filter(
        candidates, base_dir, cluster_files=cluster_files, counters=counters,
    )


# Internal helpers

# RUN-5: three presence outcomes, not two

# Third presence outcome: the provider never gave a verdict (RUN-5).
_PRESENCE_CONFIRMED = "confirmed"
_PRESENCE_REJECTED = "rejected"
_PRESENCE_UNKNOWN = "unknown"
#: [gate1] presence_deadline_sec when the ini does not set it (R3: half an hour a candidate)
DEFAULT_PRESENCE_DEADLINE_SEC = 1800.0

# Reason for a candidate accepted with no verdict; it is matched so it is not counted as confirmed and not removed from the plan (RUN-5).
UNKNOWN_PRESENCE_REASON = "presence unknown — provider gave no verdict"

# The note appended to a kept-unknown candidate's instruction, so the reason reaches plan.json and the coder (RUN-5).
_PRESENCE_UNKNOWN_NOTE_HEAD = "Gate 1 note: "


def _with_presence_unknown_note(instruction: str, reason: str) -> str:
    """RUN-5: append the ``presence unknown`` reason to *instruction*, once.

    The coder is told to check the claim before touching code — the only
    thing Gate 1 knows about this task is that nobody verified it. Idempotent
    so a `--validate-plan` re-check that ends unknown again does not stack a
    second note.
    """
    marker = _PRESENCE_UNKNOWN_NOTE_HEAD + UNKNOWN_PRESENCE_REASON
    if marker in instruction:
        return instruction
    return (
        instruction.rstrip()
        + f"\n\n{_PRESENCE_UNKNOWN_NOTE_HEAD}{reason}. Gate 1 could not verify "
        "this claim — confirm the described problem is actually present at "
        "the cited location before changing code."
    )


# Empty-reply kinds and their cause strings (transport, exhausted, garbled); all are "no verdict", never a rejection (RUN-9).
_EMPTY_KIND_TRANSPORT = "transport"
_EMPTY_KIND_EXHAUSTED = "exhausted"
_GARBLED_CAUSE = "garbled"
# Completion tokens at this share of max_tokens count as a spent budget, even without finish_reason.
_EXHAUSTED_TOKEN_FRACTION = 0.9


def _empty_cause(kind: str) -> str:
    """RUN-9: the cause string for an empty reply classified as *kind*."""
    return f"empty ({kind})"


def _reply_cause(cleaned: str, reason: str, empty_kind: "str | None") -> str:
    """RUN-9: the cause an unknown presence verdict carries — the shape of
    the last reply. An empty reply says ``empty (transport)`` / ``empty
    (exhausted)`` and nothing else (it was never parsed, so the JSON
    decoder's "Expecting value: line 1 column 1" said nothing true about
    it); a garbled one keeps the parser's own reason, which names what was
    wrong with a reply that did arrive, behind a ``garbled: `` prefix. With
    no classification (the transport reported no metadata) the parse reason
    is kept unchanged."""
    if cleaned.strip() == "":
        return _empty_cause(empty_kind) if empty_kind else reason
    return f"{_GARBLED_CAUSE}: {reason}"


def _classify_empty_reply(meta, max_tokens: "int | None") -> str:
    """RUN-9: ``"exhausted"`` or ``"transport"`` for an empty reply, from its
    ``CompletionMeta``.

    Exhausted when ``finish_reason == "length"``, or ``completion_tokens`` is
    within ``_EXHAUSTED_TOKEN_FRACTION`` of *max_tokens*, or any reasoning
    was streamed; transport otherwise — including the degraded-gateway shape
    ``completion_tokens is None`` with ``finish_reason`` in (None, "stop") and
    zero content chunks. ``None`` tokens never exhaust by themselves. Never
    raises.
    """
    try:
        if getattr(meta, "finish_reason", None) == "length":
            return _EMPTY_KIND_EXHAUSTED
        if int(getattr(meta, "reasoning_chars", 0) or 0) > 0:
            return _EMPTY_KIND_EXHAUSTED
        tokens = getattr(meta, "completion_tokens", None)
        if (isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
                and max_tokens and tokens >= _EXHAUSTED_TOKEN_FRACTION * float(max_tokens)):
            return _EMPTY_KIND_EXHAUSTED
    except Exception:  # noqa: BLE001 — classification must never sink a run
        pass
    return _EMPTY_KIND_TRANSPORT


def _is_technical_failure(reason: str) -> bool:
    """Return True when *reason* names a call/parse failure, not a genuine
    LLM verdict.

    AUTO-LOG-1: ``filter()`` used to log every rejection at WARNING,
    whether it was Gate 1 doing its job (the LLM read the code and
    disagreed with the claim — an ordinary, expected outcome) or a real
    technical hiccup (the call failed, the response wasn't valid JSON).
    Conflating the two made a normal run's console/log look like it was
    full of problems, and made a genuine failure no easier to spot than
    routine business output. This distinguishes the two so callers can
    log at the right level: WARNING for an actual anomaly worth noticing,
    INFO for "the LLM looked and said no" — a state of the *request*
    (confirmed/rejected), not a fault in it.

    Matches the exact reason prefixes ``_check_presence`` /
    ``_parse_presence_response`` produce on failure. A genuine LLM
    "reason" field is free-text from the model and, in practice, never
    coincidentally starts with one of these.
    """
    return reason.startswith((
        "LLM call failed:",
        "JSON decode failed",
        "expected JSON object,",
        "unrecognised verdict ",
        UNKNOWN_PRESENCE_REASON,
        # The causes of an empty or garbled reply are logged, and never count as a rejection (RUN-9).
        "empty (",
        _GARBLED_CAUSE,
    ))


# M4: the per-stage split

# Stage-split columns, always printed in this order so log parsing stays stable (M4).
_GATE1_SPLIT_FIELDS = (
    "existence",
    "presence_confirmed",
    "presence_rejected",
    "presence_fail_closed",
    "presence_unknown",
    "presence_reask",
    # RUN-9: why the empty replies were empty — see Gate1Filter.__init__.
    "presence_empty_transport",
    "presence_empty_exhausted",
    "presence_nothink_ignored",
    "duplicate",
    "non_py",
)

# Acceptance reasons that were never asked of the model; they are not counted as confirmed (M4).
_NON_MODEL_CONFIRMATIONS = (
    "existence check passed",
    "new file —",
    UNKNOWN_PRESENCE_REASON,
)

# A stage the split does not know: a gap in the split, which split_gate1_results warns about.
_UNKNOWN_STAGE = "_unknown_stage"


def gate1_outcome(stage: str, reason: str, accepted: bool) -> str:
    """M4: which split column one Gate-1 outcome belongs to.

    Returns a bucket name, `""` when the outcome is expected to be uncounted
    (an existence-only acceptance — nothing was asked of a model), or
    `_UNKNOWN_STAGE` when the stage is one this split does not know about
    (V12's `already_safe` is the next to land). Keeping the two uncounted
    cases apart is the point: an existence-only acceptance happens on every
    skip_llm run, so warning about it would make the warning meaningless.

    A `presence_unknown` outcome (RUN-5) also returns `""`: its number is
    fed from the filter's `presence_unknown` counter, not from these records,
    because a candidate kept as unknown and then dropped as a duplicate
    leaves only a `duplicate` record behind. Checked before
    `_is_technical_failure` because an unknown reason is one too.
    """
    stage = str(stage or "")
    reason = str(reason or "")
    if stage == "existence":
        return "existence"
    if stage == "duplicate":
        return "duplicate"
    if stage != "presence":
        return _UNKNOWN_STAGE
    if reason.startswith(UNKNOWN_PRESENCE_REASON):
        return ""
    if _is_technical_failure(reason):
        # An unanswered presence check is a fail-closed rejection only when the config says reject; by default it is counted as unknown.
        return "presence_fail_closed"
    if accepted and reason.startswith(_NON_MODEL_CONFIRMATIONS):
        return ""
    if accepted:
        return "presence_confirmed"
    return "presence_rejected"


def split_gate1_results(
    all_results,
    *,
    reask: int = 0,
    unknown: int = 0,
    non_py: int = 0,
    empty_transport: int = 0,
    empty_exhausted: int = 0,
    nothink_ignored: int = 0,
) -> dict:
    """M4: the per-stage split of one Gate-1 pass, as a dict.

    RUN-9 adds `presence_empty_transport`, `presence_empty_exhausted` and
    `presence_nothink_ignored`, counter-fed like `presence_reask`: an empty
    reply leaves no trace in `stage` or `reason` once the candidate has a
    verdict.

    `all_results` is the `all_results` list `filter()` builds — it is the
    only place a `FilterResult` for an *accepted* candidate exists, so the
    split has to be computed where `filter()` computes the verdicts.

    Every `_GATE1_SPLIT_FIELDS` key is always present, zero when the mode does
    not produce it. `presence_reask`, `presence_unknown` and `non_py` come
    from the filter's own counters rather than from any result record: no
    candidate that only ever answered on a re-ask, no candidate the provider
    never answered at all, and no candidate whose cited file is not a module,
    leaves a trace in `stage` or `reason`.

    `""` outcomes are dropped silently — they are the expected case. An
    `_UNKNOWN_STAGE` outcome is counted in `_uncounted` (never in a real
    column) and warned about once, so the next stage added to Gate 1 cannot
    silently vanish from the line. Never raises.
    """
    out = {name: 0 for name in _GATE1_SPLIT_FIELDS}
    out["presence_reask"] = int(reask or 0)
    out["presence_unknown"] = int(unknown or 0)
    out["non_py"] = int(non_py or 0)
    out["presence_empty_transport"] = int(empty_transport or 0)
    out["presence_empty_exhausted"] = int(empty_exhausted or 0)
    out["presence_nothink_ignored"] = int(nothink_ignored or 0)
    unknown: set = set()
    try:
        for r in list(all_results or []):
            bucket = gate1_outcome(
                getattr(r, "stage", ""),
                getattr(r, "reason", ""),
                bool(getattr(r, "accepted", False)),
            )
            if bucket in out:
                out[bucket] += 1
            elif bucket == _UNKNOWN_STAGE:
                unknown.add(str(getattr(r, "stage", "") or "?"))
    except Exception as exc:  # noqa: BLE001 — a counter must never sink a run
        logger.warning(
            "M4 gate1 split: %s: %s — printing what was counted",
            type(exc).__name__, exc,
        )
    out["_uncounted"] = len(unknown)
    if unknown:
        logger.warning(
            "M4 gate1 split: stage(s) %s are not part of this split — their "
            "candidate(s) are not counted in any column of the plan_phase line",
            ", ".join(sorted(unknown)),
        )
    return out


def format_gate1_split(split: dict) -> str:
    """M4: ``existence=3 presence_confirmed=5 …`` for one Gate-1 pass.

    Every field of `_GATE1_SPLIT_FIELDS`, in order, always — including the
    zeros — so a log parser does not have to know which mode produced the
    line. Anything in `split` that is not a known field is dropped rather
    than appended, for the same reason.
    """
    return " ".join(
        f"{name}={int((split or {}).get(name, 0) or 0)}"
        for name in _GATE1_SPLIT_FIELDS
    )


def _fingerprint(c: CandidateTask) -> str:
    """Stable deduplication key for a candidate, based on cited location.

    Two candidates are considered duplicates when they cite the same
    location and have the same normalised title (lowercased, stripped).
    """
    loc = c.cited_location
    anchor = loc.symbol or f"L{loc.line_start}-{loc.line_end}"
    return f"{loc.file}::{anchor}::{c.title.strip().lower()}"


def _target_fingerprint(c: CandidateTask) -> "str | None":
    """AUTO-BUG-3: secondary dedup key based on the file(s) being WRITTEN.

    ``_fingerprint`` alone keys on ``cited_location`` — the SOURCE the
    candidate references — which is the right disambiguator for code tasks
    (two different fixes can legitimately cite the same file at different
    symbols/lines). For creative-generation tasks, though, ``cited_location``
    is largely arbitrary (there's no meaningful symbol/line in a chapter
    that doesn't exist yet) and can differ between two batches of the same
    cluster even when both are proposing to write the exact same target
    file — which is exactly the "two independent tasks generate chapter_4"
    duplication observed in practice.

    AUTO-BUG fix: the key used to also include the normalised title, on the
    theory that "same target + same title" was enough to call it a
    duplicate. In practice two *independent* architect batches essentially
    never phrase a "write chapter_4" task identically — different wording,
    different framing — so titles almost always differ even when the
    target file is the exact same one, and the title-inclusive key silently
    let the very duplication this function exists to catch straight
    through. A chapter file being generated fresh has no legitimate reason
    for two independent tasks to both target it in the same run (unlike
    code, where "same file, same title" undersells how differently two
    code fixes on one file can legitimately coexist) — so for creative
    mode the target file set alone is a safe, sufficient duplicate key.
    Returns ``None`` when there are no target files to key on.
    """
    if not c.target_files:
        return None
    return "|".join(sorted(c.target_files))


def _location_str(loc: Any) -> str:
    """Human-readable location string for the LLM prompt."""
    parts = [loc.file]
    if loc.symbol:
        parts.append(f"symbol={loc.symbol!r}")
    if loc.line_start is not None:
        parts.append(f"lines {loc.line_start}–{loc.line_end or loc.line_start}")
    return ", ".join(parts)


def _truncate(text: str, max_chars: int = _DEFAULT_MAX_BLOCK_CHARS) -> str:
    """Truncate *text* to *max_chars*, appending a notice when clipped."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated — {len(text) - max_chars} more chars]"


# Minimum length of a quote that counts as evidence, so a trivial token cannot satisfy the requirement (AUTO-H3).
_MIN_EVIDENCE_CHARS = 8


def _evidence_found(evidence: str, code_block: str) -> bool:
    """Whitespace-normalised substring check: does *evidence* actually
    occur in *code_block*?

    Deliberately simple (no fuzzy/edit-distance matching): the point is to
    catch a model asserting "confirmed" with nothing behind it, not to be
    lenient about minor formatting drift. A model that read the code
    carefully enough to ground its verdict can copy a real line closely
    enough to survive whitespace normalisation; a model that's pattern-
    matching on the instruction text usually can't produce anything that
    passes even this loose a bar.

    Note this is a presence check, not a relevance check: it confirms the
    quote is real, not that it actually supports the specific claim. It
    closes the "confirmed with a fabricated citation" hole; it does not by
    itself prove the citation is on-topic — that judgment still rests with
    the LLM and the surrounding grounding notes.
    """
    ev = " ".join(evidence.split())
    if len(ev) < _MIN_EVIDENCE_CHARS:
        return False
    block = " ".join(code_block.split())
    return ev in block