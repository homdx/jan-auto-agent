"""tools/auto/coder.py — AUTO-C2: Code-change generator for the autonomous loop.

Given a validated task (instruction + cited location + target files), calls the
LLM once and returns the full revised content for every target file.  The caller
(AUTO-C3 inner attempt loop) applies the files to disk, then hands them to the
Executor (AUTO-C1) for the acceptance check.

Public surface consumed by the attempt loop (AUTO-C3)::

    from tools.auto.coder import Coder, CoderResult, make_coder

    coder = make_coder(config)
    result: CoderResult = coder.generate(
        task           = task_dict,            # plan.json task schema
        base_dir       = Path("."),
        prior_feedback = ["round 1 failure …"] # optional, from feedback_round_N.md
    )

    result.succeeded       # True iff ≥1 file was written without error
    result.files_written   # list[str] of relative paths written to disk
    result.error           # non-empty string on failure

Output contract
---------------
The LLM is asked to return a JSON object:

    {
      "files": [
        {"path": "relative/path.py", "content": "... complete file ..."},
        ...
      ]
    }

Before JSON parsing:
  * ``strip_think`` removes any ``<think>…</think>`` reasoning blocks.
  * Outer markdown code fences (``` or ```json) are stripped.

After parsing:
  * Each ``content`` string is passed through :func:`_strip_code_fence` so a
    model that wraps file content in triple-backticks still produces clean output.

Fail-closed behaviour
---------------------
Any LLM / network error, JSON parse failure, or missing required key returns a
:class:`CoderResult` with ``error`` set and ``files_written`` empty.  The
attempt loop (AUTO-C3) treats this as a failed attempt and may feed it back as
feedback for the next round.

Configuration (agents.ini [coder])
------------------------------------
temperature   — sampling temperature (default 0.2)
max_tokens    — token budget (default 4096)
system        — override the built-in system prompt (optional)

agents.ini [api] / [api_local] / [api_remote] supply base_url, api_key, model,
api_format, verify_ssl — the same pattern used throughout this codebase.
"""

from __future__ import annotations

import ast
import configparser
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.block_extractor import extract_block as _block_extractor_extract_block
# SearchAgent is imported lazily inside Coder._fetch_needed

from tools.agent_trace import tracer
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think

logger = logging.getLogger(__name__)

# _MAX_FILE_CHARS is now read from [coder] max_file_chars in agents.ini.
# This default is used only when the key is absent.
_DEFAULT_MAX_FILE_CHARS = 8_000

# ── Coder system prompt ───────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are a senior software engineer implementing a targeted code improvement. "
    "You will be given a task instruction, the current content of one or more "
    "target files, a cited location describing where the issue lives, and "
    "optionally feedback from previous failed attempts. "
    "Your job is to produce the COMPLETE revised content for every file that "
    "needs to change. "
    "Return ONLY a JSON object — no prose, no preamble, no markdown fences "
    "outside the JSON value itself. "
    "JSON schema (follow exactly):\n"
    "{\n"
    '  "files": [\n'
    '    {"path": "relative/path/to/file.py", "content": "<full revised file>"},\n'
    "    ...\n"
    "  ]\n"
    "}\n"
    "Rules:\n"
    "1. Output the COMPLETE file for every changed file — not a diff, not a snippet.\n"
    "2. Only include files that require changes; omit files left unchanged.\n"
    "3. Paths must be relative (matching the target_files list).\n"
    "4. Do NOT wrap the file content inside inner code fences.\n"
    "5. Do NOT add any explanation or commentary outside the JSON.\n"
    "6. The 'files' array MUST NEVER be empty. The task always requires at least one "
    "file change — if you think nothing needs changing, re-read the instruction and "
    "produce the correct implementation. An empty files array is always wrong.\n"
    "7. If a symbol you must use (a class, function, or constant) is referenced but its "
    "definition is NOT shown to you, do not guess — add a top-level \"context_request\" "
    "array naming the exact symbols you need, e.g. "
    '{"files": [...], "context_request": ["Config", "_resolve_path"]}. '
    "The names you request are resolved and provided on the next attempt. Omit "
    "context_request (or use []) when you already have everything you need."
    "\n"
    "8. If the provided file contents are insufficient to complete the task — "
    "for example, a referenced class or function is not shown — add a top-level "
    "\"missing_context\" key to your JSON response listing the symbol names you "
    "needed but did not receive. "
    "Format: \"missing_context\": [\"ClassName\", \"function_name\"] "
    "Omit the key entirely if context was sufficient. "
    "Do not include it as an empty list."
)

# Backward-compat alias — "code" is the default persona.
_SYSTEM_PROMPT_CODE = _SYSTEM_PROMPT

# Docs/creative reuse the IDENTICAL JSON contract + rules above; only the writing
# persona and the "code improvement" framing change. This mirrors the architect
# (_SYSTEM_PROMPTS) and Gate-2 validator (_GATE2_SYSTEMS), which already switch
# persona by task_mode — the coder was previously the only stage that did not.
_SYSTEM_PROMPT_DOCS = _SYSTEM_PROMPT_CODE.replace(
    "You are a senior software engineer implementing a targeted code improvement. ",
    "You are a senior technical writer implementing a targeted documentation "
    "improvement. You produce clear, accurate prose — not code. ",
    1,
)
_SYSTEM_PROMPT_CREATIVE = _SYSTEM_PROMPT_CODE.replace(
    "You are a senior software engineer implementing a targeted code improvement. ",
    "You are a creative writing editor implementing a targeted revision to a piece "
    "of creative writing. You produce polished, engaging prose — not code. ",
    1,
)

