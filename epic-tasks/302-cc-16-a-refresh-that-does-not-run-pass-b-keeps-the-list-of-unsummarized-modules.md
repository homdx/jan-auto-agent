# CC-16 — five follow-ups: a refresh without Pass B erases the list of modules without a summary (CC-15), a recorded base side is reused against a head judged with other packs (CC-14), `/edit` throws a whole edit away on a file that is not UTF-8 (fix 34), Ctrl-C leaves the executor's child running (fix 35), and one claim classified differently by two entries refuses the whole claims phase (CC-14)

**Status:** landed (round 302: step-5-preview as the base, with the seed and the prompt mode written into the votes files and a budget-less record read as one that states none; black box 302, 27 checks). Before: open — FINAL, found 2026-10-14 by outside reviews of the landed CC-15 (297) and CC-14 (296), both reproduced on `0800b2e` (Part A as a process, Part B on the function). Five independent defects, one commit.
**Severity:** MEDIUM (it is the defect CC-15 set out to remove — "never retried, because the manifest says up to date" — back through a normal path)
**File:** `tools/collect/cli.py` (Part A), `scripts/claim_diff.py` (Part B), `tools/actions.py` (Part C), `tools/auto/executor.py` (Part D), `scripts/judge_epic_round.py` and `scripts/claim_diff.py` (Part E)
**Symbol:** A: `action_refresh` (the incremental batch: `summarized_paths`, `previous_manifest.unsummarized`, the `unsummarized` it writes), `Manifest.unsummarized`. B: `BASE_FIELDS`, `base_side_record`, `base_side_from_votes`, `base_side_differs`, `judge_both`. C: `OrchestratorActions.run_edit` (the write block: the backup, the encoding choice, `atomic_write_text`). D: `Executor._execute` (the `subprocess.Popen(..., start_new_session=True)` block and its `except` clauses)
**Round:** 302
**Size:** S
**Depends on:** CC-15 (297, landed: `Manifest.unsummarized`, the stale rule, the retry of listed modules); CC-14 (296, landed: the base side judged once and reused)
**Also touches:** `tests/test_collect_unsummarized.py`, `tests/test_judge_epic_round_claims.py`, a new `tests/test_actions_edit_legacy_encoding.py`, a new `tests/test_executor_interrupt.py`, `tests/test_judge_epic_round_claims.py` (Part E)

---

## Part A — a refresh without Pass B erases the list of modules without a summary

### What happens today

CC-15 records, in the manifest, the modules Pass B tried and left without a summary (`Manifest.unsummarized`), calls such a manifest stale, and sends exactly
those modules to Pass B again at the next `--collect` / `--refresh`. The list is rebuilt on every refresh from the modules **handed to Pass B in that run**
(`summarized_paths`). A refresh that does not run Pass B hands none, so it writes the manifest with `unsummarized = ()` and the previous list is gone.

```
1  full build, the model refuses pkg/store.py        unsummarized = ['pkg/store.py']     store.py has no summary
2  edit pkg/still.py, then a refresh with no Pass B   unsummarized = []                  store.py STILL has no summary
3  --check                                           collect check: up to date
```

Reproduced as a process (a fake model server that answers 500 for `store.py`; `main.py --collect`, then an edit of `still.py` and `main.py --collect --no-llm`):

```
1 full build         : []
2 LLM refuses store  : ['pkg/store.py'] | store has summary: False
3 refresh --no-llm   : []               | store has summary: False
4 --check            : collect check: up to date
```

**It is a normal path, not a corner.** A refresh with no Pass B is what runs when `[collect] llm_summaries = false`, when the config has no usable model, and when the
caller passes no `llm_call`: `loader.load` with `[collect] staleness = refresh` calls `action_refresh(root, config=…)` with no `llm_call`; `claimcheck/target.py` and
`contest/draft.py` call `action_collect` with no model, which falls into `action_refresh` on a stale tree. So the **first stale load after a failed Pass B rewrites
the manifest as clean**, and the module is never retried.

