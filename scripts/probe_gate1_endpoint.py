#!/usr/bin/env python3
"""scripts/probe_gate1_endpoint.py — is the Gate-1 presence endpoint ready for
a RUN-9 run?

Two tiny live calls shaped exactly like Gate 1's presence check — the same
profile resolution (``[gate1] presence_llm_profile``), the same system
prompt, the same payload fields (``response_format``, ``think`` /
``think_effort``, ``stream: true`` + ``stream_options``) — and a report of what
the provider actually sends back, which is what RUN-9's empty-reply
classification runs on:

* a usage chunk (``completion_tokens``) — the number that tells "exhausted
  its budget and said nothing" from "HTTP 200 with an empty stream";
* whether ``stream_options`` was accepted, or 400-rejected and stripped
  (fine — replies then just carry no token count);
* whether reasoning text is visible with ``think = true`` — a think spill
  is recognised as exhaustion only if the provider streams it under a key
  ``request_completion`` reads (``reasoning_content`` / ``reasoning`` /
  ``thinking``);
* whether ``think = false`` is honoured — the ladder's last rung is real
  only then; otherwise expect ``presence_nothink_ignored = 1`` in the split.

Why a script: the contest bench proved the code against a scripted provider;
this is the one question a stub cannot answer. Costs two prompts plus a few
hundred completion tokens. Prints no secret — the key stays in the headers.

    python3 scripts/probe_gate1_endpoint.py --config agents_128k.ini

Exit status: 0 — both calls came back (READY, possibly with degraded
classification, spelled out); 1 — a call failed (NOT READY).
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as _llm_stream  # noqa: E402
import tools.auto.gate1_filter as G  # noqa: E402
from tools.config_safe import safe_getboolean  # noqa: E402

# A claim that is plainly NOT present in the code shown — every model should
# answer ``rejected``; the text of the verdict is not what is measured, the
# metadata around it is.
_PROBE_INSTRUCTION = "The function `add` returns `a - b` instead of `a + b`."
_PROBE_LOCATION = "probe.py:1  add"
_PROBE_CODE = "def add(a, b):\n    return a + b\n"


def _resolve_filter(config: configparser.ConfigParser) -> "G.Gate1Filter":
    """The shared-provider resolution ``run_gate1`` does, then the filter —
    whose ``_presence_*`` attributes are the profile Gate 1 really calls."""
    active = config.get("api", "active", fallback="local")
    section = f"api_{active}"
    return G.Gate1Filter(
        config=config,
        base_url=config.get(section, "base_url"),
        api_key=config.get(section, "api_key", fallback=""),
        model=config.get(section, "model"),
        api_format=config.get(section, "api_format", fallback="openai"),
        verify_ssl=safe_getboolean(config, "api", "verify_ssl", fallback=True),
    )


def _one_call(flt, *, think, timeout: float, error_retries: int) -> dict:
    """One presence-shaped call. Returns a plain dict — text, meta, the
    payload's non-message fields, or the exception — never raises."""
    url, headers, payload = _llm_stream.build_chat_request(
        base_url=flt._presence_base_url, api_key=flt._presence_api_key,
        model=flt._presence_model, api_format=flt._presence_api_format,
        temperature=flt._presence_temperature, max_tokens=flt._presence_max_tokens,
        system=flt._system,
        user_msg=G._USER_PROMPT_TMPL.format(
            instruction=_PROBE_INSTRUCTION, location=_PROBE_LOCATION,
            code_block=_PROBE_CODE, grounding_notes="",
        ),
        num_ctx=flt._presence_num_ctx, think=think,
        response_format=flt._presence_response_format,
        think_effort=flt._presence_think_effort,
        stream=True,
    )
    fields = {k: v for k, v in payload.items() if k != "messages"}
    out = {"think": think, "url": url, "fields": fields, "error": None,
           "text": "", "meta": None, "verdict": None}
    try:
        text, meta = _llm_stream.request_completion_ex(
            url, headers, payload, timeout, stream=True,
            api_format=flt._presence_api_format,
            ssl_context=flt._presence_ssl_context,
            error_retries=error_retries, error_retry_wait_sec=3.0,
        )
    except Exception as exc:  # noqa: BLE001 — the report IS the failure
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return out
    cleaned = _llm_stream.strip_think(text)
    out["text"] = cleaned
    out["meta"] = meta
    try:
        parsed = json.loads(cleaned)
        out["verdict"] = parsed.get("verdict") if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        out["verdict"] = None
    if cleaned.strip() == "":
        out["empty_kind"] = G._classify_empty_reply(meta, fields.get("max_tokens"))
    return out


def _fmt_call(i: int, c: dict) -> str:
    head = f"call {i} (think={c['think']}):"
    if c["error"]:
        return f"{head:<22} FAILED  {c['error']}"
    m = c["meta"]
    body = (f"ok  elapsed={m.elapsed:.1f}s  finish_reason={m.finish_reason}  "
            f"completion_tokens={m.completion_tokens}  reasoning_chars={m.reasoning_chars}  "
            f"content_chunks={m.content_chunks}  text={len(c['text'])} chars")
    if c.get("empty_kind"):
        body += f"  EMPTY -> classified {c['empty_kind']}"
    elif c["verdict"] is not None:
        body += f"  verdict={c['verdict']}"
    else:
        body += "  (not a JSON verdict)"
    return f"{head:<22} {body}"


