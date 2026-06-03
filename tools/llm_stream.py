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

    Handles well-formed blocks, a dangling close tag with no open (keep text
    after the last </think>), and stray tags. Returns the cleaned, stripped text.
    Needed because models like qwen3 wrap their JSON / answer in <think> tags,
    which otherwise breaks json.loads and pollutes rendered answers.
    """
    if not text:
        return text
    out = _THINK_RE.sub("", text)
    if "</think>" in out:                       # unclosed open tag case
        out = out.rsplit("</think>", 1)[-1]
    out = out.replace("<think>", "").replace("</think>", "")
    return out.strip()


def request_completion(url, headers, payload, timeout, stream=False, on_token=None):
    """
    POST a chat-completions request and return the assistant message text.

    stream=False : normal blocking request, returns the full content string.
    stream=True  : reads the SSE token stream; calls on_token(tok) for each
                   token (if provided) and returns the accumulated content.

    Raises urllib errors / network exceptions to the caller, which keeps each
    agent's existing fail-closed handling intact.
    """
    body = dict(payload)
    if stream:
        body["stream"] = True

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    def _open():
        # Surface the server's error body (Jan often explains WHY in the 500 body:
        # model not loaded, out of memory, context length exceeded, etc.).
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
            return raw["choices"][0]["message"]["content"].strip()

    # Streaming path: echo + accumulate.
    parts = []
    with _open() as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                token = chunk["choices"][0]["delta"].get("content", "")
            except (json.JSONDecodeError, KeyError):
                continue
            if token:
                parts.append(token)
                if on_token is not None:
                    on_token(token)
    return "".join(parts).strip()
