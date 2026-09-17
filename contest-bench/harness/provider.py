"""Fake OpenAI-compatible provider at the urllib.request.urlopen level.

Every entrant's presence check ends in ``urllib.request.urlopen(req, ...)``
inside ``tools.llm_stream.request_completion`` — whatever API they wrapped
around it. Scripting the SSE stream here is therefore the one black box
that fits all 18 trees.

A reply spec is a dict:
    content        str | callable(payload) -> str | None   (None = no content delta)
    finish_reason  "stop" | "length" | None                (None = no finish chunk)
    usage          int | callable(payload) -> int | None   (completion_tokens; None = no usage chunk)
    reasoning      str | None                              (reasoning_content deltas)
    reasoning_key  "reasoning_content" (default) | "reasoning"
    role_chunk     bool (default True)  send the role-only first chunk
    role_content   None | ""            role chunk carries "content": "" when ""
    http           (status, body)       raise HTTPError instead of streaming
    exc            "timeout"            raise TimeoutError
    ollama         bool                 NDJSON /api/chat shape instead of SSE
"""
from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.request


def _sse(obj) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def _chunk(delta: dict, finish_reason=None) -> dict:
    return {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _split(text: str, n: int = 3) -> list[str]:
    if not text:
        return []
    if len(text) <= n:
        return [text]
    step = max(1, len(text) // n)
    return [text[i:i + step] for i in range(0, len(text), step)]


def build_sse(spec: dict, payload: dict) -> bytes:
    content = spec.get("content")
    if callable(content):
        content = content(payload)
    usage = spec.get("usage")
    if callable(usage):
        usage = usage(payload)
    out = []
    if spec.get("role_chunk", True):
        d = {"role": "assistant"}
        if spec.get("role_content") == "":
            d["content"] = ""
        out.append(_sse(_chunk(d)))
    rk = spec.get("reasoning_key", "reasoning_content")
    for piece in _split(spec.get("reasoning") or ""):
        out.append(_sse(_chunk({rk: piece})))
    fr = spec.get("finish_reason", "stop")
    usage_obj = None if usage is None else {"prompt_tokens": 100, "completion_tokens": int(usage), "total_tokens": 100 + int(usage)}
    pieces = _split(content or "")
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        if last and spec.get("finish_inline") and fr is not None:
            out.append(_sse(_chunk({"content": piece}, fr)))
        else:
            out.append(_sse(_chunk({"content": piece})))
    if fr is not None and not (spec.get("finish_inline") and pieces):
        ch = _chunk({}, fr)
        if spec.get("usage_inline") and usage_obj is not None:
            ch["usage"] = usage_obj
        out.append(_sse(ch))
    if usage_obj is not None and not spec.get("usage_inline"):
        out.append(_sse({"id": "chatcmpl-stub", "object": "chat.completion.chunk",
                         "choices": [], "usage": usage_obj}))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


def build_ndjson(spec: dict, payload: dict) -> bytes:
    content = spec.get("content")
    if callable(content):
        content = content(payload)
    usage = spec.get("usage")
    if callable(usage):
        usage = usage(payload)
    out = []
    for piece in _split(content or ""):
        out.append(json.dumps({"model": "test-model", "message": {"role": "assistant", "content": piece},
                               "done": False}).encode() + b"\n")
    last = {"model": "test-model", "message": {"role": "assistant", "content": ""}, "done": True}
    if spec.get("finish_reason") is not None:
        last["done_reason"] = spec["finish_reason"]
    if usage is not None:
        last["eval_count"] = int(usage)
    out.append(json.dumps(last).encode() + b"\n")
    return b"".join(out)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, delay: float = 0.0):
        self._delay = delay
        self._lines = body.splitlines(keepends=True)
        self._body = body
        self.status = status
        self.code = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        if self._delay:
            t = time.perf_counter()
            while time.perf_counter() - t < self._delay:   # time.sleep is stubbed out by the harness
                pass
        return iter(self._lines)

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


def candidate_key(payload: dict) -> str:
    """Route by the candidate's cited file — the same trick the RUN-5 tests use."""
    try:
        user_msg = payload["messages"][-1]["content"]
    except Exception:
        return "?"
    m = re.search(r"Location:\s*(.+)$", user_msg, re.M)
    return m.group(1).split(",")[0].strip() if m else "?"


class FakeProvider:
    """plans: {candidate_file: [spec, spec, ...]} — last spec repeats forever.
    `default` plan is used for unknown keys. Records every request."""

    def __init__(self, plans: dict, default: list | None = None):
        self.plans = plans
        self.default = default
        self.requests: list[dict] = []      # every request in order
        self.by_key: dict[str, list] = {}   # key -> [payload, ...]
        self.t0 = time.monotonic()

    def __call__(self, req, timeout=None, context=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raw = req.data if hasattr(req, "data") else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {"_raw": raw.decode("utf-8", "replace")}
        key = candidate_key(payload)
        seq = self.by_key.setdefault(key, [])
        plan = self.plans.get(key, self.default)
        if plan is None:
            raise RuntimeError(f"stub: no plan for candidate key {key!r}")
        spec = plan[min(len(seq), len(plan) - 1)]
        if callable(spec):
            spec = spec(payload)
        seq.append(payload)
        self.requests.append({"key": key, "n": len(seq), "url": url, "payload": payload,
                              "t": round(time.monotonic() - self.t0, 3), "spec": _spec_name(spec)})
        if "exc" in spec:
            raise TimeoutError("stub timeout")
        if "http" in spec:
            status, body = spec["http"]
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, io.BytesIO(body.encode()))
        if spec.get("ollama") or "/api/chat" in url:
            return FakeResponse(build_ndjson(spec, payload), delay=float(spec.get("delay") or 0.0))
        if not payload.get("stream"):
            content = spec.get("content")
            if callable(content):
                content = content(payload)
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content or ""},
                                            "finish_reason": spec.get("finish_reason", "stop")}],
                               "usage": {"completion_tokens": spec.get("usage") or 0}}).encode()
            return FakeResponse(body)
        return FakeResponse(build_sse(spec, payload), delay=float(spec.get("delay") or 0.0))


