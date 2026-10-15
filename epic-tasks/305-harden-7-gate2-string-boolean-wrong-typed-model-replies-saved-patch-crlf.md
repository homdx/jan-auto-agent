# HARDEN-7 — a model reply of the wrong JSON type: Gate 2 approves a rejection, one Gate 1 reply ends the planning, a validator or an improvement reply ends the shell; and the patch that keeps the work of a failed commit does not apply to a CRLF or non-UTF-8 file

**Status:** open — FINAL, written 2026-10-15 against `6ddbe7c`. Four defects from outside reviews, each **reproduced by running the code** on that commit (the output is below). Parts A to C are one class of defect — a reply that is valid JSON whose field has another
type than the prompt asked for, which small local models send — and Part D is a gap left by the landed HARDEN-2 B2. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: a change its own validator rejected is committed); MEDIUM (B, C, D)
**File:** `tools/auto/inner_loop.py` (A), `tools/auto/gate1_filter.py` and `tools/validator_agent.py` and `tools/actions.py` and `main.py` and `tools/formatter.py` (B, C), `tools/auto/git_manager.py` and `tools/git_run.py` and `tools/auto/controller.py` (D)
**Symbol:** A: `LLMGate2Validator.approve`. B: `Gate1Filter._parse_presence_response`. C: `ValidatorAgent.validate` (the `feedback` it returns), the callers that read it (`tools/actions.py`, `main.py`), `OutputFormatter.render`. D: `GitManager.diff_working_tree`, `run_git`, `Controller._save_uncommitted_patch`
**Round:** —
**Size:** M
**Depends on:** HARDEN-2 (299), HARDEN-5 (303), HARDEN-6 (304), all landed
**Also touches:** `tools/llm_stream.py` (a shared reader of a reply field, if the parts share one), `tests/`, `tests_bugfix/`

---

## Why

The repository already treats this class as a bug: `architect.py` has `_to_str_or_empty` and `_to_bool_or`, with "BUGFIX (audit)" docstrings, for exactly a model that sends a list where a string was asked. These are the places that never got it. Each one
is a model-dependent failure, but the model is a small local one by design, and what it costs is large: a rejected change in the repository (A), every paid Gate 1 verdict (B), the answer after every LLM call has been paid for (C), the only copy of a task's work (D).

---

## Part A — Gate 2 approves a candidate its own validator rejected

### What happens today

`LLMGate2Validator.approve` reads the verdict as `approved = bool(parsed.get("approved", False))`. A string is true: `bool("false")` is `True`. With the HTTP call stubbed and the reply `{"approved": <value>, "feedback": "REJECT: breaks the public API"}`, on `6ddbe7c`:

| `approved` in the reply | `approve()` returns |
|---|---|
| `true` | `True` (right) |
| `false` | `False` (right) |
| `"false"` | **`True`** |
| `"no"` | **`True`** |
| `"False"` | **`True`** |
| `"rejected"` | **`True`** |
| `0`, `null` | `False` (right) |

Gate 2 is the last check before a commit and the class is documented as fail-closed.

### What it does

* The reply field is read **fail-closed**: only an explicit yes approves — `true`, or the strings `true`, `yes`, `y`, `1`, `approved`, `ok` (any case, trimmed) and the number `1`. Everything else, a string such as `"false"`, `"no"`, `"rejected"` included, is a rejection, and the feedback of the reply is kept.
* The behaviour for `true`, `false`, `0` and `null` does not change.

### Tests (sketch)

The table above, row by row, through `approve()` with the model call stubbed; a rejection keeps its critique; the existing Gate 2 tests green unchanged.

---

## Part B — one malformed Gate 1 reply ends the whole filter

### What happens today

`Gate1Filter._parse_presence_response` strips three fields of the reply without looking at their type. On `6ddbe7c`, with the reply `{"verdict": "confirmed", "reason": …, "evidence": …}`:

| field sent as | result |
|---|---|
| `evidence` a list | `AttributeError: 'list' object has no attribute 'strip'` |
| `reason` a number | `AttributeError: 'int' object has no attribute 'strip'` |
| `verdict` a list | `AttributeError: 'list' object has no attribute 'strip'` |
| all strings | `(False, "… no matching evidence quote …", False)` (the control) |

