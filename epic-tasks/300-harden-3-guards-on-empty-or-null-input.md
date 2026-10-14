# HARDEN-3 — five guards on empty or null input: an empty ticket, a brace-form rename, an empty TARGET, a null timestamp, an empty exception message

**Status:** open — FINAL, written 2026-10-14 against `e9e5d13`; every defect below was reproduced (`run`) or read in the code (`read`) on that commit. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** LOW (C2 is the only one that changes a result on a real input)
**File:** `tools/contest/gates.py`, `tools/contest/runner.py`, `scripts/gate1_vote_layer2.py`, `tools/auto/view_trace.py`, `tools/arena/profile.py`
**Symbol:** `ticket_for_round`, `_split_numstat`, the `TARGET` read in `gate1_vote_layer2.main`, `render_summary`, `_one_line`
**Round:** —
**Size:** S
**Depends on:** CC review fixes 34–36 (landed, `c2ef95b`): this ticket starts from their code
**Also touches:** `tests/test_guards_empty_input.py` and one pinned test per row in `tests_bugfix/`

---

## Why

Two outside reviewers listed small defects; each entry here was checked against the code and **reproduced** or **read** (marked `run` / `read` below). Most of their other entries were already fixed or wrong and are refused in HARDEN-1's table. Each defect below is a wrong answer on an input nobody tested; each needs the guard the sibling code already has. A defect is done when it has its test and the suites are green unchanged (`.smoke_tests/ .regression_tests/`, then `tests_bugfix -n 4`).

## guards on empty or null input (five, all small)

| id | where | input | now | after |
|---|---|---|---|---|
| C1 | `tools/contest/gates.py` `ticket_for_round` | an empty `NN-*.md` | `IndexError` on `splitlines()[0]` | the title is `""`; the round refuses it with a message naming the file |
| C2 | `tools/contest/runner.py` `_split_numstat` | a rename `a/{b => c}/d.txt` | the path becomes `c}/d.txt` | `a/c/d.txt` (the brace form is expanded; `old => new` without braces as now) |
| C3 | `scripts/gate1_vote_layer2.py` `main` | an empty `TARGET` file | `IndexError` on `.split()[0]` | exit 1 with a message naming the file |
| C4 | `tools/auto/view_trace.py` `render_summary` | an event with `"ts": null` | `TypeError` on `None[:19]` | `(evt.get("ts") or "")[:19]` |
| C5 | `tools/arena/profile.py` `_one_line` | an exception whose message is empty | `IndexError` on `splitlines()[0]` | the exception's type name |

C2 is the only one that changes a result on a real input: `git diff --numstat` prints a rename in the brace form by default, so a moved file was counted
under a path that does not exist. **Evidence:** a repo with `git mv a/b/d.txt a/c/d.txt` gives `1	0	a/{b => c}/d.txt`, and `_split_numstat` returns `(1, 0, 'c}/d.txt')`.
**Test:** one for each row, in the table's words; C2 with a rename, a rename in the root (`{a => b}/x`), and a plain `old => new`.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries, so nothing here can be written to pass them: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Small; free models, any profile. The judge checks C2 with a real `git mv` repo.

## Not in scope

Parts A, B, D are HARDEN-1 (298), HARDEN-2 (299), HARDEN-4 (301).
