# Before and after — how the collect work gets measured

**The "before" for the live runs is captured.**
`docs/collect-epics/baseline-live-2026-09-09.json`, produced by
`scripts/trace_round_snapshot.py`, which is read-only over `.agent/trace_*.jsonl`.
The "before" for the *static* side is not captured yet — it is ticket `M1`,
round 2, and that is the whole reason M1 blocks every other ticket.

This file answers one question: **what exactly do we run afterwards, and which
numbers are allowed to decide anything.**

---

## Three instruments, three jobs

| # | instrument | when | deterministic? | what it can decide |
|---|---|---|---|---|
| 1 | `scripts/collect_metrics.py` (ticket **M1**, **M2**, **M3**) | after every merged round that touches `tools/collect/` or `context_assembler.py` | **yes** — no LLM, no network, one artifact in, a table out | whether a ticket did what it said |
| 2 | `scripts/trace_round_snapshot.py` (exists) | after a full re-run of the five scenarios | **no** — LLM output varies run to run | whether anything *regressed*; how much wall-clock gate 1 gave back |
| 3 | `scripts/collect_ab.sh` (ticket **M5**) | once, after the pack is complete | yes, by construction — same goal, same seed, pack off vs pack on | whether the pack helps or is noise |

**Instrument 1 is the one that proves the tickets.** Instruments 2 and 3 are
guards. A round is never won or lost on a candidate count from a live run.

---

## Instrument 1 — the static before/after

The measurement is a diff of two JSON files over the **same artifact**:

```bash
# "before" — captured by M1, round 2, on the merged tree at that point
python3 scripts/collect_metrics.py --collect-dir ../jan-to-fix-pull-v2/.collect \
    --json docs/collect-epics/baseline-static.json

# "after" — same command, same artifact, later tree
python3 scripts/collect_metrics.py --collect-dir ../jan-to-fix-pull-v2/.collect \
    --json docs/collect-epics/after-r<NN>.json
```

The artifact does not change between the two, so every delta is code.
The numbers that must move, and the ticket that owns each:

| number | before | after | owner |
|---|---|---|---|
| tables dropped by the loader (`import_edges` / `imported_by` / `entry_points`) | 3 dropped | 0 dropped | `V1` |
| mean **new** (non-redundant) rows per block | 0.008 | ≥ 3 | `V2`–`V5`, measured by `M2` |
| blocks with ≥1 new row | 4 of 477 | ≥ 400 of 477 | `M2` |
| redundant chars / total chars | ~100% | < 50% | `M2` |
| blocks over budget (`_shrink` fires) | 50 of 477 (10%) | lower, and `_shrink` still byte-identical | `V6` |
| signatures still elided `name(…)` | 4030 / 4030 | 0 | `V10` |
| Pass B claims dropped by Pass C | 1368 of 2238 (61%) | lower | `V11` |
| suppressible findings ceiling | unknown | the number that decides EPIC B | `M3` |

`M1` writes the "before" column with script output; every number in the left
column above is hand-measured and is expected to move a little when M1 lands.
That is fine — what matters is that both columns come from the same script.

---

## Instrument 2 — re-running the five scenarios

### Freeze the baseline first

The snapshot is a **moving target while the runs are alive**. Between
22:17 and 23:20 on 2026-09-09 the totals went 942 → 1092 gate-1 calls.
Re-take it once all five report `"status": "idle"`:

```bash
for d in ../testtext ../testtext3 ../testtext5 ../testtext6 ../testtext7; do
    printf "%-14s " "$d"; python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['status'])" "$d/.agent/progress.json"
done
python3 scripts/trace_round_snapshot.py ../testtext ../testtext3 ../testtext5 ../testtext6 ../testtext7 \
    --out docs/collect-epics/baseline-live-2026-09-09.json
```

That file is the frozen "before". It carries each run's goal string verbatim
and its `pre_run_sha`, which is what makes a re-run comparable at all.

### The re-run

Every tree is the same repository. Four sat on `12df523`; **`testtext3` sat one
commit earlier, on `bb9b2ea`** — that is recorded per run in the JSON and it is
why the field exists. Reset each tree to its own `pre_run_sha`:

