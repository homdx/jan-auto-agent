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
from tools.auto.repo_ingest import RepoCluster
from tools.auto.utils import atomic_write_text, file_set_fingerprint
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think

logger = logging.getLogger(__name__)

# ── Architect system prompts ──────────────────────────────────────────────────
# Injected as the system role.  Can be overridden via agents.ini [architect] system.

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

# ── AUTO-CR-20-3: Plan-validator prompt ───────────────────────────────────────
# Narrow: contradictions and missing required facts only — never style/ordering.

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

# ── Per-cluster user prompt template ─────────────────────────────────────────
# {goal}        — the user's overall improvement goal
# {cluster}     — cluster name
# {file_listing}— newline-separated list of relative paths in this cluster
# {file_contents}—concatenated, annotated file contents

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

# AUTO-CR-34: creative tasks are prose, not code — the shared code template
# demands a "symbol" and shell-test acceptance_check, which contradicts the
# creative prompt and makes the model emit meaningless shell commands. This
# variant drops that guidance and asks for prose-appropriate tasks while
# keeping the exact-path rules.
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

# _MAX_FILE_CHARS and _MAX_FILES_PER_REVIEW are now read from [architect] in
# agents.ini (max_file_chars / max_files_per_review).
# The fallback values below apply only when the keys are absent from the config.
_DEFAULT_MAX_FILE_CHARS    = 4000
_DEFAULT_MAX_FILES_PER_REVIEW = 6


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CitedLocation:
    """Grounding reference: where in the codebase the candidate applies."""
    file: str
    symbol: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    new_file: bool = False

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


