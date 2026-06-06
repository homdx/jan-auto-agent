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
<think> blocks are silently discarded before JSON parsing.  The call is
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
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.agent_trace import tracer
from tools.auto.repo_ingest import RepoCluster
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
    "You are a creative writing editor reviewing drafts for improvement. "
    "Your job is to identify concrete, actionable improvements in the creative "
    "writing files provided. Each improvement MUST be grounded in a real location "
    "— cite the exact file path and a line range where the issue lives. "
    "cited_location.symbol must be null; cite line range only. "
    "acceptance_check should be 'true' or a word-count sanity check. "
    "Do NOT invent problems that are not actually present in the provided text. "
    "Return ONLY a JSON array — no prose, no markdown fences, no preamble."
)

# Backward-compat alias — existing code that references _SYSTEM_PROMPT still works.
_SYSTEM_PROMPT = _SYSTEM_PROMPT_CODE

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

Produce up to 5 concrete tasks for files in this cluster that ACHIEVE THE GOAL \
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
   does not yet have — that is expected when the goal asks for new features.
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
      "file": "<exact path from the list below>",
      "symbol": "<function or class name, or null>",
      "line_start": <integer or null>,
      "line_end":   <integer or null>
    }}
  }}
]

REMINDER — the ONLY file paths you may put in "target_files" or \
"cited_location.file" are these, copied character-for-character. \
Any other path will be rejected:
{file_listing}

Now produce ONLY the JSON array of up to 5 concrete tasks that IMPLEMENT the \
goal "{goal}" against the files above (no prose, no markdown fences):
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

    def is_valid(self, task_mode: str = "code") -> bool:
        """A location is valid when it has a file AND at least one anchor.

        For docs/creative modes, a file alone is sufficient grounding —
        no symbol or line range is required.
        """
        if not self.file:
            return False
        if task_mode == "code":
            return bool(self.symbol) or self.line_start is not None
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

