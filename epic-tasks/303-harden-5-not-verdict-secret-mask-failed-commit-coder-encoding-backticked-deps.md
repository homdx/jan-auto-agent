# HARDEN-5 — five wrong answers that cost work or leak: a "NOT …" verdict approved, the secret mask, a failed commit marked DONE, the Coder on a non-UTF-8 file, backticked file names as dependencies

**Status:** open — FINAL, written 2026-10-14 against `1fa8599`. Five defects from outside reviews, each **reproduced by running the code** on that commit (the output is below). Two of them (Parts A and B)
are gaps left by the landed fixes HARDEN-1 A1 and A8; Part C is a gap in HARDEN-2 B2. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: a chapter the model rejected goes through; B: an API key reaches the provider); the rest MEDIUM
**File:** `tools/auto/inner_loop.py` (A), `tools/llm_stream.py` (B), `tools/auto/controller.py` and `tools/auto/outer_loop.py` (C), `tools/auto/coder.py` (D), `tools/auto/backlog_prioritiser.py` (E)
**Symbol:** A: `_parse_verdict_soft`. B: `_API_KEY_LINE_RE`, `_value_is_secret_literal`, `mask_api_key`, `_mask_payload_secrets`. C: `Controller._run_task_loop` (the `git_error` branch), `OuterLoop.run_task` (where a passing task is set `DONE`).
D: `Coder._read_file_contents`, `Coder._write_files`. E: `BacklogPrioritiser._cross_file_symbol_ref`
**Round:** —
**Size:** M
**Depends on:** HARDEN-1 (298), HARDEN-2 (299), CC-16 (302), all landed
**Also touches:** `tests/`, `tests_bugfix/` (one file per part)

---

## Why

The landed fixes closed the first reports. These five are what the next reviews found **in the fixes themselves** or beside them: a verdict parser that learned whole words and forgot the negative ones, a mask that
still rewrites code and still lets keys through, a commit failure that is counted but not undone, a write path that was fixed for `/edit` and not for the Coder, a dependency rule that treats a file name as a symbol.
Each part has its evidence, the fix in a few sentences, and the tests that pin it. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a reply that begins with "NOT …" is approved

### What happens today

HARDEN-1 A1 made `NO` / `REVISE` / `REJECT` match as whole words, so `NOT BAD` is no longer read as `NO` plus the tail `T BAD`. The same change took the rejection away from **every** reply that begins with `NOT`: such a reply
now matches nothing, is "unparseable", and unparseable means approve (fail-open, so a rambling reply cannot block a chapter). On `1fa8599`, `_parse_verdict_soft(reply)` gives `(approved, reason, unparseable)`:

| reply | before A1 | now |
|---|---|---|
| `NOT APPROVED` | rejected | **approved** (unparseable) |
| `Not good enough: pacing drags` | rejected | **approved** |
| `NOT OK`, `Not complete`, `Not fixed`, `NOT RECOMMENDED` | rejected | **approved** |
| `Nothing works`, `Nonsense` | rejected | **approved** |
| `NOT BAD`, `Nothing to add`, `NOTE: fine` | rejected with a clipped reason (the A1 defect) | approved (right) |
| `NO: broken`, `No - bad`, `REVISE: x` | rejected | rejected |

The creative prompt asks for `APPROVED` or `REVISE:`, but models often write "Not approved: …". Gate 2 retries an unparseable reply once with a nudge; the Fact, Theme and Continuity validators and the Architect's plan
validation share the parser and pass an unparseable reply with no retry. End to end through the creative Gate 2 with the model stubbed, "Not approved: the chapter contradicts the story bible" gives `approved=True`
(`False` on `c2ef95b`).

### What it does

* `NO` and `NOT` as whole words open a **rejection**, as `REVISE` and `REJECT` do (`NO: x`, `NOT approved`, `Not good enough`); `NOTHING` and `NONSENSE`, which the old prefix test rejected by accident, are rejections too
  unless the reply is one of the explicit approvals below.
* Explicit approvals, matched before the rejection: `NOT BAD`, `NOTHING TO ADD / FIX / CHANGE / IMPROVE`, `NOTHING WRONG`, `NOTE:` (a note is not a verdict: it falls through to the later rules as prose).
* The reason of a rejection is the rest of the reply, never a clipped word.

### Tests (sketch)

`tests/test_auto_verdict_not.py`: the table above, row by row (both the whole-reply and the per-line form: the verdict on a later line behind a preamble); the creative `LLMGate2Validator.approve()` with the model call stubbed
returns `approved=False` for "Not approved: the chapter contradicts the story bible" and keeps the full critique; `NOT BAD` and `Nothing to add` still approve.

---

