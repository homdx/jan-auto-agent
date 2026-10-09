# 169 — `output.scrub` leaks `OPENAI_API_KEY=…`, `access_token=…`, `CLIENT_SECRET=…`

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 165 — its first fix was quadratic and was redone together with ticket 166 there
**Severity:** MEDIUM
**File:** tools/arena/output.py
**Symbol:** _KV_SECRET, scrub
**Round:** 169
**Size:** S
**Also touches:** tests_bugfix/test_arena_scrub_169_170.py

## The bug

`_KV_SECRET` is `\b(?:api_?key|key|token|secret|passw(?:or)?d)=`. `\b` needs a word/non-word boundary, and `_` is a word character, so any **prefixed** name never matches: the secret word sits after `_`, with no boundary in front of it. The module docstring names `gate_token` and `client-secret` as secrets, but only the `-` form is caught.

Reproduced:

```
access_token=abc        -> access_token=abc        (leaked)
x?gate_token=abc&y=1    -> unchanged               (leaked)
CLIENT_SECRET=s3        -> unchanged               (leaked)
OPENAI_API_KEY=sk-1     -> unchanged               (leaked)
api-key=abc             -> api-key=***             (ok)
```

`scrub` is the only text guard on every `arena` line: `refuse` (git and kilo stderr), `_run_child`'s printed run line (it includes profile `extra` and the `--` passthrough), `models._hint` and `models._hide` (provider errors, `base_url` echoes). An `extra = --env OPENAI_API_KEY=sk-…` or a provider error echoing `?access_token=…` goes to the screen and into logs in clear. Not covered by the existing test (`apikey=a1 passwd=p2 monkey=banana tokens=12`).

## Fix

Match the secret word as the last `_`/`-`/`.`-separated part of the name, not after a `\b`:
`(?P<k>(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_\-.])*(?:api_?key|key|token|secret|passw(?:or)?d)=)[^&\s;,]+` (case-insensitive), so `monkey=` and `tokens=` stay untouched. Optionally also mask the `--x-key VALUE` / `Authorization: Bearer X` spellings.

## Tests

1. The four leaked strings above come out as `NAME=***`; `&y=1` survives.
2. `monkey=banana tokens=12 max_tokens=5` are unchanged.
3. `_run_child` printing a line with `OPENAI_API_KEY=sk-1` in the passthrough shows `***`.

**Landed:** with 170 and 171 in one `tools/arena/output.py`. The name is one flat class `[A-Za-z0-9_.\-]+=` behind a look-behind and `_kv_sub` decides by the LAST word of the name (`output._words`, so `accessToken=` and `openaiApiKey=` are found too — the camelCase spelling the ticket's own fix did not reach); the regex of the Fix section above is the first attempt, quadratic, and was not landed.
