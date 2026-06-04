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
max_tokens    — token cap for the presence-check call (default 256)
system        — override the built-in system prompt (optional)
skip_llm      — "true" to run existence checks only, skip LLM stage (testing)

agents.ini [api] / [api_local] / [api_remote] supply the same connection
keys used everywhere else in this codebase.
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
from tools.auto.architect import CandidateTask
from tools.block_extractor import extract_block
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think

logger = logging.getLogger(__name__)

# ── Gate 1 system prompt ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a static code reviewer performing a false-positive check. "
    "You will be shown a code excerpt and a description of a claimed problem. "
    "Your ONLY job is to verify whether the described problem is actually present "
    "in the code shown, and has NOT already been fixed. "
    "Do NOT suggest improvements. Do NOT run the code. "
    "Return ONLY a JSON object — no prose, no markdown fences, no preamble."
)

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

Is the claimed problem actually present in the code shown above, \
and NOT already fixed?

Return ONLY this JSON (no extra keys):
{{"verdict": "confirmed" | "rejected", "reason": "<one sentence explaining your decision>"}}
"""

# How many lines of context to include when only a line range is cited.
_MAX_CONTEXT_LINES = 60
# Maximum characters of a code block sent to the LLM.
_MAX_BLOCK_CHARS = 4000


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

class Gate1Filter:
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

        sec = "gate1"
        self._temperature = float(config.get(sec, "temperature", fallback="0.0"))
        self._max_tokens  = int(config.get(sec, "max_tokens",   fallback="256"))
        self._system      = config.get(sec, "system", fallback=_SYSTEM_PROMPT).strip()
        self._skip_llm    = config.getboolean(sec, "skip_llm", fallback=False)
        self._timeout     = float(config.get("loop", "timeout_seconds", fallback="300"))

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

        for c in candidates:
            ok, reason, block = self._check_existence(c, base_dir, cluster_files)
            if ok:
                existence_passed.append((c, block))
            else:
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="existence", reason=reason,
                ))
                logger.warning(
                    "Gate1[existence] REJECTED %r — %s", c.title, reason,
                )

        print(
            f"\n🔎 Gate 1 existence: "
            f"{len(existence_passed)}/{len(candidates)} candidate(s) passed"
        )

        # ── Stage B: LLM problem-presence check ───────────────────────────────
        presence_passed: list[tuple[CandidateTask, str]] = []  # (task, reason)

        if self._skip_llm:
            presence_passed = [(c, "existence check passed (LLM skipped)") for c, _ in existence_passed]
        else:
            for c, block in existence_passed:
                ok, reason = self._check_presence(c, block)
                if ok:
                    presence_passed.append((c, reason))
                else:
                    all_results.append(FilterResult(
                        candidate=c, accepted=False, stage="presence", reason=reason,
                    ))
                    logger.warning(
                        "Gate1[presence] REJECTED %r — %s", c.title, reason,
                    )

        print(
            f"🔎 Gate 1 presence: "
            f"{len(presence_passed)}/{len(existence_passed)} candidate(s) confirmed"
        )

        # ── Stage C: deduplication ────────────────────────────────────────────
        accepted: list[CandidateTask] = []
        seen_fingerprints: set[str] = set()

        for c, reason in presence_passed:
            fp = _fingerprint(c)
            if fp in seen_fingerprints:
                all_results.append(FilterResult(
                    candidate=c, accepted=False, stage="duplicate",
                    reason=f"duplicate of an earlier candidate with fingerprint {fp!r}",
                ))
                logger.info("Gate1[dedup] merged duplicate %r", c.title)
                continue
            seen_fingerprints.add(fp)
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

        # 2. Symbol anchor: must be locatable by block_extractor.
        if loc.symbol:
            block = extract_block(source, loc.symbol, file_ext)
            if not block:
                return (
                    False,
                    f"symbol {loc.symbol!r} not found in {loc.file!r}",
                    "",
                )
            return True, "symbol found", _truncate(block)

        # 3. Line range anchor: lines must exist in the file.
        lines = source.splitlines()
        total = len(lines)
        start = loc.line_start  # already validated non-None by CitedLocation.is_valid()
        end   = loc.line_end if loc.line_end is not None else start

        if start < 1 or start > total:
            return (
                False,
                f"line_start={start} is out of range (file has {total} lines)",
                "",
            )
        if end < start or end > total:
            # Clamp end rather than reject — a slightly-off end line is still useful.
            end = min(end, total)

        # Include some context around the cited range.
        ctx_start = max(0, start - 1)
        ctx_end   = min(total, end + 5)
        block = "\n".join(lines[ctx_start:ctx_end])
        return True, "line range found", _truncate(block)

    # ── Stage B helpers ───────────────────────────────────────────────────────

    def _check_presence(
        self,
        candidate: CandidateTask,
        code_block: str,
    ) -> tuple[bool, str]:
        """Call the LLM to confirm the claimed problem is present.

        Returns (confirmed: bool, reason: str).
        Fail-closed: any error → (False, reason).
        """
        loc = candidate.cited_location
        location_str = _location_str(loc)

        user_msg = _USER_PROMPT_TMPL.format(
            instruction=candidate.instruction,
            location=location_str,
            code_block=code_block,
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

        tracer.event(
            source="gate1",
            target="llm",
            kind="llm_request",
            content=user_msg,
            params={"model": self._model, "candidate": candidate.title},
        )

        try:
            raw_text = _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self._timeout,
                stream=True,
                api_format=self._api_format,
                ssl_context=self._ssl_context,
            )
        except Exception as exc:
            reason = f"LLM call failed: {exc}"
            logger.warning("Gate1._check_presence: %s — failing closed", reason)
            tracer.event(
                source="gate1", target="llm", kind="llm_response",
                content=f"[ERROR] {exc}", params={"candidate": candidate.title},
            )
            return False, reason

        cleaned = strip_think(raw_text)

        tracer.event(
            source="llm",
            target="gate1",
            kind="llm_response",
            content=cleaned,
            params={"candidate": candidate.title},
        )

        return self._parse_presence_response(cleaned, candidate.title)

    def _parse_presence_response(
        self,
        text: str,
        candidate_title: str,
    ) -> tuple[bool, str]:
        """Parse the LLM's JSON verdict.  Fail-closed on any error."""
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
            return False, reason

        if not isinstance(data, dict):
            reason = f"expected JSON object, got {type(data).__name__} — failing closed"
            logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, reason)
            return False, reason

        verdict = (data.get("verdict") or "").strip().lower()
        reason  = (data.get("reason")  or "").strip()

        if verdict == "confirmed":
            return True, reason or "LLM confirmed problem is present"
        if verdict == "rejected":
            return False, reason or "LLM found problem absent or already fixed"

        # Unrecognised verdict → fail closed.
        msg = f"unrecognised verdict {verdict!r} — failing closed"
        logger.warning("Gate1._parse_presence_response [%s]: %s", candidate_title, msg)
        return False, msg


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory (mirrors architect.review_clusters pattern)
# ─────────────────────────────────────────────────────────────────────────────

