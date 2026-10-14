"""tools/auto/architect.py — AUTO-B2: Cluster review → candidate tasks.

For each repo cluster produced by AUTO-B1 (RepoIngestor), sends one Architect
LLM call that returns a list of candidate improvement tasks.  Every candidate
MUST cite a concrete file + symbol/line range; candidates without grounding are
rejected at parse time before they ever reach Gate 1 (AUTO-B3).

Public surface consumed by controller.py / the Architect stage:

    from tools.auto.architect import ClusterReviewer, CandidateTask

    reviewer = ClusterReviewer(config, base_url, api_key, model)
    candidates = reviewer.review_clusters(clusters, base_dir)
    # candidates: list[CandidateTask] — pre-filtered, grounded

Each call is traced via agent_trace and uses strip_think so reasoning-model
"think" blocks are silently discarded before JSON parsing.  The call is
fail-closed: a bad/missing JSON response is logged and produces zero candidates
for that cluster rather than crashing the run.

Configuration (agents.ini [architect])
---------------------------------------
temperature      — sampling temperature (default 0.2)
max_tokens       — token cap (default 2048)
system           — override the built-in system prompt (optional)

agents.ini [api] / [api_local] / [api_remote] supply base_url, api_key, model,
api_format, verify_ssl — same pattern as every other agent in this codebase.
"""

from __future__ import annotations

import configparser
import json
import logging
import math
import re as _re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.auto.repo_ingest import RESERVED_META_FILES
from typing import Any

from tools.agent_trace import tracer
from tools.auto import arch_probe as _arch_probe
from tools.auto.repo_ingest import RepoCluster
from tools.auto.utils import atomic_write_text, file_set_fingerprint
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think
from tools.config_safe import safe_getboolean

logger = logging.getLogger(__name__)

# Architect system prompts, injected as the system role; overridable with [architect] system.

_SYSTEM_PROMPT_CODE = (
    "You are a senior software architect performing a targeted code review. "
    "Your job is to identify concrete, actionable improvements in the files "
    "provided. Each improvement you suggest MUST be grounded in a real location "
    "in the code — you MUST cite the exact file path and either a symbol name "
    "(function, class, method) or a line range. "
    "Do NOT invent problems that are not actually present in the provided code. "
    "Every task's instruction MUST require new or updated tests for the change, "
    "its target_files MUST include the test file (tests/test_*.py), and its "
    "acceptance_check MUST run those tests (e.g. python3 -m pytest "
    "tests/test_x.py -q). "
    "Return ONLY a JSON array — no prose, no markdown fences, no preamble."
)

_SYSTEM_PROMPT_DOCS = (
    "You are a senior technical writer performing a documentation review. "
    "Your job is to identify concrete, actionable improvements in the documentation "
    "files provided. Each improvement MUST be grounded in a real location — "
    "cite the exact file path and the line range where the issue lives. "
    "symbol may be null. "
    "acceptance_check must be a shell command such as "
    "'grep -q \"expected heading\" file.md' or 'true' if not checkable. "
    "Do NOT invent problems that are not actually present in the provided docs. "
    "Return ONLY a JSON array — no prose, no markdown fences, no preamble."
)

_SYSTEM_PROMPT_CREATIVE = (
    "You are a creative-writing planner for long-form fiction. The author wants "
    "to CONTINUE or GENERATE prose (e.g. write the next chapter), not refactor "
    "code. Produce concrete writing tasks that fulfil the stated goal. "
    "GROUNDING RULE: cited_location.file MUST be one of the EXISTING files listed "
    "(typically the latest existing chapter) — this is the source you continue "
    "from. cited_location.symbol MUST be null and line_start/line_end SHOULD be "
    "null (do not invent line numbers). "
    "target_files names the file to write; it MAY be a NEW file that does not "
    "exist yet (e.g. the next chapter). "
    "acceptance_check is optional for creative tasks (omit it or use 'true'). "
    "Prefer ONE focused task that writes a complete chapter over many tiny "
    "fragment tasks. "
    "When fixing inconsistencies, change ONLY the specific conflicting detail "
    "(a name, an age, a description) — NEVER make chapters identical, NEVER "
    "copy one chapter's text into another, and NEVER renumber chapters. Each "
    "chapter must stay a distinct scene. "
    "Return ONLY a JSON array — no prose, no markdown fences, no preamble."
)

# Backward-compat alias — existing code that references _SYSTEM_PROMPT still works.
_SYSTEM_PROMPT = _SYSTEM_PROMPT_CODE

# Plan-validator prompt (AUTO-CR-20-3): contradictions and missing facts only, never style.

_ARCH_PLAN_SYSTEM = (
    "You are a plan reviewer. Given a GOAL and a list of proposed TASKS, reply "
    "ONE line. First token APPROVED or REVISE. Reply REVISE only when a task "
    "contradicts a fact in the goal, or a required fact in the goal is "
    "covered by no task. Name the problem. Do not REVISE for wording or "
    "ordering. No JSON, no preamble."
)

_SYSTEM_PROMPTS: dict[str, str] = {
    "code":     _SYSTEM_PROMPT_CODE,
    "docs":     _SYSTEM_PROMPT_DOCS,
    "creative": _SYSTEM_PROMPT_CREATIVE,
}

# Per-cluster user prompt template: goal, cluster name, file listing and file contents.

_USER_PROMPT_TMPL = """\
Goal: {goal}

You are reviewing the "{cluster}" cluster of the repository.

Files in this cluster (EXACT paths you MUST use verbatim in cited_location.file \
and target_files — do NOT invent, shorten, or add prefixes):
{file_listing}

File contents:
{file_contents}

Produce up to {max_tasks} concrete tasks for files in this cluster that ACHIEVE THE GOAL \
above.  The goal may ask you to ADD or CHANGE functionality (new command-line \
arguments, new behavior, new output) — not only to fix bugs.  Treat behavior the \
goal requires but the code does not yet have as the work to be done.  If the goal \
lists explicit items (e.g. "Task 1: ...", "Task 2: ..."), produce ONE task per \
requested item.  You may also include genuine improvements (missing tests, error \
handling, input validation) when they serve the goal.

STRICT RULES:
1. Every task MUST cite the exact file path and a symbol name OR line range \
   where the issue lives.  Tasks without a cited_location are invalid.
2. The "file" field in cited_location and every entry in target_files MUST \
   be copied EXACTLY from the list below — character for character. \
   Do NOT invent new paths, add directory prefixes, or modify the paths in any way. \
   To add new tests, target an EXISTING test file from the list and add test \
   functions to it.
3. Ground every task in the actual code shown (cite a real file and the symbol \
   or line range where the change belongs), but a task MAY add behavior the code \
   does not yet have — that is expected when the goal asks for new features. If the \
   goal genuinely requires a file that does not exist yet, you MAY propose creating \
   it: set "new_file": true in cited_location and leave "symbol"/"line_start"/ \
   "line_end" null (there is nothing to anchor to yet). Only do this when no listed \
   file is a reasonable place for the change — prefer extending an existing file.
4. Keep each task small enough to be implemented and tested independently.
5. Return an empty array [] ONLY if the goal is ALREADY fully implemented in the \
   code shown.  If the goal asks for behavior the code does not yet have, that \
   absence IS the work — do not return [].
6. The "acceptance_check" MUST be a real shell command (not a description or sentence). \
   Use the project's OWN build/test runner — match the language of the files shown. \
   Examples: "pytest tests/test_foo.py", "python main.py --name Alice", \
   "./gradlew test", "gradle build", "mvn -q test", "npm test", "go test ./...", \
   "cargo test", "make check". If no automated check is feasible for this change, \
   use "true". For a Python CLI flag use the double-dash form \
   ("python main.py --name Alice"), never a positional argument form \
   ("python main.py Alice") unless the instruction explicitly defines a positional argument.

Each element of the JSON array must match this schema exactly (no extra keys):

[
  {{
    "title": "<short imperative phrase>",
    "instruction": "<detailed instruction for the coder agent>",
    "target_files": ["<exact path from the list below>"],
    "acceptance_check": "<shell command that exits 0 when the task is done — a real runnable command using the project's own test/build runner (e.g. 'pytest tests/test_foo.py', 'python main.py --name Alice', './gradlew test', 'mvn -q test', 'npm test', 'go test ./...', 'cargo test'), or 'true' if no automated check fits. For a Python CLI flag --flag use 'python script.py --flag value', NOT a positional argument>",
    "cited_location": {{
      "file": "<exact path from the list below, UNLESS new_file is true>",
      "symbol": "<function or class name, or null>",
      "line_start": <integer or null>,
      "line_end":   <integer or null>,
      "new_file": <true ONLY if this file does not exist yet and this task creates it, otherwise false>
    }}
  }}
]

REMINDER — every path in "target_files" or "cited_location.file" MUST either (a) be \
copied character-for-character from the list below, or (b) be a brand-new path AND \
have "cited_location.new_file" set to true. Any other path will be rejected:
{file_listing}

Now produce ONLY the JSON array of up to {max_tasks} concrete tasks that IMPLEMENT the \
goal "{goal}" against the files above (no prose, no markdown fences):
"""

# Creative tasks are prose: a variant prompt without the code rules (AUTO-CR-34).
_USER_PROMPT_CREATIVE = """\
Goal: {goal}

You are reviewing the "{cluster}" group of chapter/text files.

Files in this group (EXACT paths you MUST use verbatim in cited_location.file \
and target_files — do NOT invent, shorten, or add prefixes):
{file_listing}

File contents:
{file_contents}

Produce up to {max_tasks} concrete writing/editing task(s) for the files above that \
ACHIEVE THE GOAL.  Prefer ONE focused task that writes or revises a complete chapter \
over many tiny fragment tasks.  Treat content the goal asks for but the text does not \
yet have as the work to be done.

STRICT RULES:
1. Every entry in target_files and cited_location.file MUST be copied EXACTLY from \
   the list above — character for character.  Do NOT invent new paths or add prefixes.
2. Ground each task in a real file from the list.  This is prose, so you do NOT need a \
   function/class symbol or a line range — leave "symbol", "line_start" and "line_end" \
   as null.
3. Keep each task self-contained: one chapter/scene per task where possible.
4. Return an empty array [] ONLY if the goal is ALREADY fully satisfied by the text shown.
5. acceptance_check MUST be the literal string "true".  Prose quality is judged by the \
   editorial review gate, not by a shell command — never put a test/build command here.

Each element of the JSON array must match this schema exactly (no extra keys):

[
  {{
    "title": "<short imperative phrase>",
    "instruction": "<detailed instruction for the writing agent: what to write or change>",
    "target_files": ["<exact path from the list above>"],
    "acceptance_check": "true",
    "cited_location": {{
      "file": "<exact path from the list above>",
      "symbol": null,
      "line_start": null,
      "line_end":   null
    }}
  }}
]

REMINDER — the ONLY file paths you may put in "target_files" or \
"cited_location.file" are these, copied character-for-character. \
Any other path will be rejected:
{file_listing}

Now produce ONLY the JSON array of up to {max_tasks} concrete writing/editing task(s) \
that achieve the goal "{goal}" against the files above (no prose, no markdown fences):
"""

# Fallbacks for max_file_chars and max_files_per_review, used only when [architect] lacks the keys.
_DEFAULT_MAX_FILE_CHARS    = 4000
_DEFAULT_MAX_FILES_PER_REVIEW = 6


# Data model