## Part B — the secret mask lets keys through and still rewrites code

### What happens today

`mask_api_key` is applied to every message of every request (`_mask_payload_secrets`), including the file a model is asked to rewrite in full. `_API_KEY_LINE_RE` ends in `$`: the value must be the last thing on the line.
On `1fa8599`:

| input | result |
|---|---|
| `api_key = sk-live-abcdef1234567890` | masked (right) |
| `api_key = sk-live-abcdef1234567890  # production key` | **leaked** |
| `api_key = sk-live-abcdef1234567890 ; production key` | **leaked** |
| `Set api_key = 'sk-live-abcdef1234567890' in agents.ini` | **leaked** |
| `Authorization: Bearer sk-or-v1-ABCDEF1234567890abcdef` | **leaked** |
| `self.api_key = api_key`, `    token = fetch(api_key=token)` | kept (right) |
| `self.api_key = None`, `self.api_key = key`, `API_KEY = DEFAULT_API_KEY` | **masked: `= here_your_key`**, code rewritten |

HARDEN-1 A8 meant "an identifier is code, never a literal secret", but `_value_is_secret_literal` returns True for every identifier except the key's own name. A value that says "no key here" is rewritten too
(review v8, NEW-A; `mask_api_key` on `1fa8599`):

| input | now |
|---|---|
| `api_key = ""`, `api_key = ''` | **masked** → `api_key = here_your_key` (`if not api_key:` then sees a non-empty fake key and authenticates with it) |
| `api_key = None`, `api_key = null`, `api_key = true` | **masked** |
| `api_key = your_key`, `api_key = changeme` | **masked** |
| `api_key = ""  # set later`, `api_key = <your key>`, `api_key = here_your_key` | kept (right) |

Once the key regex no longer needs the value to end the line, the comment form of the first row would be masked too: the unset-value rule is part of the fix, not an extra. End to end on a small `Client` class, a faithful model asked to change
`timeout = 30` to `60` changes three lines: `self.api_key = None` and `self.api_key = key` come back as `here_your_key`, and importing the file raises `NameError: name 'here_your_key' is not defined`. A quoted default
(`api_key = "x"`) comes back unquoted, which breaks the same way. The key leaks are the purpose of the function failing; they are the common way a key appears in a config file or a note.

### What it does

