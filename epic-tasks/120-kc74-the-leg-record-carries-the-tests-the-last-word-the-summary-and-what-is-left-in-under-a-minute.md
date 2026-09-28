# KC-74 — the leg record carries the tests, the last word, the summary and what is left, in under a minute

**Status:** landed `3154f15` on 2026-09-28 (round 120, winner sensenova-6-7-flash-lite-var1 31/31 on contest-bench/kc74, taken as-is). Split out of KC-43 (82, `4a4c9fa`).
**Severity:** MEDIUM (without it leg 2 knows *what* changed but not whether it passes or what was meant next — the handover the operator writes by hand today)
**Round:** 120
**Size:** S
**File:** `tools/contest/runner.py` (`leg_record`, `leg_message`, `run_agent`'s harvest turn)
**Symbol:** `leg_record`, `LEG_RECORD_MAX_LINES` (new)
**Depends on:** KC-43 (82, landed `4a4c9fa` — `leg_record`, `leg_message`, `run_leg`, `--legs`), KC-40 (landed `dac85bd` — the model-written summary copied out of the session).
**Also touches:** `tests/test_contest_runner.py`

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, no test starts a real `kilo` or calls a live provider, do not edit `epic-tasks/` other than this ticket, one commit, no push; a test ships with the change and fails without it.

## What is on the base (after KC-43, `4a4c9fa`)

Read these before writing anything — they are the code this ticket extends:

- `runner.leg_record(run, ws, out_dir) -> Path` writes `<out_dir>/<agent>.leg.md`:
  a first line `leg <NN>.<L> — <agent>`, then `files:` (diffstat of the
  worktree against `ws.base_sha`, committed and uncommitted, via `_leg_files`),
  `commit:` and `harvest:` (the newest turn's `harvest` verdict and reason
  codes). Each says `none` when empty.
- `runner.leg_message(leg, ticket_path, records)` builds the next leg's
  paragraph: the instruction to continue, the ticket's declared files, and
  the records **already newest first**.
- `cli.cmd_run` runs the legs. It calls `leg_record` **after** `run_leg`
  returns, when that leg's `kilo serve` is already closed. So the record
  cannot ask the session for anything. The last message has to be saved
  while the session is alive, or read back from `<leg dir>/<agent>/events.jsonl`.
- The turn's saved harvest, `turn["harvest"]` in `run_agent`, keeps only
  `verdict`, `reasons`, `elapsed` and `waited`. The root counts live in
  `Harvest.facts["tests_run"]` (`harvest.py`, e.g. `tests:12✓ 1✗` or
  `skipped: no_commit`) and are **dropped** today. They must be kept on the
  turn (e.g. `turn["harvest"]["tests_run"]`) for the record to show them.
- `_last_assistant_text(backend, session)` exists (KC-40). `run.summary`
  holds KC-40's summary text.
- `legs = 1` never calls `leg_record` or `leg_message`. That must stay so.

## What happens after KC-43

The leg record is mechanical only: files and diffstat, the commit the leg
ended on, the harvest verdict. Leg 2 does not learn which pytest roots
were run and how they ended, what the agent said last, or what it meant to
do next.

## What must change

1. `leg_record` adds, in this order, after KC-43's fields:
   - **tests** — which pytest roots the leg ran (from the agent's own runs
     in the logs and from the harvest), each with its pass/fail counts;
     `none` when none ran;
   - **last message** — the agent's last assistant text, quoted, cut to
     20 lines; `none` when there is none;
   - **summary** — KC-40's model-written summary when the leg has one,
     `none` otherwise;
   - **what is left** — the ticket's declared files not touched yet, plus
     the failing test names from the **tests** field. Mechanical, not prose.
2. Only **last message** and **summary** come from a model; every other
   field comes from git and the logs. An empty mechanical field says `none`
   — prose never fills it.
3. **Size cap.** The whole record stays under `LEG_RECORD_MAX_LINES` (80).
   Over it, **last message** and **summary** are cut first (with a
   `… cut` marker); the mechanical fields are never cut.
4. The next leg's prompt lists the records newest first — KC-43 already
   does this in `leg_message`; keep it.

## Acceptance

- [ ] A leg with a red root: the record's **tests** names the root and its
      counts, and **what is left** names the failing test.
- [ ] A leg with no pytest, no message and no KC-40 summary: those three
      fields say `none`; **what is left** still names the declared files.
- [ ] A leg whose last message is 500 lines: the record is ≤ 80 lines, the
      mechanical fields are whole, the message ends in `… cut`.
- [ ] With KC-40's summary present it appears in the record verbatim (up
      to the cap).
- [ ] Two legs of records: leg 3's prompt has leg 2's record before leg 1's.
- [ ] `legs = 1` → no leg record is written; behaviour byte-identical.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- The live 2-leg round — the judge runs it by hand after this lands
  (`contest-bench/kc43/RESULTS.md`, per KC-43).
- Choosing the number of legs — KC-44.
- Cross-agent relay — see KC-43's Out of scope.
