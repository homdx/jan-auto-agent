"""tools/collect/summarizer.py — COLLECT-16: Summarizer (Pass B).

One *bounded* LLM call per module, reusing the same request-building/
network plumbing the rest of the pipeline already uses
(``tools.llm_stream.build_chat_request`` / ``request_completion``) and the
same retry/resume machinery (``tools.backoff``). Nothing here re-derives a
structural fact: the prompt's "Structural facts" block is Pass A's own
output rendered as text, and the model is asked only to *summarize*, not
to re-discover.

Antihallucination contract (this module's half of it)
-------------------------------------------------------
Pass B's only sanctioned side effect is calling ``ModuleRecord.with_llm_summary``
with an ``LLMSummary`` — the whitelist type COLLECT-1 defines with exactly
two fields, ``purpose`` and ``notes``. There is no code path here that
touches ``public_symbols``, ``config_reads``, ``except_sites``,
``guarded_accesses``, or any other structural field: ``with_llm_summary``
uses ``dataclasses.replace`` (frozen dataclass, no setters) and only ever
writes the ``summary`` slot. A malformed or hostile-looking LLM reply can
at worst produce a wrong/empty *summary* — never a wrong structural fact —
because there is no field for it to overwrite.

Pass C (COLLECT-17, ``verifier.py``) is the second half of the guarantee:
it citation-checks and contradiction-suppresses whatever prose survives
Pass B before it reaches the artifact. This module does not attempt that
check itself — it only refuses to ever *write* outside the whitelist.

``--no-llm`` (CLI, COLLECT-19) is simply "don't call anything in this
module" — a purely structural artifact is just Pass A's output with no
``summary`` attached, which is already every ``ModuleRecord``'s default
state.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from tools.collect.model import LLMSummary, ModuleRecord

logger = logging.getLogger(__name__)

# (system, user) -> response_text — same shape as summary_memory.LlmCall,
# so any existing `_make_llm_call`-style factory can be used interchangeably.
LlmCall = Callable[[str, str], str]

DEFAULT_NUM_CTX = 4096
DEFAULT_MAX_TOKENS = 400
DEFAULT_MAX_RETRIES = 3
_PROMPT_OVERHEAD_TOKENS = 300  # rough system-prompt + facts-block scaffolding
_CHARS_PER_TOKEN = 4.0

SYSTEM_PROMPT = (
    "You are a codebase summarizer. You are given already-verified "
    "structural facts about one Python module (extracted by static AST "
    "analysis, not by you) and the module's source code. Write a short, "
    "accurate summary of what the module is for.\n"
    "Respond with ONLY a JSON object of the form "
    '{"purpose": "<one or two sentence purpose>", "notes": "<optional short notes>"}. '
    "The structural facts you are given are ground truth — never contradict "
    "them, never restate them as if you discovered them yourself, and never "
    "invent function names, line numbers, or behavior that is not present "
    "in the facts or the source. If you are unsure about something, leave "
    "it out rather than guessing. Do not include markdown code fences or "
    "any text outside the single JSON object."
)


# ── prompt construction ────────────────────────────────────────────────────────


def _facts_block(module: ModuleRecord) -> str:
    """Render Pass A's facts for `module` as compact, unambiguous text.

    This is the entire "input" side of the antihallucination contract for
    Pass B: everything structural the model could possibly need is already
    here, spelled out, so there's no reason for it to guess.
    """
    lines: List[str] = [f"path: {module.path}"]
    if module.parse_error:
        lines.append(f"parse_error: {module.parse_error}")
    if module.public_symbols:
        lines.append("public_symbols:")
        for s in module.public_symbols:
            doc = f" — {s.docstring_first_line}" if s.docstring_first_line else ""
            lines.append(f"  - {s.qualname}{s.signature}{doc}")
    if module.imports:
        lines.append("imports: " + ", ".join(module.imports))
    if module.config_reads:
        lines.append("config_reads:")
        for c in module.config_reads:
            lines.append(f"  - [{c.section}].{c.key} (fallback={c.fallback!r})")
    if module.except_sites:
        lines.append("except_sites:")
        for e in module.except_sites:
            flag = " FAIL-OPEN" if e.is_fail_open else ""
            lines.append(f"  - {e.location} {e.exception_type} -> {e.body_kind}{flag}")
    if module.guarded_accesses:
        lines.append("guarded_accesses:")
        for g in module.guarded_accesses:
            guard = f" ({g.guard})" if g.guard else ""
            lines.append(f"  - {g.location} {g.access}: {g.status}{guard}")
    return "\n".join(lines)


def _budget_chars(num_ctx: int, max_tokens: int,
                   chars_per_token: float = _CHARS_PER_TOKEN) -> int:
    """Char budget for the source excerpt.

    Mirrors `context_assembler`'s convention: reserve `max_tokens` for the
    reply and a fixed overhead for the system prompt + facts block, convert
    what's left to characters. `num_ctx=0` (server-default context) is
    treated as `DEFAULT_NUM_CTX` rather than producing a budget of 0/negative.
    """
    ctx = int(num_ctx) if num_ctx else DEFAULT_NUM_CTX
    budget_tokens = max(ctx - int(max_tokens) - _PROMPT_OVERHEAD_TOKENS, 0)
    return int(budget_tokens * chars_per_token)


def _truncate_source(source: str, budget_chars: int) -> str:
    if budget_chars <= 0 or len(source) <= budget_chars:
        return source
    return source[:budget_chars] + "\n# ... (truncated to fit summarization budget)"


def build_summary_prompt(
    module: ModuleRecord,
    source: str,
    *,
    num_ctx: int = DEFAULT_NUM_CTX,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """The user-turn prompt: static facts first (authoritative), then a
    budget-truncated source excerpt for prose color only."""
    facts = _facts_block(module)
    budget = _budget_chars(num_ctx, max_tokens)
    excerpt = _truncate_source(source, budget)
    return (
        "Structural facts (from AST analysis — treat as ground truth, do not "
        "contradict):\n"
        f"{facts}\n\n"
        "Source:\n"
        f"{excerpt}\n"
    )


# ── reply parsing ───────────────────────────────────────────────────────────────


def _parse_llm_summary(raw: str) -> LLMSummary:
    """Parse a Pass B reply into an `LLMSummary`.

    Anything that isn't the expected `{"purpose": ..., "notes": ...}` JSON
    object — empty reply, prose, malformed JSON, a JSON array/number/string
    — degrades to an empty summary rather than raising. A confused model
    must never crash the batch; COLLECT-1 already guarantees it can't
    corrupt anything structural even if it tried.
    """
    from tools.llm_stream import strip_json_fence, strip_think

    text = strip_think(raw or "")
    text = strip_json_fence(text).strip()
    if not text:
        return LLMSummary(purpose="", notes="")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return LLMSummary(purpose="", notes="")
    if not isinstance(data, dict):
        return LLMSummary(purpose="", notes="")
    purpose = data.get("purpose") or ""
    notes = data.get("notes") or ""
    return LLMSummary(purpose=str(purpose), notes=str(notes))


# ── single-module Pass B ─────────────────────────────────────────────────────────


def summarize_module(
    module: ModuleRecord,
    source: str,
    llm_call: LlmCall,
    *,
    num_ctx: int = DEFAULT_NUM_CTX,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ModuleRecord:
    """Run Pass B on one module: exactly one LLM call, output attached only
    through `with_llm_summary` (COLLECT-1's write-once-from-AST contract).

    A module that failed to parse (`module.parse_error` set) is returned
    unchanged — there is nothing coherent to summarize, and no facts to
    ground a summary in.
    """
    if module.parse_error is not None:
        return module
    user_msg = build_summary_prompt(module, source, num_ctx=num_ctx, max_tokens=max_tokens)
    raw = llm_call(SYSTEM_PROMPT, user_msg)
    summary = _parse_llm_summary(raw)
    return module.with_llm_summary(summary)


# ── batch Pass B: retry / resume over a whole repo ───────────────────────────────


def summarize_repo(
    modules: Iterable[ModuleRecord],
    sources: Dict[str, str],
    llm_call: LlmCall,
    *,
    num_ctx: int = DEFAULT_NUM_CTX,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_error: Optional[Callable[[str, Exception, int], None]] = None,
    checkpoint_path: Optional[Path] = None,
) -> List[ModuleRecord]:
    """Run Pass B over every module, in order.

    Retry/resume (♻️ `tools.backoff`):
      * A module whose LLM call raises is retried with the project's
        standard exponential backoff schedule (`backoff_seconds`), up to
        `max_retries` times; after that it's kept structural-only (no
        `summary` attached) rather than aborting the whole run — one flaky
        module must not cost every other module its summary.
      * If `checkpoint_path` is given, a summary that lands successfully is
        persisted immediately (`tools.backoff.save_state`); a run that
        restarts with the same `checkpoint_path` picks up already-summarized
        modules from the checkpoint instead of re-calling the LLM for them,
        and the checkpoint is cleared once the whole batch completes.

    `sleep_fn` defaults to `time.sleep` but is injectable so tests can run
    the retry path without actually waiting.
    """
    from tools.backoff import backoff_seconds, clear_state, load_state, save_state

    done: Dict[str, dict] = {}
    if checkpoint_path is not None:
        state = load_state(checkpoint_path)
        if state and state.get("loop") == "collect_summarize":
            done = dict(state.get("modules", {}))

    out: List[ModuleRecord] = []
    for module in modules:
        if module.parse_error is not None:
            out.append(module)
            continue

        cached = done.get(module.path)
        if cached is not None:
            out.append(module.with_llm_summary(
                LLMSummary(purpose=cached.get("purpose", ""), notes=cached.get("notes", ""))
            ))
            continue

        source = sources.get(module.path, "")
        result = module
        error_count = 0
        while True:
            try:
                result = summarize_module(
                    module, source, llm_call, num_ctx=num_ctx, max_tokens=max_tokens
                )
                break
            except Exception as exc:  # noqa: BLE001 - one bad module must not abort the batch
                error_count += 1
                if on_error is not None:
                    on_error(module.path, exc, error_count)
                if error_count > max_retries:
                    logger.warning(
                        "collect summarizer: giving up on %s after %d error(s): %s",
                        module.path, error_count, exc,
                    )
                    result = module
                    break
                wait = backoff_seconds(error_count - 1)
                logger.warning(
                    "collect summarizer: %s failed (%s); retrying in %ds",
                    module.path, exc, wait,
                )
                sleep_fn(wait)

        out.append(result)
        if checkpoint_path is not None and result.summary is not None:
            done[result.path] = {
                "purpose": result.summary.purpose,
                "notes": result.summary.notes,
            }
            save_state({"loop": "collect_summarize", "modules": done}, checkpoint_path)

    if checkpoint_path is not None:
        clear_state(checkpoint_path)
    return out


# ── config-driven LLM call factory ────────────────────────────────────────────


def collect_llm_budget(config, task_mode: str = "code") -> "tuple[int, int]":
    """`(num_ctx, max_tokens)` for Pass B, reading `[collect]` first (with
    `{key}_{task_mode}` override support, same convention as `[coder]`),
    falling back to the active API profile's `num_ctx` / a safe default.
    """
    from tools.auto.utils import _cfg_mode

    active = config.get("api", "active", fallback="local")
    api_sec = f"api_{active}"

    num_ctx_str = _cfg_mode(config, "collect", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        num_ctx_str = config.get(api_sec, "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)

    max_tokens_str = _cfg_mode(
        config, "collect", "max_tokens", task_mode, fallback=str(DEFAULT_MAX_TOKENS)
    )
    max_tokens = int(max_tokens_str)
    return num_ctx, max_tokens


def should_run_pass_b(config) -> bool:
    """`[collect] llm_summaries` (default true) — the config-level twin of
    the CLI's `--no-llm` flag (COLLECT-19 wires the flag; this reads the
    config default it falls back to)."""
    return config.getboolean("collect", "llm_summaries", fallback=True)


def _make_llm_call(config, task_mode: str = "code") -> LlmCall:
    """Build a `(system, user) -> str` callable from *config*.

    Same API-profile / SSL / request-building plumbing as
    `tools.auto.summary_memory._make_llm_call` (♻️ `build_chat_request` /
    `request_completion`), read through `[collect]` for Pass B's own
    budget/temperature/think knobs instead of `[coder]`/`[summary_memory]`.
    """
    import tools.llm_stream as _llm_stream

    active = config.get("api", "active", fallback="local")
    api_sec = f"api_{active}"

    base_url = config.get(api_sec, "base_url", fallback="http://localhost:11434")
    api_key = config.get(api_sec, "api_key", fallback="ollama")
    model = config.get(api_sec, "model", fallback="llama3.1:8b")
    api_format = config.get(api_sec, "api_format", fallback="ollama")
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

    num_ctx, max_tokens = collect_llm_budget(config, task_mode=task_mode)
    temperature = config.getfloat("collect", "temperature", fallback=0.1)
    timeout = config.getint("loop", "timeout_seconds", fallback=300)
    # Thinking models (qwen3) prepend a <think> block; Pass B is a short,
    # structured-JSON reply, so default this off the same way gate1/coder do
    # — otherwise a small max_tokens cap can truncate mid-<think> and the
    # reply never reaches a usable JSON object at all. [collect] think=true
    # re-enables it (strip_think below still cleans up either way).
    think = config.getboolean("collect", "think", fallback=False)

    ssl_context = _llm_stream.make_unverified_context() if not verify_ssl else None

    def _call(system: str, user: str) -> str:
        url, headers, payload = _llm_stream.build_chat_request(
            base_url=base_url, api_key=api_key, model=model, api_format=api_format,
            temperature=temperature, max_tokens=max_tokens, system=system, user_msg=user,
            num_ctx=num_ctx, think=think if api_format == "ollama" else None,
        )
        return _llm_stream.strip_think(
            _llm_stream.request_completion(
                url=url, headers=headers, payload=payload, timeout=timeout,
                api_format=api_format, ssl_context=ssl_context,
            ) or ""
        )

    return _call


def make_summarizer_call(config, task_mode: str = "code") -> LlmCall:
    """Public entry point CLI code (COLLECT-19) uses to get a Pass B
    `LlmCall` from `agents.ini`. Thin wrapper so tests can monkeypatch
    `tools.collect.summarizer._make_llm_call` the same way existing tests
    monkeypatch `tools.auto.summary_memory._make_llm_call`."""
    return _make_llm_call(config, task_mode=task_mode)
