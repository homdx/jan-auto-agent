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
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# HTTP retry defaults; the same numbers are the fallback when the .ini has no [loop] retry keys (AUTO-RATE-1).
DEFAULT_ERROR_RETRIES = 60
DEFAULT_ERROR_RETRY_WAIT_SEC = 10.0
DEFAULT_MAX_RETRY_AFTER_SEC = 180.0


@dataclass
class CompletionMeta:
    """RUN-9: what a completed chat call actually did, alongside its text.

    Collected by ``request_completion`` on both branches (blocking and
    streaming) and handed to the caller through ``on_meta`` /
    ``request_completion_ex``. It is what lets Gate 1 tell an *empty reply
    that spent the whole budget* (exhaustion — more tokens buys more silence)
    from an *empty reply that never got the chance to say anything* (a
    degraded provider: HTTP 200, a role-only chunk or an empty stream). Until
    this existed the SSE branch kept only ``delta.content``, so both looked
    exactly like a garbled verdict.

    finish_reason:
        The last non-null one seen on the stream (``choices[0].finish_reason``;
        Ollama ``done_reason``). ``None`` when the provider never sent one.
        ``"length"`` is the exhaustion signal.
    completion_tokens:
        ``usage.completion_tokens`` when the provider sent a usage object —
        on a stream only with ``stream_options.include_usage`` (see
        ``build_chat_request``); Ollama ``eval_count``. ``None`` is a legal
        value: a gateway that omits usage still leaves the other signals.
    reasoning_chars:
        Characters of ``reasoning_content`` (or OpenRouter's ``reasoning``,
        Ollama's ``message.thinking``) streamed. ``> 0`` with empty content
        is either exhaustion (the budget went into the chain-of-thought) or a
        ``reasoning.exclude`` request the provider ignored.
    content_chunks:
        Chunks that carried non-empty content. ``0`` with no finish reason
        and no token count is the degraded-provider shape.
    elapsed:
        Wall-clock seconds from entry to return, retries included.
    """

    finish_reason: "str | None" = None
    completion_tokens: "int | None" = None
    reasoning_chars: int = 0
    content_chunks: int = 0
    elapsed: float = 0.0

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Per (url, model) memory: a model that rejects the reasoning field is not sent it again (AUTO-REASONING-2).
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


# Per (url, model) memory for the flat reasoning_effort field; the three-tier cascade falls back on rejection (AUTO-THINKDEPTH-2).
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


# Per (url, model) memory for the JSON-mode field; a rejected field is dropped for the rest of the process (AUTO-JSONMODE-1).
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


# Per (url, model) memory for the Ollama think depth, the same way as the other memories (AUTO-THINKDEPTH-1).
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


# Per (url, model) memory for Z.ai's top-level thinking field; sent additively, dropped on rejection (AUTO-ZAITHINK-1).
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


# Per (url, model) memory for stream_options include_usage; a rejection costs one call per run (RUN-9).
_STREAM_OPTIONS_UNSUPPORTED_KEYS: set = set()


def mark_stream_options_unsupported(url: str, model=None) -> None:
    """Record that *model* on *url* rejects `stream_options`, so future
    `build_chat_request(stream=True)` calls for this exact (url, model) pair
    never send it again this process. Idempotent; safe from any thread."""
    _STREAM_OPTIONS_UNSUPPORTED_KEYS.add((url, model))


def stream_options_is_supported(url: str, model=None) -> bool:
    """`False` once `mark_stream_options_unsupported(url, model)` has fired
    for this exact (url, model) pair in this process; `True` otherwise
    (including for pairs never tried)."""
    return (url, model) not in _STREAM_OPTIONS_UNSUPPORTED_KEYS


# Matches an api_key assignment anywhere in a line, commented or not, so a
# pasted secret is masked before the LLM sees it (MASK-KEY-1). The key name is
# its own group, so `mask_api_key` can tell a pass-through assignment
# (`self.api_key = api_key`) from a literal value (HARDEN-1 A8).
#
# HARDEN-5 B: neither the line start nor the line end is required. The old
# `^…$` anchors meant the value had to be the last thing on the line, so the
# common shapes — a trailing `#`/`;` comment (`api_key = sk-live-…  # prod`), a
# value inside prose (`Set api_key = 'sk-live-…' in agents.ini`) — leaked.
# The value is a quoted string (quotes kept when it is masked) or a run of
# non-space characters that stops at a space, a `#` or a `;`.
_API_KEY_LINE_RE = re.compile(
    r'(?i)([ \t]*(?:[#;]+[ \t]*)?(?:export[ \t]+)?)([\w.-]*api[_-]?key)([ \t]*=[ \t]*)'
    r"(\"[^\"]*\"|'[^']*'|[^\s'\";]+)([ \t]*\r?)"
)
# the same key in JSON: "api_key": "<value>", "openai_api_key": "<value>"
_API_KEY_JSON_RE = re.compile(r'(?i)("[\w.-]*api[_-]?key"[ \t]*:[ \t]*")[^"\r\n]*(")')
# The same key in YAML/INI colon form, UNQUOTED: `api_key: <value>`, indented or
# bullet-led (`  - api_key: <value>`), commented or not. `_API_KEY_LINE_RE` wants an
# `=` and `_API_KEY_JSON_RE` wants a quoted value, so this is the one shape neither
# reaches (HARDEN-9 B): a hex key, a UUID, a base64 string, an AWS key or a GitHub
# token after a colon all left the machine. The value stops at a space, a `#`, a YAML
# flow separator or a closing bracket, and never at a `=` — so `api_key: Optional[str]
# = None` yields the annotation, not the tail.
_API_KEY_COLON_RE = re.compile(
    r'(?i)([ \t]*(?:[#;]+[ \t]*)?(?:[-*][ \t]+)?)([\w.-]*api[_-]?key)([ \t]*:[ \t]*)'
    r"(\"[^\"]*\"|'[^']*'|[^#\s;\"'\,\}\]\)\|=&]+)"
)
# A bare Python/JS identifier: a NAME, i.e. a reference to something, never a literal secret.
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Characters that make a value an expression rather than a literal token: a call, an
# index, an attribute, a container or punctuation (`os.environ["X"]`, `config.api_key`).
# `.` is the one that needs a second look, since a dotted value is either an attribute
# path or a token — `_dotted_value_is_secret` settles it before this list is walked
# (HARDEN-9 A: a pasted JWT used to lose the line here).
_EXPRESSION_CHARS = "([{)]},;'\"."
#: The shortest value that may be a credential in its own right: a shorter bare name is a
#: variable or a type in either form, and a shorter dotted value is a path, not a token.
_TOKEN_MIN_LEN = 16
#: A dotted part that reads as a run of base64url: long, and mixed-case or digit-bearing,
#: so it is not a plain word (`settings`, `OPENAI_KEY`).
_BASE64URL_RUN_RE = re.compile(r"^[A-Za-z0-9_\-]{8,}$")
# A bare number: also code (`api_key = 30` is not a secret).
_NUMERIC_VALUE_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")
# Names that are never secrets (HARDEN-5 B: `self.api_key = None` was sent out as
# `self.api_key = here_your_key`, and a faithful rewrite raised a NameError).
_CODE_WORDS = frozenset({"none", "true", "false", "null", "undefined", "nil"})
_NON_ALNUM = re.compile(r"[^a-z0-9]")
# A template placeholder is an unset value, not a secret: `api_key = your_key`, `<your key>`,
# `changeme`, `xxx` are sent as they are (HARDEN-5 B), the same as the mask's own output.
_PLACEHOLDER_FLAT = frozenset({"yourkey", "yourapikey", "yourtoken", "changeme", "hereyourkey", "insertkeyhere",
                               "replaceme", "todo", "xxx", "xxxx", "xxxxx", "xxxxxx"})
