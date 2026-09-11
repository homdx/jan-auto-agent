# Status — where the collect epics stand, and what is being waited on

**As of:** 2026-09-12, branch `tickets`, HEAD `2f9005d`.
Ticket-by-ticket state is also stamped into every `epic-tasks/NN-*.md`
(`**Status:**` line) and the `status` column of `epic-tasks/INDEX.md`.

---

## 1. Ledger

### Landed — 14 of 28, the whole short path (PLAN-v2 §6) plus L2 and V16

| ticket | commit | one line |
|---|---|---|
| L2 | `8212df1` | probe_config says `stale_artifact`, not `no_artifact` |
| M1 | `5fe7737` | `scripts/collect_metrics.py`, `baseline.json` |
| V1 | `81658b6` | loader keeps `import_edges` / `imported_by` / `entry_points` |
| V2 | `72bbc86` | block is an ordered row list (`_PACK_ROWS`) |
| V3 | `b609492` | rows `callers`, `calls_into`, `tests` |
| V5 | `da78fa3` | row `neighbours` `(llm)` — the one Pass B consumer |
| L4 | `6f2550f` | `from pkg import mod as alias` reaches the graph |
| L5 | `9664530` | test_map scans every root the graph scans |
| L6 | `ced5940` | budget loop shrinks fact rows to their count; `public_symbols` last |
| V7 | `25d5de8` | `--collect` on a stale tree is incremental; `--rebuild` is the full build |
| V11 | `9406601` | Pass C: a test file may cite what it imports |
| M2 | `5ace54f` | redundancy is a number: 65.1% (was ~100%), 3.608 new rows/block |
| V16 | `ac28672` | Pass C: a bare name in a source summary is the symbol defined here |
| V9 | `2f9005d` | a committed edit invalidates that path's facts; `auto_refresh_between_tasks` |

### Open — 14, re-verified against `2f9005d` on 2026-09-12

Ordered by what to do next, not by the original numbering.

| # | ticket | why this position | premise moved? |
|---|---|---|---|
| 1 | **V6** budget wire + memo | **L6 is dead code in a live run without it** — the bridge never passes `budget=`, so overshoots go to `_shrink` (LLM). Smallest, highest-value open ticket. | yes — see the ticket's "Verified" block |
| 2 | **M4** trace events | `collect_block` / `collect_shrink` / `collect_summary` + Gate-1 stage split. Without it the live snapshot counts blocks by grepping a header string. `collect_miss` already exists (V9). | partly |
| 3 | **M5** A/B harness | the only "did it help" answer. Must **execute** tasks (not `--dry-run`) against a stub; switch is `use_in_auto` today, `pack_enabled` once V6 lands. | yes |
| 4 | **L1** Gate-1 Stage A0 | 74 of 834 gate-1 LLM calls were on `.md/.ini/.yaml`; bridge already reaches `Gate1Filter`. Deterministic saving on every run. | slightly |
| 5 | V4 rows `fails_open`, `risk` | pure supply; deterministic; +2 rows to the pack | no |
| 6 | V10 real signatures | one intentional `collector_version` bump → one full rebuild | no |
| 7 | V8 `--no-llm` keeps summaries | narrowed by V7: only changed modules + `--rebuild` still null them | yes |
| 8 | **M3** suppressor ceiling | measurement only; decides V12/V13 | no |
| 9 | V12, V13 | **only if M3 says ceiling > 0** | gated |
| 10 | V14 docs mode | `use_in_doc` wiring exists, `_PACK_ROWS_DOC` does not | no |
| 11 | M6 outcome metric | needs M3 and whatever of V12 ships; scores against `validate1/truth.csv` | path fix: Measured table is in `EPIC-M-metrics.md` |
| 12 | V15 docs sync | last; reads every landed ticket | path fix as above |
| 13 | L3 goal-shape measurement | measurement only, no code; independent of everything | no |

Not scheduled: PLAN-v2 §5 appendix (persist call graph, index methods).

---

## 2. What is being waited on right now

**Instrument 2 — the live re-run of the five scenarios on `2f9005d`.**
Baseline: `baseline-live-2026-09-09.json` (five `--auto --dry-run` runs on the
pre-epic code). The re-run is *not* dry-run — `--dry-run` never reaches the
coder, so the collect block cannot appear — and it needs one execution run per
tree. The user runs it on their own provider config; nothing here is pointed at
a live config by the reviewer.

Per tree (each reset to **its own** `pre_run_sha` from the baseline JSON, goal
byte for byte from the same file):

| tree | `pre_run_sha` | goal (first words) |
|---|---|---|
| `../testtext3` | `bb9b2ea` | Find except blocks that swallow an exception… |
| `../testtext` | `12df523` | Find tests that would still pass if the code they guard were deleted… |
| `../testtext5` | `12df523` | Find regular expressions that parse structured documents… |
| `../testtext6` | `12df523` | Trace every write to a JSON state file… |
| `../testtext7` | `12df523` | Find places where one module documents a guarantee… |

```bash
git -C ../testtext3 reset --hard bb9b2ea && rm -rf ../testtext3/.agent
python3 main.py --collect --refresh --base ../testtext3 --config <config>
python3 main.py --auto "<goal, verbatim from the JSON>" --base ../testtext3 --config <config>
```

