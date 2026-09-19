# KC-15 — a bash redirect outside the worktree is never asked: `ask_commands` turns it into a permission, layer 1 keeps the harmless ones free

**Status:** queued — after KC-13 (round 52); round 54 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-19 (`contest-bench/kc6/RUNBOOK.md` §11). **After KC-13** (round 52): both edit `tools/contest/policy.py`, and the scan KC-13 adds is what makes the asked command judgeable. Independent of KC-12 and KC-14.
**Severity:** HIGH
**File:** `tools/contest/roster.py` (`ContestConfig.session_rules`, `CONTEST_KEYS`, `load_roster`), `contest.ini`, `tools/contest/policy.py` (`Policy._mechanical`)
**Symbol:** `ContestConfig.ask_commands`, `ContestConfig.session_rules`, `Policy._mechanical`
**Round:** 54
**Size:** S
**Source:** live round 2 of `contest-bench/kc6/live_smoke.py --models …` (ticket 02 tells the model to write one line to `/tmp/contest/notes/<name>.txt` and one to `/tmp/kc6-outside-<name>.txt`). Four of five models ran the second write as its own bash call — `echo "…" > /tmp/kc6-outside-agnes-2-0-flash.txt` — and **no `permission.asked` was raised at all**: `decisions.jsonl` has one line per agent (the `mkdir -p /tmp/contest/notes && …` that asked `external_directory` for the `mkdir` argument), and `/tmp/kc6-outside-*.txt` exists for agnes-2-0-flash, agnes-2-5-flash, glm-4-7-flash, mimo-v2-5 and step-3-7-flash (the last one never asked anything: the notes folder already existed, so not even the first write had a directory argument). Kilo's `external_directory` looks at command *arguments* of the commands it knows (`mkdir`, `rm`, `ls`, `cd`, …); a redirect target, `tee`, `cp`/`mv` destinations are not arguments it parses. The EPIC's line "everything else that leaves the worktree arrives as `external_directory`" (§3, The session) is false for a redirect — and a redirect is the simplest way to write a file.
**Depends on:** KC-3 (`policy.py`, landed `1499cd8`), KC-2 (`roster.py`), KC-13 (round 52 — `_extract_paths` scans `metadata.command`).
**Also touches:** `tests/test_contest_roster.py`, `tests/test_contest_policy.py`, `docs/kilo-contest/EPIC-KC.md` (§3 one sentence, §4 the layer table's row 0/1)

---

## What happens today

`session_rules()` sends `* allow`, `external_directory ask`, `doom_loop ask`
and one `bash deny` per `deny_commands`. `bash` itself is `allow`, so a
command Kilo does not recognise as touching an outside directory runs
without any event. A `bash: *` ask was rejected by the probe because layer 1
would then send every `pytest`/`git` to the gate: `_inside_worktree_or_tmp`
returns `False` for an event with **no** paths, so a bash ask that names no
path at all falls through to layer 2 — one gate call per command.

## What must change

1. **`ask_commands`** — a new `[contest]` key, comma-separated bash globs in
   the same shape as `deny_commands`, default in `contest.ini`:
   `*>*, *|*tee *, cp *, mv *, ln *, rsync *, install *, dd *` (the ways a
   shell writes a file somewhere a command argument does not show; the
   comment above the key says so). `CONTEST_KEYS` grows by one;
   `ContestConfig.ask_commands: tuple[str, ...] = ()`; `load_roster` reads
   it with the same `list_()` helper as `deny_commands`.
2. **`session_rules()`** returns: the three `BASE_RULES`, then one
   `{"permission": "bash", "pattern": p, "action": "ask"}` per `ask_commands`
   entry in order, then the `deny` rules **last** (a command matching both
   is denied — Kilo's last match wins, which is what the probe relied on
   for `*` allow followed by `external_directory` ask).
3. **Layer 1 for a `bash` ask** (`Policy._mechanical`), after the forbidden
   check and before the `deny_commands` check: when
   `permission == "bash"` and `_extract_paths(props)` (with KC-13's command
   scan) yields **no path outside the worktree/tmp_roots** — including the
   case of *no path at all* (`pytest -q`, `git status`, `2>&1`, a relative
   redirect `> out.txt`) — the decision is `once`/`mechanical` with reason
   `bash: no path outside worktree/tmp_roots`. `external_directory` keeps
   today's rule (an event with no paths still goes to the gate — it always
   carries one). A `bash` ask with an outside path still goes to the gate;
   with a forbidden path it is still a mechanical `reject`; a
   `deny_commands` match is still checked first among the bash rules —
   order: doom_loop → forbidden → inside → deny_commands → the new no-path
   rule → gate.
