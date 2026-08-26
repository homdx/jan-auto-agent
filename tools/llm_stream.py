"""
tools/llm_stream.py

Single helper for calling the chat-completions endpoint either blocking or
streaming.  When streaming, each token is passed to `on_token` as it arrives
(so the caller can echo it live, like the direct-chat path does) AND
accumulated, so the full assistant message is still returned for JSON parsing.

This lets the validator / improvement agents show their answer being typed out
in real time while still receiving the complete text to json.loads() at the end.
"""

import json
import logging
import re
import ssl
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# AUTO-REASONING-2 / GATE1-PROVIDER-1: process-lifetime memory of which
# (endpoint, model) pairs have already told us (via HTTP 400) that they
# don't recognise the `reasoning` field build_chat_request() sends for
# think=False.
#
# Keyed by (url, model), NOT url alone. Many of this project's free-tier
# gateways (kenari.id, bynara.id, NaraRouter, ...) route several different
# *models* through the exact same `{base_url}/chat/completions` endpoint.
# Keying by url alone means one model's 400 would incorrectly poison every
# other model behind that same router — e.g. `deepseek-r1` on kenari.id
# rejects `reasoning` but `llama-3.3` on the SAME kenari.id URL might
# accept it fine. The reverse (same model, different provider/URL,
# different support) was already handled correctly before this change
# since url was part of the key — this extends that same "don't assume,
# remember per-endpoint" principle to also cover "don't assume, remember
# per-model-at-that-endpoint". `model` is whatever string identifies the
# model to that endpoint (the payload's "model" field) — an empty/None
# model still works as an ordinary dict key, it just means "no model info
# available", which never collides with a real model name that happens to
# be the empty string.
_REASONING_UNSUPPORTED_KEYS: set = set()


def mark_reasoning_field_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects the `reasoning` field, so
    future `build_chat_request()` calls targeting this exact (url, model)
    pair never send it again this process. Idempotent; safe to call from
    any thread (set.add is atomic under the GIL for this simple case).
    Does NOT affect a different model at the same url, or the same model
    at a different url."""
    _REASONING_UNSUPPORTED_KEYS.add((url, model))


def reasoning_field_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_reasoning_field_unsupported(url, model)` has
    fired for this exact (url, model) pair in this process; `True`
    otherwise (including for pairs never tried, and for the same url with
    a different model)."""
    return (url, model) not in _REASONING_UNSUPPORTED_KEYS


# AUTO-THINKDEPTH-2 / GATE1-PROVIDER-1: process-lifetime memory, mirroring
# _REASONING_UNSUPPORTED_KEYS above, but for the top-level
# `reasoning_effort` string field — the actual OpenAI/Gemini-compat
# Chat Completions standard (flat `"reasoning_effort": "low"`), which is
# DIFFERENT from OpenRouter's nested `"reasoning": {"effort": "low"}`
# shape that `_REASONING_UNSUPPORTED_KEYS` already tracks. This is tier 1
# of a three-tier cascade for `think_effort` on the openai branch:
#   1. top-level `reasoning_effort` (OpenAI/Gemini standard) — tried first
#   2. nested `reasoning: {"effort": ...}` (OpenRouter) — fallback if (1)
#      is rejected
#   3. no depth field at all, plain `think`-only mode — fallback if both
#      (1) and (2) are rejected
# Each tier has its own per-(url, model) "stop asking" memory (see
# GATE1-PROVIDER-1 note above — same router-sharing concern applies here)
# so a provider that rejects tier 1 but accepts tier 2 keeps getting tier
# 2 forever, without re-paying for a guaranteed-to-fail tier-1 round trip
# on every call, and without one model's rejection poisoning a sibling
# model on the same URL.
_REASONING_EFFORT_TOPLEVEL_UNSUPPORTED_KEYS: set = set()


def mark_reasoning_effort_toplevel_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects the top-level
    `reasoning_effort` field, so future `build_chat_request()` calls
    targeting this exact (url, model) pair go straight to tier 2 (nested
    `reasoning: {"effort": ...}`) instead. Idempotent."""
    _REASONING_EFFORT_TOPLEVEL_UNSUPPORTED_KEYS.add((url, model))


def reasoning_effort_toplevel_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_reasoning_effort_toplevel_unsupported(url,
    model)` has fired for this exact (url, model) pair in this process;
    `True` otherwise (including for pairs never tried)."""
    return (url, model) not in _REASONING_EFFORT_TOPLEVEL_UNSUPPORTED_KEYS


# AUTO-JSONMODE-1 / GATE1-PROVIDER-1: same per-process, per-(url, model)
# "stop asking" memory as AUTO-REASONING-2 above, but for the
# *response_format* / Ollama "format" JSON-mode field driven by the
# global `[api] response_format = true` ini switch (see
# build_chat_request's *response_format* parameter), or by a
# presence_llm_profile's own override. Once a given (endpoint, model)
# pair has rejected the field with HTTP 400, every subsequent
# build_chat_request() call targeting that exact pair stops sending it
# for the rest of the process — matching the reasoning-field fallback so
# a provider/model combination that doesn't support enforced JSON mode
# never repeats a guaranteed-to-fail round trip, and the rest of the run
# falls back to the existing best-effort JSON parsing/salvage path
# unchanged, without affecting a different model sharing the same router
# URL.
_RESPONSE_FORMAT_UNSUPPORTED_KEYS: set = set()


def mark_response_format_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects enforced JSON mode
    (``response_format`` / Ollama ``format``), so future
    ``build_chat_request()`` calls targeting this exact (url, model) pair
    never send it again this process. Idempotent."""
    _RESPONSE_FORMAT_UNSUPPORTED_KEYS.add((url, model))


def response_format_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_response_format_unsupported(url, model)` has
    fired for this exact (url, model) pair in this process; `True`
    otherwise (including for pairs never tried)."""
    return (url, model) not in _RESPONSE_FORMAT_UNSUPPORTED_KEYS


# AUTO-THINKDEPTH-1 / GATE1-PROVIDER-1: per-process, per-(url, model)
# "stop asking" memory for the Ollama branch's *think_effort* depth
# string (e.g. "low"/"medium"/"high" — gpt-oss-style models), mirroring
# AUTO-JSONMODE-1 above but scoped to Ollama's own top-level "think"
# field specifically. NOT reused for the openai branch: there, a depth
# request rides inside the SAME "reasoning" object that think=False's
# suppression already uses, so a single rejection of that object already
# means "this endpoint+model doesn't recognise reasoning at all" and the
# existing _REASONING_UNSUPPORTED_KEYS cache already covers both
# directions. Ollama's plain boolean "think" field, by contrast, is
# separate from the depth-string variant and known-good on its own — so a
# depth-string rejection here degrades to the plain boolean, not to
# omitting "think" altogether. Keyed by (url, model) for the same reason
# as the other three caches above — a multi-model Ollama-compatible proxy
# behind one URL should get independent verdicts per model.
_THINK_DEPTH_UNSUPPORTED_KEYS: set = set()


