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

import configparser
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.agent_trace import tracer
from tools.llm_stream import request_completion, strip_think

logger = logging.getLogger(__name__)

# ── Maximum file content characters sent in the prompt (per file). ───────────
_MAX_FILE_CHARS = 8_000

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
    "5. Do NOT add any explanation or commentary outside the JSON."
)

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

        sec = "coder"
        self._temperature = float(config.get(sec, "temperature", fallback="0.2"))
        self._max_tokens  = int(config.get(sec, "max_tokens",   fallback="4096"))
        self._system      = config.get(sec, "system", fallback=_SYSTEM_PROMPT).strip()
        self._timeout     = float(config.get("loop", "timeout_seconds", fallback="300"))

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        task: dict,
        base_dir: str | Path,
        prior_feedback: Optional[list[str]] = None,
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
        user_msg = self._build_prompt(task, base_dir, prior_feedback or [])

        payload: dict[str, Any] = {
            "model":       self._model,
            "temperature": self._temperature,
            "max_tokens":  self._max_tokens,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user",   "content": user_msg},
            ],
        }
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/chat/completions"

        tracer.event(
            source="coder", target="llm", kind="llm_request",
            content=user_msg,
            params={"model": self._model, "temperature": self._temperature,
                    "task_id": task_id},
        )

        try:
            raw_text = request_completion(
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

        # ── Strip think blocks + outer fences, then parse JSON ────────────────
        cleaned = strip_think(raw_text)
        tracer.event(
            source="llm", target="coder", kind="llm_response",
            content=cleaned, params={"task_id": task_id},
        )

        parsed_files, parse_error = self._parse_response(cleaned, task_id)
        if parse_error:
            return CoderResult(task_id=task_id, error=parse_error, raw_response=cleaned)

        # ── Write files to disk ───────────────────────────────────────────────
        target_files = task.get("target_files") or []
        allowed = frozenset(target_files) if target_files else None
        written, write_error = self._write_files(
            parsed_files, base_dir, task_id, allowed_paths=allowed
        )
        if write_error and not written:
            return CoderResult(
                task_id=task_id, error=write_error, raw_response=cleaned
            )

        result = CoderResult(
            task_id=task_id,
            files_written=written,
            error=write_error,
            raw_response=cleaned,
        )
        logger.info("coder.generate: %s", result.summary())
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        task: dict,
        base_dir: Path,
        prior_feedback: list[str],
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
        file_contents = self._read_file_contents(target_files, base_dir)

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

    def _read_file_contents(self, target_files: list[str], base_dir: Path) -> str:
        """Read and annotate file contents for the prompt.

        Each file is prefixed with a ``### path/to/file.py`` header.  Content
        is truncated to ``_MAX_FILE_CHARS`` with a notice.  Files that don't
        exist yet are labelled as ``[new file — no existing content]`` so the
        model knows it must create them from scratch.
        """
        parts: list[str] = []
        for rel in target_files:
            abs_path = base_dir / rel
            if not abs_path.exists():
                content = "[new file — no existing content]"
            else:
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    content = f"[unreadable: {exc}]"
                if len(content) > _MAX_FILE_CHARS:
                    content = (
                        content[:_MAX_FILE_CHARS]
                        + f"\n... [truncated — {len(content) - _MAX_FILE_CHARS} more chars]"
                    )
            parts.append(f"### {rel}\n{content}")
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
        # Drop the opening fence line (e.g. "```json")
        t = t.split("\n", 1)[1] if "\n" in t else ""
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

def make_coder(config: configparser.ConfigParser) -> Coder:
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
    )
