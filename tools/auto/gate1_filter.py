"""tools/auto/gate1_filter.py — AUTO-B3: Gate 1 false-positive filter.

For each candidate produced by AUTO-B2 (ClusterReviewer), this module
performs a two-stage static grounding check *before* the task ever enters
the Coder loop:

Stage A — Existence check (no LLM, instant):
  1. The cited file exists under base_dir.
  2. If a symbol is cited, ``block_extractor.extract_block`` finds it.
  3. If only a line range is cited, those line numbers are within the file.

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
system        — override the built-in system prompt (optional)
skip_llm      — "true" to run existence checks only, skip LLM stage (testing)

agents.ini [api] / [api_local] / [api_remote] supply the same connection
keys used everywhere else in this codebase.
"""

from __future__ import annotations

import configparser
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.agent_trace import tracer
from tools.auto.architect import CandidateTask
from tools.auto.gate1_grounding import (
    callee_context, config_fallback_note, target_file_context,
    collect_contract_note, existing_test_coverage_note, truncation_safety_note,
    intentional_design_note, test_helper_note,
)
from tools.block_extractor import extract_block, extract_module_docstring
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think

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

# {instruction} — the candidate's instruction (problem description)
# {location}    — human-readable location string
# {code_block}  — the actual code at the cited location
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

