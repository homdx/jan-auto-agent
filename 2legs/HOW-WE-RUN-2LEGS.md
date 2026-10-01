# Two-leg run — our hand checklist

We do this together, step by step. After each step there is a **✔ check** —
paste me its output before going on.

Paths (any machine — `env.sh` derives them from where it lives):

```
JAN    = the jan-auto-agent checkout on branch kc-2legs-runbook (holds this 2legs/)
TARGET = $JAN/../ext-demo-repo   (new, external repo; override: TARGET=/x source env.sh)
```

---

## Running everything at once (the runbook's test)

```bash
source 2legs/env.sh
export MODELS=provider/model-a,provider/model-b   # YOUR contestants, see step 0 #4
2legs/run_2legs.sh                # rebuild $TARGET, leg 1, land, leg 2, land, check
2legs/run_2legs.sh --check-only   # validate the last run, run nothing
```

It prints `PASS`/`FAIL` per check and exits 0 only when both legs ran and
every check passed. The winner pick is mechanical (first READY patch that
applies with green tests); the steps below are the hand version where you
read the patches yourself. `tests/test_2legs_runbook.py` checks offline that
the commands here still parse and the checker is honest — run it after edits.

## Verified

2026-09-29, both machines: `run_2legs.sh` → **11/11 checks passed**, with 2 and with 5
free models; every agent READY in both legs, leg 2 left `__repr__` untouched. One
run had a provider-overload `ERROR` in leg 2 — the other model's READY patch landed.
Which models ran is not recorded here (models are per machine, never in git).

## Every new terminal tab: `source` the env first

```bash
cd <your jan-auto-agent checkout>   # the folder that holds 2legs/
source 2legs/env.sh
export MODELS=provider/model-a,provider/model-b:free   # YOUR contestants, see step 0 #4
```

Variables (`$JAN`, `$TARGET`, `$KILO`, `$MODELS`) live only in the tab where you set them.
Without them `git -C $TARGET status` fails with `cannot change to 'status'`.

## 0. What you need to have

| # | thing | how to check |
|---|---|---|
| 1 | `JAN` on branch `kc-2legs-runbook` (on top of `kc`): holds `--target` (KC-76) | `python3 -m tools.contest run --help \| grep -- --target` |
| 2 | Kilo VS Code extension 7.6.x (`$KILO` comes from `env.sh`). The contest finds it by itself (`kilo_bin = auto` in `contest.ini` → newest `~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo`). PATH is **not** needed for the rounds; only step 5 (manual leg 2) calls kilo by hand, via the alias below | `$KILO --version` → 7.6.x |
| 3 | `contest.ini` (in git) + `contest.local.ini` (keys, gitignored — copy it by hand to each machine) in `JAN` | `ls $JAN/contest*.ini` |
| 3b | Roster loads (old `contest.local.ini` may carry unknown keys like `agent_suite_slots` — comment them out) | `cd $JAN && python3 -c "from tools.contest.roster import load_roster;load_roster('contest.ini');print('ok')"` |
| 4 | Contestants = 2–5 models that answer today, chosen by you: pick ids from the list, join with commas, `export MODELS=a,b,c`. Models are **not** stored in git — `--models $MODELS` replaces the roster of `contest.ini` for this run. `model@high` pins a reasoning variant; an id without `provider/` gets the provider from `--provider` (the CLI default) | `$KILO models \| grep ':free'` |
| 5 | Python 3.10+, pytest | `python3 -m pytest --version` |
| 6 | Time: ~30–60 min per leg | — |

Two ways to "split into two legs" — we do **A**, then **B** only if some agent did not finish:

* **A. Two tickets, two rounds** (works fully today): `01` = `__repr__`, `02` = `__eq__`.
* **B. One ticket, two legs**: leg 2 is a new Kilo session in the same checkout that
  continues leg 1's work. `--legs 2` (KC-43) does it automatically — only agents that
  ended GAVE_UP / STALLED go on to leg 2, the record is `contest-out/NN.1/<agent>.leg.md`;
  step 5 below is the same thing by hand, for an agent you want to push on yourself.

---

## 1. Make the target repo (once)

Builds the demo repo; refuses to delete `$TARGET` unless it is this demo repo.

```bash
$JAN/2legs/make_target.sh
```

✔ check:

```bash
git -C $TARGET status --porcelain -- epic-tasks; (cd $TARGET && python3 -m pytest -q -p no:xdist tests); git -C $TARGET log --oneline
```

(empty line, `1 passed`, one commit).

---

## 2. Round 1 — ticket 01 (leg 1 of the task)