def mark_think_depth_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects a string reasoning-depth
    value in Ollama's ``think`` field, so future ``build_chat_request()``
    calls targeting this exact (url, model) pair send the plain boolean
    instead. Idempotent."""
    _THINK_DEPTH_UNSUPPORTED_KEYS.add((url, model))


def think_depth_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_think_depth_unsupported(url, model)` has fired
    for this exact (url, model) pair in this process; `True` otherwise
    (including for pairs never tried)."""
    return (url, model) not in _THINK_DEPTH_UNSUPPORTED_KEYS


# AUTO-ZAITHINK-1: per-process, per-(url, model) "stop asking" memory for
# Z.ai/GLM's own top-level ``thinking: {"type": "enabled" | "disabled"}``
# object — a THIRD, distinct thinking-control convention alongside
# OpenRouter's nested ``reasoning: {"effort": ..., "exclude": ...}``
# (AUTO-REASONING-1) and the OpenAI/Gemini-compat flat ``reasoning_effort``
# string (AUTO-THINKDEPTH-2). Confirmed live: a GLM-5.2 endpoint ignored
# both existing fields outright (neither is Z.ai's documented shape) and
# defaulted to its own "Max" reasoning effort, exhausting a modest
# max_tokens budget on <think> alone and leaving `content` genuinely
# empty — the same failure signature AUTO-REASONING-1 exists to prevent,
# just via a field this codebase didn't send yet.
#
# Sent ADDITIVELY alongside the existing reasoning/reasoning_effort
# fields (never instead of them) — a provider that doesn't recognise
# `thinking` is expected to silently ignore the unknown field, same
# assumption the other three fields already rely on, so trying all three
# conventions costs nothing extra against a provider that only speaks
# one of them. Keyed by (url, model) from the start, matching the other
# four caches — a router fronting several models must give each an
# independent verdict.
_ZAI_THINKING_UNSUPPORTED_KEYS: set = set()


def mark_zai_thinking_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects the top-level
    ``thinking: {"type": ...}`` object, so future ``build_chat_request()``
    calls targeting this exact (url, model) pair never send it again this
    process. Idempotent."""
    _ZAI_THINKING_UNSUPPORTED_KEYS.add((url, model))


def zai_thinking_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_zai_thinking_unsupported(url, model)` has fired
    for this exact (url, model) pair in this process; `True` otherwise
    (including for pairs never tried)."""
    return (url, model) not in _ZAI_THINKING_UNSUPPORTED_KEYS


# MASK-KEY-1: matches an ini-style `api_key = <value>` assignment line,
# including comment-prefixed variants such as `### api_key = ...` or
# `; api_key = ...` (any leading whitespace, optional comment chars
# [#;]+ before the key name; case-insensitive key name) so a real or
# test secret pasted/read into a prompt (agents.ini contents, a config
# dump, etc.) never reaches the LLM verbatim. Captures the prefix
# (indent + optional comment + "api_key = ") separately so the value
# alone is swapped for the placeholder.
_API_KEY_LINE_RE = re.compile(
    r'(?im)^([ \t]*(?:[#;]+[ \t]*)?api_key[ \t]*=[ \t]*)(\S+)([ \t]*)$'
)


def mask_api_key(text: str) -> str:
    """Replace every ``api_key = <value>`` line in *text* with
    ``api_key = here_your_key``, regardless of what the value actually is
    (a placeholder like "test", a real key, anything non-blank).

    This is a plain string transform with no knowledge of *which* key is
    "real" — it masks unconditionally so a secret embedded in file content
    that gets pulled into a prompt can't leak to the LLM. Non-string input
    (``None``, already-masked text, text with no such line) is returned
    unchanged.
    """
    if not text:
        return text
    return _API_KEY_LINE_RE.sub(r"\1here_your_key\3", text)


def strip_think(text: str) -> str:
    """
    Remove reasoning-model <think>…</think> blocks from model output.

    Handles four cases:
      1. Well-formed  <think>…</think>answer  → strips the block, keeps answer.
      2. Dangling </think> with no open tag   → keeps text after last </think>.
      3. Unclosed <think> with no close tag   → discards everything from <think>
         onward (the model truncated mid-think; there is no usable answer after).
      4. Stray lone tags after the above       → stripped with replace().
    Returns the cleaned, stripped text.
    Needed because models like qwen3 wrap their JSON / answer in <think> tags,
    which otherwise breaks json.loads and pollutes rendered answers.
    """
    if not text:
        return text
    out = _THINK_RE.sub("", text)
    if "</think>" in out:                       # dangling close tag — keep tail
        out = out.rsplit("</think>", 1)[-1]
    elif "<think>" in out:                      # unclosed open tag — discard from here
        out = out.split("<think>", 1)[0]
    out = out.replace("<think>", "").replace("</think>", "")
    return out.strip()


def _extract_content(raw: dict, api_format: str) -> str:
    """Extract assistant message text from a non-streaming response dict.

    BUGFIX: a message's `content` can legitimately come back JSON `null`
    instead of an empty string — e.g. some OpenAI-compatible gateways
    (reasoning/tool-call-only turns, certain filtered/empty replies) send
    `"content": null` with HTTP 200 rather than omitting the key or using
    `""`. `raw["message"]["content"].strip()` / `choices[0]["message"]
    ["content"].strip()` then raised `AttributeError: 'NoneType' object
    has no attribute 'strip'` — a response that arrived successfully was
    crashing the whole call instead of degrading to an empty reply the
    same way a filtered/empty `choices` list already does below.
    """
    # AUTO-FIX (medium-priority audit, DeepSeek-plan finding): both
    # dict-key accesses below (`raw["message"]`, `choices[0]["message"]`)
    # already handle a `content` of `null` (the BUGFIX above) and an empty
    # `choices` list (the BUGFIX below), but a genuinely malformed reply
    # from a non-standard OpenAI-compatible backend — one missing
    # "message" entirely, not just a null/empty content/choices field —
    # still raised a bare KeyError with no diagnostic content. Surface the
    # same kind of descriptive ValueError the empty-choices case already
    # uses, rather than letting a raw KeyError propagate.
    if api_format == "ollama":
        try:
            return (raw["message"]["content"] or "").strip()
        except KeyError as exc:
            raise ValueError(
                f"LLM response missing expected key {exc} — raw response "
                f"shape was unexpected (raw keys: {list(raw.keys())})"
            ) from exc
    # openai (default)
    # BUGFIX: mirror the streaming path's empty-choices guard (introduced in
    # the "Fix llm stream" commit).  Some OpenAI-compatible backends — Jan,
    # LiteLLM, vLLM, Azure proxy wrappers — return a non-streaming response
    # with `"choices": []` when the request is filtered/blocked instead of
    # returning an HTTP error.  The raw `raw["choices"][0]` access raises
    # IndexError in that case, which callers that only catch broad
    # `except Exception` misclassify as a generic API failure rather than a
    # distinct "response arrived but was empty/filtered" outcome.
    # Raising a descriptive ValueError keeps the failure distinguishable for
    # logging and for the AUTO-BUG pattern in validator_agent.py.
    choices = raw.get("choices") or []
    if not choices:
        raise ValueError(
            f"LLM response had no choices — likely blocked/filtered by the "
            f"backend (raw keys: {list(raw.keys())})"
        )
    try:
        return (choices[0]["message"]["content"] or "").strip()
    except KeyError as exc:
        raise ValueError(
            f"LLM response's first choice is missing expected key {exc} — "
            f"raw response shape was unexpected (choice keys: "
            f"{list(choices[0].keys()) if isinstance(choices[0], dict) else type(choices[0]).__name__})"
        ) from exc


