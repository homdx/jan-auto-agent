# Gate 1 claim vote — the live measurement (CC-10, layer 2), launch and forget

`[gate1] claim_vote` (CC-10, ticket 292) is `off` by default. The ticket's pass rule needs a live measurement before it
may be turned `on`; this page is how to run it, how to read it, and what to do with the answer. Everything is one command.

## 1. Run it

```bash
python3 scripts/gate1_vote_layer2.py --detach
```

That returns at once. The run goes on in the background and needs no attention:

| where | what |
|---|---|
| `contest-out/gate1-layer2/<UTC>/progress.log` | one line per variant start/end; its last line is `DONE — …/SUMMARY.md` |
| `contest-out/gate1-layer2/<UTC>/SUMMARY.md` | the answer; written after every variant, so a partial one exists while it runs |
| `contest-out/gate1-layer2/<UTC>/<variant>/run.log` | the full `--validate-plan` output of that variant (the vote line, every reader reason) |

Check on it, if you want to: `tail -3 contest-out/gate1-layer2/*/progress.log`. It never asks anything.

What it needs: `contest.local.ini` with the `claim_vote_llm.*` sections (the keys; they are copied into a 0600 ini inside
the output directory and never printed), and the target sha in this checkout's history. Nothing else; no Lenz, no Kilo.

Options worth knowing:

| option | meaning |
|---|---|
| `--sets 3,5,7,9` | voter sets to test (the default is `3:all,5:committed,7:committed,9:committed`); `--sets 3,5,7,9,11` adds the 11-voter set with two weak voters |
| `--sets 5:all,5:committed` | the same size under both acceptance rules, to see what `committed` buys |
| `--dry-run` | prints the variants, their voters and the reader; runs nothing |
| `--resume DIR` | continues a stopped run; variants that have a `result.json` are not run again |
| `--reader NAME` | the `claim_vote_llm.NAME` section used as Gate 1's single reader (default `kenary_agnes_3_0_flash_free`) |
| `--known DIR` | another known-answer set (`plan.json`, `truth.csv`, `TARGET`) |

The variants run **one after the other** on purpose: the free providers share a rate limit (kenary 15 requests a minute
across all its models), and two variants side by side would only turn each other's votes into missing votes.

## 2. What it measures

For every variant the script clones this repository at the sha in `TARGET`, puts the recorded Architect plan into
`.agent/plan.json` and runs `main.py --validate-plan`: the same Gate 1 (stage A, then the presence check) that
`--auto` runs at plan time, once per candidate. The tasks Gate 1 removes are read off its report.

| variant | `claim_vote` | voters | `vote_accept` |
|---|---|---|---|
| `off` | off | — | — |
| `on-3` | on | step-5-preview, muse-spark-1-3, nemotron-3-ultra | `all` |
| `on-5` | on | + agnes-3-0-flash, hy3 | `committed` |
| `on-7` | on | + nemotron-3-super, ling-3.1-flash (vercel) | `committed` |
| `on-9` | on | + agnes-2-5, mimo-v2-6 | `committed` |
| `on-11` (optional) | on | + laguna-s-2-1, north-mini-code — the two **weak** voters (7 and 2 wrong alone in the CC-11 table) | `committed` |

Why `committed` from five up: under `all` an abstainer blocks the vote for everybody, so more voters decide *fewer*
candidates (CC-11 measured 129, 125, 115, 103 of 150 claims for 3, 5, 7, 9 voters). `committed` keeps the unanimity among
the voters that committed and the three-families rule; a dissent still sends the candidate to the single reader.

The single reader is the same in every variant, so a difference between two variants is the vote's.

### The known-answer set

`contest-bench/cc/gate1_known_answers/`:

* `plan.json` — 56 candidates: the 53 the Architect wrote on 2026-09-08 for the goal "find every accessor that returns a
  mutable object from internal state and return a copy", and three (`AUTO-R1..R3`) taken from defects an earlier round
  fixed by hand (patches that still apply at the sha);
* `truth.csv` — the answer for each, `REAL` or `FALSE`, with the reason. The rule is the one of
  `ADJUDICATE-findings.md`: `REAL` means the defect is in the code at the sha (latent counts), `FALSE` means it is not, read
  against the callers. These answers come from reading the code, not from the vote;
* `TARGET` — the sha (`4ff7d14…`) the answers are valid at.

Composition, so the numbers are read fairly: **52 FALSE and 4 REAL.** That is what an Architect asked an over-broad
question produces (the repository's own audit of such lists found them almost all false positives), and it is the realistic
case for the vote's first job, *not letting false positives through*. It is a thin base for the other half, *not rejecting
true bugs*: four real bugs can only show a vote that rejects them, not prove that it never does. Add rows to `truth.csv`
and `plan.json` as real defects are settled; the script reads whatever is there.

## 3. Reading `SUMMARY.md`

The table has one row per variant:

* `false positives through` — kept by Gate 1 although the answer is `FALSE` (lower is better);
* `true bugs rejected` — removed by Gate 1 although the answer is `REAL` (lower is better);
* `asked / decided / overridden / requests` — the vote's own counters: candidates put to the voters, candidates the voters
  settled (the reader not asked, or its veto not used), unanimous `FALSE` verdicts the reader overruled with `confirmed`,
  and HTTP requests made;
* `verdict` — `PASS` when, against `off`, false positives are not more, true bugs rejected are not more, and `decided` is at
  least a third of the candidates. Otherwise `FAIL (...)` says which.

Below it is a per-candidate matrix (`R` removed, `k` kept) next to the recorded answer. A column that disagrees with `off`
on a candidate with an answer is where to look: `<variant>/run.log` has the vote line and the reader's reason.

Exit code: `0` all `on` variants passed, `1` one failed the rule, `2` a variant could not run.

## 4. What to do with the answer

* A variant `PASS`es and others do not: turn `claim_vote = on` with that variant's `vote_profiles` (and `vote_accept`) in
  your `agents.ini` / profile; the smallest passing set is the cheapest (three requests a candidate against five, seven, nine).
* None passes: leave `claim_vote = off`. The cause is in `run.log` (voters dead, rate-limited, or wrong), not in the rule.
* All pass equally: take three. More voters cost requests and the CC-11 table says they buy nothing measured.
* After flipping the flag, rerun this on every new Architect goal you care about once: add its candidates and their
  answers to the known-answer set first.

## 5. Gotchas met while building it

* Gate 1's reader is **not** `[gate1_llm]` unless `presence_llm_profile = gate1_llm` is set; by default it is the shared
  `[api_local]` provider (an Ollama on `localhost:11434`). The script sets it; a hand-made ini must too.
* The voters' sections (`claim_vote_llm.*`) live in `contest.local.ini`, not in `agents.ini`. The script copies them into the
  run's ini; a hand-made one has to contain them or the vote stays off and says why in the log.
* `main.py --validate-plan` edits and commits `plan.json` of the repository it is pointed at: that is why every variant has a
  clone of its own and why you never point it at a working checkout.
* The vote is a shortcut for the candidates that reach the presence check; a candidate removed by stage A (the cited place
  does not exist) is removed in every variant alike.
