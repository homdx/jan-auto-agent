# KC-44 — The ticket's own `Size` field decides how many legs it gets, and an `L` ticket is refused in one

> ## 🔎 Ticket audit — 2026-09-27 (after the KC-10 patch series, branch head `b5257cf`)
> **Verdict: still needed, 0% implemented, description fully accurate — no corrections required.**
>
> | Checked against real code | Found |
> |---|---|
> | `ticket_size`, `ContestConfig.legs_by_size` | **Neither exists anywhere** in `cli.py` or `contest.ini` (repo-wide grep, zero hits). |
> | Any `KC-44` code/tests | **Zero hits** outside `epic-tasks/`. |
> | **`KC-43` dependency** (its only one) | **Accurate, no change.** KC-43 is confirmed still `queued` (`INDEX.md` row 82, same batch, audited above) — `--legs` and the whole relay mechanism it configures genuinely do not exist yet. |
> | Chain position | This is correctly the **last** ticket in the `KC-36→…→KC-44` chain — every other ticket in this batch (KC-42, KC-40, KC-43) still blocks it transitively through KC-43. |
>
> **Net effect:** nothing to fix. This ticket's header, "What happens today," "What must change," and "Acceptance" sections all match the current repo state exactly. Reproduced below unchanged, with this audit note as the only addition, so the four-ticket batch is delivered as a consistent set.

**Status:** landed `f9bd680` (2026-09-29) — round 83, winner agnes-3-0-flash 30/30 on `contest-bench/kc44/acceptance_kc44.py`, taken as-is. Was: open — after KC-43, which it configures.
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