_CODER_SYSTEM_PROMPTS: dict[str, str] = {
    "code":     _SYSTEM_PROMPT_CODE,
    "docs":     _SYSTEM_PROMPT_DOCS,
    "creative": _SYSTEM_PROMPT_CREATIVE,
}

# ── Per-task user prompt template ─────────────────────────────────────────────
_USER_PROMPT_TMPL = """\
TASK ID:    {task_id}
TITLE:      {title}
INSTRUCTION:
{instruction}

CITED LOCATION:
  file   : {cited_file}
  symbol : {cited_symbol}
  lines  : {cited_lines}

TARGET FILES TO MODIFY:
{target_files_listing}

CURRENT FILE CONTENTS:
{file_contents}
{feedback_section}
Produce the corrected files now. Return ONLY the JSON object described in the \
system prompt — nothing else.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CoderResult:
    """Structured result of one Coder.generate() call.

    Attributes
    ----------
    task_id:
        The ``id`` field of the task that was processed.
    files_written:
        Relative paths of files that were successfully written to *base_dir*.
        Empty when the generation failed before writing.
    error:
        Human-readable error description; empty string on success.
    raw_response:
        The raw (post-strip_think) LLM text; kept for logging / feedback.
    """

    task_id:       str = ""
    files_written: list[str] = field(default_factory=list)
    error:         str = ""
    raw_response:  str = field(default="", repr=False)
    missing_context: list[str] = field(default_factory=list)
    context_satisfied: bool = True  # False when the LLM reported missing_context

    @property
    def succeeded(self) -> bool:
        """``True`` iff at least one file was written and no error was recorded."""
        return bool(self.files_written) and not self.error

    def summary(self) -> str:
        """One-line status string for logging."""
        if self.succeeded:
            return f"[{self.task_id}] CODER OK — wrote {self.files_written}"
        return f"[{self.task_id}] CODER FAIL — {self.error or 'no files written'}"


# ─────────────────────────────────────────────────────────────────────────────
# Coder
# ─────────────────────────────────────────────────────────────────────────────

class Coder:
    """Generates full revised file content for a single autonomous task.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    base_url:
        LLM API endpoint (e.g. ``http://localhost:1337/v1``).
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

        import ssl
        self._ssl_context = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

        self._task_mode = task_mode
        sec = "coder"
        self._temperature = float(config.get(sec, "temperature", fallback="0.2"))
        self._max_tokens  = int(config.get(sec, "max_tokens",   fallback="16384"))
        # Select system prompt by task_mode (mirrors architect / validator).
        # Priority: mode-specific ini key > legacy "system" key > built-in constant.
        _mode_key = f"system_{self._task_mode}" if self._task_mode != "code" else None
        if _mode_key and config.has_option(sec, _mode_key):
            self._system  = config.get(sec, _mode_key).strip()
        else:
            _builtin      = _CODER_SYSTEM_PROMPTS.get(self._task_mode, _SYSTEM_PROMPT_CODE)
            self._system  = config.get(sec, "system", fallback=_builtin).strip()
        self._timeout     = float(config.get("loop", "timeout_seconds", fallback="300"))
        self._max_file_chars = int(config.get(sec, "max_file_chars", fallback=str(_DEFAULT_MAX_FILE_CHARS)))
        active_profile = config.get("api", "active", fallback="local")
        # num_ctx controls the total context window on Ollama; 0 means "use server default".
        self._num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        # Context-probe: fetch missing symbols on first LLM response, then retry once.
        self._context_probe_enabled = config.getboolean(sec, "context_probe", fallback=True)
        self._max_chars_per_dep     = config.getint(sec, "max_chars_per_dep", fallback=2000)
        self._max_dep_chars         = config.getint(sec, "max_dep_chars",     fallback=6000)

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        task: dict,
        base_dir: str | Path,
        prior_feedback: Optional[list[str]] = None,
        prefetched_context: str = "",
    ) -> CoderResult:
        """Generate and write code changes for *task*.

        Reads the current content of every ``target_files`` entry from *base_dir*,
        builds a grounded prompt (instruction + cited location + file contents +
        any prior feedback), calls the LLM, parses the JSON response, and writes
        each changed file back to *base_dir*.

        A backup of each original file is written alongside it as ``<file>.coder.bak``
        so changes are reversible without git.

        Parameters
        ----------
        task:
            A plan.json task dict (fields: ``id``, ``title``, ``instruction``,
            ``target_files``, ``cited_locations``).
        base_dir:
            Root of the repository.  All paths in *task* are relative to this.
        prior_feedback:
            Optional list of feedback strings from previous rounds (each
            typically the content of a ``feedback_round_N.md`` file).

        Returns
        -------
        CoderResult
            Always returns — never raises on LLM or write failures.
        """
        task_id = (task.get("id") or "").strip()
        base_dir = Path(base_dir).resolve()

        # ── Build and send the prompt ─────────────────────────────────────────
        user_msg = self._build_prompt(task, base_dir, prior_feedback or [], prefetched_context)

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
            payload = {
                "model":       self._model,
                "temperature": self._temperature,
                "max_tokens":  self._max_tokens,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user",   "content": user_msg},
                ],
            }

        tracer.event(
            source="coder", target="llm", kind="llm_request",
            content=user_msg,
            params={"model": self._model, "temperature": self._temperature,
                    "task_id": task_id},
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
            msg = f"LLM call failed: {exc}"
            logger.warning("coder.generate [%s]: %s", task_id, msg)
            tracer.event(
                source="coder", target="llm", kind="llm_response",
                content=f"[ERROR] {exc}", params={"task_id": task_id},
            )
            return CoderResult(task_id=task_id, error=msg)

        # ── Strip think blocks; check for missing-context signal ─────────────
        cleaned = strip_think(raw_text)
        # Pull-model: capture any symbols the coder asked for (context_request).
        missing_ctx = self._extract_context_request(cleaned)

        # ── Context probe: if the LLM reported missing_context, fetch once ───
        _missing = self._extract_missing_context(cleaned)
        context_satisfied = not bool(_missing)

        if _missing and self._context_probe_enabled:
            dep_ctx = self._fetch_needed(
                _missing, base_dir,
                max_chars_per_dep=self._max_chars_per_dep,
                max_total_dep_chars=self._max_dep_chars,
            )
            if dep_ctx:
                user_msg += f"\n\n## Fetched context (requested)\n{dep_ctx}"
                # Mutate payload in-place — messages[-1] is always the user role.
                payload["messages"][-1]["content"] = user_msg
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
                    cleaned = strip_think(raw_text)
                    # Re-extract context_request symbols from the new response
                    # so the CoderResult reflects any new symbols the second
                    # call asked for.  context_satisfied intentionally keeps
                    # the first response's value: False signals "probe was
                    # needed" and is used by outer_loop to skip TaskRewriter.
                    missing_ctx = self._extract_context_request(cleaned)
                except Exception as exc:
                    logger.warning(
                        "coder.generate [%s]: context-probe second call failed: %s "
                        "— using first response", task_id, exc,
                    )
                    # cleaned already holds the first response; carry on.

        tracer.event(
            source="llm", target="coder", kind="llm_response",
            content=cleaned, params={"task_id": task_id},
        )

        parsed_files, parse_error = self._parse_response(cleaned, task_id)
        if parse_error:
            return CoderResult(
                task_id=task_id, error=parse_error,
                raw_response=cleaned, missing_context=missing_ctx,
                context_satisfied=context_satisfied,
            )

        # ── Write files to disk ───────────────────────────────────────────────
        target_files = task.get("target_files") or []
        allowed = frozenset(target_files) if target_files else None
        written, write_error = self._write_files(
            parsed_files, base_dir, task_id, allowed_paths=allowed
        )
        if write_error and not written:
            return CoderResult(
                task_id=task_id, error=write_error, raw_response=cleaned,
                missing_context=missing_ctx, context_satisfied=context_satisfied,
            )

        result = CoderResult(
            task_id=task_id,
            files_written=written,
            error=write_error,
            raw_response=cleaned,
            missing_context=missing_ctx,
            context_satisfied=context_satisfied,
        )
        logger.info("coder.generate: %s", result.summary())
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        task: dict,
        base_dir: Path,
        prior_feedback: list[str],
        prefetched_context: str = "",
    ) -> str:
        """Construct the user-role prompt for this task."""
        task_id      = task.get("id", "")
        title        = task.get("title", "")
        instruction  = task.get("instruction", "")
        target_files = task.get("target_files") or []

        # ── Cited location ────────────────────────────────────────────────────
        cited_locations = task.get("cited_locations") or []
        if cited_locations and isinstance(cited_locations[0], dict):
            loc = cited_locations[0]
        else:
            # Also accept the flat ``cited_location`` shape from CandidateTask.
            loc_raw = task.get("cited_location")
            loc = loc_raw if isinstance(loc_raw, dict) else {}

        cited_file   = loc.get("file", "")
        cited_symbol = loc.get("symbol") or "—"
        ls = loc.get("line_start")
        le = loc.get("line_end")
        if ls is not None:
            cited_lines = f"{ls}–{le}" if le is not None else str(ls)
        else:
            cited_lines = "—"

        # ── File listing ──────────────────────────────────────────────────────
        target_files_listing = "\n".join(f"  - {f}" for f in target_files) or "  (none)"

        # ── File contents ─────────────────────────────────────────────────────
        # Pass task so _read_file_contents can resolve cited_symbol itself.
        file_contents = self._read_file_contents(
            target_files, base_dir, task=task
        )
        # Pull-model: prepend any context the previous attempt requested.
        if prefetched_context:
            file_contents = prefetched_context.rstrip() + "\n\n" + file_contents

        # ── Prior feedback section ────────────────────────────────────────────
        if prior_feedback:
            lines = ["PRIOR ROUND FEEDBACK (read carefully before writing code):"]
            for i, fb in enumerate(prior_feedback, 1):
                lines.append(f"\n--- Feedback from round {i} ---\n{fb.strip()}")
            feedback_section = "\n".join(lines) + "\n\n"
        else:
            feedback_section = ""

        return _USER_PROMPT_TMPL.format(
            task_id               = task_id,
            title                 = title,
            instruction           = instruction,
            cited_file            = cited_file or "—",
            cited_symbol          = cited_symbol,
            cited_lines           = cited_lines,
            target_files_listing  = target_files_listing,
            file_contents         = file_contents,
            feedback_section      = feedback_section,
        )

    @staticmethod
    def _extract_context_request(text: str) -> list[str]:
        """Extract a top-level 'context_request' list of symbol names from the LLM
        JSON response. Returns [] on absence or any parse error (fail-safe)."""
        try:
            data = json.loads(_strip_outer_fence(text))
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        req = data.get("context_request")
        if not isinstance(req, list):
            return []
        return [str(s).strip() for s in req if str(s).strip()]

    def _extract_missing_context(self, text: str) -> list[str]:
        """Extract the top-level ``missing_context`` list from the LLM response.

        Returns up to 8 symbol names; returns ``[]`` on absence or any parse
        error so callers can treat it as fail-safe.
        """
        try:
            data = json.loads(_strip_outer_fence(text))
            missing = data.get("missing_context")
            if isinstance(missing, list):
                return [str(s).strip() for s in missing if s][:8]
        except Exception:
            pass
        return []

    def _fetch_needed(
        self,
        symbols: list[str],
        base_dir: Path,
        max_chars_per_dep: int,
        max_total_dep_chars: int,
    ) -> str:
        """Search *base_dir* for the source blocks of *symbols* and return them
        formatted as prompt context.

        Each found block is trimmed to *max_chars_per_dep* and prefixed with::

            ### dep: SymbolName  (from path/to/file.py)

        Accumulation stops once the total reaches *max_total_dep_chars*.
        Symbols that cannot be found are skipped with a DEBUG log entry.
        Returns ``""`` when nothing was found.  Never raises.
        """
        try:
            from tools.search_agent import SearchAgent as _SearchAgent  # lazy import
            active  = self._config.get("api", "active", fallback="local")
            section = f"api_{active}"
            agent = _SearchAgent(
                model      = self._config.get(section, "model",      fallback=None),
                base_url   = self._config.get(section, "base_url",   fallback=None),
                api_key    = self._config.get(section, "api_key",    fallback=""),
                api_format = self._config.get(section, "api_format", fallback="openai"),
                timeout    = int(self._config.get("loop", "timeout_seconds", fallback="120")),
                ssl_context = self._ssl_context,
            )
        except Exception as exc:
            logger.warning("coder._fetch_needed: could not create SearchAgent: %s", exc)
            return ""

        parts: list[str] = []
        total = 0

        for symbol in symbols:
            if total >= max_total_dep_chars:
                break
            try:
                result = agent.run(references=[symbol], base_dir=str(base_dir))
                found  = result.get("found", {})
                if symbol not in found:
                    logger.debug("coder._fetch_needed: symbol %r not found", symbol)
                    continue
                entry = found[symbol]
                block = (entry.get("code") or "").strip()
                file_path = entry.get("file", "")
                if not block:
                    logger.debug("coder._fetch_needed: empty block for %r", symbol)
                    continue
                # Trim individual block to budget.
                if len(block) > max_chars_per_dep:
                    block = block[:max_chars_per_dep] + "\n... [trimmed]"
                header = f"### dep: {symbol}  (from {file_path})"
                entry_text = f"{header}\n{block}"
                # Check total budget.
                if total + len(entry_text) > max_total_dep_chars:
                    remaining = max_total_dep_chars - total
                    if remaining < len(header) + 20:
                        break  # not worth including a stub
                    entry_text = entry_text[:remaining] + "\n... [trimmed]"
                parts.append(entry_text)
                total += len(entry_text)
            except Exception as exc:
                logger.debug("coder._fetch_needed: error fetching %r: %s", symbol, exc)
                continue

        return "\n\n".join(parts)

    def _read_file_contents(
        self,
        target_files: list[str],
        base_dir: Path,
        cited_symbol: str | None = None,
        task: "dict | None" = None,
    ) -> str:
        """Read and annotate file contents for the prompt.

        Each file is prefixed with a ``### path/to/file.py`` header.

        Small files (≤ ``max_file_chars``) are included byte-for-byte.  Larger
        files are split into symbol-aware chunks via :func:`chunk_file` and
        assembled with :func:`select_relevant_chunks`, guaranteeing that the
        import block and the *cited_symbol* are always present.  Any exception
        in the smart path falls back to plain character truncation so the caller
        always receives something useful.

        Parameters
        ----------
        task:
            When provided, the cited symbol is extracted from
            ``task["cited_locations"][0]["symbol"]`` (takes priority over the
            *cited_symbol* keyword argument).
        """
        # Resolve cited_symbol: explicit kwarg first, then task dict.
        if task is not None:
            locs = task.get("cited_locations") or []
            if locs and isinstance(locs[0], dict):
                _sym = locs[0].get("symbol")
                if _sym:
                    cited_symbol = _sym

        parts: list[str] = []
        for rel in target_files:
            abs_path = base_dir / rel
            if not abs_path.exists():
                parts.append(f"### {rel}\n[new file — no existing content]")
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                parts.append(f"### {rel}\n[unreadable: {exc}]")
                continue

            ext = Path(rel).suffix.lower()

            if len(content) <= self._max_file_chars:
                parts.append(f"### {rel}\n{content}")
                continue

            try:
                chunks   = chunk_file(content, ext, self._max_file_chars)
                assembled = select_relevant_chunks(chunks, cited_symbol, self._max_file_chars)
                parts.append(f"### {rel}\n{assembled}")
            except Exception as exc:
                logger.warning(
                    "coder: smart context failed for %s: %s — falling back", rel, exc
                )
                truncated = (
                    content[:self._max_file_chars]
                    + f"\n... [truncated — {len(content) - self._max_file_chars} more chars]"
                )
                parts.append(f"### {rel}\n{truncated}")

        return "\n\n".join(parts) if parts else "(no target files)"

    def _parse_response(
        self, text: str, task_id: str
    ) -> tuple[list[dict], str]:
        """Parse the LLM's JSON response into a list of ``{path, content}`` dicts.

        Returns
        -------
        (parsed_files, error_message)
            ``parsed_files`` is a non-empty list on success; ``error_message``
            is a non-empty string on failure (fail-closed: never returns a
            partially-constructed file list on parse error).
        """
        stripped = _strip_outer_fence(text)

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # Distinguish a TRUNCATED response (ran out of output tokens mid-file)
            # from genuinely malformed JSON, so the retry feedback is actionable.
            truncated = (
                "Unterminated string" in str(exc)
                or "Expecting" in str(exc)
            ) and not stripped.rstrip().endswith("}")
            if truncated:
                msg = (
                    "LLM response was cut off before the JSON was complete — the "
                    "revised file was too long for the output token budget. Emit "
                    "the COMPLETE file content and keep the response minimal (only "
                    "the required files), or raise [coder] max_tokens. "
                    f"(decode error: {exc})"
                )
            else:
                msg = f"JSON decode failed: {exc} — raw[:200]={text[:200]!r}"
            logger.warning("coder._parse_response [%s]: %s", task_id, msg)
            return [], msg

        if not isinstance(data, dict):
            msg = f"expected JSON object, got {type(data).__name__}"
            logger.warning("coder._parse_response [%s]: %s", task_id, msg)
            return [], msg

        files_raw = data.get("files")
        if not isinstance(files_raw, list):
            msg = f"'files' key missing or not a list — keys present: {list(data.keys())}"
            logger.warning("coder._parse_response [%s]: %s", task_id, msg)
            return [], msg

        parsed: list[dict] = []
        for i, item in enumerate(files_raw):
            if not isinstance(item, dict):
                logger.debug(
                    "coder._parse_response [%s]: item %d is not a dict — skipped",
                    task_id, i,
                )
                continue
            path    = (item.get("path") or "").strip()
            content = item.get("content")
            if not path:
                logger.warning(
                    "coder._parse_response [%s]: item %d missing 'path' — skipped",
                    task_id, i,
                )
                continue
            if content is None:
                logger.warning(
                    "coder._parse_response [%s]: item %d (%r) missing 'content' — skipped",
                    task_id, i, path,
                )
                continue
            # Strip any inner code fence the model may have added around the content.
            cleaned_content = _strip_code_fence(str(content))
            parsed.append({"path": path, "content": cleaned_content})

        if not parsed:
            msg = f"no valid file entries after parsing {len(files_raw)} item(s)"
            logger.warning("coder._parse_response [%s]: %s", task_id, msg)
            return [], msg

        return parsed, ""

    # ── Content safety ────────────────────────────────────────────────────────

    # Patterns that should never appear in LLM-generated file content.
    # Keyed by a short label (used in the rejection message) → substring/regex.
    # This is defence-in-depth, not a complete sandbox; it catches the most
    # dangerous accidental or injected payloads before they reach disk.

    # Patterns always checked regardless of task_mode: catastrophic payloads
    # with no legitimate false-positive risk in any document or creative context.
    _BLOCKED_ALWAYS: tuple[tuple[str, str], ...] = (
        ("fork bomb shell",     ":|:&"),
        ("fork bomb py",        "os.fork()"),
    )

    # Patterns checked only in code mode.  These have high false-positive rates
    # in documentation or creative writing:
    #   - A tutorial might teach "sudo apt install nginx"
    #   - A story character might "curl the data"
    #   - A poem about wounds could contain 'open("'
    _BLOCKED_CODE_ONLY: tuple[tuple[str, str], ...] = (
        # Destructive filesystem operations
        ("shutil.rmtree",       "shutil.rmtree"),
        ("os.remove",           "os.remove("),
        ("os.unlink",           "os.unlink("),
        ("rm -rf",              "rm -rf"),
        ("rm -f /",             "rm -f /"),
        # Shell injection via subprocess / os.system with dangerous args
        ("subprocess rm",       "subprocess"),          # paired with danger token — see _SUBPROCESS_DANGER_TOKENS
        ("os.system rm",        'os.system('),
        # Outbound data exfiltration via common tools
        ("curl exfil",          "curl "),
        ("wget exfil",          "wget "),
        # Overwrite root / system paths via open()  (sentinel — see _check_content_safety)
        ("open root write",     'open("/'),
    )

    # System directory prefixes that must never be written to by generated
    # code.  /tmp, /var/tmp, /home, and /Users are intentionally excluded:
    # they are legitimate destinations for temp files, exports, and local data.
    # Both single-quote and double-quote forms are checked in
    # _check_content_safety to close the single-quote bypass.
    _DANGEROUS_WRITE_PREFIXES: tuple[str, ...] = (
        "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/",
        "/proc/", "/sys/", "/root/", "/lib/",
    )

    # Patterns checked with whole-word regex (\b) instead of plain substring
    # containment.  Same (label, pattern) structure as _BLOCKED_CODE_ONLY so
    # tests can introspect membership identically.  Python \b treats '_' as a
    # word character, so underscore-joined identifiers (test_reboot_gracefully,
    # handle_shutdown_signal, test_sudo_not_needed) do NOT fire.
    # Applied in code mode only — same scope as _BLOCKED_CODE_ONLY.
    _BLOCKED_CODE_WORD_BOUNDARY: tuple[tuple[str, str], ...] = (
        ("sudo invocation",     "sudo"),
        ("shutdown cmd",        "shutdown"),
        ("reboot cmd",          "reboot"),
    )

    # subprocess is legitimate in many generated files; only flag it when
    # combined with a shell-deletion token so we avoid false positives.
    _SUBPROCESS_DANGER_TOKENS: tuple[str, ...] = (
        "rm ", "rm\t", '"rm"', "'rm'",  # rm with space, tab, or as a quoted list arg
        "rmdir", "rm -rf", "shutil.rmtree",
        "dd ", "mkfs",
        "sudo ", '"sudo"', "'sudo'",    # privilege escalation in any invocation form
        "shutdown", "reboot",
    )

    @classmethod
    def _check_content_safety(cls, content: str, task_mode: str = "code") -> tuple[bool, str]:
        """Return *(safe, reason)* — ``safe=False`` blocks the write.

        Scans generated file content for patterns that would be dangerous
        when the file is later executed by the Executor.  This mirrors the
        command-level ``_BLOCKED_COMMAND_PATTERNS`` check in Executor but
        operates on the *source text* before it reaches disk.

        For ``task_mode="code"`` (the default), all patterns are checked.
        For ``task_mode="docs"`` or ``task_mode="creative"``, only
        ``_BLOCKED_ALWAYS`` patterns are checked — prose content legitimately
        contains words like "sudo", "curl", and 'open("/' that would be
        false positives in code mode.

        The check is intentionally conservative: false positives (blocking a
        legitimately safe file) are far preferable to false negatives (writing
        and executing a destructive payload).
        """
        lower = content.lower()

        # Select the active pattern set based on task_mode.
        if task_mode == "code":
            active_patterns = cls._BLOCKED_ALWAYS + cls._BLOCKED_CODE_ONLY
        else:
            # docs / creative: only block truly catastrophic payloads
            active_patterns = cls._BLOCKED_ALWAYS

        for label, pattern in active_patterns:
            pat_lower = pattern.lower()

            # Special case: subprocess alone is fine; only block when paired
            # with a shell-deletion token in the same file.
            if pat_lower == "subprocess":
                if "subprocess" in lower:
                    for danger in cls._SUBPROCESS_DANGER_TOKENS:
                        if danger.lower() in lower:
                            return (
                                False,
                                f"blocked content: subprocess combined with "
                                f"dangerous token {danger!r} ({label})",
                            )
                continue

            # Special case: open() root-write — check both quote styles and
            # restrict to genuinely dangerous system paths so that legitimate
            # patterns like open("/tmp/out.txt", "w") are not blocked.
            if label == "open root write":
                for prefix in cls._DANGEROUS_WRITE_PREFIXES:
                    for q in ('"', "'"):
                        if f"open({q}{prefix}".lower() in lower:
                            return (
                                False,
                                f"blocked content: open() targeting system path "
                                f"{prefix!r} ({label})",
                            )
                continue

            if pat_lower in lower:
                return False, f"blocked content pattern {pattern!r} ({label})"

        # Word-boundary patterns (code mode only) — sudo / shutdown / reboot.
        # Checked separately so that identifier names embedding the keyword
        # (test_reboot_gracefully, handle_shutdown_signal, test_sudo_not_needed)
        # do NOT fire.  Python \b treats '_' as a word character, so
        # underscore-joined names have no boundary at the underscore.
        if task_mode == "code":
            import re as _re
            for label, pattern in cls._BLOCKED_CODE_WORD_BOUNDARY:
                keyword = pattern.lower().rstrip()
                if _re.search(r"\b" + _re.escape(keyword) + r"\b", lower):
                    return False, f"blocked content pattern {pattern!r} ({label})"

        return True, ""

    @staticmethod
    def _safe_dest(base_dir: Path, rel: str) -> tuple["Path | None", str]:
        """Resolve *rel* relative to *base_dir* and verify it stays inside.

        Prevents path traversal (``../../etc/passwd``) and absolute-path
        injection from LLM responses.  Returns ``(resolved_path, "")`` on
        success or ``(None, error_message)`` on violation.
        """
        # Reject obviously absolute paths before Path() normalises them.
        if rel.startswith("/") or (len(rel) > 1 and rel[1:3] == ":\\"):
            return None, f"rejected absolute path from LLM: {rel!r}"
        try:
            dest = (base_dir / rel).resolve()
        except (OSError, ValueError) as exc:
            return None, f"path resolution error for {rel!r}: {exc}"
        try:
            dest.relative_to(base_dir)
        except ValueError:
            return None, f"path escapes base_dir: {rel!r} → {dest}"
        return dest, ""

    def _write_files(
        self,
        parsed_files: list[dict],
        base_dir: Path,
        task_id: str,
        allowed_paths: "frozenset[str] | None" = None,
    ) -> tuple[list[str], str]:
        """Write parsed files to *base_dir*.

        Only paths listed in *allowed_paths* (the task's ``target_files``) are
        written.  Paths that escape *base_dir* (traversal / absolute) are
        rejected.  Each original file is backed up as ``<file>.coder.bak``
        before overwriting so changes are reversible without git.

        Parameters
        ----------
        parsed_files:
            List of ``{path, content}`` dicts from the LLM response.
        base_dir:
            Repo root — all writes must remain inside this directory.
        task_id:
            Used only for log messages.
        allowed_paths:
            Normalised relative paths the task declared as ``target_files``.
            When not ``None``, any path outside this set is skipped with a
            warning so the LLM cannot silently touch unrelated files.

        Returns
        -------
        (written_paths, first_error_message)
            *written_paths* contains every path successfully written even when
            a later file errors.  *first_error_message* is ``""`` when all
            writes succeed.
        """
        written: list[str] = []
        first_error = ""

        for item in parsed_files:
            rel     = item["path"]
            content = item["content"]

            # ── Guard 1: path must not escape base_dir ─────────────────────
            dest, path_err = self._safe_dest(base_dir, rel)
            if path_err:
                msg = f"[SAFETY] {path_err}"
                logger.error("coder._write_files [%s]: %s", task_id, msg)
                if not first_error:
                    first_error = msg
                continue

            # ── Guard 2: path must be in the task's approved target_files ──
            if allowed_paths is not None:
                # Normalise to forward-slash for comparison.
                norm = rel.replace("\\", "/").lstrip("./")
                allowed_norm = {p.replace("\\", "/").lstrip("./") for p in allowed_paths}
                if norm not in allowed_norm:
                    msg = (
                        f"[SAFETY] LLM tried to write {rel!r} which is not in "
                        f"target_files — skipped to protect unrelated files"
                    )
                    logger.warning("coder._write_files [%s]: %s", task_id, msg)
                    if not first_error:
                        first_error = msg
                    continue

            # ── Guard 3: scan file content for dangerous patterns ──────────
            content_safe, content_reason = self._check_content_safety(content, self._task_mode)
            if not content_safe:
                msg = f"[SAFETY] {content_reason} in file {rel!r} — write blocked"
                logger.error("coder._write_files [%s]: %s", task_id, msg)
                if not first_error:
                    first_error = msg
                continue

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Back up existing file before overwriting (reversible).
                if dest.exists():
                    backup = dest.with_suffix(dest.suffix + ".coder.bak")
                    backup.write_text(
                        dest.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8",
                    )
                    logger.debug(
                        "coder._write_files [%s]: backed up %s → %s",
                        task_id, dest, backup,
                    )

                dest.write_text(content, encoding="utf-8")
                written.append(rel)
                logger.info(
                    "coder._write_files [%s]: wrote %s (%d chars)",
                    task_id, rel, len(content),
                )

            except OSError as exc:
                msg = f"write failed for {rel}: {exc}"
                logger.error("coder._write_files [%s]: %s", task_id, msg)
                if not first_error:
                    first_error = msg

        return written, first_error