# ─────────────────────────────────────────────────────────────────────────────
# ClusterReviewer
# ─────────────────────────────────────────────────────────────────────────────

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

    # AUTO-H5-ESCALATE-1: mirrors gate1_filter.py's Gate1Filter
    # _UNPARSEABLE_* escalation — an "unsalvageable" (empty/degenerate/
    # non-JSON) response on a reasoning-heavy model is often the SAME
    # root cause Gate 1 already handles: the model's thinking channel
    # (see AUTO-ZAITHINK-1 / AUTO-THINKDEPTH-2 in tools/llm_stream.py)
    # consumed the entire max_tokens budget before any JSON was written,
    # not a one-off decoding hiccup a same-settings plain retry can fix.
    # Each retry (bounded by [architect] empty_response_retry_max, same
    # config key as before — just now escalating instead of static) both
    # widens the token budget and lowers temperature, exactly like Gate
    # 1's presence-check retry. Floor/ceiling/step deliberately match
    # Gate1Filter's constants — same failure mode, same fix.
    _UNSALVAGEABLE_TOKENS_FLOOR = 4096
    _UNSALVAGEABLE_TOKENS_STEP_MULT = 2.0
    _UNSALVAGEABLE_TOKENS_DEFAULT_CEILING = 32768
    _UNSALVAGEABLE_TOKENS_CTX_FRACTION = 0.5
    _UNSALVAGEABLE_TEMPERATURE_STEP = 0.1
    _UNSALVAGEABLE_TEMPERATURE_FLOOR = 0.0

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
        self._temperature            = float(config.get(arch, "temperature",         fallback="0.2"))
        # AUTO-CR-11: mode-aware token cap — creative tasks (Cyrillic, longer
        # instructions) need more room or the JSON array truncates mid-object.
        from tools.auto.utils import _cfg_mode
        self._max_tokens             = int(_cfg_mode(config, arch, "max_tokens", task_mode, fallback="2048"))
        self._timeout                = float(config.get("loop", "timeout_seconds",   fallback="300"))
        self._max_file_chars         = int(config.get(arch,   "max_file_chars",      fallback=str(_DEFAULT_MAX_FILE_CHARS)))
        self._max_files_per_review   = int(config.get(arch,   "max_files_per_review", fallback=str(_DEFAULT_MAX_FILES_PER_REVIEW)))
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        active_profile               = config.get("api", "active", fallback="local")
        self._num_ctx                = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        # AUTO-FIX (fable follow-up): mirrors the gate1_filter.py fix — a
        # thinking model (e.g. qwen3) wraps its JSON task array in
        # a "think" reasoning block by default. If that reasoning consumes the
        # whole max_tokens budget, strip_think() discards everything and
        # architect produces 0 candidates (observed live: architect call
        # returned a totally empty response after ~5 minutes on qwen3:8b).
        # Default to disabling thinking for this call; [architect] think =
        # true re-enables it for a model/setup that needs it.
        self._think                  = config.getboolean(arch, "think", fallback=False)

        # ── DM-2: select system prompt based on task_mode + ini overrides ─────
        # Priority: mode-specific ini key > legacy "system" key > built-in constant.
        mode_ini_key = f"system_{task_mode}" if task_mode != "code" else None
        if mode_ini_key and config.has_option(arch, mode_ini_key):
            self._system = config.get(arch, mode_ini_key).strip()
        else:
            built_in = _SYSTEM_PROMPTS.get(task_mode, _SYSTEM_PROMPT_CODE)
            self._system = config.get(arch, "system", fallback=built_in).strip()

        # ── AUTO-CR-20-3: LLM callable for validate_plan ─────────────────────
        # Built lazily on first call if construction fails so __init__ never
        # raises just because summary_memory is unavailable.
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

            # Split large clusters into batches so each LLM prompt stays within
            # a local model's context window (overflow => hallucinated paths).
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
                # Content-aware key: a fingerprint of the batch files is appended so
                # the checkpoint is invalidated when the reviewed content changes
                # (file edits, or a different file set after a max_depth / cluster
                # change). Without this a stale result would be replayed forever.
                batch_key = f"{sub.name}||{goal}||{_batch_fingerprint(base_dir, sub.files)}"

                # ── Checkpoint hit: skip LLM call, reuse saved candidates ────
                if checkpoint_path is not None and batch_key in checkpoint:
                    saved = checkpoint[batch_key]
                    restored = _deserialise_candidates(saved)
                    print(f"   ♻️  Cluster/batch '{sub.name}' restored from checkpoint "
                          f"({len(restored)} candidate(s)) — skipping LLM call")
                    cluster_candidates.extend(restored)
                    continue

                # ── Live LLM call ─────────────────────────────────────────────
                _all_files = files if (len(batches) > 1 and self._task_mode == "creative") else None
                batch_results = self._review_one_cluster(sub, base_dir, goal, all_files=_all_files)
                # None means the LLM call failed outright (all retries exhausted).
                # Do NOT checkpoint a failure — the batch must be retried on next run.
                if batch_results is None:
                    continue
                cluster_candidates.extend(batch_results)

                # ── Checkpoint save: persist immediately after each batch ─────
                # Only persist NON-EMPTY results. An empty list means the LLM
                # found nothing — caching that would prevent a retry after the
                # inputs are fixed (e.g. raising max_depth to expose source).
                if checkpoint_path is not None and batch_results:
                    checkpoint[batch_key] = _serialise_candidates(batch_results)
                    try:
                        # Bugfix: this used to be a plain checkpoint_path.write_text(),
                        # which truncates-then-writes — a process killed mid-write
                        # (the exact "LLM crash or HTTP 500 storm" scenario this
                        # checkpoint exists to survive) could leave invalid JSON
                        # behind. The loader below then silently resets to an
                        # empty checkpoint on ANY parse failure, discarding every
                        # previously-cached batch result and forcing a full,
                        # expensive re-review. atomic_write_text (temp file +
                        # os.replace) guarantees the file on disk is always either
                        # the old complete content or the new complete content —
                        # see state.py's StateStore._atomic_write, which fixed the
                        # identical bug for plan.json/progress.json.
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

            # AUTO-BUG fix: batches use a sub-cluster named
            # "<cluster.name> (batch N/M)" (see `sub = ... RepoCluster(name=...)`
            # above) so each batch can be checkpointed independently. That
            # suffixed name must never leak onto CandidateTask.cluster: it is
            # stored there verbatim by _parse_candidates (called from
            # _review_one_cluster with `sub.name`/`cluster.name` as seen from
            # inside that helper), and tools/auto/pipeline.py's cluster_files
            # lookup — used by Gate 1 to catch hallucinated file citations —
            # is keyed by the plain, un-suffixed cluster name straight from
            # repo_ingest. `cluster_files.get(candidate.cluster, set())` then
            # silently returns an empty set for every candidate from a
            # batched review, and gate1_filter.py's `if known and loc.file
            # not in known:` guard — an empty set is falsy — never runs, so
            # the "catch hallucinated paths before filesystem I/O" safety net
            # is disabled for every cluster large enough to need more than
            # one review batch, which in practice is the common case. Apply
            # this once, after the batch loop, so it repairs candidates from
            # BOTH the live-LLM branch above (`cluster_candidates.extend(
            # batch_results)`) AND any restored from an on-disk checkpoint
            # written before this fix existed (`cluster_candidates.extend(
            # restored)` on a checkpoint hit) — not just one of the two.
            for _c in cluster_candidates:
                _c.cluster = cluster.name

            print(f"   → {len(cluster_candidates)} grounded candidate(s)")

            # AUTO-BUG-3 fix: when a cluster is large enough to be split into
            # multiple batches, each batch is reviewed independently and can
            # (and in practice does, e.g. in creative mode with a single
            # target file) propose the *identical* task. Gate-1 has its own
            # fingerprint-based dedup, but de-duplicating right here — before
            # candidates from different batches of the same cluster are even
            # merged — is a cheap, local belt-and-braces fix that does not
            # depend on downstream Gate-1 behaving as expected.
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

    def _build_llm_call(self):
        """Build a ``llm_call(system, user) -> str`` callable for validate_plan.

        Uses the same ``_make_llm_call`` factory as SummaryMemory / CanonValidator.
        Falls back to a no-op that always returns ``""`` (fail-open) if the
        import fails, so __init__ never raises.
        """
        try:
            from tools.auto.summary_memory import _make_llm_call  # noqa: PLC0415
            return _make_llm_call(self._config, task_mode=self._task_mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ClusterReviewer._build_llm_call failed — validate_plan will fail-open: %s", exc)
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

        # AUTO-H4: shrink-retry on truncation. A "confirmed" candidate array
        # cut off mid-object (see _parse_candidates_ex's truncated flag) means
        # the model tried to fit more tasks into this response than its
        # output-token budget allowed — the fix that actually addresses the
        # cause is asking for FEWER tasks (a smaller JSON array is less likely
        # to overflow the same max_tokens cap), not just re-sending the same
        # over-ambitious request and hoping for a shorter roll. Each shrink
        # halves the requested task count (floor 1) and is a fresh, independent
        # LLM call — not a merge with the previous partial result, since a
        # re-ask under a smaller budget is meant to complete cleanly on its
        # own, and mixing salvaged fragments from two different requests risks
        # duplicate/inconsistent candidates.
        #
        # Configurable via [architect]:
        #   truncation_retry_max    — how many shrink attempts after the
        #                             first try (default 2; 0 disables this
        #                             and preserves the old "keep whatever
        #                             was salvaged from the first response"
        #                             behaviour).
        #   truncation_shrink_factor — multiplier applied to max_tasks on each
        #                              shrink (default 0.5; clamped to (0, 1)
        #                              and to never fail to shrink at all).
        _retry_max = self._config.getint("architect", "truncation_retry_max", fallback=2)
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

        # AUTO-H5 / AUTO-H5-ESCALATE-1: retry on an unsalvageable
        # (empty/garbage/degenerate) response — a distinct failure mode
        # from AUTO-H4's truncation. There's no partial content here to
        # blame on a tight max_tasks budget, so shrinking wouldn't help.
        # Originally a same-settings plain retry (typical "one-off
        # decoding hiccup" cause: a model repeating "title": "title": ...
        # until it hits its cap, or a flaky upstream/proxy returning an
        # empty body) — now ALSO escalates max_tokens/temperature each
        # attempt (see the class docstring's AUTO-H5-ESCALATE-1 note),
        # since a genuinely empty response is just as often a
        # reasoning-heavy model (GLM-5.2 and similar) burning the entire
        # budget on <think>/thinking-channel output before writing any
        # JSON — a plain identical retry never recovers from that, an
        # escalating one usually does.
        #
        # Configurable via [architect]:
        #   empty_response_retry_max — how many escalating retries after
        #                              the first try when a response is
        #                              unsalvageable (default 5; 0
        #                              disables this and preserves the
        #                              old "0 candidates from this batch,
        #                              move on" behaviour for that
        #                              failure mode). Previously 2 — the
        #                              cap didn't matter much when every
        #                              attempt used identical settings,
        #                              but each of the now-escalating
        #                              attempts has a real chance to
        #                              recover, so more of them are
        #                              worth spending.
        _empty_retry_max = self._config.getint("architect", "empty_response_retry_max", fallback=5)
        _empty_retry_max = max(0, _empty_retry_max)

        def _call_and_parse(
            max_tasks: int, *,
            max_tokens: "int | None" = None,
            temperature: "float | None" = None,
        ) -> "tuple[list[CandidateTask] | None, bool, bool]":
            """One LLM call + parse at a given max_tasks budget.

            Returns ``(candidates_or_None, truncated, unsalvageable)``.
            ``None`` candidates means the call itself failed after all
            transient-error retries (unrelated to either AUTO-H4/H5 —
            see the network-retry loop below); ``truncated`` and
            ``unsalvageable`` are only meaningful when candidates is not
            ``None``, and are mutually exclusive (see
            ``_parse_candidates_ex``'s docstring).

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

            url, headers, payload = _llm_stream.build_chat_request(
                base_url=self._base_url, api_key=self._api_key, model=self._model,
                api_format=self._api_format,
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens if max_tokens is None else max_tokens,
                system=self._system, user_msg=user_msg,
                num_ctx=self._num_ctx, think=self._think,
            )

            # AUTO-H5-ESCALATE-1: the effective temperature actually sent
            # this call — computed directly rather than read back out of
            # `payload`, since the ollama branch nests it under
            # payload["options"]["temperature"] (no top-level key there),
            # which would have silently traced the wrong value.
            _effective_temperature = self._temperature if temperature is None else temperature

            # Trace the outgoing call.
            tracer.event(
                source="architect",
                target="llm",
                kind="llm_request",
                content=user_msg,
                params={"model": self._model, "temperature": _effective_temperature,
                        "cluster": cluster.name, "max_tasks": max_tasks},
            )

            # AUTO-BUG-8 fix: configurable retry backoff. Previously hardcoded to
            # [5, 15, 30] (~50s total) regardless of deployment — for a local
            # Ollama instance still loading a model into memory, that's ~50s of
            # silent retrying inside this call before the cluster fails open.
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
                    )
                    # request_completion returns the full accumulated response; prefer it
                    # and fall back to the streamed tokens if the return is empty.
                    raw_text = returned or "".join(tokens_list)
                    print("\n" + "═" * 80 + "\n")
                    last_exc = None
                    break  # success — exit retry loop

                except Exception as exc:
                    last_exc = exc
                    exc_str = str(exc)
                    # Only retry on transient server-side errors (5xx) or connection
                    # failures, not deterministic 4xx client errors. Prefer the real
                    # status from exc.code (urllib's HTTPError); only fall back to a
                    # word-boundary scan of the message for non-HTTP errors, to avoid
                    # false positives from substrings like a model name "gpt-4-5000".
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
                    source="llm", target="architect", kind="llm_response",
                    content=f"[ERROR] {last_exc}", params={"cluster": cluster.name},
                )
                # None (not []) so callers can distinguish "call failed" from
                # "call succeeded but LLM produced no valid candidates".  The
                # checkpoint must NOT record a failed batch; None signals that.
                return None, False, False

            # Strip reasoning tokens before JSON parsing.
            cleaned = strip_think(raw_text)

            tracer.event(
                source="llm",
                target="architect",
                kind="llm_response",
                content=cleaned,
                params={"cluster": cluster.name, "max_tasks": max_tasks},
            )

            return self._parse_candidates_ex(cleaned, cluster.name, cluster.files)

        max_tasks = _base_max_tasks
        candidates, truncated, unsalvageable = _call_and_parse(max_tasks)

        _shrink_attempts = 0
        _empty_attempts = 0
        # Single adaptive loop: on each iteration, react to whichever
        # failure mode THIS attempt actually hit — a batch can legitimately
        # flip between them (e.g. truncated on attempt 1, unsalvageable on
        # a shrunk attempt 2) — rather than committing to one strategy for
        # the whole retry budget upfront. Each failure mode has its own
        # independent attempt budget so one doesn't steal retries from the
        # other.
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
                # AUTO-H5-ESCALATE-1: mirrors gate1_filter.py's
                # Gate1Filter unparseable-retry escalation exactly — an
                # unsalvageable (empty/degenerate/non-JSON) response is
                # very often the SAME thinking-budget-exhaustion failure
                # Gate 1 already handles (see the class docstring above),
                # not a one-off decoding hiccup a same-settings plain
                # retry fixes. Doubles from a healthy floor each attempt
                # (capped at a fraction of num_ctx, or a flat default when
                # num_ctx is unset), and nudges temperature down a little
                # to reduce rambling.
                _ctx_ceiling = (
                    int(self._num_ctx * self._UNSALVAGEABLE_TOKENS_CTX_FRACTION)
                    if self._num_ctx
                    else self._UNSALVAGEABLE_TOKENS_DEFAULT_CEILING
                )
                _attempt_tokens = min(
                    self._UNSALVAGEABLE_TOKENS_FLOOR
                    * int(self._UNSALVAGEABLE_TOKENS_STEP_MULT ** (_empty_attempts - 1)),
                    _ctx_ceiling,
                )
                _attempt_temp = max(
                    self._temperature - _empty_attempts * self._UNSALVAGEABLE_TEMPERATURE_STEP,
                    self._UNSALVAGEABLE_TEMPERATURE_FLOOR,
                )
                _call_kwargs = {"max_tokens": _attempt_tokens, "temperature": _attempt_temp}
                logger.warning(
                    "review_one_cluster [%s]: response unsalvageable (empty, "
                    "degenerate, or non-JSON) — plain retry %d/%d at the "
                    "same max_tasks=%d (max_tokens=%d, temperature=%.2f).",
                    cluster.name, _empty_attempts, _empty_retry_max, max_tasks,
                    _attempt_tokens, _attempt_temp,
                )
            candidates, truncated, unsalvageable = _call_and_parse(max_tasks, **_call_kwargs)

        if candidates is not None and truncated:
            # Shrink-retries exhausted (or disabled via
            # truncation_retry_max=0) and the LAST attempt was still
            # truncated — keep whatever got salvaged rather than
            # discarding it outright; this matches the pre-AUTO-H4
            # fail-open behaviour for the final attempt.
            logger.warning(
                "review_one_cluster [%s]: still truncated after %d shrink-retry "
                "attempt(s) (max_tasks=%d) — keeping %d salvaged candidate(s).",
                cluster.name, _shrink_attempts, max_tasks, len(candidates),
            )
        if candidates is not None and unsalvageable:
            # Plain retries exhausted (or disabled via
            # empty_response_retry_max=0) and the response is still
            # unsalvageable — nothing to keep this time; fail open with 0
            # candidates for this batch, same as the pre-AUTO-H5 behaviour,
            # but only after actually trying instead of giving up on the
            # first empty/garbage reply.
            logger.warning(
                "review_one_cluster [%s]: still unsalvageable after %d plain "
                "retry attempt(s) — giving up on this batch with 0 candidates.",
                cluster.name, _empty_attempts,
            )

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
            # AUTO-CR-11: a small model (e.g. llama3.1:8b) frequently runs out
            # of output tokens mid-array, truncating the final object's string
            # and making the WHOLE array unparseable. Rather than discarding
            # every candidate, salvage the complete top-level objects that DID
            # finish and drop only the truncated tail.
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
                # AUTO-H5: nothing salvageable at all — an empty response
                # (`text == ""`), a degenerate repetitive ramble that never
                # produces one complete `{...}` object, or garbage that
                # isn't JSON in the first place. Distinct from `truncated`
                # above precisely because there is no partial content to
                # build on; flagged so the caller can retry the SAME
                # request rather than shrinking a budget that isn't the
                # actual problem here.
                logger.warning(
                    "_parse_candidates [%s]: JSON decode failed: %s\nRaw text: %.400s",
                    cluster_name, exc, text,
                )
                return [], False, True

        if not isinstance(data, list):
            # Valid JSON, but not the array shape we asked for (e.g. a bare
            # object or string) — equally unsalvageable: there is no
            # per-item content to extract from a non-list top level.
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
            title       = (item.get("title") or "").strip()
            instruction = (item.get("instruction") or "").strip()
            acceptance  = (item.get("acceptance_check") or "").strip()

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

            # AUTO-CR-28: never let a task target a control/memory file
            # (story_bible.md, synopsis.md, IMPROVEMENTS.md, plan.json) —
            # editing these corrupts the bible/synopsis and sends the
            # redundancy gate into an endless rewrite loop. Bounce the
            # candidate here, before Gate 1, so no attempt is ever spent.
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

            # AUTO-CR-10: in creative mode the grounding is "the story so far,"
            # but a small model often omits cited_location or cites the new
            # (nonexistent) target chapter, blocking the whole run. Repair by
            # grounding on an existing cluster file (the latest chapter)
            # instead of rejecting.
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
                file       = (loc_raw.get("file") or "").strip(),
                symbol     = (loc_raw.get("symbol") or None),
                line_start = _to_int_or_none(loc_raw.get("line_start")),
                line_end   = _to_int_or_none(loc_raw.get("line_end")),
                new_file   = bool(loc_raw.get("new_file", False)),
            )

            if not cited.is_valid(self._task_mode):
                logger.warning(
                    "_parse_candidates [%s]: item %d cited_location lacks file "
                    "and/or anchor (symbol/line_start) — rejected. loc=%r",
                    cluster_name, i, loc_raw,
                )
                continue

            # AUTO-CR-3: creative_acceptance_default (config-driven) decides whether
            # an empty acceptance_check is permitted and defaults to "true".
            # Replaces the previous hard-coded task_mode == "creative" check (DM-2).
            if not acceptance:
                _acc_default = (
                    self._config.getboolean("auto", "creative_acceptance_default", fallback=True)
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

            # AUTO-CR-17: prose has no meaningful objective shell test, and a
            # small model often invents nonsensical checks (e.g. "diff
            # chapter_1.txt chapter_2.txt") that fail on every attempt, burning
            # the whole task. In creative mode force acceptance to the "true"
            # no-op — quality is judged by Gate-2 and the canon gate instead.
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

        # AUTO-CR-18: hard-cap creative tasks — a small model ignores the
        # "up to N" request and emits many overlapping "synchronize X" tasks,
        # which collapse every chapter into a copy of one when run
        # sequentially over shared files. Keep only the first N.
        if self._task_mode == "creative":
            cap = self._config.getint("architect", "max_tasks_creative", fallback=1)
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


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory
# ─────────────────────────────────────────────────────────────────────────────

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
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

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


# ─────────────────────────────────────────────────────────────────────────────
# TaskRewriter  (LOOP-2)
# ─────────────────────────────────────────────────────────────────────────────

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
        self._max_tokens  = int(config.get(arch, "rewrite_max_tokens",  fallback="512"))
        self._temperature = float(config.get(arch, "rewrite_temperature", fallback="0.4"))
        self._think       = config.getboolean(arch, "think", fallback=False)
        raw_system        = config.get(arch, "rewrite_system", fallback="").strip()
        self._system      = raw_system or _REWRITER_SYSTEM_DEFAULT
        self._timeout     = float(config.get("loop", "timeout_seconds", fallback="300"))
        active_profile    = config.get("api", "active", fallback="local")
        self._num_ctx     = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)

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
            content=user_msg,
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
            content=cleaned,
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

        new_title       = (data.get("title") or "").strip()
        new_instruction = (data.get("instruction") or "").strip()
        new_acceptance  = (data.get("acceptance_check") or "").strip()

        if not new_instruction:
            logger.warning(
                "TaskRewriter._parse_rewrite: rewrite produced empty instruction — "
                "keeping original task"
            )
            return original_task

        # Guard: reject acceptance_check values that look like prose rather than
        # a real shell command (a valid check starts with a known executable
        # token and is short). When the LLM drifts and writes a sentence
        # instead, bash can't find a binary for it and every attempt fails
        # with exit 127 — even on correct code — so falling back to the
        # original command is always safer.
        if new_acceptance and not _looks_like_shell_command(new_acceptance):
            logger.warning(
                "TaskRewriter._parse_rewrite: acceptance_check looks like prose, "
                "not a shell command — keeping original: %r -> %r",
                new_acceptance[:120], original_task.get("acceptance_check", ""),
            )
            new_acceptance = ""   # force fallback to original below

        # Guard: if the rewriter produced the same instruction and acceptance_check
        # as the original (semantic identity — same string content), return the
        # *original_task object* unchanged.  outer_loop.py checks `new_task is not
        # task` for object identity; returning a new dict with identical values would
        # pass that check, waste a rewrites_done slot, and leave the coder cycling
        # through the same strategy forever.
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


# ─────────────────────────────────────────────────────────────────────────────
# Internal utilities
# ─────────────────────────────────────────────────────────────────────────────

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
