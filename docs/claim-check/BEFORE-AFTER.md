# Before and after — the same claims judged at two commits (CC-7)

A review says "`run_step` calls `subprocess.run` with `check=False`" and a fix lands. Did it
fix it? `scripts/claim_diff.py` judges the claim twice with CC-6's evidence-bound vote:
at **base** with the pack built at base, and at **head** with the pack built at head **plus**
the `base..head` diff chunks of the paths the claim names, so the voters see what changed.

```bash
python3 scripts/claim_diff.py claims.json --target . --base main --head origin/kc \
    --profiles a b c --runs 3 --out claim-check-out/diff-kc --check
python3 contest-bench/cc/score_cc.py claim-check-out/diff-kc/delta.json claims.json   # with a key
```

Both commits are pinned and checked out as two read-only worktrees under `--scratch`; the
operator's checkout is never read, and both trees are removed at the end. The two sides
use the same voters, runs, pacing and retry budget (`claim_vote.py`'s own), and their jobs
share one pool.

## The words

| change | rule ("unanimous X" = `verdict == X` **and** `unanimous`) |
|---|---|
| `FIXED` | unanimous `TRUE` at base and unanimous `FALSE` at head |
| `STILL` | unanimous `TRUE` at both |
| `NEW` | `FALSE` at base (a plurality of `FALSE`; split, unsure or `TRUE` with a dissenter stay `UNCLEAR`) and unanimous `TRUE` at head |
| `GONE` | the claim's primary anchor (its first symbol, else its first path) resolves at base and not at head; the head votes are not read |
| `UNCLEAR` | anything else: a side that is not unanimous, `UNSURE` on either side, `FALSE` at both |
| `n/a` | a `world` claim: judged once, at head; it has no before and after |

`FALSE` at both is `UNCLEAR` on purpose: the claim was never true here, and "fixed" would be
a lie. A plurality is not enough: three `FALSE` and one `UNSURE` at head is not unanimous,
and the change is `UNCLEAR`, not `FIXED`. A model with no vote at all (dead, out of its free
plan) is not counted as a voter on either side, so it does not block the others.

A claim that is `world` at one side only (a commit that exists at head alone) is not treated
as a world claim: its world side gets a pack too, usually the empty one, so a vote cast
without evidence there is downgraded to `UNSURE` and the change is `NEW` or `UNCLEAR`.

**Expect many `UNCLEAR`.** A delta needs two decided sides. The costly error is a `still`
claim reported `FIXED` (score_cc's `still_as_fixed`), and that one must be zero. An `UNCLEAR`
is an honest answer.

## A worked example (`FIXED`)

Base `app.py`:

```python
def run_step(cmd):
    return subprocess.run(cmd, check=False)
```

Head `app.py`: the same function with `check=True`. The claim: "`run_step` in `app.py` calls
subprocess.run with check=False."

* base: the pack holds `src:app.py:4-5`; the three voters say `TRUE`, each quoting
  `return subprocess.run(cmd, check=False)` → unanimous `TRUE`.
* head: the pack holds `src:app.py:4-5` (the same id with another text, because `src:` ids
  carry no sha) and `diff:<base7>..<head7>:app.py:…`. The voters say `FALSE`, quoting
  `return subprocess.run(cmd, check=True)` → unanimous `FALSE`.
* change: `FIXED`; `delta.json` keeps `base.evidence` and `head.evidence` apart, one list a side.

## `--check`

A claim may carry `"expect": "fixed" | "still" | "new" | "gone"`. With `--check` the exit
code is `1` when any such claim got another change, and each mismatch is printed with the
evidence ids of both sides:

```
MISMATCH c-fix: expect still, got FIXED (base TRUE* [src:app.py:4-5]; head FALSE* [src:app.py:4-5])
```

Claims without `expect` never fail it. An `expect` that is not one of the four words is a
refusal (exit 2), not a claim that silently never fails. That makes the command a gate for
"this branch fixes what its tickets say": take the claims the branch's tickets make and run
base = `main`, head = the branch.

## What lands in `--out`

`delta.json` (`{"base": sha, "head": sha, "deltas": [{id?, claim, change, kind, expect?,
base: {verdict, unanimous, evidence}, head: {…}}]}`), `delta.txt` (the table printed, `*` =
unanimous), and `votes_base.json` / `votes_head.json` in `claim_vote.py --out`'s shape
(`score_cc.py votes_head.json key.json --side head` scores one side on its own).

## Cost

One voter, one run, one side is one request per 10 world claims plus one per 2 code claims.
The fixture's 80 claims are 36 requests a side and 72 for both; three voters with `--runs 3`
come to about 650. A `[plan]` line on stderr gives the count before the first call, and a
`[progress]` line follows every request (with the voter) and every finished voter run
(with the side).

## The honest limit

A `FIXED` means "the code at head no longer shows what the claim says", not "the behaviour is
fixed". Runtime behaviour that the text does not show stays `UNCLEAR`. Nothing is executed at
either commit.