def _spec_name(spec: dict) -> str:
    return spec.get("_name") or ("http" if "http" in spec else "exc" if "exc" in spec else "sse")


# ── reply spec builders ──────────────────────────────────────────────────
def rejected(**over) -> dict:
    d = {"_name": "rejected", "content": json.dumps({"verdict": "rejected", "reason": "already handled"}),
         "finish_reason": "stop", "usage": 24}
    d.update(over)
    return d


def confirmed_for(**over) -> dict:
    def _c(payload):
        user_msg = payload["messages"][-1]["content"]
        m = re.search(r"Code at that location:\n```\n(.*?)\n```", user_msg, re.S)
        lines = [ln.strip() for ln in (m.group(1) if m else "").splitlines() if ln.strip()]
        ev = lines[0][:200] if lines else "def "
        return json.dumps({"verdict": "confirmed", "evidence": ev, "reason": "the claim holds"})
    d = {"_name": "confirmed", "content": _c, "finish_reason": "stop", "usage": 30}
    d.update(over)
    return d


def transport_empty(**over) -> dict:
    """Degraded provider: role-only chunk, finish_reason=stop, completion_tokens=0."""
    d = {"_name": "transport", "content": None, "finish_reason": "stop", "usage": 0}
    d.update(over)
    return d


def exhausted_empty(**over) -> dict:
    """Budget burnt: no content, finish_reason=length, completion_tokens == max_tokens."""
    d = {"_name": "exhausted", "content": None, "finish_reason": "length",
         "usage": lambda p: int(p.get("max_tokens") or 0)}
    d.update(over)
    return d


def reasoning_only(**over) -> dict:
    d = {"_name": "reasoning_only", "content": None, "finish_reason": "length",
         "reasoning": "Let me think about whether the claim holds... " * 5,
         "usage": lambda p: int(p.get("max_tokens") or 0)}
    d.update(over)
    return d


def garbled(**over) -> dict:
    d = {"_name": "garbled", "content": "Sure! The problem is present, I think. verdict: maybe",
         "finish_reason": "stop", "usage": 15}
    d.update(over)
    return d
