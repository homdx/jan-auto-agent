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
import re
import ssl
import urllib.request
import urllib.error

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


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
    """Extract assistant message text from a non-streaming response dict."""
    if api_format == "ollama":
        return raw["message"]["content"].strip()
    # openai (default)
    return raw["choices"][0]["message"]["content"].strip()


def _build_payload(payload: dict, api_format: str, stream: bool) -> dict:
    """
    Return a copy of payload shaped for the target API format.

    openai : top-level temperature, stream flag, standard messages array.
    ollama : temperature moves into options{}, num_ctx added if present,
             /api/chat expects {"model", "messages", "stream", "options"}.
    """
    body = dict(payload)
    if api_format == "ollama":
        options = {}
        if "temperature" in body:
            options["temperature"] = body.pop("temperature")
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


def ollama_chat_url(base_url: str) -> str:
    """Return the correct Ollama /api/chat URL from *base_url*.

    Handles two conventions:
      - base_url already ends with ``/api``  → append ``/chat`` only
      - base_url does not end with ``/api``  → append ``/api/chat``

    This avoids both the doubled ``/api/api/chat`` and the broken
    ``/chat`` that stripping produced for non-auto callers.
    """
    base = base_url.rstrip("/")
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
) -> tuple[str, dict, dict]:
    """
    Build the (url, headers, payload) triple for a one-shot system/user chat
    call, branching on *api_format* ("ollama" vs an openai-compatible API).

    Shared by Coder, Gate1Filter, Architect, and TaskRewriter — all four send
    the same single-turn system+user request and only differ in which
    model/temperature/system prompt they configure.

    *think*, when not ``None`` and *api_format* is ``"ollama"``, is passed as
    the top-level ``"think"`` field Ollama uses to toggle a reasoning model's
    (e.g. qwen3) internal ``<think>...</think>`` chain-of-thought. Callers
    that don't need the model's reasoning in the reply (short, deterministic
    classification calls like Gate 1's presence check) should pass
    ``think=False``: otherwise a small ``max_tokens`` cap can truncate the
    reply mid-``<think>``, before any usable answer is emitted, and the
    caller sees an empty/unparseable response. Ignored for non-Ollama
    formats and omitted entirely when ``None`` (server/model default).
    """
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_msg},
    ]
    if api_format == "ollama":
        url = ollama_chat_url(base_url)
        opts: dict = {"temperature": temperature, "num_predict": max_tokens}
        if num_ctx:
            opts["num_ctx"] = num_ctx
        payload: dict = {"model": model, "messages": messages, "options": opts}
        if think is not None:
            payload["think"] = think
    else:
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": messages,
        }
    return url, headers, payload


def request_completion(url, headers, payload, timeout, stream=False, on_token=None,
                       api_format: str = "openai", ssl_context: ssl.SSLContext | None = None):
    """
    POST a chat-completions request and return the assistant message text.

    api_format : "openai"  → /v1/chat/completions  (SSE streaming, choices[])
                 "ollama"  → /api/chat              (NDJSON streaming, message{})

    stream=False : normal blocking request, returns the full content string.
    stream=True  : reads the token stream; calls on_token(tok) for each token
                   (if provided) and returns the accumulated content.

    Raises urllib errors / network exceptions to the caller.
    """
    body = _build_payload(payload, api_format, stream)

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    def _open():
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=ssl_context)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail or e.reason}") from None

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
                    token = chunk["choices"][0]["delta"].get("content", "")
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
