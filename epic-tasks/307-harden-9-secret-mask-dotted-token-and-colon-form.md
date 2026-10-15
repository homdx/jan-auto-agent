# HARDEN-9 — the secret mask lets a dotted token and an unquoted `api_key: <value>` line reach the model

**Status:** open — FINAL, written 2026-10-15 against `0d28a9b`. Two gaps of one mask, each **reproduced by running the code** on that commit (the output is below). The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: a JWT or an OAuth token in a `.env` or INI line leaves the machine); MEDIUM (B)
**File:** `tools/llm_stream.py`
**Symbol:** `_value_is_secret_literal`, `_EXPRESSION_CHARS`, `_API_KEY_LINE_RE`, `mask_api_key`, `_mask_payload_secrets`
**Round:** —
**Size:** S
**Depends on:** HARDEN-1 (298), HARDEN-5 (303), both landed
**Also touches:** `tests/test_mask_api_key_forms.py`, `tests_bugfix/test_harden1_a8_mask_only_literals.py` (pins that must stay green), `tests_bugfix/`

---

## Why

`mask_api_key` runs on the wire, on every request, so what it lets through is sent to the provider. HARDEN-1 A8 and HARDEN-5 B made it leave code alone (`self.api_key = token`, `os.environ["X"]`); the price was that a **literal** which merely contains a dot, and a literal in the YAML form, are now taken for code or are not looked at. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a dotted token is taken for an attribute access

### What happens today

`_value_is_secret_literal` returns `False` for any bare value containing a character of `_EXPRESSION_CHARS`, and `.` is one of them (it is there for `config.api_key`). On `0d28a9b`, through `mask_api_key`:

```
API_KEY=eyJhbGciOiJIUzI1NiIs….eyJzdWIiOiIx….SflKxwRJSMeK…      (a JWT)        sent unchanged
OPENAI_API_KEY=ya29.a0AfH6SMBx9abcdefghij1234567890                             sent unchanged
api_key = eyJhbGci….eyJzdWIi….SflKx…  (INI form)                                sent unchanged
```

The same lines leave unmasked on `f50d9a2`, the commit before HARDEN-5, so this is an old gap and not something HARDEN-5 introduced.

### What it does

* A bare value with dots is a literal when it does **not** read as code: it is not `name.name…` of identifier parts that look like words, a call `name(…)` or an index `name[…]`; it is a long (16 characters or more) token whose parts mix letters and digits, or whose parts are base64url runs (a JWT has three of them). `config.api_key`, `self.settings.api_key`, `os.environ.get`, `settings.OPENAI_KEY` stay as they are.
* A masked value stays masked on a second pass, as now.

### Tests (sketch)

The three lines above are masked, in `.env`, INI, `export` and commented forms; the identifier-path lines of the HARDEN-1 A8 and HARDEN-5 B pins are unchanged.

---

## Part B — `api_key: <value>` without quotes is not looked at

### What happens today

`_API_KEY_LINE_RE` takes `=` only, and `_API_KEY_JSON_RE` takes a quoted value; a YAML line with a colon and no quotes is matched by neither. It is masked only when the value carries a known provider prefix (`sk-…`, which `_SECRET_PREFIX_RE` finds anywhere). On `0d28a9b`:

```
api_key: 0123456789abcdef0123456789abcdef                   sent unchanged   (hex)
api_key: 123e4567-e89b-12d3-a456-426614174000               sent unchanged   (uuid)
api_key: dGhpc2lzYXNlY3JldGtleXZhbHVlMTIzNDU2               sent unchanged   (base64)
api_key: AKIAIOSFODNN7EXAMPLE                               sent unchanged
api_key: ghp_abcdefghijklmnopqrstuvwxyz0123456789           sent unchanged
api_key: sk-abcdefghijklmnopqrstuvwxyz0123                  masked           (the control)
```

### What it does

* A line `<name>api_key: <value>` (YAML, with or without a leading `- ` or indentation) is masked by the same rule as the `=` form, `_value_is_secret_literal` deciding what a literal is, so a placeholder (`<your key>`, `changeme`) and a name (`api_key: api_key`) stay as they are and the quotes of a quoted value are kept.
* A Python dict entry, a type annotation (`api_key: str`, `api_key: Optional[str] = None`) and a `def f(api_key: str)` signature are code and are not touched: a value that is a bare identifier, a type or an expression is not a literal.

### Tests (sketch)

The table above; the type-annotation lines and `def` signatures of this repository's own sources are byte-identical before and after (the test runs the mask over `tools/` and `main.py` and compares).

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own `.py` files before and after and expects no line of code to change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A `.env` with a JWT and a YAML config with a hex key, sent through `/edit`, reach the provider with `here_your_key` in their place, and the edit writes the real values back.

## Not in scope

A bare identifier on the right of `=` that does not name the key (`self.api_key = token`): the mask rewrites it on the wire and `restore_masked_secrets` puts the real line back on write (HARDEN-5), so nothing reaches the disk changed. Secrets in places other than a key line (a bare token on a line by itself).
