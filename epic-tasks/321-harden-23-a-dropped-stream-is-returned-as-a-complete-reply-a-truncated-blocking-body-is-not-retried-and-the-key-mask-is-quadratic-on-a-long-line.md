# HARDEN-23 — a streaming reply cut by a dropped connection is returned as complete; an `IncompleteRead` on a blocking reply is never retried; `mask_api_key` costs 10–17 s on one 20,000-character line

**Status:** landed (round 321: laguna-s-2-1 as the base for A and B - a stream that ends with no [DONE], no finish_reason and no usage (so also a clean close after a whole content chunk) is retried while no token reached on_token and a RuntimeError naming the provider afterwards, an empty stream the same; http.client.HTTPException joins the retried transport errors of the blocking read, the stream read and _open() through one _RETRYABLE_TRANSPORT_ERRORS tuple (sensenova var4); C: the key-name patterns are anchored with (?<![\w.-]) (laguna), the blank-prefix of five patterns starts only at the start of a run of blanks, the XML attribute name is anchored, and the env attribute name has no dot - the own finds that made a line of 20,000 blanks, tabs, `apikey`, `env.` or `os.environ.` linear too)
**Severity:** MEDIUM (A: half a JSON verdict, half a file or half a ticket is parsed as whole; C: a minified file stalls a call)
**File:** `tools/llm_stream.py`
**Symbol:** A: `request_completion` (the stream-reading loop). B: `request_completion` (`stream=False`, the read-loop `except`). C: `mask_api_key`
**Round:** 321
**Size:** M
**Depends on:** none
**Also touches:** `tests_bugfix/` pins of HARDEN-11 (error event fails the call), `tests/`

---

## Why

An outside review of `5c403f1` found A and B with a local HTTP server that sends a partial reply and drops the connection, and C by timing the mask. A and B were confirmed on the head by reading the code; C was re-timed on `1e58233` (below). A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a dropped stream is a complete reply

A chunked HTTP/1.1 stream sends `The answer is: ` and `{"verdict": "TR`, then the server drops the connection (or ends the chunked body cleanly with no `finish_reason` and no `[DONE]`). `request_completion_ex` returns `'The answer is: {"verdict": "TR'` with `finish_reason=None`: no exception, no retry, no warning, in 0.0 s even with `error_retries=1`. HARDEN-11 made an *error event* fail the call; a silent drop (a proxy time-out, a provider restart) still passes as a normal reply, and Gate 1's budget check reads `finish_reason=None` like `stop`.

### What it does

A stream that ends with content but with neither a `finish_reason` nor `[DONE]` is a truncation: retried like a dropped connection when nothing reached `on_token` yet, otherwise a `RuntimeError` naming the provider (the error-event path's shape). A provider that closes cleanly without a marker is accepted only when its last line was a complete `data: {...}` chunk whose JSON parsed. A stream with a final `finish_reason: "stop"` and no `[DONE]` stays good; so do keep-alive comments, `event:` lines, CRLF endings, reasoning-then-content and a usage-only final chunk.

---

## Part B — a truncated blocking body is never retried

The `except (TimeoutError, ssl.SSLError, ConnectionError, URLError, ValueError)` of the blocking read says "a truncated or garbled body is retried like a dropped connection", but `http.client.IncompleteRead` is an `HTTPException`, not one of those: a server that sends `Content-Length: 92` and 40 bytes (or a chunked body cut after one chunk) raises `IncompleteRead(40 bytes read, 52 more expected)` at once, with neither the provider URL nor the retry count the other branches add.

### What it does

`http.client.HTTPException` joins the tuple in the blocking read, and the streaming read and the `_open()` call are checked the same way. The final error names the provider and the attempts.

---

## Part C — `mask_api_key` is quadratic on a long line

A 20,000-character word, hex or dash line costs 10–17 s per masking pass on the head (measured on `1e58233`: `"a"*20000` 17.5 s, 16 hex digits × 1250 14.4 s, `"ab-"*6666` 12.2 s); over the 1,521 tracked files the mask takes 24.5 s (0.5 s before HARDEN-1). The patterns of HARDEN-1 on were not anchored the way the old `^` ones were, so each start position scans the line to its end. A minified bundle or a base64 blob in a prompt stalls the call.

### What it does

Anchor the patterns where they can start (`(?<![\w.-])` or an atomic/possessive prefix, or a cheap pre-check on the prefix word before the regex runs); 20,000 characters of any of the three shapes masks in under a second, and the mask corpus count of changed lines does not move (the 28,000-file corpus of round 313, same numbers). Real files are not slower than now (libvirt.py ≈ 2.7 s).

---

## Tests (sketch)

A: a local server per shape (drop mid-stream, clean end without a marker, final `stop` without `[DONE]`, error event first) — truncation raises/retries, the good shapes return. B: `Content-Length` larger than the body, retried and then named. C: the three timing inputs under a bound with a wide margin (relative to a baseline of the same run), plus a table that the masked output of fifty representative lines is unchanged.

## Contest

One commit. The judge's checks are written after the round; the entries' tests are cross-tested; every diff is read.

## Not in scope

An SSE event split over several `data:` lines (rare); the extractor shapes of Rust, React wrappers, arrow members, C#, Dart.

## Why not the others (round 321)

A black box of 103 checks (Part A streams over a local server: cuts at a chunk boundary, mid-line, empty, Ollama, the shapes that must stay good; Part B blocking and chunked IncompleteRead; Part C 24 long lines and 50 masks compared with the base) scored the base 77, laguna 103, var4 99, var3 96, step-5 96, var1 96, var2 93, agnes 81, glm 68 (nemotron shipped no code beyond a 3454-line `.fixed` copy and was not scored). Only laguna's rule for A rejects a stream cut after a whole content chunk, which is the usual proxy drop; var4, var3, step-5 and var2 accept a last chunk that parses, so a cut at a chunk boundary came back as the answer. var2 and agnes also reached outside the ticket: var2 rewrote the HARDEN-7 `reply_text` pins (a list became JSON), agnes touched `collect/*` and `repo_ingest`. Laguna's anchors did not make a line of blanks, `apikey` repeated or `env.` repeated linear (5-15 s each); var4's guards did not either. The judge's `--cross` was cut off: every cell on a tree with the slow mask ran into the 900 s cell timeout, so the others' tests were run on the ideal as copies instead (99 passed, 36 failed - the failures are the "complete last chunk is accepted" and "an empty stream is a reply" designs, glm's IncompleteRead built with an integer, and var2's collect pins). Two HARDEN-11 fakes got a [DONE] line because a stream with none is a truncation now.

Open for the next tickets: a provider that really never sends a finish_reason, [DONE] or usage now fails after the retries (watch the first live run); `_BARE_TOKEN_CMP_BEFORE_RE` is still quadratic on a 20,000-blank run when called on its own (mask_api_key does not reach it with such a run).