class ClusterReviewer:
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
        self._config     = config
        self._base_url   = base_url.rstrip("/")
        self._api_key    = api_key
        self._model      = model
        self._api_format = api_format
        self._task_mode  = task_mode

        import ssl
        self._ssl_context = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

        arch = "architect"
        self._temperature            = float(config.get(arch, "temperature",         fallback="0.2"))
        self._max_tokens             = int(config.get(arch,   "max_tokens",          fallback="2048"))
        self._timeout                = float(config.get("loop", "timeout_seconds",   fallback="300"))
        self._max_file_chars         = int(config.get(arch,   "max_file_chars",      fallback=str(_DEFAULT_MAX_FILE_CHARS)))
        self._max_files_per_review   = int(config.get(arch,   "max_files_per_review", fallback=str(_DEFAULT_MAX_FILES_PER_REVIEW)))
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        active_profile               = config.get("api", "active", fallback="local")
        self._num_ctx                = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        # ── TaskRewriter config (LOOP-5) ──────────────────────────────────────
        self._rewrite_max_tokens     = int(config.get(arch,   "rewrite_max_tokens",  fallback="512"))
        self._rewrite_temperature    = float(config.get(arch, "rewrite_temperature", fallback="0.4"))
        self._rewrite_system         = config.get(arch, "rewrite_system", fallback="").strip()

        # ── DM-2: select system prompt based on task_mode + ini overrides ─────
        # Priority: mode-specific ini key > legacy "system" key > built-in constant.
        mode_ini_key = f"system_{task_mode}" if task_mode != "code" else None
        if mode_ini_key and config.has_option(arch, mode_ini_key):
            self._system = config.get(arch, mode_ini_key).strip()
        else:
            built_in = _SYSTEM_PROMPTS.get(task_mode, _SYSTEM_PROMPT_CODE)
            self._system = config.get(arch, "system", fallback=built_in).strip()

    # ── Public API ────────────────────────────────────────────────────────────

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
                batch_results = self._review_one_cluster(sub, base_dir, goal)
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
                        checkpoint_path.write_text(
                            json.dumps(checkpoint, indent=2, ensure_ascii=False),
                            encoding="utf-8",
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

            print(f"   → {len(cluster_candidates)} grounded candidate(s)")
            all_candidates.extend(cluster_candidates)
            _fire_callback(on_cluster_done)

        print(f"\n✅ Architect done — {len(all_candidates)} total candidate(s) across all clusters\n")
        return all_candidates

    # ── Private helpers ───────────────────────────────────────────────────────

    def _review_one_cluster(
        self,
        cluster: RepoCluster,
        base_dir: Path,
        goal: str,
    ) -> "list[CandidateTask] | None":
        """Send one LLM call for *cluster* and return grounded candidates.

        Returns
        -------
        list[CandidateTask]
            Grounded candidates.  May be empty if the LLM produced nothing valid.
        None
            The LLM call failed after all retries (transient server error).
            The caller must NOT checkpoint this result.
        """
        file_listing = "\n".join(f"  - {f}" for f in cluster.files)
        file_contents = self._build_file_contents(cluster.files, base_dir)

        user_msg = _USER_PROMPT_TMPL.format(
            goal=goal,
            cluster=cluster.name,
            file_listing=file_listing,
            file_contents=file_contents,
        )


        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        if self._api_format == "ollama":
            url = _llm_stream.ollama_chat_url(self._base_url)
            _ollama_opts: dict[str, Any] = {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            }
            if self._num_ctx:
                _ollama_opts["num_ctx"] = self._num_ctx
            payload: dict[str, Any] = {
                "model":       self._model,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
                "options": _ollama_opts,
            }
        else:
            url = f"{self._base_url}/chat/completions"
            payload: dict[str, Any] = {
                "model":       self._model,
                "temperature": self._temperature,
                "max_tokens":  self._max_tokens,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
            }

        # Trace the outgoing call.
        tracer.event(
            source="architect",
            target="llm",
            kind="llm_request",
            content=user_msg,
            params={"model": self._model, "temperature": self._temperature,
                    "cluster": cluster.name},
        )

        import sys
        import time

        _RETRY_DELAYS = [5, 15, 30]   # seconds between attempts (3 retries)

        raw_text = ""
        last_exc: Exception | None = None

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
                # failures.  Client errors (4xx) are deterministic — no point retrying.
                _is_transient = (
                    "500" in exc_str
                    or "502" in exc_str
                    or "503" in exc_str
                    or "504" in exc_str
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
                source="architect", target="llm", kind="llm_response",
                content=f"[ERROR] {last_exc}", params={"cluster": cluster.name},
            )
            # Return None (not []) so callers can distinguish "call failed" from
            # "call succeeded but LLM produced no valid candidates".  The checkpoint
            # must NOT record a failed batch; returning None signals that.
            return None

        # Strip reasoning tokens before JSON parsing.
        cleaned = strip_think(raw_text)

        tracer.event(
            source="llm",
            target="architect",
            kind="llm_response",
            content=cleaned,
            params={"cluster": cluster.name},
        )

        return self._parse_candidates(cleaned, cluster.name)

    def _parse_candidates(self, text: str, cluster_name: str) -> list[CandidateTask]:
        """Parse the LLM's JSON array into :class:`CandidateTask` objects.

        Ungrounded candidates (missing file, missing symbol AND line_start) are
        logged and dropped.  Any parse error is logged and returns [].

        Fail-closed: never returns a partially-constructed candidate on error.
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

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning(
                "_parse_candidates [%s]: JSON decode failed: %s\nRaw text: %.400s",
                cluster_name, exc, text,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "_parse_candidates [%s]: expected JSON array, got %s",
                cluster_name, type(data).__name__,
            )
            return []

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

            # --- Validate and build cited_location (grounding gate) ---
            loc_raw = item.get("cited_location")
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
            )

            if not cited.is_valid(self._task_mode):
                logger.warning(
                    "_parse_candidates [%s]: item %d cited_location lacks file "
                    "and/or anchor (symbol/line_start) — rejected. loc=%r",
                    cluster_name, i, loc_raw,
                )
                continue

            # DM-2: in creative mode, empty acceptance_check is allowed (default to 'true').
            if not acceptance:
                if self._task_mode == "creative":
                    acceptance = "true"
                else:
                    logger.warning(
                        "_parse_candidates [%s]: item %d missing acceptance_check — rejected",
                        cluster_name, i,
                    )
                    continue

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

        return candidates

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

def _batch_fingerprint(base_dir: "Path", files: list[str]) -> str:
    """Short hash of a batch's files (relative path + size + mtime) used to make
    the architect checkpoint content-aware. Any change to the reviewed file set
    or contents yields a new key, so a stale cached result is never replayed.
    Missing/unreadable files hash as a sentinel rather than raising."""
    h = hashlib.sha1()
    for rel in sorted(files):
        h.update(rel.encode("utf-8", "replace"))
        try:
            st = (Path(base_dir) / rel).stat()
            h.update(f"|{st.st_size}|{st.st_mtime_ns}|".encode("utf-8"))
        except OSError:
            h.update(b"|?|")
    return h.hexdigest()[:12]


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
    "Your job is to propose a genuinely different technical approach. "
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
  "acceptance_check": "<MUST be a real runnable shell command that exits 0 when done — keep the original command unchanged unless the new strategy genuinely requires a different invocation. Do NOT replace with a prose description.>"
}}
"""


class TaskRewriter:
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
        self._config     = config
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

        arch = "architect"
        self._max_tokens  = int(config.get(arch, "rewrite_max_tokens",  fallback="512"))
        self._temperature = float(config.get(arch, "rewrite_temperature", fallback="0.4"))
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

        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        if self._api_format == "ollama":
            url = _llm_stream.ollama_chat_url(self._base_url)
            _rewriter_opts: dict[str, Any] = {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            }
            if self._num_ctx:
                _rewriter_opts["num_ctx"] = self._num_ctx
            payload: dict[str, Any] = {
                "model":   self._model,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
                "options": _rewriter_opts,
            }
        else:
            url = f"{self._base_url}/chat/completions"
            payload: dict[str, Any] = {
                "model":       self._model,
                "temperature": self._temperature,
                "max_tokens":  self._max_tokens,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
            }

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
        # a real shell command.  A valid check starts with a known executable token
        # and is short enough to be a single command.  When the LLM drifts and
        # writes a sentence (e.g. "The script must run without errors …"), the
        # executor runs it as a shell command, bash cannot find "The" as a binary,
        # and every subsequent attempt fails with exit 127 — even when the generated
        # code is correct.  Falling back to the original command is always safer.
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
    "./", "/",
)

# Heuristic upper bound: real commands are short; prose descriptions are long.
_MAX_ACCEPTANCE_CHECK_CHARS = 300

import re as _re
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
    import re as _re2
    return _re2.sub(r"\s+", " ", text.strip())


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