def _build_payload(payload: dict, api_format: str, stream: bool) -> dict:
    """
    Return a copy of payload shaped for the target API format.

    openai : top-level temperature, max_tokens, stream flag, standard
             messages array.
    ollama : temperature AND max_tokens both move into options{} (the
             latter renamed to Ollama's own "num_predict"), num_ctx added
             if present, /api/chat expects
             {"model", "messages", "stream", "options"}.
    """
    body = dict(payload)
    if api_format == "ollama":
        options = {}
        if "temperature" in body:
            options["temperature"] = body.pop("temperature")
        if "max_tokens" in body:
            # AUTO-BUG: this key used to be left untouched here, so a
            # caller that builds its own OpenAI-shaped payload directly
            # (ImprovementAgent.process, OrchestratorActions._edit_file_
            # content when [file_editor] max_tokens is set, and several
            # call sites in FAQAgent — everyone who calls
            # request_completion() themselves instead of going through
            # build_chat_request(), which already handles this correctly)
            # had "max_tokens" sent as a stray TOP-LEVEL field. Ollama's
            # /api/chat does not recognize a top-level "max_tokens" — it
            # silently ignores unknown fields — so the configured cap was
            # never enforced at all against an Ollama backend, which is
            # this project's *default* profile (agents.ini's shipped
            # [api_local] section: api_format = ollama). Confirmed with a
            # direct call: max_tokens stayed in the body but never reached
            # options.num_predict. Renaming/moving it here fixes every
            # affected caller at once, the same way build_chat_request
            # already does for its own payload-construction path.
            _mt = body.pop("max_tokens")
            if _mt:
                options["num_predict"] = _mt
        if "num_ctx" in body:
            # 0 / falsy means "use server default" everywhere in this
            # project — never forward it, or Ollama would treat it as a
            # literal zero-token context window.
            _nc = body.pop("num_ctx")
            if _nc:
                options["num_ctx"] = _nc
        if options:
            body["options"] = options
        body["stream"] = stream
        # /api/chat does not use a separate system message list entry —
        # system content is passed as a messages entry with role "system",
        # which is already the format callers use, so nothing extra needed.
    else:
        # num_ctx is an Ollama-only concept; OpenAI-compatible servers
        # reject or ignore unknown fields — drop it rather than leak it.
        body.pop("num_ctx", None)
        if stream:
            body["stream"] = True
    return body


def _mask_payload_secrets(payload: dict) -> dict:
    """Return a copy of *payload* with `mask_api_key` applied to every
    string message ``content`` field.

    MASK-KEY-1: every caller in this project (Coder, Gate1Filter,
    ClusterReviewer, TaskRewriter via ``build_chat_request``, and the
    handful of agents — ImprovementAgent, FaqAgent, OrchestratorActions,
    ...  — that assemble their own OpenAI-shaped payload directly) ends up
    calling ``request_completion`` to actually make the HTTP call. Masking
    here, right before the request body is built, is the one place that
    catches an ``api_key = ...`` line regardless of which agent's prompt
    it came from (e.g. a file's contents — agents.ini or similar — pulled
    into context) without needing every call site to remember to mask it
    itself.

    Returns *payload* unchanged (no copy) if nothing needed masking, so
    callers that don't touch config-shaped text pay no extra cost.
    """
    messages = payload.get("messages")
    if not messages:
        return payload

    changed = False
    new_messages = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            masked = mask_api_key(content)
            if masked != content:
                changed = True
                new_messages.append({**msg, "content": masked})
                continue
        new_messages.append(msg)

    if not changed:
        return payload

    out = dict(payload)
    out["messages"] = new_messages
    return out


def ollama_chat_url(base_url: str) -> str:
    """Return the correct Ollama /api/chat URL from *base_url*.

    Handles three conventions:
      - base_url is already the full chat endpoint
        (ends with ``/api/chat``)   → returned unchanged
      - base_url already ends with ``/api``  → append ``/chat`` only
      - base_url does not end with ``/api``  → append ``/api/chat``

    This avoids the doubled ``/api/api/chat`` (and ``/api/chat/api/chat``)
    that resulted when a caller's ``base_url`` was configured as the
    complete chat endpoint rather than the bare host/``/api`` root, as
    well as the broken ``/chat`` that stripping produced for non-auto
    callers.
    """
    base = base_url.rstrip("/")
    if base.endswith("/api/chat"):
        return base
    if base.endswith("/api"):
        return f"{base}/chat"
    return f"{base}/api/chat"


def strip_json_fence(text: str) -> str:
    """Strip a ```json ... ``` or ``` ... ``` fence wrapping a JSON blob, if present."""
    if "```json" in text:
        return text.split("```json")[1].split("```")[0].strip()
    if "```" in text:
        return text.split("```")[1].split("```")[0].strip()
    return text