4. `docs/kilo-contest/EPIC-KC.md`: §3's rule line gains `bash ask for each
   ask_commands pattern`, the sentence about "everything else … arrives as
   `external_directory`" is corrected in one sentence naming the redirect;
   layer 1's row in the §4 table gains the no-path `bash` rule.

## Acceptance

- [ ] `tests/test_contest_roster.py`:
      - `ContestConfig(ask_commands=("*>*", "cp *"), deny_commands=("git push*",)).session_rules()`
        is `PROBE_RULES + [bash ask "*>*", bash ask "cp *", bash deny "git push*"]`
        — asks before denies, both in file order;
      - `load_roster` of the committed `contest.ini` reads the default
        `ask_commands` list above (exact tuple);
      - an ini without the key → `ask_commands == ()` and `session_rules()`
        unchanged from today (the three existing session-rule tests stay
        green as written);
      - `ask_commands` is in `CONTEST_KEYS` (the unknown-key error names it).
- [ ] `tests/test_contest_policy.py`:
      - the live event: `permission: "bash"`, `patterns: ["echo x > /tmp/kc6-outside-a.txt"]`,
        `metadata.command` the same, `tmp_roots = ("/tmp/contest/*",)`,
        a stub gate → the decision is **not** `once`/`mechanical` (gate or
        reject, the gate's message names `/tmp/kc6-outside-a.txt`);
      - `bash` with `command: "python3 -m pytest tests -q 2>&1 | tail -n 20"`
        → `once`/`mechanical`, reason contains `no path outside`, the gate
        is **not** called;
      - `bash` with `command: "echo x > out.txt"` (relative) → `once`/`mechanical`;
      - `bash` with `command: "cp README.md /tmp/contest/notes/a.md"` → `once`
        (tmp_roots), and with `/tmp/elsewhere/a.md` → gate;
      - `bash` with `command: "cat x > ~/.ssh/authorized_keys"` → `reject`/`mechanical`
        with `forbidden:`;
      - `bash` with `command: "git push origin HEAD"` and `deny_commands=("git push*",)`
        → `reject`/`mechanical` with `deny_commands match` (the deny rule
        still beats the no-path rule);
      - `external_directory` with no paths still goes to the gate (existing
        behaviour, one test).
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- `bash: *` ask (every command a round trip): the probe's objection stands
  for the gate cost; with rule 3 it would be *mechanically* cheap, and an
  operator can set `ask_commands = *` to get it — but the default stays
  the write-shaped list.
- Catching a write done from inside `python3 -c` or a script: not a shell
  problem; the round's worktree is the sandbox boundary for files it can
  see, the gate is the boundary for the rest.
- What Kilo parses as a directory argument.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5/KC-6 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every changed module
      imports there: `python3 -c "import tools.contest.roster, tools.contest.policy"`
      from the repo root. No backslash and no nested same-quote inside an
      f-string expression (a 3.12-only `f"{x.split("\t")}"` is a
      `SyntaxError` here and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no other KC
      ticket, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — `session_rules(self) -> list[dict]`,
      `ContestConfig` frozen with the new field defaulting to `()`,
      `Policy.decide`/`PolicyContext`/`Decision` unchanged; read
      `tools/contest/roster.py` (`BASE_RULES`, `list_`) and
      `tools/contest/policy.py` (`_mechanical`, `_inside_worktree_or_tmp`,
      `_deny_match`) before adding to them.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change: check the test files alone
      out onto the base, run them, see them fail; restore.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the **sha** of
      the one commit (the script stores `--outcome DONE` as `FIXED`; that
      is fine).
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
