# FL-3 — the only thing that finds a flake in this suite is a hand-typed 64-worker stress run, repeated by eye: `scripts/stress_suite.py` runs it and prints the flake table

**Status:** queued — after FL-1 (84, landed `e500d40`). FL-1 cost over three hours and five stress rounds, and every one of them was a shell line retyped by the operator with the results read off the terminal. Five agents failed the same ticket partly because they had no mechanical way to tell "green" from "green this time".

**Severity:** HIGH — not for a bug it fixes, but because without it FL-1 comes back and nobody notices until a round is already burning.
**File:** `scripts/stress_suite.py` (new).
**Symbol:** `main`, `run_pass`, `parse_failures`, `flake_table`.
**Round:** 86
**Size:** M
**Source:** FL-1's own Diagnosis section is a 20-run bash loop writing `/tmp/fl_$i.log` and a `grep -h "^FAILED" | sort | uniq -c | sort -rn` at the end. `e500d40`'s Verification section is a different command again — four suites, 64 workers, on 8 cores:

```bash
pytest tests -n 8 & pytest tests_bugfix -n 8 & \
pytest tests -n 8 & pytest tests_bugfix -n 8 &
```

Neither is in the repo. `contest-bench/fl1/RUNBOOK.md` (`d8e6d1a`) documents both and says in §4 that one run is not a result — FL-1's own evidence was 19 runs, 8 red, a different victim each time.

**Depends on:** FL-1 (84) — the suite must be green before a tool that asserts it is green is worth anything.
**Also touches:** `tests/test_stress_suite.py` (new), `contest-bench/fl1/RUNBOOK.md` §4 (the hand command is replaced by the script; keep the raw command in a footnote so the tool is never the only way to run it).

---

## What happens today

There is no artifact in this repository that answers "is the suite reproducibly green". `scripts/sync_test_tiers.py --check` answers a different question (is every test file tiered), `pytest` answers "was it green once", and the gap between those two is exactly where FL-1 lived for weeks.

Everything about the stress run is currently carried by a person:

- **The command** exists in two incompatible forms — FL-1's `-n 4` serial loop and `e500d40`'s four-suites-at-once — and neither is checked in, so the next round re-invents it and may re-invent it weaker.
- **The repetition count** is a judgement call made after the fact. FL-1 needed 19 runs to see 8 reds; a candidate who ran it three times and saw green concluded it was fixed.
- **The result is read by eye.** A red run names one to four tests out of a suite that prints thousands of lines, and the failing set is different every time, which is precisely the signal that says "independent races" and precisely the signal a human scrolling past loses.
- **The two test roots must run sequentially** and never as one pytest invocation; that rule lives in the operator's head and in ticket prose, not in a tool.

For FL-1 this was survivable because one person spent an afternoon on it. As a round handed to agents it is not: the ticket asks for "reproducibly green" and gives the candidate no way to produce that claim, so the claim comes back unevidenced and the scorer re-runs everything by hand anyway.

## What must change

A script that runs the stress command N times and turns the logs into the table the operator currently builds with `grep | sort | uniq -c`.

```bash
python3 scripts/stress_suite.py                      # default: the e500d40 shape, 5 passes
python3 scripts/stress_suite.py --passes 20 --shape serial   # FL-1's -n 4 loop
python3 scripts/stress_suite.py --passes 3 --keep-logs /tmp/fl
```

Requirements, in order of how much they matter:

1. **Both shapes, named, not retyped.** `--shape stress` is four concurrent invocations (`tests -n 8`, `tests_bugfix -n 8`, twice); `--shape serial` is the `-n 4` loop over both roots. Worker count and root list come from flags with those defaults, so a different box can say so without editing the script.
2. **Never combine the two roots into one pytest invocation**, and never run two *passes* concurrently — the concurrency inside a pass is the input, a second pass on top of it is noise that invalidates both. The script enforces this; it does not document it.
3. **The table is the output.** Per test: how many passes it failed in, out of how many. A test that fails 3/20 is the finding; a test that fails 20/20 is not a flake and should be labelled as a plain failure so nobody hunts a race that is not there.
4. **Exit code is the claim.** Zero only when every pass was green. Non-zero when any pass was red, with the table on stdout and the log paths named.
5. **Keep the logs.** `--tb=long -rf` per pass into a directory (`--keep-logs`, default a temp dir whose path is printed), because the traceback is what tells the reader which family a failure belongs to — FL-1's Diagnosis section is entirely about reading those tracebacks.
6. **`--timeout` is passed through and never raised silently.** The script prints the timeout it used. A pass killed by `pytest-timeout` is reported as such and its stack dump is kept — that dump is what identified FL-1's family A.

It must not try to classify failures into families. That is a reading task, the families are ticket-specific, and a wrong automatic label is worse than none.

## Acceptance

- [ ] `python3 scripts/stress_suite.py --passes 2` on a green tree exits 0 and prints a table with no rows.
- [ ] With a deliberately flaky test injected into a scratch tree (fails ~50 % on a coin flip), `--passes 8` exits non-zero, the table names that test with a count strictly between 1 and 8, and the kept logs contain its traceback.
- [ ] With a deterministically failing test, the table labels it as failing every pass, distinctly from a flake.
- [ ] The two test roots never appear in one pytest invocation, and two passes never overlap in time — proven by a test that records the invocations the script would make rather than by running the real suite.
- [ ] `--timeout` is reported in the output; a pass killed by `pytest-timeout` is reported as killed, not as a plain failure, and its stack dump is kept.
- [ ] The script's own tests run in seconds and never invoke the real suite.
- [ ] `contest-bench/fl1/RUNBOOK.md` §4 points at the script, with the raw command kept as a footnote.
- [ ] `pytest tests -n 4` then `pytest tests_bugfix -n 4`, sequentially, both green; `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- Any change to a test or to `tools/`. This ticket adds a script and its tests; if the stress run then finds something, that is a new ticket.
- CI wiring, schedules, or a git hook. The operator decides when to spend eight cores for ten minutes.
- Classifying failures into root-cause families, or any attempt to guess whether a failure is a race.
- Raising `--timeout` to make a pass green. The timeout is load-bearing.
- `tools/auto/collect_bridge.py`. `CollectBridge._shrink` stays byte-identical.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `scripts/stress_suite.py`, its test file, the `.smoke_tests/` link and `contest-bench/fl1/RUNBOOK.md`. Never `epic-tasks/`.
- [ ] The flaky-test and deterministic-failure cases above are real tests, and they fail without the change.
- [ ] Both test roots green, sequentially.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
