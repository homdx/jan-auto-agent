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
    result.files_skipped   # list[str] of extra paths dropped by the target_files guard
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

RUN-8: ``error_kind`` distinguishes the two halves of that.  ``"transport"``
means the socket gave up before the model could answer (a stream-read
timeout, ``URLError``, ``HTTPError``) — nothing was produced, so the attempt
is re-run instead of being charged and no ``coder failed`` line reaches the
next prompt.  ``"parse"`` means the call did come back and this attempt still
could not turn it into files (NO-JSON, malformed or cut-off JSON, a duplicate
chapter, a failed write) — those stay charged attempts, and the RUN-4 budget
ladder still climbs on a cut-off reply.

Configuration (agents.ini [coder])
------------------------------------
temperature    — sampling temperature (default 0.2)
max_tokens     — token budget (default 16384)
max_tokens_cap — ceiling for the RUN-4 truncation ladder: a reply cut off by
                 the output budget is re-asked with max_tokens × 2, up to here
                 (default 4 × max_tokens)
system         — override the built-in system prompt (optional)

agents.ini [api] / [api_local] / [api_remote] supply base_url, api_key, model,
api_format, verify_ssl — the same pattern used throughout this codebase.
"""

from __future__ import annotations

import ast
import configparser
import json
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.block_extractor import extract_block as _block_extractor_extract_block
# SearchAgent is imported lazily inside Coder._fetch_needed
from tools.auto.utils import _cfg_mode, atomic_write_text, in_style_of
from tools.auto.context_assembler import ContextAssembler
from tools.ui import safe_print as _safe_print, safe_stdout_write as _safe_stdout_write

from tools.agent_trace import tracer
import tools.llm_stream as _llm_stream
from tools.llm_stream import strip_think
from tools.config_safe import safe_getboolean

logger = logging.getLogger(__name__)

# [coder] max_file_chars is read from agents.ini; this default applies only when the key is absent.
_DEFAULT_MAX_FILE_CHARS = 8_000

# Output-budget ladder: a reply cut off mid-JSON doubles the budget on the next attempt, up to the configured cap (RUN-4).
_TRUNCATION_HEAD = "LLM response was cut off before the JSON was complete"
# The creative counterpart: a chapter cut off at the budget climbs the same ladder.
_TRUNCATION_HEAD_PROSE = "creative coder: response hit the token budget mid-sentence"
_TRUNCATION_HEADS = (_TRUNCATION_HEAD, _TRUNCATION_HEAD_PROSE)
# Advice for a cut-off reply once the budget has risen: the file must be complete, so do not say shorten; {budget} is the next attempt's budget.
_TRUNCATION_RAISED_ADVICE = (
    "Emit the COMPLETE file content — the output budget was raised to "
    "{budget} tokens for this attempt, so do NOT shorten the file. "
)
# Truncation advice at the cap, where shortening is the only lever left.
_TRUNCATION_ADVICE = (
    "Emit the COMPLETE file content and keep the response minimal (only the "
    "required files), or raise [coder] max_tokens. "
)
_BUDGET_STEP_MULT = 2
_DEFAULT_BUDGET_CAP_MULT = 4

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
    "definition is NOT shown to you, and you can still produce a reasonable, complete "
    "implementation right now (e.g. by making a sound assumption about its shape), do so — "
    "then ALSO add a top-level \"context_request\" array naming the exact symbols you'd "
    "like to see, e.g. {\"files\": [...], \"context_request\": [\"Config\", \"_resolve_path\"]}. "
    "This does NOT change what \"files\" must contain — it must still be your complete "
    "best-effort implementation (rule 6). The named symbols are fetched and provided on "
    "your NEXT attempt, if there is one, so a later pass can correct any wrong assumption. "
    "Omit context_request (or use []) when you already have everything you need.\n"
    "8. If instead you genuinely cannot produce a correct implementation without seeing a "
    "symbol's definition — not something you can reasonably assume — still fill \"files\" "
    "with your best current attempt (rule 6 always applies), but ALSO add a top-level "
    "\"missing_context\" key listing the symbol names you need. "
    "Format: \"missing_context\": [\"ClassName\", \"function_name\"]. Unlike context_request, "
    "this is fetched IMMEDIATELY and you are asked again, once, in the SAME turn, with that "
    "context added — treat your \"files\" here as a fallback in case that immediate retry "
    "doesn't happen. Omit the key entirely if context was sufficient; do not include it as "
    "an empty list.\n"
    "Use at most one of \"context_request\" / \"missing_context\" per response — "
    "\"context_request\" is for a working answer you're confident enough to give now but "
    "could improve with more context next time; \"missing_context\" is for an answer you're "
    "not confident in at all without that context, needed to fix it within this same turn.\n"
    "9. To DELETE a file (e.g. a refactor that merges a module away), include "
    '{"path": "relative/path.py", "delete": true} in "files" — with NO '
    '"content" key. Deletion is only allowed for paths listed in target_files; '
    "the original is backed up automatically. Never emit shell commands or "
    "os.remove calls to delete files — use the delete flag.\n"
    "10. Do NOT introduce a new third-party dependency. Use only the standard "
    "library and packages the repository already imports or declares in its "
    "dependency manifest (requirements.txt, package.json, pom.xml, go.mod, "
    "Cargo.toml). If the task looks like it needs a new one, implement it with "
    "what is already available — an unavailable import makes the acceptance "
    "check fail.\n"
    "11. You are rewriting the WHOLE file, so preserve everything unrelated to "
    "the task — existing imports, helpers, comments and docstrings — and match "
    "the file's existing conventions (naming, error handling, logging, comment "
    "style). Never reformat or strip comments you were not asked to change."
)

# Backward-compat alias — "code" is the default persona.
_SYSTEM_PROMPT_CODE = _SYSTEM_PROMPT

# Docs and creative reuse the JSON contract and rules; only the persona and framing differ (as the architect and validator do).
_SYSTEM_PROMPT_DOCS = _SYSTEM_PROMPT_CODE.replace(
    "You are a senior software engineer implementing a targeted code improvement. ",
    "You are a senior technical writer implementing a targeted documentation "
    "improvement. You produce clear, accurate prose — not code. ",
    1,
)
_SYSTEM_PROMPT_CREATIVE = (
    "You are a creative writing author generating a chapter of long-form fiction. "
    "Write in the SAME language as the story so far (the prior chapter / synopsis you "
    "are given, or the task instruction if this is the first chapter) — do not switch to "
    "English or any other language partway through, even for a single sentence. "
    "Return ONLY the chapter prose. No JSON, no preamble, no code fences. "
    "Write the complete chapter as plain prose text. "
    "If you are given a single target file, write the complete chapter text for it. "
    "If you are given multiple target files, prefix each file's content with "
    "<<<FILE: relative/path>>> on its own line and close it with <<<END>>> on its own line. "
    "Optional: if you need material from a prior chapter that was not provided, "
    "you may add a final line (after all prose) in the form: "
    "CONTEXT_REQUEST: name1, name2 — listing the chapter filenames or topic names you need. "
    "Strip that line from the chapter content itself. "
    "Do not include CONTEXT_REQUEST unless you genuinely need missing context."
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
{closing_instruction}
"""