def filter_candidates(
    candidates: list[CandidateTask],
    base_dir: str | Path,
    config: configparser.ConfigParser,
    cluster_files: "dict[str, set[str]] | None" = None,
) -> tuple[list[CandidateTask], list[FilterResult]]:
    """One-call entry point for ``AutoController``.

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

    Returns
    -------
    accepted : list[CandidateTask]
        Candidates that passed Gate 1.
    rejected : list[FilterResult]
        Rejected candidates with stage and reason.
    """
    active    = config.get("api", "active", fallback="local")
    section   = f"api_{active}"
    base_url  = config.get(section, "base_url")
    api_key   = config.get(section, "api_key",    fallback="")
    model     = config.get(section, "model")
    api_fmt   = config.get(section, "api_format", fallback="openai")
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

    filt = Gate1Filter(
        config=config,
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=api_fmt,
        verify_ssl=verify_ssl,
    )
    return filt.filter(candidates, base_dir, cluster_files=cluster_files)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fingerprint(c: CandidateTask) -> str:
    """Stable deduplication key for a candidate.

    Two candidates are considered duplicates when they cite the same location
    and have the same normalised title (lowercased, stripped).
    """
    loc = c.cited_location
    anchor = loc.symbol or f"L{loc.line_start}-{loc.line_end}"
    return f"{loc.file}::{anchor}::{c.title.strip().lower()}"


def _location_str(loc: Any) -> str:
    """Human-readable location string for the LLM prompt."""
    parts = [loc.file]
    if loc.symbol:
        parts.append(f"symbol={loc.symbol!r}")
    if loc.line_start is not None:
        parts.append(f"lines {loc.line_start}–{loc.line_end or loc.line_start}")
    return ", ".join(parts)


def _truncate(text: str, max_chars: int = _MAX_BLOCK_CHARS) -> str:
    """Truncate *text* to *max_chars*, appending a notice when clipped."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated — {len(text) - max_chars} more chars]"