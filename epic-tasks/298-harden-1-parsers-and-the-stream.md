# HARDEN-1 — eight defects in what a model's reply can contain and what the request masks: the verdict parser, the stream chunk, the prompt store, a dotted reference, quadratic claim extraction, two digit regexes, a code fence inside a JSON string, and the secret mask over-reaching

**Status:** queued
**Severity:** MEDIUM (wrong answers on inputs nobody tested; none loses data on a normal run)
**File:** `tools/auto/inner_loop.py`, `tools/llm_stream.py`, `tools/collect/summarizer.py`, `tools/collect/verifier.py`, `tools/auto/story_bible.py`, `scripts/judge_epic_round.py`, `tools/prompt_store.py`, `tools/block_extractor.py`, `tools/search_agent.py`
**Symbol:** `_parse_verdict_soft`, `request_completion` (the OpenAI SSE branch), `PromptStore.get_store_summary`, `extract_block` / `find_references`, `extract_claims` / `_iter_access_citations`, `_AGE_RE`, `_HAS_DURATION`, `_API_KEY_LINE_RE` / `mask_api_key`, Gate 2's fence split in `inner_loop.py`, `strip_json_fence`, `_parse_llm_summary`
**Round:** —
**Size:** M
**Depends on:** CC review fixes 34–36 (landed, `c2ef95b`): this ticket starts from their code
**Also touches:** `tests/test_auto_verdict_boundary.py` and one pinned test per defect in `tests_bugfix/`

---

## Why

Two outside reviewers listed small defects; each entry here was checked against the code and **reproduced** or **read** (marked `run` / `read` below). Most of their other entries were already fixed or wrong and are refused in HARDEN-1's table. Each defect below is a wrong answer on an input nobody tested; each needs the guard the sibling code already has. A defect is done when it has its test and the suites are green unchanged (`.smoke_tests/ .regression_tests/`, then `tests_bugfix -n 4`).

## parsers and the stream (what a model's reply can contain)

### A1. The verdict parser reads `NOT BAD` as a rejection — `run`

**Where:** `tools/auto/inner_loop.py`, `_parse_verdict_soft`, the two `for token in ("REVISE", "REJECT", "NO")` loops (the whole reply and the per-line one).
**Evidence:** `startswith("NO")` has no word boundary. On the current code:

```
_parse_verdict_soft("NOT BAD")                       -> (False, 'T BAD', False)
_parse_verdict_soft("Nothing to add, looks good")    -> (False, 'thing to add, looks good', False)
_parse_verdict_soft("NOTE: fine")                    -> (False, 'TE: fine', False)
_parse_verdict_soft("Normally fine")                 -> (False, 'rmally fine', False)
_parse_verdict_soft("NOPE")                          -> (False, 'PE', False)
```

An approval that begins with a word starting `NO` becomes a REVISE whose reason is the word's tail, and that garbage goes to the Coder as the thing to fix:
a round burned on nothing. `NO: broken` and `NO, broken` must still reject.
**Fix:** the token matches as a word (`NO` followed by a non-letter or the end), in both loops, through one helper so the two cannot drift again. `REVISE` and
`REJECT` get the same boundary (`REVISED`, `REJECTION` are not verdicts).
**Test:** the five replies above are not a bare `NO` rejection (they fall to the later rules, which is the existing behaviour for prose: fail-open, approved);
`NO: x`, `No - x`, `NO` alone still `(False, …)`; a reply with the verdict on a later line behaves the same.

### A2. A streaming chunk with `choices: [null]` kills the call — `read`

**Where:** `tools/llm_stream.py`, `request_completion`, the `data:` branch (`choices[0].get("delta")`).
**Evidence:** `choices = chunk.get("choices") or []` then `choices[0].get(...)`: for `{"choices": [null]}` that is `None.get`, an `AttributeError`; the
`except` around it names only `json.JSONDecodeError` and `KeyError`, and the transport handler below it names only network errors. The call dies with every
token already read, and the caller retries (re-billing). `_meta_from_chunk` right under it already treats this shape (`isinstance(choices[0], dict)`).
**Fix:** skip a chunk whose `choices[0]` is not a dict, like `_meta_from_chunk`; a chunk that is not a dict at all is skipped too.
**Test:** a fake SSE stream with `{"choices":[null]}` between two content chunks returns the content of the other two; `[]`, `[null]`, `[1]`, `null` as
the chunk: no exception, the same text.

### A3. A stored prompt version without `version` takes `/prompts` down — `run`