# Result dataclass

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
    files_skipped:
        Relative paths the LLM asked for that were NOT written because they
        fall outside the task's ``target_files`` (RUN-1 "Guard 2" skips).
        These are protection skips, not failures: they never set ``error`` and
        never affect :attr:`succeeded`, so an attempt whose target files all
        landed still succeeds when the model added extra files.
    error:
        Human-readable error description; empty string on success.
    raw_response:
        The raw (post-strip_think) LLM text; kept for logging / feedback.
    max_tokens:
        The token budget THIS attempt went out at (RUN-4). InnerLoop traces it
        on the coder decision event so a trace can show at which tier a task
        was running. 0 when the result did not come from Coder.generate().
    budget_raised:
        RUN-4: True when this attempt's cut-off reply just raised the budget
        for the next attempt of the same task. False both when the budget did
        not move and when the reply was not a truncation.
    error_kind:
        RUN-8: what kind of failure ``error`` is — ``"transport"`` when the
        LLM call itself failed before the model could answer (a read timeout,
        ``URLError``, ``HTTPError`` …, so nothing was produced and the attempt
        should not be charged), ``"parse"`` when the call returned something
        that this attempt could not turn into written files (NO-JSON,
        malformed or cut-off JSON, a duplicate chapter, a failed write), and
        ``""`` when the result carries no classification at all. ``error``
        itself is unchanged by this field.
    """

    task_id:       str = ""
    files_written: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    error:         str = ""
    raw_response:  str = field(default="", repr=False)
    missing_context: list[str] = field(default_factory=list)
    context_satisfied: bool = True  # False when the LLM reported missing_context
    max_tokens:    int = 0
    budget_raised: bool = False
    error_kind:    str = ""   # RUN-8: "" / "parse" / "transport"

    @property
    def succeeded(self) -> bool:
        """``True`` iff at least one file was written and no error was recorded."""
        return bool(self.files_written) and not self.error

    def summary(self) -> str:
        """One-line status string for logging."""
        if self.succeeded:
            skipped = (f" (skipped extra: {self.files_skipped})"
                       if self.files_skipped else "")
            return f"[{self.task_id}] CODER OK — wrote {self.files_written}{skipped}"
        return f"[{self.task_id}] CODER FAIL — {self.error or 'no files written'}"


# Coder


def _is_truthy_delete(value) -> bool:
    """True iff `value` should be interpreted as 'delete this file'.

    Models regularly emit the ``delete`` key in non-canonical forms:
    ``"true"`` (string), ``1`` (int), ``"yes"`` (string), etc. A strict
    ``value is True`` check rejects every one of those and the file
    silently stays in place. Accept the small canonical set:
        - JSON ``True``
        - ``"true"/"True"/"TRUE"``  (case-insensitive)
        - ``"yes"/"Yes"/"YES"``     (case-insensitive)
        - integer ``1``
    Anything else (``False``, ``0``, ``"false"``, ``None``, prose) is
    NOT a delete — preserving the prior fail-loud behaviour for
    ambiguous or missing values.
    """
    if value is True:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


# Guard-2 skip messages (RUN-1)


def _guard2_skip_message(rel: str) -> str:
    """The [SAFETY] text for one path outside the task's ``target_files``.

    Kept in one place so the WARNING line in ``_write_files`` and the failure
    message used when *nothing* was written stay word-for-word identical (the
    latter is what the model reads back as feedback).
    """
    return (
        f"[SAFETY] LLM tried to write {rel!r} which is not in "
        f"target_files — skipped to protect unrelated files"
    )


def _guard2_all_skipped_error(skipped: list[str]) -> str:
    """Error for an attempt where every returned path was outside target_files.

    Guard 2 no longer records a skip as ``first_error`` (a skip is protection,
    not a verdict). But when *nothing* was written there is no code for the
    executor or validator to look at, so the attempt must still fail — with
    the existing [SAFETY] wording, naming every dropped path, so the model
    learns which of its files never landed instead of seeing a bare
    "no files written".
    """
    if not skipped:
        return ""
    return "; ".join(_guard2_skip_message(p) for p in skipped[:5])


def _file_encodings(path: Path) -> "tuple[str, ...]":
    """The encodings *path* may be read back in, most faithful first.

    HARDEN-5 D: the Coder read a task's files with ``errors="replace"`` and
    wrote them back as UTF-8, so a legacy file was shown to the model as a run
    of U+FFFD and then stored as UTF-8 mojibake — the same bug CC-16 fixed for
    ``/edit`` by deciding the codec once, from the file's own bytes, before the
    model is asked. The Coder's whole-file path uses that same helper
    (``tools.actions._edit_write_encodings``), not a copy, so ``/edit`` and the
    Coder cannot disagree about what one file's bytes mean.

    Fail-open: anything unexpected — a file that vanished, a read error, a
    broken import — yields ``("utf-8",)``, which is the old behaviour, so a
    codec probe can never be what loses a write.
    """
    try:
        from tools.actions import _edit_write_encodings

        encodings = _edit_write_encodings(str(path))
    except Exception:
        return ("utf-8",)
    return tuple(encodings) or ("utf-8",)


def _file_read_encoding(path: Path) -> str:
    """The one codec *path* is read in: the most faithful candidate.

    ``errors="replace"`` is deliberately NOT passed — a legacy byte has a real
    character in its own codec, and swapping it for U+FFFD is what the model
    then copies back into the file. Strict is correct: if the strict decode
    still fails the caller falls back to the old lossy read, so a file that
    changed under us still gets shown.
    """
    encodings = _file_encodings(path)
    return encodings[0] if encodings else "utf-8"


def _pick_file_encoding(
    encodings: "tuple[str, ...]", content: str
) -> "tuple[str | None, str]":
    """The first of *encodings* that can hold *content*, else ``(None, missing)``.

    Delegates to ``tools.actions._pick_edit_encoding`` — the same function
    ``/edit`` uses — so both paths walk one list in one order instead of each
    picking its own codec for the same file. Fail-open: an unexpected error
    yields ``("utf-8", "")``, the behaviour before HARDEN-5 D.
    """
    try:
        from tools.actions import _pick_edit_encoding

        return _pick_edit_encoding(encodings, content)
    except Exception:
        return ("utf-8", "")


def _refuse_inencodable(encoding: "str | None", missing: str, rel: str) -> "str | None":
    """The refusal message when *encoding* is ``None``, else ``None``.

    HARDEN-5 D: a reply the file's codec cannot hold is refused, never written
    with ``?`` or U+FFFD. The file is left exactly as it was, and the message
    names what is missing so the model can drop those characters and try again
    instead of seeing a bare codec traceback.
    """
    if encoding is not None:
        return None
    absent = missing if missing.strip() else "some characters"
    return (
        f"[ENCODING] {rel!r}: the file is not UTF-8 and the reply cannot be "
        f"written in it — missing {absent}. Nothing was written to that file; "
        f"convert it to UTF-8 first, or revise without those characters."
    )


class Coder(_llm_stream.LLMClientBase):
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
        run_goal: str = "",
    ) -> None:
        super().__init__(config, base_url, api_key, model, api_format, verify_ssl)

        self._task_mode = task_mode
        # The raw goal string is the most reliable language sample for a chapter-1 cold start (AUTO-FIX-9).
        self._run_goal = str(run_goal or "")
        sec = "coder"
        # Config reads are guarded per call (BUGFIX): a malformed value falls back instead of crashing construction.
        try:
            self._temperature = float(config.get(sec, "temperature", fallback="0.2"))
        except ValueError as exc:
            logger.warning("config [%s] temperature is malformed (%s) — using 0.2", sec, exc)
            self._temperature = 0.2
        # AUTO-CR-3: prefer max_tokens_{task_mode} (e.g. max_tokens_creative) over max_tokens.
        try:
            self._max_tokens = int(_cfg_mode(config, sec, "max_tokens", task_mode, fallback="16384"))
        except ValueError as exc:
            logger.warning("config [%s] max_tokens is malformed (%s) — using 16384", sec, exc)
            self._max_tokens = 16384
        # RUN-4 ceiling for the truncation ladder: the budget doubles up to this cap (default 4x max_tokens).
        _default_cap = int(self._max_tokens) * _DEFAULT_BUDGET_CAP_MULT
        try:
            self._max_tokens_cap = int(_cfg_mode(
                config, sec, "max_tokens_cap", task_mode, fallback=str(_default_cap)))
        except ValueError as exc:
            logger.warning("config [%s] max_tokens_cap is malformed (%s) — using %d",
                           sec, exc, _default_cap)
            self._max_tokens_cap = _default_cap
        # A cap below the base budget would shrink the first attempt; clamp it up, with a warning.
        if self._max_tokens_cap < int(self._max_tokens):
            logger.warning(
                "config [%s] max_tokens_cap=%d is below max_tokens=%d — "
                "using %d (the ladder cannot shrink a budget)",
                sec, self._max_tokens_cap, self._max_tokens, self._max_tokens,
            )
            self._max_tokens_cap = int(self._max_tokens)
        # The ladder's learned budget is kept in memory only, as Gate 1's is (RUN-4).
        self._learned_max_tokens: Optional[int] = None  # last budget that parsed
        self._task_id: str = ""                          # same-task cursor
        self._task_max_tokens: int = int(self._max_tokens)  # same-task budget
        # System prompt by task_mode (as architect and validator do): mode key, legacy key, then constant.
        _mode_key = f"system_{self._task_mode}" if self._task_mode != "code" else None
        if _mode_key and config.has_option(sec, _mode_key):
            self._system  = config.get(sec, _mode_key).strip()
        else:
            _builtin      = _CODER_SYSTEM_PROMPTS.get(self._task_mode, _SYSTEM_PROMPT_CODE)
            self._system  = config.get(sec, "system", fallback=_builtin).strip()
        try:
            self._timeout = float(config.get("loop", "timeout_seconds", fallback="300"))
        except ValueError as exc:
            logger.warning("config [loop] timeout_seconds is malformed (%s) — using 300", exc)
            self._timeout = 300.0
        try:
            self._max_file_chars = int(config.get(sec, "max_file_chars", fallback=str(_DEFAULT_MAX_FILE_CHARS)))
        except ValueError as exc:
            logger.warning("config [%s] max_file_chars is malformed (%s) — using %d",
                            sec, exc, _DEFAULT_MAX_FILE_CHARS)
            self._max_file_chars = _DEFAULT_MAX_FILE_CHARS
        # Optional deterministic gate for non-ASCII identifiers in generated .py files (code mode only).
        try:
            self._ascii_identifiers_only = config.getboolean(
                sec, "ascii_identifiers_only", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] ascii_identifiers_only is malformed (%s) — using False", sec, exc)
            self._ascii_identifiers_only = False
        # Allow editing files that already contain a flagged pattern (grandfathering); introducing one is still blocked.
        try:
            self._allow_preexisting_patterns = config.getboolean(
                sec, "allow_preexisting_patterns", fallback=True)
        except ValueError as exc:
            logger.warning("config [%s] allow_preexisting_patterns is malformed (%s) — using True", sec, exc)
            self._allow_preexisting_patterns = True
        active_profile = config.get("api", "active", fallback="local")
        # num_ctx is the Ollama context size; 0 means the server default (AUTO-CR-3).
        _num_ctx_str = _cfg_mode(config, sec, "num_ctx", task_mode, fallback=None)
        if _num_ctx_str is None:
            _num_ctx_str = config.get(f"api_{active_profile}", "num_ctx", fallback="0")
        try:
            self._num_ctx = int(_num_ctx_str)
        except ValueError as exc:
            logger.warning("config num_ctx is malformed (%r: %s) — using 0", _num_ctx_str, exc)
            self._num_ctx = 0
        # Thinking-model risk: the coder's JSON plus a think block may overflow the budget, so the context read is guarded (AUTO-FIX).
        try:
            self._think = config.getboolean(sec, "think", fallback=False)
        except ValueError as exc:
            logger.warning("config [%s] think is malformed (%s) — using False", sec, exc)
            self._think = False
        # Context-probe: fetch missing symbols on first LLM response, then retry once.
        try:
            self._context_probe_enabled = config.getboolean(sec, "context_probe", fallback=True)
        except ValueError as exc:
            logger.warning("config [%s] context_probe is malformed (%s) — using True", sec, exc)
            self._context_probe_enabled = True
        try:
            self._max_chars_per_dep = config.getint(sec, "max_chars_per_dep", fallback=2000)
        except ValueError as exc:
            logger.warning("config [%s] max_chars_per_dep is malformed (%s) — using 2000", sec, exc)
            self._max_chars_per_dep = 2000
        try:
            self._max_dep_chars = config.getint(sec, "max_dep_chars", fallback=6000)
        except ValueError as exc:
            logger.warning("config [%s] max_dep_chars is malformed (%s) — using 6000", sec, exc)
            self._max_dep_chars = 6000

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

        # RUN-4: the budget for this attempt; a retry of the same task keeps the ladder's value, a new task starts at max(config, learned).
        budget = self._budget_for_task(task_id)

        # ── Build and send the prompt ─────────────────────────────────────────
        user_msg = self._build_prompt(task, base_dir, prior_feedback or [], prefetched_context)

        url, headers, payload = _llm_stream.build_chat_request(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            api_format=self._api_format, temperature=self._temperature,
            max_tokens=budget, system=self._system, user_msg=user_msg,
            num_ctx=self._num_ctx, think=self._think,
        )

        tracer.event(
            source="coder", target="llm", kind="llm_request", model=self._model,
            content=user_msg,
            params={"model": self._model, "temperature": self._temperature,
                    "task_id": task_id},
        )

        # The banner is guarded like the streaming writes below, so a closed stdout cannot break generate()'s no-raise contract (BUGFIX, B1).
        try:
            _safe_print(f"\n💻 [LIVE CODER STREAMING — task: {task_id}]:")
        except OSError as exc:
            logger.warning("Coder: could not write streaming banner (%s) — continuing", exc)
        try:
            _coder_tokens: list[str] = []

            def _coder_on_token(t: str) -> None:
                # safe_stdout_write: a closed stdout must not look like a transport failure (B1).
                _safe_stdout_write(t)
                _coder_tokens.append(t)

            raw_text = _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=self._timeout,
                stream=True,
                on_token=_coder_on_token,
                api_format=self._api_format,
                ssl_context=self._ssl_context,
                **self._retry_kwargs,
            )
            raw_text = raw_text or "".join(_coder_tokens)
            _safe_print("\n" + "═" * 80 + "\n")
        except Exception as exc:
            # The call itself failed, so this is a transport failure, not a rejected answer (RUN-8).
            msg = f"LLM call failed: {exc}"
            logger.warning("coder.generate [%s]: %s", task_id, msg)
            tracer.event(
                source="coder", target="llm", kind="llm_response", model=self._model,
                content=f"[ERROR] {exc}", params={"task_id": task_id},
            )
            return CoderResult(task_id=task_id, error=msg, max_tokens=budget,
                               error_kind="transport")

        # ── Strip think blocks; check for missing-context signal ─────────────
        cleaned = strip_think(raw_text)
        # Pull-model: context requests are captured; creative output is prose, so the prose extractor is used (AUTO-CR-19-2).
        if self._task_mode == "creative":
            missing_ctx = self._extract_context_request_prose(cleaned)
        else:
            missing_ctx = self._extract_context_request(cleaned)

        # ── Context probe: if the LLM reported missing_context, fetch once ───
        _missing = self._extract_missing_context(cleaned)
        # Creative requests are pulled on the next attempt; the in-call search probe is for code symbols only.
        context_satisfied = not bool(_missing) and not (
            self._task_mode == "creative" and bool(missing_ctx)
        )

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
                    print(f"\n💻 [LIVE CODER STREAMING — context probe, task: {task_id}]:")
                    _probe_tokens: list[str] = []

                    def _probe_on_token(t: str) -> None:
                        _safe_stdout_write(t)
                        _probe_tokens.append(t)

                    raw_text = _llm_stream.request_completion(
                        url=url,
                        headers=headers,
                        payload=payload,
                        timeout=self._timeout,
                        stream=True,
                        on_token=_probe_on_token,
                        api_format=self._api_format,
                        ssl_context=self._ssl_context,
                        **self._retry_kwargs,
                    )
                    raw_text = raw_text or "".join(_probe_tokens)
                    _safe_print("\n" + "═" * 80 + "\n")
                    _probe_cleaned = strip_think(raw_text)
                    # A probe that succeeds but returns nothing must not discard the good first response (BUGFIX).
                    if _probe_cleaned.strip():
                        cleaned = _probe_cleaned
                    else:
                        logger.warning(
                            "coder.generate [%s]: context-probe second call "
                            "returned empty — using first response", task_id,
                        )
                    # Re-extract context requests from the probe's reply; context_satisfied keeps the first value so the rewriter is skipped.
                    if self._task_mode == "creative":
                        missing_ctx = self._extract_context_request_prose(cleaned)
                    else:
                        missing_ctx = self._extract_context_request(cleaned)
                except Exception as exc:
                    logger.warning(
                        "coder.generate [%s]: context-probe second call failed: %s "
                        "— using first response", task_id, exc,
                    )
                    # cleaned already holds the first response; carry on.

        tracer.event(
            source="llm", target="coder", kind="llm_response",
            content=cleaned, model=self._model, params={"task_id": task_id},
        )

        parsed_files, parse_error = self._parse_response(
            cleaned, task_id, task.get("target_files") or []
        )
        if parse_error:
            # Only a reply cut off by the budget climbs it; when it climbed, the feedback does not say shorten (RUN-4).
            _raised = self._raise_budget_after_truncation(task_id, budget, parse_error)
            if _raised and parse_error.startswith(_TRUNCATION_HEAD_PROSE):
                parse_error += (
                    f" Output budget raised to {self._task_max_tokens} tokens "
                    "for this attempt — finish the chapter, do not shorten it."
                )
            elif _raised:
                parse_error = parse_error.replace(
                    _TRUNCATION_ADVICE,
                    _TRUNCATION_RAISED_ADVICE.format(budget=self._task_max_tokens),
                )
            return CoderResult(
                task_id=task_id, error=parse_error,
                raw_response=cleaned, missing_context=missing_ctx,
                context_satisfied=context_satisfied,
                max_tokens=budget, budget_raised=_raised,
                error_kind="parse",
            )

        # A parseable reply records its budget, so the next task starts no lower (RUN-4).
        self._record_parseable_budget(budget)

        # ── AUTO-CR-18: reject chapter duplication BEFORE writing to disk ────
        if self._task_mode == "creative":
            dup_error = self._creative_duplication_error(
                parsed_files, base_dir, task.get("target_files") or []
            )
            if dup_error:
                logger.warning("coder.generate [%s]: %s", task_id, dup_error)
                return CoderResult(
                    task_id=task_id, error=dup_error, raw_response=cleaned,
                    missing_context=missing_ctx, context_satisfied=context_satisfied,
                    max_tokens=budget,
                    error_kind="parse",
                )

        # ── Write files to disk ───────────────────────────────────────────────
        target_files = task.get("target_files") or []
        allowed = frozenset(target_files) if target_files else None
        files_skipped: list[str] = []
        written, write_error = self._write_files(
            parsed_files, base_dir, task_id, allowed_paths=allowed,
            skipped_out=files_skipped,
        )
        # RUN-1: every returned path was outside target_files, so Guard 2 blocked all of them; nothing was written.
        if not written and not write_error and files_skipped:
            write_error = _guard2_all_skipped_error(files_skipped)
        if write_error and not written:
            return CoderResult(
                task_id=task_id, error=write_error, raw_response=cleaned,
                missing_context=missing_ctx, context_satisfied=context_satisfied,
                files_skipped=files_skipped,
                max_tokens=budget,
                error_kind="parse",
            )

        result = CoderResult(
            task_id=task_id,
            files_written=written,
            error=write_error,
            raw_response=cleaned,
            missing_context=missing_ctx,
            context_satisfied=context_satisfied,
            files_skipped=files_skipped,
            max_tokens=budget,
            # A partial write with an error is a parse failure, never a transport one.
            error_kind="parse" if write_error else "",
        )
        logger.info("coder.generate: %s", result.summary())
        return result

    # RUN-4: a cut-off whole-file rewrite is retried with a doubled budget, up to max_tokens_cap (Gate 1's ladder ported); state is in memory.

    def _budget_for_task(self, task_id: str) -> int:
        """The token budget the current ``generate()`` call goes out at.

        A retry of the SAME task keeps whatever the previous attempt set —
        that cursor IS the ladder (see :meth:`_raise_budget_after_truncation`).
        A new task starts at :meth:`_start_task_budget`. An empty ``task_id``
        always starts a fresh budget: with no id there is no "same task" to
        climb from, so the ladder would otherwise leak across tasks forever.
        """
        if task_id and task_id == self._task_id and self._task_max_tokens > 0:
            return int(self._task_max_tokens)
        budget = self._start_task_budget()
        self._task_id = task_id
        self._task_max_tokens = budget
        return budget

    def _start_task_budget(self) -> int:
        """The configured budget, or the largest one that already produced a
        parseable reply on this instance, whichever is bigger — Gate 1's
        ``_record_parseable_budget``, task-local. Never below the configured
        budget and never above the cap."""
        start = int(self._max_tokens)
        if self._learned_max_tokens is not None:
            start = max(start, int(self._learned_max_tokens))
        return min(start, int(self._max_tokens_cap))

    def _record_parseable_budget(self, max_tokens: int) -> None:
        """A reply parsed at *max_tokens* — the next task starts no lower."""
        self._learned_max_tokens = (
            int(max_tokens)
            if self._learned_max_tokens is None
            else max(int(self._learned_max_tokens), int(max_tokens))
        )

    def _raise_budget_after_truncation(
        self, task_id: str, used: int, parse_error: str,
    ) -> bool:
        """A reply cut off by the output budget: the next attempt of this task
        goes out at ``used × 2``, capped at [coder] max_tokens_cap.

        Only truncation climbs. The NO-JSON and ``JSON decode failed`` cases
        get the same budget again, because more tokens do not fix a reply with
        no JSON in it or one whose object is malformed — the same reasoning
        GATE1-LEARN-2 applied to empty replies.

        Returns ``True`` iff the budget actually rose. ``False`` at the cap is
        the one case where "shorten the file" is real advice, so the feedback
        keeps the existing sentence there.
        """
        if not parse_error.startswith(_TRUNCATION_HEADS):
            return False
        wanted = int(used) * _BUDGET_STEP_MULT
        capped = min(wanted, int(self._max_tokens_cap))
        if capped <= int(used):
            logger.warning(
                "coder [%s]: reply cut off at max_tokens=%d, which is already "
                "max_tokens_cap=%d — the budget cannot climb; raise "
                "[coder] max_tokens_cap or make the file smaller.",
                task_id, used, self._max_tokens_cap,
            )
            return False
        self._task_max_tokens = capped
        logger.info(
            "coder [%s]: reply cut off at max_tokens=%d — next attempt of this "
            "task raised to %d (cap %d).",
            task_id, used, capped, self._max_tokens_cap,
        )
        return True

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

        # Creative mode: continuity context (synopsis and previous chapter) replaces the generic file fill (AUTO-CR-4).
        if self._task_mode == "creative":
            file_contents = self._build_creative_file_contents(target_files, base_dir)
            # The language lock uses the story text: the run goal first, then the task, never structural labels (AUTO-CR-9/16, AUTO-FIX-5/9).
            from tools.auto.utils import resolve_creative_language, language_instruction
            _sample = (
                self._creative_language_sample(target_files, base_dir)
                or self._run_goal
                or task.get("goal", "")
                or task.get("instruction", "")
            )
            _lang = resolve_creative_language(
                getattr(self, "_config", None),
                _sample,
                task_mode="creative",
            )
            _lang_instr = language_instruction(_lang)
            if _lang_instr:
                file_contents = _lang_instr + "\n\n" + file_contents
                logger.info("Coder: creative language locked to %s", _lang)
        else:
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
            closing_instruction   = (
                # The closing instruction asks for prose; an existing target is revised in place (AUTO-CR-13/16).
                (
                    "Now output the COMPLETE revised text of the file(s) above, "
                    "applying ONLY the change the instruction requires and "
                    "preserving everything else (plot, names, wording, and the "
                    "ORIGINAL LANGUAGE). The reviewer's notes tell you WHAT to "
                    "improve — write the prose yourself to achieve it; do NOT "
                    "copy the reviewer's wording or summary sentences into the "
                    "chapter, and turn any exchange they describe into real "
                    "dialogue in your own words. Do NOT copy text from another chapter, "
                    "do NOT make two chapters identical, and do NOT change any "
                    "chapter number or heading — each chapter stays its own "
                    "distinct scene. For multiple files, wrap each in "
                    "<<<FILE: path>>> … <<<END>>>. Plain prose only — no JSON, "
                    "no commentary."
                    if self._task_mode == "creative"
                    and self._creative_is_edit(task.get("target_files") or [], base_dir)
                    else
                    "Write the complete chapter now as plain prose — no JSON, no "
                    "preamble, no code fences, nothing but the chapter text."
                )
                if self._task_mode == "creative" else
                "Produce the corrected files now. Return ONLY the JSON object "
                "described in the system prompt — nothing else."
            ),
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

    @staticmethod
    def _extract_context_request_prose(text: str) -> list[str]:
        """AUTO-CR-19-2: extract the names from a trailing ``CONTEXT_REQUEST:``
        line in a creative (non-JSON) response.

        Creative output is prose, so the JSON-based ``_extract_context_request``
        always returns ``[]`` and the pull channel was silently severed. This
        reads the same line ``_parse_response_prose`` strips from the chapter
        and returns the comma-separated names (e.g. ``["chapter_2", "chapter_3"]``).
        Fail-safe: ``[]`` when no such line is present.
        """
        names: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if line.upper().startswith("CONTEXT_REQUEST:"):
                payload = line.split(":", 1)[1]
                for part in payload.split(","):
                    name = part.strip().strip(".\"'`")
                    if name:
                        names.append(name)
        # Deduplicate, preserve order, cap (matches _extract_missing_context).
        return list(dict.fromkeys(names))[:8]

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
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            # Narrowed to the parse's real exceptions and logged at debug, so an unparseable reply is visible (BUGFIX).
            logger.debug(
                "_extract_missing_context: could not parse missing_context "
                "from LLM response: %s", exc,
            )
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
            from tools.search_agent import make_search_agent  # lazy import
            # Build the search agent through make_search_agent, so [search] and [api] settings such as verify_ssl apply (AUTO-BUG).
            agent = make_search_agent(self._config, str(base_dir))
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
                raw = abs_path.read_bytes()
            except OSError as exc:
                parts.append(f"### {rel}\n[unreadable: {exc}]")
                continue
            try:
                # HARDEN-5 D: the file's own codec, decided from its bytes by the
                # same helper /edit uses, applied strictly. A legacy byte has a
                # real character in that codec, so nothing has to be replaced —
                # and the Coder no longer feeds the model a run of U+FFFD that
                # it then copies straight back into the file.
                content = raw.decode(_file_read_encoding(abs_path))
            except UnicodeError:
                # Fail open: the file changed between the codec probe and the
                # read, or no candidate described its bytes. The old lossy read
                # is kept only as that fallback, so the task still gets its
                # context instead of none.
                content = raw.decode("utf-8", errors="replace")

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

    def _creative_is_edit(self, target_files: list[str], base_dir: Path) -> bool:
        """True when any target file already has non-empty content → this is an
        EDIT of existing prose, not generation of a new chapter."""
        for tf in target_files or []:
            try:
                if (base_dir / tf).read_text(encoding="utf-8", errors="replace").strip():
                    return True
            except OSError:
                continue
        return False

    def _creative_duplication_error(
        self, parsed_files: list[dict], base_dir: Path, target_files: list[str],
    ) -> str:
        """AUTO-CR-18: detect when an edit turned a chapter into a near-copy of
        another chapter (the "make identical" failure mode). Returns an
        actionable error string, or "" if all produced chapters stay distinct.

        AUTO-CR-18-2 (bugfix): the cross-file comparison below only catches
        duplication ACROSS differently-named files (chapter_2.txt vs
        chapter_3.txt). It cannot catch the single-file case — a changelog,
        FAQ, or any document with multiple `### `-headed entries in ONE
        file — because ``produced`` and ``chapters`` share the same key
        (the target file's own name), so ``oname == pname`` is always true
        and the comparison is skipped by construction. Observed live: a
        changelog task looped for 8 attempts across 7 rounds, each one
        APPENDING a new near-duplicate "### Greeting <verb> Hello world"
        entry (a synonym-swapped rewrite of the one before it) rather than
        writing ONE entry, because nothing ever rejected the growing file —
        it kept growing until the bloated, heavily-repetitive prompt made
        the Gate-2 validator itself degenerate (see AUTO-BUG-empty-response
        in inner_loop.py) and the run exhausted without ever surfacing the
        real problem. _split_entries below adds the missing WITHIN-file
        check: entries in the SAME file, split the same way the rest of
        this codebase already treats a changelog/entry document (`### `
        headings — see e.g. scripts/check_runbook.py's ``_ENTRY_RE``), are
        pairwise-compared exactly like cross-file chapters are.
        """
        import difflib

        try:
            thresh = float(self._config.get("coder", "dup_reject_ratio", fallback="0.92"))
        except (ValueError, TypeError):
            thresh = 0.92
        if thresh <= 0:
            return ""

        def _norm(s: str) -> str:
            return " ".join((s or "").split()).lower()

        # All chapters in play: the siblings on disk, overlaid with the new content for the targets.
        chapters: dict[str, str] = {}
        try:
            tgt0 = Path(target_files[0]) if target_files else Path(".")
            cdir = base_dir / tgt0.parent
            for pat in ("chapter_*.md", "chapter_*.txt", "chapter*.md", "chapter*.txt"):
                for p in cdir.glob(pat):
                    try:
                        chapters[p.name] = p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
        except OSError as exc:
            # Narrowed to OSError and logged: a failed directory listing is visible, not a silent skip (BUGFIX).
            logger.debug(
                "_creative_duplication_error: could not list candidate "
                "chapters under %s: %s", base_dir, exc,
            )

        produced: dict[str, str] = {}
        for f in parsed_files:
            name = Path(f.get("path", "")).name
            if name:
                produced[name] = f.get("content", "")
                chapters[name] = f.get("content", "")

        for pname, pcontent in produced.items():
            n1 = _norm(pcontent)
            if len(n1) < 50:
                continue
            for oname, ocontent in chapters.items():
                if oname == pname:
                    continue
                n2 = _norm(ocontent)
                if len(n2) < 50:
                    continue
                # autojunk=False: a synonym-swapped near-duplicate scores high instead of near zero (AUTO-CR-18-2).
                ratio = difflib.SequenceMatcher(None, n1, n2, autojunk=False).ratio()
                if ratio >= thresh:
                    return (
                        f"duplication detected: '{pname}' is {int(ratio * 100)}% "
                        f"identical to '{oname}'. Do NOT copy text between chapters "
                        f"or make them the same. Edit ONLY the specific detail the "
                        f"task names and keep '{pname}' a distinct scene with its "
                        f"own events and its own chapter heading."
                    )

        # Within-file check: near-duplicate "###" entries in one produced file are caught too (AUTO-CR-18-2).
        for pname, pcontent in produced.items():
            entries = _split_creative_entries(pcontent)
            if len(entries) < 2:
                continue
            for i in range(len(entries)):
                title_a, body_a = entries[i]
                n1 = _norm(body_a)
                if len(n1) < 50:
                    continue
                for j in range(i + 1, len(entries)):
                    title_b, body_b = entries[j]
                    n2 = _norm(body_b)
                    if len(n2) < 50:
                        continue
                    # autojunk=False, as in the cross-file comparison above (AUTO-CR-18-2).
                    ratio = difflib.SequenceMatcher(None, n1, n2, autojunk=False).ratio()
                    if ratio >= thresh:
                        return (
                            f"duplication detected: in '{pname}', entry "
                            f"'{title_a}' is {int(ratio * 100)}% identical to "
                            f"entry '{title_b}'. Do NOT add a new entry that "
                            f"restates an existing one with different words — "
                            f"either write ONE entry for this change, or make "
                            f"the new entry say something genuinely different. "
                            f"Do not copy or paraphrase text from another "
                            f"entry in this file."
                        )
        return ""

    def _creative_language_sample(self, target_files: list[str], base_dir: Path) -> str:
        """Raw story prose for language detection: the target files' current
        content, else the nearest preceding chapter. Excludes all English
        scaffolding/labels so a short non-English chapter is detected correctly.
        """
        chunks: list[str] = []
        for tf in target_files or []:
            try:
                t = (base_dir / tf).read_text(encoding="utf-8", errors="replace")
            except OSError:
                t = ""
            if t.strip():
                chunks.append(t)
        if chunks:
            return "\n".join(chunks)
        try:
            tgt = Path(target_files[0]) if target_files else None
            cdir = base_dir / (tgt.parent if tgt else Path("."))
            cands: list[Path] = []
            for pat in ("chapter_*.md", "chapter_*.txt", "chapter*.md", "chapter*.txt"):
                cands.extend(cdir.glob(pat))

            # Pick the latest chapter numerically, not by path string: chapter_10 is newer than chapter_9 (BUGFIX).
            def _chapter_num(path: Path) -> "int | None":
                m = re.search(r"chapter[_\-\s]?(\d+)", path.name, re.IGNORECASE)
                return int(m.group(1)) if m else None

            numbered = [(n, p) for p in cands if (n := _chapter_num(p)) is not None]
            ordered = (
                [p for _, p in sorted(numbered, key=lambda t: t[0], reverse=True)]
                if numbered
                else sorted(cands, reverse=True)
            )
            for p in ordered:
                if tgt is None or p.name != tgt.name:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    if txt.strip():
                        return txt
        except OSError as exc:
            # Narrowed to OSError and logged, so a failed read is visible instead of silently meaning no prior chapter (BUGFIX).
            logger.warning(
                "_creative_language_sample: could not read a candidate "
                "chapter under %s: %s", cdir, exc,
            )
        return ""

    def _build_creative_file_contents(
        self, target_files: list[str], base_dir: Path,
    ) -> str:
        """Creative-mode replacement for ``_read_file_contents`` (AUTO-CR-4/16).

        Assembles two things:
          1. **Current content of every target file** (AUTO-CR-16) — so an EDIT
             task ("fix inconsistencies") sees the text it must revise instead
             of inventing a brand-new chapter from scratch. Previously only the
             *previous* chapters were loaded and the target's own text never
             was, so editing chapter 1 (no predecessor) gave the model an empty
             context → it wrote an unrelated new chapter in the wrong language.
          2. **Story-so-far context** (synopsis + previous chapter) for
             continuity, via :class:`ContextAssembler`.

        Never raises: any failure degrades to a placeholder.
        """
        if not target_files:
            return "(no target files)"

        parts: list[str] = []

        # 1) Current content of ALL target files (not just the first).
        edit_blocks: list[str] = []
        for tf in target_files:
            try:
                content = (base_dir / tf).read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            if content.strip():
                edit_blocks.append(f"<<<FILE: {tf}>>>\n{content.rstrip()}\n<<<END>>>")
        if edit_blocks:
            parts.append(
                "FILES TO REVISE — current content (edit these in place; keep "
                "everything the instruction does not require changing, and keep "
                "the original language):\n" + "\n\n".join(edit_blocks)
            )

        # 2) Story-so-far continuity context (previous chapters + synopsis).
        target_file = target_files[0]
        try:
            target_rel = Path(target_file)
            chapter_dir = base_dir / target_rel.parent

            all_chapter_files: list[str] = []
            if chapter_dir.is_dir():
                # AUTO-CR-12: match prose chapters by BOTH common extensions.
                _seen: set[str] = set()
                for pat in ("chapter_*.md", "chapter_*.txt",
                            "chapter*.md", "chapter*.txt"):
                    for p in sorted(chapter_dir.glob(pat)):
                        rel = (
                            p.name if target_rel.parent in (Path("."), Path(""))
                            else str(target_rel.parent / p.name)
                        )
                        if rel not in target_files and rel not in _seen:
                            _seen.add(rel)
                            all_chapter_files.append(rel)

            assembler = ContextAssembler(
                num_ctx=self._num_ctx, max_tokens=self._max_tokens, base_dir=base_dir,
            )
            context = assembler.build_creative_context(target_file, all_chapter_files)
        except Exception as exc:
            logger.warning(
                "coder: ContextAssembler failed for %s: %s — proceeding with no "
                "prior-chapter context.", target_file, exc,
            )
            context = ""

        # The continuity block is added only when it has real prior content.
        if context and "no prior chapters" not in context:
            parts.append("STORY SO FAR (for continuity):\n" + context)

        if parts:
            return "\n\n".join(parts)
        return "(new chapter — no prior content to continue from)"

    @staticmethod
    def _is_truthy_delete(value) -> bool:
        """Accept truthy variants of a ``delete`` flag from the model.

        JSON ``true`` is the canonical form, but models also emit ``"true"``,
        ``"True"``, ``"TRUE"``, ``"yes"``, ``"Yes"``, and the integer ``1``.
        Every one of those should trigger the delete branch; anything else
        (``false``, ``"false"``, ``0``, ``None``, arbitrary strings) must
        fall through to the content branch.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes")
        return False

    def _parse_response(
        self, text: str, task_id: str,
        target_files: "list[str] | None" = None,
    ) -> tuple[list[dict], str]:
        """Parse the LLM response into a list of ``{path, content}`` dicts.

        In ``creative`` mode delegates to :meth:`_parse_response_prose` which
        is line-oriented and fail-open.  In ``code`` / ``docs`` modes uses the
        existing strict JSON path (byte-for-byte unchanged behaviour).

        Returns
        -------
        (parsed_files, error_message)
            ``parsed_files`` is a non-empty list on success; ``error_message``
            is a non-empty string on failure.
        """
        if self._task_mode == "creative":
            return self._parse_response_prose(text, task_id, target_files or [])

        stripped = _strip_outer_fence(text)

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # Three failure modes get three prescriptions: no JSON at all, JSON cut off by the budget, or malformed JSON (codeapp-sim fix).
            if "{" not in stripped:
                msg = (
                    "LLM response contained NO JSON at all — it looks like "
                    "prose or a clarifying question. Do NOT ask questions and "
                    "do NOT explain: reply with ONLY the JSON object "
                    '{"files": [{"path": ..., "content": ...}]} containing the '
                    "complete revised file(s). Make reasonable assumptions for "
                    "anything unspecified. "
                    f"(reply started with: {stripped[:120]!r})"
                )
            else:
                truncated = (
                    "Unterminated string" in str(exc)
                    or "Expecting" in str(exc)
                ) and not stripped.rstrip().endswith("}")
                if truncated:
                    # The truncation head is the stable marker that the budget-climb advice keys on (RUN-4).
                    msg = (_TRUNCATION_HEAD
                           + " — the revised file was too long for the output "
                             "token budget. "
                           + _TRUNCATION_ADVICE
                           + f"(decode error: {exc})")
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
            path    = str(item.get("path") or "").strip()
            content = item.get("content")
            if not path:
                logger.warning(
                    "coder._parse_response [%s]: item %d missing 'path' — skipped",
                    task_id, i,
                )
                continue
            # A delete flag marks a file for removal; truthy variants such as "true" or 1 are accepted, "false" is not (BUGFIX A5).
            if _is_truthy_delete(item.get("delete")):
                parsed.append({"path": path, "delete": True})
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

    def _parse_response_prose(
        self,
        text: str,
        task_id: str,
        target_files: list[str],
    ) -> tuple[list[dict], str]:
        """Fail-open prose parser for ``task_mode='creative'``.

        Protocol (line-oriented, never requires JSON):

        * Strip outer code fences (reuses :func:`_strip_outer_fence`).
        * Extract and remove a trailing ``CONTEXT_REQUEST:`` line if present;
          callers read context_request from :meth:`_extract_context_request`
          but this method also strips it from the written content.
        * **Single target file:** treat the entire cleaned body as that file's
          content.
        * **Multiple target files:** split on ``<<<FILE: relpath>>>`` /
          ``<<<END>>>`` markers.  Text outside markers is ignored.  If no
          markers are found but the body is non-empty and there is exactly one
          target file, fall back to single-file behaviour.
        * **Truncation guard:** if the body ends mid-sentence AND its length is
          within 5 % of ``self._max_tokens * 4`` (chars), surface an actionable
          retry message instead of silently writing a truncated chapter.
        * Returns ``([], error)`` **only** when the body is empty/whitespace
          after stripping — that is the single legitimate failure mode.

        Returns
        -------
        (parsed_files, error_message)
        """
        # ── Strip outer fences (```markdown, ```md, ``` …) ─────────────────
        body = _strip_outer_fence(text)

        # Salvage prose from a JSON wrapper: a small model sometimes wraps the chapter in JSON (AUTO-CR-13).
        _salvaged = _extract_prose_from_json(body)
        if _salvaged is not None:
            logger.info(
                "coder._parse_response_prose [%s]: model returned a JSON wrapper "
                "— extracted chapter prose from it.", task_id,
            )
            body = _salvaged

        # Strip every CONTEXT_REQUEST line, not just those in the last three lines (BUGFIX).
        lines = body.splitlines()
        kept_lines = [
            ln for ln in lines if not ln.strip().upper().startswith("CONTEXT_REQUEST:")
        ]
        if len(kept_lines) != len(lines):
            body = "\n".join(kept_lines)

        body = body.strip()

        # ── Empty-body failure (the only real failure mode) ─────────────────
        if not body:
            msg = "creative coder: LLM returned empty body — no chapter content to write"
            logger.warning("coder._parse_response_prose [%s]: %s", task_id, msg)
            return [], msg

        # Refusal guard: a model refusal or empty stub fails the attempt for a retry; genuine prose is never rejected (AUTO-CR-12).
        if _looks_like_refusal(body):
            msg = ("creative coder: response looks like a refusal / meta-reply, "
                   "not chapter prose — retrying")
            logger.warning("coder._parse_response_prose [%s]: %s", task_id, msg)
            return [], msg

        # Truncation guard in the language's own chars per token, so it also fires on Russian text.
        from tools.auto.utils import chars_per_token
        # Measured against the budget this attempt actually got, which grows after a truncation climb (RUN-4).
        char_budget = self._task_max_tokens * chars_per_token(body)
        last_char = body[-1] if body else ""
        ends_mid_sentence = last_char not in {".", "!", "?", '"', "'", "\n", "…"}
        if ends_mid_sentence and len(body) >= char_budget * 0.95:
            msg = (
                _TRUNCATION_HEAD_PROSE + " — "
                "raise [coder] max_tokens or shorten the chapter target. "
                f"(body length {len(body)} chars ≈ budget {char_budget:.0f} chars)"
            )
            logger.warning("coder._parse_response_prose [%s]: %s", task_id, msg)
            return [], msg

        # ── Multi-file marker split ─────────────────────────────────────────
        _FILE_OPEN  = re.compile(r"^<<<FILE:\s*(.+?)\s*>>>$", re.MULTILINE)
        _FILE_CLOSE = re.compile(r"^<<<END>>>$", re.MULTILINE)

        open_matches = list(_FILE_OPEN.finditer(body))
        # Marker extraction runs whenever markers are present, for single- and multi-file tasks (FIX).
        if open_matches:
            parsed: list[dict] = []
            for i, m_open in enumerate(open_matches):
                relpath = m_open.group(1).strip()
                content_start = m_open.end()
                next_open = open_matches[i + 1] if i + 1 < len(open_matches) else None
                boundary = next_open.start() if next_open else len(body)
                # A missing closer must not match a later file's marker; the closer is searched only before the next opener (BUGFIX).
                close_match = _FILE_CLOSE.search(body, content_start, boundary)
                content_end = close_match.start() if close_match else boundary
                chunk = body[content_start:content_end].strip()
                chunk = re.sub(r"\s*<<<END>>>\s*$", "", chunk)
                if chunk:
                    parsed.append({"path": relpath, "content": chunk + "\n"})
                else:
                    logger.debug(
                        "coder._parse_response_prose [%s]: marker %r had empty body — skipped",
                        task_id, relpath,
                    )
            if parsed:
                return parsed, ""
            # Markers present but all bodies empty — fall through to single-file.
            logger.warning(
                "coder._parse_response_prose [%s]: FILE markers produced no content "
                "— falling back to single-file mode", task_id,
            )

        # ── Single-file (default) ────────────────────────────────────────────
        if target_files:
            relpath = target_files[0]
        else:
            # No target declared — synthesise a sensible default name.
            relpath = "chapter.md"

        return [{"path": relpath, "content": body + "\n"}], ""

    # ── Content safety ────────────────────────────────────────────────────────

    # Patterns that must never appear in generated file content, keyed by a label (defence in depth).

    # Catastrophic payload patterns, checked in every task mode, since no document needs them.
    _BLOCKED_ALWAYS: tuple[tuple[str, str], ...] = (
        ("fork bomb shell",     ":|:&"),
        ("fork bomb py",        "os.fork()"),
    )

    # Patterns checked only in code mode; they false-positive in docs and creative prose.
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

    # System directories that generated code must never write to (temp dirs are allowed); both quote forms are checked.
    _DANGEROUS_WRITE_PREFIXES: tuple[str, ...] = (
        "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/",
        "/proc/", "/sys/", "/root/", "/lib/",
    )

    # Whole-word patterns (sudo, shutdown, reboot), code mode only: identifiers such as test_reboot_x do not match.
    _BLOCKED_CODE_WORD_BOUNDARY: tuple[tuple[str, str], ...] = (
        ("sudo invocation",     "sudo"),
        ("shutdown cmd",        "shutdown"),
        ("reboot cmd",          "reboot"),
    )

    # subprocess is legitimate in generated files, so it is flagged only with a shell-deletion token.
    _SUBPROCESS_DANGER_TOKENS: tuple[str, ...] = (
        "rm ", "rm\t", '"rm"', "'rm'",  # rm with space, tab, or as a quoted list arg
        "rmdir", "rm -rf", "shutil.rmtree",
        "dd ", "mkfs",
        "sudo ", '"sudo"', "'sudo'",    # privilege escalation in any invocation form
        "shutdown", "reboot",
    )

    @classmethod
    def _check_content_safety(cls, content: str, task_mode: str = "code",
                              existing_content: "str | None" = None) -> tuple[bool, str]:
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

        # Grandfathering: a file that already contains a flagged pattern may be edited, but introducing one is still blocked (selfhost pilot).
        _existing_lower = (existing_content or "").lower()
        _existing_lines = _existing_lower.splitlines()

        def _preexisting(token: str) -> bool:
            return bool(_existing_lower) and token.lower() in _existing_lower

        def _strip_py_line_comment(line: str) -> str:
            """Strip everything from the first unquoted # to end of line.
            Prevents comment text (e.g. the explanation comment inside this
            very method) from triggering the same-line co-occurrence check
            in _preexisting_combo — a comment about a dangerous pattern is
            not the same as real code that uses it.

            BUGFIX: a backslash-escaped quote inside a string (e.g. the `\\"`
            in `x = "say \\"hi\\""  # comment`) was counted as a real
            delimiter, toggling in_single/in_double the same as an
            unescaped one. An ODD number of such quote characters before
            the actual `#` desyncs the tracked state, so the scanner
            believes it is still "inside a string" right when the real
            comment starts — and returns the line completely unstripped,
            comment and all. That silently reopens the exact hole this
            helper was written to close: comment text (which can freely
            mention two dangerous tokens together, e.g. explaining why NOT
            to combine them) counts toward the same-line co-occurrence
            check again, for any existing line shaped this way. Track a
            pending backslash so an escaped quote toggles nothing.
            """
            in_single = in_double = False
            escaped = False
            for i, ch in enumerate(line):
                if escaped:
                    escaped = False
                    continue
                if ch == "\\" and (in_single or in_double):
                    escaped = True
                elif ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
                elif ch == '#' and not in_single and not in_double:
                    return line[:i]
            return line

        def _preexisting_combo(token_a: str, token_b: str) -> bool:
            """True if *token_a* and *token_b* already co-occur on the same
            line somewhere in the old file — not just each independently
            appearing anywhere in it.

            AUTO-BUG (follow-up): `_preexisting(danger) and
            _preexisting("subprocess")` still passes for a file where the
            two tokens never actually appeared together — e.g. `import
            subprocess  # for git commands` on one line and a plain-English
            `# never run rm -rf here` warning on another, unrelated line.
            Neither mention is dangerous on its own, but together they were
            enough to grandfather in a brand-new, genuinely dangerous
            subprocess+rm-rf call introduced by this edit. Requiring
            same-line co-occurrence is a reasonable, cheap proxy for "this
            was already a real combined usage" without needing a full
            parser: actual dangerous calls are written on one line
            (`subprocess.run(["rm", "-rf", path])`), while incidental
            separate mentions almost never share a line by chance.
            """
            token_a, token_b = token_a.lower(), token_b.lower()
            return any(
                token_a in code and token_b in code
                for code in (_strip_py_line_comment(ln) for ln in _existing_lines)
            )

        for label, pattern in active_patterns:
            pat_lower = pattern.lower()

            # subprocess alone is allowed; it is blocked only with a shell-deletion token in the file.
            if pat_lower == "subprocess":
                if "subprocess" in lower:
                    for danger in cls._SUBPROCESS_DANGER_TOKENS:
                        if danger.lower() in lower:
                            # A pattern is grandfathered only if its compound parts co-occur on one line in the old file (BUGFIX).
                            if _preexisting_combo("subprocess", danger):
                                logger.info(
                                    "coder._check_content_safety: subprocess+%r "
                                    "pre-exists in the target file — edit "
                                    "permitted (grandfathered).", danger,
                                )
                                continue
                            return (
                                False,
                                f"blocked content: subprocess combined with "
                                f"dangerous token {danger!r} ({label})",
                            )
                continue

            # open() root writes are checked for both quote styles, limited to dangerous system paths.
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
                if _preexisting(pat_lower):
                    logger.info(
                        "coder._check_content_safety: pattern %r pre-exists in "
                        "the target file — edit permitted (grandfathered).",
                        pattern,
                    )
                    continue
                return False, f"blocked content pattern {pattern!r} ({label})"

        # Whole-word patterns apply in code mode; identifiers containing the keyword are not matched.
        if task_mode == "code":
            import re as _re
            for label, pattern in cls._BLOCKED_CODE_WORD_BOUNDARY:
                keyword = pattern.lower().rstrip()
                if _re.search(r"\b" + _re.escape(keyword) + r"\b", lower):
                    if _preexisting(keyword):
                        logger.info(
                            "coder._check_content_safety: word-boundary pattern "
                            "%r pre-exists in the target file — edit permitted "
                            "(grandfathered).", pattern,
                        )
                        continue
                    return False, f"blocked content pattern {pattern!r} ({label})"

        return True, ""

    @staticmethod
    def _safe_dest(base_dir: Path, rel: str) -> tuple["Path | None", str]:
        """Resolve *rel* relative to *base_dir* and verify it stays inside.

        Prevents path traversal (``../../etc/passwd``) and absolute-path
        injection from LLM responses.  Returns ``(resolved_path, "")`` on
        success or ``(None, error_message)`` on violation.

        Resolves both sides before comparing, so a caller passing an
        unresolved base_dir (e.g. a symlinked workspace) doesn't get a
        false-positive rejection on every legitimate write.
        """
        # Reject obviously absolute paths before Path() normalises them.
        if rel.startswith("/") or (len(rel) > 1 and rel[1:3] == ":\\"):
            return None, f"rejected absolute path from LLM: {rel!r}"
        try:
            base_resolved = base_dir.resolve()
            dest = (base_dir / rel).resolve()
        except (OSError, ValueError) as exc:
            return None, f"path resolution error for {rel!r}: {exc}"
        try:
            inside = dest.relative_to(base_resolved)
        except ValueError:
            return None, f"path escapes base_dir: {rel!r} → {dest}"
        if ".git" in inside.parts:
            return None, f"rejected a path inside .git: {rel!r}"
        return dest, ""

    def _write_files(
        self,
        parsed_files: list[dict],
        base_dir: Path,
        task_id: str,
        allowed_paths: "frozenset[str] | None" = None,
        skipped_out: "list[str] | None" = None,
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
        skipped_out:
            Optional list that receives the relative paths dropped by the
            ``target_files`` guard.  Pass ``None`` to keep the legacy
            two-tuple-only behaviour (nothing is recorded).  RUN-1: these
            skips are protection, not a verdict, so they never set
            *first_error_message* — a skipped write and a skipped delete are
            treated alike (neither touches the file).

        Returns
        -------
        (written_paths, first_error_message)
            *written_paths* contains every path successfully written even when
            a later file errors.  *first_error_message* is ``""`` when all
            writes succeed — and also when the only thing that happened was a
            ``target_files`` skip (see *skipped_out*).
        """
        import shutil       # .coder.bak copies (a module-level import moved every pinned line)
        written: list[str] = []
        first_error = ""

        for item in parsed_files:
            rel = item["path"]

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
                # Only a literal leading ./ is stripped, so a dotfile such as .notes.md is never confused with notes.md.
                def _norm_target(p: str) -> str:
                    p = p.replace("\\", "/")
                    while p.startswith("./"):
                        p = p[2:]
                    return p

                norm = _norm_target(rel)
                allowed_norm = {_norm_target(p) for p in allowed_paths}
                if norm not in allowed_norm:
                    msg = _guard2_skip_message(rel)
                    logger.warning("coder._write_files [%s]: %s", task_id, msg)
                    # A Guard-2 skip is protection, not a failure: nothing was written, so it is recorded instead of failing the attempt (RUN-1).
                    if skipped_out is not None:
                        skipped_out.append(rel)
                    continue

            # Delete flag: same guards as writes; the original is backed up to .coder.bak; deleting an absent file is a no-op (pullrun-sim).
            if _is_truthy_delete(item.get("delete")):
                try:
                    if dest.exists():
                        backup = dest.with_suffix(dest.suffix + ".coder.bak")
                        # bytes and mode: read as utf-8 with errors=replace, a latin-1 \xe9 came back
                        # as U+FFFD, and a BOM + CRLF file as BOM + LF — no backup of the original
                        backup.unlink(missing_ok=True)    # copy2 keeps a read-only mode: it could not overwrite
                        shutil.copy2(dest, backup)
                        dest.unlink()
                        logger.info(
                            "coder._write_files [%s]: DELETED %s (backup: %s)",
                            task_id, rel, backup.name,
                        )
                    else:
                        logger.info(
                            "coder._write_files [%s]: delete of %s — already "
                            "absent, treated as success", task_id, rel,
                        )
                    written.append(rel)
                except OSError as exc:
                    msg = f"delete failed for {rel}: {exc}"
                    logger.error("coder._write_files [%s]: %s", task_id, msg)
                    if not first_error:
                        first_error = msg
                continue

            content = item["content"]

            # Guard 2.5: tokenise and reject non-ASCII identifiers in code mode, so the retry names what to rename (pullrun-sim).
            if (
                self._ascii_identifiers_only
                and self._task_mode == "code"
                and rel.endswith(".py")
            ):
                bad_idents = _find_non_ascii_identifiers(content)
                if bad_idents:
                    msg = (
                        f"[STYLE] non-ASCII identifiers in {rel!r}: "
                        f"{', '.join(sorted(bad_idents)[:8])} — rename all "
                        f"identifiers to English/ASCII (strings, comments and "
                        f"docstrings may stay in any language). "
                        f"Set [coder] ascii_identifiers_only = false to allow."
                    )
                    logger.warning("coder._write_files [%s]: %s", task_id, msg)
                    if not first_error:
                        first_error = msg
                    continue

            # Guard 2.7: the model was shown a file that is not UTF-8 with U+FFFD for each byte it could not
            # read; writing that text back replaced every such byte for good (a latin-1 caf\xe9 became EF BF BD).
            try:
                if "\ufffd" in content and dest.is_file():
                    dest.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                msg = f"[SAFETY] {rel!r} is not UTF-8 — the edit would replace its undecodable bytes; write blocked"
                logger.error("coder._write_files [%s]: %s", task_id, msg)
                if not first_error:
                    first_error = msg
                continue
            except OSError:
                pass

            # ── Guard 3: scan file content for dangerous patterns ──────────
            _existing = None
            if self._allow_preexisting_patterns and dest.exists():
                try:
                    _existing = dest.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    _existing = None
            content_safe, content_reason = self._check_content_safety(
                content, self._task_mode, existing_content=_existing)
            if not content_safe:
                msg = f"[SAFETY] {content_reason} in file {rel!r} — write blocked"
                logger.error("coder._write_files [%s]: %s", task_id, msg)
                if not first_error:
                    first_error = msg
                continue

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Keep the existing file's permission bits when rewriting it.
                existing_mode: "int | None" = None
                encoding = "utf-8"

                # Back up existing file before overwriting (reversible).
                if dest.exists():
                    try:
                        existing_mode = stat.S_IMODE(dest.stat().st_mode)
                    except OSError:
                        existing_mode = None

                    # HARDEN-5 D: write the reply back in the file's own codec,
                    # decided from its bytes by the same helper /edit uses, so
                    # the bytes outside the reply are the original bytes and a
                    # legacy file is not silently re-encoded as UTF-8. Walking
                    # the candidate list in order accepts a revision that only
                    # the file's looser codec holds; a reply none of them holds
                    # is refused rather than written as ? or U+FFFD, which the
                    # model would then read back and happily keep. in_style_of
                    # runs first, so the codec is the one that holds exactly the
                    # text about to be written.
                    # The model was shown the file with its secrets masked: a line it
                    # gave back masked is the original line again, not the placeholder.
                    try:
                        content = _llm_stream.restore_masked_secrets(
                            dest.read_bytes().decode("utf-8", errors="replace"), content)
                    except OSError:
                        pass
                    content = in_style_of(dest, content)
                    encoding, missing = _pick_file_encoding(
                        _file_encodings(dest), content
                    )
                    refusal = _refuse_inencodable(encoding, missing, rel)
                    if refusal:
                        logger.warning("coder._write_files [%s]: %s", task_id, refusal)
                        if not first_error:
                            first_error = refusal
                        continue
                    logger.debug(
                        "coder._write_files [%s]: writing %s in %s",
                        task_id, dest, encoding,
                    )

                    backup = dest.with_suffix(dest.suffix + ".coder.bak")
                    backup.unlink(missing_ok=True)
                    shutil.copy2(dest, backup)        # bytes and mode (see the delete branch)
                    logger.debug(
                        "coder._write_files [%s]: backed up %s → %s",
                        task_id, dest, backup,
                    )

                # Write atomically and keep the file's permission bits: a kill mid-write must not truncate the user's source file (BUGFIX).
                atomic_write_text(dest, content, newline="", encoding=encoding)
                if existing_mode is not None:
                    try:
                        os.chmod(dest, existing_mode)
                    except OSError:
                        pass

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


# Entry splitting for the within-file duplication check (AUTO-CR-18-2)

#: The "### Heading" marker, kept in sync with check_runbook's entry regex.
_CREATIVE_ENTRY_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)


def _split_creative_entries(text: str) -> list[tuple[str, str]]:
    """Split *text* into ``(title, body)`` pairs at each ``### `` heading.

    Returns ``[]`` when the document has fewer than two such headings (a
    single-scene chapter with no internal structure, or an empty/malformed
    file) — the within-file duplication check only makes sense once there
    are at least two entries to compare against each other.
    """
    matches = list(_CREATIVE_ENTRY_RE.finditer(text or ""))
    if len(matches) < 2:
        return []
    entries: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((title, text[start:end]))
    return entries


# Symbol-aware chunking (SCTX Task 1)

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

    # Leading import and preamble lines across the supported languages; unsupported languages get an empty block.
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
        # Best-effort top-level symbol scan for brace- and indent-based languages.
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
        # JS/TS arrow-function assignments at statement level count as symbols.
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


# Helpers

_REFUSAL_MARKERS: tuple[str, ...] = (
    "i cannot fulfill", "i can't fulfill", "i cannot assist", "i can't assist",
    "i cannot help", "i'm unable to", "i am unable to", "i cannot generate",
    "i don't see any", "i do not see any", "please provide more context",
    "provide more context", "clarify what", "as an ai", "i need more context",
    "there's a misunderstanding", "there is a misunderstanding",
    "seem to be related to a programming task",
)


def _extract_prose_from_json(body: str) -> "str | None":
    """If *body* is a JSON wrapper the model emitted instead of raw prose,
    extract the chapter text from it. Returns the prose string, or ``None``
    when *body* is not such a wrapper (so the caller treats it as plain prose).

    Handles the shapes small models actually produce:
      {"files":[{"content": "..."}]}
      {"tasks":[{"files":[{"content": "..."}]}]}
      {"content": "..."}  /  {"target_files":[{"content": "..."}]}
    The ``content``/``text`` key inside a file object may be named ``content``
    or ``text``; the file path key is ignored.
    """
    s = body.strip()
    if not (s.startswith("{") or s.startswith("[")):
        return None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None

    def _content_of(obj) -> "str | None":
        if isinstance(obj, dict):
            for k in ("content", "text", "body", "prose"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        return None

    def _files_of(obj):
        if isinstance(obj, dict):
            for k in ("files", "target_files"):
                v = obj.get(k)
                if isinstance(v, list):
                    return v
        return None

    # Top-level content
    top = _content_of(data) if isinstance(data, dict) else None
    if top:
        return top

    # files[] at top level, or nested under tasks[]
    candidate_files = _files_of(data) if isinstance(data, dict) else None
    if candidate_files is None and isinstance(data, dict):
        tasks = data.get("tasks")
        if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
            candidate_files = _files_of(tasks[0])
    if isinstance(candidate_files, list):
        chunks = [c for c in (_content_of(f) for f in candidate_files) if c]
        if chunks:
            return "\n\n".join(chunks)
    return None


def _looks_like_refusal(body: str) -> bool:
    """Heuristic: True when *body* is a refusal / meta-reply, not chapter prose.

    Deliberately conservative to avoid rejecting real fiction:
      * an empty JSON stub (``{}`` / ``[]``) is always a refusal/no-op;
      * otherwise the body must be SHORT (< 600 chars) AND contain an explicit
        refusal marker. Long prose is never tripped even if it quotes such a
        phrase in dialogue.
    """
    stripped = body.strip()
    if stripped in ("{}", "[]"):
        return True
    if len(stripped) >= 600:
        return False
    low = stripped.lower()
    return any(marker in low for marker in _REFUSAL_MARKERS)


def _find_non_ascii_identifiers(content: str) -> set:
    """Return NAME tokens containing non-ASCII characters (PEP 3131 idents).

    Strings, comments and docstrings are naturally excluded because only
    NAME tokens are inspected. Falls back to an empty set on tokenize
    errors — the syntax problem will surface via execution anyway.
    """
    import io
    import tokenize

    bad: set = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(content).readline):
            if tok.type == tokenize.NAME and not tok.string.isascii():
                bad.add(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return set()
    return bad


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
        # Drop the opening fence line; a bare fence with no content is returned as-is so json.loads fails cleanly.
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

    BUGFIX: stripping fired on a leading "```" with no matching closer,
    deleting the real first line of e.g. a markdown file that opens with a
    fenced snippet. Requires a genuine whole-body wrap now.
    """
    t = text.strip()
    if t.startswith("```"):
        if "\n" not in t:
            t = ""
        else:
            rest = t.split("\n", 1)[1]
            if rest.rstrip().endswith("```"):
                t = rest.rstrip()[:-3]
    return t.rstrip("\n") + "\n"


# Convenience factory

def make_coder(config: configparser.ConfigParser, task_mode: str = "code",
               run_goal: str = "") -> Coder:
    """Create a :class:`Coder` from *config* (agents.ini).

    Reads the active API section (``[api_local]`` / ``[api_remote]``) using
    the same convention as every other agent in this codebase.

    Parameters
    ----------
    config:
        A ``ConfigParser`` instance loaded from ``agents.ini``.
    run_goal:
        AUTO-FIX-9: the raw --auto GOAL string, forwarded so a chapter-1
        cold start can detect the story's language from the user's own text
        instead of the architect-authored (and English-prone) instruction.

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
    verify_ssl = safe_getboolean(config, "api", "verify_ssl", fallback=True)

    return Coder(
        config     = config,
        base_url   = base_url,
        api_key    = api_key,
        model      = model,
        api_format = api_fmt,
        verify_ssl = verify_ssl,
        task_mode  = task_mode,
        run_goal   = run_goal,
    )