@dataclass
class CitedLocation:
    """Grounding reference: where in the codebase the candidate applies."""
    file: str
    symbol: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    new_file: bool = False

    def __post_init__(self) -> None:
        """Normalise ``symbol`` to ``str | None`` regardless of how this
        dataclass was built (FIX-1 bug #5).

        Two call sites build a ``CitedLocation`` from untrusted data:
        ``ClusterReviewer._parse_candidates`` (raw LLM JSON) and the
        module-level ``_deserialise_candidates`` (a JSON checkpoint file,
        which could have been written by an older/buggy build or edited
        by hand). Either can hand this a truthy non-string ``symbol`` —
        an LLM-emitted ``123``, ``["Foo"]``, ``True``. ``is_valid()``
        only checks truthiness, so such a value used to pass the
        grounding gate and then crash downstream where a string is
        assumed: ``re.escape()`` in ``backlog_prioritiser.py`` and
        ``extract_block()`` in ``gate1_filter.py`` both raise
        ``TypeError`` on a non-string.

        Enforcing the invariant here, once, protects every construction
        site — present and future, including direct construction in
        tests — instead of only whichever one happened to be reported.
        A non-string value carries no real grounding information (an
        LLM's ``symbol: 123`` doesn't name any real symbol in the file),
        so it is treated the same as "no symbol was given": discarded to
        ``None`` rather than stringified into a fake anchor that would
        just fail to be found later, silently, by ``extract_block``.
        This also matches the sibling fields' existing pattern
        (``_to_str_or_empty``, ``_to_int_or_none``, ``_to_bool_or``),
        which is the pattern ``symbol`` alone was missing.
        """
        if self.symbol is None:
            return
        if isinstance(self.symbol, str):
            self.symbol = self.symbol.strip() or None
            return
        logger.debug(
            "CitedLocation: discarding non-string symbol %r (%s) for "
            "file=%r — treated as no symbol given.",
            self.symbol, type(self.symbol).__name__, self.file,
        )
        self.symbol = None

    def is_valid(self, task_mode: str = "code") -> bool:
        """A location is valid when it has a file AND at least one anchor.

        For docs/creative modes, a file alone is sufficient grounding —
        no symbol or line range is required. For code mode, a task that
        explicitly declares ``new_file=True`` is also valid without a
        symbol/line anchor: a file that does not exist yet has nothing to
        anchor to, by definition (AUTO-BUG: new-file creation was
        previously impossible to ground — see gate1_filter._check_existence).
        """
        if not self.file:
            return False
        if task_mode == "code":
            return self.new_file or bool(self.symbol) or self.line_start is not None
        # docs / creative: file alone is sufficient grounding
        return True


@dataclass
class CandidateTask:
    """A single improvement candidate produced by the Architect for one cluster.

    Attributes
    ----------
    title:
        Short imperative phrase, e.g. "Add input validation to parse_config".
    instruction:
        Full instruction string for the Coder agent (AUTO-C2).
    target_files:
        List of relative paths that the task will touch.
    acceptance_check:
        Shell command whose exit-0 signals task completion.
    cited_location:
        Grounding reference.  Candidates where ``cited_location.is_valid()``
        returns False are rejected at parse time.
    cluster:
        Name of the cluster this candidate came from (set by ClusterReviewer).
    raw:
        Original parsed dict from the LLM (kept for debugging / Gate 1).
    """

    title: str
    instruction: str
    target_files: list[str]
    acceptance_check: str
    cited_location: CitedLocation
    cluster: str = ""
    raw: dict = field(default_factory=dict, compare=False, repr=False)


# ClusterReviewer

class ConfigValueError(ValueError):
    """A config value the operator explicitly set that this code refuses.

    Distinct from a plain ValueError on purpose: ClusterReviewer raises
    ValueError both for values it cannot READ (a malformed int) and for
    values it actively REFUSES (max_files_per_review <= 0). A caller that
    degrades setup failures into "feature off" — pipeline._build_plan_validator
    — must not swallow the second kind, or the creative plan phase runs
    unvalidated and the operator never learns their config was rejected.
    Kept a ValueError subclass so existing ``except ValueError`` still catches it.
    """