def make_unverified_context() -> ssl.SSLContext:
    """Return an SSLContext that skips certificate verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── AUTO-RATE-1: rate-limit / transient-error retry ──────────────────────────
#
# request_completion() previously raised on the very first non-2xx response,
# with no concept of a rate limit being a "wait and try again" situation
# rather than a hard failure. Confirmed real-world impact: Gate1Filter's
# presence check (tools/auto/gate1_filter.py) fails a candidate CLOSED on
# ANY exception from this function — so a single transient HTTP 429 from a
# free-tier gateway (e.g. kenari.id: "free_quota_rpm ... slow down and
# retry") was silently indistinguishable from "the LLM confirmed this task
# is already fixed", and a real, still-needed task could be wrongly dropped
# from an --auto plan or --validate-plan pass purely because the gateway
# was rate-limiting a burst of calls, not because anything was actually
# wrong with the task.
#
# _parse_retry_after / the retry loop below are ported from the sibling
# learn-in-play1 project's llm_client.py (its own docstring: "Логика и
# формат payload'ов взяты ... из tools/llm_stream.py проекта jan-auto-agent"
# — llm_client.py started as a simplified copy of THIS file and was
# hardened independently against real Groq/Gemini/HuggingFace 429s; this
# brings that hardening back to its origin).
_RETRY_AFTER_MS_RE = re.compile(r"(?:try again|retry)\s+in\s+([\d.]+)\s*ms", re.IGNORECASE)
_RETRY_AFTER_S_RE = re.compile(r"(?:try again|retry)\s+in\s+([\d.]+)\s*s(?:econds?)?\b", re.IGNORECASE)

# Status codes worth waiting and retrying: 429 (rate limit) and 402
# ("payment required" — several gateways, e.g. HuggingFace's router, use
# this for a transient billing hiccup, not just a genuinely exhausted
# quota) and 5xx (server-side, plausibly transient). Deliberately NOT
# other 4xx codes (400 bad request, 401/403 auth, 404 not found, ...) —
# those describe something wrong with THIS request that a wait can't fix;
# retrying them only wastes error_retry_wait_sec for a guaranteed repeat
# failure.
def _is_retryable_status(code: int) -> bool:
    return code == 429 or code == 402 or 500 <= code < 600


def _parse_retry_after(e: urllib.error.HTTPError, detail: str) -> "float | None":
    """Return how long the server asked us to wait before retrying, in
    seconds, or ``None`` if no hint was found anywhere.

    Checked in order:
      1. The standard ``Retry-After`` HTTP header, when present.
      2. A millisecond hint in the error body text — e.g. Groq's
         ``"Please try again in 820ms"`` — some gateways send an exact
         figure in the body even when they don't set the header.
      3. A seconds hint in the error body text — e.g. Google Gemini's
         ``"Please retry in 57.062042596s."`` (a different verb, "retry"
         rather than "try again", and no header at all — the two regexes
         below cover both phrasings).

    Only meaningful for 429s in practice — other retryable codes (402,
    5xx) don't reliably carry a precise wait hint in this shape, so
    callers should only consult this for ``e.code == 429`` and fall back
    to a fixed wait otherwise.
    """
    if e.headers:
        retry_after = e.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.1, float(retry_after))
            except ValueError:
                pass
    m = _RETRY_AFTER_MS_RE.search(detail)
    if m:
        try:
            return max(0.1, float(m.group(1)) / 1000.0)
        except ValueError:
            pass
    m = _RETRY_AFTER_S_RE.search(detail)
    if m:
        try:
            return max(0.1, float(m.group(1)))
        except ValueError:
            pass
    return None


class LLMClientBase:
    """Shared constructor for Coder, Gate1Filter, ClusterReviewer, and
    TaskRewriter: the connection fields and SSL context are identical
    across all four; each subclass adds its own model/prompt settings."""

    def __init__(self, config, base_url: str, api_key: str, model: str,
                 api_format: str = "openai", verify_ssl: bool = True) -> None:
        self._config     = config
        self._base_url   = base_url.rstrip("/")
        self._api_key    = api_key
        self._model      = model
        self._api_format = api_format
        self._ssl_context = make_unverified_context() if not verify_ssl else None


def build_chat_request(
    *, base_url: str, api_key: str, model: str, api_format: str,
    temperature: float, max_tokens: int, system: str, user_msg: str,
    num_ctx: int = 0, think: "bool | None" = None,
    response_format: bool = False, think_effort: "str | None" = None,
) -> tuple[str, dict, dict]:
    """
    Build the (url, headers, payload) triple for a one-shot system/user chat
    call, branching on *api_format* ("ollama" vs an openai-compatible API).

    Shared by Coder, Gate1Filter, Architect, and TaskRewriter — all four send
    the same single-turn system+user request and only differ in which
    model/temperature/system prompt they configure.

    *think*, when not ``None``, asks the server to suppress (``False``) or
    allow (``True``) a reasoning model's hidden chain-of-thought, so a small
    ``max_tokens`` budget meant for a short, deterministic answer (e.g.
    Gate 1's presence check) doesn't get consumed by reasoning before any
    usable reply is written — leaving an empty or truncated response.

    ``api_format="ollama"``: passed as the top-level ``"think"`` field.

    ``api_format="openai"`` (or any other non-Ollama value): ``think=False``
    sends OpenRouter's unified ``reasoning: {"effort": "low", "exclude":
    true}`` object — the closest thing to a de-facto standard across
    OpenAI-compatible aggregators (the free-tier gateways this project
    targets, e.g. kenari.id, follow the same convention). A provider that
    doesn't recognise the field is expected to silently ignore it rather
    than reject the request — but AUTO-REASONING-2 / GATE1-PROVIDER-1: if
    this exact (endpoint, model) pair already told us otherwise (a prior
    ``request_completion()`` call hit an HTTP 400 rejecting the field and
    called ``mark_reasoning_field_unsupported(url, model)``), the field is
    skipped outright here rather than repeating a guaranteed-to-fail round
    trip — keyed by (url, model), not url alone, so a different model
    behind the same router URL gets its own independent answer.
    ``think=True`` sends nothing extra — most models reason by default;
    only *suppressing* it needs an explicit field. Omitted entirely (both
    branches) when ``think`` is ``None`` (server/model default either way).

    AUTO-JSONMODE-1: *response_format*, when ``True``, asks the server to
    enforce valid JSON at the token-sampling level instead of relying on
    prompt instructions + this project's after-the-fact salvage parsing
    (``_salvage_json_objects`` in ``tools/auto/architect.py``). Driven by a
    single global ``[api] response_format = true`` ini switch (default
    ``false`` — current best-effort parsing behaviour is unchanged unless a
    user opts in), NOT a per-caller setting, so one flag governs every
    caller that goes through this function (Gate1Filter today; Coder /
    Architect / TaskRewriter can opt in the same way later). A
    presence_llm_profile (see Gate1Filter) may override the flag for its
    own (url, model) pair independently.

    ``api_format="openai"``: sends the OpenAI-standard
    ``response_format: {"type": "json_object"}``.
    ``api_format="ollama"``: sends Ollama's own ``format: "json"`` field
    (Ollama does not use the OpenAI ``response_format`` shape).

    Skipped outright — regardless of the flag — once
    ``mark_response_format_unsupported(url, model)`` has already fired for
    this exact (url, model) pair in this process (see
    ``request_completion``'s 400 handling below): a provider/model
    combination that has already rejected the field once is not asked
    again, and every call after the first failure silently falls back to
    the pre-existing behaviour for the rest of the run — a different model
    sharing the same router URL is unaffected.

    AUTO-THINKDEPTH-1: *think_effort* ("low" / "medium" / "high", or any
    string the target model recognises), when given AND ``think`` is
    ``True``, asks the server for that specific reasoning depth instead of
    a bare on/off toggle. Driven by a single global ``[api]
    think_effort_enabled = true`` + ``[api] think_effort = <depth>`` ini
    pair (default disabled — a plain ``think=True/False`` on/off keeps
    working exactly as it does today when this is off or omitted). Has NO
    effect when ``think`` is ``False`` or ``None`` — depth only makes
    sense once reasoning is actually enabled.

    ``api_format="openai"``: AUTO-THINKDEPTH-2 three-tier cascade, each
    tier gated by its own per-(url, model) "stop asking" cache so a
    provider+model pair that has already rejected a tier is never asked
    again this process:

      1. Top-level ``reasoning_effort: <depth>`` — the flat string field
         OpenAI's own Chat Completions API and Gemini's OpenAI-compat
         layer both document
         (https://ai.google.dev/gemini-api/docs/openai#thinking). Tried
         first. Governed by ``reasoning_effort_toplevel_is_supported(url,
         model)``.
      2. Nested ``reasoning: {"effort": <depth>}`` — OpenRouter's own
         unified reasoning-control shape (no ``exclude``, unlike the
         ``think=False`` suppression case above, since reasoning is being
         *requested* here, not suppressed). Tried once tier 1 has been
         marked unsupported for this (url, model) pair. Governed by the
         SAME ``reasoning_field_is_supported(url, model)`` cache as the
         suppression case: an (endpoint, model) pair that has already
         rejected the ``reasoning`` object in either direction is not
         asked again.
      3. Neither field sent — falls through to whatever the plain
         ``think`` on/off handling already put in the payload (or
         nothing). Reached once both tier 1 and tier 2 are marked
         unsupported for this (url, model) pair: the model still thinks,
         at its own default depth, no crash, no manual intervention
         needed.

    ``api_format="ollama"``: Ollama's own top-level ``think`` field
    accepts a depth string directly on models that support it (e.g.
    gpt-oss), replacing the plain boolean. Governed by a separate
    ``think_depth_is_supported(url, model)`` cache — a provider/model that
    rejects the depth *string* still supports the plain boolean, so this
    degrades to ``payload["think"] = think`` rather than omitting the
    field.

    AUTO-ZAITHINK-1: on the ``openai`` branch, whenever ``think`` is not
    ``None``, an ADDITIONAL top-level ``thinking: {"type": "enabled" |
    "disabled"}`` object is also sent — Z.ai/GLM's own thinking-control
    convention, distinct from both fields above (neither OpenRouter's
    ``reasoning`` object nor the OpenAI/Gemini ``reasoning_effort``
    string is recognised by Z.ai's API). Sent alongside, never instead
    of, the other fields: a provider that doesn't recognise ``thinking``
    is expected to silently ignore it, so trying all three conventions
    at once costs nothing against a provider that only speaks one.
    Governed by ``zai_thinking_is_supported(url, model)`` — a provider
    that rejects the field outright with an HTTP 400 has it stripped for
    the rest of the process for that exact (url, model) pair, same
    mechanics as the other four capability caches in this module.
    """
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    # MASK-KEY-1: mask any `api_key = ...` line that ended up inside the
    # system/user text itself (e.g. agents.ini contents pulled into
    # context) — NOT *api_key* the parameter above, which is the caller's
    # own credential for THIS request and is used as-is in the
    # Authorization header.
    messages = [
        {"role": "system", "content": mask_api_key(system)},
        {"role": "user",   "content": mask_api_key(user_msg)},
    ]
    if api_format == "ollama":
        url = ollama_chat_url(base_url)
        opts: dict = {"temperature": temperature, "num_predict": max_tokens}
        if num_ctx:
            opts["num_ctx"] = num_ctx
        payload: dict = {"model": model, "messages": messages, "options": opts}
        if think is not None:
            if think is True and think_effort and think_depth_is_supported(url, model):
                payload["think"] = think_effort
            else:
                payload["think"] = think
        if response_format and response_format_is_supported(url, model):
            payload["format"] = "json"
    else:
        # AUTO-URL-1: normalize a trailing slash on base_url before
        # concatenating — without this, `base_url = ".../v1beta/openai/"`
        # (trailing slash) produced `".../v1beta/openai//chat/completions"`
        # (double slash), which strict routers (confirmed live: Gemini's
        # openai-compat endpoint) 404 on rather than normalizing away.
        # ollama_chat_url() above already does the equivalent `.rstrip("/")`
        # for the Ollama branch — this brings the openai branch to the same
        # standard instead of trusting every caller's base_url to be
        # slash-free.
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": messages,
        }
        # AUTO-THINK-1: *think* used to be read by every caller
        # (Gate1Filter / Coder / ClusterReviewer / TaskRewriter — each
        # defaults its own [section] think = false out of the box, no user
        # action needed) but only ever forwarded into the payload above,
        # in the Ollama branch. Against any OpenAI-compatible remote
        # gateway (api_format=openai) fronting a reasoning model, the
        # think=False request was silently dropped: the model still
        # emitted its hidden chain-of-thought, which can consume most or
        # all of a tight max_tokens budget (Gate 1 wants a tiny,
        # deterministic JSON verdict, e.g. 512 tokens) before any usable
        # answer is written — the reply comes back empty, or truncated
        # mid-JSON. Confirmed directly: a real kenari.id / deepseek 429-
        # free-tier run produced `{"verdict": "confirmed", "reason` — a
        # genuine, otherwise-valid answer cut off mid-string by the token
        # cap, not a network or parsing bug.
        #
        # `reasoning` (OpenRouter's own unified reasoning-control object)
        # is deliberately the ONLY field sent here, not the fuller set the
        # sibling learn-in-play1 project's llm_client.py sends (which also
        # tries `reasoning_effort` and a vLLM/SGLang-specific
        # `chat_template_kwargs`): that project found `chat_template_kwargs`
        # produces a hard HTTP 400 on at least one real gateway (Groq), and
        # recovering from that needs a per-server "this field is rejected,
        # stop sending it" memory this stateless function doesn't have. A
        # provider that doesn't recognise `reasoning` is expected to
        # silently ignore the unknown field rather than reject the whole
        # request — keeping this a plain, best-effort addition with no new
        # failure mode of its own.
        if think is False and reasoning_field_is_supported(url, model):
            payload["reasoning"] = {"effort": "low", "exclude": True}
        elif think is True and think_effort:
            # AUTO-THINKDEPTH-2: three-tier cascade, each tier gated by
            # its own per-URL memory so a provider that has already
            # rejected a tier doesn't pay for that guaranteed-to-fail
            # round trip again:
            #   1. top-level `reasoning_effort` — OpenAI/Gemini-compat
            #      standard flat string field. Tried first; this is what
            #      Gemini's own OpenAI-compatibility layer documents
            #      (https://ai.google.dev/gemini-api/docs/openai#thinking)
            #      and what real OpenAI accepts too.
            #   2. nested `reasoning: {"effort": ...}` — OpenRouter's
            #      unified reasoning-control shape. Tried only once tier 1
            #      has been marked unsupported for this URL (via a real
            #      HTTP 400 — see request_completion below).
            #   3. neither field sent at all — falls through to whatever
            #      `think`'s own on/off handling already put in the
            #      payload (or nothing, if `think` is None/unset): the
            #      model still thinks, just at its own default depth, no
            #      crash, no manual intervention. Reached once BOTH tier 1
            #      and tier 2 have been marked unsupported for this URL.
            if reasoning_effort_toplevel_is_supported(url, model):
                payload["reasoning_effort"] = think_effort
            elif reasoning_field_is_supported(url, model):
                payload["reasoning"] = {"effort": think_effort}
            # else: both tiers exhausted for this URL — send nothing,
            # plain thinking mode, no depth hint (tier 3).
        if think is not None and zai_thinking_is_supported(url, model):
            # AUTO-ZAITHINK-1: Z.ai/GLM's own thinking-control convention
            # — a top-level `thinking: {"type": "enabled"|"disabled"}`
            # object, distinct from both fields above. Sent ADDITIONALLY,
            # never instead of them: a provider that doesn't recognise
            # this exact shape is expected to silently ignore it, same
            # assumption `reasoning`/`reasoning_effort` already rely on.
            # Confirmed live: without this, a GLM-5.2 endpoint ignored
            # both existing fields, defaulted to its own "Max" reasoning
            # effort, and exhausted a modest max_tokens budget on <think>
            # alone before any JSON was written — `content` came back
            # genuinely empty every attempt.
            payload["thinking"] = {"type": "enabled" if think else "disabled"}
        if response_format and response_format_is_supported(url, model):
            payload["response_format"] = {"type": "json_object"}
    return url, headers, payload


def request_completion(url, headers, payload, timeout, stream=False, on_token=None,
                       api_format: str = "openai", ssl_context: "ssl.SSLContext | None" = None,
                       error_retries: int = 3, error_retry_wait_sec: float = 60.0,
                       max_retry_after_sec: float = 180.0, on_retry=None,
                       _sleep_fn=None):
    """
    POST a chat-completions request and return the assistant message text.

    api_format : "openai"  → /v1/chat/completions  (SSE streaming, choices[])
                 "ollama"  → /api/chat              (NDJSON streaming, message{})

    stream=False : normal blocking request, returns the full content string.
    stream=True  : reads the token stream; calls on_token(tok) for each token
                   (if provided) and returns the accumulated content.

    AUTO-RATE-1: on a retryable HTTP status (429, 402, 5xx — see
    ``_is_retryable_status``), waits and retries the SAME request up to
    ``error_retries`` times before giving up, instead of raising on the
    very first non-2xx response. For 429 specifically, the wait is the
    server's OWN requested delay when it provides one (``_parse_retry_after``
    — the ``Retry-After`` header, or a "retry/try again in Ns/Nms" hint in
    the body); otherwise (or for 402/5xx) ``error_retry_wait_sec``.

    A requested wait longer than ``max_retry_after_sec`` is treated as a
    daily/monthly quota reset, not a transient rate limit, and raises
    immediately rather than blocking the caller for minutes or hours — a
    free-tier gateway can legitimately ask for a ``Retry-After`` in the
    thousands of seconds on a TPD (tokens-per-day) limit, and sleeping
    through that would stall an entire ``--auto``/``--validate-plan`` run
    on one call instead of surfacing the failure so the caller can move on.

    Pass ``error_retries=0`` to restore the pre-AUTO-RATE-1 fail-fast
    behavior (raise immediately on the first error, no wait).

    ``on_retry``, if given, is called with a one-line human-readable
    string before each wait, mirroring the retry callbacks already used
    elsewhere in this codebase (e.g. OuterLoop/Coder) — so a caller can
    surface "retrying in Ns" to its own progress display. Every retry is
    also logged via this module's logger regardless of ``on_retry``.
    ``_sleep_fn``, if given, replaces ``time.sleep`` — for tests that need
    to exercise the retry/backoff paths without a real wait (same
    injectable-for-tests convention as ``AutoController``'s ``_time_fn``).

    Raises urllib errors / network exceptions to the caller once retries
    (if any) are exhausted, or immediately for a non-retryable status.

    AUTO-FIX (medium-priority audit): ``TimeoutError``, ``ssl.SSLError``,
    ``ConnectionError``, and the broader ``urllib.error.URLError`` (DNS
    failure, connection refused, etc.) are now explicitly caught and
    retried using the same ``error_retries``/``error_retry_wait_sec``
    budget as a retryable HTTP status, instead of propagating as a raw,
    context-free exception on the first transient network hiccup.
    """
    # MASK-KEY-1: mask any `api_key = ...` line in the outgoing message
    # content *last*, right before the body is shaped/serialized — this is
    # the one point every caller passes through, regardless of how its
    # payload/messages were assembled upstream.
    payload = _mask_payload_secrets(payload)
    body = _build_payload(payload, api_format, stream)

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    sleep = _sleep_fn or time.sleep

    # AUTO-REASONING-1: `build_chat_request`'s think=False path sends an
    # OpenRouter-style `reasoning: {"effort": "low", "exclude": true}`
    # field, documented there as "expected to be silently ignored by a
    # provider that doesn't recognise it" — true for OpenRouter/kenari-style
    # aggregators, false for strict OpenAI-schema validators. Confirmed
    # live: Gemini's openai-compat endpoint
    # (generativelanguage.googleapis.com/v1beta/openai/chat/completions)
    # rejects it outright with HTTP 400 "Unknown name \"reasoning\": Cannot
    # find field.", which is NOT in `_is_retryable_status` and would
    # otherwise fail the whole call closed on every single request to a
    # strict provider. `_reasoning_stripped` tracks whether this one-time,
    # no-budget-cost fallback (drop the field, rebuild the body, retry
    # immediately) has already fired for this call — it's a payload-shape
    # fix, not a transient error, so it must never repeat or consume
    # `error_retries`.
    _reasoning_stripped = False

    def _looks_like_unknown_reasoning_field(detail: str) -> bool:
        low = detail.lower()
        return "reasoning" in low and ("unknown name" in low or "cannot find field" in low
                                        or "unrecognized" in low or "not supported" in low or "unsupported" in low
                                        or "invalid" in low)

    # AUTO-THINKDEPTH-2: tier 1 of the three-tier think_effort cascade —
    # payload-shape fix (this endpoint doesn't accept the top-level
    # `reasoning_effort` string field), not a transient error, fires ONCE
    # per call, immediately, without touching error_retries/backoff. On
    # rejection this does NOT give up on depth entirely — it downgrades to
    # tier 2 (nested `reasoning: {"effort": ...}`, OpenRouter's shape) in
    # the SAME retry, which the existing tier-2 block below then handles
    # exactly like it always has (including its own further fallback to
    # tier 3 if tier 2 also gets rejected).
    _reasoning_effort_toplevel_stripped = False

    def _looks_like_unknown_reasoning_effort_field(detail: str) -> bool:
        low = detail.lower()
        return "reasoning_effort" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    # AUTO-THINKDEPTH-1: mirrors _reasoning_stripped/_response_format_stripped
    # — a payload-shape fix (the endpoint doesn't accept a string depth in
    # Ollama's "think" field), not a transient error, fires ONCE per call.
    _think_depth_stripped = False

    def _looks_like_unknown_think_depth_value(detail: str) -> bool:
        low = detail.lower()
        return "think" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low or "boolean" in low
        )

    # AUTO-JSONMODE-1: mirrors _reasoning_stripped above — a payload-shape
    # fix (the provider doesn't support enforced JSON mode), not a
    # transient error, so it fires ONCE per call, immediately, without
    # touching error_retries/backoff.
    _response_format_stripped = False

    def _looks_like_unknown_response_format_field(detail: str) -> bool:
        low = detail.lower()
        # Match "format" as a mention of the field name loosely — error
        # bodies quote it in inconsistent ways (`"format"`, `\"format\"`
        # inside a JSON-encoded message string, unquoted, ...) — so check
        # the bare word rather than a specific quoting style. Combined
        # with the caller's _has_rf_field check (the outgoing payload
        # actually had the field) and the rejection-keyword check below,
        # this stays specific enough not to misfire on unrelated 400s.
        has_field_name = "response_format" in low or "format" in low
        return has_field_name and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    # AUTO-ZAITHINK-1: mirrors _reasoning_stripped above — a payload-shape
    # fix (this endpoint rejects the top-level `thinking: {"type": ...}`
    # object outright, rather than silently ignoring it), not a transient
    # error, so it fires ONCE per call, immediately, without touching
    # error_retries/backoff. Sent additively alongside `reasoning`/
    # `reasoning_effort`, so stripping it never removes those — a strict
    # provider that rejects `thinking` but still accepts one of the other
    # two keeps working exactly as it did before this field existed.
    _zai_thinking_stripped = False

    def _looks_like_unknown_zai_thinking_field(detail: str) -> bool:
        low = detail.lower()
        return "thinking" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    def _open():
        nonlocal body, req, _reasoning_stripped, _response_format_stripped, _think_depth_stripped, _reasoning_effort_toplevel_stripped, _zai_thinking_stripped
        attempt = 0
        while True:
            try:
                return urllib.request.urlopen(req, timeout=timeout, context=ssl_context)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass

                # AUTO-THINKDEPTH-2: tier 1 — strict-schema provider
                # rejected the top-level `reasoning_effort` string field
                # outright. Strip it and downgrade to tier 2 (nested
                # `reasoning: {"effort": ...}`) in the SAME retry, rather
                # than giving up on depth entirely — a provider that
                # doesn't speak the OpenAI-standard flat field (e.g. an
                # OpenRouter-style aggregator) may still understand the
                # nested shape, so tier 2 gets a real chance before
                # falling through to plain thinking mode.
                if (e.code == 400 and not _reasoning_effort_toplevel_stripped
                        and isinstance(body, dict) and "reasoning_effort" in body
                        and _looks_like_unknown_reasoning_effort_field(detail)):
                    _reasoning_effort_toplevel_stripped = True
                    # Remember this endpoint for the rest of the process —
                    # every future build_chat_request() call against the
                    # same URL now goes straight to tier 2 instead of
                    # repeating this exact 400.
                    mark_reasoning_effort_toplevel_unsupported(url, body.get("model"))
                    _depth = body.get("reasoning_effort")
                    body = {k: v for k, v in body.items() if k != "reasoning_effort"}
                    body["reasoning"] = {"effort": _depth}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    msg = (
                        f"HTTP 400 from {url}: provider rejected the "
                        f"top-level 'reasoning_effort' field used for "
                        f"[api] think_effort (depth={_depth!r}) — "
                        f"disabling it for this endpoint for the rest of "
                        f"the run, downgrading to the nested "
                        f"'reasoning: {{\"effort\": ...}}' shape "
                        f"(OpenRouter-style) and retrying once."
                    )
                    logger.warning("request_completion: %s", msg)
                    if on_retry:
                        on_retry(msg)
                    continue

                # AUTO-REASONING-1: strict-schema provider rejected the
                # `reasoning` field outright — strip it and retry ONCE,
                # immediately, without touching error_retries/backoff (this
                # isn't a rate limit or a transient server error).
                if (e.code == 400 and not _reasoning_stripped
                        and isinstance(body, dict) and "reasoning" in body
                        and _looks_like_unknown_reasoning_field(detail)):
                    _reasoning_stripped = True
                    # AUTO-REASONING-2: remember this endpoint for the rest
                    # of the process — every future build_chat_request()
                    # call against the same URL now skips the field
                    # outright instead of repeating this exact 400.
                    mark_reasoning_field_unsupported(url, body.get("model"))
                    # Distinguish which caller put "reasoning" in the
                    # payload — AUTO-REASONING-1's think=false suppression
                    # (`{"effort": "low", "exclude": True}`) vs
                    # AUTO-THINKDEPTH-1's think_effort depth hint
                    # (`{"effort": <configured depth>}`, no "exclude") —
                    # purely for an accurate log message; both share the
                    # same field name, cache, and strip/retry mechanics.
                    _rf = body.get("reasoning")
                    _is_effort_mode = isinstance(_rf, dict) and "exclude" not in _rf
                    body = {k: v for k, v in body.items() if k != "reasoning"}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    if _is_effort_mode:
                        msg = (
                            f"HTTP 400 from {url}: provider rejected the "
                            f"'reasoning' field used for [api] think_effort "
                            f"(depth={_rf.get('effort')!r}) — disabling "
                            f"think_effort for this endpoint for the rest "
                            f"of the run and retrying once without it. "
                            f"Falling back to plain think=true (no depth "
                            f"hint)."
                        )
                    else:
                        msg = (
                            f"HTTP 400 from {url}: provider rejected the "
                            f"'reasoning' field — retrying once without it "
                            f"(this provider needs [api_...] to not rely on "
                            f"OpenRouter-style think=false suppression)"
                        )
                    logger.warning("request_completion: %s", msg)
                    if on_retry:
                        on_retry(msg)
                    continue

                # AUTO-JSONMODE-1: provider rejected enforced-JSON-mode
                # field ("response_format" on openai-format, "format" on
                # ollama) outright — strip it and retry ONCE, immediately,
                # same shape as the reasoning-field fallback above. Then
                # remember this endpoint for the rest of the process (via
                # mark_response_format_unsupported) and fall back to the
                # existing best-effort JSON parsing/salvage path for every
                # subsequent call — this provider simply doesn't support
                # engine-level JSON enforcement.
                _has_rf_field = (
                    isinstance(body, dict)
                    and ("response_format" in body or body.get("format") == "json")
                )
                if (e.code == 400 and not _response_format_stripped
                        and _has_rf_field
                        and _looks_like_unknown_response_format_field(detail)):
                    _response_format_stripped = True
                    mark_response_format_unsupported(url, body.get("model"))
                    body = {k: v for k, v in body.items()
                            if k != "response_format" and k != "format"}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    msg = (
                        f"HTTP 400 from {url}: provider does NOT support "
                        f"enforced JSON mode ([api] response_format = true) — "
                        f"disabling it for this endpoint for the rest of the "
                        f"run and retrying once without it. Falling back to "
                        f"best-effort JSON parsing (as before this setting "
                        f"existed)."
                    )
                    logger.warning("=" * 78)
                    logger.warning("request_completion: %s", msg)
                    logger.warning("=" * 78)
                    if on_retry:
                        on_retry(msg)
                    continue

                # AUTO-ZAITHINK-1: provider rejected the top-level
                # `thinking: {"type": ...}` object outright — strip ONLY
                # that field and retry ONCE, immediately, same shape as
                # the other payload-shape fixes above. The `reasoning`/
                # `reasoning_effort` fields (if either was also sent)
                # stay untouched — this is purely "this one extra field
                # isn't recognised", not a rejection of thinking-control
                # in general.
                _has_zai_thinking_field = (
                    isinstance(body, dict) and "thinking" in body
                )
                if (e.code == 400 and not _zai_thinking_stripped
                        and _has_zai_thinking_field
                        and _looks_like_unknown_zai_thinking_field(detail)):
                    _zai_thinking_stripped = True
                    mark_zai_thinking_unsupported(url, body.get("model"))
                    body = {k: v for k, v in body.items() if k != "thinking"}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    msg = (
                        f"HTTP 400 from {url}: provider rejected the "
                        f"top-level 'thinking' field (Z.ai/GLM-style "
                        f"thinking-control) — disabling it for this "
                        f"endpoint for the rest of the run and retrying "
                        f"once without it."
                    )
                    logger.warning("request_completion: %s", msg)
                    if on_retry:
                        on_retry(msg)
                    continue

                # AUTO-THINKDEPTH-1: endpoint rejected a string depth value
                # in Ollama's "think" field ([api] think_effort_enabled =
                # true) — degrade to the plain boolean, which is
                # independently known-good (this is NOT a rejection of
                # thinking-control itself, just of the depth-string
                # variant), retry ONCE immediately, remember the endpoint
                # so future build_chat_request() calls send the boolean
                # straight away, and warn loudly.
                _think_val = isinstance(body, dict) and body.get("think")
                _has_think_depth_field = isinstance(_think_val, str)
                if (e.code == 400 and not _think_depth_stripped
                        and _has_think_depth_field
                        and _looks_like_unknown_think_depth_value(detail)):
                    _think_depth_stripped = True
                    mark_think_depth_unsupported(url, body.get("model"))
                    body = {**body, "think": True}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    msg = (
                        f"HTTP 400 from {url}: provider does NOT support a "
                        f"reasoning-depth string in the 'think' field "
                        f"([api] think_effort_enabled = true, think_effort "
                        f"= '{_think_val}') — falling back to plain "
                        f"think: true for this endpoint for the rest of "
                        f"the run and retrying once."
                    )
                    logger.warning("=" * 78)
                    logger.warning("request_completion: %s", msg)
                    logger.warning("=" * 78)
                    if on_retry:
                        on_retry(msg)
                    continue

                if not _is_retryable_status(e.code) or attempt >= error_retries:
                    raise RuntimeError(f"HTTP {e.code} from {url}: {detail or e.reason}") from None

                wait_s = _parse_retry_after(e, detail) if e.code == 429 else None
                if wait_s is None:
                    wait_s = error_retry_wait_sec

                if wait_s > max_retry_after_sec:
                    msg = (
                        f"HTTP {e.code} from {url}: server asked for a "
                        f"{wait_s:.0f}s wait, longer than the "
                        f"{max_retry_after_sec:.0f}s cap (looks like a daily/"
                        f"monthly quota reset, not a transient rate limit) "
                        f"— not waiting"
                    )
                    logger.warning("request_completion: %s", msg)
                    if on_retry:
                        on_retry(msg)
                    raise RuntimeError(f"HTTP {e.code} from {url}: {detail or e.reason}") from None

                msg = (
                    f"HTTP {e.code} from {url} ({detail or e.reason}), "
                    f"waiting {wait_s:.1f}s and retrying "
                    f"(attempt {attempt + 1}/{error_retries})"
                )
                logger.warning("request_completion: %s", msg)
                if on_retry:
                    on_retry(msg)
                sleep(wait_s)
                attempt += 1
            except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError) as e:
                # AUTO-FIX (medium-priority audit): these previously had no
                # explicit handling at all and propagated as a raw,
                # low-level exception with no indication of which provider/
                # URL was involved or how many attempts were made — easy to
                # misread as a bug in this module rather than a flaky
                # provider. Given several configured providers are free-tier
                # aggregators known to drop connections under load, this
                # reuses the same retry/backoff budget as the HTTP-status
                # path above rather than introducing a second, divergent
                # retry mechanism.
                if attempt >= error_retries:
                    raise RuntimeError(
                        f"{type(e).__name__} calling {url}: {e} "
                        f"(after {attempt} retr{'y' if attempt == 1 else 'ies'})"
                    ) from e
                msg = (
                    f"{type(e).__name__} calling {url}: {e}, waiting "
                    f"{error_retry_wait_sec:.1f}s and retrying "
                    f"(attempt {attempt + 1}/{error_retries})"
                )
                logger.warning("request_completion: %s", msg)
                if on_retry:
                    on_retry(msg)
                sleep(error_retry_wait_sec)
                attempt += 1

    if not stream:
        with _open() as response:
            raw = json.loads(response.read().decode("utf-8"))
            return _extract_content(raw, api_format)

    # ── Streaming ────────────────────────────────────────────────────────
    parts = []
    with _open() as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue

            if api_format == "ollama":
                # Ollama streams newline-delimited JSON objects
                # {"message": {"role": "assistant", "content": "tok"}, "done": false}
                try:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    done  = chunk.get("done", False)
                except json.JSONDecodeError:
                    continue
            else:
                # OpenAI SSE: "data: {...}" lines
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    # BUGFIX: some OpenAI-COMPATIBLE backends (this branch
                    # supports any base_url speaking the openai format, not
                    # just literal OpenAI) send an SSE chunk with an EMPTY
                    # choices array — a real usage-reporting chunk shape
                    # OpenAI itself sends when stream_options.include_usage
                    # is set, and also seen from various proxy/gateway
                    # wrappers regardless of client request options.
                    # `chunk["choices"][0]` on an empty list raises
                    # IndexError, which the except clause below did NOT
                    # catch (only JSONDecodeError/KeyError) — so this chunk
                    # crashed the WHOLE streaming request with an unhandled
                    # exception, losing every token already accumulated in
                    # `parts`, rather than just being skipped like any other
                    # unparseable chunk. Reproduced directly:
                    #
                    #   lines = [..normal tokens.., '{"choices": [],
                    #            "usage": {...}}', "[DONE]"]
                    #   -> IndexError: list index out of range
                    #      (uncaught, propagates out of request_completion)
                    choices = chunk.get("choices") or []
                    token = choices[0]["delta"].get("content", "") if choices else ""
                    done  = False
                except (json.JSONDecodeError, KeyError):
                    continue

            if token:
                parts.append(token)
                if on_token is not None:
                    on_token(token)
            if done:
                break

    return "".join(parts).strip()
