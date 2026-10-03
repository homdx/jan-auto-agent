# AR-7 — `arena issue create`: a ticket drafted from a brief, a file or one epic item, into `.arena/drafts/`, no branch, no commit

**Status:** queued (round 145 ran on arena; judged there, not on this branch)
**Severity:** LOW
**File:** tools/arena/tickets.py
**Symbol:** issue_create, next_number, build_brief
**Round:** 145
**Size:** M
**Also touches:** tools/arena/cli.py, tools/contest/cli.py, tools/contest/draft.py, tests/test_arena_issue_create.py, tests/test_contest_draft.py

---

## Why

Today a ticket is drafted with `python3 -m tools.contest draft --target . "brief"`. That command checks out and commits on `contest-legs`, which can be stale (ticket 135). It hands out the first free number from 1 (`draft.next_round`), which can be an old round whose `contest-out/` still exists. It also takes the brief only as one command-line string, so drafting from an epic item means `"$(sed -n '/### AR-8/,/### AR-9/p' …)"`.

`arena issue create` fixes all three:
- the draft lands in `.arena/drafts/NN-<slug>.md`, with no branch switch and no commit;
- the number is max + 1 over every place a number lives;
- the brief can be a text, one or more files, or one `### ID …` section of a file, plus a text that tells the model what to do with that file ("the file also has AR-5, write AR-7").

`arena run start NN` already finds a ticket in `.arena/drafts/` (`rounds.find_ticket`, AR-3), so a drafted ticket is one command away from a round.

---

## What AR-1 … AR-6 already give you (read them before writing)

- `tools/arena/cli.py`:
  - `OBJECTS["issue"].verbs["create"]` exists with `ticket="AR-7"` and no handler. AR-7 sets its `handler` and `add_arguments`; no other verb changes (`issue land` stays "not implemented").
  - `REPO_ROOT` is the one repo seam. `-o json` is a global flag. `output.emit` / `output.refuse(msg)` (exit 2).
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/rounds.py`: `load_config(repo)`, `round_folder`, `integration_branch(repo, flag, prof)`, `REF_PREFIX = "arena-round/"`, `find_ticket` (drafts → checkout → branch), `git` from `gitref.py`.
- `tools/arena/tickets.py` (AR-6): `scan(repo, config, branch, proc_root)` returns every ticket number from the drafts, the checkout's `epic-tasks/` and `epic-tasks/` on the branch; `_by_number` skips `*.rejected.md`.
- `tools/contest/draft.py`: `draft_ticket(brief, *, repo, llm_call, config, round_no, out, write, review_call, commit)`. It runs collect (Pass A), builds the prompt, lints, does one rework, runs the review rounds, then writes the file or a `.rejected.md`. Also `title_of`, `slug_for`, `REJECTED_SUFFIX`, `SOURCE_BUDGET`, `llm_call_for(settings, system=…)`, `REVIEW_SYSTEM_PROMPT`.
- `tools/contest/cli.py`: `cmd_draft` (lines ~2657–2765) prepares the writer and reviewer callables, `_same_model(a, b)` and `_round_out_dir(repo, config, nn)`. The profile keys are `[contest] draft_llm_profile`, `draft_review_llm_profile` and `gate_llm_profile`.

---

## What to build

### 1. `tools/contest/cli.py` — one extraction (the only edit in this file)

`draft_callables(config, no_review: bool) -> tuple[Callable, Optional[Callable]]` returns `(llm_call, review_call)`.
- It holds exactly the preparation `cmd_draft` has today:
  - no draft profile, or a draft profile that does not resolve;
  - reviewer = `draft_review_llm_profile` when set, else `gate_llm_profile`;
  - no reviewer profile without `no_review`;
  - reviewer model == writer model (`_same_model`).
- Each of these refusals raises `DraftSetupError(message)`, a new `ValueError` subclass, with the same text `cmd_draft` prints today. No model is called.
- `cmd_draft` calls it and prints `draft: <message>` with exit 1, exactly as before.
- **`tests/test_contest_draft.py` stays green untouched.** A new test may be appended there for `draft_callables` alone.

### 2. `tools/contest/draft.py` — one keyword (the only edit in this file)

`draft_ticket(..., out_dir=None)` sets the folder that the default file name `<NN>-<slug>.md`, and a refused draft's `<NN>-<slug>.rejected.md`, are written into instead of `epic-tasks/`.
- When `out` is given, it still wins over `out_dir`.
- `commit=True` with `out_dir` is a `ValueError`: a draft outside `epic-tasks/` is never committed.
- `lint_ticket`'s "round is taken" check keeps reading `epic-tasks/`.
- Nothing else in `draft.py` changes.
- Why not "call with `write=False` and write the file yourself": the slug comes from the ticket's own title, which is known only after the model answers, and with `write=False` a refused draft writes no `.rejected.md` at all.

### 3. `tools/arena/tickets.py` — the verb

#### `next_number(repo, config, branch) -> int`

Max + 1 (not the first gap) over:
- every number `scan` sees: drafts, checkout `epic-tasks/`, branch `epic-tasks/`;
- `*.rejected.md` in `.arena/drafts/` and `epic-tasks/`, so a refused number is not handed out to a different brief while its rejected text is still lying there (see test 6 for the retry);
- round folder names under the roster's `out_dir`: `NN` and `NN.K`, zero-padded or not, read as integers; other names are ignored;
- `refs/heads/arena-round/NN`.

With nothing anywhere, the result is 1.

`next_number` is the only allocator. Do not call `draft.next_round`.

#### Brief sources: `build_brief(repo, text, files, item) -> str`

| flags | the brief the model gets |
|---|---|
| `"text"` | the text |
| `--file F` | the file |
| `--file F --item ID` | only the `ID` section of F |
| `--file F "text"` / `--file F --item ID "text"` | the text first, as the task, then the file or section as material |
| `--file A --file B ["text"]` | the text (if any), then A, then B |

Layout, verbatim. Each block is present only when it has content; blocks are separated by one blank line:

```
## Task (from the operator — this wins over anything in the material below)