# ─────────────────────────────────────────────────────────────────────────────
# Symbol-aware file chunking (SCTX Task 1)
# ─────────────────────────────────────────────────────────────────────────────

def chunk_file(source: str, file_ext: str, max_chars: int) -> list[dict]:
    """Split *source* into symbol-aware chunks for prompt assembly.

    Each returned dict has:
        name      — "full" | "imports" | <symbol_name> | "truncated"
        content   — the source text for this chunk
        line      — 1-based line number where this chunk starts
        is_import — True only on the imports chunk

    If ``len(source) <= max_chars`` the list contains a single "full" chunk
    whose content is the unmodified source (byte-for-byte identical).

    On AST parse failure for a .py file a WARNING is logged and a single
    "truncated" chunk is returned; no exception is raised.  For all other
    extensions a regex scan is used (best-effort; partial results are fine).
    """
    if len(source) <= max_chars:
        return [{"name": "full", "content": source, "line": 1}]

    ext = (file_ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext

    # ── Extract import / preamble block ────────────────────────────────────────
    # Collect all leading lines that look like imports/includes/pragmas for
    # any supported language, plus blank lines interleaved between them.
    # Patterns covered:
    #   Python/JS/TS:   import …  /  from … import …
    #   Go:             import "…"  /  import ( … )
    #   Rust:           use …;
    #   C/C++/ObjC:     #include …  /  #pragma …  /  #define …  /  #ifndef …
    #   Java/Kotlin:    package …  /  import …
    #   Ruby:           require …  /  require_relative …
    #   PHP:            use …;  /  require …  /  include …
    #   Shell:          #!/…  (shebang on line 1)
    # Unknown languages: the block will simply be empty (first non-blank line
    # is not a preamble line), which is safe — no import chunk is emitted.
    _import_re = re.compile(
        r"^\s*("
        r"import\s"                             # Python, JS, TS, Java, Go bare
        r"|from\s"                              # Python from-import
        r"|use\s"                               # Rust / PHP
        r"|#include\s*[\"<]"                  # C/C++/ObjC
        r"|#pragma\s"                           # C/C++ pragmas
        r"|#ifndef\s|#ifdef\s|#define\s|#endif"  # C/C++ guards
        r"|package\s"                           # Go, Java, Kotlin
        r"|require\s*[\"'(]"                 # Ruby, PHP, Node.js
        r"|require_relative\s*[\"'(]"        # Ruby
        r"|include\s*[\"'(]"                 # PHP
        r"|#!/"                                  # shebang (line 1 only)
        r")"
    )
    _blank_re = re.compile(r"^\s*$")
    lines = source.splitlines(keepends=True)
    import_end = 0
    for i, line in enumerate(lines):
        # Shebang is only valid on the very first line.
        if i == 0 and line.startswith("#!"):
            import_end = 1
        elif _blank_re.match(line) or _import_re.match(line):
            import_end = i + 1
        else:
            break
    import_content = "".join(lines[:import_end])
    import_chunk: dict = {
        "name": "imports",
        "content": import_content,
        "line": 1,
        "is_import": True,
    }

    # ── Collect top-level symbol names + start lines ──────────────────────────
    symbols: list[tuple[str, int]] = []  # (name, 1-based lineno)

    if ext == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            logger.warning(
                "chunk_file: ast.parse failed for ext=%r — returning truncated chunk", ext
            )
            return [
                {
                    "name": "truncated",
                    "content": source[:max_chars] + "\n... [truncated]",
                    "line": 1,
                }
            ]
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append((node.name, node.lineno))
    else:
        # Best-effort regex scan for top-level symbols across all brace-based
        # and indent-based languages.  Covers:
        #   JS/TS:    function foo / async function foo / class Foo
        #             const foo = (...) =>    (arrow functions)
        #   Go:       func foo / func (r T) foo
        #   Rust:     fn foo / pub fn foo / pub(crate) fn foo
        #   Java/C#:  class Foo / interface IFoo / enum Status
        #             public void foo(  (methods — best-effort)
        #   C/C++:    type_name foo(   (free functions, rough heuristic)
        #   Ruby:     def foo / class Foo / module Bar
        #   PHP:      function foo / class Foo
        #   Swift:    func foo / class Foo / struct Bar / enum Baz / protocol P
        #   Kotlin:   fun foo / class Foo / object Obj / data class Bar
        _sym_re = re.compile(
            r"(?m)^[ \t]*"
            # optional visibility / modifier keywords (greedy, non-capturing)
            r"(?:(?:pub(?:\s*\([^)]*\))?|public|private|protected|internal"
            r"|static|final|abstract|override|open|sealed|data|inline|suspend"
            r"|async|export|default)\s+)*"
            # the keyword that introduces a symbol
            r"(?:function|func|fn|def|class|interface|struct|enum|module"
            r"|object|protocol|trait|impl|type|fun)"
            r"\s+"
            # optional receiver for Go methods: (r *Receiver)
            r"(?:\([^)]*\)\s+)?"
            # the symbol name — captured
            r"([A-Za-z_][A-Za-z0-9_]*)"
        )
        # Also capture JS/TS arrow-function assignments at statement level:
        #   const myFunc = (...) => {
        #   let   myFunc = async (...) => {
        _arrow_re = re.compile(
            r"(?m)^[ \t]*(?:export\s+)?(?:const|let|var)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*="
            r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=>"
        )
        seen_names: set[str] = set()
        for m in _sym_re.finditer(source):
            name = m.group(1)
            if name not in seen_names:
                seen_names.add(name)
                lineno = source.count("\n", 0, m.start()) + 1
                symbols.append((name, lineno))
        for m in _arrow_re.finditer(source):
            name = m.group(1)
            if name not in seen_names:
                seen_names.add(name)
                lineno = source.count("\n", 0, m.start()) + 1
                symbols.append((name, lineno))
        # Keep stable line-order after the two passes.
        symbols.sort(key=lambda t: t[1])

    # ── Extract the source block for each symbol ──────────────────────────────
    symbol_chunks: list[dict] = []
    for name, lineno in symbols:
        try:
            block = _block_extractor_extract_block(source, name, ext)
        except Exception:
            block = ""
        if block:
            symbol_chunks.append({"name": name, "content": block, "line": lineno})

    symbol_chunks.sort(key=lambda c: c["line"])

    return [import_chunk] + symbol_chunks


def select_relevant_chunks(
    chunks: list[dict],
    cited_symbol: str | None,
    budget_chars: int,
) -> str:
    """Assemble *chunks* into a prompt string, respecting *budget_chars*.

    Ordering and budget rules:
      1. Import chunk always first; its size is deducted from the budget.
      2. *cited_symbol* chunk always second — never stubbed, even if its size
         alone exceeds the remaining budget.
      3. All remaining chunks are included in ascending line order; any chunk
         that does not fit is replaced with a single stub comment::

             # [symbol_name — N chars, not included]

    Chunks are separated by a blank line.  Returns ``"(no content)"`` when
    *chunks* is empty.
    """
    if not chunks:
        return "(no content)"

    # "full" chunk means the file was already within max_chars — return as-is.
    full = next((c for c in chunks if c.get("name") == "full"), None)
    if full is not None:
        return full["content"]

    import_chunk = next((c for c in chunks if c.get("is_import")), None)

    result_parts: list[str] = []
    remaining = budget_chars
    included: set[str] = set()

    # 1. Import chunk — always first.
    if import_chunk and import_chunk["content"]:
        result_parts.append(import_chunk["content"])
        remaining -= len(import_chunk["content"])
        included.add(import_chunk["name"])

    # 2. Cited symbol — always second, never stubbed.
    if cited_symbol:
        cited_chunk = next(
            (c for c in chunks if c.get("name") == cited_symbol), None
        )
        if cited_chunk:
            result_parts.append(cited_chunk["content"])
            remaining -= len(cited_chunk["content"])
            included.add(cited_symbol)

    # 3. Remaining chunks in ascending line order.
    others = sorted(
        (c for c in chunks if c.get("name") not in included),
        key=lambda c: c.get("line", 0),
    )
    for chunk in others:
        name    = chunk.get("name", "?")
        content = chunk.get("content", "")
        if len(content) <= remaining:
            result_parts.append(content)
            remaining -= len(content)
        else:
            result_parts.append(f"# [{name} — {len(content)} chars, not included]")

    return "\n\n".join(result_parts)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_outer_fence(text: str) -> str:
    """Remove an outer ``` or ```json fence wrapping the entire JSON response.

    Models sometimes emit:
        ```json
        {"files": [...]}
        ```
    even when told not to.  This strips that wrapper before ``json.loads``.
    """
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence line (e.g. "```json").
        # If there is no newline the input is just a bare fence marker with no
        # content — return the original text so json.loads can fail gracefully
        # rather than getting an empty string.
        if "\n" not in t:
            return t
        t = t.split("\n", 1)[1]
        # Drop the closing fence
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _strip_code_fence(text: str) -> str:
    """Strip an optional leading code-fence from a file content string.

    Mirrors ``OrchestratorActions._strip_code_fence`` from tools/actions.py:
    if the model wrapped the whole file in ``` fences, return the inside.
    Preserves the final newline expected of well-formed source files.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.rstrip("\n") + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory — mirrors make_executor / review_clusters pattern
# ─────────────────────────────────────────────────────────────────────────────

def make_coder(config: configparser.ConfigParser, task_mode: str = "code") -> Coder:
    """Create a :class:`Coder` from *config* (agents.ini).

    Reads the active API section (``[api_local]`` / ``[api_remote]``) using
    the same convention as every other agent in this codebase.

    Parameters
    ----------
    config:
        A ``ConfigParser`` instance loaded from ``agents.ini``.

    Returns
    -------
    Coder
        Ready to call ``.generate()``.
    """
    active    = config.get("api", "active", fallback="local")
    section   = f"api_{active}"
    base_url  = config.get(section, "base_url")
    api_key   = config.get(section, "api_key",    fallback="")
    model     = config.get(section, "model")
    api_fmt   = config.get(section, "api_format", fallback="openai")
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

    return Coder(
        config     = config,
        base_url   = base_url,
        api_key    = api_key,
        model      = model,
        api_format = api_fmt,
        verify_ssl = verify_ssl,
        task_mode  = task_mode,
    )
