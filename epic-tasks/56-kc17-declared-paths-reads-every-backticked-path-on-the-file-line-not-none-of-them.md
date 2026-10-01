# KC-17 — `gates._declared_paths`: the `**File:**` line is read like `**Also touches:**` — every backticked path — instead of not at all

**Status:** landed `6d038f9` — by hand, no contest, on 2026-09-20: round 53's rework prompts carried `touched 1 file(s) outside the ticket's declared list: tools/contest/harvest.py` for KC-14's own file, and the judge had to be right before round 53 could be scored (with KC-26). Round 56 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found on 2026-09-19 while writing KC-16. Independent of KC-12, KC-13, KC-14 and KC-16 (different file); can run in parallel with any of them.
**Severity:** MEDIUM
**File:** `tools/contest/gates.py` (`_declared_paths`, `declared_files`)
**Symbol:** `_declared_paths`, `declared_files`
**Round:** 56
**Size:** S
**Source:** `python3 -c "from tools.contest.gates import declared_files as d; print(d('epic-tasks/45-kc6-runner-prompt-wait-harvest-rework-in-the-same-session-for-n-agents.md'))"` prints `('tests/test_contest_runner.py', 'tests/_kilo_fake.py')` — `tools/contest/runner.py`, the ticket's one **File**, is missing. The same for every KC ticket: `40` lacks `kilo_client.py`, `44` lacks `gates.py` and `harvest.py`, `53` lacks `harvest.py`, `55` lacks `cli.py`, `__main__.py` and `runner.py`. The `**File:**` regex wants the whole rest of the line inside one optional pair of backticks (`` ^\*\*File:\*\*\s*`?([^`\n]+?)`?\s*$ ``), and every epic ticket writes `` `path` (new) `` or `` `a.py` (`sym`), `b.py` `` — so it matches nothing and the primary file is never declared. Downstream, `judge_worktree` then counts the ticket's own file as `off_ticket`, and every rework prompt on this repo would carry `Also noted: touched 1 file(s) outside the ticket's declared list: tools/contest/runner.py` — wrong, and read by a model that is told to stay on the ticket. Non-blocking (`off_ticket_files` is the one non-blocker), so no verdict changed; the sandbox tickets of the live runs (`contest-bench/kc6/RUNBOOK.md` §10–§11) have a bare `` **File:** `pkg/thing.py` `` and never showed it.
**Depends on:** KC-5 (`tools/contest/gates.py`, landed `0d91dd6`).
**Also touches:** `tests/test_contest_harvest.py`

---

## What happens today

`_declared_paths(body)` takes the `**File:**` line through a regex that
only matches a line that is exactly one path, optionally backticked, with
nothing after it. `**Also touches:**` is read the right way — every
`` `…` `` on the line. `scripts/next_task.py::_field` has the same shape
for its own display and is not this ticket (it prints; it does not judge).

## What must change

1. The `**File:**` line is read exactly like `**Also touches:**`: every
   backticked span on the line is a declared path, in order, `**File:**`
   paths first. A line with **no** backticks keeps today's behaviour: the
   whole value, stripped, is the one path (a `—` is dropped, as now).
2. A backticked span that is not a path — `` (`_is_ancestor`, `harvest`) ``
   on KC-14's line, `(new)` is not backticked — must not become one:
   keep a span only when it contains a `/` or ends in `.py`, `.md`,
   `.ini`, `.sh`, `.json`, `.txt`, `.gitignore` or a trailing `/`; drop the
   rest. Apply the same filter to `**Also touches:**` (today KC-9's line
   yields `tool_parts`, `messages`, `"assistant": ""`, and KC-10's
   `compact_at_percent = 80` — they are harmless as declared paths but
   they are not paths). `declared_files` is unchanged in signature and
   return type.
3. `scripts/judge_epic_round.py` stays byte-for-byte. It *does* call
   `ticket_for_round` → `_declared_paths` (its `declared files:` line and the
   `off_ticket` column simply become right), and its golden test
   (`test_contest_harvest.py`, against `tests/fixtures/judge_epic_round_pre_kc5.py`)
   uses a bare `` **File:** `tools/auto/probe.py` `` ticket — the one shape
   both versions parse the same — so it stays green without being edited.

## Acceptance

- [ ] `tests/test_contest_harvest.py`:
      - `declared_files` on a temp ticket whose lines are KC-6's verbatim
        (`` **File:** `tools/contest/runner.py` (new) `` and
        `` **Also touches:** `tests/test_contest_runner.py` (new), `tests/_kilo_fake.py` ``)
        → `('tools/contest/runner.py', 'tests/test_contest_runner.py', 'tests/_kilo_fake.py')`;
      - KC-14's `**File:**` line (`` `tools/contest/harvest.py` (`_is_ancestor`, `harvest`) ``)
        → `harvest.py` only, not the symbols;
      - KC-5's three-path `**File:**` line → the three paths in order;
      - a bare `**File:** tools/x.py` (no backticks) → `('tools/x.py',)`;
        `**File:** —` → `()`;
      - the live case through `harvest`: a worktree whose one commit
        changes the ticket's `**File:**` path and adds a test → **no**
        `off_ticket_files` reason; the same commit also touching a file the
        ticket does not name → `off_ticket_files` names that file only.
- [ ] Every existing `tests/test_contest_harvest.py` test unmodified and
      green; `tests/test_contest_runner.py` untouched.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- `scripts/next_task.py::_field` — display only.
- Making `off_ticket_files` blocking — KC-5 chose non-blocking on purpose.
- `scripts/judge_epic_round.py` (`--csv` golden test).

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.gates"` from the repo root. No
      backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/gates.py`
      and `tests/test_contest_harvest.py` (plus `.smoke_tests/` links).
      Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
