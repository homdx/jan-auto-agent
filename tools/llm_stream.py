"""
tools/llm_stream.py

Single helper for calling the chat-completions endpoint either blocking or
streaming.  When streaming, each token is passed to `on_token` as it arrives
(so the caller can echo it live, like the direct-chat path does) AND
accumulated, so the full assistant message is still returned for JSON parsing.

This lets the validator / improvement agents show their answer being typed out
in real time while still receiving the complete text to json.loads() at the end.
"""

import difflib
import http.client
import json
import logging
import re
from collections import Counter
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

#: The transport failures every branch of `request_completion` retries on the same budget as a
#: retryable HTTP status: the network errors of AUTO-FIX, plus `http.client.HTTPException` -
#: the family that holds `IncompleteRead`, what a body shorter than its `Content-Length` (or a
#: chunked body cut after one chunk) raises instead of a `ConnectionError` (HARDEN-23 B). One
#: tuple, read by the three read loops, so a new transport error is added in one place.
_RETRYABLE_TRANSPORT_ERRORS = (
    TimeoutError, ssl.SSLError, ConnectionError,
    urllib.error.URLError, http.client.HTTPException,
)


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
#
# HARDEN-10 B: the `=` must be an assignment's, not one half of a comparison.
# `assert client.api_key == key` has two `=` in a row, and the second alternative
# of the value class matched the first one on its own — so the line went out as
# `assert client.api_key =here_your_key key`, code that does not parse. The
# `~=` of a match (`api_key =~ /re/`) is the same shape with a tilde ahead of the
# second `=`. A comparison's `=` is always preceded by `=`, `!`, `<`, `>`, `~`,
# `&` or `|`, and never followed by another `=`, a `~` or a `>`.
#
# HARDEN-12 B: a trailing `>` is the Ruby or PHP hash arrow, `api_key => 'gsk_…'`, and
# the value class ate the `>` on its own, so the line went out as
# `api_key =here_your_key '…'` — the comparison's `=` is the one this guard was written
# for, but the arrow is the other half of that shape. The `=>` is the colon rule's to
# take (`_API_KEY_COLON_RE`), which keeps the arrow and only touches the value.
# The string prefix of a quoted value (`r`, `b`, `f`, `u`, `rb`, …) is part of the
# value: without it the value class matched the prefix alone and left the quote pair
# behind, so a raw string went out with its quotes in the wrong place (HARDEN-10 B).
_API_KEY_LINE_RE = re.compile(
    r'(?i)((?:(?<![ \t])[ \t]+)?(?:[#;]+[ \t]*)?(?:export[ \t]+)?)((?<![\w.-])[\w.-]*api[_-]?key)'
    r'([ \t]*(?<![=!<>~&|])=(?![=~>])[ \t]*)'
    r"((?:[rbfuRBFU]{1,2})?(?:\"[^\"]*\"|'[^']*')|[^\s'\";&#]+)([ \t]*\r?)"
)
# The same key with a type annotation before its `=`: `api_key: str = "…"`, `apiKey: string = '…'`.
# Only a token-shaped literal is masked there (`_mask_annotated`): a short default is code.
_API_KEY_ANNOTATED_RE = re.compile(
    r'(?im)((?:^|[(,])[ \t]*(?:(?:val|var|let|const|readonly|public|private|protected|static|final|export)[ \t]+)*)'
    r'([\w.-]*api[_-]?key)([ \t]*:[ \t]*[A-Za-z_][\w.\[\], |]*?[ \t]*=(?!=)[ \t]*)'
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*')"
)
# the same key in JSON: "api_key": "<value>", "openai_api_key": "<value>"
# An empty value (`"api_key": ""`) is an unset key, not a secret.
_API_KEY_JSON_RE = re.compile(r'(?i)("[\w.-]*api[_-]?key"[ \t]*:[ \t]*")[^"\r\n]+(")')
# The same key in YAML/INI colon form, UNQUOTED: `api_key: <value>`, indented or
# bullet-led (`  - api_key: <value>`), commented or not. `_API_KEY_LINE_RE` wants an
# `=` and `_API_KEY_JSON_RE` wants a quoted value, so this is the one shape neither
# reaches (HARDEN-9 B): a hex key, a UUID, a base64 string, an AWS key or a GitHub
# token after a colon all left the machine. The value stops at a space, a `#`, a YAML
# flow separator or a closing bracket, and never at a `=` — so `api_key: Optional[str]
# = None` yields the annotation, not the tail.
#
# HARDEN-12 B widens the left-hand side and the separator, not the value rule: a key
# name may wear quotes (`{'api_key': 'gsk_…'}`, a Python dict written with single
# quotes — `_API_KEY_JSON_RE` takes a double-quoted name only), and the separator may
# be the Ruby or PHP hash arrow `=>` or a plain `=`, which is what left
# `api_key => 'gsk_…'` behind. The arrow alternative is tried before the `=` one, and
# the `=` one keeps HARDEN-10 B's guards, so `api_key >= '…'` and `api_key == '…'`
# are still comparisons and not assignments.
#
# HARDEN-13 C: the name may carry a space, `API key:` — the very same key written
# the way a sentence writes it. Only between `api` and `key`, so a name is not a
# run of prose words.
_API_KEY_COLON_RE = re.compile(
    r'(?i)((?:(?<![ \t])[ \t]+)?(?:[#;]+[ \t]*)?(?:[-*][ \t]+)?)'
    r"((?:['\"])?(?<![\w.-])[\w.-]*api[ _\-]?key(?:['\"])?)"
    r"([ \t]*(?:=>|(?<![=!<>~&|])=(?![=~>])|:)[ \t]*)"
    r"((?:[rbfuRBFU]{1,2})?(?:\"[^\"]*\"|'[^']*')|[^#\s;\"'\,\}\]\)\|=&]+)"
)
# HARDEN-12 B: a subscript on the left, `os.environ["GROQ_API_KEY"] = "gsk_…"` (or the
# single-quoted `os.environ['GROQ_API_KEY'] = '…'`). None of the rules above reaches it:
# `_API_KEY_LINE_RE` wants the *name* to hold `api_key`, and here the key name sits inside
# the brackets. Only a name that names a credential counts, so `os.environ["HOME"] = "x"`
# is left alone, and the value goes through `_value_is_secret_literal` like every other one
# — `os.environ["X_API_KEY"] = os.getenv("Y")` stays code.
#
# HARDEN-19 A: the container is no longer `environ` alone, and the name inside the brackets
# is not restricted to one that carries a credential word as a whole part — `STRIPEKEY` and
# `clientSecret` name credentials too, and CC review fix 40's rule is what threw them away.
# The pattern takes any name and asks `_name_is_a_credential`: `os.environ["HOME"] = "x"`,
# `process.env["HOME"] = "/root"`, `TOKENIZERS_PARALLELISM` and `KEYBOARD_LAYOUT` all stay.
_ENV_NAME_RE = r"[A-Za-z_][A-Za-z0-9_.\-]*"
_ENV_CONTAINER_RE = r"(?:\$\{?[A-Za-z_]\w*\}?|\w+(?:\.\w+)*)"
_ENV_LHS_VALUE_RE = r"((?:[rbfuRBFU]{1,2})?(?:\"[^\"]*\"|'[^']*')|[^\s'\";]+)"
_ENV_SUBSCRIPT_ASSIGN_RE = re.compile(
    r"(?i)((?:(?<![ \t])[ \t]+)?(?:[#;]+[ \t]*)?)"
    r"((?<![\w$.])" + _ENV_CONTAINER_RE + r")\[([\"'])"
    r"(" + _ENV_NAME_RE + r")\3\]"
    r"([ \t]*(?<![=!<>~&|])=(?![=~])[ \t]*)"
    + _ENV_LHS_VALUE_RE + r"([ \t]*\r?)"
)
# HARDEN-19 A: the same name written to a call rather than a subscript,
# `os.environ.setdefault("N", v)`, `os.environ.get("N", default)`,
# `System.setProperty("api.key", v)`, `os.putenv("N", v)`. The name is the first
# argument, the value the second, and the value is not a subscript's right-hand side, so
# it may not take a closing bracket. `get` and `getenv` are read-only with one argument —
# `System.getenv("api.key")` — and this pattern needs the comma, so they are read the way
# they are written: only the assigned default is looked at.
_ENV_SUBSCRIPT_CALL_RE = re.compile(
    r"(?i)((?:(?<![ \t])[ \t]+)?(?:[#;]+[ \t]*)?)"
    r"((?<![\w$.])" + _ENV_CONTAINER_RE + r")"
    r"((?:\.setdefault|\.get(?:env)?|\.setProperty|\.putenv)\()"
    r"([\"'])(" + _ENV_NAME_RE + r")\4([ \t]*,[ \t]*)"
    r"((?:[rbfuRBFU]{1,2})?(?:\"[^\"]*\"|'[^']*')|[^\s'\";)]+)"
)
# HARDEN-19 A: the attribute form of the same write, `process.env.STRIPEKEY = "…"` and
# `Bun.env.clientSecret = "…"`: the environment object, a dot, the name, an assignment. The
# generic assignment rules read the name by its underscore-separated words, so a name
# written as one word went through. The container must END in `env` (or `environ`) as a
# whole path part, so `dotenv.STRIPEKEY` and `self.environment.KEY` are not it.
_ENV_ATTR_ASSIGN_RE = re.compile(
    r"(?i)((?:(?<![ \t])[ \t]+)?(?:[#;]+[ \t]*)?)"
    r"((?<![\w$.])(?:[A-Za-z_$][\w$]*\.)*(?:env|environ))\."
    # HARDEN-23 C: a property name has no dot (the shared _ENV_NAME_RE allows one, for the quoted
    # subscript form). With the dot the name swallowed the rest of an `env.env.env.…` line and gave
    # it back one character at a time from every `env.` — quadratic on a long line.
    r"([A-Za-z_][A-Za-z0-9_\-]*)"
    r"([ \t]*(?<![=!<>~&|])=(?![=~])[ \t]*)"
    + _ENV_LHS_VALUE_RE + r"([ \t]*\r?)"
)
#: The object that holds environment variables, read off the tail of the container path:
#: `os.environ`, `process.env`, `ENV`, `$_ENV`, or the JVM's property store `System`.
#: The tail is `env` or `environ`, plain or with a `_` ahead of it (`_ENV`), so `dotenv`
#: and `FOOENV` are not it.
_ENV_CONTAINER_TAIL_RE = re.compile(r"(?i)(?:^|[_])(?:env|environ)$")


#: The module-level functions that write an environment value, or take a default for it:
#: `os.putenv("N", v)` assigns and `os.getenv("N", default)` holds one. There is no env
#: object in their path — the function name is the store.
_ENV_MODULE_CALL_RE = re.compile(r"(?i)\bos\.(?:putenv|getenv)\($")


def _container_is_a_credential_store(container: str) -> bool:
    """Whether *container* is the object that holds environment variables, whatever language
    wrote the line (HARDEN-19 A). Never raises on a malformed container."""
    if not container or not isinstance(container, str):
        return False
    tail = container.rsplit(".", 1)[-1].strip("${}")
    if tail.lower() == "system":
        return True
    return bool(_ENV_CONTAINER_TAIL_RE.search(tail))


def _is_an_environment_write(container: str, call: str = "") -> bool:
    """Whether a write goes to the environment at all (HARDEN-19 A). Never raises."""
    if _ENV_MODULE_CALL_RE.search((container or "") + (call or "")):
        return True
    return _container_is_a_credential_store(container)