<text>

## Material: docs/arena/EPIC-ARENA.md — section AR-7

<section text>
```

- Without the text, the brief is the material blocks only.
- The header is `## Material: <path>` for a whole file, and `## Material: <path> — section <ID>` for an `--item`.
- The path is printed repo-relative when it is inside `REPO_ROOT`, absolute otherwise.

**`--item ID`** cuts the section:
- The section starts at the first Markdown heading line (`#` to `######`) whose text after the hashes starts with `ID` followed by a non-word character or the end of the line. So `AR-7` matches `### AR-7 — …`, but not `### AR-70 — …` and not `### AR-7b`. The match is case-sensitive.
- The section ends before the next heading of the same or a higher level (fewer or equal `#`), or at the end of the file.
- The heading line itself is part of the section.
- Headings inside fenced code blocks (lines between ```` ``` ```` fences) are not headings.

**Refusals** (`output.refuse`, exit 2, one line, before collect and before any model call, nothing written):
- no text and no `--file`;
- `--item` without `--file`, or `--item` with more than one `--file`;
- `--file` missing, a directory, or not decodable as UTF-8 (message names the path);
- a file, or the cut section, that is empty after stripping whitespace;
- `--item ID` not found in the file → `no section AR-7 in docs/…`;
- `--item ID` matching two headings → `AR-7 is in docs/… twice: lines 779, 1201`;
- the brief over `BRIEF_LIMIT` characters (module constant `= draft.SOURCE_BUDGET`, i.e. 40000), measured on the assembled brief → `brief is 61234 characters, limit 40000 — cut it with --item ID or a shorter file`. Never silently cut: a cut epic is a ticket written against half a spec.

**Paths:**
- A relative `--file` resolves against `REPO_ROOT`, never against the cwd. An absolute path is used as it is.
- A file outside the repo is allowed; it is only read.

#### `issue_create(repo, args, prof) -> int`

1. `rounds.load_config(REPO_ROOT)`. A roster error is a refusal.
2. `branch = rounds.integration_branch(REPO_ROOT, args.branch, prof)`. `RoundError` is a refusal.
3. `brief = build_brief(...)`. Its refusals come from the table above.
4. Number:
   - `--number NN` is refused when `NN` is not a positive integer, or when any source of `next_number` already holds NN: `ticket 12 exists (epic-tasks/12-x.md)` or `round 40 exists (out/40)`.
   - An existing `.rejected.md` for NN does **not** block an explicit `--number NN`: that is how a refused number is retried.
   - Otherwise the number is `next_number(...)`.
5. `llm_call, review_call = contest_cli.draft_callables(config, args.no_review)`. `DraftSetupError` → refusal with exit **2** (not `cmd_draft`'s 1); no model is called.
6. `draft.draft_ticket(brief, repo=REPO_ROOT, config=config, round_no=NN, out_dir=REPO_ROOT/".arena/drafts", llm_call=llm_call, review_call=review_call, commit=False)`. A `ValueError` out of it is a refusal.
7. Output:
   - **Success**, stdout, exit 0, exactly two lines:
     ```
     ticket 145 drafted: .arena/drafts/145-<slug>.md
     next: arena run start 145
     ```
     With `--no-review`, one extra line on stderr: `review skipped (--no-review)`.
   - **Lint or review refusal**: every problem as `- <problem>` on stderr, then `rejected draft: .arena/drafts/145-<slug>.rejected.md`, exit 2. No `.md` draft file.
   - **`-o json`**: one object `{"number", "path", "rejected", "problems", "rejected_path", "reviewed"}`. `path` and `rejected_path` are repo-relative or null, `problems` is a list, `reviewed` is false with `--no-review`. On a rejection the JSON is printed to stdout and the exit is still 2.

#### Flags (`add_arguments`)

```
arena issue create ["text"] [--file PATH]... [--item ID] [--number NN] [--no-review] [--branch B]
```
- `text` is positional and optional (`nargs="?"`).
- `--file` can be repeated (`action="append"`).
- `--item` is AR-7's own flag. AR-15's planned `--epic NAME` (a `**Epic:**` field) is a different thing; do not add it.

---

## Not in this ticket

- Moving a draft into `epic-tasks/` and committing it is AR-8 (`issue land`).
- `--blank` and `--after` are AR-15. `issue lint` and `issue review` are AR-17 and AR-18.
- The model never opens a file itself: the draft is one prompt, not an agent. Every file the model sees is the brief built above, plus `brief_sources` (files the brief names by path, as today) and collect.

---

## Tests

### `tests/test_arena_issue_create.py`

Set-up: a throw-away git repo under `tmp_path` (`git init`, one commit on branch `arena`) and a `contest.ini` with `out_dir = out`, one `[contest.agent.a]`, and `[contest] draft_llm_profile` / `gate_llm_profile` naming two `LlmSettings` sections with **different placeholder models** (`model-writer`, `model-reviewer`; no real names). Patch:
- `cli.REPO_ROOT`;
- `rounds.PROC_ROOT` (a fake `/proc`);
- `draft.run_collect` with a no-op;
- `contest_cli.draft_callables` (or `draft.llm_call_for`) to return fake callables that **record their prompts**.

The fake writer returns a well-formed ticket (all `draft.HEADER_FIELDS`, `**Round:** NN`, the sections the lint wants), and the fake reviewer approves. `monkeypatch.chdir` elsewhere in every test.

1. **Lands in drafts, nothing else moves.**
   - `issue create "brief"` → exit 0, `.arena/drafts/01-<slug>.md` exists, stdout is the two lines.
   - `git status --porcelain -- epic-tasks` is empty. No branch `contest-legs`. `git rev-parse HEAD` and `git for-each-ref` are unchanged.
2. **Numbering, max + 1.**
   - `epic-tasks/12-a.md` + `out/40/` → 41.
   - `out/040.2/` alone → 41.
   - a ref `arena-round/50` → 51.
   - a ticket 60 only on branch `arena` → 61.
   - a draft `.arena/drafts/70-x.md` → 71.
   - a gap (1, 2, 9) → 10, not 3.
   - an empty repo → 1.
3. **`--number`.**
   - `--number 12` with `epic-tasks/12-a.md` → exit 2.
   - `--number 40` with `out/40/` → exit 2.
   - `--number 0` / `--number x` → exit 2.
   - `--number 99` free → `99-<slug>.md`.
4. **Text only.** The writer's prompt contains the text verbatim and no `## Material:` header.
5. **`--file`, `--item`, text.** Use an epic fixture with `### AR-5 — five`, `### AR-7 — seven`, `### AR-70 — seventy`, a ```` ``` ```` fence that contains the line `### AR-7 fake`, and a `## Next part` heading.
   - `--file epic.md --item AR-7 "skip AR-5, do AR-7"`: the prompt has the `## Task …` block before `## Material: epic.md — section AR-7`.
   - The section contains `seven`, and contains neither `five`, nor `seventy`, nor `Next part`.
   - The fenced `### AR-7 fake` does not count as a second match.
   - `--file epic.md` without `--item`: the whole file is in the prompt.
   - `--file a.md --file b.md "t"`: blocks in the order t, a, b.
   - A relative `--file` resolves against the repo while the cwd is elsewhere.
6. **Brief refusals, all exit 2, one stderr line, the fake writer never called, nothing written under `.arena/`:**
   - no arguments;
   - `--item AR-7` without `--file`;
   - `--item` with two `--file`;
   - a missing file;
   - a directory;
   - an empty file;
   - `--item AR-9` not present;
   - `AR-7` heading present twice (outside fences);
   - a brief over `BRIEF_LIMIT` (the message names both numbers).
7. **Lint refusal.**
   - The fake writer returns garbage twice (draft and rework) → exit 2, `- <problem>` lines, `.arena/drafts/NN-<slug>.rejected.md` exists, no `NN-<slug>.md`.
   - Then `issue create` without `--number` gives **NN + 1** (the rejected number is held).
   - `issue create --number NN` retries the same number and succeeds.
8. **Review refusal** (the fake reviewer always refuses) → exit 2, `.rejected.md`, the reviewer's problems are printed.
9. **Writer = reviewer model** → exit 2, one line, neither fake called. **No draft profile** → exit 2. **No gate profile and no `--no-review`** → exit 2. **`--no-review`** → exit 0, reviewer never called, `review skipped` on stderr.
10. **`-o json`** parses. On success `rejected` is false and `path` ends `.md`. On a lint refusal `rejected` is true, `problems` is a non-empty list, and the exit is 2.
11. **Secrets.** The api key placed in the ini sections never appears in stdout or stderr, in any case above.
12. **`issue land`** still says "not implemented". **`issue list`** (AR-6) shows the new draft with state `draft`.

### Additions to `tests/test_contest_draft.py` (append only)

13. `draft_ticket(..., out_dir=tmp/".arena/drafts")`: the ticket lands there. A lint-refused draft's `.rejected.md` lands there too. `epic-tasks/` is not created or changed. `out=` wins over `out_dir=`. `commit=True` + `out_dir` → `ValueError`.
14. `draft_callables`: each refusal raises `DraftSetupError` with the text `cmd_draft` prints. `cmd_draft`'s existing tests are untouched and green.

Tier new test files with `python3 scripts/sync_test_tiers.py`; never edit the tier symlinks by hand.

---

## Acceptance

Run each command separately and in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests/test_arena_issue_create.py tests/test_contest_draft.py tests/test_arena_issue_list.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `contest-bench/139`, `142`, `143` and `144` still pass. `bench 139`'s "not implemented" example moves from `issue create` to `issue land`; that is the only allowed edit to an existing bench.

---

## Rules

- In `tools/contest/` only the two edits named above: `cli.draft_callables` + `DraftSetupError`, and `draft.draft_ticket(out_dir=)`. Nothing under `scripts/`.
- `REPO_ROOT` is the only repo seam; never read the current working directory.
- **No real model call anywhere in the tests.** Every LLM is a fake callable; no network.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …`, no `arena run start|rerun …`, no `scripts/revive_round.py` on a real `contest-out/`. Never run `tools.contest draft` or `arena issue create` against the real repo with a real model.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing; every refusal before step 6 happens before collect and before any model call.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files. Never print an api key.
- Do not change existing tests except to add cases; `tests/test_arena_cli.py` may move a "not implemented" example off `issue create` to `issue land`.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/rounds.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `145: …`.
