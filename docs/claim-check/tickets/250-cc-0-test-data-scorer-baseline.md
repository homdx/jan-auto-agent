# CC-0 — the shared test data, the scorer, the baseline and the package skeleton

**Status:** done (operator, see `contest-bench/cc/BASELINE.md`)
**Severity:** MEDIUM (nothing else in the epic can be scored without it)
**File:** `contest-bench/cc/make_fixture.py`
**Symbol:** `build_fixture`, `score`
**Round:** — (operator work, not a contest)
**Size:** L
**Depends on:** —
**Also touches:** `contest-bench/cc/claims_fixture.json`, `contest-bench/cc/claims_real.json`, `contest-bench/cc/real_checks.py`, `contest-bench/cc/score_cc.py`, `contest-bench/cc/thresholds.json`, `contest-bench/cc/run_cc.sh`, `contest-bench/cc/BASELINE.md`, `tools/claimcheck/__init__.py`, `tests/test_claimcheck_bench.py`

---

## Why

The rounds of this epic are scored by **shared test data**, not by the entries' own
tests (the way every large round has been since `contest-bench/` was introduced): the
operator writes the data once, runs every entry against it, and reads code only to build
the ideal patch. For this epic the data is a set of claims about code **with a known
answer**, and a repository to judge them against. `validate1/truth.csv`, which would have
been that key, is not in the tree; the key has to be built.

CC-0 also creates the empty package so that CC-1 and CC-2, which run at the same time,
do not both create `tools/claimcheck/__init__.py`.

## What it makes

### 1. The package skeleton

`tools/claimcheck/__init__.py`: a docstring naming the epic and nothing else.

### 2. The fixture repository (`make_fixture.py`)

`build_fixture(dest: Path) -> Fixture` creates a small git repository, deterministically:
fixed author, committer, dates and `GIT_CONFIG_GLOBAL=/dev/null`, so the two shas are the
same on every machine. `Fixture` is `(root, base_sha, head_sha)`.

* **Base commit:** ~300 lines in 6 Python modules (`pipeline.py`, `store.py`,
  `gates.py`, `config.py`, `paths.py`, `retry.py`) with seeded, documented behaviours of
  the kinds our reports are about: an exception swallowed in one branch, a return of `0`
  on a failed subprocess, a regex that reads one line (`re.M` without `re.S`), a default
  read from the wrong ini section, an off-by-one in a slice, a shallow copy handed out, a
  guard that runs after the action it guards, a retry loop without a bound. Each seeded
  behaviour is listed in `fixture_facts.json` with file, symbol and the exact lines.
* **`epic-tasks/`:** three small tickets (`01-…md`, `02-…md`, `03-…md`) with `**Status:**`.
* **Head commit:** fixes **20** of the seeded behaviours (the commit message names them),
  leaves 10 unfixed, and renames one function (so `GONE` has a case).
* The fixture is plain Python so `tools.collect` can model it.

### 3. The claims

`claims_fixture.json`: **80** claims, each

```json
{"id": "f017", "claim": "...", "kind": "code|world|mixed|dangling|commit|ticket",
 "truth_base": true, "truth_head": false, "anchors": ["gates.py", "check_exit"],
 "expect": "fixed|still|new|gone|null", "how": "read gates.py:41-48"}
```

Composition: ≥ 25 true code claims and ≥ 25 false code claims (the false ones include
near-miss wording: right function, wrong detail), 10 world claims (5 true, 5 false), 8
mixed, 6 dangling (a path or symbol that does not exist at the pinned sha), 6 claims about
a commit or a ticket by name. Of the code claims, 20 have `expect` set to `fixed` and 10 to
`still`, 2 to `gone`, 2 to `new`: the CC-7 key. Every claim's truth is established by
reading the fixture, written down in `how`, and re-checked by `test_fixture_truth_is_stated`.

*As built:* the two counts above cannot both hold in 50 code claims — the 20 `fixed`,
10 `still` and 2 `gone` claims are true at base by definition (32), leaving 18 for the
false side (2 `new` + 16 near misses). The CC-7 key wins: **32 true / 18 false** at base;
at head the split is 12 true / 36 false / 2 undecided (`gone`). The 6 commit/ticket
claims are 3 + 3. Each claim's truth is computed from a *probe* (the text whose presence
at a commit makes it true) by `make_fixture.py --write`, never typed in.

`claims_real.json`: **30** claims about *this* repository at a pinned sha
(`real_sha`, recorded in the file), taken from `kc-bug-report.md` and the review files.
Each has `truth` and `how`; `how` is a command or a reading that settles it, and
`real_checks.py` runs the commands that can be run (`pytest --bogus` exits 4; `git diff
--numstat -M` prints `a => b`; `Policy.decide("cat $HOME/.ssh/id_rsa")` is approved) and
fails when a recorded truth no longer matches. A claim whose truth cannot be established
by running or reading is not put in the file.

### 4. The scorer (`score_cc.py`)

`score(votes: dict, truth: dict) -> dict` and a CLI `score_cc.py votes.json
claims.json [--thresholds thresholds.json]`: per kind and overall — claims, decided
(`unanimous`), right, wrong, undecided, coverage, precision of the unanimous verdicts;
fabricated-quote downgrades when `votes.json` carries them; for CC-7 files, the
`FIXED/STILL/NEW/GONE` confusion. Exit 1 when a threshold is missed.

`thresholds.json` carries the §6.2 targets of `EPIC-CC.md`, each with a `note` field.

### 5. The baseline (`BASELINE.md`, `run_cc.sh`)

`run_cc.sh [--profiles …]` runs today's `scripts/claim_vote.py` (no evidence) over both
claim files with three voters of three families and prints the score table. The result,
with the date, the voters and the sha, goes into `BASELINE.md`. If the baseline moves a
target in `thresholds.json`, the note says why.

## Tests (`tests/test_claimcheck_bench.py`)

| Test | What it pins |
|---|---|
| `test_fixture_shas_are_stable` | `build_fixture` twice, in two directories → identical base and head shas |
| `test_fixture_facts_match_the_files` | every entry of `fixture_facts.json` names lines that exist and contain the seeded text |
| `test_fixture_truth_is_stated` | every claim has `how`, `truth_base`, `truth_head`; the fix claims differ between base and head |
| `test_claim_files_are_well_formed` | ids unique, kinds from the allowed set, counts per kind as above |
| `test_scorer_counts` | a hand-made `votes.json` → exact right/wrong/undecided/coverage |
| `test_scorer_exit_code_on_threshold` | below threshold → exit 1; at threshold → 0 |
| `test_real_checks_agree_with_recorded_truth` | `real_checks.py` run on the runnable claims (all 30; ≈2 s, so not marked `slow` — the repo has no such marker), skipped without a git checkout of the pinned sha |

## Acceptance (the operator's)

* `python3 -m pytest tests/test_claimcheck_bench.py -q` passes; tiered.
* `contest-bench/cc/run_cc.sh` prints the baseline table, no network beyond the voters.
* `BASELINE.md` states, for the fixture and for the real claims, how many code claims the
  no-evidence voters decide; the expected value is near zero. If it is not, the epic's
  premise (§1) is wrong and the epic stops here.

## Not in scope

The behaviour of any later ticket; the thresholds are recorded here, enforced from CC-6.