Not the same as the ticket's own sentence "a `--no-llm` build records `[]`": that is about a **full build**, which carries no summaries at all and expects none.
An incremental refresh keeps the summaries of the modules it does not touch, so it must keep the knowledge of which of them have none.

### What it does

* **A refresh that does not run Pass B carries the previous list over**, restricted to what is still true: the paths of `previous_manifest.unsummarized` that are
  still in the tree (a deleted or moved module drops out) **and** whose module still has no summary in the artifact. Nothing is added by such a refresh (it
  attempted nothing).
* **A refresh that runs Pass B** keeps its present rule: the modules it handed to Pass B that ended without a summary, plus the previously listed modules it did
  not hand (none, since CC-15 hands them all) — so the two cases give one list, not two.
* **A full build is unchanged**: it rebuilds the list from the modules it handed to Pass B; `--no-llm` / `llm_summaries = false` full builds record `[]`.
* `--check` and `load` / `load_readonly` therefore still say `stale (N modules without a summary)` after a refresh that could not retry, and the module is retried
  by the next refresh that does run Pass B.

### Tests (sketch)

`tests/test_collect_unsummarized.py`, the file's own fake model in-process:

* the reproduction above, step for step: after step 2 `unsummarized == ["pkg/store.py"]`, `--check` is still stale with the count, and a later refresh **with** the
  model asks about `pkg/store.py` alone and clears the list;
* the list is carried through several refreshes without Pass B, and a listed module that is deleted (or moved) between two of them is dropped;
* a refresh without Pass B that changes a **listed** module: the module is changed, still has no summary (nothing ran), and stays listed;
* `[collect] llm_summaries = false` in the config does the same as `llm_call = None`;
* a full `--no-llm` build still records `[]`; the existing tests are green unchanged.

## Acceptance (two layers)

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.

**Layer 2 — the operator, live.** On a copy of a repository whose `.collect/` has a module without a summary: `main.py --collect --no-llm` after an edit of another
file, then `--check`: it still names the module without a summary; one `--collect` with the model fills it and the count goes to 0.

---

## Part B — a recorded base side is reused against a head judged with other packs

### What happens today

CC-14 judges the base side of the ticket's claims once and reuses it for every entry (and from the command line: `claim_diff.py --base-votes FILE`). The reuse is
refused when the record is not this run's: its identity is five fields (`BASE_FIELDS`: the claims, the base sha, the voters, the runs, the acceptance rule).
The votes of a side, however, depend on two more settings of the run, which `judge_both` hands to the packs it builds: the **budget** (`--pack-chars`: the
rendered characters of a claim's pack, the chunks, the characters per chunk) and the **collect flag** (whether the collect facts are in the pack). Neither is in the
identity: `base_side_differs` does not even take them as parameters. Yet `votes_base.json` already records both — `side_report` writes `target.collect` and
`target.budget` — and `base_side_from_votes` does not read them.

Reproduced on `0800b2e`, calling `base_side_differs` with a record whose identity is the five fields, as a run with `--pack-chars 1000 --no-collect` against a
`votes_base.json` judged with `budget.chars = 6000` and `collect = true`:

```
base_side_differs(record, claims=…, base_sha=…, voters=…, runs=1, accept="all", index=[…])   ->   ''      (accepted)
```

A base judged with the big pack and the collect facts is then compared with a head judged with the small pack and none; the FIXED / STILL / NEW / GONE table mixes
two evidence setups — the "stale base judged as this round's" error the refusal exists to prevent.

**Scope.** `judge_epic_round.py --claims` holds the budget and the collect flag fixed for the whole round, so only the command line route `--base-votes` is exposed.
That is why it is low, not why it is fine: the command line is the documented way to reuse a base across runs.

A second review (NB-1) found the same gap wider than the first named it. The base verdicts depend on **everything that decides what the voters are shown and who they are**, and the
identity holds none of these beyond the five: the pack **budget** (`chars`, `chunks`, `per_chunk`), the **collect flag**, the **seed**, the **fixed-prompt** mode (`--fixed-prompt`), and the
**model behind each voter name** (`voters` is compared by profile name only, so a profile whose model changed while its name stayed is accepted). `votes_base.json` already records the budget,
the collect flag and the models of the voters (`side_report`); the seed and the prompt mode are options of the run. The reviewer's reproduction (a two-commit repository, a fake voter that judges by
how much evidence it is shown): recorded with the default budget the base verdict is TRUE; judged fresh with `chars=400` it is FALSE; reusing the recorded side with `chars=400` gives TRUE and no refusal.