`--auto` takes the goal as its own argument; there is no `--goal` flag.
Run trees one after another, not in parallel.

Done so far: `testtext3` reset + refresh —
`collect refresh: tree unchanged — recomputed derived artifacts only, wrote 12
file(s)`. That is V7 behaving: `bb9b2ea` → `a3a0cc4` differs only in `.agent/`,
so no module hash changed and Pass B was not re-run. The `--auto` runs are
pending.

When all five `.agent/progress.json` say `idle`:

```bash
python3 scripts/trace_round_snapshot.py ../testtext ../testtext3 ../testtext5 ../testtext6 ../testtext7 \
    --out docs/collect-epics/after-r14-live.json
```

---

## 3. How to read the comparison

Side by side: `baseline-live-2026-09-09.json` vs `after-r14-live.json`
(the script prints one row per run; the JSON has the same fields).

| column | verdict or weather | what to expect on `2f9005d` |
|---|---|---|
| `collect blocks` | **verdict.** 0 → >0 is the pass | baseline is 0 for all five (dry-run). Any positive number passes. **It will be lower than a no-V9 tree would give**: after the first commit, every already-edited file's block is withheld (`auto_refresh_between_tasks = false`). That is the fix working, not a regression. With the flag `true` in a *copied* config the blocks come back at one Pass B call per repaired file. |
| `reason` on `testtext7` | verdict | must be `stale_artifact` or usable, never `no_artifact`, after the refresh |
| `probe misses` | verdict (floor) | must not rise above the baseline 24/773 ops |
| gate-1 requests on non-`.py` | verdict once **L1** lands | still ~74 today; L1 is open |
| confirm rate | floor | should not fall materially below 37% |
| `arch` / `gate1` / `conf` / `rej` counts, `s/cand` | **weather** | the model is non-deterministic; testtext5 vs testtext6 differed by 52 candidates on the same tree |

Two things the live run cannot show yet, and which ticket makes each visible:

- how many blocks went over budget and hit `_shrink`, and whether L6's row
  shrinking ever ran → **V6** (it does not run today) and **M4** (`collect_shrink`).
- whether the pack changed the coder's behaviour rather than just its prompt →
  **M5** (A/B against a stub).

**Instrument 1 — the static side — is already in.** Same artifact, same script,
before and after: redundant share ~100% → 65.1%, new rows/block 0.008 → 3.608,
blocks with ≥1 new row 4/477 → 497/497. Re-run after any round that touches
`tools/collect/` or `context_assembler.py`:

```bash
python3 scripts/collect_metrics.py --collect-dir ../jan-to-fix-pull-v2/.collect \
    --json docs/collect-epics/after-r<NN>.json
```

---

## 4. Config — what has to be set

Nothing new is required. The user's `agents_128k.ini` already has
`[collect] enabled = true`, `use_in_auto = true`, `staleness = warn`,
`llm_summaries = true`, `max_context_chars_auto = 1200` — every landed ticket
works through those. The one key the epics added so far:

```ini
[collect]
auto_refresh_between_tasks = false   # V9. true = repair a dirty module in place, one Pass B call each
```

Absent = `false`. `pack_enabled` (V6) does not exist yet; `use_in_doc`,
`use_in_bughunt` are still parsed-and-ignored until V14 / V12.

---

## 5. How each round was validated (the reviewer's loop)

Every round: N agent patches in `<ID>/`, each applied alone on the merged tip
in a scratch checkout, then:

```bash
git apply --check <patch> && git apply <patch>
grep -n "_shrink" — the diff must not touch a line inside CollectBridge._shrink
python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
```

The two roots run **one after the other, never combined** (conftest collision
→ ~362 false errors) and candidates run **one at a time, never in parallel**
— the machine is shared. `pytest.ini` carries `-n auto`; a single file is run
with `-n 1`, not `-p no:xdist` (that flag fails against the ini's addopts).
Between candidates the tree is restored with `git checkout -- .` plus removal
of the one new test file — never `git stash` / `git clean`, which would touch
untracked work.

Then a ranking table + `<ID>/REPORT-<ID>.md`, and the reviewer's own "ideal"
patch (best candidate as base + the fixes the review found) as **one local
commit, never pushed**. The user merges. `epic-tasks/` is edited only by the
reviewer (this file's ledger), never by a candidate patch.

Round results so far: every candidate in every round was green on both roots;
rounds were decided on the review, not on test failures. Reports:
`M1/`, `V5/`, `L4/`, `L5/`, … `V9/REPORT-V9.md`.

---

## 6. If the remaining tickets are worked

Same loop, in the order of §1's table. Two gates inside it:

- after **M4 + M5**, re-run the A/B once and write the numbers into
  `EPIC-M-metrics.md` **Measured** before touching V4/V10 — from then on every
  supply ticket is checked against "probe misses and Gate-2 attempts did not
  rise".
- after **M3**, decide V12/V13 by the ceiling (0 → drop both; 1–5 → V12 as
  ~40 lines; >5 → as written). Write the number into the M3 ticket first.

Every ticket's premise was checked against `2f9005d`; where the tree moved,
the ticket now carries a "Verified against `2f9005d`" block that says what
changed and what to do instead. Ground rule 1 still applies to whoever picks
one up: grep first, and if the code has moved again, the ticket is the stale one.