**Where:** `tools/prompt_store.py`, `PromptStore.get_store_summary` (`stack[-1]["version"]`, `item["version"]`).
**Evidence:** `{"validator_agent": {"stack": [{"prompt": "x", "score": 0.5}]}}` → `KeyError: 'version'`; the REPL's `/prompts` has no handler, so one hand-edited
or older-format `prompts.json` ends the shell. A missing or `null` `score` is already shown as `(no score)` (fix 36); `version` was not given the same.
**Fix:** read `version` with a placeholder (`v?`) for a record that has none; the summary still lists the other records.
**Test:** a store with a record lacking `version` (first, middle, last) prints a table with `v?` and does not raise; a normal store prints as before.

### A4. A dotted reference can never be resolved — `run`

**Where:** `tools/block_extractor.py`, `extract_block` (and what `find_references` hands it, `tools/search_agent.py`).
**Evidence:** for `src = "def func(a): …"`, `extract_block(src, "func", ".py")` finds the definition and `extract_block(src, "module.func", ".py")` returns `''`.
A call through an imported module is the commonest Python reference, so the search reports it missing on every run and the validator is fed that as noise.
**Fix:** look a dotted name up by its **last segment** when the whole name is not defined (`module.func` → `func`; `Class.method` → the method inside the class
if the extractor can see it, else the name). The whole name still wins when it exists. Check what `find_references` emits first: if it already strips the
prefix for some languages, make the extractor and the emitter agree rather than fixing both.
**Test:** `module.func` finds `func`; `a.b.c` finds `c`; a name that is a real definition with a dot (a string key, not code) is unchanged; no match returns `''` as now.

---

### A5. Claim extraction is quadratic on a model's long text — `run`

**Where:** `tools/collect/verifier.py`, `extract_claims` → `_iter_access_citations`, `_LOCATION_RE`, `_SYMBOL_CITATION_RE`; called by `verify_repo` on every module's
`summary.purpose` and `summary.notes`, straight from the model, with no cap on the length.
**Evidence (current code, after fixes 34–36; seconds for 6 000 / 12 000 / 24 000 characters):**

| text | seconds |
|---|---|
| `"a[" * n/2` (brackets that never close) | 8.4 / 13.9 / 38.6 |
| `"word[ " …` | 2.3 / 10.8 / 34.4 |
| `"x[ " …` | 1.0 / 3.8 / 40.6 |
| `"a/" * n/2` (path characters that never reach `.py:`) | 0.01 / 0.02 / 0.05 (fixed by fix 36) |

Fix 36 made the path regexes linear but not the bracket walk: `_iter_access_citations` finds every `name[` start and walks forward to the matching `]`; when the brackets never
balance every start walks to the end of the sentence, O(n²) in a pure-Python loop. A long or looping Pass B answer holds `--collect` at 100 % CPU for minutes, with no error,
no log line and no timeout.
**Fix:** pair the brackets once with a stack, then read the starts (the reviewer's replacement, tested against the old function on 20 000 random bracket strings with 0
differences: 24 000 characters in 0.004 s):

```python
def _iter_access_citations(sentence):
    match, stack = {}, []
    for i, ch in enumerate(sentence):
        if ch == "[":
            stack.append(i)
        elif ch == "]" and stack:
            match[stack.pop()] = i
    out = []
    for m in _ACCESS_START_RE.finditer(sentence):
        if m.group(0).rsplit(".", 1)[-1] in _TYPING_BASE_NAMES:
            continue
        end = match.get(m.end())
        if end is not None:
            out.append(sentence[m.start():end + 1])
    return out
```

Check the result against the old function on the existing tests **and** on nested and interleaved brackets (`a[b[c]]`, `a[b]c]`) before accepting: the stack pairs differently from "the first `]` at depth 0" when brackets cross. A cap on the length of a sentence handed to `extract_claims` (a "claim" over about 2 000 characters is not a claim) is the second line of defence.
**Test:** `extract_claims("a[" * 12000, "m.py")`, `"word[ " * 4000` and `"x[ " * 8000` each finish in under a second; results equal the old function's on 20 000 random bracket strings.

### A6. Two regexes on a run of digits are quadratic — `run` (low reach)