# HARDEN-12 B: an XML element whose tag is a key name, `<api_key>gsk_…</api_key>`. The tag
# name carries the rule's job, the content is judged by `_value_is_secret_literal`, so
# `<api_key>Value</api_key>` stays as it is.
#
# HARDEN-13 C: the plain `<api_key>` shape never matched at all — the leading
# `[A-Za-z]` of the old name class ate the `a` of `api`, so only a *prefixed* name
# (`<openai_api_key>`, `<myapikey>`) could match and the commonest one of them all left
# the machine. The prefix is optional now, and `secret_key` is a key name too.
_API_KEY_XML_RE = re.compile(
    r"(?i)(<([\w.-]*(?:api[_-]?key|apikey|secret[_-]?key)[\w.-]*)[ \t]*>)"
    r"([^<\r\n]*)(</\2[ \t]*>)"
)
# HARDEN-13 C: a bare `<key>V</key>` element. The tag names no key, so it counts
# only when the element's own line or the element it sits inside does — found
# first, judged second (see `_mask_bare_key_element`).
_BARE_KEY_XML_RE = re.compile(
    r"(?i)(<key(?:[ \t]+[^<>]*)?>)([^<\r\n]*)(</key[ \t]*>)"
)
# HARDEN-13 C: an XML start tag, found first so its attributes can be walked as a
# pair — `key="api_key" value="V"` — before the value is judged at all.
_XML_START_TAG_RE = re.compile(r"<([A-Za-z][\w:.-]*)((?:[ \t]+[A-Za-z_][\w:.-]*"
                               r"(?:[ \t]*=[ \t]*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)[ \t]*/?>")
