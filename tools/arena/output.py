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
from typing import Any, Iterable, Mapping

MASK = "***"

#: A key is secret when it is, or contains as a whole word, one of these.
SECRET_WORDS = frozenset({"key", "apikey", "token", "secret", "password", "passwd"})

# camelCase boundary: `apiKey` → `api Key`, `HTTPToken` → `HTTP Token`.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[_\-.]+")

# `scheme://user:pass@host` — the whole userinfo part goes, user included.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")
# `api_key=…`, `token=…` in a query string or a form-style line. The value runs
# to the next separator, so `&x=1` after it survives. `apikey` and `passwd` are
# here too, so a value `mask` hides by its key cannot leak by its text.
_KV_SECRET = re.compile(
    r"(?P<k>\b(?:api_?key|key|token|secret|passw(?:or)?d)=)[^&\s;,]+",
    re.IGNORECASE,
)


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
    return _KV_SECRET.sub(lambda m: m.group("k") + MASK, text)


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