**Where:** `tools/auto/story_bible.py` (`_AGE_RE = (\d+)\s*(?:лет|год)`), `scripts/judge_epic_round.py` (`_HAS_DURATION = \d+(?:\.\d+)?\s*[smh]`).
**Evidence:** a single line of `"1" * n`: `_AGE_RE` 3.7 s at 10 000 digits and 13.9 s at 20 000; `_HAS_DURATION` 4.3 s and 14.1 s. A run of thousands of digits in one line is very
unlikely, which is why this is low.
**Fix:** anchor the start with `(?<!\d)` or cap the line length.
**Test:** 20 000 digits on one line: under a second, and the matches on ordinary text are unchanged.

### A7. A reply with a code fence inside a JSON string is discarded as unparseable — `run`

**Where:** `tools/auto/inner_loop.py` (Gate 2's own copy: `raw.split("```json")[1].split("```")[0]` at lines 729 and 731), `tools/llm_stream.py` `strip_json_fence` (the **bare**
JSON case), and the callers that take a reply through it: `tools/collect/summarizer.py` `_parse_llm_summary`, `tools/validator_agent.py`, `tools/actions.py`, `tools/improvement_agent.py`.
Not a regression of fix 36: it predates it. Fix 36 repaired only the ```` ```json ````-wrapped body with a fence inside, and only in `strip_json_fence`.
**Evidence (current code):** a valid reply `{"purpose": "Retry helper.", "notes": "Typical use:\n```python\nretry(fetch, 3)\n```"}`:

| call | result |
|---|---|
| `strip_json_fence(bare)` | breaks: `JSONDecodeError`, the text is cut at the fence inside the string (`'python\nretry(fetch, 3)\n'`) |
| `strip_json_fence("```json\n" + bare + "\n```")` | parses (fix 36) |
| `_parse_llm_summary(bare)` | `('', '')`, no error: the "module left without a summary" case CC-15 (297) says collect never retries |
| Gate 2 (`inner_loop.py:729/731`), bare or wrapped, a verdict whose hint quotes a code sample | cut at the inner fence: garbage |

The reviewer's run of the real `LLMGate2Validator.approve()` with the model call stubbed (not repeated here): bare and wrapped rejections, and bare and wrapped **approvals**,
all end `approved=False unavailable=True`; the control with no fence anywhere is fine. So an APPROVED verdict is read as "validator unavailable", `InnerLoop` re-runs Gate 2
`validator_unavailable_retries` (default 2) times with a 60 s wait, and when the replies keep the fence the round ends unavailable; the controller resets the task to TODO and
**discards the passing candidate**. A rejection that quotes a code sample (the most useful hint) never reaches the Coder. The next run sends the same prompt and usually gets the
same shape of reply.
**Fix:** parse the whole stripped reply first and return it if it is valid JSON; only then fall back to fence stripping. Replace the inline split in `inner_loop.py` with
`strip_json_fence`, so Gate 2 gets fix 36's `_before_closing_fence` as well. The reviewer tested the wrapper on bare, wrapped, prose-plus-block, unclosed-fence, plain-fence and
no-fence replies: all six parse, where the current code fails the bare-with-fence one.
**Test:** the table's four rows return the object; the six reply shapes above all parse; Gate 2 with a stubbed model returns `approved=True` for a bare and a wrapped approval whose
text quotes a fence, and a rejection's hint reaches the Coder intact; no-fence replies are unchanged.

### A8. The secret mask rewrites code that is not a secret, and the model writes the placeholder back to disk — `run` (regression of fix 34)

**Where:** `tools/llm_stream.py`: `_API_KEY_LINE_RE`, `_API_KEY_JSON_RE`, `mask_api_key`, applied to every request by `_mask_payload_secrets`.
**Evidence (current code, `_mask_payload_secrets` on one user message):**

| text in the file | what the model is sent |
|---|---|
| `self.api_key = api_key` | `self.api_key = here_your_key` |
| `OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]` | `OPENAI_API_KEY = here_your_key` |
| `"api_key": "k1"` | `"api_key": "here_your_key"` |
| `api_key = sk-abc123` | `api_key = here_your_key` (the intended case) |
| `x = 1` | unchanged |

Fix 34 widened the mask: before it, only a bare `api_key = x` line was hit; now any assignment whose name contains `api_key` is. The text is the file the model is asked to rewrite **in
full** (`/edit`, and the Coder, which reads and writes whole files through the same request path). The model gives the placeholder back, the file is written, and working code becomes a
`NameError` (`here_your_key` is undefined) that nothing catches. The reviewer reproduced it end to end with `/edit` against a fake model that edits faithfully, asked to change one line
(`timeout = 30` → `60`): 3 lines changed on disk instead of 1 (`1a5f18a`: 1). Across this repository's own `.py` files the mask matches hundreds of lines (445 lines in 210 files by the
reviewer's count; I counted 43 assignment lines in `tools/`, `scripts/` and `main.py` alone with a looser pattern, so the numbers are not comparable — measure it in the test).
**Fix (the intent is to keep a real secret out of the request without touching code):**
1. Mask only a **literal** value: a quoted string, or a bare token that looks like a key (long, no `.`, `(`, `[`, spaces; not an identifier that is itself an expression). `self.api_key = api_key`
   and `OPENAI_API_KEY = os.environ[...]` are expressions, not secrets.
2. And/or restore on the way back: remember each masked line's original and put it back where the reply returns the placeholder, so a faithful rewrite round-trips (a model that changes the
   line is its own business).
Pick one and say which; the test below must pass under either.
**Test:** the five rows: only the 3rd (a literal) and 4th are masked, the first two are sent as they are; a faithful `/edit` round-trip over a file holding `self.api_key = api_key` changes
exactly the one line asked for; a real key in the same file is still not in the outgoing request.

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries, so nothing here can be written to pass them: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. A1, A5 and A7 are the ones that cost: A1 sends an approval to the Coder as a defect, A5 freezes `--collect`, A7 throws away an approved candidate, A8 writes `here_your_key` into working code: an approval that begins with `NO…` goes to the Coder as a defect. Judge A1 by hand; A2–A4 by the pinned tests. Free models are enough for A2–A4 and A6; A1, A5, A7 and A8 want a strong one (A5 must match the old function on crossing brackets).

## Refused (shared by HARDEN-1 … HARDEN-4, so they are not opened again)

| from the lists | verdict | why |
|---|---|---|
| executor timeout kills only the shell | **already fixed** | `executor.py` starts the command in a session of its own and `os.killpg(SIGKILL)` on timeout |
| `/edit` ignores symlinks | **already fixed** | `_resolve_within_base` uses `realpath` + `commonpath` (fix 34) |
| `/edit` re-encodes non-UTF-8 files | **already fixed** | bytes and encoding kept, backup by `copy2` (fix 34) |
| `strip_json_fence` breaks a `` ``` `` in a string | **already fixed** | `_before_closing_fence` (fix 36) |
| root commit's changed files are `[]` | **false** | `git_manager.py` already passes `--root` to `diff-tree` |
| `ticket_status` reads without `encoding` | **false as stated** | `LANG=C … show 144` passes: Python's UTF-8 mode covers the C locale; only a non-UTF-8 locale would fail (add `encoding="utf-8"` if touched anyway) |
| URL/host ports read as line numbers | **not reproduced** | `app.py:8080` is a line citation by design; a host without a file suffix is not an anchor |
| malformed `noqa` in `gate1_grounding` | **false** | the comments are valid; ruff's `RUF100` says the rule `BLE001` is not enabled here |
| `append_finding` lock / `utf-8-sig` | **not worth it** | one reviewer, one file, written in turn; the tool writes no BOM |
| dead `Coder._is_truthy_delete` drops `"1"` | **cosmetic** | no caller; delete the dead copy if a part touches `coder.py`, no test |
| unreachable `HTTPError` handler in the validator | **not shown** | `request_completion` may well raise `RuntimeError`; not proved either way |
| `inner_loop` silently drops a `None` code | **false** | a non-path entry in `files_written` gets its own visible note in the prompt (`inner_loop.py`, "B8") |
| `configparser` not imported in `manifest.py` | **false** | `import configparser` is line 31; `typing.get_type_hints` on the four functions runs clean |
| `_COLLECT_READS_LOCK` held across `load_readonly` | **by design** | every voter needs the same result; waiting for the first read is correct, and double-checked locking would not shorten the wait |
| `/edit` corrupts CRLF, mode, encoding, symlink; guard ignores symlinks (N2, N3) | **already fixed** | fix 34: `atomic_write_text` + `copy2` backup, `_resolve_within_base` uses `realpath` |
| concurrent exclusive writers both win without hard links (EX1-R) | **already fixed** | fix 34: `O_EXCL`; 0 of 300 trials with more than one winner |
| a path in `[collect] skip_dirs` is silently ignored (CC13-4) | **now a warning** | the entry is still ignored (names only), but `[collect] skip_dirs: '…' is a path, not a directory name — ignored` is printed |

## Not in scope

Parts B, C, D of the same list are HARDEN-2 (299), HARDEN-3 (300), HARDEN-4 (301). The retry budget of `request_completion`; what the validator asks the search for.
