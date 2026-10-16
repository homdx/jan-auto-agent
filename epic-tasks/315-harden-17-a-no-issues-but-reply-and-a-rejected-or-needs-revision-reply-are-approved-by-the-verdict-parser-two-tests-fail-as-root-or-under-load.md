# HARDEN-17 — the verdict parser approves `No issues found, but …`, `Rejected: …`, `REJECTED - …`, a bare `Rejected` and `Needs revision: …` / `Needs work: …`; two tests make a CI run red (one fails as root, one is timing-dependent)

**Status:** open
**Severity:** HIGH-MEDIUM (A: a rejected chapter or edit is approved; the parser's fail-open default); LOW (B: tests only)
**File:** `tools/auto/inner_loop.py` (A), `tests/test_actions_edit_legacy_encoding.py`, `tests/test_gate1_vote_inflight_review.py` (B)
**Symbol:** A: `_parse_verdict_soft`, `_en_not_family_reason`, the rejection word lists next to them. B: `test_a_failing_write_removes_the_backup_it_made`, `test_votes_of_eight_workers_wait_for_the_whole_queue`
**Round:** (set when the round starts)
**Size:** S
**Depends on:** HARDEN-14 (312, the NOT family in the same function; land after it)
**Also touches:** `tests_bugfix/` pins of HARDEN-5 A and HARDEN-10 C (must stay green), `tests/`

---

## Why

An outside review of `0815666` found these. The parser items were re-run on `0815666` and are real; they are not in HARDEN-14 (312), which covers only the `NOT …` / `Nothing …` family. The two test items are environment and load effects, not product bugs, but they make a CI run red. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a reply that rejects, or approves and then objects, is approved

### What happens today

On `0815666`, `_parse_verdict_soft(reply)[0]` is `True` (approved) for the lines below. **Update after CC review fix 41 (landed on top of round 313):** the first two (`No issues found, but …`, `No issues with the prose, but …`) are rejected now; the rest are still approved on the current head and remain this part's work:

```
No issues found, but the ring is stolen in ch 3.
No issues with the prose, but chapter 2 contradicts the bible.
Looks good, but the ring is stolen.
Rejected: the timeline breaks.
REJECTED - Anna is dead
Rejected
Needs revision: Anna is dead.
Needs work: pacing.
```

`Revise: scene 3.` is already `False`. The first three open with an approval phrase and the objection follows the `but`; the rest carry the rejection word but the default of the parser is approval. For the continuity gate, which has no second chance, each of these ships a chapter its validator rejected.

### What it does

* A reply whose first clause is a rejection word (`rejected`, `reject`, `needs revision`, `needs work`, `needs changes`, `revise`, `rewrite`, `fail`, `failed`), alone or followed by `:`, `-`, `—` or a period and a reason, is not approved.
* An approval phrase that is followed by `but`, `however`, `although`, `except`, `yet` or `though` and a further clause is not approved on the approval phrase alone: the part after the conjunction decides, and a reply whose objection part cannot be told from praise is a not-approved with the reason kept. `No issues found, but all minor.` is a judgement call: decide it and pin it in a test, say which way in the commit.
* The existing approvals stay approved: `No issues found.`, `No problems.`, `Approved`, `APPROVED: …`, `Looks good.`, and the HARDEN-5 A / HARDEN-10 C tables.
* Do not make a second list of words beside the NOT family of HARDEN-14: one place decides a reply, and both families go through it.

### Tests (sketch)

Each line above → not approved, with a reason that quotes the objection; the approvals above → approved; the continuity gate (`ContinuityValidator`, no second chance) rejects the first, fourth and seventh line.

---

## Part B — two tests that fail for the environment

### B1 — `test_a_failing_write_removes_the_backup_it_made` fails as root

The test makes the write fail with `os.chmod(tmp_path, 0o500)`, which root ignores: the write lands, `note.txt` reads `line two` and the assertion `'line two\n' == 'line one\n'` fails. It fails whenever the suite runs as uid 0 (containers, CI). Make the write fail without file permissions — monkeypatch `os.replace` or the atomic writer to raise `OSError` — so the test means the same thing for every user; keep the assertion that the backup the write made is removed.

### B2 — `test_votes_of_eight_workers_wait_for_the_whole_queue` is timing-dependent

`VOTE_SLACK_SEC = 0.1`, a 0.05 s pacer interval, a 0.4 s fake server, 8 threads on 24 requests; the docstring already says it failed "1 round in 14" under load, and it failed once in a full `-n 8` run (3 of 3 in isolation). Scale the slack from a measured baseline of the same machine in the same run, or take the wall clock out of the assertion (a fake clock), and say which; do not just raise a constant, and do not mark it `flaky` and leave it.

### Tests (sketch)

B1 passes as uid 0 and as an ordinary user (run once under `unshare`/`sudo` or assert on the monkeypatched failure); B2 passes 10 times in a row under `-n 8` load.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the parser over a table of approvals and rejections in English and Russian and counts the flips against `0815666`; the only flips allowed are the ones above.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A creative-mode run whose validator answers `Rejected: …` returns for another revision instead of shipping the chapter.

## Not in scope

Rust (`pub(crate)` functions are not found: not fixed), the `NOT …` / `Nothing …` family and the cp1251 `/edit` (HARDEN-14, ticket 312), the mask (HARDEN-15, 16).