### What it does

* The identity gets more fields: **`collect`**, **`budget`** (chars, chunks, per chunk), **`seed`**, **`fixed_prompt`** and, for each voter, **its model** (not only its name). `base_side_record`
  writes them, `base_side_from_votes` reads them from the file (`target.collect`, `target.budget`, `models`, and the seed and prompt mode the file carries or the CLI states), and
  `base_side_differs` takes this run's values and compares them.
* A refusal names the field, as the other five do (`collect: the record was judged with the collect facts, this run is not`; `budget: …`; `seed: …`; `voter models: …`).
* A record written before this change has neither field: it is **refused with a message that says so** (it cannot be shown to be this run's), not accepted.
* `judge_epic_round --claims` passes its own (fixed) values, so its reuse keeps working unchanged.

### Tests (sketch)

`tests/test_judge_epic_round_claims.py`:

* a record with `budget.chars = 6000`, `collect = true` against a run with `--pack-chars 1000` and then against a run with `--no-collect`: each refused, the field named;
* the same record against a run with another `--seed`, with `--fixed-prompt`, and with a voter name whose model changed (the same profile name): each refused, the field named;
* the reviewer's case: a fake voter whose verdict depends on the pack length — recording at the default budget and reusing at `chars=400` is refused, not TRUE;
* the same record against a run with the same two settings: accepted;
* `base_side_from_votes` reads both fields from a real `votes_base.json` written by `side_report`; a file without them is refused with the message;
* the CLI: `claim_diff.py --base-votes FILE --pack-chars …` exits 2 on a mismatch and issues no request; the existing reuse tests are green unchanged.

### Acceptance

**Layer 1 — offline, in the round.** The tests above and the existing suites, green unchanged.
**Layer 2 — the operator.** Two `claim_diff.py` runs on the same claims and base, the second with `--base-votes votes_base.json --pack-chars 1000`: refused, naming `budget`.

---

## Part C — `/edit` on a file that is not UTF-8 throws the whole edit away when the revision has a character outside Latin-1

### What happens today

Fix 34 made `/edit` write back the file's own bytes: the original is saved with `shutil.copy2`, then the encoding is chosen by testing whether the file's bytes decode as
UTF-8 — if not, `latin-1` (`read_file`'s fallback: "write back what it read") — and the revision is written with `atomic_write_text(..., encoding=encoding)`. That is right
while the model hands back what it was shown (the file read as Latin-1, mojibake and all). It is wrong when the model **fixes** the text, which is what it is asked to do for
a legacy file: it returns real Cyrillic, or `“ ” — … → ✓`, and none of that can be encoded in Latin-1.

Reproduced on `0800b2e`, the write block run on a file of `"Привет, мир"` saved as cp1251 (invalid UTF-8) with the revision `Привет, мир` + `He said “ok” — done…`:

```
encoding chosen: latin-1
write failed: UnicodeEncodeError 'latin-1' codec can't encode characters in position 0-5: ordinal not in range(256)
file unchanged: True | .bak left behind: ['note.txt.bak.135608']
```

`run_edit` catches it (`❌ Failed to write …`) and returns — **after every LLM iteration has run**, so the whole edit is lost, and the user is told the codec could not encode, not that the file is
not UTF-8. The timestamped `.bak` made just before the write stays behind, because `copy2` runs before the encoding is known and nothing removes it on failure. The same revision
**would have fitted in cp1251** (the curly quotes and the dash are in it), so the data was fine; the choice was not.

It is the likeliest for non-UTF-8 legacy text files — cp1251 Russian files are what this repository's creative pipeline works with.

### What it does

* **The encoding is decided once, before the model is asked**, from the file's bytes: UTF-8 if they decode; otherwise a legacy encoding the file can be written back in — the one that
  decodes the bytes *and* can encode the revision (try the file's own legacy encoding first — a BOM or `cp1251` / `cp1252` by what the bytes decode to — then `latin-1` last). If the
  revision fits none of them, the edit is **refused with a message that says the file is not UTF-8** and names the characters, the file untouched and no `.bak` left. (The first version of this ticket said "before the first LLM call": a revision is only known after the model has answered, so a refusal can only come after it; what can be decided before the call is the codec the file is read and written in.)
* **A failed write removes the backup it made**, so no `.bak` is left for an edit that did not happen (a backup of a successful write stays, as now).
* **The error says what is wrong**: `file is not UTF-8 (cp1251?) and the revised text has characters it cannot hold: “ ” —` — not the bare codec message.
* A UTF-8 file is unchanged in every respect; a file that is already read as `latin-1` and edited with Latin-1 text is written back byte for byte as now.

### Tests (sketch)

`tests/test_actions_edit_legacy_encoding.py`, the two LLM calls stubbed as the existing `/edit` tests do:

* a cp1251 file and a revision with `“ ” — …` and real Cyrillic: written back as cp1251 (decoded with cp1251 it equals the revision), no `.bak` problem, the unedited lines byte-identical;
* a revision with a character no legacy encoding holds (an emoji): refused **before** the first LLM call (a counter on the stub), the file untouched, no `.bak` left, the message names the problem;
* a failing write (the directory made read-only after the backup): the backup is removed;
* a UTF-8 file, a CRLF file and a 0755 file keep everything fix 34 made them keep; the existing `/edit` tests green unchanged.

### Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `/edit` on a real cp1251 file of the creative pipeline asking for a punctuation fix: the file stays cp1251 and carries the fix.

---

## Part D — Ctrl-C leaves the executor's child running

### What happens today

Fix 35 made `Executor._execute` start the command in a session of its own (`subprocess.Popen(..., shell=True, start_new_session=True)`) and, on `TimeoutExpired`, kill
the whole group (`os.killpg(proc.pid, SIGKILL)`). That fixes the timeout: `pytest` and its xdist workers no longer outlive it. But `subprocess.run` also did `except: process.kill();
raise` for **any** exception, `KeyboardInterrupt` included, and that is gone; and `start_new_session=True` takes the child out of the terminal's foreground process group, so a terminal
Ctrl-C no longer reaches it either. Nothing handles an interruption any more except the timeout.

Reproduced on `0800b2e` and on `1a5f18a` (before fix 35): the real `Executor._execute` runs `echo $$ > pidfile; exec sleep 300`, and the Python process gets SIGINT (what Ctrl-C does to it) while
the command runs; then the child's state is read from `/proc`:

```
0800b2e (now)   child state after Ctrl-C: S    (sleeping: still running)
1a5f18a         child state after Ctrl-C: Z    (killed, waiting to be reaped)
```

The command is typically an acceptance check — `pytest` with workers, minutes long. After Ctrl-C they keep running and may still be writing into the worktree when the next `--auto` run starts and
resets it. (Linux; Windows has no `start_new_session` here and was not tested.)

### What it does

* `_execute` kills the command's whole group on **any** exception that leaves the `communicate` call, not only `TimeoutExpired`: `os.killpg(proc.pid, SIGKILL)` (and `proc.kill()` where there is no group),
  `proc.wait()` so no zombie is left, then the exception goes on (`KeyboardInterrupt` stays a `KeyboardInterrupt`). The timeout branch keeps its present behaviour (the partial output it returns).
* A command that ends by itself is untouched.

### Tests (sketch)

`tests/test_executor_interrupt.py`:

* `_execute("sleep 60")` in a thread with `communicate` patched to raise `KeyboardInterrupt` (or a SIGINT sent to the process): the child's pid is dead afterwards (`/proc/<pid>` gone, or state `Z` reaped), the exception propagates;
* the same with a shell pipeline whose children are grandchildren (`sh -c 'sleep 60 & sleep 60; wait'`): every process of the group is dead;
* a command that finishes, and a command that times out, behave as before (the existing executor tests green unchanged).

### Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** Start `--auto` on a task whose acceptance check is `sleep 120`, press Ctrl-C: `pgrep -af 'sleep 120'` finds nothing.

---

## Part E — one claim that two entries classify differently refuses the whole claims phase

### What happens today

CC-14 judges the base side of the ticket's claims once and reuses it for every entry (`claims_phase` → `judge_both`, `base_side_record` / `base_side_differs`). Whether a claim is a **world**
claim (no code to anchor: judged once, at head) or a **code** claim is decided **per (base, head) pair** by `prepare_sides`: a name that one entry's head defines and another's does not
anchors in the first and not in the second. So in a real round — entries touch different code — the same claim against the same base classifies differently from entry to entry.
The recorded base side stores which claims were voted at the base (the first entry's classification); `base_side_differs` compares it with every later entry's own and raises
`ValueError` on any difference; `claims_phase` catches it and calls `_claims_refused`, which ends the **whole** phase.

Reproduced on `0800b2e` as a process (`judge_epic_round.py --claims FILE`; a repository whose base has no `widget`, entry A that does not define it, entry B that does; the claim
"`widget()` returns 42." in a claims file, no `expect`), in both orders of the entries:

```
order A, B -> exit 2   claims: B: recorded base side not re-used: the claims are not the record's (0 recorded, 1 in this run, first difference 1)
order B, A -> exit 2   claims: A: recorded base side not re-used: the claims are not the record's (1 recorded, 0 in this run, first difference 1)
rows: none      (not even the first entry's)
```

The feature meant to cut the requests across entries takes down the judging of the rest of the round over one entry's code shape, and gives no row at all. (A claim that carries an `expect` on a
world claim is a different, earlier refusal — "expect on a world claim" — and is not this defect.)

### What it does

* A base side is reused **claim by claim**, not as an all-or-nothing record: for every entry, the claims that are code claims for that entry and whose base verdict is in the record are taken from it;
  the base verdict of a claim the record does not hold is voted for that entry (once, and recorded for the next entry that needs it). A mismatch of the **claims** is therefore never a refusal.
* The refusals for what really makes a record another run's (the base sha, the voters, the runs, the rule, and — Part B — the budget, the collect flag, the seed, the models) stay as they are.
* Alternatively (the reviewer's other hint, acceptable if it is simpler and tested): decide up front the union of the claims that are non-world for at least one scored entry, vote the base side once
  for that union, and let each entry use its subset.
* The table has a row for every scored entry, whatever the order.

### Tests (sketch)

`tests/test_judge_epic_round_claims.py`, with the vote seam filled by scripted voters as the file does:

* the reproduction: entry A without `widget`, entry B with it, a claim without `expect`, both orders: no refusal, a row for each entry, the base voted for the claim at most once per entry that needs it;
* three entries, two classes of classification: every entry judged; the request count is below "a fresh base side per entry" and above "one base side";
* a record of another base sha, other voters or another rule is still refused, naming the field (the existing reuse tests green unchanged).

### Acceptance

**Layer 1 — offline, in the round.** The tests above; both suites green unchanged.
**Layer 2 — the operator.** On a finished round whose entries differ in what they define, `judge_epic_round.py --claims FILE` with a claim naming something only one entry defines: a table with every entry.

## Contest

A small round on the free roster, one commit for the five parts. Part A: read the carried-over list (it must come from the manifest **and** the artifact, not from the
manifest alone). Part B: read what the identity holds and what a record without the new fields does. Part C: read when the encoding is decided (before the first model call) and what a failed write leaves. Part D: read which exceptions kill the group and that the timeout branch is unchanged. Part E: read that a claims mismatch is never a refusal and that every scored entry gets a row. The judge's own process-level checks are not published.

## Not in scope

The wording of the three messages, the Gate 1 collect line, the manifest format beyond `unsummarized`, and the summarizer's retries (all CC-15, landed); how `judge_epic_round --claims` chooses its budget and collect flag; the voting and the tally; `/edit`'s containment, CRLF, mode and symlink handling (fix 34, landed).