End to end, three candidates and the model call stubbed to answer `evidence` as a list: `Gate1Filter.filter()` **raises** `AttributeError: 'list' object has no attribute 'strip'` at the first presence check, so `filter_candidates` (called from `pipeline.py` and `plan_validator.py` without a guard) takes the planning phase down with it. The verdicts of every other candidate and the LLM calls already paid for are lost.

### What it does

* A field of another type is read as text (a list one item per line, a number or a boolean as its string, `None` as empty), and a reply whose `verdict` is not a recognisable word is an **unparseable** reply — the existing re-ask path — not an exception. One bad reply costs one candidate.

### Tests (sketch)

The three rows above through `filter()` over three candidates: the other two are judged, the bad one is re-asked once and then rejected; the all-string control unchanged.

---

## Part C — a validator or an improvement reply of the wrong type ends the shell

### What happens today

`ValidatorAgent.validate` returns the parsed reply as it is, so a `feedback` that is a list, a number or an object reaches the code that strips it. On `6ddbe7c`, with the HTTP call stubbed and the reply `{"status": "needs_fix", "feedback": <value>}`:

```
feedback = ["too long", "no tests"]   -> the caller's (validation.get("feedback") or "").strip():  AttributeError: 'list' object has no attribute 'strip'
feedback = 5                          -> AttributeError: 'int' object has no attribute 'strip'
feedback = {"a": 1}                   -> AttributeError: 'dict' object has no attribute 'strip'
feedback = "text"                     -> fine
```

The same line is in `tools/actions.py` (the `/edit` paths: four sites) and `main.py` (the pipeline); in the interactive shell nothing catches it (`main.py` catches `KeyboardInterrupt` and `EOFError` only), so the shell exits. `OutputFormatter.render` does the same
for the improvement agent's `explanation` and `improved_code` (`(improvement.get("explanation") or "").strip()`): it is the last step of a run, after every LLM call has been paid for.

### What it does

* The validator's `feedback` is a string when it leaves `ValidatorAgent.validate` (a list one item per line, any other non-string as its text), and the formatter reads `explanation` and `improved_code` the same way. The callers need no change if the one place is right, but none of them may raise on a non-string.

### Tests (sketch)

`validate()` with the three rows above returns a string `feedback`; `/edit` and the pipeline survive a list feedback; `render()` prints a list `explanation` and `improved_code` instead of raising; a string reply is byte-identical to today.

---

## Part D — the patch that keeps the work of a failed commit does not apply to a CRLF or a non-UTF-8 file

### What happens today

HARDEN-2 B2 saves the work as `.agent/uncommitted-<id>.patch` when a task's commit fails; the controller says "work saved to …patch". `GitManager.diff_working_tree` reads git's output in text mode, so `\r\n` becomes `\n` and each non-UTF-8 byte becomes U+FFFD. On `6ddbe7c`, a tracked CRLF file with one changed line, and a tracked latin-1 file with one changed line:

```
CRLF file:     diff_working_tree() has 0 carriage returns;  git diff HEAD has 4;
               after git checkout -- w.txt:  git apply  ->  error: patch does not apply  (rc 1)
latin-1 file:  the saved patch contains 2 U+FFFD;
               after git checkout -- l.py:   git apply  ->  error: patch does not apply  (rc 1)
```

A patch is applied as one piece, so one such file anywhere in the tree sinks the recovery of the task's other files as well.

### What it does

* The saved patch is the bytes git printed: a CRLF file and a non-UTF-8 file round-trip. `diff_working_tree` keeps returning a `str` for its existing callers (a lossless decoding such as `surrogateescape`), and the file the controller writes holds the original bytes.
* A UTF-8 file with LF line ends gives the same patch as today.

### Tests (sketch)

A real git repository: a CRLF file, a latin-1 file and a UTF-8 file edited, the controller's own save, `git reset --hard` and `git clean`, `git apply` of the saved patch, and the tree equal byte for byte to the one before; a new CRLF file and a new latin-1 file (untracked) come back byte for byte too.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Parts A to C are one class and may share one small reader of a
reply field (for example in `tools/llm_stream.py`); the judge looks at whether a rejection is ever turned into an approval.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A local model that answers `"approved": "false"` is not approved; `/edit` with a validator that lists its complaints as an array prints them and goes on; a failed commit on a CRLF checkout leaves a patch that `git apply` accepts.

## Not in scope

The wording of the feedback, the prompts, the codec the Coder or `/edit` writes a legacy file in (the Windows-1252 euro sign guessed as Windows-1251 is a heuristic with no safe general answer), and the retry policy of Gate 1.