def report(flt, calls: list[dict], *, profile_note: str, out=None) -> int:
    """Print the readiness report; return the exit status."""
    out = out if out is not None else sys.stdout
    url = flt._presence_base_url
    model = flt._presence_model
    p = print
    p(f"Gate-1 presence endpoint: {url}  model={model}  "
      f"api_format={flt._presence_api_format}  {profile_note}", file=out)
    p(f"  think={flt._presence_think}  think_effort={flt._presence_think_effort}  "
      f"response_format={flt._presence_response_format}  "
      f"max_tokens={flt._presence_max_tokens}  "
      f"presence_empty_retries={getattr(flt, '_presence_empty_retries', '?')}", file=out)
    if calls:
        p("  payload fields (call 1): " + ", ".join(
            k if k in ("model", "temperature", "max_tokens", "stream") else f"{k}={json.dumps(v)}"
            for k, v in calls[0]["fields"].items()), file=out)
    p("", file=out)
    for i, c in enumerate(calls, 1):
        p(_fmt_call(i, c), file=out)
    p("", file=out)

    failed = [c for c in calls if c["error"]]
    ok = [c for c in calls if not c["error"]]
    on = next((c for c in ok if c["think"] is not False), None)
    off = next((c for c in ok if c["think"] is False), None)
    degraded: list[str] = []
    lines: list[str] = []

    def row(label: str, text: str) -> None:
        lines.append(f"  {label + ' ':.<22} {text}")

    if ok:
        toks = [c["meta"].completion_tokens for c in ok]
        if all(t is not None for t in toks):
            row("usage chunk", f"yes (completion_tokens={', '.join(map(str, toks))}) — "
                               "empty replies are classified by token count")
        else:
            row("usage chunk", "NO — the provider sends none; an empty reply is classified "
                               "by finish_reason / reasoning only")
            degraded.append("no usage chunk")
    # The "stop sending it" memories are keyed by the request URL (the
    # /chat/completions endpoint), not the base_url the profile names.
    so_key = ((calls[0]["url"] if calls else url), model)
    if _llm_stream.stream_options_is_supported(*so_key):
        row("stream_options", "accepted")
    else:
        row("stream_options", "rejected with HTTP 400 — stripped and remembered for this "
                              "(url, model); no token counts this run")
        if "no usage chunk" not in degraded:
            degraded.append("stream_options rejected")
    if not _llm_stream.response_format_is_supported(*so_key):
        row("response_format", "rejected with HTTP 400 — stripped and remembered")
        degraded.append("response_format rejected")
    else:
        row("response_format", "accepted" if flt._presence_response_format else "not requested")
    if on is not None and on["think"] is True:
        if on["meta"].reasoning_chars > 0:
            row("reasoning visible", f"yes ({on['meta'].reasoning_chars} chars with think=on) — "
                                     "a think spill counts as exhaustion")
        else:
            row("reasoning visible", "no reasoning text seen with think=on — either the model did "
                                     "not think or the provider streams it under a key not read; "
                                     "exhaustion is then told by finish_reason / token count")
            degraded.append("reasoning not visible")
    if off is not None:
        if off["meta"].reasoning_chars == 0:
            row("think=off honoured", "yes — the ladder's no-think rung is real")
        else:
            row("think=off honoured", f"NO — {off['meta'].reasoning_chars} reasoning chars arrived "
                                      "with think=false; expect presence_nothink_ignored = 1 and "
                                      "one WARNING per run")
            degraded.append("think=off ignored")
    empties = [c for c in ok if c.get("empty_kind")]
    if empties:
        row("probe replies", f"{len(empties)} of {len(ok)} EMPTY — the very case RUN-9 handles; "
                             "watch the `empty t/x` column")
    for ln in lines:
        p(ln, file=out)
    if failed:
        p(f"verdict: NOT READY — {len(failed)} of {len(calls)} call(s) failed", file=out)
        return 1
    if degraded:
        p("verdict: READY (degraded: " + "; ".join(degraded) + ")", file=out)
    else:
        p("verdict: READY", file=out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="agents ini the run will use")
    ap.add_argument("--calls", choices=("both", "think", "nothink"), default="both",
                    help="which calls to make (default both: config think, then think=off)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="per-call timeout in seconds (default: the filter's own [loop] timeout)")
    ap.add_argument("--error-retries", type=int, default=2,
                    help="HTTP error retries per call (the run uses request_completion's default)")
    ap.add_argument("--quiet", action="store_true", help="hide WARNING log lines")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not config.read(args.config, encoding="utf-8"):
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    flt = _resolve_filter(config)
    profile = config.get("gate1", "presence_llm_profile", fallback="").strip()
    profile_note = f"profile=[{profile}]" if profile else "profile=shared provider"
    timeout = args.timeout if args.timeout is not None else flt._timeout

    calls = []
    if args.calls in ("both", "think"):
        calls.append(_one_call(flt, think=flt._presence_think, timeout=timeout,
                               error_retries=args.error_retries))
    if args.calls in ("both", "nothink"):
        calls.append(_one_call(flt, think=False, timeout=timeout,
                               error_retries=args.error_retries))
    return report(flt, calls, profile_note=profile_note)


if __name__ == "__main__":
    sys.exit(main())