* The secret is found **where it sits**, not only at the end of a line: a quoted string (`'…'` or `"…"`) or a run of non-space characters after `api_key =`, with any trailing `#` / `;` comment left alone; the line anchor goes.
* A `Bearer <token>` is masked wherever it appears; a known key prefix (`sk-`, `sk-or-v1-`, `sk-proj-`, `AIza`, `xoxb-`) is masked in a key or token context (after `api_key =`, `token:`, `Authorization:`, `Bearer`), **not**
  anywhere in a file: a string with that prefix inside code is code (the reviewers' own caution: masking it would rewrite working code).
* An **unset value** is not a secret and is sent as it is: an empty quoted string, `None` / `null` / `nil` / `undefined` / `true` / `false`, and a template placeholder (`<…>`, `your_key`, `changeme`, `xxx`, `here_your_key`).
* Every bare identifier or expression on the right-hand side is **code and is sent as it is** (`None`, `key`, `DEFAULT_API_KEY`, `os.environ[...]`, `self._key`); only a quoted string or a key-shaped token (long, no `.`, `(`, `[`, spaces) is masked, and a masked quoted string keeps its quotes.
* Or, and acceptable if simpler and tested: replace each masked value with a unique placeholder, remember the mapping for the request, and put the original text back in the reply before `/edit` or the Coder writes it.

### Tests (sketch)

`tests/test_mask_api_key_forms.py`: every row of both tables with its expected output (the unset values are kept, the comment form of an unset value too); a real key in a file the model rewrites faithfully is not in the outgoing request **and** the file on disk is byte-identical afterwards except the line asked
for; the existing mask tests green unchanged.

---

## Part C — a task whose commit failed is still marked DONE

### What happens today

HARDEN-2 B2 made the controller tell a git failure from "nothing to commit": on a failure it logs "NOT counted done", saves the work as a patch and skips `tasks_done += 1`. But the real `OuterLoop.run_task` has already set
`STATUS_DONE` the moment the task passed (`tools/auto/outer_loop.py:392`, before any commit), and the `git_error` branch of `Controller._run_task_loop` never sets the status back. The pinned test passes only because it swaps in
a stand-in outer loop that never writes the status. With the real loop and a commit that raises `GitError`:

```
tasks_done        = 0
task status       = done          <- the ticket (299) and its test require: not DONE
patch saved       = True
work.py still in the tree, uncommitted
```

Two tasks, `C1` depending on `B2`, only `B2`'s commit failing: `C1` runs (its dependency reads as DONE) and its commit carries `B2`'s files under `C1`'s id — the sweep B2 was meant to prevent. A resume skips the task (DONE),
and the work survives only as `.agent/uncommitted-<id>.patch`, which nothing re-applies. If nothing sweeps the files, the untracked work trips the dirty-tree guard and the next `--auto` is refused for changes the agent made.

### What it does

* In the `git_error` branch the task goes back to `TODO` (or to a status such as `FAILED_COMMIT` that the dependency check treats as not done) and the log says so.
* After the patch is saved the work is taken **out of the tree** (the patch is the record), so no later task's commit sweeps it in and the dirty-tree guard has nothing to refuse; a resume re-applies the patch before the task runs again,
  or runs the task afresh — either is acceptable, but a task is never both "done" and "not committed".
* The test drives the **real** `OuterLoop` with a stub inner loop, not a stand-in that skips the status write.

### Tests (sketch)

`tests_bugfix/…`: the real outer loop, a passing stub inner loop and a `GitManager.commit_task` that raises `GitError`: `tasks_done == 0`, the status is not `done`, the patch exists, the tree is clean; the two-task scenario: `C1` does
not run (or runs without `B2`'s files in its commit); a normal commit is unchanged.

---

## Part D — the Coder corrupts a file that is not UTF-8

### What happens today

`Coder._read_file_contents` decodes with `errors="replace"`, so the model is shown each non-UTF-8 byte as `�`; it returns the whole file, and `_write_files` writes the reply back as UTF-8. CC review fix 34 and CC-16 (302)
fixed this for `/edit`; the Coder's path was not touched. On `1fa8599`, a latin-1 file (`LIMIT = 10`, a line with `¡ñé`) edited by a faithful model that changes `LIMIT = 10` to `20`:

```
the model is shown U+FFFD:        3 characters
after the write:                  3 U+FFFD in the file; 2 lines changed (asked 1)
backup:                           data.py.coder.bak (the original bytes are kept there)
```

### What it does

* The file's codec is decided once, from its bytes, by the same rule `/edit` uses (UTF-8 if the bytes decode; otherwise the legacy codec the file can be written back in): the model is shown the file decoded **strictly** with that codec, and
  the reply is written back with it, so the bytes outside the edit are the original bytes.
* A reply the file's codec cannot hold is **refused** for that file (the task fails visibly; the file is untouched), never written with `?` or `�`.
* A UTF-8 file, a CRLF file and a 0755 file keep everything they keep today.

### Tests (sketch)

`tests/test_coder_legacy_encoding.py`: the latin-1 case above: one line changed, no U+FFFD; a cp1251 file with a revision in Cyrillic and curly quotes; a revision the codec cannot hold is refused with the file untouched;
UTF-8 / CRLF / 0755 unchanged.

---

## Part E — a file name in backticks is a dependency

### What happens today

The "plain word is a dependency only as code" fix excludes `run.log` and `run (when enabled)` written in prose, but its first alternative (a backticked span that contains the word) accepts **any** backticked span. File names are normally
written in backticks, so an unrelated task becomes a dependent. `BacklogPrioritiser._cross_file_symbol_ref(task_a, task_b)` for `task_a` citing the symbol in the first column and `task_b`'s instruction in the second:

| symbol | instruction of B | now |
|---|---|---|
| `main` | Update `main.py` to log more | **True** |
| `run` | Rotate `run.log` weekly | **True** |
| `run` | Edit `scripts/run.sh` | **True** |
| `parse` | Fix `parse.json` schema | **True** |
| `config` | Rename `config.ini` | **True** |
| `main` | Call `main` first | True (right) |
| `run` | Call `run()` first | True (right) |
| `run` | the loop will run (when enabled) | False (right) |

This is the failure the fix describes: one failed task blocks an unrelated one (the controller holds a task whose dependency is not done).

### What it does

* The extension and path exclusion apply **inside** a backticked span too: `main` / `run()` / `Store.load` count; `main.py`, `run.log`, `scripts/run.sh`, `docs/main.md`, `parse.json`, `config.ini` do not.

### Tests (sketch)

`tests/test_backlog_dependency_backticks.py`: the table, row by row; the existing dependency tests green unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Part C is the one that
changes the controller's accounting: its test must drive the real `OuterLoop`.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A creative `--auto` run whose Gate 2 model answers "Not approved: …" gets a rework round, not a pass.

## Not in scope

Changing the retry rule for an unparseable reply, the placeholder text `here_your_key`, the task states other than what Part C needs, and the dependency graph beyond `_cross_file_symbol_ref`.