```bash
# one tree; repeat for all five, each with ITS OWN pre_run_sha from the JSON
SHA=$(python3 -c "import json;print([r for r in json.load(open('docs/collect-epics/baseline-live-2026-09-09.json'))['runs'] if r['run']=='testtext3'][0]['pre_run_sha'])")

git -C ../testtext3 reset --hard "$SHA"
rm -rf ../testtext3/.agent                    # a fresh trace, not an appended one
python3 main.py --collect --refresh --base ../testtext3 --config <your config>
```

The `--collect --refresh` is not optional. `testtext7` planned its entire
baseline run blind against a stale artifact; if the "after" runs are refreshed
and the "before" run was not, the comparison measures the refresh, not the code.

Then re-run the **same goal string, byte for byte** — take it from the JSON, do
not retype it — with the same flags, and snapshot again:

```bash
python3 scripts/trace_round_snapshot.py ../testtext ../testtext3 ../testtext5 ../testtext6 ../testtext7 \
    --out docs/collect-epics/after-r<NN>-live.json
```

### Which columns are a verdict and which are weather

| column | reading | why |
|---|---|---|
| `collect blocks` | **0 → >0 is a pass/fail.** Structural: the block is either built or it is not. | deterministic |
| `reason` on `testtext7` | **must not be `no_artifact`** once `L2` lands and the tree is refreshed | deterministic |
| `gate1` requests on non-`.py` locations | **must be 0 in code mode** once `L1` lands (74 of them in the baseline) | deterministic given the same candidates |
| `probe misses` | **must not rise** (baseline 24 of 773 ops) | a pack that makes the model ask *more* is a pack that added noise |
| `s/cand` | directional. 12.9–18.4 s today. | wall clock, shared machine |
| `arch` / `gate1` / `conf` / `rej` counts | **weather, never a verdict** | the model is non-deterministic; testtext5 and testtext6 differ by 52 candidates on the same tree |

Confirm *rate* is the one quality number worth watching, and only as a floor:
if it drops materially below the baseline 37%, something added noise.

### `--dry-run` skips the coder, by design

All five runs pass `--dry-run`, and the log ends
`dry-run: plan phase complete; execution skipped`. The coder never runs, so the
collect block — built only in `Coder._build_prompt` — is unreachable in this
mode *by construction*, not by accident. Two consequences:

1. For planning-only runs, which is how competition prep is done, the entire
   coder-side pack delivers nothing. Gate 1 and the architect are the only
   surfaces that exist. This is the argument for the reordering in
   `LIVE-RUN-VALIDATION.md` §6.
2. `collect blocks: 0 → >0` cannot be shown by a `--dry-run` re-run. It needs
   one execution run, or instrument 3.

---

## Instrument 3 — the A/B (ticket `M5`)

The only clean answer to "did the facts help". One goal, one tree, fixed seed,
local stub, `pack_enabled` false then true, everything else identical including
the config file — which the script copies and patches rather than editing.

Stated pass condition, deliberately weak: **probe misses and Gate-2 attempts do
not increase, and prompt size grows by less than the pack's own budget.**

---

## The order this happens in

| round | ticket | what it does to the measurement |
|---|---|---|
| 1 | `L2` | calibration. Touches a log line and a trace reason — no metric moves. Fixes the thing that silently invalidates run data. |
| 2 | `M1` | **captures the static "before".** Nothing else may land first. |
| 3+ | `L1`, `V1`… | each moves one row of the M1 table |
| 10 | `M2` | the redundancy number EPIC A exists to move |
| 15 | `M3` | go/no-go: how much of EPIC B gets built |
| 19–20 | `M4`, `M5` | runtime counters, then the A/B |

`L2` before `M1` is deliberate: it changes nothing M1 measures, and it is small
enough to calibrate the agents against a ticket with one right answer.

---

## Standing rule

Never point a measurement run at a live provider config. Copy `agents_128k.ini`
to a scratch path and stub every `base_url` first. `M5`'s acceptance says this
explicitly and `judge_epic_round.py` treats a diff that touches the live config
as a failed round.
