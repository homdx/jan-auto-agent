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
            options["num_ctx"] = body.pop("num_ctx")
        if options:
            body["options"] = options
        body["stream"] = stream
        # /api/chat does not use a separate system message list entry —
        # system content is passed as a messages entry with role "system",
        # which is already the format callers use, so nothing extra needed.
    else:
        if stream:
            body["stream"] = True
    return body


def request_completion(url, headers, payload, timeout, stream=False, on_token=None,
                       api_format: str = "openai"):
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
            return urllib.request.urlopen(req, timeout=timeout)
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
