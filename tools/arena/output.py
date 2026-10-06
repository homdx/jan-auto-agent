"""tools/arena/output.py — AR-1: one way to print, one way to hide secrets.

`emit` is the only path from a command's rows to stdout, and it always runs
the rows through `mask` (by key) and `scrub` (inside every string), so a later
ticket cannot print a profile's `api_key` or a `base_url` carrying `user:pass@`
by forgetting to. `refuse` is the only path for a refusal: one scrubbed stderr
line, exit code 2 (principle 9 — one line; principle 6 — no key on screen).

Masking keys on whole words, not substrings: `tokens` is a usage count that
`run view -o json` prints as a number, `max_tokens` is a limit, `monkey` merely
contains `key` — none of them is a secret, and masking them would make the
JSON useless. `api_key`, `apiKey`, `gate_token`, `client-secret` are.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from typing import Any, Iterable, Mapping

MASK = "***"

#: A key is secret when it is, or contains as a whole word, one of these.
SECRET_WORDS = frozenset({"key", "apikey", "token", "secret", "password", "passwd"})

# camelCase boundary: `apiKey` → `api Key`, `HTTPToken` → `HTTP Token`.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[_\-.]+")

# `scheme://user:pass@host` — the whole userinfo part goes, user included, up to
# the LAST `@` before the path: a password may hold an unencoded `@` (bug 171).
# Bug 170: the scheme starts only where a scheme-character run starts — without
# the look-behind every position of a long word was a new start, O(n²). The
# userinfo class stops at `/`, `?`, `#` and whitespace, so a run is bounded by
# the next `/` and the greedy match backtracks inside it only.
_URL_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/?#\s]+@")
# `api_key=…`, `token=…` in a query string or a form-style line. The value runs
# to the next separator, so `&x=1` after it survives. `apikey` and `passwd` are
# here too, so a value `mask` hides by its key cannot leak by its text.
# Bug 169: the secret word may be the last `_`/`-`/`.` part of a longer name
# (`OPENAI_API_KEY=`, `access_token=`) — `\b` alone never fires after a `_`.
# One flat character class for the name (no nested repetition: a long line is
# linear, never a backtracking stall); `_kv_sub` then checks the name *ends* in
# a secret word, so `monkey=` and `tokens=` stay as they are.
_KV_PAIR = re.compile(r"(?<![A-Za-z0-9_.\-])(?P<k>[A-Za-z0-9_.\-]+=)(?P<v>[^&\s;,]+)")
_KV_SECRET_LAST = frozenset({"key", "apikey", "token", "secret", "password", "passwd"})


def _kv_sub(match: "re.Match[str]") -> str:
    parts = _words(match.group("k")[:-1])  # `_`/`-`/`.` and camelCase: `accessToken=` too
    if parts and parts[-1] in _KV_SECRET_LAST:
        return match.group("k") + MASK
    return match.group(0)


def _words(key: str) -> list[str]:
    """Split *key* on `_`, `-`, `.` and camelCase, lower-cased."""
    return [w.lower() for w in _SEPARATORS.split(_CAMEL.sub("_", key)) if w]


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return any(w in SECRET_WORDS for w in _words(key))


def mask(mapping: Any) -> Any:
    """Return a copy of *mapping* with every secret-keyed value replaced by `***`.

    Recursive over dicts and lists; the input is never mutated. Non-container
    values come back as they are, so `tokens: 1234` stays a number.
    """
    if isinstance(mapping, Mapping):
        return {
            k: (MASK if _is_secret_key(k) else mask(v)) for k, v in mapping.items()
        }
    if isinstance(mapping, (list, tuple)):
        return [mask(v) for v in mapping]
    return mapping


def scrub(text: str) -> str:
    """Return *text* with URL credentials and `secret=value` pairs replaced by `***`."""
    text = _URL_USERINFO.sub(lambda m: m.group("scheme") + MASK + "@", text)
    return _KV_PAIR.sub(_kv_sub, text)


def _scrub_all(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        return {k: _scrub_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_all(v) for v in value]
    return value


def emit(
    rows: Iterable[Mapping[str, Any]],
    columns: list[str],
    fmt: str,
    stream=None,
) -> None:
    """Print *rows* as a left-aligned table over *columns*, or as JSON.

    Every row goes through `mask` then `scrub` first, whatever *fmt* is.
    JSON keeps numbers as numbers; the table shows them with `str`.
    """
    out = stream if stream is not None else sys.stdout
    clean = [_scrub_all(mask(dict(r))) for r in rows]
    if fmt == "json":
        print(json.dumps(clean), file=out)
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in columns] for r in clean]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    for line in [list(columns)] + cells:
        print("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip(), file=out)


def refuse(msg: str) -> int:
    """Print `arena: <scrubbed msg>` as one stderr line and return 2."""
    one_line = " ".join(scrub(str(msg)).splitlines())
    print(f"arena: {one_line}", file=sys.stderr)
    return 2


# ── AR-14: the flow block — "you are here, the error is here" ─────────────────
#: The three marks of a flow step: done, the step that failed, not reached.
MARKS = {"ok": "[✓]", "fail": "[✗]", "todo": "[ ]"}

#: A refusal line's width, so `where:` wraps and the continuation lines align.
REFUSE_WIDTH = 78


def flow_line(flow) -> str:
    """`(step, state[, detail])` → `[✓] a → [✗] b: why → [ ] c`, one line."""
    parts = []
    for item in flow:
        step, state = item[0], item[1]
        detail = item[2] if len(item) > 2 else ""
        text = f"{step}: {detail}" if detail else step
        parts.append(f"{MARKS.get(state, MARKS['todo'])} {text}")
    return " → ".join(parts)


def flow_json(flow) -> list:
    """The same steps as `[{"step": …, "state": …}]` for `-o json`."""
    return [{"step": item[0], "state": item[1]} for item in flow]


def _one_line(text: str) -> str:
    """*text* as one scrubbed line — a message that carries a newline is not one."""
    return " ".join(scrub(str(text)).splitlines())


def _wrap(label: str, text: str) -> str:
    """`  <label>: <text>` wrapped to `REFUSE_WIDTH`, the continuation lines aligned
    under the text, not the label."""
    prefix = f"  {label}: "
    lines = textwrap.wrap(scrub(str(text)), width=max(24, REFUSE_WIDTH - len(prefix)),
                          break_long_words=False, break_on_hyphens=False) or [""]
    return "\n".join([prefix + lines[0]]
                     + [" " * len(prefix) + line for line in lines[1:]])


def _hint_line(hint: Mapping) -> str:
    """`  → <why>:  <command>` — the command stays on one line, so it is pasteable."""
    why = scrub(str(hint.get("why") or ""))
    command = scrub(str(hint.get("command") or ""))
    text = f"{why}:  {command}" if command else why
    return f"  → {text}"


def refuse_ctx(msg, *, where=None, ticket=None, flow=None, hints=None, fmt="table",
               stream=None) -> int:
    """A refusal with the flow block: the error line, then `where:`, `ticket:` and
    `flow:`, then one `  → ` hint per way out. Exit 2, everything on stderr.

    `flow` is `(step, state[, detail])` and `hints` `[{"why": …, "command": …}]`.
    `-o json` puts the same under `error`, `where`, `ticket`, `flow` and `hints`.

    Every field is printed: one the failing step did not reach yet is `?`, never
    dropped and never guessed.
    """
    out = stream if stream is not None else sys.stderr
    message = _one_line(msg)
    if fmt != "json":
        print(f"arena: {message}", file=out)
        print(_wrap("where:", str(where) if where else "?"), file=out)
        print(_wrap("ticket:", str(ticket) if ticket else "?"), file=out)
        print(f"  flow:  {scrub(flow_line(flow))}", file=out)
        for hint in hints or []:
            print(_hint_line(hint), file=out)
    else:
        # one object a script can `json.loads`, the same fields as the text block
        print(json.dumps({
            "error": message,
            "where": scrub(str(where) if where else "?"),
            "ticket": scrub(str(ticket) if ticket else "?"),
            "flow": flow_json(flow) if flow else [],
            "hints": [{"why": scrub(str(h.get("why") or "")),
                       "command": scrub(str(h.get("command") or ""))}
                      for h in (hints or [])],
        }), file=out)
    return 2