```bash
cd $JAN
python3 -m tools.contest run --ticket 1 --target $TARGET --roster $JAN/contest.ini --models $MODELS
```

Watch stderr: `<agent>: WORKING … READY / REWORK / STALLED`. Don't Ctrl-C;
if it crashes mid-flight, re-run the same command with `--resume`.

✔ check:

```bash
ls $TARGET/contest-out/01/
python3 $JAN/contest-bench/kc77/leg_record.py $TARGET/contest-out/01 --all
```

## 3. Land the winner of round 1

We read the patches together and choose. Then:

```bash
cd $TARGET
git am contest-out/01/<WINNER>.patch
sed -i 's/^\*\*Status:\*\* open/**Status:** landed/' epic-tasks/01-*.md
git add epic-tasks && git commit -qm "KC-EXT-01 landed"
python3 -m pytest -q tests
```

✔ check: tests pass, `git log --oneline -3` shows the winner + "landed".

## 4. Round 2 — ticket 02 (leg 2, builds on leg 1)

```bash
cd $JAN
python3 -m tools.contest run --ticket 2 --target $TARGET --roster $JAN/contest.ini --models $MODELS --out $TARGET/contest-out/02.1
python3 $JAN/contest-bench/kc77/leg_record.py $TARGET/contest-out/02.1 --all > $TARGET/contest-out/02.1/leg-1-record.md
```

✔ check: the winner's patch adds `__eq__` **and leaves `__repr__` untouched**
(`grep -n '__repr__\|__eq__' <patch>`). That is the answer to "did it build on part 1?".

If every agent is READY → skip to step 6. If some agent is REWORK / STALLED / GAVE_UP → step 5.

---

## 5. (B) Manual leg 2 for an agent that did not finish

For each not-READY agent in `leg-1-record.md`:

1. Take `checkout:` from the record (its clone under `../rounds/…`).
2. Open a **new** Kilo session with that directory (not `--resume`):
   ```bash
   cd <checkout> && $KILO -m <that agent's model from $MODELS>
   ```
3. First message = ticket text + that agent's section of `leg-1-record.md` +
   > The worktree already holds work from leg 1. Continue from where leg 1 stopped. Do not start over. Make one local commit, no push.
4. When it ends:
   ```bash
   BASE=$(python3 -c "import json;print(json.load(open('$TARGET/contest-out/02.1/state.json'))['base_sha'])")
   git -C <checkout> log --oneline $BASE..HEAD
   git -C <checkout> diff --stat $BASE..HEAD
   (cd <checkout> && python3 -m pytest -q tests)
   ```

✔ check: leg-1 lines survived (`git -C <checkout> blame HEAD -- calc.py`) = continued, not restarted.

## 6. Land and finish

```bash
cd $TARGET
git am <winner patch>        # or: git -C <checkout> format-patch $BASE..HEAD --stdout | git am
sed -i 's/^\*\*Status:\*\* open/**Status:** landed/' epic-tasks/02-*.md
git add epic-tasks && git commit -qm "KC-EXT-02 landed"
python3 -m pytest -q tests
```

Then check it: `python3 $JAN/2legs/check_2legs.py $TARGET` — this needs
`$TARGET/contest-out/winners.txt` (`<leg> <agent> <sha>` per line), which
`run_2legs.sh` writes for you. Results are run output — never committed.

---

## If something breaks

| symptom | fix |
|---|---|
| `epic-tasks/ has uncommitted or untracked changes` | commit `epic-tasks/` in `TARGET` |
| round 2 refused, 01 still on offer | step 3's `landed` edit not committed |
| `kilo: command not found` | normal — use `$KILO` (step 1); the contest itself uses `kilo_bin = auto` |
| model refused at intake | pick another free model; `--register-missing` |
| all agents `quota` / 429 | wait or switch models; the round runs short, not wrong |
| `--roster` not found | always pass the absolute `$JAN/contest.ini` |
| `--resume` finds other agents | use the same `$MODELS` as the first start — agent names come from it |
| `unknown key … in [contest]` | comment that key out in `$JAN/contest.local.ini` |
| `gate: shares <host> with N agents` | warning only; a 429 there hits the gate too — point `[contest_gate_llm]` in `contest.local.ini` at another endpoint |
| `intake probe came back unparsable` | warning only; the round starts anyway |
| REWORK `shrink_changed` loops on the external repo | your checkout lacks KC-76's harvest fix — `git -C $JAN log --oneline \| grep KC-76` |
| `READY uncommitted_files` | the agent left files outside its commit; harmless for the pick — `git -C <checkout> status --short` shows them |
| `variant: …@highest → high` | normal — the variant is mapped to what Kilo offers |
