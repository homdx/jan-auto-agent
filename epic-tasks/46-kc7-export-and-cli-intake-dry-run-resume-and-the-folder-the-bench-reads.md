# KC-7 — `run --dry-run`, `status`, `entrants.json` and `SUMMARY.md`: the round's folder is the one `contest-bench` reads

**Status:** landed `3e9f5d5` — applied from patch 1-kc7-round46-winner + KC-72 62719ee
**Severity:** HIGH
**File:** `tools/contest/export.py` (new), `tools/contest/cli.py`
**Symbol:** `write_entrants`, `write_summary`, `cmd_status`, `cmd_run`, `intake`, `_parser`
**Round:** 46
**Size:** M
**Source:** `contest-bench/harness/setup_worktrees.py`'s docstring is the `entrants.json` contract (`base`, `entrants.<name>.source`, optional `duplicate_of`); `scripts/judge_epic_round.py --worktree name=path` is the scorer's input; `scripts/next_task.py` reads `**File:**` and `**Symbol:**` from every ticket. The run must stop where those start.
**Depends on:** KC-16 (landed), KC-31 (landed), KC-41 (landed).
**Also touches:** `tests/test_contest_cli_export.py` (new), `AGENTS.md` (the `--dry-run` and `status` lines under Build)

---

## What happens today

`python3 -m tools.contest run --ticket NN` runs the round and leaves
`contest-out/NN/` with one `.patch` or `.diff` per agent, `state.json`, the
per-agent folders and one JSON line per agent on stdout. After that the operator
still has to:

- hand-write `entrants.json` for `contest-bench` — round 87's folder has six
  patches and diffs and no `entrants.json`;
- read `state.json` or scroll the terminal to see who ended how, and grep
  every `decisions.jsonl` for the gate's refusals;
- start the whole round, kilo server included, to see the prompt the agents
  would get. No command prints the plan and stops.

`cli.py`'s own docstring says so: *"No summary, no `entrants.json`, no
scoring"*, and *"KC-7 (round 46) adds `status` and `--dry-run`"*.

## What must change

1. **`run --dry-run`.** Run `intake`, then `prepare_round`, then print the plan
   (`_print_plan`) and the exact prompt `runner.round_prompt` would send the
   first agent. Then exit 0.
   - It starts no `kilo serve`, not even intake's throwaway offer server, and
     opens no session and no gate call.
   - The checks that need a server (the offer, the variant probe, the gate
     probe) are skipped. Each prints one `dry-run: skipped …` line so the plan
     does not look fully checked.
   - The worktrees it creates are the round's own. A later `run` without
     `--fresh` meets them as KC-23 does today, and says so. `--dry-run
     --fresh` is allowed.

2. **`intake` refuses a ticket without `**File:**` or `**Symbol:**`.**
   `scripts/next_task.py` needs both, so the sessions would be handed a
   different ticket, or none. One `intake:` line per missing field, read from
   the base tree like the status check.

3. **`export.write_entrants(out_dir, base_sha, state, paths) -> Path`** writes
   `entrants.json` in the shape `setup_worktrees.py` reads.
   - `base` is the round's base sha.
   - There is one entry per exported `.patch` or `.diff`, keyed by agent name.
     `source` is relative to the repo root when the file is under it, and
     absolute otherwise. `setup_worktrees.py` and `validate_inputs.py` both
     resolve it against `--repo`, not against the JSON file's folder.
   - A patch that is byte-identical to an earlier one gets
     `duplicate_of: <first name>` instead of being a second entry. The
     comparison drops the `From <sha>` and `Date:` lines first.
   - A `GAVE_UP`, `STALLED` or `ERROR` entry is included. Its state is in its
     file name, and the bench scores it like any other; it gets `"state"`
     alongside `source`.
   - With no patch at all, no file is written.

4. **`export.write_summary(out_dir, state, base_sha, paths) -> Path`** writes
   `SUMMARY.md` in three parts.
   - **Header:** ticket, base, gate model, the round's start and end, and wall
     time.
   - **Table:** one row per agent from `state.table_rows()`: name, model,
     state, attempts, turns, asked/allowed/rejected/gated/gate-failed,
     questions, cost, tokens in/out, commit, last reason, and the file
     exported.
   - **"Decisions worth a look":** every `gate` and `gate-failed` decision
     from each agent's `decisions.jsonl`, with the command and the reason.
     The operator reads it to see whether the gate blocked something the
     ticket needed.
   - It ends with the two commands to run next, using the real paths:
     `python3 contest-bench/harness/setup_worktrees.py <out>/entrants.json --wt …`
     and `python3 scripts/judge_epic_round.py --round NN --base <sha>
     --worktree <agent>=<its worktree> …`. It does not name
     `scripts/contest_reset.sh`: that script prepares a round's worktrees and
     cleans nothing up.

   `cmd_run` calls both after `export_patches`, and prints their paths with the
   `patch:` lines. A failure to write either one is a `warn:` line, never a
   different exit code.

5. **`status --ticket NN [--out DIR]`** prints the SUMMARY table from
   `<out>/state.json` and touches nothing. It works mid-round too: `state.json`
   is saved at every transition. With no `state.json`, it prints one line and
   exits 1.

6. **`cli.py`'s module docstring** stops saying "no summary, no
   `entrants.json`". `AGENTS.md` gains the `--dry-run` and `status` lines.

## Acceptance

All tests are offline: no `kilo`, no provider, no network. The fake is
`tests/_kilo_fake.py`, attached through a monkeypatched `KiloServer.spawn`, as
in the existing `test_contest_cli*` files.

- [ ] `--dry-run` on a temp repo with a committed open ticket:
  - prints the plan, the prompt and the `dry-run: skipped` lines;
  - creates the worktrees;
  - `KiloServer.spawn`, `KiloServer.attach` and the gate client are never
    called (monkeypatched to raise);
  - exits 0.
- [ ] A ticket without `**File:**`, and one without `**Symbol:**`, each fail
      intake with that line and exit 1.
- [ ] A full `run` with two agents, one READY and one GAVE_UP with a commit,
      writes `<a>.patch`, `<b>.GAVE_UP.patch` and `entrants.json`, which
      `setup_worktrees.py`'s own parsing accepts: load it and resolve every
      `source` against the repo root, as that script does. It also writes a `SUMMARY.md` with
      both rows and a gate section, and exits 0.
- [ ] A STALLED agent with only a KC-31 `.diff` is in `entrants.json` as a
      `.diff` source.
- [ ] Two patches that differ only in `From <sha>` and `Date:`: the second
      is `duplicate_of` the first.
- [ ] `git am` of the READY agent's patch onto the base, in a fresh temp
      worktree, applies cleanly and reproduces the branch's tree.
- [ ] `status` after the run prints the same table as `SUMMARY.md`.
      Without `state.json` it exits 1.
- [ ] Every new test fails against `5d4d834`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` is green.

## Out of scope

- Merging, cherry-picking, scoring: the operator's stages 3–5.
- An `--attach URL` flag. The roster's `server = <url>` already does it.
- Uploading anything anywhere.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push. A test ships with the change and fails without it.
