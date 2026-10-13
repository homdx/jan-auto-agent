# CC-13 — the collect model for the judges: used when it is there and fresh, on the voters' side too, with a line that says so

**Status:** open — FINAL, written from what the CC-10 layer-2 runs showed (2026-10-12/13) and from reading the code paths; not yet measured.
Cut it into `epic-tasks/` with the first word `open` when the round starts. It does not depend on CC-12 (callers in the pack, 294): the two touch the same
pack and can be done in either order, but the layer-2 numbers should be taken for each separately.
**Severity:** MEDIUM (it decides whether the judges are measured, and later run, with the evidence the pipeline has for them)
**File:** `tools/auto/gate1_vote.py`, `tools/collect/manifest.py`, `scripts/gate1_vote_layer2.py`
**Symbol:** `Gate1Vote._presence_by_vote` (the `model=` of `build_pack`), `discover_files` / `DEFAULT_SKIP_DIRS`, `[gate1] vote_collect`
**Round:** —
**Size:** M
**Depends on:** CC-5 (the pack accepts a collect model: `build_pack(model=…)`, landed), CC-10 (the vote and the layer-2 instrument, landed)
**Also touches:** `agents.ini` (`[gate1]`, `[collect]`), `tools/auto/gate1_filter.py` (one log line), `docs/claim-check/GATE1-VOTE-LAYER2.md`, `tests/test_gate1_vote_collect.py`

---

## Why

"Judges" here are the two models that decide whether a candidate's problem is present: Gate 1's single reader and the CC-10 voters. What they are shown is a
code block and, for the reader, notes. The repository has a structural model of itself for exactly this — `collect` (`.collect/`: contracts, fail-open
registry, config map, gates, test map, per-module summaries) — and the code paths that could use it are there but not all connected:

| judge | uses the collect model today | where |
|---|---|---|
| Gate 1's single reader | yes, **when** `.collect/` is in the base directory of the run and `[collect] use_in_auto = true`: a contract note for the cited symbol (`collect_contract_note`, GATE1-CTX-1) and the "does the model know this file" check (stage A0) | `make_collect_bridge` in `plan_validator.validate_plan` and the controller |
| the CC-10 voters | **no**: `Gate1Vote` calls `build_pack(claim, resolved, view, budget=…)` without a `model=`, so a pack never holds a `collect` chunk, although `build_pack` and `claim_vote --collect` support them | `tools/auto/gate1_vote.py` |
| a layer-2 run | **no, silently**: it works on a fresh `git clone` of the target at a sha, `.collect/` is in `.gitignore` and not there, and nothing in the log says collect was absent | `scripts/gate1_vote_layer2.py` |

So every layer-2 number so far (`docs/claim-check/GATE1-VOTE-LAYER2.md`) is of a reader **leaner than the one `--auto` runs with**, and of voters that never could have had
the collect chunks. Whether collect would change them — fewer false positives let through by the reader, fewer wrong unanimous TRUEs — is not known, and it is
a cheaper candidate for improving the vote than a new pack (CC-12): the artifact exists and is built once per tree.

Two findings from making one:

* `collect` of this tree takes about an hour with a free model (`sensenova-6.8-flash-lite`: 395 modules re-summarized at ~10 s each on an incremental run, 943 on
  a full one) and is rate limited; it must be built for the tree the judges look at, not rebuilt per run.
* `tools/collect/manifest.py` skips a **fixed** set of directories (`.git`, `__pycache__`, `.pytest_cache`, `node_modules`, `.venv`, `venv`, `.collect`, and any
  starting with a dot) — there is no setting. A checkout that keeps round output (`contest-out/` with 32,539 `.py` files in the sandbox where the judges'
  references run) is scanned whole: no output for five minutes at 96 % CPU, and the module list would be 33,000 long. Round outputs are not part of the project.

## What it does

