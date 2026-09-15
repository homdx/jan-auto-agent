# Next rounds — what to do first, second, and after, and what each step is measured by

**As of:** 2026-09-14, branch `tickets`, HEAD `6ca675c`.
Evidence: `after-r31-live.json` (snapshot of every `../testtext*` tree), the
per-session traces in `../testtext/.agent/` and `../testtext6/.agent/`,
`metrics.json` / `plan.json` of both. Nothing in this document was run
against a live provider by the reviewer; the runs are the user's.

---

## 1. What the live runs said

Two trees, one goal each, resumed over 6–10 sessions (plan 12–13.09,
execution 13–14.09).

| | testtext | testtext6 |
|---|---|---|
| plan | 41 tasks | 57 tasks |
| done / blocked / todo | 19 / 13 / 8 | 7 / 16 / 33 |
| collect blocks in coder prompts | 7 (tasks T1–T3) | 85 (T5–T9, T14) |
| latest session (M4 code) | 0 blocks, `collect_miss stale` ×22 | 0 blocks, `stale` ×23 |
| tasks with pack: done / blocked | 3 / 0 | 4 / 2 |
| tasks without pack: done / blocked | 16 / 13 | 3 / 14 |
| gate-1 candidates lost to empty replies | 142 / 403 (35 %) | 95 / 375 (25 %) |
| executor rejections (exit 5 = no tests collected) | 285 (67) | 153 (77) |
| coder rejections | 394 attempts | 501 attempts |
| probe misses (verdict floor) | 0 / 16 | 5 / 316 (baseline 9 / 312) |

Three findings decide the order below.

1. **The pack was off for ~80 % of executed tasks.** The artifact's
   `git_sha` fell behind HEAD after the first agent commits;
   `staleness = warn` turns the whole pack off for the session; nothing
   said so on stdout. (→ RUN-6)
2. **The blocked tasks are not blocked on context.** 29 BLOCKED tasks:
   6 coder output cut off at `[coder] max_tokens = 3000`, 4 prose instead of
   JSON, 7 validator "cannot see the body", 7 exec `exit 5` / ImportError
   whose cause was beyond the 400-character feedback cut, 2 real test
   failures. (→ RUN-3, RUN-4)
3. **Plan membership is decided by provider silence.** A third of the
   architect's candidates got no verdict and were logged `rejected`.
   Every downstream measurement (M3, M6, V12) reads a plan that a different
   provider mood would have made 50 % larger. (→ RUN-5)

The static instrument (M2) is unaffected by any of this and remains
positive: redundancy ~100 % → 65.1 %, new rows/block 0.008 → 3.608.

The within-run signal on the pack — 7 of 9 tasks done with it, 19 of 46
without — points the right way but is nine tasks, early ones, on a fresh
artifact. It is not a measurement.

---

## 2. The sequence

One ticket per round, same contest as before
(`RUN-THE-EPIC-COMPETITION.md`). Do not start a step before the previous
one is merged; steps 2–4 do not depend on each other in code, but the
snapshot after each is only readable if one thing changed.

### Step 1 — `RUN-6` stale artifact at session start
*Why first:* until the pack is on in a resumed session, no collect-side
number from a live run means anything. Also the smallest change with the
largest effect on what the next runs can show.
*Measure after:* next session on either tree → `python3
scripts/trace_round_snapshot.py ../testtext --run-id <new>` shows
`collect blocks > 0` and `collect_refresh` in the trace.
*Until it lands:* set `[collect] staleness = refresh` in a **copy** of the
config (see §4) — same effect, one key.

### Step 2 — `RUN-3` exec feedback tail
*Landed* — the commit after `d4ffb12`. Note for the measurement: the
workspace pytest command now ends in `-n 0`, so `bringing up nodes...`
disappears from every `exec failed` detail, not only from the tail slice.
*Why:* XS, deterministic, 7 blocked tasks, and it separates "coder could
not see the error" from "coder could not fix it" in the next snapshot.
*Measure after:* count of `exec failed` attempts whose detail contains an
`E   ` line; BLOCKED tasks ending on `exit 1/5`.

### Step 3 — `RUN-4` coder output budget ladder
*Landed* — `ade28e6`. Note for the measurement: a retry of
the same task keeps its climbed budget across outer rounds (task-id cursor),
so a task that needed 12 000 once is not re-climbed from 3 000 next round.
*Why:* the most frequent coder failure in both trees.
*Measure after:* `cut off before the JSON` occurrences per task; done/blocked
ratio; the new `max_tokens=` param on coder decision events.

### Step 4 — `RUN-5` presence unknown ≠ rejected
*Why:* plan size stops depending on the provider's mood; M3/M6 become
measurable.
*Measure after:* snapshot column `unknown` (new) vs `unparsed`; plan size
on a fresh `--auto` plan phase against the same goal.

### Step 5 — `L1` gate-1 Stage A0 for non-indexed locations
Deterministic: 82 (testtext) / 30 (testtext6) gate-1 LLM calls on
`.md/.ini/.yaml/.json` locations go away.
*Measure after:* `gate1_location_ext` in the snapshot has no non-`.py`
rows.

### Step 6 — `M3` suppressor ceiling
Measurement only. Its number decides whether V12/V13 exist.

### Step 7 — `V4`, `V10` supply rows
Only now, because only now the rows reach a coder. Re-run
`scripts/collect_metrics.py` after each (M2 numbers).

### Step 8 — `V12` / `V13`
Only if step 6 says ceiling > 0.

### Step 9 — `M6` outcome metric
Needs a plan that step 4 has made stable.

### Step 10 — `V8`, `V14`, `V15`, `L3`
Low value now. V14 only if docs mode is used at all; V15 last, it reads
every landed ticket.

---

## 3. The first live answer to "did the pack help"

After steps 1–4 are merged, on one tree:

```bash
git -C ../testtext reset --hard 12df523 && rm -rf ../testtext/.agent
python3 main.py --collect --refresh --base ../testtext --config <copy of the config>
python3 main.py --auto "<goal, verbatim from after-r31-live.json>" --base ../testtext --config <copy of the config>
python3 scripts/trace_round_snapshot.py ../testtext --run-id <run id> --out docs/collect-epics/after-r35-live.json
```

Then the M5 A/B with recordings from that run (`scripts/collect_ab.sh`,
`docs/collect-epics/ab-2026-09-14-stub-selfcheck.json` shows the harness
shape; it has not yet been run on real recordings). Two runs, same seeded
plan, `pack_enabled` on/off, one diff. That is the measurement STATUS.md
§2 has been waiting for since 12.09.

---

## 4. Config — the one key that matters today

`[collect] staleness` — the section is `[collect]`; in the user's config it
is the line `staleness = warn` (comment block above it explains the three
values). The reference copy with the full comment is `agents.ini` §`[collect]`
and `README.md` §collect (`staleness = warn # warn | refresh | ignore`).
Every preset (`agents_4k.ini` … `agents_256k.ini`, `agents_stub.ini`) has the
same key with `warn`.

```ini
[collect]
staleness = refresh        # rebuild incrementally (V7) before reading a stale artifact
auto_refresh_between_tasks = true
```

`refresh` runs `cli.action_refresh` — since V7 only modules whose hash
changed go through Pass B, so on a resumed run it costs one Pass B call per
file the previous session touched. Change it in a copy of the config, not
in `agents_128k.ini` on the branch.
