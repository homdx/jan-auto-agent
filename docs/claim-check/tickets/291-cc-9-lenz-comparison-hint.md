# CC-9 — a Lenz comparison hint: print the commands, never run them

**Status:** draft
**Severity:** LOW (a convenience; nothing in the epic depends on it)
**File:** `tools/claimcheck/lenz_hint.py`
**Symbol:** `pick_for_lenz`, `lenz_hint_markdown`
**Round:** —
**Size:** S
**Depends on:** CC-6 (`votes.json`), CC-8 (the pending list and `run_claim_check.sh`)
**Also touches:** `scripts/run_claim_check.sh`, `scripts/claimcheck_truth.py`, `docs/claim-check/RUNBOOK.md`, `tests/test_claimcheck_lenz_hint.py`

---

## Why

The epic's own judge is free models with the code in front of them. Whether it is *as good
as* a paid fact-checker is a question we can only answer by putting the two side by side on
the same claims, and Lenz is the one we know. But Lenz costs: `/extract` is free (1000 a day);
`/assess` is **1 credit per claim** and `/verify` **10 credits**, from one pool of **100 a
month + 400 extra** (`docs/claim-check/REPORT.md`, measured against the billing page). So the
comparison must be something the operator *chooses* to spend, never something a run spends on
its own.

EPIC-CC §3 rule 7 stays: **no run of this epic calls `/assess` or `/verify`.** This ticket
adds the other half of that rule: after a run, the operator is *told* how to spend credits on
purpose, with the commands ready and the cost stated.

## What it does

After `run_claim_check.sh` / `claimcheck_truth.py` finish, a section is appended to the pending
list (`pending.md`) and printed on the console, **text only**:

```
## If you want Lenz's opinion (costs credits — nothing below was run)

Lenz cannot see this repository, so only claims about the world are worth sending.
Worth comparing (public facts the voters split on or were unsure about): 7 claims.
Cost if you send them: /assess = 7 credits (of 100 a month + 400 extra; this month: unknown
to this tool — check the billing page). /verify on the single most doubtful row = 10 more.

  1. Look at what would be sent, no network, no credit:
       python3 scripts/lenz_claim_filter.py <report> --dry-run
  2. Send them (asks for LENZ_API_KEY on the console; cached on disk, a repeat costs nothing):
       python3 scripts/lenz_claim_filter.py <report> --json lenz.json --max 7
  3. Compare with our verdicts:
       python3 scripts/claimcheck_truth.py <votes.json> --lenz lenz.json
```

* `pick_for_lenz(claims, *, limit=12)` returns the claims worth a credit: `kind == "world"`
  and the verdict `SPLIT` or `UNSURE` (the voters did not settle it), ordered `SPLIT` first,
  at most `limit` (the script's own cap, `DEFAULT_MAX`). A code, mixed or dangling claim is
  **never** picked: Lenz cannot know it.
* `lenz_hint_markdown(picked, report_path) -> str` renders the block above. The credit figures
  come from constants in `scripts/lenz_claim_filter.py` (`VERIFY_CREDITS`, one credit per
  claim); the monthly balance is **not** guessed (no API for it is used): the block says to
  look at the billing page.
* Nothing in this ticket imports `urllib`, reads `LENZ_API_KEY`, or calls
  `scripts/lenz_claim_filter.py`. A test pins that (see below).
* `--lenz FILE` on `claimcheck_truth.py` is the only new reading: it takes the JSON that
  `lenz_claim_filter.py --json` wrote and adds a column `lenz` (`True/high`, `Mixed/low`, …) next
  to our verdict in the compare table. Absent file: the column is not there; the command
  works as before.
* `run_claim_check.sh --no-lenz-hint` leaves the block out.

## Tests

`tests/test_claimcheck_lenz_hint.py` (no network, `socket` blocked for the module):

| Test | What it pins |
|---|---|
| `test_only_world_claims_the_voters_did_not_settle_are_picked` | code/mixed/dangling never; `TRUE`/`FALSE` unanimous never; `SPLIT` before `UNSURE` |
| `test_limit_is_respected` | 30 candidates, `limit=12` gives 12 |
| `test_hint_states_the_cost_and_runs_nothing` | the text has the three commands, the credit numbers, and the words "nothing below was run" |
| `test_module_has_no_network_and_no_key_access` | no `urllib`, `socket`, `requests`, `LENZ_API_KEY`, `subprocess` in the module's source |
| `test_run_claim_check_prints_the_hint_and_makes_no_call` | the script run with a fake voter and `LENZ_API_KEY` unset: the hint is in the output, no process named `lenz_claim_filter` was started |
| `test_lenz_column_is_read_from_the_json` | `--lenz` adds the column; a missing file leaves the table unchanged |
| `test_no_hint_flag` | `--no-lenz-hint` drops the block |

## Acceptance (the operator)

Run the pipeline on a report, read the block, copy command 1 (the dry run): it lists the same
claims the block counted. Credits on the billing page before and after the whole run: unchanged.

## Edge cases to handle

No claim qualifies (the block says "nothing worth sending to Lenz" and prints no commands); a
report where every world claim was settled unanimously; a `lenz.json` with `[]` (the script writes
that when nothing is left to send); a `lenz.json` row for a claim that is not in `votes.json`
(ignored, counted in a warning).

## Not in scope

Calling Lenz from the pipeline for any reason; reading the credit balance; `/verify` automation;
sending code claims; changing `lenz_claim_filter.py`'s own filtering.
