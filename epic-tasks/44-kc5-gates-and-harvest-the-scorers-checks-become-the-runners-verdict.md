# KC-5 — `tools/contest/gates.py` + `harvest.py`: the checks the round is scored on become the checks the runner sends back

**Status:** landed `0d91dd6` — ideal patch from a 13-entry contest, `kc5/*.patch`, scored black-box via `contest-bench/kc5/` (43 sandbox scenarios; the claim written by the real `scripts/append_task.py`, the pre-move `judge_epic_round.py` as the byte-for-byte oracle): winner kc5-Sensenova-6-8 (43/43) taken as the skeleton, three trims borrowed from sonet5-var2 (42/43) — `Harvest.commit` is the claimed sha or `None`, `rework_message` without the `Blocking:`/`(none)` scaffolding, the literal `runs/<agent>/PROGRESS.csv` in reason texts. What sank the rest: `append_task.py` rewrites `--outcome DONE` → `FIXED` before the CSV exists, so entries accepting one spelling only (sonet5-var2b: DONE; sonet, sonet5: FIXED) reject every real row; `kc5_sonet` (was `910a19c` on `kc`) called `harvest(..., base="HEAD")` and never read `ws.base_sha` — every worktree is "0 commits", never READY; glm4-7 and sonet4-var2 do not import on the judge's Python 3.10 (backslashes inside f-string expressions); NorthMini decorated `__post_init__` with `@property`; hy3 and sensenova-6-7-var2 bundled KC-12 into the same submission; one file was 0 bytes. `tests` + `tests_bugfix` green. Written against `docs/kilo-contest/PROBE.md`.  
**Severity:** HIGH  
**File:** `tools/contest/gates.py` (new), `tools/contest/harvest.py` (new), `scripts/judge_epic_round.py` (becomes a thin wrapper)  
**Symbol:** `judge_worktree` (moved from `judge_epic_round.judge`), `extract_shrink`, `ticket_for_round`, `harvest`, `Harvest`, `Reason`  
**Round:** 44  
**Size:** M  
**Source:** `scripts/judge_epic_round.py` already computes the mechanical scorecard per worktree (`shrink`, `commits`, `pushed`, `test_files`, `off_ticket`, `progress`) — as a script, for the operator, after the round. The runner (KC-6) needs the same facts *during* the round, per agent, as a verdict with reasons it can send back to the session. The probe established that a rework message into the same session works and that the model reads our text; the reasons have to be sentences an agent can act on.  
**Depends on:** KC-4 (`Workspace.progress_csv`, `base_sha`).  
**Also touches:** `tests/test_contest_harvest.py` (new), the existing tests of `judge_epic_round.py` (unchanged and still green), `scripts/next_task.py` / `append_task.py` (read only — the CSV columns they write are the contract)

---

## What happens today

`judge_epic_round.judge(name, path, base, declared, want_tests)` returns
one dict per worktree and `main()` prints/CSVs it. The logic lives in a
script under `scripts/`, so `tools/` cannot import it without a `sys.path`
hack, and its output is a table row, not a decision. The
`runs/<agent>/PROGRESS.csv` contract (`ticket`, `outcome`, `commit`,
`note`, written by `append_task.py`) is the agent's own claim of being
done; nothing today combines the claim with the facts.

## What must change

1. **`tools/contest/gates.py`** — `judge`, `extract_shrink`,
   `ticket_for_round`, `git` move here verbatim (renamed `judge_worktree`;
   the script imports them and keeps its CLI). **The script's stdout and
   CSV are byte-identical before and after** on the same inputs — a
   golden test captures `--csv` output on a temp round before the move
   and compares after. Add `declared_files(ticket_path) -> tuple[str, ...]`
   (the `**File:**` line plus `**Also touches:**` paths) so the runner
   does not re-parse tickets.

2. **`Reason`** — frozen: `code` (one of `no_progress_row`,
   `progress_not_done`, `no_commit`, `commit_not_on_branch`,
   `commits_ne_1`, `pushed`, `no_test_file`, `shrink_changed`,
   `off_ticket_files`, `tests_failed`), `text` (one sentence for the
   agent, ≤ 200 chars, naming the file/number), `blocking: bool`.

3. **`Harvest`** — frozen: `verdict: Literal["READY", "REWORK"]`,
   `reasons: tuple[Reason, ...]`, `commit: str | None`, `facts: dict`
   (the `judge_worktree` row), `elapsed: float`.

4. **`harvest(ws: Workspace, ticket_path: Path, *, run_tests: bool = False) -> Harvest`**:
   - read `ws.progress_csv` (may not exist); the last row for this
     ticket's filename is the claim; no row → `no_progress_row`;
     `outcome != DONE` → `progress_not_done`; no commit → `no_commit`;
     a commit that is not an ancestor of the branch head → `commit_not_on_branch`;
   - `judge_worktree(...)` → `commits != 1` → `commits_ne_1`; `pushed == yes`
     → `pushed`; `test_files == 0` → `no_test_file`; `shrink != same` →
     `shrink_changed`; `off_ticket` non-empty → `off_ticket_files`
     (**non-blocking**, listed);
   - `run_tests=True` → `gates.run_tests(path)` (the script's four roots)
     → any failure → `tests_failed` with the tail of the output (last 20
     lines) in `text`;
   - `READY` iff no blocking reason. `reasons` always lists everything
     found, so a READY with an off-ticket note still shows the note.

5. **`rework_message(h: Harvest, attempt: int, max_rework: int) -> str`** —
   the text the runner sends into the session: a fixed header ("Your
   ticket is not accepted yet. Attempt N of M. Fix the items below in
   this same worktree, amend into **one** commit, and run
   `append_task.py` again."), one bullet per blocking reason, the
   non-blocking ones under "Also noted", and the two ground rules that
   settle a round (`_shrink` untouched, a test that fails without the
   change). No ticket text is repeated — the session has it.

## Acceptance

- [ ] Golden test: `scripts/judge_epic_round.py --round 1 --worktree a=… --worktree b=… --csv out.csv`
      on a temp round produces the same stdout and CSV bytes as it did
      before this change (capture with a `git stash`-free method: the test
      builds the expected strings from the *moved* function and the *old*
      function imported from a pre-change copy of `scripts/judge_epic_round.py`
      into a temp module).
- [ ] `tests/test_contest_harvest.py` on temp worktrees: no
      `PROGRESS.csv` → REWORK `no_progress_row`; a DONE row with a commit
      that is on the branch, one commit, a `tests/test_x.py` in the diff,
      `_shrink` untouched → READY with empty blocking reasons; two commits
      → `commits_ne_1`; commit without a test file → `no_test_file`; a
      changed `_shrink` → `shrink_changed`; a file outside the ticket's
      list → READY with a non-blocking `off_ticket_files`; `run_tests=True`
      with a failing test → `tests_failed` and the tail in the text.
- [ ] `rework_message` on a two-reason harvest contains both sentences,
      the attempt counter, and the word `append_task.py`.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- An LLM review of the diff — harvest is mechanical by design; the
  scoring side (`contest-bench`, the operator) reads code.
- Running the entries' tests in parallel — one worktree at a time, as
  `contest-bench/README.md` insists.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink` (the harvest checks it; it must not
  change it).
- `scripts/judge_epic_round.py` keeps its CLI and its output bytes.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
