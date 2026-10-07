# 203 — `claim_vote.py` counts and reads wrongly: a bracket in a reply, a tied model, a voter that said nothing, a shared parser, a missing profile, a key file readable by all

**Status:** queued
**Origin:** operator review (`bugs-to-review/`, bugs 17, 18, 213, 214, 215, 216, 217 and the "left unchanged" voter finding), each re-probed on `arena` @ `00355fd` (CLAIM-1 merged from `origin/arena` @ `03a77c5`)
**Severity:** HIGH (18, 19: a claim is accepted that a voter did not commit to — the one rule the claim check exists for), LOW (17, 20, 21, 22)
**File:** scripts/claim_vote.py
**Symbol:** parse_votes, tally, voter_settings, ask, main, add_profiles
**Round:** 203
**Size:** M
**Also touches:** docs/claim-check/RUNBOOK.md (one line each for 18, 19, 22), tests_bugfix/

## Why

The claim check (`scripts/run_claim_check.sh`) cuts a report into claims and lets free models of three families vote; **a verdict is accepted only when all voters commit to it** (`unanimous`, the `*` in the table — "36 of 40 unanimous, none wrong" is the number the runbook stands on). Four things make that star appear when it should not, and two make a whole model's answer disappear without a word. All were probed on `arena` @ `00355fd`.

## Bugs (each probed)

**17 — a bracket outside the JSON list loses every vote of the reply** (`parse_votes`, `claim_vote.py:162`).
It takes the text from the *first* `[` to the *last* `]` and parses that as one JSON value. Probed with `parse_votes(text, [0, 1])`:

| reply | result |
| --- | --- |
| `[{"id":1,"verdict":"TRUE"},{"id":2,"verdict":"FALSE"}]` | `{0: 'TRUE', 1: 'FALSE'}` |
| `Judging [3 claims]:` + the same list | `{}` |
| the same list + `[Note: see above]` | `{}` |
| `See [1].` + the same list | `{}` |

A model that answered every claim counts as a model that gave nothing: `votes=0/N`, which the runbook reads as "a model to drop from the roster", and a claim loses voters below the quorum of three. Fix: try every `[` as the start of a JSON array (`json.JSONDecoder().raw_decode`) and take the first list that holds vote rows (`id` and `verdict`).

**18 — a model that contradicts itself is a committed voter** (`tally`: `Counter(v).most_common(1)[0][0]`).
`most_common` settles a tie by insertion order, so the *first* run decides. Probed: model `a` voted TRUE then FALSE, `b` and `c` voted TRUE twice each → `verdict TRUE`, `unanimous True`. With `--runs 2` (or any even number, or three different answers) one model's own disagreement is invisible and the claim is accepted. Fix: a model's verdict is its **strict** plurality; a tie is an abstention (`UNSURE`), which the runbook already defines as "not a vote". This makes acceptance stricter on purpose — say so in the commit and in the runbook line. Also make it independent of the order of the runs (a test).

**19 — `unanimous` ignores a voter that returned nothing for the claim** (`tally`: `committed == len(model_major)`).
`model_major` holds only the models that voted *on this claim*, so a dead model, a lost batch or a batch whose reply `parse_votes` could not read is not in the denominator. Probed: voters `a`, `b`, `c` TRUE and `d` with no vote → `unanimous True`, `by_model` lists three models. The rule is "every voter", and the voters are the ones asked, not the ones that answered. Fix: pass the voter list into `tally` (or derive it from `results`, every `model` of every result) and require `committed == len(voters)`; a claim a voter has no vote for is not unanimous. Keep the quorum rule for fewer than three voters as it is.

**20 — two voters resolved at once can get each other's settings** (`voter_settings`, `claim_vote.py:116`).
It writes the profile name into the *one shared* `ConfigParser` (`parser.set(SECTION, "_pick", ref)`) and reads it back through `resolve_llm_profile`; `main` shares that parser between the `--parallel` worker threads. A thread that set `_pick` can have it overwritten before it reads. The vote model `prof_a` was asked for is then cast by `prof_b`'s endpoint and recorded under `prof_a`'s name. Fix: no write into shared state — resolve through a copy of the parser per call (or under a lock for the write+read pair); a test with two threads and a barrier.

**21 — a voter that cannot be resolved aborts the whole pool** (`ask`, `main`).
`ask` calls `voter_settings(ref, parser)` outside any `try`; a misspelt profile name raises `ValueError: [claim_vote] _pick = 'p_typo' but the config has no [p_typo] section`, and a `provider/model` whose Kilo file is missing raises `FileNotFoundError` — both out of the thread pool, so one bad voter throws away the other voters' completed work. The `ask` docstring and the runbook say "a dead model is a result, not a crash". Fix: catch `(ValueError, OSError, KeyError)` around the resolution and return the usual result row with `errors` set and no votes; `main` prints one line per such voter.

**22 — `--add-profiles` copies API keys into a file with the umask's mode** (`add_profiles`, `claim_vote.py:127`).
It copies a provider's real key out of Kilo's `auth.json` (kept private by Kilo) into `contest.local.ini` with `Path.write_text`. Probed: the file came out `0o664` (a `022` umask gives `0o644`): every other user of the machine can read the key. The arena's own `write_profile_keys` (`mkstemp`, `0600`) never did this. Fix: create the file with `os.open(..., 0o600)` when it does not exist (a file that exists keeps its mode); the runbook line says the key is "copied, never printed" — add "and never world-readable".

## Not in this ticket (a decision, listed so it is not lost)

`tally` with fewer than three voters can never accept a claim and `main` prints no warning. That follows the documented quorum; only the missing warning is questionable. One line on stderr at the start of a run is enough if the owner wants it.

## Tests

One file per bug in `tests_bugfix/` (`test_claim_vote_<what>_203.py`), each failing on the old code:
17 — the four replies above, a reply with two lists (the first with vote rows wins), a reply with no vote rows (still `{}`);
18 — a/b/c above, the order of a model's runs does not change its answer, a 2-of-3 majority of one model's runs still stands, three consistent models are still unanimous;
19 — four voters, one silent: `unanimous` is False and the verdict is still decided by the three that voted;
20 — two threads and a barrier: each gets its own model; 21 — a misspelt profile and a missing Kilo file are rows with `errors`, the others still vote; 22 — a new file is `0600`, an existing file keeps its mode.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "203" -q
python3 -m pytest .smoke_tests/ -q
python3 -m pytest tests/test_claim_check.py -q
```