_XML_ATTR_RE = re.compile(r"(?<![\w:.-])([A-Za-z_][\w:.-]*)[ \t]*=[ \t]*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
# A key name as the whole value of an attribute: `key="api_key"`.
_XML_KEY_NAME_VALUE_RE = re.compile(
    r"^[\w.-]*(?:api[ _\-]?key|apikey|secret[ _\-]?key)s?$", re.IGNORECASE)
# HARDEN-13 C: the same key in prose, with a space where a config has `=` or `:`
# (`apikey V`, `x-api-key V`). The value is a token, so `api_key is required`
# and `apikey of the provider` are prose and stay.
_API_KEY_SPACE_RE = re.compile(
    r"(?i)(?<![\w.-])([\w.-]*api[ _\-]?key)([ \t]+)"
    r"([A-Za-z0-9][A-Za-z0-9_-]{19,})(?![\w-])"
)
# A key name wherever it sits in a line, for the rules that have to look at the
# line rather than at a key-value pair (HARDEN-13 C: the bare `<key>` element).
_KEY_NAME_IN_LINE_RE = re.compile(r"(?i)api[ _\-]?key")
# A tag of any shape, open, close or self-closed, for walking a parent chain.
_XML_ANY_TAG_RE = re.compile(r"<([/]?)([A-Za-z][\w:.-]*)((?:[^<>\"']|\"[^\"]*\"|'[^']*')*)>")
# HARDEN-13 C: a bare JWT, three base64url runs joined by dots with the first
# starting `eyJ` (the base64 of `{"`), wherever it stands — with a key name or
# without one. The runs are judged one by one, not by one pattern.
_JWT_RUN_RE = re.compile(r"(?<![\w.-])[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}")
# A bare Python/JS identifier: a NAME, i.e. a reference to something, never a literal secret.
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A quoted value with no `{` inside — a literal, not a template. The prefix of a
# Python string literal (`r`, `b`, `f`, `u`, `rb`, …) is part of the value (HARDEN-10 B).
_PLAIN_QUOTED_RE = re.compile(r'^(?:[rbfuRBFU]{1,2})?(?:"[^"{}\n]*"|\'[^\'{}\n]*\')$')
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
# A value that is a reference rather than a literal (HARDEN-15 A): a shell form
# (`$KEY`, `${KEY}`) or a format placeholder (`%s`, `%(name)s`, `{}`, `{name}`). Either
# names a value that lives somewhere else, so the assignment holds no secret of its own.
# The colon form already exempted a leading `$`; this reaches the `=` form, where
# `log.info("using api_key=%s", key)` and `?api_key=$KEY` used to go out masked.
_REFERENCE_VALUE_RE = re.compile(
    r"\$\{?[A-Za-z_][\w]*(?:\.[\w]+)*\}?"
    r"|%[-+ #0]*\d*(?:\.\d+)?(?:\(\w+\))?[-+ #0]*[diouxXeEfFgGrscb]"
    r"|\{[A-Za-z_][\w]*(?:\.[\w]+)*\}|\{\}"
)
# A `Bearer <token>` credential, wherever it sits in a line. The token must be long:
# the phrase is also plain English ("bearer of the torch"), so a short word is prose.
#
# HARDEN-10 B: the token starts on a letter or a digit, and stops at a bracket, `$`, `<`
# or `>` — those delimit a template hole or an interpolation
# (`Bearer ${process.env.OPENAI_API_KEY}`, `f'Bearer {self.settings.api_key}'`), never a
# credential. A long word of letters and underscores after the phrase is English too
# ("the bearer authentication_scheme is used"), so the 16-character rule alone is not
# enough: `_mask_bearer` refuses one, since a credential carries a digit or a separator.
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9][^\s'\"`,;)}\[\]<>(){}$]{15,})")
#: A word of letters and underscores only — the tail of an English phrase, not a credential.
_BEARER_PROSE_WORD_RE = re.compile(r"[A-Za-z_]+")
# Provider key prefixes, wherever they sit in a line (HARDEN-5 B: `Authorization:
# Bearer sk-or-v1-…` leaked, since it is not an api_key assignment). The bare `sk-`
# form needs a long tail: `sk-real-1` is a placeholder in this repo's own sources,
# not a credential (test_harden1_a8 pins it as untouched).
#
# HARDEN-15 B: a long tail is not enough either. The bare form needs a digit or an
# upper-case letter, and that character in a part of at least 12 letters — a slug is
# a run of short lower-case words, hyphenated or underscored, and carries none
# (`class="sk-fading-circle"`, `sk-toggleable__label`, and the SSH key type
# `sk-ecdsa-sha2-nistp256` all read as words). `sk-live-AbC123…`,
# `sk-abcdefghijklmnopqrstuvwxyz0123` and the two explicit prefixes above keep their
# long mixed part and are still masked.
_SECRET_PREFIX_RE = re.compile(
    r"\b(?:sk-or-v1-|sk-proj-)[A-Za-z0-9_\-]{10,}"
    r"|\bsk-(?=[A-Za-z0-9_\-]*[A-Z0-9])(?=[A-Za-z0-9_\-]*[A-Za-z0-9_]{12,})[A-Za-z0-9_\-]{12,}"
    r"|\bAIza[A-Za-z0-9_\-]{20,}"
    r"|\bxoxb-[A-Za-z0-9_\-]{10,}"
    # HARDEN-12 C: the other providers' key shapes, in prose, a comment or a log line —
    # Groq, GitHub, Hugging Face, xAI, GitLab, AWS. Every tail is long, so a shorter word
    # that merely starts the same way (`hf_model`, a bare `gsk_`, `AKIA` in a sentence)
    # is untouched: a provider issues tens of characters after its prefix.
    r"|\bgsk_[A-Za-z0-9]{20,}"
    r"|\bgh(?:p|o|s|u)_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bhf_[A-Za-z0-9]{20,}"
    r"|\bxai-[A-Za-z0-9]{20,}"
    r"|\bglpat-[A-Za-z0-9_\-]{16,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{12,}"
)
_MASK_PLACEHOLDER = "here_your_key"


def _is_plain_quoted(value: str) -> bool:
    """Whether *value* is a quoted string with nothing interpolated inside it.

    A template (`api_key=f"key-{ref}"`, `api_key=f"Bearer {token}"`) is code: its
    braces are an expression, and masking the quoted part would replace the
    expression with the placeholder and change what the model is shown (HARDEN-10 B).
    """
    return bool(_PLAIN_QUOTED_RE.match(value))


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
      when it has the shape of a credential — a digit among its letters, or one of the
      providers' key prefixes — since on that form a word of letters is a type
      annotation or a parameter name, whatever its length and case (HARDEN-12 A).
    """
    value = (value or "").strip()
    if not value:
        return False
    # A quoted string is a literal, whatever is inside it — except empty, which is a
    # default value in code (`api_key = ''`), not a secret. A string prefix (`r`, `b`,
    # `f`, `u`, `rb`, …) belongs to the quote, not to the value, so a raw string is
    # the literal inside it and not the identifier `r`; and a `{` inside the quotes
    # means the string is a template rather than a literal
    # (`api_key=f"key-{ref}"` is code and is sent as it is) — both HARDEN-10 B.
    if _is_plain_quoted(value):
        body = _split_prefix(value)[1]
        inner = body[1:-1].strip()
        # `apiKey = "apiKey"` (an enum's member) names the key, it does not hold one.
        own_name = _NON_ALNUM.sub("", key_name.rsplit(".", 1)[-1].lower())
        return (bool(inner) and not _is_template_placeholder(inner)
                and _NON_ALNUM.sub("", inner.lower()) != own_name)
    body = _split_prefix(value)[1]
    if _REFERENCE_VALUE_RE.match(body):
        # A format or shell reference names a value that lives somewhere else, so it holds no
        # secret of its own (HARDEN-15 A): `log.info("using api_key=%s", key)`,
        # `?api_key=$KEY`. Only the unquoted body is looked at here — a quoted value is still
        # the literal between its quotes.
        return False
    if _is_template_placeholder(body):
        return False
    if any(ch.isspace() for ch in body):
        return False
    if not any(ch.isalnum() for ch in body):
        # `api_key=…` / `api_key: ...` in a docstring or a comment is an elision, not a key.
        return False
    if colon and "`" in body:
        # A backtick is Markdown, not a credential: `` `x-api-key: v`, `` in a comment or a
        # docstring names the header and does not hold one.
        return False
    if "." in body:
        # HARDEN-9 A: a dotted value is a token as often as it is an attribute
        # path, so `.` alone must not decide between them.
        return _dotted_value_is_secret(body)
    if any(ch in body for ch in _EXPRESSION_CHARS):
        return False
    if _NUMERIC_VALUE_RE.match(body):
        return False
    if _BARE_IDENTIFIER_RE.match(body):
        if body.strip("_").lower() in _CODE_WORDS:
            return False
        if colon:
            # HARDEN-12 A: on the colon form a bare identifier is a type, a class or a
            # variable as often as it is a value, so only one with the shape of a
            # credential counts. `AsyncOpenAIClientFactory` is an annotation.
            return (_identifier_is_a_credential(body)
                    and not _names_the_same_key(body, key_name))
        return not _names_the_same_key(body, key_name)
    # Not an identifier: an opaque bare token (sk-abc123, a long hex string) — a key.
    return True


def _looks_like_a_token(value: str) -> bool:
    """Whether a bare identifier reads as a credential rather than a name.

    A credential's letters are not all one case and not all one kind: ``AKIA…7…``,
    ``ghp_abc…123``, ``dGhpc2lz…MTIz``. A name is ``some_long_variable`` or a type.

    Used where the value sits inside a quote or a dotted run. For the bare identifier
    after a colon `_identifier_is_a_credential` is the rule: the mixed-case half of this
    test reads a CamelCase class name as a credential, which is what rewrote an
    annotation (HARDEN-12 A).
    """
    return any(ch.isdigit() for ch in value) or (
        any(ch.isupper() for ch in value) and any(ch.islower() for ch in value))


#: The last word of a variable name that says the variable holds a count, a path or a
#: label and not the credential its other words mention (`TOKEN_COUNT`, `KEY_DIR`,
#: `API_KEY_NAME`, `SECRET_FILE`): such a name is not a credential's, whatever else is in it.
_NOT_A_CREDENTIAL_SUFFIXES = frozenset({
    "count", "dir", "path", "file", "name", "id", "type", "length", "len", "size", "limit",
    "max", "min", "ttl", "timeout", "url", "uri", "host", "port", "env", "var", "field",
    "header", "prefix", "index", "version", "mode", "format", "label", "pattern",
})

#: HARDEN-19: a word that, standing alone, says the variable holds a credential. The
#: `AUTH`+`TOKEN` pairing and the compound `APIKEY` come out of the same split, so no
#: extra rule is needed for them.
_CREDENTIAL_WORDS = frozenset({"key", "token", "secret", "password", "passwd", "pwd", "apikey"})

#: HARDEN-19: the names that the "ends in a credential word" half of
#: `_name_is_a_credential` would call a credential and are not — the false friends of the
#: last word (`MONKEY`, `TURKEY`, `HOTKEY`, `DONKEY`, `KEYBOARD`, `KEYMAP`) and the ones
#: this codebase uses as a key column or a limit (`PRIMARY_KEY`, `MAX_TOKENS`,
#: `TOKENIZERS_PARALLELISM`).
_CREDENTIAL_FALSE_FRIENDS = frozenset({
    "monkey", "turkey", "hotkey", "donkey", "keyboard", "keymap",
    "tokenizers_parallelism", "primary_key", "max_tokens",
})

#: HARDEN-19: how a name splits into its words inside a run of letters — the camelCase
#: boundary, `APIKey` → `API`, `Key`, `clientSecret` → `client`, `Secret`. `_` and `-`
#: are split first, so the separators need no case structure of their own.
_NAME_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _credential_name_words(name: str) -> "list[str]":
    """The words of a variable name, lowercased: split on `_`, `-` and `.`, and inside a
    run of letters on the camelCase boundary. `SYSTEM_ACCESSTOKEN` is `system`,
    `accesstoken`; `clientSecret` is `client`, `secret`; `STRIPEKEY` is the one word
    `stripekey`. Never raises."""
    if not name or not isinstance(name, str):
        return []
    words = []
    for chunk in re.split(r"[_.\-]", name):
        words.extend(_NAME_WORD_RE.findall(chunk))
    return [word.lower() for word in words if word]


#: The words that, next to `KEY`, make it a database key and not a secret.
_DATABASE_KEY_KINDS = frozenset({"primary", "foreign", "unique", "index", "sort", "partition"})


def _name_is_a_credential(name: str) -> bool:
    """Whether a variable name names a credential — HARDEN-19's one rule, the mask and the
    command log both ask it.

    A word that stands alone and names a credential (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
    `PASSWD`, `PWD`, `APIKEY`) counts, and so does a name that *ends* in one of those words
    written together: `STRIPEKEY`, `GITHUBTOKEN`, `AUTHTOKEN`, `SECRETKEY`, `clientSecret`,
    `dbPassword`. CC review fix 40 asked for the credential word to be a whole part of the
    name only, which is what left `STRIPEKEY` and the camelCase forms unmasked; the "ends
    in" half closes that gap, and `_CREDENTIAL_FALSE_FRIENDS` and the count-and-path
    suffixes keep the friends it was written for (`MONKEY_PATCH`, `KEYBOARD_LAYOUT`,
    `TOKENIZERS_PARALLELISM`, `PRIMARY_KEY`, `MAX_TOKENS`, `KEY_DIR`) out.
    Never raises on a malformed name.
    """
    if not name or not isinstance(name, str):
        return False
    lowered = name.strip().lower()
    if lowered in _CREDENTIAL_FALSE_FRIENDS:
        return False
    words = _credential_name_words(name)
    if not words:
        return False
    if words[-1] in _NOT_A_CREDENTIAL_SUFFIXES:
        # `TOKEN_COUNT`, `KEY_DIR`, `API_KEY_NAME`: the last word says count, path or label.
        return False
    if "key" in words and not _DATABASE_KEY_KINDS.isdisjoint(words):
        # `PRIMARY_KEY`, `FOREIGN_KEY`, `UNIQUE_KEY`, `INDEX_KEY`, `user_primary_key`: a
        # database key column, not a credential.
        return False
    if any(word in _CREDENTIAL_WORDS for word in words):
        return True
    flat = re.sub(r"[^a-z0-9]", "", lowered)
    return bool(flat) and any(flat.endswith(word) for word in _CREDENTIAL_WORDS)


def _name_is_not_a_credential(name: str) -> bool:
    """Whether a variable name is not a credential's — the inverse of
    `_name_is_a_credential`, kept because the rules that skip a hit ask it that way."""
    return not _name_is_a_credential(name)


#: A name is rarely over 32 letters: a single run of letters that long, with no case
#: structure to break it into words, is what a token looks like (HARDEN-13 C).
_NAME_MAX_LETTERS = 32


def _identifier_is_a_credential(value: str) -> bool:
    """Whether a bare identifier of letters, digits and underscores carries the shape
    of a credential rather than a name (HARDEN-12 A).

    A credential either has a digit among its letters (``AKIAIOSFODNN7EXAMPLE``,
    ``ghp_…0123``) or starts with one of the providers' key prefixes. A word of letters,
    CamelCase or snake_case, whatever its length and case, is a type, a class or a
    variable: ``AsyncOpenAIClientFactory``, ``ProviderCredentialsHolder``, ``SecretStr``,
    ``client_factory``. The old mixed-case half of ``_looks_like_a_token`` called every
    CamelCase name a credential, so an indented dataclass field went out masked and a
    faithful edit wrote the placeholder back where an annotation named a class — the
    module then failed to import, and nothing reported it.

    HARDEN-13 C: a run of letters is not only a name — ``qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGh``
    is a credential too, and a rule that wanted a digit threw every digit-free token
    away. So the letters are asked whether they read as a name: a name is words of three
    or more letters (``Provider|Credentials|Holder``) with at most one short acronym
    (``Async|Open|AI|Client|Factory``), while a token has several one- and two-letter
    humps (``q|LKj|HG|fd|Sa|ZX|c|Vb``). Only a word with no case structure at all is
    read by length, since a name is rarely over 32 letters.
    """
    return (any(ch.isdigit() for ch in value)
            or bool(_SECRET_PREFIX_RE.search(value))
            or not _letters_read_as_a_name(value))


#: A CamelCase word: an acronym run that the next word does not lowercase (`API` in
#: `APIKey`, and `AABB` whole), a capitalised word, or a run of lowercase letters and
#: digits. `re.findall` of it splits a name into its humps.
_CAMEL_HUMP_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _letters_read_as_a_name(value: str) -> bool:
    """Whether a run of letters (with underscores, or digits) reads as a name
    rather than a token (HARDEN-13 C).

    Two small checks, because one pattern cannot tell ``ProviderCredentialsHolder``
    from ``qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGh``: the *humps* — a name is several words of
    three or more letters with at most one short acronym, a token has several one- or
    two-letter humps — and, for a word with no case structure at all, the *length*,
    since a name is rarely over 32 letters.
    """
    humps = _CAMEL_HUMP_RE.findall(value)
    short = sum(1 for hump in humps if len(hump) < 3)
    if len(humps) >= 3 and short <= 1:
        return True         # ProviderCredentialsHolder, APIKeyProviderFactory
    if short >= 3:
        return False        # qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGh
    if len(value) > _NAME_MAX_LETTERS and len(humps) <= 2:
        return False        # one long run of letters, with no case structure
    return True             # str, client_factory, SecretStr, OAuth


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


def _split_prefix(value: str) -> "tuple[str, str]":
    """Split *value* into a string prefix (`r`, `b`, `f`, `u`, `rb`, …) and the rest.

    Returns `("", value)` when there is no prefix — i.e. when the letters are not
    followed by a quote, as in the identifier `rbtree` (HARDEN-10 B: a prefix belongs
    to the quoted value, so it is masked with the quotes, not left behind: `rb"abc"`
    masks to `rb"here_your_key"`, never to the bare name `here_your_key`)."""
    if value[:2].lower() in {"rb", "br", "rf", "fr"} and len(value) > 2 and value[2] in "\"'":
        return value[:2], value[2:]
    i = 1 if value[:1] in "rbfuRBFU" else 0
    if i and len(value) > i and value[i] in "\"'":
        return value[:i], value[i:]
    return "", value


def _quote_parts(value: str) -> "tuple[str, str]":
    """The string prefix and its quote character, or ``("", "")`` when *value* is
    unquoted. The prefix goes on the opening side only: `rb"abc"` masks to
    `rb"here_your_key"`, not `rb"here_your_keyrb"` (HARDEN-10 B)."""
    prefix, body = _split_prefix(value)
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        return prefix, body[0]
    return "", ""


def _quote_of(value: str) -> str:
    """The string prefix and quote character wrapping *value*, or ``""`` when it is
    unquoted. `rb"abc"` answers `rb"`: the masked text must stay parseable, and the
    prefix is part of the quoted value (HARDEN-10 B)."""
    prefix, quote = _quote_parts(value)
    return prefix + quote


def _is_a_closing_quote(text: str, index: int, quote: str) -> bool:
    """Whether the *quote* at *index* closes a string that opened earlier on its line.

    Walks the line up to *index* and keeps the one open quote, so the quote that ends the
    URL string in `url = "?api_key=" + key + "&x=" + x` is seen to close, and not to open
    a value of its own (HARDEN-15 A). The value class used to take that quote and the
    quote that opens the next string for one literal, so the line went out as
    `url = "?api_key="here_your_key"&x=" + x` — a placeholder where an expression stood,
    and an edit of the string wrote the broken line to disk.

    A quote of the other kind inside an open string is a literal character, not a toggle:
    `"a 'b' c"` stays one string, and a line that mixes the kinds still balances
    (`{"a": 'b', "api_key": 'x'}` has no string open at the value).
    """
    if quote not in "\"'":
        return False
    start = text.rfind("\n", 0, index) + 1
    open_quote: "str | None" = None
    i = start
    while i < index:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char in "\"'":
            if open_quote is None:
                open_quote = char
            elif char == open_quote:
                open_quote = None
        i += 1
    return open_quote == quote


#: Words after which the next identifier is a name this source declares, imports or binds,
#: so a provider token in that spot is an identifier, not a value (HARDEN-16 B).
_BARE_TOKEN_NAME_KEYWORD_RE = re.compile(r"(?:def|class|import|from|let|const|var|function)\s*$")
#: A name-to-name comparison's operator, standing directly before a token (the token is the
#: right operand) or directly after one (the token is the left operand). The operator ends the
#: look, so the whitespace before it must be swallowed at the end of the look itself.
_BARE_TOKEN_CMP_BEFORE_RE = re.compile(r"(?:\s*==|\s*!=|\s*not\s+in|\s*(?:in|is)\b)\s*$")
_BARE_TOKEN_CMP_AFTER_RE = re.compile(r"^\s*(?:not\s+in|(?:in|is)\b|==|!=)")
#: The comparison words that are English as well — `key is sk-… here`, `the key gh_… in config.yml`
#: — so an operand of one of them proves a name only when the statement ends where code ends it
#: (`==` and `!=` are no English, so they never need the proof).
_BARE_TOKEN_CMP_WORD_RE = re.compile(r"^\s*(?:not\s+in|in|is)\b")
#: The other side of such a comparison, when the token is the left operand: a name path, then the
#: end of the statement — `x in keys:`, `x is self.keys`. Prose goes on with another word.
_BARE_TOKEN_NAME_COMPARAND_RE = re.compile(
    r"^\s*[A-Za-z_$][\w$]*(?:::[\w$]+|\[[^\]]*\]|(?:::|\.)[\w$]+)*\s*(?:$|:|;|,|\)|\]|}|\#|//|/\*)"
)
#: How far either side of a token the syntax rules read: a statement is shorter than this, and
#: reading no further keeps the mask linear in the size of the text.
_BARE_TOKEN_WINDOW = 400
#: The closing brackets of a parenthesised reference, ahead of the `.`, `(` or `[` that follows it:
#: `(NAME).strip()`, `(NAME)[0]`, `f(NAME).id` — the reference is a name, whatever it is wrapped in.
_BARE_TOKEN_CLOSERS = ")]}"
#: A statement end: what an operand of `in` or `is` is followed by in code, and never in prose.
_BARE_TOKEN_STATEMENT_END = frozenset(":;),]}")


def _statement_ends(rest: str) -> bool:
    """Whether *rest* begins the end of a statement.

    ``in``, ``is`` and ``not in`` are English too — ``key is sk-… here``, ``the key gh_… in
    config.yml`` — so an operand followed by another word is prose and stays masked (HARDEN-16 B).
    """
    tail = rest.lstrip(" \t\r\n")
    if not tail:
        return True
    return tail[0] in _BARE_TOKEN_STATEMENT_END or tail.startswith(("#", "//", "/*"))


def _quote_end(text: str, open_index: int) -> int:
    """The index just past the quote, or triple quote, that opened at *open_index*.

    A backslash escapes the next character, so a literal quote inside the string does not close it.
    A string that is not a triple quote cannot span a line: the quote is a stray character there,
    and the region ends with the line rather than running on to swallow the rest of the file.
    """
    quote = text[open_index]
    if text[open_index:open_index + 3] == quote * 3:
        end = text.find(quote * 3, open_index + 3)
        return len(text) if end < 0 else end + 3
    i = open_index + 1
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == quote:
            return i + 1
        if char == "\n":
            return i + 1
        i += 1
    return n


def _text_regions(text: str) -> "list[tuple[int, int, str]]":
    """Every string and comment of *text*, as ``(start, end, "string" | "comment")`` in order.

    The syntax rules of ``_bare_token_is_a_name`` do not apply inside one of them: a token there is
    a value the source carries, not a name it declares, so it stays masked. Escapes are honoured, a
    quote of the other kind inside an open string is a literal character, and all three comment
    forms (``#``, ``//``, ``/* … */``) are read, so a quote inside a comment does not open a string.
    """
    regions: "list[tuple[int, int, str]]" = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "#" or text[i:i + 2] == "//":
            end = text.find("\n", i)
            regions.append((i, n if end < 0 else end, "comment"))
            i = n if end < 0 else end
            continue
        if text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            regions.append((i, end, "comment"))
            i = end
            continue
        if char in "\"'`":
            end = _quote_end(text, i)
            regions.append((i, end, "string"))
            i = end
            continue
        i += 1
    return regions


def _region_at(regions: "list[tuple[int, int, str]]", index: int) -> "tuple[int, int, str] | None":
    """The ``(start, end, kind)`` region that holds *index*, or ``None`` when it is code.

    The regions are in order and do not overlap, so the one that can hold *index* is the last
    that starts at or before it: a bisection, not a walk, because a file with thousands of
    tokens and thousands of strings would otherwise cost their product.
    """
    lo, hi = 0, len(regions)
    while lo < hi:
        mid = (lo + hi) // 2
        if regions[mid][0] <= index:
            lo = mid + 1
        else:
            hi = mid
    if lo and regions[lo - 1][0] <= index < regions[lo - 1][1]:
        return regions[lo - 1]
    return None


def _in_interpolation(text: str, span_start: int, span_end: int, start: int, end: int) -> bool:
    """Whether a token at ``[start, end)`` inside the string ``(span_start, span_end)`` sits in an
    interpolation: it opens a ``{…}`` pair and a matching ``}`` closes it before the string does.

    An f-string's ``{name}`` and a template literal's ``${name}`` are expressions — a name there is
    a name, while the rest of the literal's text is a value and stays masked (HARDEN-16 B).
    """
    i = start - 1
    while i >= span_start and text[i] in " \t\r\n":
        i -= 1
    if i < span_start or text[i] != "{":
        return False
    depth = 1
    j = end
    while j < span_end:
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char in "\"'`":
            j = _quote_end(text, j)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return True
        j += 1
    return False


def _bare_token_is_a_name(text: str, start: int, end: int,
                          regions: "list[tuple[int, int, str]]") -> bool:
    """Whether the token at ``text[start:end]`` is a **name** in the syntax rather than a value.

    HARDEN-16 B: a provider token used as an identifier used to go out as ``here_your_key`` —
    ``if AKIA… in keys``, ``AKIA….split()``, ``{AKIA…}`` — and an ``/edit`` of the file wrote the
    placeholder where a name stood, leaving code that does not run. Leaking is worse than a broken
    line, so the exemption is narrow: the syntax has to prove the token is a name.

    * an attribute, a call or an index of it, or an attribute of it — ``f(x).id``, ``name.split(``,
      ``name[0]``, ``config.NAME``, ``f(NAME).id``, ``(NAME).strip()``;
    * an operand of ``in``, ``not in``, ``is``, ``==`` or ``!=`` when the other side is not a
      quoted string — a comparison of two names;
    * the whole content of a ``{…}`` pair — an f-string or template interpolation, a shorthand
      property, a set element;
    * a key of an object literal — ``{NAME: 1}``, where the colon separates a key from a value;
    * the name after ``def``, ``class``, ``import``, ``from``, ``let``, ``const``, ``var``,
      ``function``;
    * inside a string, only an interpolation counts — every other character of a quoted value is a
      value, and so is everything in a comment; both stay masked.
    """
    region = _region_at(regions, start)
    if region is not None:
        if region[2] == "comment":
            return False
        return _in_interpolation(text, region[0], region[1], start, end)

    # A window around the token, not the whole text: every rule below looks at the statement
    # the token stands in, and a copy of a megabyte of file per token costs their product.
    before = text[max(0, start - _BARE_TOKEN_WINDOW):start]
    after = text[end:end + _BARE_TOKEN_WINDOW]
    left = before.rstrip(" \t")
    right = after.lstrip(" \t")

    if left.endswith("."):
        return True
    # An attribute, a call or an index of the reference: the `.`, `(` or `[` may stand behind the
    # closing bracket of a parenthesised reference — `(NAME).strip()`, `(NAME)[0]`.
    j = 0
    while j < len(right) and right[j] in _BARE_TOKEN_CLOSERS:
        j += 1
    if j < len(right) and right[j] in ".([":
        return True
    right_cmp = _BARE_TOKEN_CMP_AFTER_RE.match(after)
    if right_cmp:
        rest = after[right_cmp.end():]
        if rest.lstrip(" \t")[:1] not in "\"'`" and (
                not _BARE_TOKEN_CMP_WORD_RE.match(right_cmp.group(0))
                or _BARE_TOKEN_NAME_COMPARAND_RE.match(rest)):
            return True
    left_cmp = _BARE_TOKEN_CMP_BEFORE_RE.search(before)
    if left_cmp:
        other = before[:left_cmp.start()].rstrip(" \t")
        quoted = bool(other) and other[-1] in "\"'`" \
            and _is_a_closing_quote(text, start - len(before) + left_cmp.start() - 1, other[-1])
        if not quoted and (not _BARE_TOKEN_CMP_WORD_RE.match(left_cmp.group(0))
                           or _statement_ends(after)):
            return True
    if left.endswith("{") and right.startswith("}"):
        return True
    if left.endswith(("{", ",")) and right.startswith(":") and not right.startswith(("::", ":=", "=>")):
        return True
    return bool(_BARE_TOKEN_NAME_KEYWORD_RE.search(before))


def _mask_secret_prefix(match: "re.Match[str]",
                        regions: "list[tuple[int, int, str]]") -> str:
    """The token of a ``_SECRET_PREFIX_RE`` match, or the placeholder for it.

    A token that the syntax proves to be a name is left alone (HARDEN-16 B): it names something in
    the source, so masking it would hand the model a line that does not parse and an ``/edit``
    would write ``here_your_key`` into it. Everything else — a value after an ``=`` or a ``:``, a
    quoted string, a config line, a header — is the placeholder.
    """
    return match.group(0) if _bare_token_is_a_name(match.string, match.start(), match.end(), regions) \
        else _MASK_PLACEHOLDER


def _xml_parent_element(before: str) -> "str | None":
    """The name of the element an XML element at the end of *before* sits inside,
    or ``None`` when it is at the top level (HARDEN-13 C).

    Walks the tags that came before it and keeps a stack of the open ones, the
    way a document's own nesting does, so `<api_keys><key>…</key></api_keys>`
    answers `api_keys` and a bare `<key>` answers `None`.
    """
    stack: "list[str]" = []
    for match in _XML_ANY_TAG_RE.finditer(before):
        closing, name = match.group(1), match.group(2)
        if match.group(0).endswith("/>"):
            continue                      # self-closed: it opens nothing
        if closing:
            while stack and stack[-1] != name:
                stack.pop()
            if stack:
                stack.pop()
            continue
        stack.append(name)
    return stack[-1] if stack else None


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

    HARDEN-12: three more shapes that reached the provider, through the same rule.
    A key name in single quotes with a ``:`` or a ``=>`` (``{'api_key': '…'}``,
    ``api_key => '…'``), a subscript assignment (``os.environ["GROQ_API_KEY"] = "…"``),
    and an XML element whose tag is a key name (``<api_key>…</api_key>``) are now
    looked at as well; and a bare key with a known provider prefix (``gsk_``,
    ``ghp_``, ``hf_``, ``xai-``, ``glpat-``, ``AKIA``/``ASIA``) is masked wherever it
    sits. In exchange the unquoted colon form no longer reads a CamelCase class name as
    a credential (Part A): ``api_key: AsyncOpenAIClientFactory`` is an annotation.

    HARDEN-16: that last prefix mask is what rewrote a token used as an identifier —
    ``if AKIA… in keys`` went out as ``if here_your_key in keys`` — and an ``/edit``
    wrote the placeholder back where a name stood. A bare token is left alone only where
    the syntax proves it is a name (see ``_bare_token_is_a_name``): in an attribute
    access, a call or an index, a name-to-name comparison, an interpolation, a binding or
    an object-literal key. In every other position — a value after an ``=`` or a ``:``,
    a quoted value, a config line, a header, a comment — the placeholder still goes.

    HARDEN-19 A: the environment write is read by its name's words, not by whether it
    carries a credential word as a whole part. ``os.environ["STRIPEKEY"]``,
    ``os.environ["clientSecret"]`` and ``os.environ["PGPASSWORD"]`` are a credential now,
    while ``TOKENIZERS_PARALLELISM``, ``KEYBOARD_LAYOUT``, ``MONKEY_PATCH``, ``PRIMARY_KEY``
    and ``MAX_TOKENS`` are not, and the write may sit in a call rather than a subscript —
    ``os.environ.setdefault("N", v)``, ``System.setProperty("api.key", v)`` and
    ``os.putenv("N", v)`` take the same rule.

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
        # Step one: is the value a value at all? A quote straight after `api_key=` that
        # closes a string opened earlier on the line is not the opening quote of a value,
        # it ends the string the key sits inside, so the line is code and goes out as it
        # is (HARDEN-15 A).
        if _is_a_closing_quote(text, m.start(4), value[0]):
            return m.group(0)
        # Step two: what is the value? A reference is not a secret, and an unquoted value
        # ends at `&` or `#`, so the rest of a query string is left to stand.
        if not _value_is_secret_literal(value, key_name):
            return m.group(0)
        # A quoted value stays quoted: the masked text is what a faithful model
        # writes back, and an unquoted placeholder is a NameError waiting to
        # happen (`api_key = "x"` must not come back as `api_key = here_your_key`).
        str_prefix, quote = _quote_parts(value)
        return f"{prefix}{key_name}{assign}{str_prefix}{quote}{_MASK_PLACEHOLDER}{quote}{suffix}"

    def _mask_colon(m: "re.Match") -> str:
        prefix, key_name, assign, value = m.groups()
        # The `=` branch of this pattern is the same join point as _mask_assignment, so it
        # needs the same first step: `url = "?api_key=" + key` is code (HARDEN-15 A).
        if _is_a_closing_quote(text, m.start(4), value[0]):
            return m.group(0)
        # The colon form is the colon's own judge: a bare identifier there is a type
        # or a parameter name as often as it is a value (HARDEN-9 B).
        if not _value_is_secret_literal(value, key_name, colon=True):
            return m.group(0)
        if value.startswith("$"):
            # `api_key: $OPENAI_API_KEY` is a reference to an environment variable (a shell, a
            # compose file, a CI config), the name of a secret and not the secret itself.
            return m.group(0)
        str_prefix, quote = _quote_parts(value)
        return f"{prefix}{key_name}{assign}{str_prefix}{quote}{_MASK_PLACEHOLDER}{quote}"

    def _masked_env_value(container: str, name: str, value: str, call: str = "") -> "str | None":
        """The masked value of one environment write, or `None` when the line is not a
        credential (HARDEN-19 A): the write does not go to the environment, the name is not
        a credential's, or the value is code — `os.environ["X_API_KEY"] = os.getenv("Y")`
        is a reference to a value that lives elsewhere.

        The call form asks one thing more of a quoted value, because the second argument of
        `setdefault`, `get`, `getenv` or `setProperty` is a *default*: `os.environ
        .setdefault("CONTEST_GATE_API_KEY", "unset-for-the-round")` is a stand-in in this
        repository's own sources, and a stand-in is letters of one case with no digit —
        not the shape of a credential. The subscript's right-hand side is the key's own
        value, so it keeps the plain rule. A value the provider issues with a prefix
        (`gsk_…`) is not a call value's to decide: the bare-token pass catches it wherever
        it sits.
        """
        if not _is_an_environment_write(container, call):
            return None
        if not _name_is_a_credential(name):
            return None
        # The subscript names the key, so it is not the value: an unquoted right-hand side is
        # judged by the colon rule, and a bare identifier there is a variable, not a token.
        if not _value_is_secret_literal(value, name, colon=not _is_plain_quoted(value)):
            return None
        if call:
            _, body = _split_prefix(value)
            if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
                inner = body[1:-1]
                if len(inner) < 4 or not _looks_like_a_token(inner):
                    return None
        str_prefix, vquote = _quote_parts(value)
        return f"{str_prefix}{vquote}{_MASK_PLACEHOLDER}{vquote}"

    def _mask_env_subscript(m: "re.Match") -> str:
        prefix, container, quote, name, assign, value, suffix = m.groups()
        masked = _masked_env_value(container, name, value)
        if masked is None:
            return m.group(0)
        return f"{prefix}{container}[{quote}{name}{quote}]{assign}{masked}{suffix}"

    def _mask_env_attr(m: "re.Match") -> str:
        prefix, container, name, assign, value, suffix = m.groups()
        masked = _masked_env_value(container, name, value)
        if masked is None:
            return m.group(0)
        return f"{prefix}{container}.{name}{assign}{masked}{suffix}"

    def _mask_env_call(m: "re.Match") -> str:
        prefix, container, call, quote, name, separator, value = m.groups()
        masked = _masked_env_value(container, name, value, call)
        if masked is None:
            return m.group(0)
        return f"{prefix}{container}{call}{quote}{name}{quote}{separator}{masked}"

    def _mask_xml(m: "re.Match") -> str:
        open_tag, tag_name, value, close_tag = m.groups()
        if value.strip().startswith("$"):
            # `<api_key>$OPENAI_API_KEY</api_key>` names an environment variable, as `api_key: $VAR` does.
            return m.group(0)
        if not _value_is_secret_literal(value, tag_name, colon=not _is_plain_quoted(value)):
            return m.group(0)
        str_prefix, quote = _quote_parts(value)
        return f"{open_tag}{str_prefix}{quote}{_MASK_PLACEHOLDER}{quote}{close_tag}"

    def _mask_bearer(m: "re.Match") -> str:
        token = m.group(2)
        # "the bearer authentication_scheme is used" is English, not a credential: a long
        # word of letters and underscores is prose, while a token carries a digit or a
        # separator. The 16-character rule alone kept both (HARDEN-10 B).
        if _BEARER_PROSE_WORD_RE.fullmatch(token) and _letters_read_as_a_name(token):
            return m.group(0)
        return m.group(1) + _MASK_PLACEHOLDER

    def _mask_space(m: "re.Match") -> str:
        key_name, separator, value = m.groups()
        # The value is a token, and a name is prose: `api_key is required`,
        # `apikey of the provider` never reach the length rule, and a class name
        # that does is still a name.
        if len(value) < _TOKEN_MIN_LEN:
            return m.group(0)
        if not (any(ch.isdigit() for ch in value) or not _letters_read_as_a_name(value)):
            return m.group(0)
        return f"{key_name}{separator}{_MASK_PLACEHOLDER}"

    def _mask_jwt(m: "re.Match") -> str:
        """A bare JWT: three base64url runs, the first of them `eyJ` — the base64
        of `{"`, the header every JWT starts with. The runs are checked one by one,
        so a dotted path of words (`self.settings.api_key`) is never one."""
        runs = m.group(0).split(".")
        if len(runs) != 3 or not runs[0].startswith("eyJ"):
            return m.group(0)
        if any(not _BASE64URL_RUN_RE.match(run) or len(run) < 4 for run in runs):
            return m.group(0)
        return _MASK_PLACEHOLDER

    def _mask_bare_key_element(m: "re.Match") -> str:
        """`<key>V</key>` where the element's line, or the element it sits inside,
        names an API key. A bare `<key>` is a dictionary entry as often as it is a
        credential, so the line has to say so."""
        open_tag, value, close_tag = m.groups()
        line_start = text.rfind("\n", 0, m.start()) + 1
        line = text[line_start:m.end()]
        parent = _xml_parent_element(text[:m.start()])
        if not (_KEY_NAME_IN_LINE_RE.search(line)
                or (parent and _XML_KEY_NAME_VALUE_RE.match(parent))):
            return m.group(0)
        if value.strip().startswith("$"):
            return m.group(0)
        if not _value_is_secret_literal(value, "api_key", colon=not _is_plain_quoted(value)):
            return m.group(0)
        str_prefix, quote = _quote_parts(value)
        return f"{open_tag}{str_prefix}{quote}{_MASK_PLACEHOLDER}{quote}{close_tag}"

    def _mask_xml_attribute_pair(m: "re.Match") -> str:
        """`key="api_key" value="V"` inside one start tag: the attribute that names
        the key, and the attribute that holds its value. The pair is found first,
        then the value is asked about."""
        attributes = m.group(2)
        pairs = _XML_ATTR_RE.findall(attributes)
        if not any(_XML_KEY_NAME_VALUE_RE.match(value.strip("\"'")) for _, value in pairs):
            return m.group(0)
        held = ""

        def _value_attribute(attr: "re.Match") -> str:
            nonlocal held
            if attr.group(1).lower() != "value":
                return attr.group(0)
            quoted = attr.group(2)
            prefix, quote = _quote_parts(quoted)
            if not quote:
                # An unquoted XML attribute value is not a literal the model would
                # ever hand back, so it is left alone.
                return attr.group(0)
            held = quoted[1:-1]
            return f"{attr.group(1)}={quote}{_MASK_PLACEHOLDER}{quote}"

        replaced = _XML_ATTR_RE.sub(_value_attribute, attributes)
        if not held or not _value_is_secret_literal(held, "api_key"):
            return m.group(0)
        return m.group(0).replace(attributes, replaced, 1)

    def _mask_annotated(m: "re.Match") -> str:
        prefix, key_name, assign, value = m.groups()
        inner = value[1:-1]
        # A typed default (`api_key: str = "jan"`) is code; a credential is long and token-shaped.
        if (not _value_is_secret_literal(value, key_name) or len(inner) < _TOKEN_MIN_LEN
                or any(ch.isspace() for ch in inner) or not _looks_like_a_token(inner)):
            return m.group(0)
        return f"{prefix}{key_name}{assign}{value[0]}{_MASK_PLACEHOLDER}{value[0]}"

    text = _API_KEY_LINE_RE.sub(_mask_assignment, text)
    text = _API_KEY_ANNOTATED_RE.sub(_mask_annotated, text)
    text = _API_KEY_COLON_RE.sub(_mask_colon, text)
    text = _API_KEY_JSON_RE.sub(r"\1" + _MASK_PLACEHOLDER + r"\2", text)
    text = _ENV_SUBSCRIPT_ASSIGN_RE.sub(_mask_env_subscript, text)
    text = _ENV_SUBSCRIPT_CALL_RE.sub(_mask_env_call, text)
    text = _ENV_ATTR_ASSIGN_RE.sub(_mask_env_attr, text)
    text = _API_KEY_XML_RE.sub(_mask_xml, text)
    text = _BARE_KEY_XML_RE.sub(_mask_bare_key_element, text)
    text = _XML_START_TAG_RE.sub(_mask_xml_attribute_pair, text)
    text = _BEARER_TOKEN_RE.sub(_mask_bearer, text)
    # HARDEN-13 C: the forms that need no key name at all, or no separator — a
    # bare JWT and a key name followed by its token by a space.
    text = _JWT_RUN_RE.sub(_mask_jwt, text)
    text = _API_KEY_SPACE_RE.sub(_mask_space, text)
    # The strings and comments of the text as it stands now: a token inside one is a value, so the
    # prefix mask below keeps it masked, whatever its neighbours look like (HARDEN-16 B).
    regions = _text_regions(text)
    return _SECRET_PREFIX_RE.sub(lambda m: _mask_secret_prefix(m, regions), text)


# Every place str.splitlines() ends a line at, `\r\n` first so the pair is one
# boundary (HARDEN-13 A3). restore_masked_secrets used to drop only `\n` and
# `\r`, so a line the reply ended with U+2028 lost 8 characters of its own value
# — `api_key = sk-…<U+2028>` came back as `api_key = here_your_key<U+2028>` with
# the original's ending nowhere and the placeholder left on disk.
_LINE_BOUNDARY_RE = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def _split_lines_keepends(text: str) -> "list[str]":
    """Split *text* into lines on every boundary `str.splitlines()` ends a line
    at, each line keeping its own ending (HARDEN-13 A3)."""
    if not text:
        return []
    lines: "list[str]" = []
    start = 0
    for match in _LINE_BOUNDARY_RE.finditer(text):
        lines.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        lines.append(text[start:])
    return lines


def _line_body(line: str) -> str:
    """*line* without the ending `str.splitlines()` would take off it."""
    match = _LINE_BOUNDARY_RE.search(line)
    return line[:match.start()] if match else line


def _line_ending(line: str) -> str:
    """The ending `str.splitlines()` would take off *line* (`""` when there is
    none) — whatever it is, so a U+2028 is put back as the U+2028 it was."""
    match = _LINE_BOUNDARY_RE.search(line)
    return line[match.start():] if match else ""


#: Above this many cells (lines of the original x lines of the reply, after the common
#: head and tail are taken off) the alignment leaves out the lines that repeat.
_EXACT_ALIGN_CELLS = 1_000_000


#: How much of a place line's own characters must still start the line that
#: replaced it for that line to count as the same one, edited (HARDEN-13 A).
#: ``[groq]`` → ``[groq_fast]`` is a rename of the same header; ``[openai]`` →
#: ``[openrouter]`` is another section, and a key must never follow it.
_SAME_LINE_EDIT_MIN = 0.7


def _line_is_the_same_edited(original: str, reply: str) -> bool:
    """Whether the *reply* line is the *original* line with an edit in it.

    A section the model renamed and a ``base_url`` it rewrote above a key are
    the line the key sits under, so the key is still restored against them. A
    line that merely took the old one's place is not: the original's own
    characters still start the reply line, and a different section's header
    does not keep that much of it.
    """
    if not original or not reply:
        return False
    kept = 0
    for left, right in zip(original, reply):
        if left != right:
            break
        kept += 1
    return kept >= _SAME_LINE_EDIT_MIN * min(len(original), len(reply))


def restore_masked_secrets(original: "str | None", reply: "str | None") -> "str | None":
    """Put a real secret back where the model gave its masked form back.

    The request carries ``mask_api_key(file)``, so a faithful model returns the
    line as ``api_key = "here_your_key"`` and a writer that stores the reply
    replaces the real key on disk with the placeholder (HARDEN-5 B: the file
    must be byte-identical except the lines the task asked for). A reply line
    that equals the masked form of an *original* line that the mask changed is
    that original line again; a line the model edited does not equal it and is
    kept as written.

    HARDEN-11 A filed every changed line under its *place* — the nearest line
    above it that the mask left alone — and that held only for a reply whose
    structure matched the file's to the line. One line added between a header
    and its key moved the place, so a routine edit ("change the base_url of one
    provider") wrote the placeholder over a working key and reported success
    (HARDEN-13 A). The restore now aligns the whole reply against the masked
    original with a line-for-line sequence alignment: a masked original line
    that sits in an unchanged stretch of the reply gets its text back, and an
    inserted, edited or re-indented line changes only its own stretch.

    HARDEN-11 A's guarantee still holds on top of it: an alignment pair is
    accepted only when the line the key sits under in the reply is the line it
    sat under in the original (the same one, or the same one edited — a renamed
    section is the same header), so no key is ever written under another header
    than the one it was under. A masked line whose own place is gone — a
    deleted, an added and a reordered section, a header the model renamed away —
    stays the placeholder as before, and
    :func:`unrestored_masked_lines` names it so the writers can refuse it.

    HARDEN-18 D: that place must be an anchor the file holds *once*. Two sections
    that share a line — the same ``base_url`` above each of their keys — were
    indistinguishable by their neighbour, so a reorder of the sections read as no
    change and each key came back under the other section's header. A masked
    line is now paired with its original only on the nearest line above it that
    occurs once in the file (usually the section header), so a whole-section
    reorder restores by the header and never by a shared neighbour.

    HARDEN-18 A: a key call the model split over several reply lines is not the
    masked text of any original line, so the masked-text branch above cannot reach
    it and the placeholder used to be written where the key had been. Inside a
    replaced region the masked lines of the region and the placeholder lines of
    the region are paired by order instead — k of each, every key back in its own
    call — and a different count is recorded as a failed restore, so the write is
    refused rather than losing a key.
    """
    text, _ = _restore_masked_secrets(original, reply)
    return text


def unrestored_masked_lines(
    original: "str | None", reply: "str | None",
) -> "list[tuple[int, str]]":
    """The reply lines that stayed the placeholder although the mask had
    changed the original line they came from (HARDEN-13 A2).

    ``[(line number, line text), …]``, one entry per failed restore. A line is
    a failed restore when it equals the masked text of an original line the
    mask changed and the restore gave no original line back for it — the one
    shape that turns a working key into ``here_your_key`` on disk while the
    writer reports success. A reply that *removes* the key line, or replaces it
    with another expression, is not a failed restore: its line is not the
    masked text of anything, so nothing was asked of the restore. Neither is a
    file that holds the placeholder on purpose: there the original line is the
    same text, so the mask never changed it.
    """
    _, failures = _restore_masked_secrets(original, reply)
    return failures


def _secrets_of_masked_line(original: str, masked: str) -> "list[str] | None":
    """The values the mask put the placeholder over in one line, in order, or ``None``
    when the line cannot be taken apart without a guess.

    ``masked`` is ``original`` with each secret replaced by the placeholder, so the text
    between the placeholders is the same on both sides and the secret is what stands
    between two of those pieces in the original.
    """
    pieces = masked.split(_MASK_PLACEHOLDER)
    if len(pieces) < 2 or not original.startswith(pieces[0]):
        return None
    secrets: "list[str]" = []
    position = len(pieces[0])
    for index in range(1, len(pieces)):
        piece = pieces[index]
        if index == len(pieces) - 1:
            if piece and not original.endswith(piece):
                return None
            end = len(original) - len(piece)
        else:
            end = original.find(piece, position) if piece else position
        if end < position:
            return None
        secrets.append(original[position:end])
        position = end + len(piece)
    rebuilt = pieces[0] + "".join(secret + piece for secret, piece in zip(secrets, pieces[1:]))
    return secrets if rebuilt == original and all(secrets) else None


def _restore_masked_secrets(
    original: "str | None", reply: "str | None",
) -> "tuple[str | None, list[tuple[int, str]]]":
    """The shared body of :func:`restore_masked_secrets` and
    :func:`unrestored_masked_lines`: the restored text and the failed lines."""
    if not original or not reply:
        return reply, []
    original_lines = _split_lines_keepends(original)
    reply_lines = _split_lines_keepends(reply)
    # Both sides are compared with their leading whitespace removed, so a file
    # the model re-indented as a whole is still the same file; the reply's own
    # indentation is what the restored line is written with.
    original_norms = [_line_body(line).lstrip() for line in original_lines]
    reply_norms = [_line_body(line).lstrip() for line in reply_lines]
    # The alignment runs against the *masked* original: that is the text the
    # model saw, so a faithful reply is line-for-line equal to it and every
    # masked line has a place to come back to.
    original_masked = [mask_api_key(norm) for norm in original_norms]

    # ── one pass over the original: every changed line, in file order ─────────
    entries: "list[str]" = []                # the original line, with its ending
    entry_of: "dict[int, int]" = {}          # original line index -> entry index
    entry_origin: "list[int]" = []           # entry index -> its original line index
    entry_place: "list[int | None]" = []     # the original index of its place
    entry_above: "list[list[int]]" = []      # the lines above its place back to the last masked line
    entry_anchors: "list[set[int]]" = []     # the ones of those that occur once in the file
    queued: "dict[tuple[str | None, str], list[int]]" = {}
    by_text: "dict[str, list[int]]" = {}
    masked_texts: "set[str]" = set()
    place: "int | None" = None
    above_place: "list[int]" = []
    line_counts = Counter(original_norms)
    for number, norm in enumerate(original_norms):
        if "\ufffd" in norm:
            # An original line the reader could not decode is never a place a
            # key may be restored into.
            continue
        masked = original_masked[number]
        if masked != norm:
            index = len(entries)
            entries.append(original_lines[number])
            entry_of[number] = index
            entry_origin.append(number)
            entry_place.append(place)
            above = above_place[:-1][::-1]
            entry_above.append(above)
            above_place = []
            # HARDEN-18 D: the anchors of the entry — the lines above it the file
            # holds exactly once. A line every section repeats (the shared
            # base_url) cannot tell two keys apart, and pairing on it is what put
            # each key under the other section's header; a section header, or any
            # line that occurs once, can. An empty set — no line above it is
            # unique — falls back to the place rule, which is the only evidence
            # left in a file of key lines.
            anchors = {candidate for candidate in ([place] if place is not None else []) + above
                       if line_counts[original_norms[candidate]] == 1}
            entry_anchors.append(anchors)
            place_text = original_norms[place] if place is not None else None
            queued.setdefault((place_text, masked), []).append(index)
            by_text.setdefault(masked, []).append(index)
            masked_texts.add(masked)
        elif norm:
            # A line the mask left alone is the next masked line's place. A
            # blank line is no place: `[a]` + blank + `api_key = …` is the same
            # layout as without the blank, and counting it made every key of an
            # ini written that way share one place (the first provider's key
            # went under the second after the first section was deleted).
            place = number
            above_place.append(number)
    if not entries:
        return reply, []

    # ── the alignment: which reply line is which original line ──────────────
    # `equal` is the strictly-identical pairing, `touched` the pairing that
    # also covers an edited line — the one shape that needs the original's own
    # text to be the place of the key below it (a renamed section header, a
    # base_url edited above a key). An inserted line is in neither: it has no
    # original line to be the place of anything.
    equal: "dict[int, int]" = {}
    touched: "dict[int, int]" = {}
    # HARDEN-18 A: a masked line the model split over several reply lines is not
    # the masked text of anything, so the masked-text branch cannot pair it. A
    # replaced region is asked for its masked lines and its placeholder lines:
    # the same count pairs them by order and every key comes back in its own
    # call, a different count records every placeholder of the region as a
    # failed restore and the write is refused.
    ph_entry: "dict[int, int]" = {}   # reply line -> the masked line it holds
    ph_fail: "set[int]" = set()       # placeholder lines with no masked line to pair with
    # The lines both sides share at the head and at the tail are paired as they stand,
    # and only what is left between them is aligned: an edit touches a few lines of a
    # long file, and SequenceMatcher is quadratic in the lines that repeat (`}` and the
    # blank line), so a 9 000-line file cost seconds on every write. A middle still too
    # large for an exact alignment is aligned with the popular lines left out, which
    # are never a key's neighbour worth anchoring on.
    head = 0
    limit = min(len(original_masked), len(reply_norms))
    while head < limit and original_masked[head] == reply_norms[head]:
        head += 1
    tail = 0
    while (tail < limit - head
           and original_masked[len(original_masked) - 1 - tail] == reply_norms[len(reply_norms) - 1 - tail]):
        tail += 1
    for offset in range(head):
        equal[offset] = touched[offset] = offset
    for offset in range(tail):
        equal[len(reply_norms) - 1 - offset] = touched[len(reply_norms) - 1 - offset] = len(original_masked) - 1 - offset
    middle_a = original_masked[head:len(original_masked) - tail]
    middle_b = reply_norms[head:len(reply_norms) - tail]
    matcher = difflib.SequenceMatcher(
        a=middle_a, b=middle_b, autojunk=len(middle_a) * len(middle_b) > _EXACT_ALIGN_CELLS)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                equal[head + j1 + offset] = head + i1 + offset
                touched[head + j1 + offset] = head + i1 + offset
            continue
        if tag == "delete":
            continue
        # replace and insert: the reply lines of the region are either the masked
        # text of one of its masked lines (the masked-text branch handles those)
        # or a placeholder the model wrote for a secret the region held.
        block_masked = [entry_of[head + i] for i in range(i1, i2) if head + i in entry_of]
        block_placeholders = [head + j for j in range(j1, j2)
                              if _MASK_PLACEHOLDER in reply_norms[head + j]
                              and reply_norms[head + j] not in masked_texts]
        if block_masked and len(block_masked) == len(block_placeholders):
            for entry_index, reply_number in zip(block_masked, block_placeholders):
                ph_entry[reply_number] = entry_index
        elif block_placeholders:
            # A different count: the restore cannot say which key a placeholder is,
            # and pairing one of them would write the placeholder over another key.
            ph_fail.update(block_placeholders)
        for offset in range(min(i2 - i1, j2 - j1)):
            touched[head + j1 + offset] = head + i1 + offset

    def _same_place(entry: int, number: int) -> bool:
        """Whether the key line at reply *number* still sits under the line the
        original entry at *entry* sits under.

        The line above it in the file is that line — the same one, or the same
        one edited (HARDEN-11 A's place rule, widened for HARDEN-13 A: a
        section renamed ``[groq]`` → ``[groq_fast]`` is the same header, and a
        ``base_url`` rewritten above a key is the same neighbour). Lines the
        model *inserted* are skipped, which is what an edit above a key used to
        break the restore with: ``max_tokens = 4000`` added under a header moved
        the place and cost the key below it. A place that is gone altogether —
        a section deleted, a header rewritten into another section's — accepts
        nothing, so no key is written under a header it was not under.

        HARDEN-18 D: the line it counts has to be one the file holds exactly
        once. A line several sections repeat (the same ``base_url`` above each of
        their keys) is no anchor: two sections that share one traded their keys on
        a reorder, because the shared neighbour looked unchanged. The walk below
        skips a non-anchor line instead of accepting it, and keeps going up to the
        header. A line the model deleted is no anchor either, so the header above
        it takes over and the key stays.
        """
        wanted = entry_place[entry]
        candidates = [wanted] + entry_above[entry] if wanted is not None else []
        if not candidates:
            # A file of nothing but key lines: there is no place to compare,
            # and the whole file is one unchanged stretch of key lines.
            return True
        anchors = entry_anchors[entry]
        anchor_texts = {original_norms[anchor] for anchor in anchors}
        for above in range(number - 1, -1, -1):
            norm = reply_norms[above]
            if not norm or norm in masked_texts:
                # A blank line and a masked line are no place (see the original
                # pass above).
                continue
            target = touched.get(above)
            if target in candidates:
                # The line the alignment pairs with one of the lines this key sat
                # under; it has to be one the file holds once.
                if (not anchors or target in anchors) \
                        and _line_is_the_same_edited(original_norms[target], norm):
                    return True
                continue
            if anchors and norm in anchor_texts:
                # The header the model moved: the alignment calls it an insertion
                # (it paired the other block with these lines), but a line the
                # file holds exactly once can only be that line, so the key under
                # it is the key of that section.
                return True
        return False

    used: "set[int]" = set()
    placed: "set[str]" = set()
    out: "list[str]" = []
    # No original line in this place: held for the second pass below.
    held: "list[tuple[int, str]]" = []
    failures: "list[tuple[int, str]]" = []
    place_norm: "str | None" = None
    for number, line in enumerate(reply_lines):
        body = _line_body(line)
        ending = _line_ending(line)
        norm = reply_norms[number]
        if norm in masked_texts:
            # A masked line is restored from the original line the alignment
            # puts it on, or — for a line the alignment could not pair — from
            # the same masked text under the same place. The line above it
            # stays the place of the next masked line, so two key lines under
            # one header share that header and are restored in order.
            entry = entry_of.get(equal.get(number, -1))
            if entry is not None and entry not in used and _same_place(entry, number):
                index = entry
            else:
                queue = queued.get((place_norm, norm))
                index = None
                if queue:
                    # HARDEN-18 D: the queue is keyed by the place's *text*, and a
                    # place several sections share is the shape that traded the keys,
                    # so the anchor decides before an entry is consumed. The entries
                    # are tried in file order and the ones that do not still sit under
                    # their own header stay in the queue for a later line.
                    kept: "list[int]" = []
                    for candidate in queue:
                        if index is None and candidate not in used and _same_place(candidate, number):
                            index = candidate
                        else:
                            kept.append(candidate)
                    queue[:] = kept
            if index is not None:
                used.add(index)
                placed.add(norm)
                # The reply's own indentation and its own ending, whatever it
                # was, are what the restored line keeps.
                indent = body[:len(body) - len(body.lstrip())]
                out.append(f"{indent}{_line_body(entries[index]).lstrip()}{ending}")
                continue
            held.append((number, norm))
            out.append(line)
            continue
        if _MASK_PLACEHOLDER in norm:
            # A line the model edited AROUND a key (`"model": "z"` on the key's own line,
            # trailing blanks after it) is not the masked text of any original line, and
            # used to be written with the placeholder in it and no word of it anywhere.
            # It is paired with the masked line of its region (HARDEN-18 A: a key call
            # split over several reply lines), or — for one line — with the original line
            # the alignment puts it on; the secrets of that line go back into the
            # placeholders, in order, or the line is a failure.
            entry = ph_entry.get(number)
            paired = None
            if entry is None:
                paired = touched.get(number)
                entry = entry_of.get(paired) if paired is not None else None
            if entry is not None:
                # HARDEN-18 D (judge): a line that names its own section before
                # the placeholder (`"work": {"api_key": "…", …}` in a JSON file,
                # one section per line) and was reordered with a comma moved is
                # paired with the line at the same *position* by the alignment,
                # and that is the other section's key. The text before the
                # placeholder is the line's identity: when exactly one other
                # unused masked line starts with the same text and the paired one
                # does not, the key is that line's.
                lead = norm.split(_MASK_PLACEHOLDER, 1)[0]
                paired_lead = original_masked[entry_origin[entry]].split(_MASK_PLACEHOLDER, 1)[0]
                if lead.strip() and lead != paired_lead:
                    same_lead = [other for other in range(len(entries))
                                 if other not in used and other != entry
                                 and original_masked[entry_origin[other]].split(
                                     _MASK_PLACEHOLDER, 1)[0] == lead]
                    if len(same_lead) == 1:
                        entry = same_lead[0]
            if entry is not None:
                origin = entry_origin[entry]
                secrets = _secrets_of_masked_line(original_norms[origin], original_masked[origin])
                if (entry not in used and secrets is not None
                        and norm.count(_MASK_PLACEHOLDER) == len(secrets) and _same_place(entry, number)):
                    used.add(entry)
                    restored = body
                    for secret in secrets:
                        restored = restored.replace(_MASK_PLACEHOLDER, secret, 1)
                    out.append(restored + ending)
                    continue
                failures.append((number + 1, body))
            elif number in ph_fail:
                # HARDEN-18 A: the placeholder came from a masked line of its region that
                # the restore could not pair it with — the write would keep the placeholder
                # on disk and the real key only in the .bak, so the write is refused.
                failures.append((number + 1, body))
        out.append(line)
        if norm:
            place_norm = norm
    for number, norm in held:
        # The line's place did not match, and that is normally the end of it: a
        # structural edit must not move a key. Two conditions open the one
        # exception that keeps HARDEN-5 B whole — a file every line of which is
        # masked, where the model rewrites a key line into an unmasked one
        # (``api_key = here_your_key`` → ``api_key = os.environ["KEY"]``): the
        # line it rewrote is then the nearest unmasked line in the reply, so the
        # place of every key below it shifts and none of them would be restored.
        # The key comes back only when *nothing* else of that masked text was
        # restored — so no section got its own key while this line got one —
        # and exactly one original line in the whole file has that text and
        # nothing has taken it, so there is no other key it could be.
        left = [index for index in by_text.get(norm, ()) if index not in used]
        # HARDEN-18 D: the exception keeps the key under its own header — an entry
        # that has no line above it at all (a file of key lines) still takes it.
        if norm not in placed and len(left) == 1 and _same_place(left[0], number):
            index = left[0]
            used.add(index)
            line = reply_lines[number]
            body = _line_body(line)
            indent = body[:len(body) - len(body.lstrip())]
            out[number] = f"{indent}{_line_body(entries[index]).lstrip()}{_line_ending(line)}"
            continue
        logger.warning(
            "restore_masked_secrets: %r has no original line in the same "
            "place — left as the placeholder (a structural edit must not "
            "move a key)", norm)
        failures.append((number + 1, _line_body(reply_lines[number])))
    return "".join(out), failures


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


#: The spellings a model's yes/no field may carry for a yes (HARDEN-7), widened
#: by HARDEN-24 C with the words the same approvals arrive as: ``okay`` (English
#: for the same thing as ``ok``), the Russian ``да``, and ``approved`` already
#: here. The list is closed on purpose: anything not in it — a string such as
#: "false", "no" or "rejected" included — is a no, so a field read this way can
#: only ever lose an approval, never invent one.
_YES_SPELLINGS: tuple[str, ...] = ("true", "yes", "y", "1", "approved", "ok", "okay", "да")

#: The punctuation a model leaves trailing on a yes/no field (HARDEN-24 C): a
#: full stop, an exclamation mark, a quote of either kind, a backtick or a
#: comma. Stripped from the right before the match, so ``"Yes."`` and ``"yes!"``
#: are the same yes as ``yes`` and ``"ok, approved"`` reads as two yes spellings
#: rather than one unrecognised string. Only from the right: a leading mark is
#: the reply's own and is left alone by the caller's ``strip()``.
_REPLY_TRAILING_CHARS = " \t.!\"'`,“”‘’«»"
_REPLY_LEADING_QUOTES = "\"'`“‘«"


def reply_text(value) -> str:
    """Read one field of a model's JSON reply as text, whatever type it arrived as.

    HARDEN-7: a prompt asks for ``"feedback": "one concise sentence"`` and a
    small local model answers ``["too long", "no tests"]``, ``5`` or ``{"a": 1}``.
    Every caller then does ``(value or "").strip()``, which raises
    ``AttributeError: 'list' object has no attribute 'strip'`` on the first two
    and takes the run down with it — after the call has already been paid for.

    A string comes back stripped, unchanged otherwise, so a well-behaved reply
    is byte-identical to what the caller's own ``.strip()`` produced. A list is
    one item per line, which HARDEN-7 A pins and HARDEN-24 D keeps.

    HARDEN-24 D: a dict comes back as compact JSON, not as the Python repr. The
    repr is what ``str()`` gives, and a log line or a prompt that quotes the
    field showed ``{'a': 1}`` — single quotes and a space — where the reply held
    JSON. The compact form is what the model actually sent, so a quoted reply
    reads back the way it was written. ``None`` stays empty: every caller that
    needs to tell "held nothing" from "held an empty string" already does so with
    the ``(none)`` marker it supplies itself, and a truthy marker here would
    defeat the ``or "no reason given"`` fallbacks callers rely on. Never raises,
    so the reader itself cannot be the thing that ends a run.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # Compact JSON: the shape the model sent, read back the way it was written.
        return _json_dump(value)
    if isinstance(value, (list, tuple)):
        # A list one item per line: the model meant several lines of text and
        # sent them as JSON. HARDEN-7 A pins the shape, and HARDEN-24 D keeps it.
        return "\n".join(reply_text(item) for item in value).strip()
    return str(value).strip()


def _json_dump(value) -> str:
    """One value as compact JSON, or its ``str()`` when JSON has no shape for it.

    A dict of one pair is ``{"a":1}`` — no space after the separators, and the
    non-ASCII kept rather than escaped to ``\\u...``. Anything JSON cannot encode
    (a set, a date, a key that is not a string) falls back to ``str()``, which is
    the reader's fail-open default rather than an exception out of a log line.
    """
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
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

    HARDEN-24 C: the match is on the *word*, not on the whole stripped string,
    so the punctuation a model leaves trailing does not cost the approval.
    ``"Yes."``, ``"yes!"``, ``"Approved."`` and ``"Yes"`` in quotes all approve,
    and ``"ok, approved"`` approves as two yes spellings. A reply that goes on
    with anything else — ``"Yes, but the date is wrong"``, ``"No"`` — is still a
    no: every comma-separated part has to be a yes spelling, and a part that is
    not is not a yes. Fail-closed is kept, only widened.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        text = value.strip().lstrip(_REPLY_LEADING_QUOTES).rstrip(_REPLY_TRAILING_CHARS).lower()
        if not text:
            return False
        # Every comma-separated part has to be a yes spelling: "ok, approved" is a yes,
        # "Yes, but the date is wrong" is not.
        return all(part.strip() in _YES_SPELLINGS for part in text.split(","))
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
    return _retry_hint_from_text(detail)


def _retry_hint_from_text(detail: str) -> "float | None":
    """HARDEN-11 B: return how long an error *body* asks us to wait, in seconds,
    or ``None`` when it asks for nothing.

    An ``error`` object on a live stream has no HTTP headers to carry a
    ``Retry-After``, so the hint in its own message — Groq's ``"Please try again
    in 820ms"``, Gemini's ``"Please retry in 57.062042596s."`` — is the only one
    there is. The two regexes are the ones the header-less 429 path already
    walked, and ``_parse_retry_after`` still asks its header first.
    """
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


class _StreamErrorEvent(Exception):
    """HARDEN-11 B: the provider failed this call *inside* a live stream.

    Raised by the streaming read of ``request_completion`` when a chunk carries
    a top-level ``error`` object or a final ``finish_reason`` of ``"error"``,
    so the SSE loop's own ``except`` can treat it exactly like the network
    failures next to it — retried while no token has been passed on, reported as
    a ``RuntimeError`` once tokens were emitted. It never escapes the function:
    both outcomes are the ``RuntimeError`` the callers' retry ladders expect.
    """

    def __init__(self, message: str, code: "int | None" = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _stream_error_of(chunk) -> "tuple[str, int | None] | None":
    """HARDEN-11 B: a streamed chunk's ``error`` object as ``(message, code)``,
    or ``None`` when the chunk carries none.

    The OpenAI-compatible shape is ``{"error": {"message": "upstream died",
    "code": 502}}``, at the top level of a chunk, mid-stream or on its own;
    Ollama's NDJSON branch sends a bare ``{"error": "model not found"}``. The
    SSE loop used to step over both: a provider that died after two tokens
    handed back half a reply as if it were the model's answer, and one that
    died before any handed back ``""``, so no retry and no 429 logic ever ran.
    Only a truthy ``error`` counts — ``{"error": null}`` is a provider's empty
    placeholder field, not a failure.
    """
    if not isinstance(chunk, dict):
        return None
    error = chunk.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        message = None
        for key in ("message", "detail", "msg"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
        if message is None:
            message = (f"provider error {code}" if code is not None
                       else "provider error (no message)")
        return message, code
    if isinstance(error, str) and error.strip():
        return error.strip(), None
    return None


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

    HARDEN-11 B: a failure the provider reports *inside* a live stream counts
    too. A chunk with a top-level ``error`` object, or a final ``finish_reason``
    of ``"error"``, is this call failing: with no token passed on yet it is
    retried on the same budget (a 429 code read like a 429 status, its own
    "try again in Nms" hint honoured), and once tokens were emitted it raises
    ``RuntimeError`` naming the error message instead of returning the half
    reply as the model's answer. HARDEN-11 C: a stream line that is not valid
    UTF-8 is decoded with replacement characters and parsed as far as it parses,
    so one bad byte costs that line rather than every token already read.

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
            except _RETRYABLE_TRANSPORT_ERRORS as e:
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
            except _RETRYABLE_TRANSPORT_ERRORS + (ValueError,) as e:
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
    # One undecodable line is logged once per call, not once per line (HARDEN-11 C).
    _bad_line_logged = False
    while True:
        try:
            with _open() as response:
                # HARDEN-23 A: tracks whether the stream saw its completion marker ([DONE]/done:true).
                _seen_done = False
                for raw_line in response:
                    _check_deadline("the stream")
                    # A BOM on the first stream line is stripped, so that chunk is not dropped (utf-8-sig).
                    # HARDEN-11 C: one byte that is not UTF-8 costs that line, not
                    # the whole reply — the good tokens on both sides of it are
                    # kept, and the fault is logged once instead of raising out of
                    # the read as a raw UnicodeDecodeError nothing catches.
                    try:
                        line = raw_line.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        line = raw_line.decode("utf-8-sig", errors="replace")
                        if not _bad_line_logged:
                            _bad_line_logged = True
                            logger.warning(
                                "request_completion: a stream line from %s is not "
                                "valid UTF-8 (%s) — decoded with replacement "
                                "characters, the rest of the stream is kept", url, exc)
                    line = line.strip()
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
                        # HARDEN-11 B: an error object on the stream is this call
                        # failing, whether or not it is the provider's last word.
                        _err = _stream_error_of(chunk)
                        if _err is not None:
                            raise _StreamErrorEvent(*_err)
                        if chunk.get("done_reason") == "error":
                            raise _StreamErrorEvent(
                                "the provider reported done_reason 'error' on the stream")
                        _meta_from_chunk(chunk, meta, api_format)
                    else:
                        # OpenAI SSE: "data: {...}" lines
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            _seen_done = True
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
                        # HARDEN-11 B: an error object is a failure of this call, and
                        # so is a final finish_reason of "error" — neither may be
                        # taken for an ordinary finish.
                        _err = _stream_error_of(chunk)
                        if _err is not None:
                            raise _StreamErrorEvent(*_err)
                        if isinstance(first, dict) and first.get("finish_reason") == "error":
                            raise _StreamErrorEvent(
                                "the provider reported finish_reason 'error' on the stream")
                        _meta_from_chunk(chunk, meta, api_format)

                    if token:
                        parts.append(token)
                        # Counts only chunks that carry content, not role-only, reasoning or usage chunks (RUN-9).
                        meta.content_chunks += 1
                        if on_token is not None:
                            on_token(token)
                    if done:
                        _seen_done = True
                        break
            # HARDEN-23 A: a stream that ends without [DONE]/done and with no
            # finish_reason (and no trailing usage chunk) is a truncation — the
            # provider dropped the body or closed it short. A silent drop passes
            # as a normal reply today, and Gate 1's budget check reads
            # finish_reason=None like "stop".
            #
            # A completion signal accepts the stream: a finish_reason set on any
            # chunk, a usage-only final chunk (OpenAI include_usage sends one with
            # empty choices), or the [DONE]/done marker above. Everything else — a
            # clean close with a content-only last chunk, a partial last line, or
            # nothing at all — is retried like a dropped connection when no token
            # reached on_token, and raises a RuntimeError naming the provider once
            # one did (the error-event path's shape, HARDEN-11 B).
            if (not _seen_done and meta.finish_reason is None
                    and meta.completion_tokens is None):
                if parts:
                    raise RuntimeError(
                        f"stream from {url} ended mid-reply with "
                        f"{meta.content_chunks} chunk(s) but no finish_reason "
                        f"and no [DONE] "
                        f"(after {_stream_attempt} retr"
                        f"{'y' if _stream_attempt == 1 else 'ies'}, "
                        f"tokens already emitted)"
                    ) from None
                if _stream_attempt >= error_retries:
                    raise RuntimeError(
                        f"stream from {url} ended mid-reply before any token "
                        f"and never sent [DONE], a finish_reason or usage "
                        f"(after {_stream_attempt} retr"
                        f"{'y' if _stream_attempt == 1 else 'ies'})"
                    ) from None
                msg = (
                    f"stream from {url} ended mid-reply before any token "
                    f"and never sent [DONE], a finish_reason or usage, "
                    f"waiting {error_retry_wait_sec:.1f}s and retrying "
                    f"(attempt {_stream_attempt + 1}/{error_retries})"
                )
                retry_after(msg, error_retry_wait_sec)
                _stream_attempt += 1
                continue
            break
        except _StreamErrorEvent as e:
            # HARDEN-11 B: before any token has been passed on this is retried
            # like a dropped connection (a 429 code like a 429 status); after
            # tokens were emitted the half reply is not the model's answer and
            # is reported instead, the way the other mid-stream failures are.
            if parts:
                raise RuntimeError(
                    f"provider error reading stream from {url}: {e.message} "
                    f"(after {_stream_attempt} retr{'y' if _stream_attempt == 1 else 'ies'}"
                    f", tokens already emitted)"
                ) from None
            if _stream_attempt >= error_retries:
                raise RuntimeError(
                    f"provider error reading stream from {url}: {e.message} "
                    f"(after {_stream_attempt} retr{'y' if _stream_attempt == 1 else 'ies'})"
                ) from None
            # The wait a 429's own message asks for, when it asks (AUTO-RATE-1);
            # anything else waits the fixed backoff, exactly as an HTTP status does.
            wait_s = _retry_hint_from_text(e.message) if e.code == 429 else None
            if wait_s is None:
                wait_s = error_retry_wait_sec

            if wait_s > max_retry_after_sec:
                msg = (
                    f"provider error reading stream from {url}: server asked for a "
                    f"{wait_s:.0f}s wait, longer than the "
                    f"{max_retry_after_sec:.0f}s cap (looks like a daily/"
                    f"monthly quota reset, not a transient rate limit) "
                    f"— not waiting"
                )
                logger.warning("request_completion: %s", msg)
                if on_retry:
                    on_retry(msg)
                raise RuntimeError(
                    f"provider error reading stream from {url}: {e.message}"
                ) from None

            msg = (
                f"provider error reading stream from {url}: {e.message}, waiting "
                f"{wait_s:.1f}s and retrying "
                f"(attempt {_stream_attempt + 1}/{error_retries})"
            )
            retry_after(msg, wait_s)
            _stream_attempt += 1
        except _RETRYABLE_TRANSPORT_ERRORS as e:
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