class ClusterReviewer(_llm_stream.LLMClientBase):
    """Sends one Architect LLM call per cluster and returns grounded candidates.

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

    # Escalation for unsalvageable replies, mirroring Gate 1: more tokens each retry, from the same floor and step (AUTO-H5-ESCALATE-1).
    _UNSALVAGEABLE_TOKENS_FLOOR = 4096
    _UNSALVAGEABLE_TOKENS_STEP_MULT = 2.0
    _UNSALVAGEABLE_TOKENS_DEFAULT_CEILING = 32768
    _UNSALVAGEABLE_TOKENS_CTX_FRACTION = 0.5
    # Fixed absolute temperatures per token tier, not relative to the base: at 0.0 a repeated mistake reproduces byte for byte (AUTO-RETRY-TEMP-1).
    _UNSALVAGEABLE_TEMPERATURES = (0.0, 0.1)

    def __init__(
        self,
        config: configparser.ConfigParser,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "openai",
        verify_ssl: bool = True,
        task_mode: str = "code",
    ) -> None:
        super().__init__(config, base_url, api_key, model, api_format, verify_ssl)
        self._task_mode  = task_mode

        arch = "architect"
        # Each config read is guarded and wrapped per call, so one malformed key cannot crash construction (BUGFIX).
        try:
            self._temperature = float(config.get(arch, "temperature", fallback="0.2"))
        except ValueError as exc:
            logger.warning("config [%s] temperature is malformed (%s) — using 0.2", arch, exc)
            self._temperature = 0.2
        # Creative tasks get a larger token cap, or the JSON array truncates mid-object (AUTO-CR-11).
        from tools.auto.utils import _cfg_mode
        try:
            self._max_tokens = int(_cfg_mode(config, arch, "max_tokens", task_mode, fallback="2048"))
        except ValueError as exc:
            logger.warning("config [%s] max_tokens is malformed (%s) — using 2048", arch, exc)
            self._max_tokens = 2048
        try:
            self._timeout = float(config.get("loop", "timeout_seconds", fallback="300"))
        except ValueError as exc:
            logger.warning("config [loop] timeout_seconds is malformed (%s) — using 300", exc)
            self._timeout = 300.0
        try:
            self._max_file_chars = int(config.get(arch, "max_file_chars", fallback=str(_DEFAULT_MAX_FILE_CHARS)))
        except ValueError as exc:
            logger.warning("config [%s] max_file_chars is malformed (%s) — using %d",
                            arch, exc, _DEFAULT_MAX_FILE_CHARS)
            self._max_file_chars = _DEFAULT_MAX_FILE_CHARS
        try:
            self._max_files_per_review = int(
                config.get(arch, "max_files_per_review", fallback=str(_DEFAULT_MAX_FILES_PER_REVIEW))
            )
        except ValueError as exc:
            logger.warning("config [%s] max_files_per_review is malformed (%s) — using %d",
                            arch, exc, _DEFAULT_MAX_FILES_PER_REVIEW)
            self._max_files_per_review = _DEFAULT_MAX_FILES_PER_REVIEW
        # A zero or negative batch size would fail deep in review_clusters; reject it here, naming the key.
        if self._max_files_per_review <= 0:
            raise ConfigValueError(
                "config [architect] max_files_per_review must be >= 1, "
                f"got {self._max_files_per_review} — it is used as the step of "
                "range(0, len(files), step), so 0 raises "
                "'range() arg 3 must not be zero' for every cluster"
            )
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        active_profile = config.get("api", "active", fallback="local")
        try:
            self._num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        except ValueError as exc:
            logger.warning("config [api_%s] num_ctx is malformed (%s) — using 0", active_profile, exc)
            self._num_ctx = 0
        # A thinking model's reasoning would eat the whole budget; thinking is off unless [architect] think is true (AUTO-FIX).
        try:
            self._think = config.getboolean(arch, "think", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] think is malformed (%s) — using False", arch, exc)
            self._think = False

        # The probe is off by default and its config reads are guarded, so a malformed key cannot abort the plan phase (FIX-2).
        try:
            self._probe_enabled          = config.getboolean(arch, "probe_enabled", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] probe_enabled is malformed (%s) — using False", arch, exc)
            self._probe_enabled = False
        try:
            self._probe_max_rounds       = max(0, config.getint(arch, "probe_max_rounds", fallback=1))
        except ValueError as exc:
            logger.warning("config [%s] probe_max_rounds is malformed (%s) — using 1", arch, exc)
            self._probe_max_rounds = 1
        try:
            self._probe_max_chars        = config.getint(arch, "probe_max_chars", fallback=2000)
        except ValueError as exc:
            logger.warning("config [%s] probe_max_chars is malformed (%s) — using 2000", arch, exc)
            self._probe_max_chars = 2000
        try:
            self._probe_max_total_chars  = config.getint(arch, "probe_max_total_chars", fallback=6000)
        except ValueError as exc:
            logger.warning("config [%s] probe_max_total_chars is malformed (%s) — using 6000", arch, exc)
            self._probe_max_total_chars = 6000
        # Escalations allowed when the digest cap cuts a round short, and the learned cap (AUTO-P8, P9).
        try:
            self._probe_budget_warmup    = config.getint(arch, "probe_budget_warmup", fallback=3)
        except ValueError as exc:
            logger.warning("config [%s] probe_budget_warmup is malformed (%s) — using 3", arch, exc)
            self._probe_budget_warmup = 3
        try:
            self._probe_budget_headroom  = config.getfloat(arch, "probe_budget_headroom", fallback=2.0)
        except ValueError as exc:
            logger.warning("config [%s] probe_budget_headroom is malformed (%s) — using 2.0", arch, exc)
            self._probe_budget_headroom = 2.0
        try:
            self._probe_budget_max_chars = config.getint(arch, "probe_budget_max_chars", fallback=0)
        except ValueError as exc:
            logger.warning("config [%s] probe_budget_max_chars is malformed (%s) — using 0", arch, exc)
            self._probe_budget_max_chars = 0
        try:
            self._probe_budget_escalations = max(0, min(
                len(_arch_probe._BUDGET_ESCALATION_LADDER),
                config.getint(arch, "probe_budget_escalations", fallback=2),
            ))
        except ValueError as exc:
            logger.warning("config [%s] probe_budget_escalations is malformed (%s) — using 2", arch, exc)
            self._probe_budget_escalations = 2
        # The ladder scales with the profile's real window; a probe scale above 1.0 grows faster (AUTO-P13).
        try:
            self._probe_budget_ladder_scale = max(
                0.1, config.getfloat(arch, "probe_budget_ladder_scale", fallback=1.0)
            )
        except ValueError as exc:
            logger.warning(
                "config [%s] probe_budget_ladder_scale is malformed (%s) — using 1.0",
                arch, exc,
            )
            self._probe_budget_ladder_scale = 1.0
        # The run-level miss memo is bounded; reset() must not clear it (AUTO-F1).
        try:
            self._probe_memo_max_entries = max(
                1, config.getint(arch, "probe_memo_max_entries", fallback=200)
            )
        except ValueError as exc:
            logger.warning("config [%s] probe_memo_max_entries is malformed (%s) — using 200", arch, exc)
            self._probe_memo_max_entries = 200
        _ops_raw                     = config.get(arch, "probe_allowed_ops", fallback="facts")
        # An empty probe_allowed_ops means allow nothing; it is not replaced by the default.
        self._probe_allowed_ops      = tuple(
            o.strip().lower() for o in _ops_raw.split(",") if o.strip()
        )
        # Built once, on first use, since the collect model needs base_dir; None (no collect) is cached too.
        self._probe: "_arch_probe.ArchProbe | None" = None
        self._probe_built            = False

        # System prompt: the mode-specific ini key, then the legacy key, then the built-in constant (DM-2).
        mode_ini_key = f"system_{task_mode}" if task_mode != "code" else None
        if mode_ini_key and config.has_option(arch, mode_ini_key):
            self._system = config.get(arch, mode_ini_key).strip()
        else:
            built_in = _SYSTEM_PROMPTS.get(task_mode, _SYSTEM_PROMPT_CODE)
            self._system = config.get(arch, "system", fallback=built_in).strip()

        # Built on first use if construction failed, so __init__ never raises for a missing summary_memory.
        self._llm_call = self._build_llm_call()

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_plan(self, goal: str, candidates: list) -> tuple[bool, str]:
        """Check the proposed task list against *goal* for contradictions / gaps.

        AUTO-CR-20-3: a narrow, fail-open validator that runs **after** the
        architect emits candidates and **before** Gate-1, in creative mode only.

        Returns
        -------
        (ok, reason)
            ``ok=True``  — plan is acceptable (or check failed open on error).
            ``ok=False`` — the LLM found a contradiction or a missing required
                           fact; *reason* names the problem for the log + re-run
                           feedback.

        Never raises — any LLM error or unparseable reply returns ``(True, "")``.
        """
        # Import locally to avoid a circular import (inner_loop imports architect).
        from tools.auto.inner_loop import _parse_verdict_soft  # noqa: PLC0415

        # Format candidates compactly: "title: instruction" per task.
        task_lines: list[str] = []
        for i, c in enumerate(candidates, 1):
            if isinstance(c, dict):
                title = c.get("title", "")
                instruction = c.get("instruction", "")
            else:
                # CandidateTask dataclass
                title = getattr(c, "title", "")
                instruction = getattr(c, "instruction", "")
            task_lines.append(f"Task {i}: {title}\n  {instruction}")

        tasks_block = "\n".join(task_lines) if task_lines else "(no tasks)"
        user_msg = f"GOAL:\n{goal}\n\nTASKS:\n{tasks_block}"

        try:
            raw = self._llm_call(_ARCH_PLAN_SYSTEM, user_msg) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("validate_plan: LLM call failed — %s; passing plan.", exc)
            return True, ""

        approved, reason, unparseable = _parse_verdict_soft(raw)
        if unparseable:
            logger.warning(
                "validate_plan: unparseable reply %r — passing plan (fail-open).",
                raw[:120],
            )
        if not approved:
            logger.info("validate_plan: REVISE — %s", reason)
        return approved, (reason if not approved else "")

    def review_clusters(
        self,
        clusters: list[RepoCluster],
        base_dir: str | Path,
        goal: str = "improve current code",
        *,
        on_cluster_done=None,
        checkpoint_path: "Path | None" = None,
    ) -> list[CandidateTask]:
        """Review every non-empty cluster and return all grounded candidates.

        Parameters
        ----------
        clusters:
            Output of :func:`tools.auto.repo_ingest.ingest_repo`.
        base_dir:
            Root directory of the repository (used to read file contents).
        goal:
            The user-supplied improvement goal string.
        on_cluster_done:
            Optional zero-argument callable invoked after each cluster is
            processed (including empty/skipped ones).  Exceptions raised by
            the callback are swallowed so they cannot abort the run.
        checkpoint_path:
            Optional path to a JSON file used to persist per-cluster results.
            When provided, successfully-reviewed clusters are written here
            immediately after each batch so that an LLM crash or HTTP 500
            storm does not force a full re-review on restart.  Clusters whose
            batch keys are already present in the checkpoint are skipped and
            their previously-saved candidates are re-used directly.

        Returns
        -------
        list[CandidateTask]
            All candidates whose ``cited_location.is_valid()`` returned True,
            in cluster order.  Ungrounded candidates are logged and dropped.
        """
        base_dir = Path(base_dir)
        all_candidates: list[CandidateTask] = []

        # ── Load checkpoint (if any) ──────────────────────────────────────────
        checkpoint: dict[str, list[dict]] = {}
        if checkpoint_path is not None:
            checkpoint_path = Path(checkpoint_path)
            if checkpoint_path.exists():
                try:
                    checkpoint = json.loads(
                        checkpoint_path.read_text(encoding="utf-8")
                    )
                    if not isinstance(checkpoint, dict):
                        raise ValueError(f"a JSON {type(checkpoint).__name__}, not an object")
                    logger.info(
                        "review_clusters: loaded architect checkpoint from %s "
                        "(%d batch key(s) cached)",
                        checkpoint_path, len(checkpoint),
                    )
                except Exception as exc:
                    logger.warning(
                        "review_clusters: could not read checkpoint %s: %s — starting fresh",
                        checkpoint_path, exc,
                    )
                    checkpoint = {}

        for cluster in clusters:
            if not cluster.files:
                logger.debug("review_clusters: skipping empty cluster %r", cluster.name)
                _fire_callback(on_cluster_done)
                continue

            # Large clusters are split into batches, so each prompt fits a local model's context.
            files = cluster.files
            batches = [
                files[i:i + self._max_files_per_review]
                for i in range(0, len(files), self._max_files_per_review)
            ]
            extra = f", {len(batches)} batches" if len(batches) > 1 else ""
            print(f"\n🔍 Architect reviewing cluster: [{cluster.name}] "
                  f"({len(files)} files{extra})")

            cluster_candidates: list[CandidateTask] = []
            for bi, batch_files in enumerate(batches, 1):
                sub = cluster if len(batches) == 1 else RepoCluster(
                    name=f"{cluster.name} (batch {bi}/{len(batches)})",
                    patterns=cluster.patterns,
                    files=batch_files,
                )
                # The checkpoint key includes a fingerprint of the batch files, so edited content invalidates it.
                batch_key = f"{sub.name}||{goal}||{_batch_fingerprint(base_dir, sub.files)}"

                # ── Checkpoint hit: skip LLM call, reuse saved candidates ────
                if checkpoint_path is not None and batch_key in checkpoint:
                    saved = checkpoint[batch_key]
                    # A malformed checkpoint entry falls back to a fresh review instead of crashing (BUGFIX).
                    try:
                        restored = _deserialise_candidates(saved)
                    except Exception as exc:  # noqa: BLE001 — fall back to a live call
                        logger.warning(
                            "review_clusters: could not restore checkpoint for "
                            "'%s' (%s) — making a live LLM call instead",
                            sub.name, exc,
                        )
                    else:
                        print(f"   ♻️  Cluster/batch '{sub.name}' restored from checkpoint "
                              f"({len(restored)} candidate(s)) — skipping LLM call")
                        cluster_candidates.extend(restored)
                        continue

                # ── Live LLM call ─────────────────────────────────────────────
                _all_files = files if (len(batches) > 1 and self._task_mode == "creative") else None
                batch_results = self._review_one_cluster(sub, base_dir, goal, all_files=_all_files)
                # A failed batch is not checkpointed, so it is retried on the next run.
                if batch_results is None:
                    continue
                cluster_candidates.extend(batch_results)

                # Only non-empty results are checkpointed, so a retry after fixing the inputs is not skipped.
                if checkpoint_path is not None and batch_results:
                    checkpoint[batch_key] = _serialise_candidates(batch_results)
                    try:
                        # Atomic write (temp file, then replace): a kill mid-write cannot leave invalid checkpoint JSON (bugfix).
                        atomic_write_text(
                            checkpoint_path,
                            json.dumps(checkpoint, indent=2, ensure_ascii=False),
                        )
                        logger.debug(
                            "review_clusters: checkpoint saved — %d batch key(s) total",
                            len(checkpoint),
                        )
                    except Exception as exc:
                        logger.warning(
                            "review_clusters: could not write checkpoint %s: %s",
                            checkpoint_path, exc,
                        )

            # Batch sub-clusters are named "<cluster> (batch N/M)"; restore the plain name so Gate 1's file check still finds the cluster.
            for _c in cluster_candidates:
                _c.cluster = cluster.name

            print(f"   → {len(cluster_candidates)} grounded candidate(s)")

            # Batches of one cluster can propose the same task; drop exact duplicates before merging (AUTO-BUG-3).
            if len(batches) > 1 and len(cluster_candidates) > 1:
                from tools.auto.gate1_filter import _fingerprint, _target_fingerprint  # noqa: PLC0415
                _seen: set[str] = set()
                _seen_targets: set[str] = set()
                _deduped: list[CandidateTask] = []
                for _c in cluster_candidates:
                    _fp = _fingerprint(_c)
                    _tfp = _target_fingerprint(_c) if self._task_mode == "creative" else None
                    if _fp in _seen or (_tfp is not None and _tfp in _seen_targets):
                        logger.info(
                            "review_clusters: dropped cross-batch duplicate candidate "
                            "%r (cluster=%r)", _c.title, cluster.name,
                        )
                        continue
                    _seen.add(_fp)
                    if _tfp is not None:
                        _seen_targets.add(_tfp)
                    _deduped.append(_c)
                if len(_deduped) != len(cluster_candidates):
                    print(f"   → {len(cluster_candidates) - len(_deduped)} cross-batch "
                          f"duplicate(s) dropped, {len(_deduped)} remaining")
                cluster_candidates = _deduped

            all_candidates.extend(cluster_candidates)
            _fire_callback(on_cluster_done)

        print(f"\n✅ Architect done — {len(all_candidates)} total candidate(s) across all clusters\n")
        return all_candidates

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_probe(self, base_dir: Path) -> "_arch_probe.ArchProbe | None":
        """Build the AUTO-P probe executor once, on first use.

        Returns ``None`` — a valid, expected result the caller must treat as
        "no probing this run" — when the feature is off, when the collect
        artifact is absent or stale, or when constructing the bridge fails.
        Never raises: an unavailable probe is a degraded plan phase, not a
        failed one.
        """
        if not self._probe_enabled:
            return None
        if self._probe_built:
            return self._probe
        self._probe_built = True
        try:
            from tools.auto.collect_bridge import make_collect_bridge  # noqa: PLC0415

            bridge = make_collect_bridge(
                base_dir, self._config, task_mode=self._task_mode
            )
        except Exception as exc:  # noqa: BLE001 — opt-in feature, never fatal
            logger.warning(
                "architect: probe_enabled=true but the collect bridge could not "
                "be built (%s) — planning without probes this run.", exc,
            )
            self._trace_probe_config(usable=False, reason="bridge_error", detail=str(exc))
            return None
        if bridge is None or not getattr(bridge, "usable", False):
            # "stale" and "absent" need different remedies, so the message names which one applies.
            _stale = (
                bridge is not None
                and getattr(bridge, "status", "absent") == "stale"
            )
            if _stale:
                logger.warning(
                    "architect: probe_enabled=true but the collect artifact "
                    "is stale (run --collect --refresh to update it) — "
                    "planning without probes this run.",
                )
                self._trace_probe_config(usable=False, reason="stale_artifact")
            else:
                logger.warning(
                    "architect: probe_enabled=true but no fresh collect artifact is "
                    "available ([collect] use_in_auto) — planning without probes "
                    "this run.",
                )
                self._trace_probe_config(usable=False, reason="no_artifact")
            return None
        self._probe = _arch_probe.ArchProbe(
            bridge,
            max_chars=self._probe_max_chars,
            max_total_chars=self._probe_max_total_chars,
            # read needs a root to contain paths against; without one the op disables itself (AUTO-P7).
            base_dir=base_dir,
            # AUTO-F1: run-level miss memo cap.
            memo_max_entries=self._probe_memo_max_entries,
        )
        # The learned and escalated digest caps never exceed the window (AUTO-P11).
        if self._num_ctx > 0:
            _prompt_tok = (
                self._max_files_per_review * self._max_file_chars + 4000
            ) / 3.5
            _free_tok = self._num_ctx - _prompt_tok - self._max_tokens
            # 3.5 chars/token, then 80% so a digest never fills the last of it.
            self._probe.set_window_budget(max(0, int(_free_tok * 3.5 * 0.8)))
        self._probe.configure_learning(
            warmup=self._probe_budget_warmup,
            headroom=self._probe_budget_headroom,
            ceiling=self._probe_budget_max_chars,
        )
        # Logs when probing is enabled, so the run shows whether the probe is available (AUTO-DEBUG-1).
        logger.info(
            "architect: probe available this run (collect artifact fresh) — "
            "offering ARCH_PROBE on every batch."
        )
        self._trace_probe_config(usable=True, reason="ok")
        return self._probe

    def _trace_probe_config(
        self, *, usable: bool, reason: str, detail: str = "",
    ) -> None:
        """AUTO-P4a: emit exactly one ``probe_config`` event per run.

        This is the trace-side counterpart to AUTO-DEBUG-1's console banner,
        and it exists to make ONE state visible that nothing else could
        express: *the probe was enabled and available, and the model never
        used it*.

        Before this event, ``analyze_logs.py`` rendered its probe line only
        when at least one ``probe_request`` existed, so a run with zero probes
        produced output byte-identical to a run on pre-AUTO-P code. For a
        feature whose entire Phase 0 decision gate is "how often is this
        actually used", the most important measurement rendered as silence,
        and the only way to tell "never asked" from "not installed" was to
        grep the raw prompt log.

        No event is emitted when ``probe_enabled = false`` — an old trace and
        a feature-off trace should stay indistinguishable, because for a
        feature that is off they genuinely are the same thing.

        Never raises: tracing is diagnostics, not control flow.
        """
        try:
            tracer.event(
                source="architect", target="probe", kind="probe_config",
                model=self._model,
                content=detail,
                params={
                    "usable":          str(bool(usable)),
                    "reason":          reason,
                    "max_rounds":      self._probe_max_rounds,
                    "max_chars":       self._probe_max_chars,
                    "max_total_chars": self._probe_max_total_chars,
                    "allowed_ops":     ",".join(self._probe_allowed_ops),
                    "memo_max_entries": self._probe_memo_max_entries,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("architect: probe_config trace failed: %s", exc)

    def _build_llm_call(self):
        """Build a ``llm_call(system, user) -> str`` callable for validate_plan.

        Uses the same ``_make_llm_call`` factory as SummaryMemory / CanonValidator.
        Falls back to a no-op that always returns ``""`` (fail-open) if the
        import fails, so __init__ never raises for that reason.

        GATE3-PROFILE-3: also resolves ``[architect] plan_llm_profile``,
        falling back straight to ``[api_{active}]`` when unset — the plan
        validator is architect's own, single call site, with no shared
        ``validator_llm_profile``-style middle step to inherit from first.
        ``resolve_llm_profile`` itself is deliberately OUTSIDE any
        ``except Exception`` below: a malformed ``plan_llm_profile`` must
        raise here — at construction, before any task runs — never be
        discovered mid-run. Unset, it resolves to the exact same
        :func:`~tools.auto.summary_memory._default_llm_settings` defaults
        ``_make_llm_call`` has always computed inline, so default
        behaviour is unchanged.

        AUTO-FIX (regression from GATE3-PROFILE-3): the import guard above
        was the ONLY try/except here for a while — ``_default_llm_settings``
        and ``_make_llm_call`` ran unguarded, so a config error that has
        nothing to do with ``plan_llm_profile`` (a malformed ``[coder]
        max_tokens``, say) crashed ``ClusterReviewer.__init__`` outright
        instead of degrading gracefully, contradicting this docstring's own
        "so __init__ never raises for that reason" — confirmed by
        reproducing it directly: same bad ``max_tokens``, ``_build_llm_call``
        raised ``ValueError`` instead of returning the no-op fallback. Each
        of those two calls now has its own narrow try/except, so a bad
        profile name still raises (the GATE3-PROFILE-3 behaviour this
        function exists to provide) while everything else fails open again
        (the original contract this docstring has described all along).
        """
        try:
            from tools.auto.summary_memory import (  # noqa: PLC0415
                _default_llm_settings, _make_llm_call,
            )
            from tools.auto.llm_profile import resolve_llm_profile  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("ClusterReviewer._build_llm_call failed — validate_plan will fail-open: %s", exc)
            return lambda system, user: ""

        try:
            defaults = _default_llm_settings(self._config, self._task_mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ClusterReviewer._build_llm_call: could not compute default "
                "LLM settings — validate_plan will fail-open: %s", exc,
            )
            return lambda system, user: ""

        settings, _ = resolve_llm_profile(
            self._config, "architect", "plan_llm_profile", defaults=defaults,
        )

        try:
            return _make_llm_call(self._config, task_mode=self._task_mode, settings=settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ClusterReviewer._build_llm_call: could not build the LLM "
                "call — validate_plan will fail-open: %s", exc,
            )
            return lambda system, user: ""

    def _review_one_cluster(
        self,
        cluster: RepoCluster,
        base_dir: Path,
        goal: str,
        all_files: "list[str] | None" = None,
    ) -> "list[CandidateTask] | None":
        """Send one LLM call for *cluster* and return grounded candidates.

        Parameters
        ----------
        all_files
            AUTO-BUG-9 fix: the FULL set of files in the cluster this batch
            belongs to (names only), used for the file listing shown to the
            model even when this call's file contents only cover one
            batch's subset (cluster.files). Without this, a batched review
            of a growing creative-writing repo only ever sees a partial
            file listing, and "what's the next chapter number" logic can
            conclude a stale/wrong answer -- observed in practice: it
            proposed regenerating an already-completed middle chapter
            instead of only ever extending the book forward. Defaults to
            cluster.files (old behaviour) when not given.

        Returns
        -------
        list[CandidateTask]
            Grounded candidates.  May be empty if the LLM produced nothing valid.
        None
            The LLM call failed after all retries (transient server error).
            The caller must NOT checkpoint this result.
        """
        listing_files = all_files if all_files is not None else cluster.files
        file_listing = "\n".join(f"  - {f}" for f in listing_files)
        file_contents = self._build_file_contents(cluster.files, base_dir)

        _tmpl = (
            _USER_PROMPT_CREATIVE
            if self._task_mode == "creative"
            else _USER_PROMPT_TMPL
        )

        _base_max_tasks = 1 if self._task_mode == "creative" else 5

        # Truncated candidate arrays shrink the requested task count and retry as a fresh call (AUTO-H4); see truncation_retry_max and truncation_shrink_factor.
        try:
            _retry_max = self._config.getint("architect", "truncation_retry_max", fallback=2)
        except ValueError as exc:
            logger.warning("config [architect] truncation_retry_max is malformed (%s) — using default 2", exc)
            _retry_max = 2
        _retry_max = max(0, _retry_max)
        try:
            _shrink_factor = float(
                self._config.get("architect", "truncation_shrink_factor", fallback="0.5")
            )
        except ValueError:
            _shrink_factor = 0.5
        if not (0.0 < _shrink_factor < 1.0):
            _shrink_factor = 0.5

        def _shrink(n: int) -> int:
            """Reduce max_tasks by _shrink_factor, always making forward
            progress: never returns a value >= n when n > 1, floors at 1."""
            reduced = math.floor(n * _shrink_factor)
            if reduced >= n:
                reduced = n - 1
            return max(1, reduced)

        # Unsalvageable replies retry with escalating tokens and temperature (AUTO-H5); see empty_response_retry_max.
        try:
            _empty_retry_max = self._config.getint("architect", "empty_response_retry_max", fallback=6)
        except ValueError as exc:
            logger.warning("config [architect] empty_response_retry_max is malformed (%s) — using default 6", exc)
            _empty_retry_max = 6
        _empty_retry_max = max(0, _empty_retry_max)

        # Probe state used by the call closure; reassigned by the probe loop below (AUTO-P1).
        _probe = self._get_probe(base_dir)
        if _probe is not None:
            # Tell the probe which files this batch saw, so results from elsewhere are marked (AUTO-P7).
            _probe.set_batch_files(cluster.files)
            # The probe's digest budget is per batch (AUTO-P4a); reset it so one run does not switch it off.
            _probe.reset()
        _probe_digest = ""
        _probe_forced = False

        def _call_and_parse(
            max_tasks: int, *,
            max_tokens: "int | None" = None,
            temperature: "float | None" = None,
        ) -> "tuple[list[CandidateTask] | None, bool, bool, list]":
            """One LLM call + parse at a given max_tasks budget.

            Returns ``(candidates_or_None, truncated, unsalvageable,
            probe_request)``.  ``None`` candidates means the call itself
            failed after all transient-error retries (unrelated to either
            AUTO-H4/H5 — see the network-retry loop below); ``truncated``
            and ``unsalvageable`` are only meaningful when candidates is
            not ``None``, and are mutually exclusive (see
            ``_parse_candidates_ex``'s docstring).

            AUTO-P2: ``probe_request`` is the list of
            ``arch_probe.ProbeOp`` the Architect asked for, or ``[]``.
            **Invariant: when it is non-empty, both ``truncated`` and
            ``unsalvageable`` are forced ``False``** — see the block above
            the return statement for why.

            *max_tokens*/*temperature*, when given, override this
            instance's own ``self._max_tokens``/``self._temperature`` for
            just this one call — used by the AUTO-H5-ESCALATE-1 retry
            loop below to widen the budget on an unsalvageable response
            without touching the AUTO-H4 truncation path, which has its
            own, unrelated remedy (shrink max_tasks).
            """
            user_msg = _tmpl.format(
                goal=goal,
                cluster=cluster.name,
                file_listing=file_listing,
                file_contents=file_contents,
                max_tasks=max_tasks,
            )

            # Probe sections are appended only when probing is on, so the default prompts stay byte-identical (AUTO-P1).
            if _probe_forced:
                # A forced call appends the stop suffix even if probing was not offered, so the model cannot re-probe.
                user_msg += "\n" + _arch_probe.FORCED_SUFFIX
            elif _probe is not None and _probe.usable:
                user_msg += "\n" + _arch_probe.PROBE_INSTRUCTIONS
            if _probe_digest:
                user_msg += "\n\n" + _probe_digest

            url, headers, payload = _llm_stream.build_chat_request(
                base_url=self._base_url, api_key=self._api_key, model=self._model,
                api_format=self._api_format,
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens if max_tokens is None else max_tokens,
                system=self._system, user_msg=user_msg,
                num_ctx=self._num_ctx, think=self._think,
            )

            # The temperature actually sent, read from the request rather than the payload (ollama nests it).
            _effective_temperature = self._temperature if temperature is None else temperature

            # Trace the outgoing call.
            tracer.event(
                source="architect",
                target="llm",
                kind="llm_request",
                content=user_msg, model=self._model,
                params={"model": self._model, "temperature": _effective_temperature,
                        "cluster": cluster.name, "max_tasks": max_tasks},
            )

            # Retry delays are configurable instead of fixed (AUTO-BUG-8).
            _delays_raw = self._config.get("architect", "retry_delays_sec", fallback="5,15,30")
            try:
                _RETRY_DELAYS = [float(x.strip()) for x in _delays_raw.split(",") if x.strip()]
            except ValueError:
                logger.warning(
                    "architect: invalid [architect] retry_delays_sec=%r — using default [5, 15, 30].",
                    _delays_raw,
                )
                _RETRY_DELAYS = [5, 15, 30]

            raw_text = ""
            last_exc: Exception | None = None
            _attempt = 1

            for _attempt, _delay in enumerate([0] + _RETRY_DELAYS, start=1):
                if _delay:
                    logger.warning(
                        "review_one_cluster: retrying cluster %r in %ds "
                        "(attempt %d/%d) after: %s",
                        cluster.name, _delay, _attempt, 1 + len(_RETRY_DELAYS), last_exc,
                    )
                    time.sleep(_delay)

                try:
                    tokens_list: list[str] = []

                    def streaming_callback(token: str) -> None:
                        sys.stdout.write(token)
                        sys.stdout.flush()
                        tokens_list.append(token)

                    print("\n🧠 [LIVE ARCHITECT STREAMING THINKING & RESPONSE]:")
                    returned = _llm_stream.request_completion(
                        url=url,
                        headers=headers,
                        payload=payload,
                        timeout=self._timeout,
                        stream=True,
                        on_token=streaming_callback,
                        api_format=self._api_format,
                        ssl_context=self._ssl_context,
                        **self._retry_kwargs,
                    )
                    # The full response is preferred, falling back to the streamed tokens when it is empty.
                    raw_text = returned or "".join(tokens_list)
                    print("\n" + "═" * 80 + "\n")
                    last_exc = None
                    break  # success — exit retry loop

                except Exception as exc:
                    last_exc = exc
                    exc_str = str(exc)
                    # Retry only transient server errors (5xx) and connection failures; not deterministic 4xx.
                    _http_status: int = 0
                    _code = getattr(exc, "code", None)
                    if isinstance(_code, int):
                        _http_status = _code
                    else:
                        _status_match = _re.search(r"\b([1-5]\d{2})\b", exc_str)
                        if _status_match:
                            _http_status = int(_status_match.group(1))
                    _is_transient = (
                        (500 <= _http_status <= 599)
                        or "ConnectionRefused" in exc_str
                        or "Connection refused" in exc_str
                        or "timed out" in exc_str.lower()
                        or "timeout" in exc_str.lower()
                    )
                    if not _is_transient or _attempt > len(_RETRY_DELAYS):
                        break  # non-retryable or retries exhausted — fall through

            if last_exc is not None:
                logger.warning(
                    "review_one_cluster: LLM call failed for cluster %r after %d attempt(s): %s",
                    cluster.name, _attempt, last_exc,
                )
                tracer.event(
                    source="llm", target="architect", kind="llm_response", model=self._model,
                    content=f"[ERROR] {last_exc}", params={"cluster": cluster.name},
                )
                # None means the call failed, distinct from an empty result; a failed batch is not checkpointed.
                return None, False, False, []

            # Strip reasoning tokens before JSON parsing.
            cleaned = strip_think(raw_text)

            tracer.event(
                source="llm",
                target="architect",
                kind="llm_response",
                content=cleaned, model=self._model,
                params={"cluster": cluster.name, "max_tasks": max_tasks},
            )

            _cands, _truncated, _unsalvageable = self._parse_candidates_ex(
                cleaned, cluster.name, cluster.files
            )

            # An ARCH_PROBE reply is classified here, before the retry ladders, so a probe never uses up H4 or H5 retries (AUTO-P2).
            _probe_request: list = []
            if self._probe_enabled:
                try:
                    _probe_request = _arch_probe.extract_probe_request(
                        cleaned, allowed_ops=self._probe_allowed_ops
                    ) or []
                except Exception as exc:  # noqa: BLE001 — never fail the batch on this
                    logger.warning(
                        "review_one_cluster [%s]: probe parse raised (%s) — "
                        "treating response as a normal (non-probe) reply.",
                        cluster.name, exc,
                    )
                    _probe_request = []

            if _probe_request:
                _truncated = False
                _unsalvageable = False
                # A plain info line when a probe fires, so a run log shows it without the trace (AUTO-DEBUG-1).
                logger.info(
                    "architect [%s]: probe FIRED — %s",
                    cluster.name, ", ".join(str(op) for op in _probe_request),
                )
                try:
                    tracer.event(
                        source="architect", target="probe", kind="probe_request",
                        model=self._model,
                        content=", ".join(str(op) for op in _probe_request),
                        params={"cluster": cluster.name, "ops": len(_probe_request)},
                    )
                except Exception as exc:  # noqa: BLE001 — AUTO-P4a
                    # A failing trace write must not abort the batch; it is logged at debug level (AUTO-P4a).
                    logger.debug("probe_request trace failed: %s", exc)

            return _cands, _truncated, _unsalvageable, _probe_request

        def _attempt_plan(
            *, temperature: "float | None" = None,
        ) -> "tuple[list[CandidateTask] | None, list]":
            """One full planning attempt: initial call + the AUTO-H4/H5 ladders.

            AUTO-P1 extracted this from the body of ``_review_one_cluster`` so
            the probe loop below can run it more than once. Nothing inside
            changed: ``max_tasks`` still starts fresh at ``_base_max_tasks``
            (each probe round is a new question, not a continuation of the
            previous round's shrink state), and both retry budgets are still
            per-attempt and independent of each other.

            Returns ``(candidates_or_None, probe_request)``.
            """
            max_tasks = _base_max_tasks
            # An escalated re-ask pins temperature, so the same request is re-issued with the larger budget (AUTO-P8).
            candidates, truncated, unsalvageable, probe_request = _call_and_parse(
                max_tasks, temperature=temperature
            )

            _shrink_attempts = 0
            _empty_attempts = 0
            # One adaptive loop: each attempt reacts to the failure it actually hit, since a batch can flip between failure modes.
            while candidates is not None and (truncated or unsalvageable):
                _call_kwargs: dict = {}
                if truncated:
                    if _shrink_attempts >= _retry_max or max_tasks <= 1:
                        break
                    max_tasks = _shrink(max_tasks)
                    _shrink_attempts += 1
                    logger.warning(
                        "review_one_cluster [%s]: response truncated "
                        "(shrink-retry %d/%d) — re-asking with max_tasks=%d.",
                        cluster.name, _shrink_attempts, _retry_max, max_tasks,
                    )
                else:  # unsalvageable
                    if _empty_attempts >= _empty_retry_max:
                        break
                    _empty_attempts += 1
                    # Unsalvageable replies escalate tokens; each temperature is tried at a tier before the tier doubles (AUTO-H5-ESCALATE-1).
                    _ctx_ceiling = (
                        int(self._num_ctx * self._UNSALVAGEABLE_TOKENS_CTX_FRACTION)
                        if self._num_ctx
                        else self._UNSALVAGEABLE_TOKENS_DEFAULT_CEILING
                    )
                    _n_temps = len(self._UNSALVAGEABLE_TEMPERATURES)
                    _tier_index = (_empty_attempts - 1) // _n_temps
                    _temp_index = (_empty_attempts - 1) % _n_temps
                    _uncapped_tokens = (
                        self._UNSALVAGEABLE_TOKENS_FLOOR
                        * int(self._UNSALVAGEABLE_TOKENS_STEP_MULT ** _tier_index)
                    )
                    _attempt_tokens = min(_uncapped_tokens, _ctx_ceiling)
                    if _attempt_tokens < _uncapped_tokens:
                        # Warn when escalation is capped by a small num_ctx, since that silently limits retries (AUTO-CTX-CAP-WARN-1).
                        logger.warning(
                            "review_one_cluster [%s]: escalation wants "
                            "max_tokens=%d but is capped at %d by "
                            "num_ctx=%s (ceiling = num_ctx * %.1f). If this "
                            "model's real context window is bigger, raise "
                            "num_ctx in the active [api_*] section to let "
                            "escalation actually use it.",
                            cluster.name, _uncapped_tokens, _attempt_tokens,
                            self._num_ctx or "unset",
                            self._UNSALVAGEABLE_TOKENS_CTX_FRACTION,
                        )
                    _attempt_temp = self._UNSALVAGEABLE_TEMPERATURES[_temp_index]
                    _call_kwargs = {"max_tokens": _attempt_tokens, "temperature": _attempt_temp}
                    logger.warning(
                        "review_one_cluster [%s]: response unsalvageable (empty, "
                        "degenerate, or non-JSON) — plain retry %d/%d at the "
                        "same max_tasks=%d (max_tokens=%d, temperature=%.2f).",
                        cluster.name, _empty_attempts, _empty_retry_max, max_tasks,
                        _attempt_tokens, _attempt_temp,
                    )
                candidates, truncated, unsalvageable, probe_request = _call_and_parse(
                    max_tasks, **_call_kwargs
                )

            if candidates is not None and truncated:
                # Shrink retries are exhausted and the last attempt was still truncated: keep the salvaged candidates (pre-AUTO-H4 behaviour).
                logger.warning(
                    "review_one_cluster [%s]: still truncated after %d shrink-retry "
                    "attempt(s) (max_tasks=%d) — keeping %d salvaged candidate(s).",
                    cluster.name, _shrink_attempts, max_tasks, len(candidates),
                )
            if candidates is not None and unsalvageable:
                # Plain retries are exhausted and the reply is still unsalvageable: fail open with no candidates for this batch.
                logger.warning(
                    "review_one_cluster [%s]: still unsalvageable after %d plain "
                    "retry attempt(s) — giving up on this batch with 0 candidates.",
                    cluster.name, _empty_attempts,
                )

            # A probe request is handed back to the probe loop, so it never consumes an H4 shrink or H5 escalation (AUTO-P2).
            return candidates, probe_request

        # Probe loop: bounded by probe_max_rounds, the digest cap and an empty digest; then one forced call plans with what it has (AUTO-P1).
        candidates, probe_request = _attempt_plan()
        _probe_rounds = 0
        # Op-sets already asked in this batch; a repeat is declined so the loop cannot spin (AUTO-P4b).
        _asked_before: set = set()
        # AUTO-P8: escalated re-asks spent on this batch.
        _budget_escalations = 0

        def _decline_by_op(reason: str, ops: list) -> str:
            """Per-op tallies for a declined request, as `facts=0/2`.

            For an `unresolved` decline the executor ran and already counted
            what it found, so its own tally is authoritative. For every other
            reason (round_cap, digest_budget, no_executor, repeat,
            post_forced) nothing was looked up at all, so the ops are counted
            as neither hits nor misses — recording them as misses would blame
            the collect artifact for a budget the harness spent.
            """
            if reason == "unresolved" and _probe is not None:
                return _probe.last_by_op_str()
            counts: dict = {}
            for op in ops or ():
                counts.setdefault(op.op, 0)
                counts[op.op] += 1
            return " ".join(f"{k}=0/0" for k in sorted(counts))

        def _decline_facts_seq(reason: str, ops: list) -> tuple:
            """AUTO-F4a, corrected: informed_facts / blind_facts for a
            declined request.

            This is deliberately NOT a copy of :func:`_decline_by_op`'s
            "0/0 unless unresolved" rule, even though it looks like the same
            shape of problem. `by_op` is a PER-ROUND delta — analyze_logs
            sums every round's contribution, so a decline round that looked
            nothing up correctly contributes "0/0" to that sum without
            erasing anything: earlier rounds already emitted their own
            events.

            `informed_facts` / `blind_facts` are the opposite: a
            cumulative-for-the-whole-batch snapshot (see
            ArchProbe.informed_facts), and analyze_logs deliberately keeps
            only the LAST recorded value per cluster, not a sum — summing
            would double-count a batch's own running total (see
            analyze_logs.py's `_probe_last_facts_seq` comment). An earlier,
            broken version of this function returned a hardcoded "0/0" for
            any reason other than `unresolved` — which is correct for
            `by_op`'s per-round semantics but wrong here: whenever a
            `repeat` (or other non-`unresolved`) decline happened to be the
            LAST event recorded for a cluster, that hardcoded "0/0"
            overwrote and permanently erased whatever real informed/blind
            counts earlier rounds in the SAME batch had already
            established. Measured impact on a live run (trace
            `47f94038c230`): two batches' entire facts-sequencing
            contribution — one of them 1 informed hit plus 3 blind misses —
            vanished this way, undercounting the run's `facts` denominator
            by 5 and its hit count by 2, both silently.

            The fix: always read the CURRENT cumulative counters, whatever
            the decline reason — they already reflect every `facts` ask
            made so far in this batch and do not need `reason` to interpret
            them. Only genuinely unavailable when `_probe` itself is `None`
            (`no_executor`: the probe was never built at all, so there is
            no cumulative state to read).
            """
            if _probe is None:
                return ("0/0", "0/0")
            return (
                "%d/%d" % _probe.informed_facts,
                "%d/%d" % _probe.blind_facts,
            )

        def _decline(reason: str, ops: list) -> None:
            """AUTO-P4a: record WHY a probe request went unanswered.

            Every ``break`` below leaves a ``probe_request`` in the trace with
            no matching ``probe_result``. Before AUTO-P4a, analyze_logs.py
            reported that gap as a single "N unresolved" figure, which read —
            and was documented in the runbook — as "collect did not know these
            symbols". Four different situations were collapsed into that one
            number, and they call for opposite fixes: `round_cap` says raise
            probe_max_rounds, `unresolved` says rebuild the collect artifact,
            `no_executor` says the config is wrong, `post_forced` says the
            model is ignoring instructions. The real run that exposed this had
            4 "unresolved" of which 4 were round_cap and 0 were collect
            misses — collect had in fact answered everything it was asked.
            """
            try:
                _informed_seq, _blind_seq = _decline_facts_seq(reason, ops)
                tracer.event(
                    source="architect", target="probe", kind="probe_declined",
                    model=self._model,
                    content=", ".join(str(op) for op in ops),
                    params={
                        "cluster": cluster.name,
                        "reason":  reason,
                        "ops":     len(ops),
                        "round":   _probe_rounds,
                        # Per-op tallies on the decline side too: an all-miss round emits no probe_result, so its misses must still be counted (AUTO-P6).
                        "by_op":   _decline_by_op(reason, ops),
                        # The informed/blind split is also recorded on declines, since an all-miss round would otherwise hide it (AUTO-F4a).
                        "informed_facts": _informed_seq,
                        "blind_facts":    _blind_seq,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — diagnostics never break a batch
                logger.debug("review_one_cluster: probe_declined trace failed: %s", exc)

        while probe_request and candidates is not None and not candidates:
            if _probe is None or not _probe.usable:
                # The model probed although probing was not offered: there is nothing to resolve, so go straight to the forced call.
                logger.info(
                    "review_one_cluster [%s]: architect probed but no collect "
                    "artifact is available — forcing a final plan call.",
                    cluster.name,
                )
                _decline("no_executor", probe_request)
                break
            if _probe.budget_exhausted:
                # Escalate the digest cap before giving up on a probe round, bounded by probe_budget_escalations (AUTO-P8).
                if _budget_escalations < self._probe_budget_escalations:
                    # Once a cap is learned the temperature rung is skipped: more room is the remedy, not a re-roll (AUTO-P9).
                    _ladder = (
                        _arch_probe._BUDGET_ONLY_LADDER
                        if _probe.has_seeded_cap
                        else _arch_probe._BUDGET_ESCALATION_LADDER
                    )
                    _factor, _temp = _ladder[
                        min(_budget_escalations, len(_ladder) - 1)
                    ]
                    # The rung is scaled by probe_budget_ladder_scale; at the default 1.0 the ladder is unchanged (AUTO-P13).
                    _factor *= self._probe_budget_ladder_scale
                    # Capture the old cap before raising it, so the log shows the cap that was exceeded (AUTO-P9-followup).
                    _old_cap = _probe.current_cap
                    _new_cap = _probe.raise_budget(_factor)
                    _budget_escalations += 1
                    logger.info(
                        # The denominator is the cap in force, not the configured one (AUTO-P11).
                        "review_one_cluster [%s]: probe digest budget spent "
                        "(%d/%d chars) — escalation %d/%d: cap raised to %d, "
                        "temperature %.1f.",
                        cluster.name, _probe.chars_used,
                        _old_cap, _budget_escalations,
                        self._probe_budget_escalations, _new_cap, _temp,
                    )
                    tracer.event(
                        source="architect", target="probe",
                        kind="probe_escalated", model=self._model,
                        content=", ".join(str(op) for op in probe_request),
                        params={
                            "cluster":     cluster.name,
                            "step":        _budget_escalations,
                            "new_cap":     _new_cap,
                            "temperature": _temp,
                            # Records which probe ladder was in force, so a run can be read as learning or learned (AUTO-P9).
                            "seeded":      str(_probe.has_seeded_cap),
                        },
                    )
                    # The escalated re-ask must not be dropped as a duplicate: its earlier attempt was cut off, not answered.
                    _asked_before.discard(frozenset(str(op) for op in probe_request))
                    candidates, probe_request = _attempt_plan(temperature=_temp)
                    continue
                logger.info(
                    "review_one_cluster [%s]: probe digest budget spent "
                    "(%d/%d chars) after %d escalation(s) — forcing a final "
                    "plan call.",
                    cluster.name, _probe.chars_used,
                    _probe.current_cap, _budget_escalations,
                )
                _decline("digest_budget", probe_request)
                break
            if _probe_rounds >= self._probe_max_rounds:
                # On the last round, answer the pending request once with no digest cap, and fold it into the forced call (AUTO-P12).
                _last_digest = _probe.execute(probe_request, unbounded=True)
                if _last_digest:
                    logger.info(
                        "review_one_cluster [%s]: probe round cap reached "
                        "(%d) — answering the pending request without a "
                        "digest cap before the forced final call.",
                        cluster.name, self._probe_max_rounds,
                    )
                    _probe_digest = (
                        f"{_probe_digest}\n\n{_last_digest}"
                        if _probe_digest else _last_digest
                    )
                    try:
                        tracer.event(
                            source="probe", target="architect",
                            kind="probe_result", model=self._model,
                            content=_last_digest,
                            params={
                                "cluster":        cluster.name,
                                "round":          _probe_rounds + 1,
                                "ops":            len(probe_request),
                                "hits":           _probe.last_hits,
                                "misses":         _probe.last_misses,
                                "by_op":          _probe.last_by_op_str(),
                                "chars_used":     _probe.chars_used,
                                "run_chars_used": _probe.run_chars_used,
                                "unbounded":      "True",
                                # AUTO-F4 — see the other probe_result event.
                                "informed_facts": "%d/%d" % _probe.informed_facts,
                                "blind_facts":    "%d/%d" % _probe.blind_facts,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 — AUTO-P4a
                        logger.debug(
                            "probe_result (unbounded) trace failed: %s", exc
                        )
                else:
                    logger.info(
                        "review_one_cluster [%s]: probe round cap reached "
                        "(%d) — forcing a final plan call.",
                        cluster.name, self._probe_max_rounds,
                    )
                    _decline("round_cap", probe_request)
                break

            _fingerprint = frozenset(str(op) for op in probe_request)
            if _fingerprint in _asked_before:
                logger.info(
                    "review_one_cluster [%s]: architect re-asked an already "
                    "answered probe (%s) — forcing a final plan call.",
                    cluster.name, ", ".join(str(op) for op in probe_request),
                )
                _decline("repeat", probe_request)
                break
            _asked_before.add(_fingerprint)

            _digest_add = _probe.execute(probe_request)
            if not _digest_add:
                logger.info(
                    "review_one_cluster [%s]: probe resolved nothing for %s — "
                    "forcing a final plan call.",
                    cluster.name, ", ".join(str(op) for op in probe_request),
                )
                _decline("unresolved", probe_request)
                break

            _probe_rounds += 1
            _probe_digest = (
                f"{_probe_digest}\n\n{_digest_add}" if _probe_digest else _digest_add
            )
            try:
                tracer.event(
                    source="probe", target="architect", kind="probe_result",
                    model=self._model, content=_digest_add,
                    params={
                        "cluster":        cluster.name,
                        "round":          _probe_rounds,
                        "ops":            len(probe_request),
                        # Per-batch counters gate the loop; per-run ones are reported only. Hits and misses are the honest measure (AUTO-P4a, P4b).
                        "hits":           _probe.last_hits,
                        "misses":         _probe.last_misses,
                        # Memo hits are misses that cost nothing: the saving is visible here, not only asserted (AUTO-F1).
                        "memo_hits":      _probe.last_memo_hits,
                        # Per-op breakdown of the last execute(), e.g. "facts=3/1 module=1/0" (AUTO-P5).
                        "by_op":          _probe.last_by_op_str(),
                        "chars_used":     _probe.chars_used,
                        "run_chars_used": _probe.run_chars_used,
                        # Informed and blind facts asks, cumulative for the batch: did the model check module before guessing names (AUTO-F4).
                        "informed_facts": "%d/%d" % _probe.informed_facts,
                        "blind_facts":    "%d/%d" % _probe.blind_facts,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — AUTO-P4a, see above
                logger.debug("probe_result trace failed: %s", exc)
            logger.info(
                "review_one_cluster [%s]: probe round %d/%d — %d of %d op(s) "
                "found (%d chars total) — re-asking.",
                cluster.name, _probe_rounds, self._probe_max_rounds,
                _probe.last_hits, len(probe_request), _probe.chars_used,
            )
            candidates, probe_request = _attempt_plan()

        # A forced final call only when the model still asks and has no usable plan; a reply with grounded tasks is already complete.
        if probe_request and candidates is not None and not candidates:
            _probe_forced = True
            candidates, probe_request = _attempt_plan()
            if probe_request:
                # Probing stays detected on the forced call: a probe after the budget is spent is ignored, not retried (AUTO-H5).
                logger.warning(
                    "review_one_cluster [%s]: architect probed again after the "
                    "forced final call (%s) — giving up on this batch with 0 "
                    "candidate(s).",
                    cluster.name, ", ".join(str(op) for op in probe_request),
                )
                _decline("post_forced", probe_request)

        return candidates

    def _parse_candidates(
        self, text: str, cluster_name: str, cluster_files: "list[str] | None" = None
    ) -> list[CandidateTask]:
        """Parse the LLM's JSON array into :class:`CandidateTask` objects.

        Ungrounded candidates (missing file, missing symbol AND line_start) are
        logged and dropped.  Any parse error is logged and returns [].

        Fail-closed: never returns a partially-constructed candidate on error.

        Thin backward-compatible wrapper around :meth:`_parse_candidates_ex`,
        which additionally reports whether the JSON array looked truncated
        (AUTO-H4 shrink-retry) or was unsalvageable garbage/empty (AUTO-H5
        plain retry). Kept separate so existing callers (tests included)
        that only want the candidate list don't need to unpack a tuple.
        """
        candidates, _truncated, _unsalvageable = self._parse_candidates_ex(
            text, cluster_name, cluster_files,
        )
        return candidates

    def _parse_candidates_ex(
        self, text: str, cluster_name: str, cluster_files: "list[str] | None" = None
    ) -> "tuple[list[CandidateTask], bool, bool]":
        """Same parsing as :meth:`_parse_candidates`, additionally returning
        two failure-classification flags consumed by ``_review_one_cluster``:

        ``truncated`` (AUTO-H4): the raw text looked like a JSON array cut
        off mid-object (at least one complete object was salvaged via
        ``_salvage_json_objects`` from an otherwise-unparseable tail). This
        means the model tried to fit more into this response than its
        output-token budget allowed — shrinking ``max_tasks`` and re-asking
        is the appropriate fix.

        ``unsalvageable`` (AUTO-H5): the raw text was NOT valid JSON and
        NOTHING could be salvaged from it at all — an empty response, a
        degenerate/repetitive non-JSON ramble, or valid JSON that wasn't
        even a list. This is a different failure mode from truncation:
        there's no partial content to blame on a tight token budget, so
        shrinking ``max_tasks`` wouldn't address it — a plain retry of the
        SAME request is the appropriate fix, since it's often a one-off
        decoding hiccup (or, over a flaky proxy/router, a genuinely empty
        upstream response) rather than a structural budget problem.

        The two flags are mutually exclusive: ``truncated`` implies at
        least one usable candidate was salvaged; ``unsalvageable`` implies
        zero. A clean parse (including a clean, validly-empty ``"[]"``
        response — the model legitimately found nothing to propose) sets
        both to ``False`` and must NOT be retried by either mechanism.
        """
        # Strip optional markdown code fences the model may emit despite instructions.
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Drop first line (```json or ```) and last line (```)
            inner_lines = lines[1:] if len(lines) > 1 else lines
            if inner_lines and inner_lines[-1].strip() == "```":
                inner_lines = inner_lines[:-1]
            stripped = "\n".join(inner_lines).strip()

        truncated = False
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # Salvage the complete objects from a truncated array instead of discarding every candidate (AUTO-CR-11).
            salvaged = _salvage_json_objects(stripped)
            if salvaged:
                truncated = True
                logger.warning(
                    "_parse_candidates [%s]: array was truncated (%s) — "
                    "salvaged %d complete task object(s) from the prefix.",
                    cluster_name, exc, len(salvaged),
                )
                data = salvaged
            else:
                # Nothing salvageable (empty, degenerate or not JSON): flagged so the same request is retried, not a smaller one (AUTO-H5).
                logger.warning(
                    "_parse_candidates [%s]: JSON decode failed: %s\nRaw text: %.400s",
                    cluster_name, exc, text,
                )
                return [], False, True

        if not isinstance(data, list):
            # Valid JSON that is not an array: unsalvageable, since there is no per-item content to extract.
            logger.warning(
                "_parse_candidates [%s]: expected JSON array, got %s",
                cluster_name, type(data).__name__,
            )
            return [], False, True

        candidates: list[CandidateTask] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                logger.debug("_parse_candidates [%s]: item %d is not a dict — skipped", cluster_name, i)
                continue

            # --- Validate required scalar fields ---
            title       = _to_str_or_empty(item.get("title"))
            instruction = _to_str_or_empty(item.get("instruction"))
            acceptance  = _to_str_or_empty(item.get("acceptance_check"))

            # In creative mode, empty acceptance_check is allowed (handled below).
            if not title or not instruction or (not acceptance and self._task_mode != "creative"):
                logger.warning(
                    "_parse_candidates [%s]: item %d missing title/instruction/"
                    "acceptance_check — rejected",
                    cluster_name, i,
                )
                continue

            # --- Validate target_files ---
            target_files = item.get("target_files")
            if not isinstance(target_files, list) or not target_files:
                logger.warning(
                    "_parse_candidates [%s]: item %d has empty/missing target_files — rejected",
                    cluster_name, i,
                )
                continue

            # Control and memory files (bible, synopsis, IMPROVEMENTS.md, plan.json) are never task targets; bounced before Gate 1 (AUTO-CR-28).
            if any(
                str(tf).strip().lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                in RESERVED_META_FILES
                for tf in target_files
            ):
                logger.warning(
                    "_parse_candidates [%s]: item %d targets a reserved "
                    "memory/control file %r — rejected (not editable).",
                    cluster_name, i, target_files,
                )
                continue

            # --- Validate and build cited_location (grounding gate) ---
            loc_raw = item.get("cited_location")

            # Creative grounding repair: a missing or nonexistent cited file is replaced by an existing cluster file (AUTO-CR-10).
            if self._task_mode == "creative":
                existing = list(cluster_files or [])
                anchor_file = _latest_chapter_file(existing)
                if not isinstance(loc_raw, dict):
                    loc_raw = {"file": anchor_file, "symbol": None,
                               "line_start": None, "line_end": None}
                    logger.info(
                        "_parse_candidates [%s]: item %d had no cited_location — "
                        "grounding on existing file %r (creative).",
                        cluster_name, i, anchor_file,
                    )
                else:
                    _cf = (loc_raw.get("file") or "").strip()
                    if anchor_file and _cf not in existing:
                        logger.info(
                            "_parse_candidates [%s]: item %d cited non-existing "
                            "file %r — remapping grounding to %r (creative).",
                            cluster_name, i, _cf, anchor_file,
                        )
                        loc_raw = dict(loc_raw)
                        loc_raw["file"] = anchor_file
                        loc_raw["line_start"] = None
                        loc_raw["line_end"] = None

            if not isinstance(loc_raw, dict):
                logger.warning(
                    "_parse_candidates [%s]: item %d missing cited_location — rejected",
                    cluster_name, i,
                )
                continue

            cited = CitedLocation(
                file       = _to_str_or_empty(loc_raw.get("file")),
                symbol     = (loc_raw.get("symbol") or None),
                line_start = _to_int_or_none(loc_raw.get("line_start")),
                line_end   = _to_int_or_none(loc_raw.get("line_end")),
                new_file   = _to_bool_or(loc_raw.get("new_file", False)),
            )

            if not cited.is_valid(self._task_mode):
                logger.warning(
                    "_parse_candidates [%s]: item %d cited_location lacks file "
                    "and/or anchor (symbol/line_start) — rejected. loc=%r",
                    cluster_name, i, loc_raw,
                )
                continue

            # An empty acceptance_check is allowed and defaults to true, as creative_acceptance_default says (AUTO-CR-3).
            if not acceptance:
                _acc_default = (
                    safe_getboolean(self._config, "auto", "creative_acceptance_default", fallback=True)
                    if self._task_mode == "creative"
                    else False
                )
                if _acc_default:
                    acceptance = "true"
                else:
                    logger.warning(
                        "_parse_candidates [%s]: item %d missing acceptance_check — rejected",
                        cluster_name, i,
                    )
                    continue

            # Prose has no meaningful shell check, so creative acceptance is forced to the no-op true (AUTO-CR-17).
            if self._task_mode == "creative" and acceptance.strip().lower() != "true":
                logger.info(
                    "_parse_candidates [%s]: item %d — overriding creative "
                    "acceptance_check %r with 'true' (no shell test for prose).",
                    cluster_name, i, acceptance,
                )
                acceptance = "true"

            candidates.append(CandidateTask(
                title            = title,
                instruction      = instruction,
                target_files     = [str(p) for p in target_files],
                acceptance_check = acceptance,
                cited_location   = cited,
                cluster          = cluster_name,
                raw              = item,
            ))

        rejected = len(data) - len(candidates)
        if rejected:
            logger.info(
                "_parse_candidates [%s]: %d/%d candidate(s) rejected (ungrounded/malformed)",
                cluster_name, rejected, len(data),
            )

        # Creative tasks are capped at max_tasks_creative: a small model emits overlapping tasks that collapse chapters (AUTO-CR-18).
        if self._task_mode == "creative":
            # A malformed max_tasks_creative falls back to 1 instead of crashing the plan phase (FIX-1 #6).
            try:
                cap = self._config.getint("architect", "max_tasks_creative", fallback=1)
            except ValueError as exc:
                logger.warning(
                    "config [architect] max_tasks_creative is malformed (%s) — using 1",
                    exc,
                )
                cap = 1
            cap = max(1, cap)
            if len(candidates) > cap:
                logger.info(
                    "_parse_candidates [%s]: creative cap — keeping %d of %d task(s).",
                    cluster_name, cap, len(candidates),
                )
                candidates = candidates[:cap]

        return candidates, truncated, False

    def _build_file_contents(self, files: list[str], base_dir: Path) -> str:
        """Read and annotate file contents for the prompt.

        Each file is prefixed with a ``### path/to/file.py`` header.
        Content is truncated to ``self._max_file_chars`` (configured via
        ``max_file_chars`` in the ``[architect]`` section of agents.ini) with a
        notice so the model knows the file continues beyond the excerpt.
        """
        parts: list[str] = []
        for rel_path in files:
            abs_path = base_dir / rel_path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("_build_file_contents: cannot read %s: %s", rel_path, exc)
                content = f"[unreadable: {exc}]"

            if len(content) > self._max_file_chars:
                content = content[:self._max_file_chars] + f"\n... [truncated — {len(content) - self._max_file_chars} more chars]"

            parts.append(f"### {rel_path}\n{content}")

        return "\n\n".join(parts)


# Convenience factory

def _salvage_json_objects(text: str) -> list:
    """Extract complete top-level JSON objects from a (possibly truncated) array.

    A small model often emits ``[ {..}, {..}, {..  ← cut off here``. ``json.loads``
    then fails on the whole string. This scanner walks the text tracking string
    state and brace depth, and returns every ``{...}`` object that closed
    cleanly at array level, parsing each independently. The truncated trailing
    object is silently dropped. Returns ``[]`` when nothing complete is found.
    """
    objects: list = []
    depth = 0          # brace depth
    in_str = False
    escape = False
    start = -1
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass  # malformed individual object — skip
                    start = -1
    return objects


def _latest_chapter_file(files: "list[str]") -> str:
    """Return the best existing file to ground a creative continuation on.

    Prefers the highest-numbered ``chapter_<N>`` file (the most recent chapter
    the new one continues from); falls back to the last file alphabetically.
    Returns ``""`` when *files* is empty.
    """
    import re as _re
    if not files:
        return ""
    rx = _re.compile(r"chapter[_\-\s]?(\d+)", _re.IGNORECASE)
    numbered: list[tuple[int, str]] = []
    for f in files:
        m = rx.search(str(f))
        if m:
            numbered.append((int(m.group(1)), f))
    if numbered:
        numbered.sort()
        return numbered[-1][1]
    return sorted(files)[-1]


def _batch_fingerprint(base_dir: "Path", files: list[str]) -> str:
    """Short hash of a batch's files (relative path + size + mtime) used to make
    the architect checkpoint content-aware. Any change to the reviewed file set
    or contents yields a new key, so a stale cached result is never replayed.
    Missing/unreadable files hash as a sentinel rather than raising.

    Delegates to tools.auto.utils.file_set_fingerprint — the same content-aware
    fingerprint plan_emitter.py's changed_clusters() now uses, so the two
    "has this content actually changed?" checks in the PLAN phase can't drift
    out of sync with each other again.
    """
    return file_set_fingerprint(base_dir, files)


def _serialise_candidates(candidates: list[CandidateTask]) -> list[dict]:
    """Convert CandidateTask objects to plain dicts for JSON checkpoint storage."""
    out = []
    for c in candidates:
        out.append({
            "title":            c.title,
            "instruction":      c.instruction,
            "target_files":     c.target_files,
            "acceptance_check": c.acceptance_check,
            "cluster":          c.cluster,
            "cited_location": {
                "file":       c.cited_location.file,
                "symbol":     c.cited_location.symbol,
                "line_start": c.cited_location.line_start,
                "line_end":   c.cited_location.line_end,
                "new_file":   c.cited_location.new_file,
            },
            "raw": c.raw,
        })
    return out


def _deserialise_candidates(data: list[dict]) -> list[CandidateTask]:
    """Reconstruct CandidateTask objects from a JSON checkpoint list."""
    result = []
    for item in data:
        loc = item.get("cited_location", {})
        result.append(CandidateTask(
            title            = item.get("title", ""),
            instruction      = item.get("instruction", ""),
            target_files     = item.get("target_files", []),
            acceptance_check = item.get("acceptance_check", ""),
            cluster          = item.get("cluster", ""),
            cited_location   = CitedLocation(
                file       = loc.get("file", ""),
                symbol     = loc.get("symbol"),
                line_start = loc.get("line_start"),
                line_end   = loc.get("line_end"),
                new_file   = bool(loc.get("new_file", False)),
            ),
            raw = item.get("raw", {}),
        ))
    return result


def review_clusters(
    clusters: list[RepoCluster],
    base_dir: str | Path,
    config: configparser.ConfigParser,
    goal: str = "improve current code",
    *,
    task_mode: str = "code",
    on_cluster_done=None,
    checkpoint_path: "Path | str | None" = None,
) -> list[CandidateTask]:
    """One-call entry point for ``AutoController``.

    Reads API settings from *config* (same ``[api]`` / ``[api_local]`` /
    ``[api_remote]`` convention used throughout this codebase) and delegates to
    :class:`ClusterReviewer`.

    Parameters
    ----------
    clusters:
        Output of :func:`tools.auto.repo_ingest.ingest_repo`.
    base_dir:
        Root of the project being reviewed.
    config:
        Parsed ``agents.ini``.
    goal:
        The user's improvement goal string.
    checkpoint_path:
        Optional path to a JSON file used to persist per-cluster results.
        Pass ``agent_dir / "architect_checkpoint.json"`` from the controller
        to survive LLM crashes and HTTP 500 storms across restarts.

    Returns
    -------
    list[CandidateTask]
        Grounded candidates, ready for Gate 1 (AUTO-B3).
    """
    active    = config.get("api", "active", fallback="local")
    section   = f"api_{active}"
    base_url  = config.get(section, "base_url")
    api_key   = config.get(section, "api_key",    fallback="")
    model     = config.get(section, "model")
    api_fmt   = config.get(section, "api_format", fallback="openai")
    verify_ssl = safe_getboolean(config, "api", "verify_ssl", fallback=True)

    reviewer = ClusterReviewer(
        config=config,
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=api_fmt,
        verify_ssl=verify_ssl,
        task_mode=task_mode,
    )
    return reviewer.review_clusters(
        clusters, base_dir, goal,
        on_cluster_done=on_cluster_done,
        checkpoint_path=Path(checkpoint_path) if checkpoint_path else None,
    )


# TaskRewriter (LOOP-2)

_REWRITER_SYSTEM_DEFAULT = (
    "You are an architect who has reviewed failed implementation attempts. "
    "First, read the failure history and classify the failure. There are two "
    "distinct kinds: (a) the CODE is wrong — the acceptance_check ran and "
    "reported a real defect (assertion error, wrong output, test failure, "
    "compile error in the edited file); or (b) the acceptance_check COMMAND "
    "ITSELF could not run — meaning the failure occurred before or instead of "
    "any test logic executing, because the checker process failed to launch or "
    "initialize (missing binary, missing dependency, bad path, bad interpreter, "
    "insufficient permissions, or any other environment issue). "
    "Case (b) is an environment/verification problem, NOT a code defect: the "
    "implementation may already be correct. In case (b) do NOT keep re-issuing "
    "the same un-runnable command and do NOT thrash the code — instead change "
    "acceptance_check to a verification that CAN run in this environment. "
    "Only in case (a) should you propose a genuinely different technical approach. "
    "Do not suggest the same solution with minor changes. "
    "If the previous approach used class inheritance, consider composition. "
    "If it used a loop, consider a generator. "
    "If it modified data in place, consider returning a new object. "
    "Think about what assumption the previous approach made that might be wrong, "
    "and start from a different assumption. "
    "Return ONLY a JSON object — no prose, no markdown fences, no preamble."
)

_REWRITER_USER_TMPL = """\
The following task has failed multiple implementation rounds. You must produce \
a completely different implementation strategy — repeating the same approach is \
not acceptable.

Original task title: {title}

Original instruction:
{instruction}

Failure history (one entry per failed round):
{failure_history}

Return a JSON object with exactly these fields:
{{
  "title": "<keep original or append '— alternative approach'>",
  "instruction": "<new implementation strategy written for the coder agent>",
  "acceptance_check": "<MUST be a real runnable shell command that exits 0 when done. KEEP the original command if the code simply produced a wrong result. BUT if the failure history shows the command itself could not execute (exit 127 / 'not found' / 'No such file' / 'permission denied'), REPLACE it with a check that can run here: a different runner for the same language (e.g. 'gradle test' or 'sh gradlew test' instead of './gradlew test', or 'mvn -q test'), a lighter compile-only check, or 'true' as a last resort — with 'true' the change is judged by code review alone. Never repeat a command that already failed to execute. Do NOT replace with a prose description.>"
}}
"""


class TaskRewriter(_llm_stream.LLMClientBase):
    """Rewrites a repeatedly-failing task with a new implementation strategy.

    Mirrors the constructor signature of :class:`ClusterReviewer` so
    ``make_outer_loop`` can build it with the same config block.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    base_url, api_key, model, api_format, verify_ssl:
        Same meaning as in :class:`ClusterReviewer`.
    """

    def __init__(
        self,
        config: configparser.ConfigParser,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "openai",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(config, base_url, api_key, model, api_format, verify_ssl)

        arch = "architect"
        # Each rewriter setting is converted with a fallback to its default, so a malformed key cannot break construction (FIX-1 #7).
        try:
            self._max_tokens = int(
                config.get(arch, "rewrite_max_tokens", fallback="512")
            )
        except ValueError as exc:
            logger.warning(
                "config [%s] rewrite_max_tokens is malformed (%s) — using 512",
                arch, exc,
            )
            self._max_tokens = 512
        try:
            self._temperature = float(
                config.get(arch, "rewrite_temperature", fallback="0.4")
            )
        except ValueError as exc:
            logger.warning(
                "config [%s] rewrite_temperature is malformed (%s) — using 0.4",
                arch, exc,
            )
            self._temperature = 0.4
        try:
            self._think = config.getboolean(arch, "think", fallback=False)
        except ValueError as exc:
            logger.warning(
                "config [%s] think is malformed (%s) — using False", arch, exc,
            )
            self._think = False
        raw_system = config.get(arch, "rewrite_system", fallback="").strip()
        self._system = raw_system or _REWRITER_SYSTEM_DEFAULT
        try:
            self._timeout = float(
                config.get("loop", "timeout_seconds", fallback="300")
            )
        except ValueError as exc:
            logger.warning(
                "config [loop] timeout_seconds is malformed (%s) — using 300", exc,
            )
            self._timeout = 300.0
        active_profile    = config.get("api", "active", fallback="local")
        try:
            self._num_ctx = config.getint(
                f"api_{active_profile}", "num_ctx", fallback=0
            )
        except ValueError as exc:
            logger.warning(
                "config [api_%s] num_ctx is malformed (%s) — using 0",
                active_profile, exc,
            )
            self._num_ctx = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def rewrite(self, task: dict, failure_history: list[str]) -> dict:
        """Return a new task dict with a different implementation strategy.

        On any failure (network error, bad JSON, missing fields) logs a warning
        and returns the *original* task dict unchanged.  Never raises.
        """
        title       = task.get("title", "")
        instruction = task.get("instruction", "")

        history_text = "\n\n".join(
            f"--- Round {i + 1} ---\n{entry}"
            for i, entry in enumerate(failure_history)
        ) if failure_history else "(no failure history available)"

        user_msg = _REWRITER_USER_TMPL.format(
            title=title,
            instruction=instruction,
            failure_history=history_text,
        )

        url, headers, payload = _llm_stream.build_chat_request(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            api_format=self._api_format, temperature=self._temperature,
            max_tokens=self._max_tokens, system=self._system, user_msg=user_msg,
            num_ctx=self._num_ctx, think=self._think,
        )

        tracer.event(
            source="task_rewriter",
            target="llm",
            kind="llm_request",
            content=user_msg, model=self._model,
            params={"model": self._model, "task": task.get("id", "?")},
        )

        try:
            raw = _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self._timeout,
                api_format=self._api_format,
                ssl_context=self._ssl_context,
                **self._retry_kwargs,
            )
        except Exception as exc:
            logger.warning("TaskRewriter: LLM call failed for task %r: %s",
                           task.get("id", "?"), exc)
            return task

        cleaned = strip_think(raw or "")
        tracer.event(
            source="llm",
            target="task_rewriter",
            kind="llm_response",
            content=cleaned, model=self._model,
            params={"task": task.get("id", "?")},
        )

        return self._parse_rewrite(cleaned, task)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _parse_rewrite(self, text: str, original_task: dict) -> dict:
        """Parse the rewrite JSON and merge it into a copy of *original_task*.

        Returns *original_task* unchanged on any parse error.
        """
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            stripped = "\n".join(inner).strip()

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning(
                "TaskRewriter._parse_rewrite: JSON decode failed: %s\nRaw: %.400s",
                exc, text,
            )
            return original_task

        if not isinstance(data, dict):
            logger.warning(
                "TaskRewriter._parse_rewrite: expected JSON object, got %s",
                type(data).__name__,
            )
            return original_task

        new_title       = _to_str_or_empty(data.get("title"))
        new_instruction = _to_str_or_empty(data.get("instruction"))
        new_acceptance  = _to_str_or_empty(data.get("acceptance_check"))

        if not new_instruction:
            logger.warning(
                "TaskRewriter._parse_rewrite: rewrite produced empty instruction — "
                "keeping original task"
            )
            return original_task

        # A rewritten acceptance_check that reads like prose (no known executable) is dropped: bash would fail every attempt with exit 127.
        if new_acceptance and not _looks_like_shell_command(new_acceptance):
            logger.warning(
                "TaskRewriter._parse_rewrite: acceptance_check looks like prose, "
                "not a shell command — keeping original: %r -> %r",
                new_acceptance[:120], original_task.get("acceptance_check", ""),
            )
            new_acceptance = ""   # force fallback to original below

        # An identical rewrite returns the original task object, so outer_loop's identity check sees no change and no rewrite slot is wasted.
        orig_instruction = original_task.get("instruction", "").strip()
        orig_acceptance  = original_task.get("acceptance_check", "").strip()
        effective_acceptance = new_acceptance or orig_acceptance
        if (
            _normalise(new_instruction) == _normalise(orig_instruction)
            and _normalise(effective_acceptance) == _normalise(orig_acceptance)
        ):
            logger.warning(
                "TaskRewriter._parse_rewrite: rewrite is semantically identical "
                "to the original task — returning original object so outer_loop "
                "identity check short-circuits correctly"
            )
            return original_task

        # Merge into a shallow copy so the original dict is never mutated.
        rewritten = dict(original_task)
        rewritten["title"]            = new_title or original_task.get("title", "")
        rewritten["instruction"]      = new_instruction
        rewritten["acceptance_check"] = effective_acceptance
        return rewritten


# Internal utilities

# Known executable tokens that legitimately start an acceptance_check command.
_SHELL_COMMAND_PREFIXES: tuple[str, ...] = (
    "python", "python3", "pytest", "bash", "sh", "node", "npm", "npx",
    "make", "cargo", "go ", "ruby", "rspec", "php", "java ", "mvn",
    "gradle", "gradlew", "mvnw", "dotnet", "true", "false",
    "./", "/",
)

# Heuristic upper bound: real commands are short; prose descriptions are long.
_MAX_ACCEPTANCE_CHECK_CHARS = 300

_SENTENCE_END_RE = _re.compile(r"\.\s+[A-Z]")


def _looks_like_shell_command(text: str) -> bool:
    """Return True when *text* looks like a real shell command.

    A shell command:
    - starts with a known executable token (python, pytest, bash, …)
    - is short (< 300 chars — prose descriptions are typically longer)
    - does not contain multiple sentences (". Capital" pattern)

    Used by TaskRewriter._parse_rewrite to reject LLM-generated prose that
    accidentally replaces a valid acceptance_check, which would cause the
    executor to fail with exit 127 ("command not found") on every attempt.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > _MAX_ACCEPTANCE_CHECK_CHARS:
        return False
    if _SENTENCE_END_RE.search(stripped):
        return False
    lower = stripped.lower()
    return any(lower.startswith(prefix) for prefix in _SHELL_COMMAND_PREFIXES)


def _normalise(text: str) -> str:
    """Collapse whitespace for semantic-identity comparison.

    Strips leading/trailing whitespace and compresses internal runs of
    whitespace (including newlines) to a single space.  Used by
    ``TaskRewriter._parse_rewrite`` to detect when a rewrite produced the
    same instruction/acceptance_check content as the original task, even
    if the LLM emitted different surrounding whitespace.
    """
    return _re.sub(r"\s+", " ", text.strip())


def _fire_callback(cb) -> None:
    """Call *cb* if not None; swallow any exception it raises."""
    if cb is None:
        return
    try:
        cb()
    except Exception as exc:  # noqa: BLE001
        logger.debug("on_cluster_done callback raised (ignored): %s", exc)


def _to_int_or_none(val: Any) -> int | None:
    """Coerce *val* to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_str_or_empty(val: Any) -> str:
    """Coerce *val* to a stripped str, or "" when it isn't string-like.

    BUGFIX: callers previously did ``(val or "").strip()``, which assumes
    *val* is already a string whenever it's truthy — a truthy non-string
    LLM field (e.g. ``123``, ``["x"]``) crashed ``.strip()`` with
    AttributeError. Treating a non-string value the same as an absent one
    lets the existing "missing required field" validation just below each
    call site reject the candidate with a proper warning instead of the
    whole run crashing on one malformed field.
    """
    return val.strip() if isinstance(val, str) else ""


def _to_bool_or(val: Any, default: bool = False) -> bool:
    """Coerce *val* to bool, treating common string spellings sensibly.

    BUGFIX (audit): ``bool(loc_raw.get("new_file", False))`` treated an
    LLM-emitted JSON *string* like ``"new_file": "false"`` as True — any
    non-empty string is truthy in Python — silently marking an
    existing-file task as new-file and bypassing the symbol/line
    grounding requirement. A string value is now matched against common
    false/true spellings instead of just checked for truthiness.
    """
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "no", "0", "")
    return bool(val)
