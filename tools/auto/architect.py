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
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.agent_trace import tracer
from tools.auto.repo_ingest import RepoCluster
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think

logger = logging.getLogger(__name__)

# ── Architect system prompt ───────────────────────────────────────────────────
# Injected as the system role.  Can be overridden via agents.ini [architect] system.

_SYSTEM_PROMPT = (
    "You are a senior software architect performing a targeted code review. "
    "Your job is to identify concrete, actionable improvements in the files "
    "provided. Each improvement you suggest MUST be grounded in a real location "
    "in the code — you MUST cite the exact file path and either a symbol name "
    "(function, class, method) or a line range. "
    "Do NOT invent problems that are not actually present in the provided code. "
    "Return ONLY a JSON array — no prose, no markdown fences, no preamble."
)

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

Identify up to 5 concrete improvement tasks for files in this cluster that \
are relevant to the goal above.  Look in particular for: missing or weak tests, \
missing error handling, unvalidated inputs, missing timeouts on network calls, \
duplicated logic, and unclear naming.

STRICT RULES:
1. Every task MUST cite the exact file path and a symbol name OR line range \
   where the issue lives.  Tasks without a cited_location are invalid.
2. The "file" field in cited_location and every entry in target_files MUST \
   be copied EXACTLY from the list below — character for character. \
   Do NOT invent new paths, add directory prefixes, or modify the paths in any way. \
   To add new tests, target an EXISTING test file from the list and add test \
   functions to it.
3. Only report problems that are actually present in the code shown above.
4. Keep each task small enough to be implemented and tested independently.
5. Returning an empty array [] is allowed ONLY if the code is genuinely clean; \
   most real source files have at least one concrete improvement, so look \
   carefully before returning [].

Each element of the JSON array must match this schema exactly (no extra keys):

[
  {{
    "title": "<short imperative phrase>",
    "instruction": "<detailed instruction for the coder agent>",
    "target_files": ["<exact path from the list below>"],
    "acceptance_check": "<shell command that exits 0 when the task is done>",
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

Now review the code above against the goal "{goal}" and output ONLY the JSON \
array of up to 5 improvement tasks (no prose, no markdown fences):
"""

# Maximum characters of a single file's content to include in the prompt.
# Batching keeps the file COUNT per call small, so we can afford fuller content
# per file — too-aggressive truncation hides the code and the model returns [].
_MAX_FILE_CHARS = 4000

# Maximum files reviewed in a single LLM call.  Larger clusters are split into
# batches so each prompt stays small enough for the model to follow the
# verbatim-path instruction while still seeing enough code to find issues.
_MAX_FILES_PER_REVIEW = 6


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

    def is_valid(self) -> bool:
        """A location is valid when it has a file AND at least one anchor."""
        return bool(self.file) and (
            bool(self.symbol) or self.line_start is not None
        )


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
        self._temperature = float(config.get(arch, "temperature", fallback="0.2"))
        self._max_tokens  = int(config.get(arch, "max_tokens",  fallback="2048"))
        self._system      = config.get(arch, "system", fallback=_SYSTEM_PROMPT).strip()
        self._timeout     = float(config.get("loop", "timeout_seconds", fallback="300"))

    # ── Public API ────────────────────────────────────────────────────────────

    def review_clusters(
        self,
        clusters: list[RepoCluster],
        base_dir: str | Path,
        goal: str = "improve current code",
        *,
        on_cluster_done=None,
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

        Returns
        -------
        list[CandidateTask]
            All candidates whose ``cited_location.is_valid()`` returned True,
            in cluster order.  Ungrounded candidates are logged and dropped.
        """
        base_dir = Path(base_dir)
        all_candidates: list[CandidateTask] = []

        for cluster in clusters:
            if not cluster.files:
                logger.debug("review_clusters: skipping empty cluster %r", cluster.name)
                _fire_callback(on_cluster_done)
                continue

            # Split large clusters into batches so each LLM prompt stays within
            # a local model's context window (overflow => hallucinated paths).
            files = cluster.files
            batches = [
                files[i:i + _MAX_FILES_PER_REVIEW]
                for i in range(0, len(files), _MAX_FILES_PER_REVIEW)
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
                cluster_candidates.extend(
                    self._review_one_cluster(sub, base_dir, goal)
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
    ) -> list[CandidateTask]:
        """Send one LLM call for *cluster* and return grounded candidates.

        Fail-closed: any parse/network error returns an empty list so the run
        continues with the remaining clusters.
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
            payload: dict[str, Any] = {
                "model":       self._model,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
                "options": {
                    "temperature": self._temperature,
                    "num_predict": self._max_tokens
                }
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

        try:
            import sys
            tokens_list = []
            def streaming_callback(token: str):
               sys.stdout.write(token)
               sys.stdout.flush()
               tokens_list.append(token)

            print(f"\n🧠 [LIVE ARCHITECT STREAMING THINKING & RESPONSE]:")
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
            print(f"\n" + "═" * 80 + "\n")
        except Exception as exc:
            logger.warning(
                "review_one_cluster: LLM call failed for cluster %r: %s",
                cluster.name, exc,
            )
            tracer.event(
                source="architect", target="llm", kind="llm_response",
                content=f"[ERROR] {exc}", params={"cluster": cluster.name},
            )
            return []
        print(f"\n🧠 [LIVE ARCHITECT THINKING CHAIN & RESPONSE]:")
        print(raw_text)
        print(f"═" * 80 + "\n")

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

            if not title or not instruction or not acceptance:
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

            if not cited.is_valid():
                logger.warning(
                    "_parse_candidates [%s]: item %d cited_location lacks file "
                    "and/or anchor (symbol/line_start) — rejected. loc=%r",
                    cluster_name, i, loc_raw,
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

    @staticmethod
    def _build_file_contents(files: list[str], base_dir: Path) -> str:
        """Read and annotate file contents for the prompt.

        Each file is prefixed with a ``### path/to/file.py`` header.
        Content is truncated to ``_MAX_FILE_CHARS`` with a notice so the model
        knows the file continues beyond the excerpt.
        """
        parts: list[str] = []
        for rel_path in files:
            abs_path = base_dir / rel_path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("_build_file_contents: cannot read %s: %s", rel_path, exc)
                content = f"[unreadable: {exc}]"

            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + f"\n... [truncated — {len(content) - _MAX_FILE_CHARS} more chars]"

            parts.append(f"### {rel_path}\n{content}")

        return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory
# ─────────────────────────────────────────────────────────────────────────────

def review_clusters(
    clusters: list[RepoCluster],
    base_dir: str | Path,
    config: configparser.ConfigParser,
    goal: str = "improve current code",
    *,
    on_cluster_done=None,
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
    )
    return reviewer.review_clusters(clusters, base_dir, goal, on_cluster_done=on_cluster_done)


# ─────────────────────────────────────────────────────────────────────────────
# Internal utilities
# ─────────────────────────────────────────────────────────────────────────────

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