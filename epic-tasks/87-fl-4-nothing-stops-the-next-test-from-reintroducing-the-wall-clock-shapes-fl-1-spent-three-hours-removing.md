# FL-4 — the three test shapes FL-1 spent five stress rounds removing can be reintroduced by the next test anybody writes: `scripts/check_test_clocks.py` names them

**Status:** queued — after FL-1 (84, landed `e500d40`) and ideally after FL-3 (86), which gives this check something to be checked against. FL-1's fix is a set of *rules* applied by hand to ~30 call sites; nothing in the repo records the rules or notices when a new test breaks them.

**Severity:** MEDIUM — no bug today; a guarantee with no guard, on a suite that took three hours and five stress rounds to make green once.
**File:** `scripts/check_test_clocks.py` (new); `docs/` or the script's own docstring for the three rules.
**Symbol:** `main`, `scan`, `Finding`; the rules C1 / C5 / hang-guard as named constants.
**Round:** 87
**Size:** M
**Source:** `e500d40`'s commit message, families C1, C5 and the "Hang guards that were pretending to be assertions" section. The rules it applied, in its own words:

- **C1** — "widen the slow path, do not tighten the bound": a test proving "the 0.5 s window fired, not the 2 s pause" has 1 s of slack; against a 60 s pause and a 120 s deadline bounded at 20 s it proves strictly more and has 19.5 s, and a green run returns in the same half second either way.
- **C5** — for a test that must *survive* a window rather than trip it, "tolerance for a starved box == window == how long the test runs". There is no margin to widen and no beat interval that substitutes.
- **Hang guards**: a subprocess/`join`/`wait` bound that is not the claim is scaffolding and belongs at 60–120 s, named and documented as a hang guard. `tests/test_auto_1.py` had 15 s against a 1.5 s run and the stress run went straight through it — and `subprocess.TimeoutExpired` is a far worse diagnostic than the `--timeout` stack dump that would have followed.

**Depends on:** FL-1 (84) for the rules; FL-3 (86) for the stress runner the check's findings are argued against.
**Also touches:** `tests/test_check_test_clocks.py` (new). Expected to *report* on existing files without changing them.

---

## What happens today

FL-1's family C was not one bug. It was thirty-odd wall-clock constants written by different people at different times under an assumption — that a test's own timing is roughly what the box will give it — that a box running four `pytest -n 8` invocations on eight cores does not honour. Fixing it meant deciding, per call site, which of three categories the number was in:

1. **A bound that *is* the claim** — "the 0.5 s window fired, not the 2 s pause". Untouched by FL-1; the deliberately short ones (0.1 / 0.3 / 0.5 / 1.0 s) are load-bearing and weakening them destroys the test.
2. **A bound that is a weaker second proof of what an adjacent assertion already makes load-independently** — `prov.peak == 3`, `status == "timeout"`. Removed or widened.
3. **Scaffolding** — the 21 `wait_idle(..., 5.0)` deadlines in the two contest test files, `barrier.wait(2)` (a rendezvous between two agent threads, not a deadline), the `main.py` subprocess guard. Raised to 60 s / 120 s.

Nothing in the repository records which is which, or that the categories exist. The next person who writes `assert time.monotonic() - started < 5` next to a one-second window reintroduces family C1, and they will be right that it passes on their machine — that is the entire character of this bug. The two C5-shaped tests are among the slowest in the suite and their docstrings explain why; the next reviewer who sees a 12-second test and "speeds it up" reintroduces C5, and the suite goes red on the operator's box and nowhere else.

The cost of finding that again is known: over three hours, five stress rounds, and five agents who could not.

## What must change

A check in the shape of `scripts/sync_test_tiers.py --check` — advisory, fast, reads source, changes nothing — that finds the shapes and makes the author say which category the number is in.

What it looks for:

1. **A wall-clock upper bound near a configured window.** `time.monotonic()`/`time.time()` deltas compared with `<`, and timeout-ish keyword arguments, where the constant is within a small multiple of a window configured in the same test (`idle_event_timeout_sec`, `turn_timeout_sec`, a `wait_idle` deadline). That is C1: the margin is absolute, and a few seconds of slack is not a margin.
2. **A "survives the window" test whose beats are counted rather than timed** — the C5 shape. The signal is a test that asserts a turn *reached* a terminal state under a configured silence window: its wall-clock span must come from the beat count, not from a sleep interval, and the window and the span must be in the file as the docstring says they are.
3. **A short bound on a `subprocess`/`join`/`wait`/`barrier.wait` whose expiry is not asserted on.** If nothing in the test asserts that the timeout fired, it is scaffolding, and scaffolding under 60 s is a hang guard pretending to be an assertion.

How an author answers it: a short marker comment on the line (`# clock: claim` / `# clock: guard`) that says which category this number is, with `guard` additionally requiring the value to be at or above the guard floor. The point is not to be clever about intent — the check cannot know it — but to make the choice explicit at the moment it is made, and to make a reviewer see it in the diff.

Deliberately **not** a formatter and **not** a failure. `--check` exits non-zero on an unmarked new finding, the plain run prints the table, and existing marked sites are silent. If the rules turn out to be noisy on this tree, the right response is to narrow the patterns, not to mark thirty sites `claim` to shut it up — a report with false positives that nobody trusts is the failure mode to avoid here.

## Acceptance

- [ ] `python3 scripts/check_test_clocks.py` on `e500d40`'s tree reports zero unexplained findings — every C1 / C5 / hang-guard site FL-1 touched is either marked or outside the patterns. If it cannot reach zero without marking sites that are genuinely fine, the patterns are wrong and must be narrowed; a wall of markers is not an acceptable way to reach zero.
- [ ] The check fires on all three shapes, proven by fixture files, not by the live suite: a `< 5` bound against a 1 s configured window; a "survives the window" test whose span comes from a sleep interval instead of a beat count; a `subprocess.run(..., timeout=15)` in a test that never asserts the timeout fired.
- [ ] It does **not** fire on the deliberately short bounds (0.1 / 0.3 / 0.5 / 1.0 s) where firing is the claim, nor on `barrier.wait(60)`, nor on a marked site.
- [ ] `--check` exits non-zero on an unmarked finding and zero otherwise; the plain run prints the table and exits zero.
- [ ] The three rules are written down where the check can be understood without reading `e500d40`'s commit message — including C5's line, that tolerance for a starved box equals the window equals how long the test runs, and that this is bought with wall time or not at all.
- [ ] The check runs in under a few seconds on the whole tree and starts no subprocess, no server and no provider.
- [ ] `pytest tests -n 4` then `pytest tests_bugfix -n 4`, sequentially, both green; `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- Changing any existing timing constant. FL-1 already made those calls; this ticket records them and guards them. A site the check disagrees with is a finding to argue, not a number to edit here.
- Rewriting a test to a different shape, or "speeding up" the two C5 tests. Their wall time is the assertion.
- Enforcement in CI or a hook, and any failing gate on the existing tree.
- Guessing which root-cause family a live failure belongs to — that is FL-3's table and a human's reading.
- `tools/auto/collect_bridge.py`. `CollectBridge._shrink` stays byte-identical.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `scripts/check_test_clocks.py`, its test file, the `.smoke_tests/` link, and marker comments if any were added. **No timing constant changed.** Never `epic-tasks/`.
- [ ] The fixture files above are real tests and fail without the change.
- [ ] Both test roots green, sequentially.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