* **`[collect] skip_dirs`** (default empty = today's list). Extra directory names, comma separated, added to `DEFAULT_SKIP_DIRS` by `discover_files`; the manifest records
  the effective list, so a change of it makes the tree stale (and nothing else about an existing artifact changes). `contest-out`, `rounds`, `claim-check-out`
  are named in the `agents.ini` comment as the usual ones.
* **`[gate1] vote_collect = off | auto`** (default `off`: every pack is what it is). With `auto`, the vote loads the collect model of the run's base directory once
  (`CollectModel`, read-only) and passes it as `model=` to `build_pack`, **only if it is fresh**: the same freshness test as `main.py --collect --check` (the manifest's
  hashes match the tracked files; `[collect] staleness` is honoured: `ignore` never uses a model, `warn` uses a stale one and flags it, `refresh` is not done by the
  vote — it never writes). An absent or stale model is no model: the pack is today's, and the log says so once.
* **One line at the start of Gate 1** (all variants, also with the vote off): `collect: fresh | stale (N modules changed) | absent — reader: used/not used, vote pack: used/not used`.
  Counters `collect_chunks` (chunks of kind `collect` in the packs of the run) and `collect_state` next to the vote's own, so a result says what it had.
* **`scripts/gate1_vote_layer2.py --collect-from DIR`**: DIR is a `.collect/` built for the known-answer target (`TARGET`'s sha; a checker refuses one whose manifest does not match
  the clone's tree), copied into every clone, with a `+collect` suffix on the variant name (`off+collect`, `on-3+collect`) so the table puts the same variants with and
  without side by side. `contest-bench/cc/gate1_known_answers/README.txt` says how the artifact is built (`git worktree` at the sha, `main.py --collect --config <a
  config whose active provider is the free one>`), and that it is rebuilt only when the sha changes.

## Tests (sketch)

`tests/test_gate1_vote_collect.py`, no network: a fixture repository and a hand-written `.collect/artifact.json` + manifest. `vote_collect = off` or no artifact: the pack is
byte for byte today's and the log line says `absent`. A fresh artifact with `auto`: the pack of a claim about a module the artifact knows holds a `collect` chunk, and a quote
taken from it is verified by `ask_with_packs` like any other (real reply shapes: a quote over two lines, separate JSON objects); a file changed after the manifest was written makes the
model stale and not used (and `staleness = warn` uses it and says so); a manifest of another `collector_version` is stale. `skip_dirs`: a tree with `contest-out/x.py` lists no
module of it, the manifest records the list, changing the setting marks the tree stale. `--collect-from` refuses a mismatching manifest and names the first file that differs.

## Acceptance (two layers)

**Layer 1 — offline, in the round.** The tests above; the existing suites green unchanged (two roots, one after the other); a black box (`contest-bench/295/blackbox_295.py`, a fake
model server, the real process) in which three scripted voters answer FALSE **only** when a `collect` chunk about the cited symbol is in the pack: with `vote_collect = off` the case is
TRUE (today, pinned), with `auto` and a fresh artifact it is rejected, with `auto` and a stale one it is TRUE again and the log line says `stale`.

**Layer 2 — the operator, live, one command.** An artifact for `4ff7d141` is built once (about an hour with a free model; the README says how) and
`python3 scripts/gate1_vote_layer2.py --detach --collect-from <dir> --off-runs 3 --sets 3,5,3+collect,5+collect,5+veto+collect` runs the baseline three times and each
voting variant with and without the collect model. Pass for the option, not for the vote: with `+collect` the reader's false positives through are not more than
without, and the vote's own wrong unanimous TRUEs are fewer (the rate at the third run was 75 to 88 %); no real bug rejected that was not rejected without it. Anything less leaves
`vote_collect = off`, and the layer-2 guide records what the collect model changed — the answer to "do the judges pull from collect" is a number, not a yes.

## Contest

A mini contest, **Opus, Sonnet and Haiku by hand**, like CC-12: small, deterministic, offline acceptance; the risk is the freshness test and the one place the pack is built, not the coding.
Do CC-12 and CC-13 as two rounds, not one: they change the same function and their layer-2 effects must be separable.

## Questions

1. **Why not make the vote always use collect.** An artifact can be stale or wrong (it was built by a model: Pass B summaries are LLM text, the rest is static). The static chunks are
   the safe ones; the layer-2 numbers decide whether the summaries help or mislead. A later ticket can split the two (`vote_collect = static`).
2. **Does the reader's use of collect change.** No: it is wired already; this ticket adds the line that says whether it was there, and a way to measure it.
3. **Why a copy per clone and not a shared path.** The layer-2 clones are separate trees at one sha so that `--validate-plan`, which edits and commits `plan.json`, cannot touch
   anything shared; the artifact is read-only data and is copied, not linked.

## Not in scope

Changing what collect extracts, the collect prompts, `--auto` itself, the unanimity rule, the families rule, the Architect, or the default of `claim_vote`.
