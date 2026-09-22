# KC-44 — The ticket's own `Size` field decides how many legs it gets, and an `L` ticket is refused in one

**Status:** queued — after KC-43, which it configures. Asked by the operator on 2026-09-22.
**Severity:** LOW (a convenience on top of KC-43 — but the one that decides whether KC-43 is ever used, since nobody remembers to pass `--legs` by hand)
**File:** `tools/contest/cli.py` (`cmd_run`, ticket parsing), `contest.ini`
**Symbol:** `ticket_size` (new), `cmd_run`, `ContestConfig.legs_by_size` (new)
**Round:** 83
**Size:** S
**Source:** every ticket in `epic-tasks/` already carries a `**Size:**` line — `XS`, `S`, `M`, `L` — and nothing reads it. It is documentation for the operator and nothing else. KC-43 introduces a leg count that has to come from somewhere; this is the somewhere, and it costs one parser and one mapping.
**Depends on:** KC-43 (queued — `--legs`, the whole relay).
**Also touches:** `tests/test_contest_cli.py`, `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`

---

## What happens today

`**Size:**` is parsed by nothing. `run --ticket NN` gives every ticket one
turn regardless of whether its header says `XS` or `L`, and (after KC-43)
`--legs` would have to be remembered and typed correctly every time.

## What must change

1. **`ticket_size(ticket_path) -> str | None`** — read the `**Size:**` line
   from the ticket's header, normalised to upper case, tolerant of the
   parenthetical notes some tickets carry (`S (measurement only)`,
   `M`, and KC-40's `L` with its separate `**Size note:**` line). No line,
   or an unrecognised value → `None`, and `None` behaves as today.
2. **`legs_by_size` in `contest.ini`**, default `XS=1, S=1, M=1, L=3`. The
   default deliberately changes nothing for the sizes the epic has actually
   been running: only `L` gets a relay.
3. **`--legs` still wins** when passed explicitly, including `--legs 1` on
   an `L` ticket.
4. **An `L` ticket in one leg is refused** unless `--legs 1` says so out
   loud — the refusal names the size, the leg count it would have used, and
   the flag that overrides it. This is the whole behavioural content of the
   ticket: the size field stops being decoration and becomes a statement
   the runner acts on.
5. The chosen leg count and where it came from (flag or size) go into
   `state.json` and the round's first log line.

## Acceptance

- [ ] `tests/test_contest_cli.py`: a ticket whose header says `Size: L`
      runs three legs with no flag; `Size: S` runs one; `--legs 2`
      overrides both; `--legs 1` on an `L` ticket runs one leg without a
      refusal.
- [ ] An `L` ticket with `legs_by_size` unset or `L=1` and no flag → the
      refusal fires, names the size and the override, and exits non-zero
      without preparing a single worktree.
- [ ] `Size: S (measurement only)` parses as `S`; a missing `**Size:**`
      line parses as `None` and runs one leg.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Changing any ticket's `Size` value, or backfilling the field where it is
  missing.
- Inferring size from the ticket's content rather than its header.
- Any other use of the header's fields (`Severity`, `Round`, `Depends on`).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
