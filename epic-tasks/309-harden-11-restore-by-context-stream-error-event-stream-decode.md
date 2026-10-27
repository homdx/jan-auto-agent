# HARDEN-11 — a restored key goes to the wrong provider after a structural edit, a stream that ends in an error event returns the half reply as a normal one, one invalid byte in a stream loses the whole reply

**Status:** landed (round 309: step-5-preview as the base — a masked line is restored by its place (the nearest unmasked line above it) and a line with no original in its place stays the placeholder with a warning (A); an `error` object or `finish_reason: "error"` on a stream is a failure of the call, retried before the first token and a `RuntimeError` after it (B); a stream line with a bad byte is decoded with replacement characters and the rest of the reply is kept (C)). The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: a real key is written under another provider's section); MEDIUM (B, C)
**File:** `tools/llm_stream.py`
**Symbol:** A: `restore_masked_secrets` (called from `Coder._write_files`). B/C: the SSE loop of `request_completion` (`line = raw_line.decode("utf-8-sig")`, the `done`/`token` handling, `_meta_from_chunk`)
**Round:** 309
**Size:** S
**Depends on:** HARDEN-5 (303), HARDEN-9 (307, same file), all landed or queued
**Also touches:** `tests/`, `tests_bugfix/` (the HARDEN-5 B restore tests must stay green)

---

## Part A — a masked line is restored by position, not by place

### What happens today

`restore_masked_secrets(original, reply)` keeps, for every original line the mask changed, a queue of the real lines per masked text, and gives them back **in order** to the reply lines that equal that masked text. Three provider sections that all mask to `api_key = here_your_key` share one queue. On `cd6fa18`, an ini with `[openai]`, `[openrouter]`, `[groq]`, each with its own key, a faithful reply that only **deletes** `[openai]` and its line:

```
[openrouter]
api_key = <the OpenAI key>
[groq]
api_key = <the OpenRouter key>
```

Adding a placeholder `[anthropic]` section first copies the OpenAI key into it and leaves `[groq]` without one; reordering the three sections swaps all three keys. A real key is written under the wrong provider, and nothing refuses it.

### What it does

* A reply line is restored only when its **place** matches: the nearest preceding line of the reply that is not itself a masked line (a section header, a key above it) is the same line that preceded the original. When no original line matches by place the line is left as the placeholder and a warning is logged naming the line, so a structural edit never moves a key.
* A faithful reply (no structural edit), a reply that only changes other lines, and two identical lines in one place are restored as today.

### Tests (sketch)

The three cases above (delete, add, reorder) and the order-preserving cases of HARDEN-5 B.

---

## Part B — an error event in the middle of a stream is a normal end

### What happens today

The SSE loop ignores a chunk with an `error` object and takes `finish_reason: "error"` as an ordinary finish. On `cd6fa18`, a local server answering a streamed request:

| stream | `request_completion(..., stream=True)` |
|---|---|
| two tokens, then `{"error": {"message": "upstream died", "code": 502}}`, then `finish_reason: "error"` | returns `'def f():\n return 1'` (the half reply, as a success) |
| `{"error": {"message": "rate limited", "code": 429}}` only | returns `''` |

Neither raises, so the retry and 429 logic never run; callers (the Coder, the summariser, the search agent, the validator) take a truncated reply for the model's answer.

### What it does

* A chunk with a top-level `error` object, or a final `finish_reason` of `"error"`, is a failure of the call: with no token emitted yet it is retried like a dropped connection (a 429 code like a 429 status), and after tokens were emitted it raises a `RuntimeError` naming the error message, as the other mid-stream failures do — never a half reply returned as a success.

### Tests (sketch)

The two streams above through a local server; a normal stream, a stream ending with `finish_reason: "length"` and a stream with an empty `choices` list unchanged.

---

## Part C — one invalid byte in a stream line loses the reply with a raw exception

### What happens today

The streaming branch decodes each line with `raw_line.decode("utf-8-sig")`, strictly, inside a `try` that catches only network errors. On `cd6fa18`, a stream of one good token and one line `data: \xff\xfe bad`:

```
stream=True   ->  UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 6
stream=False  ->  RuntimeError: UnicodeDecodeError reading response body from …   (wrapped, and retried first)
```

The whole reply, tokens already read included, is lost, and the exception is not the `RuntimeError` the callers expect.

### What it does

* A line is decoded with `errors="replace"`: a line with a bad byte is parsed as far as it parses or skipped, and the good tokens before and after it are kept. If a caller needs it, the fault is logged once.

### Tests (sketch)

The stream above returns `"ok"`; a stream whose bad byte is inside a token returns the token with U+FFFD; a BOM on the first line still works.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `/edit` of an ini with three provider keys, asked to delete the first section, leaves the other two keys under their own sections; a provider that dies in the middle of a stream is retried or reported, not accepted as a short answer.

## Not in scope

The wording of the errors, the retry budget, and a provider whose error shape differs from `{"error": {...}}` or `finish_reason: "error"`.