# A `Bearer <token>` credential, wherever it sits in a line. The token must be long:
# the phrase is also plain English ("bearer of the torch"), so a short word is prose.
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)([^\s'\"`,;)}]{16,})")
# Provider key prefixes, wherever they sit in a line (HARDEN-5 B: `Authorization:
# Bearer sk-or-v1-…` leaked, since it is not an api_key assignment). The bare `sk-`
# form needs a long tail: `sk-real-1` is a placeholder in this repo's own sources,
# not a credential (test_harden1_a8 pins it as untouched).
_SECRET_PREFIX_RE = re.compile(
    r"\b(?:sk-or-v1-|sk-proj-)[A-Za-z0-9_\-]{10,}"
    r"|\bsk-[A-Za-z0-9_\-]{12,}"
    r"|\bAIza[A-Za-z0-9_\-]{20,}"
    r"|\bxoxb-[A-Za-z0-9_\-]{10,}"
)
_MASK_PLACEHOLDER = "here_your_key"


def _value_is_secret_literal(value: str, key_name: str, colon: bool = False) -> bool:
    """Whether the *value* of an ``api_key = …`` (or, with *colon*, ``api_key: …``)
    assignment is a literal that
    must be masked, as opposed to code that must be sent as it is.

    HARDEN-1 A8: the mask used to replace *whatever* followed the ``=``, so a
    request carrying a file the model is asked to rewrite (``/edit``, the
    Coder, which reads and writes whole files through this same request path)
    had its code rewritten for it — ``self.api_key = api_key`` went out as
    ``self.api_key = here_your_key``, and a faithful edit wrote the
    placeholder back, turning working code into a ``NameError`` nothing
    catches. Across this repo's own sources the old pattern matched hundreds
    of lines.

    HARDEN-5 B: A8 kept only the key's *exact* own name (``self.api_key =
    api_key``, ``_API_KEY``) and still masked every other identifier —
    ``self.api_key = None`` and ``API_KEY = DEFAULT_API_KEY`` went out as
    ``here_your_key``. An identifier that names the key it is assigned to, in
    either direction (``key`` inside ``api_key``, ``DEFAULT_API_KEY`` outside
    ``API_KEY``), is a reference, not a literal; so is a language constant.
    A placeholder like ``test_or_key`` still does not name the key, so it is
    still masked, and a masked value never survives a second pass.

    The intent is to keep a real secret out of the request without touching
    code, so only a *literal* is masked:

    * a quoted string (``api_key = "k1"``, ``"api_key": "k1"``) — always a literal;
    * a bare token with no whitespace and no call, index, attribute or
      punctuation character (``api_key = sk-abc123``) — a key-shaped literal;
    * anything else is an expression (``os.environ["OPENAI_API_KEY"]``,
      ``config.api_key``, ``get_key()``), a bare number, a language constant, or a
      name for the key itself — and is sent verbatim;
    * a dotted value that is a token — a JWT or ``ya29.a0AfH6…`` (HARDEN-9 A) — is a
      literal, while a path of words stays code;
    * with *colon* (the unquoted YAML form, HARDEN-9 B) a bare identifier counts only
      when it is long enough to be a credential and token-shaped, since on that form a
      short identifier is a type annotation or a parameter name.
    """
    value = (value or "").strip()
    if not value:
        return False
    # A quoted string is a literal, whatever is inside it — except empty,
    # which is a default value in code (`api_key = ''`), not a secret.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1].strip()
        return bool(inner) and not _is_template_placeholder(inner)
    if _is_template_placeholder(value):
        return False
    if any(ch.isspace() for ch in value):
        return False
    if "." in value:
        # HARDEN-9 A: a dotted value is a token as often as it is an attribute
        # path, so `.` alone must not decide between them.
        return _dotted_value_is_secret(value)
    if any(ch in value for ch in _EXPRESSION_CHARS):
        return False
    if _NUMERIC_VALUE_RE.match(value):
        return False
    if _BARE_IDENTIFIER_RE.match(value):
        if value.strip("_").lower() in _CODE_WORDS:
            return False
        if colon:
            # HARDEN-9 B: on the colon form a bare identifier is a type or a
            # parameter name as often as it is a value, so only one that is long
            # enough to be a credential and token-shaped in its letters counts.
            return (len(value) >= _TOKEN_MIN_LEN and _looks_like_a_token(value)
                    and not _names_the_same_key(value, key_name))
        return not _names_the_same_key(value, key_name)
    # Not an identifier: an opaque bare token (sk-abc123, a long hex string) — a key.
    return True


def _looks_like_a_token(value: str) -> bool:
    """Whether a bare identifier reads as a credential rather than a name.

    A credential's letters are not all one case and not all one kind: ``AKIA…7…``,
    ``ghp_abc…123``, ``dGhpc2lz…MTIz``. A name is ``some_long_variable`` or a type.
    """
    return any(ch.isdigit() for ch in value) or (
        any(ch.isupper() for ch in value) and any(ch.islower() for ch in value))


def _part_mixes_letters_and_digits(part: str) -> bool:
    return any(ch.isalpha() for ch in part) and any(ch.isdigit() for ch in part)


def _is_base64url_run(part: str) -> bool:
    """Whether *part* reads as a run of base64url rather than a word: long enough
    to be a token piece, and its letters not one uniform case without a digit."""
    if not _BASE64URL_RUN_RE.match(part):
        return False
    return _looks_like_a_token(part)


def _dotted_value_is_secret(value: str) -> bool:
    """Whether a dotted bare *value* is a credential rather than code (HARDEN-9 A).

    ``_EXPRESSION_CHARS`` held `.` on the strength of ``config.api_key``, so every
    dotted value was read as an attribute path and a pasted JWT
    (``eyJhbGci…eyJzdWIi…SflKxw…``, three base64url runs) or a Google OAuth token
    (``ya29.a0AfH6SMBx9…``) went out to the provider unchanged — the shape every
    ``.env`` and INI holds for a token. The same lines leaked before HARDEN-5, so
    this is an old gap, not a regression of it.

    Code reads as a path of names, so the literal is the rest of it: a token at
    least as long as one that is normally issued, whose parts either all mix
    letters with digits (a hex or alphanumeric run split on a dot) or hold at
    least two long base64url runs, as a JWT's three do. ``config.api_key``,
    ``self.settings.api_key``, ``os.environ.get`` and ``settings.OPENAI_KEY`` are
    paths of words and stay code.
    """
    if len(value) < _TOKEN_MIN_LEN:
        return False
    parts = value.split(".")
    if any(not part for part in parts):
        return False
    if all(_part_mixes_letters_and_digits(part) for part in parts):
        return True
    runs = sum(1 for part in parts if _is_base64url_run(part))
    return runs >= 2


def _is_template_placeholder(value: str) -> bool:
    """Whether *value* is an unset template value: ``<…>`` or a well-known stand-in."""
    value = value.strip()
    if value.startswith("<"):
        return True
    return _NON_ALNUM.sub("", value.lower()) in _PLACEHOLDER_FLAT


def _names_the_same_key(value: str, key_name: str) -> bool:
    """Whether the bare *value* names the key it is being assigned to.

    Case and separators are ignored, and either direction counts: ``key`` is a
    piece of ``api_key`` (``self.api_key = key`` is a parameter pass-through) and
    ``DEFAULT_API_KEY`` contains ``API_KEY``. ``test_or_key`` contains neither, so
    it stays a literal and is masked.
    """
    stem = key_name.rsplit(".", 1)[-1]
    flat_value = _NON_ALNUM.sub("", value.lower())
    flat_stem = _NON_ALNUM.sub("", stem.lower())
    return bool(flat_value and flat_stem and (flat_value in flat_stem or flat_stem in flat_value))


def _quote_of(value: str) -> str:
    """The quote character wrapping *value*, or ``""`` when it is unquoted."""
    return value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else ""


def mask_api_key(text: "str | None") -> "str | None":
    """Replace every ``api_key = <literal>`` in *text* with
    ``api_key = here_your_key``, leaving code on the right-hand side alone
    (HARDEN-1 A8 / HARDEN-5 B, see ``_value_is_secret_literal``).

    HARDEN-5 B: the secret is found where it sits, not only at the end of a
    line — a quoted value keeps its quotes, a trailing ``#``/``;`` comment is
    left alone, and a ``Bearer`` token or a known key prefix is masked anywhere
    in the text.

    HARDEN-9: the two shapes the two assignments above did not cover. A dotted
    token (a JWT, ``ya29.a0AfH6…``) was read as an attribute path, and an
    unquoted YAML value (``api_key: <hex>``) was not looked at at all — a
    literal after an ``=`` and after a ``:`` is one rule,
    ``_value_is_secret_literal``, so both are masked by it.

    This is a plain string transform with no knowledge of *which* key is
    "real" — it masks unconditionally so a secret embedded in file content
    that gets pulled into a prompt can't leak to the LLM. Non-string input
    (``None``, already-masked text, text with no such line) is returned
    unchanged.
    """
    if not text:
        return text

    def _mask_assignment(m: "re.Match") -> str:
        prefix, key_name, assign, value, suffix = m.groups()
        if not _value_is_secret_literal(value, key_name):
            return m.group(0)
        # A quoted value stays quoted: the masked text is what a faithful model
        # writes back, and an unquoted placeholder is a NameError waiting to
        # happen (`api_key = "x"` must not come back as `api_key = here_your_key`).
        quote = _quote_of(value)
        return f"{prefix}{key_name}{assign}{quote}{_MASK_PLACEHOLDER}{quote}{suffix}"

    def _mask_colon(m: "re.Match") -> str:
        prefix, key_name, assign, value = m.groups()
        # The colon form is the colon's own judge: a bare identifier there is a type
        # or a parameter name as often as it is a value (HARDEN-9 B).
        if not _value_is_secret_literal(value, key_name, colon=True):
            return m.group(0)
        if value.startswith("$"):
            # `api_key: $OPENAI_API_KEY` is a reference to an environment variable (a shell, a
            # compose file, a CI config), the name of a secret and not the secret itself.
            return m.group(0)
        quote = _quote_of(value)
        return f"{prefix}{key_name}{assign}{quote}{_MASK_PLACEHOLDER}{quote}"

    text = _API_KEY_LINE_RE.sub(_mask_assignment, text)
    text = _API_KEY_COLON_RE.sub(_mask_colon, text)
    text = _API_KEY_JSON_RE.sub(r"\1" + _MASK_PLACEHOLDER + r"\2", text)
    text = _BEARER_TOKEN_RE.sub(r"\1" + _MASK_PLACEHOLDER, text)
    return _SECRET_PREFIX_RE.sub(_MASK_PLACEHOLDER, text)


def restore_masked_secrets(original: "str | None", reply: "str | None") -> "str | None":
    """Put a real secret back where the model gave its masked form back.

    The request carries ``mask_api_key(file)``, so a faithful model returns the
    line as ``api_key = "here_your_key"`` and a writer that stores the reply
    replaces the real key on disk with the placeholder (HARDEN-5 B: the file
    must be byte-identical except the lines the task asked for). A reply line
    that equals the masked form of an *original* line that the mask changed is
    that original line again; a line the model edited does not equal it and is
    kept as written. Two lines that mask to the same text are restored in order.
    """
    if not original or not reply:
        return reply
    queues: "dict[str, list[str]]" = {}
    for line in original.splitlines():
        if "\ufffd" in line:
            continue
        masked = mask_api_key(line)
        if masked != line:
            queues.setdefault(masked, []).append(line)
    if not queues:
        return reply
    out = []
    for line in reply.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        queue = queues.get(body)
        if queue:
            out.append(queue.pop(0) + line[len(body):])
        else:
            out.append(line)
    return "".join(out)


def strip_think(text: "str | None") -> "str | None":
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


def _not_an_object_error(where: str, value, keys_desc: str) -> ValueError:
    """Build the malformed-reply ValueError for a `message` (or choice) field
    that is present but is not a JSON object.

    A missing key raises KeyError, which the guards below already convert to
    this same descriptive ValueError — so callers' retry ladders see one
    exception class either way. Indexing a non-object raises TypeError
    instead, which is NOT a KeyError and escaped the guard: that is what
    ``{"message": null}`` did before this helper existed.
    """
    return ValueError(
        f"LLM response's {where} is not a JSON object (got "
        f"{type(value).__name__}) — malformed reply from the backend "
        f"({keys_desc})"
    )


def _content_to_text(content, subject: str) -> str:
    """Return a message's ``content`` as text, or raise ValueError.

    ``content`` may legitimately be JSON ``null`` (an empty reply). It may
    also arrive as a LIST — the OpenAI multimodal shape, which some gateways
    relay verbatim — or as any other non-string. ``(content or "").strip()``
    raised ``AttributeError`` on those: neither a KeyError nor a ValueError,
    so it escaped this module's error convention with no diagnostic.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # A list of text and image parts is a valid reply; its text parts are joined (OpenAI multimodal).
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if parts:
            return "".join(parts).strip()
        if not content:
            return ""
    raise ValueError(
        f"{subject}'s 'content' is not a string (got "
        f"{type(content).__name__}) — malformed reply from the backend"
    )


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

    BUGFIX: the same two accesses also had to handle `message: null`
    (present, not an object). `raw["message"]["content"]` then raised
    `TypeError: 'NoneType' object is not subscriptable` — not a KeyError, so
    it escaped the guard below and aborted the call instead of taking the
    descriptive-ValueError path every caller's retry ladder already handles.
    Both dict accesses now isinstance-check the `message` field first and
    raise the same ValueError.
    """
    # A reply without a message key raises a descriptive ValueError, not a bare KeyError (AUTO-FIX).
    if not isinstance(raw, dict):
        preview = repr(raw)
        if len(preview) > 120:
            preview = preview[:117] + "..."
        raise ValueError(
            f"LLM response is not a JSON object (got {type(raw).__name__}: "
            f"{preview}) — malformed reply from the backend"
        )
    if api_format == "ollama":
        message = raw.get("message")
        if not isinstance(message, dict):
            if "message" in raw:
                # A present but null message (or a string or list) is a malformed reply, not a KeyError.
                raise _not_an_object_error("message", message,
                                           f"raw keys: {list(raw.keys())}")
            raise ValueError(
                "LLM response missing expected key 'message' — raw response "
                f"shape was unexpected (raw keys: {list(raw.keys())})"
            )
        try:
            return _content_to_text(message["content"], "LLM response")
        except KeyError as exc:
            raise ValueError(
                f"LLM response missing expected key {exc} — raw response "
                f"shape was unexpected (raw keys: {list(raw.keys())})"
            ) from exc
    # A non-streaming reply with an empty choices list raises a ValueError, not an IndexError (BUGFIX).
    choices = raw.get("choices") or []
    if not choices:
        raise ValueError(
            f"LLM response had no choices — likely blocked/filtered by the "
            f"backend (raw keys: {list(raw.keys())})"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _not_an_object_error(
            "first choice", choice, f"raw keys: {list(raw.keys())}"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        if "message" in choice:
            raise _not_an_object_error(
                "first choice's message", message, f"choice keys: {list(choice.keys())}"
            )
        raise ValueError(
            "LLM response's first choice is missing expected key 'message' — "
            f"raw response shape was unexpected (choice keys: {list(choice.keys())})"
        )
    try:
        return _content_to_text(message["content"], "LLM response's first choice")
    except KeyError as exc:
        raise ValueError(
            f"LLM response's first choice is missing expected key {exc} — "
            f"raw response shape was unexpected (choice keys: "
            f"{list(choice.keys())})"
        ) from exc


def _reasoning_text(container) -> str:
    """RUN-9: the reasoning string a delta/message carries, or ``""``.

    ``reasoning_content`` is the DeepSeek/vLLM/SGLang field the ticket names;
    ``reasoning`` is what OpenRouter-style gateways (the free-tier routers this
    project targets) stream instead; ``thinking`` is Ollama's. Only string
    values count — OpenRouter's request-side ``reasoning`` is an object and
    must not be mistaken for streamed text.
    """
    if not isinstance(container, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _as_int_or_none(value) -> "int | None":
    """RUN-9: ``usage.completion_tokens`` / ``eval_count`` as an int, or
    ``None`` for anything that is not a number (``bool`` included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _meta_from_chunk(chunk, meta: CompletionMeta, api_format: str) -> None:
    """RUN-9: fold one streamed chunk into *meta*, in place. Never raises —
    a non-standard chunk must not turn a delivered stream into an exception.

    Reads only shapes both branches of ``request_completion`` already
    tolerate (null ``delta``, empty ``choices``, null ``message``). The usage
    chunk OpenAI sends with ``include_usage`` has an EMPTY ``choices`` array,
    so ``usage`` is read before and independently of ``choices``; a
    ``finish_reason`` is only ever overwritten by a non-null one, so a
    trailing usage chunk cannot erase the ``"stop"`` that came before it.
    """
    try:
        if not isinstance(chunk, dict):
            return
        if api_format == "ollama":
            meta.reasoning_chars += len(_reasoning_text(chunk.get("message")))
            done_reason = chunk.get("done_reason")
            if isinstance(done_reason, str) and done_reason:
                meta.finish_reason = done_reason
            eval_count = _as_int_or_none(chunk.get("eval_count"))
            if eval_count is not None:
                meta.completion_tokens = eval_count
            return
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            completion_tokens = _as_int_or_none(usage.get("completion_tokens"))
            if completion_tokens is not None:
                meta.completion_tokens = completion_tokens
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return
        finish_reason = choices[0].get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            meta.finish_reason = finish_reason
        meta.reasoning_chars += len(_reasoning_text(choices[0].get("delta")))
    except Exception:  # noqa: BLE001 — metadata must never break a delivered stream
        pass


def _response_meta(raw: dict, api_format: str, *, content: str, elapsed: float) -> CompletionMeta:
    """RUN-9: ``CompletionMeta`` for a blocking (non-streamed) reply. Never
    raises — a reply shaped differently still returns the defaults."""
    meta = CompletionMeta(elapsed=elapsed, content_chunks=1 if content else 0)
    try:
        if api_format == "ollama":
            _meta_from_chunk(raw, meta, api_format)
            return meta
        usage = raw.get("usage")
        if isinstance(usage, dict):
            meta.completion_tokens = _as_int_or_none(usage.get("completion_tokens"))
        choices = raw.get("choices") or []
        if choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                meta.finish_reason = finish_reason
            meta.reasoning_chars = len(_reasoning_text(choices[0].get("message")))
    except Exception:  # noqa: BLE001 — see above
        pass
    return meta


def _notify_meta(on_meta, meta: CompletionMeta) -> None:
    """RUN-9: hand *meta* to the caller's ``on_meta``, if any. A callback
    that raises is swallowed: metadata is diagnostic, and a caller bug must
    never turn a delivered reply into a failure the ladder then retries."""
    if on_meta is None:
        return
    try:
        on_meta(meta)
    except Exception:  # noqa: BLE001 — see above
        pass


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
            # max_tokens is moved to the Ollama option it means, since /api/chat ignores the top-level key (AUTO-BUG).
            _mt = body.pop("max_tokens")
            if _mt:
                options["num_predict"] = _mt
        if "num_ctx" in body:
            # num_ctx of 0 or less means the server default, so it is never forwarded.
            _nc = body.pop("num_ctx")
            if _nc:
                options["num_ctx"] = _nc
        if options:
            body["options"] = options
        body["stream"] = stream
        # stream_options is OpenAI-only; Ollama reports its usage without it and does not know the field.
        body.pop("stream_options", None)
        # The Ollama chat format needs no extra system-message handling.
    else:
        # num_ctx is Ollama-only, so it is dropped for OpenAI-compatible servers that reject unknown fields.
        body.pop("num_ctx", None)
        if stream:
            body["stream"] = True
        else:
            # stream_options is dropped on a blocking request, since OpenAI rejects it there (RUN-9).
            body.pop("stream_options", None)
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


_JSON_FENCE_OPEN_RE = re.compile(
    r"`{3,}json[A-Za-z0-9_+\-#.,]*(?=\s|$|[{\[\"])", re.IGNORECASE
)
# A fence label is the word after the opening backticks, ended by whitespace or the end of the line.
_FENCE_LABEL_RE = re.compile(
    r"`{3,}(?:[ \t]*[A-Za-z0-9_+\-#.,]*(?=\s|$|[{\[\"]))?"
)


def _is_json(text: str, strict: bool = True) -> bool:
    """Whether *text* parses as a JSON value.

    ``strict=False`` accepts the literal newlines and tabs a model emits
    *inside* a JSON string (``{"notes": "use:\n```python\n…\n```"}``), which
    strict JSON forbids but which every caller would hand straight to
    ``json.loads`` anyway. That is only a probe: a candidate that needs it is
    escaped on the way out (see ``_escape_raw_controls``), so the returned
    body is still strict-JSON.
    """
    try:
        json.loads(text, strict=strict)
        return True
    except ValueError:
        return False


_RAW_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _escape_raw_controls(text: str) -> str:
    """*text* with the control characters JSON forbids inside a string written
    as their escapes, so a body a model emitted with literal newlines inside a
    string still passes every caller's plain ``json.loads`` (HARDEN-1 A7).

    Only characters *inside* a string are touched: a literal newline between
    tokens is legal JSON whitespace, and escaping it would produce a stray
    backslash outside any string.
    """
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch < " ":
                out.append(_RAW_CONTROL_ESCAPES.get(ch, f"\\u{ord(ch):04x}"))
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _before_closing_fence(rest: str) -> str:
    """The body up to its closing fence: the first ``` before which the body parses as JSON (a ``` inside a JSON
    string — a model echoing a code block — cut the body short), else the first ``` as before.

    A body with literal newlines inside one of its strings parses too
    (HARDEN-1 A7): ``strict=False`` accepts them and ``_escape_raw_controls``
    writes them as escapes, so the returned body is strict JSON either way.
    """
    # A fence that was opened and never closed (truncation, or the model just omitted it): the body is the
    # whole rest, and it is complete JSON even though a string of it quotes a code fence.
    whole = rest.strip()
    if _is_json(whole, strict=False):
        return whole if _is_json(whole) else _escape_raw_controls(whole)
    first, start = None, 0
    while True:
        pos = rest.find("```", start)
        if pos < 0:
            break
        candidate = rest[:pos].strip()
        if first is None:
            first = candidate
        if _is_json(candidate, strict=False):
            return candidate if _is_json(candidate) else _escape_raw_controls(candidate)
        start = pos + 3
    return first if first is not None else rest.strip()


def strip_json_fence(text: str) -> str:
    """Strip a ```json ... ``` or ``` ... ``` fence wrapping a JSON blob, if present."""
    # HARDEN-1 A7: the whole reply is tried as JSON FIRST, before any fence is
    # cut. A bare reply that is already valid JSON is exactly that, even when
    # one of its strings quotes a code fence — the commonest such reply is a
    # verdict whose hint or a summary whose notes quotes a code sample:
    # {"purpose": "...", "notes": "Typical use:\n```python\nretry(fetch, 3)\n```"}.
    # Cutting at that inner fence returned 'python\nretry(fetch, 3)\n', so every
    # caller's json.loads failed: a Pass B summary became the "module left
    # without a summary" case, and a Gate 2 APPROVED verdict became "validator
    # unavailable", which re-runs Gate 2 `validator_unavailable_retries` times
    # and then discards a passing candidate. Only a reply that is not valid
    # JSON falls through to fence stripping below.
    if text:
        stripped = text.strip()
        if _is_json(stripped, strict=False):
            # Escaped only when the body needs it, so a plain reply is returned byte-for-byte.
            return stripped if _is_json(stripped) else _escape_raw_controls(stripped)
    # The JSON fence is matched case-insensitively, and only the matched span is cut (BUGFIX).
    m = _JSON_FENCE_OPEN_RE.search(text)
    if m:
        rest = text[m.end():]
        # A lone opening fence is not a block; a closing fence is required first (AUTO-FIX).
        if "```" in rest:
            return _before_closing_fence(rest)
        # An unclosed fence returns the content after the marker, so complete JSON still parses (BUGFIX).
        return rest.strip()
    if "```" in text:
        # Any fence label is removed, not just json, so the body parses as JSON (BUGFIX).
        rest = text[_FENCE_LABEL_RE.search(text).end():]
        if "```" in rest:
            return _before_closing_fence(rest)
        # A single unmatched fence returns what follows it, for the same reason (BUGFIX).
        return rest.strip()
    return text


#: The spellings a model's yes/no field may carry for a yes (HARDEN-7). The
#: list is closed on purpose: anything not in it — a string such as "false",
#: "no" or "rejected" included — is a no, so a field read this way can only
#: ever lose an approval, never invent one.
_YES_SPELLINGS: tuple[str, ...] = ("true", "yes", "y", "1", "approved", "ok")


def reply_text(value) -> str:
    """Read one field of a model's JSON reply as text, whatever type it arrived as.

    HARDEN-7: a prompt asks for ``"feedback": "one concise sentence"`` and a
    small local model answers ``["too long", "no tests"]``, ``5`` or ``{"a": 1}``.
    Every caller then does ``(value or "").strip()``, which raises
    ``AttributeError: 'list' object has no attribute 'strip'`` on the first two
    and takes the run down with it — after the call has already been paid for.

    A string comes back stripped, unchanged otherwise, so a well-behaved reply
    is byte-identical to what the caller's own ``.strip()`` produced. A list is
    one item per line; any other value is its text; ``None`` is empty. Never
    raises, so the reader itself cannot be the thing that ends a run.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        # A list one item per line: the model meant several lines of text and
        # sent them as JSON. Nesting is flattened with the same rule.
        return "\n".join(reply_text(item) for item in value).strip()
    return str(value).strip()


def reply_is_yes(value) -> bool:
    """Read one field of a model's JSON reply as a yes/no, fail-closed.

    HARDEN-7 A: ``bool(parsed.get("approved", False))`` treats every non-empty
    string as ``True``, so Gate 2 — the last check before a commit — read
    ``{"approved": "false", "feedback": "REJECT: breaks the public API"}`` as an
    APPROVAL and committed a change its own validator had rejected.

    Only an explicit yes approves: the JSON ``true``, the number ``1``, or a
    string whose trimmed, lower-cased spelling is in ``_YES_SPELLINGS``. Every
    other value — a string such as ``"false"``, ``"no"`` or ``"rejected"``
    included, and any number that is not 1 — is a no.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _YES_SPELLINGS
    # A list or an object is not a verdict in any spelling: a no.
    return False


def make_unverified_context() -> ssl.SSLContext:
    """Return an SSLContext that skips certificate verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Retry on rate limits and transient errors; a valid task is not dropped by a 429 (AUTO-RATE-1).
_RETRY_AFTER_MS_RE = re.compile(r"(?:try again|retry)\s+in\s+([\d.]+)\s*ms", re.IGNORECASE)
_RETRY_AFTER_S_RE = re.compile(r"(?:try again|retry)\s+in\s+([\d.]+)\s*s(?:econds?)?\b", re.IGNORECASE)

# Retry on 429, 402 and 5xx only; other 4xx codes cannot be fixed by waiting.
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

    # Retry budget from [loop]; empty by default, so an object built without __init__ keeps the old behaviour (RUN-10).
    _retry_kwargs: dict = {}

    def __init__(self, config, base_url: str, api_key: str, model: str,
                 api_format: str = "openai", verify_ssl: bool = True) -> None:
        # The [loop] retry budget is resolved once here and passed to every request_completion call (RUN-10).
        self._retry_kwargs = retry_kwargs_from_config(config)
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
    stream: bool = False,
) -> tuple[str, dict, dict]:
    """
    Build the (url, headers, payload) triple for a one-shot system/user chat
    call, branching on *api_format* ("ollama" vs an openai-compatible API).

    RUN-9: *stream*, when ``True``, adds ``stream_options: {"include_usage":
    true}`` to the openai-branch payload so the streamed reply ends with a
    ``usage`` chunk — the ``completion_tokens`` a caller needs to tell "the
    model exhausted its budget and said nothing" from "the provider returned
    HTTP 200 with an empty stream" (``request_completion``'s ``on_meta``).
    Gated by ``stream_options_is_supported(url, model)``: a gateway that
    rejects the field with HTTP 400 has it stripped for that exact (url,
    model) pair for the rest of the process, same mechanics as the JSON-mode
    cache, so one unsupported gateway costs one failed call rather than
    every call. Ollama never gets it (``eval_count`` arrives on its own).
    Defaults to ``False``: every existing caller builds a byte-identical
    payload to before this parameter existed.

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
    # Masks any api_key line in the messages, but not the api_key parameter that authenticates the call (MASK-KEY-1).
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
        # A trailing slash on base_url is removed, so the endpoint path has no double slash (AUTO-URL-1).
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": messages,
        }
        # reasoning (OpenRouter) is the only field sent here; a provider that rejects it is kept per (url, model).
        if think is False and reasoning_field_is_supported(url, model):
            payload["reasoning"] = {"effort": "low", "exclude": True}
        elif think is True and think_effort:
            # Reasoning effort is tried as reasoning_effort, then as reasoning, then not at all (AUTO-THINKDEPTH-2).
            if reasoning_effort_toplevel_is_supported(url, model):
                payload["reasoning_effort"] = think_effort
            elif reasoning_field_is_supported(url, model):
                payload["reasoning"] = {"effort": think_effort}
            # Both tiers rejected for this URL: nothing is sent, so the model uses plain thinking (tier 3).
        if think is not None and zai_thinking_is_supported(url, model):
            # Z.ai thinking is sent in addition to the others, and a provider that ignores it is not harmed (AUTO-ZAITHINK-1).
            payload["thinking"] = {"type": "enabled" if think else "disabled"}
        if response_format and response_format_is_supported(url, model):
            payload["response_format"] = {"type": "json_object"}
        if stream and stream_options_is_supported(url, model):
            # A trailing usage chunk is requested on streams, unless this endpoint has refused it (RUN-9).
            payload["stream_options"] = {"include_usage": True}
    return url, headers, payload


def _retry_value(config, section: str, key: str, default, cast):
    """Read one retry knob out of *section*; never raises.

    A *missing* config, section or key gives *default* — that is what a
    config without the keys gets today, so adding the keys changes nothing
    for existing profiles. A *present but unparseable* value logs the RUN-9
    warning (``config [loop] error_retries is malformed (…) — using default
    60``) and falls back to that one key's default only, leaving the other
    two keys intact. A negative value is clamped to zero: a budget of "-5"
    attempts or "-1.0" seconds is nonsense, not a request to skip retrying.
    """
    if config is None:
        return default
    try:
        raw = config.get(section, key, fallback=None)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning(
            "config [%s] %s could not be read (%s) — using default %s",
            section, key, exc, default,
        )
        return default
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "config [%s] %s is malformed (%s) — using default %s",
            section, key, exc, default,
        )
        return default
    # A type-matched floor: max(0, 0.0) returns the int 0, which would silently turn a float knob into an int.
    return max(0 if cast is int else 0.0, value)


def retry_kwargs_from_config(config, section: str = "loop") -> dict:
    """The auto-mode HTTP retry budget, resolved once from *section*.

    RUN-10: request_completion() has carried its own retry budget since
    AUTO-RATE-1 — 60 extra attempts, 10 s apart, on a Retry-After capped at
    180 s — and every auto-mode caller (architect, Gate 1, coder, Gate 2
    validator, story bible, summary memory) silently inherited it. This is
    the one reader for the three [loop] keys that make it visible, read the
    same way [loop] timeout_seconds already is:

        retry_kwargs = retry_kwargs_from_config(config)
        _llm_stream.request_completion(url, headers, payload,
                                       timeout=self._timeout, **retry_kwargs)

    Returns ``{"error_retries": int, "error_retry_wait_sec": float,
    "max_retry_after_sec": float}`` — splat it straight into
    request_completion(). [collect] is deliberately NOT read here: the Pass B
    summarizer has its own three keys and defaults (2 / 60 / 180).
    """
    return {
        "error_retries": _retry_value(
            config, section, "error_retries", DEFAULT_ERROR_RETRIES, int),
        "error_retry_wait_sec": _retry_value(
            config, section, "error_retry_wait_sec",
            DEFAULT_ERROR_RETRY_WAIT_SEC, float),
        "max_retry_after_sec": _retry_value(
            config, section, "max_retry_after_sec",
            DEFAULT_MAX_RETRY_AFTER_SEC, float),
    }


def request_completion(url, headers, payload, timeout, stream=False, on_token=None,
                       api_format: str = "openai", ssl_context: "ssl.SSLContext | None" = None,
                       error_retries: int = DEFAULT_ERROR_RETRIES,
                       error_retry_wait_sec: float = DEFAULT_ERROR_RETRY_WAIT_SEC,
                       max_retry_after_sec: float = DEFAULT_MAX_RETRY_AFTER_SEC,
                       on_retry=None,
                       _sleep_fn=None, on_meta=None, deadline: "float | None" = None):
    """
    POST a chat-completions request and return the assistant message text.

    *deadline* (a ``time.monotonic()`` value, None by default: no change for any
    caller that does not pass it) bounds the WHOLE call, retries included: no
    attempt starts past it, no attempt's socket timeout reaches beyond it, and a
    retry whose wait would end past it is not made — the call raises
    ``RuntimeError`` instead. ``timeout`` alone bounds one attempt, and with the
    default retry budget a provider that accepts the connection and never answers
    held one call for 61 × timeout + 60 × 10 s (about 4 h at 240 s; CC-10's Gate 1
    reader and its voters, bugs R3 and G4).

    RUN-9: *on_meta*, if given, is called once with the ``CompletionMeta`` of
    the reply being returned (``finish_reason``, ``completion_tokens`` or
    ``None``, ``reasoning_chars``, ``content_chunks``, ``elapsed``) — on both
    branches, right before the return. ``None`` (the default) keeps every
    existing caller byte-for-byte as before: the return value is still the
    plain ``str``. ``request_completion_ex`` is the same call returning the
    metadata alongside the text. A callback that raises is swallowed.

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
    # Secrets are masked last, right before the body is serialised (MASK-KEY-1).
    payload = _mask_payload_secrets(payload)
    body = _build_payload(payload, api_format, stream)

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    _sleep = _sleep_fn or time.sleep

    def _left() -> "float | None":
        return None if deadline is None else deadline - time.monotonic()

    def sleep(wait_s: float) -> None:
        # the deadline: a retry that would start past it is not made
        left = _left()
        if left is not None and left <= wait_s:
            raise RuntimeError(f"deadline reached calling {url}: not retrying "
                               f"(a wait of {wait_s:.1f}s with {max(0.0, left):.1f}s left)")
        _sleep(wait_s)

    def retry_after(msg: str, wait_s: float) -> None:
        # The deadline is checked before the log line, so a retry refused at once is not logged as waiting.
        left = _left()
        if left is not None and left <= wait_s:
            sleep(wait_s)                       # raises: the deadline line is the one logged
        logger.warning("request_completion: %s", msg)
        if on_retry:
            on_retry(msg)
        sleep(wait_s)

    def _check_deadline(what: str) -> None:
        # The deadline bounds the whole reply, not only the gap between reads.
        left = _left()
        if left is not None and left <= 0:
            raise RuntimeError(f"deadline reached reading {what} from {url}: stopped after "
                               f"{time.monotonic() - _started:.1f}s, the reply still coming")

    def _read_body(response) -> bytes:
        if deadline is None:
            return response.read()
        read1 = getattr(response, "read1", None)
        if read1 is None:                       # a stand-in response with no read1: read it whole
            return response.read()
        chunks = []
        while True:
            chunk = read1(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            _check_deadline("the response body")

    def _attempt_timeout() -> float:
        left = _left()
        if left is None:
            return timeout
        if left <= 0:
            raise RuntimeError(f"deadline reached before calling {url}")
        return min(timeout, left) if timeout else left
    # CompletionMeta.elapsed counts from before the first attempt, so retries are included (RUN-9).
    _started = time.monotonic()

    # Strict endpoints reject the reasoning field with HTTP 400, so it is dropped once and the call is retried (AUTO-REASONING-1).
    _reasoning_stripped = False

    def _looks_like_unknown_reasoning_field(detail: str) -> bool:
        low = detail.lower()
        return "reasoning" in low and ("unknown name" in low or "cannot find field" in low
                                        or "unrecognized" in low or "not supported" in low or "unsupported" in low
                                        or "invalid" in low)

    # A rejected top-level reasoning_effort is dropped once and the call retried with the nested form (AUTO-THINKDEPTH-2).
    _reasoning_effort_toplevel_stripped = False

    def _looks_like_unknown_reasoning_effort_field(detail: str) -> bool:
        low = detail.lower()
        return "reasoning_effort" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    # A rejected string think depth is dropped once per call, without using the retry budget (AUTO-THINKDEPTH-1).
    _think_depth_stripped = False

    def _looks_like_unknown_think_depth_value(detail: str) -> bool:
        low = detail.lower()
        return "think" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low or "boolean" in low
        )

    # A rejected JSON-mode field is dropped once per call, without using the retry budget (AUTO-JSONMODE-1).
    _response_format_stripped = False

    def _looks_like_unknown_response_format_field(detail: str) -> bool:
        low = detail.lower()
        # A 400 that names the format field is read as a rejection of that field, not as another error (AUTO-JSONMODE-1).
        has_field_name = "response_format" in low or "format" in low
        return has_field_name and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    # A rejected thinking object is dropped once per call; the other reasoning keys stay (AUTO-ZAITHINK-1).
    _zai_thinking_stripped = False

    # A rejected stream_options is dropped once per call, without using the retry budget (RUN-9).
    _stream_options_stripped = False

    def _looks_like_unknown_stream_options_field(detail: str) -> bool:
        low = detail.lower()
        # Any mention of the stream option next to a 400 is a rejection of that option.
        return ("stream_options" in low or "include_usage" in low
                or "stream options" in low)

    def _looks_like_unknown_zai_thinking_field(detail: str) -> bool:
        low = detail.lower()
        return "thinking" in low and (
            "unknown name" in low or "cannot find field" in low
            or "unrecognized" in low or "not supported" in low or "unsupported" in low
            or "invalid" in low or "unexpected" in low
        )

    def _open():
        nonlocal body, req, _reasoning_stripped, _response_format_stripped, _think_depth_stripped, _reasoning_effort_toplevel_stripped, _zai_thinking_stripped, _stream_options_stripped
        attempt = 0
        while True:
            try:
                return urllib.request.urlopen(req, timeout=_attempt_timeout(), context=ssl_context)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass

                # Tier 1 rejected: drop reasoning_effort and retry with the nested reasoning form (AUTO-THINKDEPTH-2).
                if (e.code == 400 and not _reasoning_effort_toplevel_stripped
                        and isinstance(body, dict) and "reasoning_effort" in body
                        and _looks_like_unknown_reasoning_effort_field(detail)):
                    _reasoning_effort_toplevel_stripped = True
                    # The rejection is remembered per URL, so later calls skip tier 1 (AUTO-THINKDEPTH-2).
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

                # A strict provider's rejection of reasoning is dropped once, with no backoff (AUTO-REASONING-1).
                if (e.code == 400 and not _reasoning_stripped
                        and isinstance(body, dict) and "reasoning" in body
                        and _looks_like_unknown_reasoning_field(detail)):
                    _reasoning_stripped = True
                    # The rejection is remembered per URL, so later calls skip the field (AUTO-REASONING-2).
                    mark_reasoning_field_unsupported(url, body.get("model"))
                    # Logs which caller sent the reasoning field: think=false suppression or the depth hint.
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

                # A rejected JSON-mode field is dropped once; the endpoint is remembered and JSON is salvaged (AUTO-JSONMODE-1).
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

                # A rejected stream_options is dropped once; the reply simply has no usage (RUN-9).
                _has_stream_options_field = (
                    isinstance(body, dict) and "stream_options" in body
                )
                if (e.code == 400 and not _stream_options_stripped
                        and _has_stream_options_field
                        and _looks_like_unknown_stream_options_field(detail)):
                    _stream_options_stripped = True
                    mark_stream_options_unsupported(url, body.get("model"))
                    body = {k: v for k, v in body.items() if k != "stream_options"}
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST",
                    )
                    msg = (
                        f"HTTP 400 from {url}: provider does not support "
                        f"'stream_options' (streamed usage reporting, RUN-9) "
                        f"— disabling it for this endpoint for the rest of "
                        f"the run and retrying once without it. Replies "
                        f"from it will carry no token count; everything "
                        f"else is unchanged."
                    )
                    logger.warning("request_completion: %s", msg)
                    if on_retry:
                        on_retry(msg)
                    continue

                # A rejected thinking object is dropped once; the other reasoning fields are kept (AUTO-ZAITHINK-1).
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

                # A rejected string think depth is degraded to the boolean once, and remembered (AUTO-THINKDEPTH-1).
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
                retry_after(msg, wait_s)
                attempt += 1
            except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError) as e:
                # Connection-level errors reuse the HTTP retry budget, and are reported with the provider (AUTO-FIX).
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
                retry_after(msg, error_retry_wait_sec)
                attempt += 1

    if not stream:
        # The whole open and read is retried, since no partial result has reached the caller yet (BUGFIX).
        _read_attempt = 0
        while True:
            try:
                with _open() as response:
                    # A UTF-8 BOM is dropped before parsing, so json.loads does not fail on it (utf-8-sig).
                    raw = json.loads(_read_body(response).decode("utf-8-sig"))
            except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError,
                    ValueError) as e:
                # A truncated or garbled body is retried like a dropped connection (BUGFIX).
                if _read_attempt >= error_retries:
                    raise RuntimeError(
                        f"{type(e).__name__} reading response body from {url}: {e} "
                        f"(after {_read_attempt} retr{'y' if _read_attempt == 1 else 'ies'})"
                    ) from e
                msg = (
                    f"{type(e).__name__} reading response body from {url}: {e}, "
                    f"waiting {error_retry_wait_sec:.1f}s and retrying "
                    f"(attempt {_read_attempt + 1}/{error_retries})"
                )
                retry_after(msg, error_retry_wait_sec)
                _read_attempt += 1
            else:
                # Only read and parse are retried; a content-shape error reaches the caller at once.
                text = _extract_content(raw, api_format)
                # A blocking reply carries finish_reason and usage, so an empty reply is classifiable (RUN-9).
                _notify_meta(on_meta, _response_meta(
                    raw, api_format, content=text,
                    elapsed=time.monotonic() - _started,
                ))
                return text

    # ── Streaming ────────────────────────────────────────────────────────
    parts = []
    # What the stream did, collected chunk by chunk; an empty result is a legal outcome (RUN-9).
    meta = CompletionMeta()
    # The streaming read is retried too, but only before any token has been passed on (BUGFIX).
    _stream_attempt = 0
    while True:
        try:
            with _open() as response:
                for raw_line in response:
                    _check_deadline("the stream")
                    # A BOM on the first stream line is stripped, so that chunk is not dropped (utf-8-sig).
                    line = raw_line.decode("utf-8-sig").strip()
                    if not line:
                        continue

                    if api_format == "ollama":
                        # Ollama chunks may carry a null message; the null is treated as empty (BUGFIX).
                        try:
                            chunk = json.loads(line)
                            # A chunk that is not an object at all carries no token either (HARDEN-1 A2, as on the SSE branch).
                            if not isinstance(chunk, dict):
                                continue
                            token = (chunk.get("message") or {}).get("content", "")
                            done  = chunk.get("done", False)
                        except json.JSONDecodeError:
                            continue
                        _meta_from_chunk(chunk, meta, api_format)
                    else:
                        # OpenAI SSE: "data: {...}" lines
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            # A chunk that is not an object at all carries no token (HARDEN-1 A2).
                            if not isinstance(chunk, dict):
                                continue
                            # An empty choices list is skipped, not an IndexError that loses the reply (BUGFIX).
                            choices = chunk.get("choices") or []
                            # A null (or otherwise non-object) first choice is skipped too, exactly
                            # as `_meta_from_chunk` skips it (HARDEN-1 A2): `{"choices": [null]}` is
                            # a real gateway shape, and `None.get("delta")` raised AttributeError
                            # here — neither a JSONDecodeError nor a KeyError, so it escaped the
                            # guard below and killed a call that had already read every token,
                            # which the caller then retried and re-billed.
                            first = choices[0] if choices else None
                            # A null delta is treated as empty, so it does not raise AttributeError (BUGFIX).
                            token = (first.get("delta") or {}).get("content", "") if isinstance(first, dict) else ""
                            done  = False
                        except (json.JSONDecodeError, KeyError):
                            continue
                        _meta_from_chunk(chunk, meta, api_format)

                    if token:
                        parts.append(token)
                        # Counts only chunks that carry content, not role-only, reasoning or usage chunks (RUN-9).
                        meta.content_chunks += 1
                        if on_token is not None:
                            on_token(token)
                    if done:
                        break
            break
        except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError) as e:
            if parts or _stream_attempt >= error_retries:
                raise RuntimeError(
                    f"{type(e).__name__} reading stream from {url}: {e} "
                    f"(after {_stream_attempt} retr{'y' if _stream_attempt == 1 else 'ies'}"
                    f"{', tokens already emitted' if parts else ''})"
                ) from e
            msg = (
                f"{type(e).__name__} reading stream from {url}: {e}, waiting "
                f"{error_retry_wait_sec:.1f}s and retrying "
                f"(attempt {_stream_attempt + 1}/{error_retries})"
            )
            retry_after(msg, error_retry_wait_sec)
            _stream_attempt += 1

    meta.elapsed = time.monotonic() - _started
    _notify_meta(on_meta, meta)
    return "".join(parts).strip()


def request_completion_ex(*args, **kwargs) -> "tuple[str, CompletionMeta]":
    """RUN-9: ``request_completion`` plus its ``CompletionMeta``.

    The text is the exact string ``request_completion`` returns — only the
    metadata rides along. Implemented on top of ``request_completion`` (not
    the other way round) so a test that stubs ``tools.llm_stream.
    request_completion`` still intercepts every call made through here; such
    a stub reports no metadata, and the result is then
    ``(text, CompletionMeta())`` — a default-filled object a caller must read
    as "no evidence", never as "empty stream".
    """
    box: list = []

    def _capture(meta: CompletionMeta) -> None:
        if not box:
            box.append(meta)

    kwargs.pop("on_meta", None)
    text = request_completion(*args, **kwargs, on_meta=_capture)
    return text, (box[0] if box else CompletionMeta())
