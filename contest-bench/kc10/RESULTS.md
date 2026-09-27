# KC-10 — round 49 — results

Base `8f50222`. Seven slots, all seven with work: two READY, three STALLED at the
90-minute limit with a commit, two ERROR with the work uncommitted (both
sensenova-6-8 entries were stopped by the runner's own context abort, fixed in
`1087584`; their diffs were scored as they stood). Every entry was set up with
`harness/setup_worktrees.py contest-out/49/entrants.json` and scored tree by tree
with `acceptance_kc10.py` (`run_all_kc10.sh <wt dir>`):

- K1–K3 — `KiloClient.model_limit`, `session_tokens`, `compact` against the fake Kilo;
- R1–R8 — the runner end to end: compact before the rework at 27 000 of a 32 768
  model and not at 20 000, a model with no limit sized by `context_limit_fallback`,
  the overflow → compact → same prompt → READY (R4, R4b), twice → ERROR (R5),
  `turns.jsonl`, `run.compactions`, `SUMMARY.md`'s `fill%` and compactions;
- D1–D5 — the fallback reaches the round (not a helper nothing calls), a remembered
  size beats it, a model Kilo sizes never reads it, the committed `contest.ini`
  names the key, and the base's recovery from two overflows is kept.

R4b and R5 are the ticket's item 6 to the letter and are marked `xfail`, outside the
score: the base already recovers from two overflows (KC-54 / KC-69: compact,
continue, compact again, READY — D5), so ERROR there throws that away, and the
"same prompt again" would restart the task where KC-69 sends `OVERFLOW_CONTINUE`.
R5 and D5 cannot both pass. A right tree shows them XFAIL; an XPASS marks a tree
that followed the letter. The score is out of the other 15.

The base passes 6 of 15 (K3, R1, R2, R4, R6, D5): KC-67/KC-69 had already built the
compact, `fill`/`compacted` and the overflow recovery the ticket asked for. What was
left is the fallback, `model_limit` / `session_tokens`, the count and SUMMARY.

| entry | state | score | suite (`tests`) | misses |
|---|---|---|---|---|
| **sensenova-6-7-flash-lite-var2** | READY `ccaa841` | **15/15** | 5924 passed | — |
| sensenova-6-8-flash-lite-var2 | ERROR (uncommitted) | 15/15 | 5922 passed | — |
| sensenova-6-7-flash-lite-var1 | STALLED `2e9a6d4` | 14/15 | 5920 passed | D5 — R5 XPASS: two overflows now ERROR |
| sensenova-6-8-flash-lite-var1 | ERROR (uncommitted) | 14/15 | 8 failed | D5 — R5 XPASS; KC-67 tests broken by the default, backend protocol test |
| mimo-v2-5 | READY `8690469` | 11/15 | 1 failed (timing flake, passes alone) | R3, D1 (fallback never read), R7, R8 |
| agnes-2-5-flash | STALLED `b933a11` | 11/15 | 17 failed | R7, R8, D2, D3; KC-67 tests broken by the default |
| agnes-2-0-flash | STALLED `d8d9db1` | 7/15 | 23 failed | R1, R6 (regressions of the base), R3, R7, R8, D1–D3 |

`tests_bugfix`: 2868 passed on every tree. Static checks (`harness/static_checks.py`):
one commit, tests shipped, `_shrink` and `epic-tasks/` untouched, tiers in sync — all seven.

## Who read the code first

| entry | reused what was there | new code wired in | cost to the base |
|---|---|---|---|
| sensenova-6-7-flash-lite-var2 | the fallback is a third source in `_context_budget`, the one place a size comes from; `_context_gate` does the compact | yes | none; fixed the three tests its own default changed |
| sensenova-6-8-flash-lite-var2 | moved `_context_gate` into `maybe_compact` (the ticket's name), −99 lines | yes | none |
| sensenova-6-7-flash-lite-var1 | same path | yes | item 6 as written: two overflows end ERROR |
| sensenova-6-8-flash-lite-var1 | same path, plus a `backend.py` method | yes | item 6 as written; the protocol test and six KC-67 tests red |
| mimo-v2-5 | its helpers call `_context_budget` / `_compact_session` | **no** — `maybe_compact` is never called, `context_limit_fallback` feeds only it (and it passes 0) | none, and nothing; its runner tests pin what the base already did |
| agnes-2-5-flash | new path beside the old one | partly | 17 red, KC-67's "no size" cases |
| agnes-2-0-flash | new path beside the old one | partly | the 32k compact and `turns.jsonl` broke (R1, R6) |

## Winner and the ideal

sensenova-6-7-flash-lite-var2: 15/15, the whole suite green, READY on its own, and
the smallest change that does the job — one new source in the function every size
already came from, the count on `AgentRun` and `state.json`, two SUMMARY columns.
sensenova-6-8-flash-lite-var2 ties on the score with a larger refactor and never
committed (the context-abort bug stopped it).

Changes on the way into `kc` (`ed8be97`, credited to the entry and to Claude):

- `context_limit_fallback` defaults to 0, in `ContestConfig` and the committed
  `contest.ini`. 32 768 was the ticket's guess for 32k free tiers; the models with
  no limit today are ~250k windows (round 49: agnes and mimo at 220–284k), and the
  fallback feeds `_context_budget`, so it would compact them every ~26k tokens and
  KC-69 would refuse their asks there. KC-67's memory sizes them from their own
  overflow; the key stays for a roster of small models. On the landed tree D4 reads
  the key, not the value.
- Item 6 is not taken as written (R4b, R5 XFAIL): KC-54 / KC-69 own the overflow.

On `kc` after the landing: 15 passed, 2 xfailed.