# _MAX_CONTEXT_LINES and _MAX_BLOCK_CHARS are now read from [gate1] in
# agents.ini (max_context_lines / max_block_chars).
# These defaults apply only when the keys are absent from the config.
_DEFAULT_MAX_CONTEXT_LINES = 60
_DEFAULT_MAX_BLOCK_CHARS   = 4000


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FilterResult:
    """Outcome record for one candidate after Gate 1.

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


# ─────────────────────────────────────────────────────────────────────────────
# Gate1Filter
# ─────────────────────────────────────────────────────────────────────────────

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

    # Max extra nudge-retries when the presence-check verdict comes back
    # unparseable (bad JSON / missing "verdict" field), before failing
    # closed on the task. Previously hardcoded to a single retry.
    _UNPARSEABLE_MAX_RETRIES = 5

    # Empty raw='' responses on retry are usually a thinking model (e.g.
    # qwen3) burning its whole max_tokens budget on <think> and never
    # reaching the JSON answer. Each nudge attempt doubles the token
    # budget so there's real headroom left after the thinking trace, and
    # nudges temperature down a little to reduce rambling.
    #
    # The floor and ceiling are NOT derived from self._max_tokens: a
    # small configured max_tokens (e.g. 512) is exactly the thing causing
    # the truncation, so multiplying it stays small too (512 -> 768 ->
    # 1024 -> ... was observed live and never escaped the failure). The
    # ceiling instead tracks self._num_ctx (the model's actual context
    # profile — e.g. 128k), since that's the real budget available.
    _UNPARSEABLE_TOKENS_FLOOR = 4096
    _UNPARSEABLE_TOKENS_STEP_MULT = 2.0
    _UNPARSEABLE_TOKENS_DEFAULT_CEILING = 32768
    _UNPARSEABLE_TOKENS_CTX_FRACTION = 0.5
    _UNPARSEABLE_TEMPERATURE_STEP = 0.1
    _UNPARSEABLE_TEMPERATURE_FLOOR = 0.0

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
        # GATE1-CTX-1/-2: tools.auto.collect_bridge.CollectBridge or None.
        # Feeds two additive grounding notes (collect contracts, existing
        # test coverage) in _build_grounding_notes — see that method.
        self._collect_bridge = collect_bridge

        sec = "gate1"
        self._temperature    = float(config.get(sec, "temperature", fallback="0.0"))
        self._max_tokens     = int(config.get(sec, "max_tokens",   fallback="512"))
        self._skip_llm       = config.getboolean(sec, "skip_llm", fallback=False)
        self._timeout        = float(config.get("loop", "timeout_seconds", fallback="300"))
        self._max_context_lines = int(config.get(sec, "max_context_lines", fallback=str(_DEFAULT_MAX_CONTEXT_LINES)))
        self._max_block_chars   = int(config.get(sec, "max_block_chars",   fallback=str(_DEFAULT_MAX_BLOCK_CHARS)))
        # AUTO-FIX: Gate 1's presence check wants a tiny, deterministic JSON
        # verdict — no reasoning needed in the reply. A thinking model (e.g.
        # qwen3) wraps its answer in <think>...</think> by default; with a
        # small max_tokens that reasoning can consume the whole budget and
        # truncate before any JSON is emitted, so strip_think() discards
        # everything and every candidate fails closed. Default to disabling
        # thinking for Gate 1's call (Ollama "think" field); an explicit
        # [gate1] think = true in agents.ini re-enables it.
        self._think = config.getboolean(sec, "think", fallback=False)
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        _active = config.get("api", "active", fallback="local")
        self._num_ctx = config.getint(f"api_{_active}", "num_ctx", fallback=0)

        # ── DM-3: select system prompt based on task_mode + ini overrides ─────
        # Priority: mode-specific ini key > legacy "system" key > built-in constant.
        mode_ini_key = f"system_{task_mode}" if task_mode != "code" else None
        if mode_ini_key and config.has_option(sec, mode_ini_key):
            self._system = config.get(sec, mode_ini_key).strip()
        else:
            built_in = _SYSTEM_PROMPTS.get(task_mode, _SYSTEM_PROMPT_CODE)
            self._system = config.get(sec, "system", fallback=built_in).strip()

        self._init_presence_provider(config, sec, base_url, api_key, model,
                                      api_format, verify_ssl)

    # GATE1-PROVIDER-2: split out of __init__ (rather than inlined) so the
    # provider-selection logic — the part with the actual "which section
    # wins" decisions — has one clear entry/exit point to read and to test
    # against, instead of being interleaved with the dozen unrelated
    # [gate1] scalar reads above it.
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
          and this instance's own ``[gate1]`` ``think``/``temperature``/
          ``max_tokens``/``num_ctx``. Byte-for-byte the pre-existing
          behaviour; nothing about a config file without this key changes.

        - **Set to a section name** (e.g. ``presence_llm_profile = gate1_llm``)
          — presence-check calls instead use THAT section's own
          ``base_url``/``api_key``/``model`` (required — raises a clear
          ``ValueError`` naming the missing key(s) rather than silently
          falling back to the shared provider, since silently reusing a
          different provider's credential against a URL it was never meant
          for is a worse failure than a loud one at startup) and
          ``api_format``/``verify_ssl`` (optional, default ``"openai"`` /
          ``True``).

          ``think``/``temperature``/``max_tokens``/``num_ctx`` are each read
          from the PROFILE section first; any one of them the profile
          doesn't set falls back to THIS instance's own ``[gate1]`` value —
          never to a hardcoded default and never to a different profile's
          value. This is the crux of the "don't conflict with another
          model's saved settings" requirement: a profile tuned for a
          thinking-capable model (``think = true``) and the default
          non-thinking ``[gate1]`` setting coexist without either bleeding
          into the other, and two DIFFERENT profiles never share settings
          with each other either — each is read independently, from its own
          section, every time.

        The module-level ``(url, model)``-keyed "does this endpoint accept
        the `reasoning` field" memory (see ``tools.llm_stream.
        mark_reasoning_field_unsupported``) already gives the presence
        profile's own (possibly different) url+model an independent verdict
        from whatever the shared provider's url+model has recorded — no
        extra wiring needed here for that part.
        """
        profile_name = config.get(sec, "presence_llm_profile", fallback="").strip()
        if not profile_name:
            self._presence_base_url    = self._base_url
            self._presence_api_key     = self._api_key
            self._presence_model       = self._model
            self._presence_api_format  = self._api_format
            self._presence_ssl_context = self._ssl_context
            self._presence_think       = self._think
            self._presence_temperature = self._temperature
            self._presence_max_tokens  = self._max_tokens
            self._presence_num_ctx     = self._num_ctx
            return

        if not config.has_section(profile_name):
            raise ValueError(
                f"[gate1] presence_llm_profile = {profile_name!r} but the "
                f"config has no [{profile_name}] section. Add one with at "
                f"least base_url/api_key/model, or remove "
                f"presence_llm_profile to use the shared provider."
            )
        required = [k for k in ("base_url", "api_key", "model")
                    if not config.has_option(profile_name, k)]
        if required:
            raise ValueError(
                f"[{profile_name}] (gate1 presence_llm_profile) is missing "
                f"required option(s): {', '.join(required)}. A presence "
                f"profile must fully specify its own connection details — "
                f"it never silently inherits base_url/api_key/model from "
                f"the shared provider, since that could send a different "
                f"provider's credential to the wrong host."
            )
        self._presence_base_url   = config.get(profile_name, "base_url").rstrip("/")
        self._presence_api_key    = config.get(profile_name, "api_key")
        self._presence_model      = config.get(profile_name, "model")
        self._presence_api_format = config.get(profile_name, "api_format", fallback="openai")
        _verify_ssl = config.getboolean(profile_name, "verify_ssl", fallback=verify_ssl)
        self._presence_ssl_context = (
            _llm_stream.make_unverified_context() if not _verify_ssl else None
        )
        # Each falls back to THIS instance's own [gate1] value (self._think
        # etc., already resolved above) when the profile doesn't set it —
        # deliberately never a hardcoded True/False/0 default, and never
        # another profile's value: fallback is always "what [gate1] itself
        # says", scoped to this one Gate1Filter instance.
        self._presence_think = config.getboolean(
            profile_name, "think", fallback=self._think)
        self._presence_temperature = float(config.get(
            profile_name, "temperature", fallback=str(self._temperature)))
        self._presence_max_tokens = int(config.get(
            profile_name, "max_tokens", fallback=str(self._max_tokens)))
        self._presence_num_ctx = config.getint(
            profile_name, "num_ctx", fallback=self._num_ctx)

    # ── Public API ────────────────────────────────────────────────────────────

    def filter(
        self,
        candidates: list[CandidateTask],
        base_dir: str | Path,
        cluster_files: "dict[str, set[str]] | None" = None,
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

        Returns
        -------
        accepted : list[CandidateTask]
            Candidates that passed existence + presence checks and are unique.
        rejected : list[FilterResult]
            Every candidate that was dropped, with the stage and reason logged.
        """
        base_dir = Path(base_dir)
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
                # AUTO-LOG-1: existence has no "the call failed" failure mode
                # of its own (no LLM, no network) — a rejection here always
                # means the citation genuinely doesn't resolve (or, with
                # cluster_files, was hallucinated), which is Gate 1 doing
                # its job, not an anomaly. INFO, not WARNING.
                logger.info(
                    "Gate1[existence] REJECTED %r — %s", c.title, reason,
                )

        print(
            f"\n🔎 Gate 1 existence: "
            f"{len(existence_passed)}/{len(candidates)} candidate(s) passed"
        )

        # ── Stage B: LLM problem-presence check ───────────────────────────────
        presence_passed: list[tuple[CandidateTask, str]] = []  # (task, reason)

        if self._skip_llm or self._task_mode == "creative":
            # AUTO-CR-8: Stage B verifies a claimed issue is present in existing
            # text — an improvement-detector — but for creative GENERATION the
            # target chapter is new/empty, so "is the issue present?" is
            # meaningless and would reject every task. Creative quality is
            # governed downstream by the soft Gate-2 (CR-2) and canon gate
            # (CR-7) instead, so existence is sufficient here.
            _why = "LLM skipped" if self._skip_llm else "creative mode — existence only"
            presence_passed = [(c, f"existence check passed ({_why})") for c, _ in existence_passed]
        else:
            n_presence = len(existence_passed)
            for i, (c, block) in enumerate(existence_passed, 1):
                if c.cited_location.new_file:
                    # AUTO-BUG (new_file): same reasoning as AUTO-CR-8 above —
                    # a file that does not exist yet has no existing content
                    # to check problem-presence against. "Is this problem
                    # present in: [nothing]" is meaningless here and would
                    # incorrectly reject every legitimate new-file task.
                    presence_passed.append(
                        (c, "new file — existence check sufficient"))
                    continue
                print(f"  [{i}/{n_presence}] presence check: {c.title}")
                module_docstring = self._module_docstring_for(c, base_dir)
                ok, reason = self._check_presence(
                    c, block, module_docstring=module_docstring, base_dir=base_dir,
                )
                if ok:
                    presence_passed.append((c, reason))
                    # AUTO-LOG-1: symmetric with REJECTED below — a
                    # confirmation used to be entirely silent (no log line
                    # at all), which made "why don't I see the ones that
                    # passed?" a fair question with no answer. Same level
                    # (INFO) as a genuine rejection: both are ordinary
                    # outcomes of the same check.
                    logger.info(
                        "Gate1[presence] CONFIRMED %r — %s", c.title, reason,
                    )
                else:
                    all_results.append(FilterResult(
                        candidate=c, accepted=False, stage="presence", reason=reason,
                    ))
                    # AUTO-LOG-1: only an actual call/parse failure (see
                    # _is_technical_failure) is a WARNING — that's a real
                    # anomaly (network hiccup, malformed response) worth
                    # standing out from routine output. An LLM reading the
                    # code and genuinely disagreeing with the claim is
                    # Gate 1 working correctly, logged at INFO like its
                    # CONFIRMED counterpart just above.
                    if _is_technical_failure(reason):
                        logger.warning(
                            "Gate1[presence] REJECTED %r — %s", c.title, reason,
                        )
                    else:
                        logger.info(
                            "Gate1[presence] REJECTED %r — %s", c.title, reason,
                        )

        print(
            f"🔎 Gate 1 presence: "
            f"{len(presence_passed)}/{len(existence_passed)} candidate(s) confirmed"
        )

        # ── Stage C: deduplication ────────────────────────────────────────────
        accepted: list[CandidateTask] = []
        seen_fingerprints: set[str] = set()
        seen_target_fingerprints: set[str] = set()

        for c, reason in presence_passed:
            fp = _fingerprint(c)
            tfp = _target_fingerprint(c) if self._task_mode == "creative" else None
            if fp in seen_fingerprints or (tfp is not None and tfp in seen_target_fingerprints):
                dup_key = fp if fp in seen_fingerprints else tfp
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="duplicate",
                    reason=f"duplicate of an earlier candidate with fingerprint {dup_key!r}",
                ))
                logger.info("Gate1[dedup] merged duplicate %r", c.title)
                continue
            seen_fingerprints.add(fp)
            if tfp is not None:
                seen_target_fingerprints.add(tfp)
            accepted.append(c)
            all_results.append(FilterResult(
                candidate=c, accepted=True, stage="presence", reason=reason,
            ))

        rejected = [r for r in all_results if not r.accepted]
        print(
            f"✅ Gate 1 done — {len(accepted)} accepted, "
            f"{len(rejected)} rejected ({len([r for r in rejected if r.stage == 'duplicate'])} duplicate(s))\n"
        )
        return accepted, rejected

    # ── Stage A helpers ───────────────────────────────────────────────────────

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

        # AUTO-BUG: new_file candidates declare that this path does not exist
        # yet and this task is what creates it — the existence/hallucination
        # checks below exist to catch citations of content that was never
        # real, which does not apply here. Still confirm the path resolves
        # inside base_dir (no ../ escape) and isn't already a directory.
        if loc.new_file:
            candidate_path = (base_dir / loc.file).resolve()
            try:
                candidate_path.relative_to(base_dir.resolve())
            except ValueError:
                return False, f"new_file path escapes base_dir: {loc.file!r}", ""
            if candidate_path.is_dir():
                return False, f"new_file path is an existing directory: {loc.file!r}", ""
            # BUGFIX: new_file's entire premise is "this path does not exist
            # yet and this task creates it" — that's *why* the checks below
            # (which exist to catch citations of content that was never
            # real) are skipped, and why Stage B is handed an empty block
            # instead of the file's actual content. If the architect (or a
            # confused/hallucinating model) marks new_file=true for a path
            # that already exists as a regular file, none of that logic
            # holds: the coder would still just overwrite target_files
            # normally, but now with zero visibility into what's already
            # there — silently clobbering real content instead of raising a
            # legitimate "already exists" existence failure the way an
            # ordinary (non-new_file) citation of that same path would.
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

        # 1. File must exist.
        if not abs_path.is_file():
            return False, f"cited file not found: {loc.file!r}", ""

        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read {loc.file!r}: {exc}", ""

        file_ext = Path(loc.file).suffix or ".py"

        # AUTO-CR-8: in docs/creative mode a FILE alone is sufficient grounding,
        # since small models often hallucinate line_start and the target
        # chapter may be new/empty, which would make strict line-range
        # validation wrongly reject every candidate. So treat the citation as
        # file-only and hand Stage B a head-of-file block (possibly empty).
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

        # docs/creative: file-only citation — no symbol, no line range.
        # Return a truncated head of the file so Stage B still has context.
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
            # Inverted range (line_end before line_start) would slice to an
            # empty block yet still be reported as "line range found".  Reject
            # it as a real existence failure instead.
            return (
                False,
                f"line_end={end} is before line_start={start}",
                "",
            )
        if end > total:
            # Clamp a too-large end rather than reject — a slightly-off end
            # line is still useful.
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
        file_ext = Path(loc.file).suffix or ".py"
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
                # AUTO-H2-6, same contract as AUTO-H2-3: best-effort context
                # enrichment must never take down Stage B over a bug in the
                # repo-wide search.
                logger.warning("Gate1._build_grounding_notes: target_file_context failed (%s) — skipping", exc)
                tf_note = None
            if tf_note:
                notes.append(tf_note)

        if base_dir is not None:
            try:
                cc_note = callee_context(
                    candidate.instruction, code_block,
                    candidate.cited_location.file, base_dir,
                )
            except Exception as exc:  # pragma: no cover - defensive, see docstring
                # AUTO-H2-3 is best-effort context enrichment. A bug in the
                # repo-wide search must never take down Stage B, which is
                # the actual check that matters here.
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

        # GATE1-CTX-2: existing test coverage for the cited module, from
        # collect's test_map — surfaces coverage even when the citation
        # itself is the SOURCE file, not a test file.
        try:
            coverage_note = existing_test_coverage_note(self._collect_bridge, loc.file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gate1._build_grounding_notes: existing_test_coverage_note failed (%s) — skipping", exc)
            coverage_note = None
        if coverage_note:
            notes.append(coverage_note)

        # GATE1-CTX-3: flag a truncated code_block explicitly rather than
        # relying on the model to notice the in-band "...[truncated]" marker.
        trunc_note = truncation_safety_note(code_block)
        if trunc_note:
            notes.append(trunc_note)

        if not notes:
            return ""
        return "\n" + "\n\n".join(notes) + "\n"

    def _check_presence(
        self,
        candidate: CandidateTask,
        code_block: str,
        *,
        module_docstring: str = "",
        base_dir: "Path | None" = None,
    ) -> tuple[bool, str]:
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

        Returns (confirmed: bool, reason: str).
        Fail-closed: any error → (False, reason), after one plain retry of
        the LLM call itself (see AUTO-FIX comment below) — a transient
        network/provider failure gets the same one-retry courtesy as an
        unparseable verdict before the candidate is treated as resolved.
        """
        loc = candidate.cited_location
        location_str = _location_str(loc)

        grounding_notes = self._build_grounding_notes(
            candidate, code_block, module_docstring, base_dir,
        )

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
        ) -> str:
            url, headers, payload = _llm_stream.build_chat_request(
                base_url=self._presence_base_url, api_key=self._presence_api_key,
                model=self._presence_model,
                api_format=self._presence_api_format,
                temperature=self._presence_temperature if temperature is None else temperature,
                max_tokens=self._presence_max_tokens if max_tokens is None else max_tokens,
                system=self._system, user_msg=msg,
                num_ctx=self._presence_num_ctx, think=self._presence_think,
            )
            tracer.event(
                source="gate1",
                target="llm",
                kind="llm_request",
                content=msg,
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
            )
            cleaned = strip_think(text)
            tracer.event(
                source="llm",
                target="gate1",
                kind="llm_response",
                content=cleaned,
                params={"candidate": candidate.title},
            )
            return cleaned

        try:
            cleaned = _call(user_msg)
        except Exception as exc:
            # AUTO-FIX (found live: agents_128k.ini + kenari.id returned
            # HTTP 400 "upstream_rejected" for three consecutive candidates
            # within one second — a provider/config-level hiccup, not a
            # judgment that the code was already fixed). Previously this
            # branch had ZERO retries while the sibling "unparseable verdict"
            # branch below already got one — an inconsistency with real
            # consequences: a transient provider error permanently removed
            # a possibly-still-valid task from plan.json and recorded it in
            # IMPROVEMENTS-FALSE.md, indistinguishable from a genuine
            # "already fixed" rejection. One plain retry (same request, no
            # nudge needed — this isn't a formatting problem) gives a
            # one-off network/provider blip a chance to clear before we
            # treat the candidate as resolved.
            logger.warning(
                "Gate1._check_presence [%s]: LLM call failed (%s) — "
                "retrying once before failing closed.", candidate.title, exc,
            )
            try:
                cleaned = _call(user_msg)
            except Exception as exc2:
                # NOTE: must keep the exact "LLM call failed:" prefix (see
                # _is_technical_failure below) so this still logs at WARNING
                # like any other real anomaly, not INFO like a routine
                # rejection — the retry count is appended after the colon,
                # not spliced into the matched prefix itself.
                reason = f"LLM call failed: {exc2} (after 1 retry)"
                logger.warning("Gate1._check_presence: %s — failing closed", reason)
                tracer.event(
                    source="gate1", target="llm", kind="llm_response",
                    content=f"[ERROR] {exc2}", params={"candidate": candidate.title},
                )
                return False, reason

        confirmed, reason, unparseable = self._parse_presence_response(
            cleaned, candidate.title, code_block=code_block,
        )

        # AUTO-CR-31-style re-ask: an unparseable verdict (bad JSON, wrong
        # shape, unrecognised verdict word — as opposed to a genuine
        # "rejected") is often a thinking-model (e.g. qwen3) truncated
        # mid-<think> by a small max_tokens, not an actual answer. Retry
        # with a hard nudge up to _UNPARSEABLE_MAX_RETRIES extra calls
        # before falling back to fail-closed.
        if unparseable:
            nudge = (
                "\n\nIMPORTANT: your previous reply was not valid JSON with a "
                "\"verdict\" field. Reply AGAIN with ONLY a JSON object of the "
                "exact form {\"verdict\": \"confirmed\" or \"rejected\", "
                "\"evidence\": \"...\" or \"\", \"reason\": \"...\"} — no "
                "reasoning, no markdown fences, no other text."
            )
            last_confirmed, last_reason, last_cleaned = confirmed, reason, cleaned
            # Ceiling tracks the model's real context profile (num_ctx),
            # not the (possibly tiny) configured max_tokens — e.g. a
            # 128k-context profile should be allowed to actually use a
            # meaningful chunk of that on a stuck retry, not just a few
            # hundred tokens more than a small default.
            ctx_ceiling = (
                int(self._presence_num_ctx * self._UNPARSEABLE_TOKENS_CTX_FRACTION)
                if self._presence_num_ctx
                else self._UNPARSEABLE_TOKENS_DEFAULT_CEILING
            )
            base_temp = self._presence_temperature
            for attempt in range(1, self._UNPARSEABLE_MAX_RETRIES + 1):
                # Doubling from a healthy floor: attempt 1 already jumps
                # straight to the floor (covers the common "starved on a
                # small configured max_tokens" case in one shot), then
                # doubles each further attempt, capped at ctx_ceiling.
                attempt_tokens = min(
                    self._UNPARSEABLE_TOKENS_FLOOR
                    * int(self._UNPARSEABLE_TOKENS_STEP_MULT ** (attempt - 1)),
                    ctx_ceiling,
                )
                attempt_temp = max(
                    base_temp - attempt * self._UNPARSEABLE_TEMPERATURE_STEP,
                    self._UNPARSEABLE_TEMPERATURE_FLOOR,
                )
                logger.info(
                    "Gate1._check_presence [%s]: verdict unparseable — "
                    "re-asking (attempt %d/%d, max_tokens=%d, temperature=%.2f). "
                    "raw=%r",
                    candidate.title, attempt, self._UNPARSEABLE_MAX_RETRIES,
                    attempt_tokens, attempt_temp, last_cleaned[:120],
                )
                try:
                    cleaned_n = _call(
                        user_msg + nudge,
                        max_tokens=attempt_tokens,
                        temperature=attempt_temp,
                    )
                except Exception as exc:
                    logger.warning(
                        "Gate1._check_presence [%s]: re-ask attempt %d/%d "
                        "failed (%s) — keeping last fail-closed result.",
                        candidate.title, attempt, self._UNPARSEABLE_MAX_RETRIES, exc,
                    )
                    return last_confirmed, last_reason
                confirmed_n, reason_n, unparseable_n = self._parse_presence_response(
                    cleaned_n, candidate.title, code_block=code_block,
                )
                if not unparseable_n:  # this answer was clear — use it
                    return confirmed_n, reason_n
                last_confirmed, last_reason, last_cleaned = confirmed_n, reason_n, cleaned_n

            logger.warning(
                "Gate1._check_presence [%s]: verdict still unparseable after "
                "%d retries — failing closed. raw=%r",
                candidate.title, self._UNPARSEABLE_MAX_RETRIES, last_cleaned[:120],
            )
            return last_confirmed, last_reason

        return confirmed, reason

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
            reason = f"JSON decode failed ({exc}) — failing closed"
            logger.warning(
                "Gate1._parse_presence_response [%s]: %s  raw=%.200s",
                candidate_title, reason, text,
            )
            return False, reason, True

        if not isinstance(data, dict):
            reason = f"expected JSON object, got {type(data).__name__} — failing closed"
            logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, reason)
            return False, reason, True

        verdict  = (data.get("verdict")  or "").strip().lower()
        reason   = (data.get("reason")   or "").strip()
        evidence = (data.get("evidence") or "").strip()

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

        # Unrecognised verdict → fail closed, but this is a parse failure,
        # not a genuine rejection — eligible for re-ask.
        msg = f"unrecognised verdict {verdict!r} — failing closed"
        logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, msg)
        return False, msg, True


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory (mirrors architect.review_clusters pattern)
# ─────────────────────────────────────────────────────────────────────────────

def filter_candidates(
    candidates: list[CandidateTask],
    base_dir: str | Path,
    config: configparser.ConfigParser,
    cluster_files: "dict[str, set[str]] | None" = None,
    task_mode: str = "code",
    model_override: "str | None" = None,
    active_override: "str | None" = None,
    collect_bridge=None,
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
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

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
    return filt.filter(candidates, base_dir, cluster_files=cluster_files)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

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
    ))


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


# AUTO-H3: minimum evidence length before a quote counts as "real". Guards
# against a model satisfying the letter of the requirement with something
# trivially present everywhere (a single keyword like "def", a stray
# punctuation mark) instead of an actual line that supports the verdict.
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