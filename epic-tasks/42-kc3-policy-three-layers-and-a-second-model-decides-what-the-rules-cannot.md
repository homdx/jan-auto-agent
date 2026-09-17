# KC-3 — `tools/contest/policy.py`: every permission is decided — by the rules, by geometry, or by a second model; never by silence

**Status:** queued — after KC-2 (round 41). Written against `67e834d`.  
**Severity:** CRITICAL (this is the thing that lets N agents run unattended)  
**File:** `tools/contest/policy.py` (new)  
**Symbol:** `Policy`, `Decision`, `PolicyContext`, `decide`, `_mechanical`, `_ask_gate`, `GATE_SYSTEM_PROMPT`  
**Round:** 42  
**Size:** M  
**Source:** `docs/kilo-contest/PROBE.md` §"The permission payloads" and §"Facts". Observed: `external_directory` arrives with `patterns` (`["/tmp/*"]`), `metadata.directories` (`["/tmp"]`) **and** `metadata.command` (`"rm -v /tmp/testfile"`); `reject` blocks the call and the model reads our message as the tool error; one model (mistral) retried the same command with `"workdir": "/tmp"` and raised a second event; `always` would whitelist the pattern for the session. The operator's requirement: whether a command outside the worktree may run is **validated by an LLM — a different model than the agent's** — with mechanical rules only for the cases that need no judgement.  
**Depends on:** KC-2 (`ContestConfig`, `gate_settings`, `tmp_roots`, `deny_commands`).  
**Also touches:** `tests/test_contest_policy.py` (new), `tests/_kilo_fake.py` (nothing — the policy is pure; the LLM call is stubbed at `request_completion`)

---

## What happens today

The probe answered every permission with a flag (`--reply reject|once`).
Nothing looks at what was asked. In the contest an agent may legitimately
need something outside its worktree (a scratch dir under `/tmp`, reading
a sibling clone's file for reference) and may also try things it must
not (write into another agent's worktree, touch `~/.ssh`, delete the
repo's hooks). A fixed reject makes the first class fail the ticket; a
fixed allow makes the second class possible. The rules can settle most
events by geometry alone; the rest need a reader.

## What must change

1. **`PolicyContext`** — frozen: `worktree: Path` (resolved), `tmp_roots: tuple[str, ...]`,
   `forbidden: tuple[Path, ...]` (the round's other worktrees, the repo
   root's `.git`, and the hard denylist: `/`, `Path.home()` itself,
   `~/.ssh`, `~/.config`, `~/.local/share/kilo`), `ticket_title: str`,
   `ticket_files: tuple[str, ...]`, `recent_tools: tuple[dict, ...]`
   (the last five `tool` parts of the session, from KC-1's `tool_parts`),
   `gate_budget_left: int`.

2. **`Decision`** — frozen: `reply: Literal["once", "reject"]`,
   `layer: Literal["mechanical", "gate", "gate-failed", "budget"]`,
   `reason: str` (one line, ≤ 200 chars — it is sent to the model and to
   `decisions.jsonl`), `gate_elapsed: float | None`, `gate_raw: str | None`.

3. **`Policy(config: ContestConfig, *, completion_fn=None, clock=None)`**
   with **`decide(event: dict, ctx: PolicyContext) -> Decision`**:

   - **Layer 1, mechanical.** Extract every path from
     `properties.patterns`, `properties.metadata.directories`, and
     `properties.metadata.patterns`; strip a trailing `/*`; `Path(...).resolve()`
     (symlinks resolved — a link out of the worktree is judged by its
     target). Then, in this order:
     - `properties.permission == "doom_loop"` → `reject`, "doom loop";
     - any path equal to or under a `forbidden` entry → `reject`, names it;
     - every path inside `worktree` or matching a `tmp_roots` glob
       (`fnmatch` on the resolved string, and on the original pattern) →
       `once`, "inside worktree/tmp_roots";
     - `properties.permission == "bash"` (only present if an operator set
       `bash: ask` in a local rules override) with a command matching
       `deny_commands` → `reject`, names the pattern;
     - otherwise → layer 2.
   - **Layer 2, the gate.** If `ctx.gate_budget_left <= 0` → `reject`,
     layer `budget`, "gate budget exhausted (N)". Else one call to the
     gate model through `completion_fn` (default:
     `tools.llm_stream.request_completion` with the URL/headers/payload
     built from `config.gate_settings` exactly as Gate 1's presence check
     builds them — `response_format` when supported, `temperature` from
     settings, `max_tokens` from settings, `stream=False`, `timeout` 60 s,
     `error_retries=0`). The prompt is `GATE_SYSTEM_PROMPT` + one user
     message containing, as labelled lines: the permission name, the
     patterns, the directories, `metadata.command`,
     `metadata.description`, the worktree path, `tmp_roots`, the ticket
     title and files, and the recent tools as `tool: input → status`.
     Expected reply: `{"verdict": "allow"|"reject", "reason": "<one line>"}`.
     Parse with the same tolerant JSON extraction Gate 1 uses (first `{…}`
     object in the text, `strip_think` first). `allow` → `once`, layer
     `gate`; `reject` → `reject`, layer `gate`; anything else (no JSON,
     unknown verdict, empty) → `reject`, layer `gate-failed`,
     "gate unavailable: <what came back, 80 chars>". An exception from
     `completion_fn` → the same, with the exception class.
   - **Never `always`.** The type forbids it; a test asserts the string
     does not occur in the module outside the docstring.

4. **`GATE_SYSTEM_PROMPT`** — a module constant, ≤ 40 lines, stating the
   job (approve or refuse one tool call from a coding agent that must
   stay inside its worktree), the only two legitimate reasons to allow
   outside it (a scratch path under `tmp_roots`; a read-only look at a
   path the ticket names), the always-refuse list (writes to another
   worktree, anything under `$HOME` dotfiles, package installs, network
   fetches piped to a shell, `git push`, destructive commands on paths
   outside the worktree), and the reply format. It ends with: *"When
   unsure, reject — a rejection costs the agent one retry; an allow can
   cost the machine."*

5. **`Policy.record(decision, event, path)`** — appends one JSON line to
   `decisions.jsonl`: `t`, `sessionID`, `permission id`, `permission`,
   `patterns`, `command`, `layer`, `reply`, `reason`, `gate_elapsed`,
   `gate_model`. The runner (KC-6) calls it; the policy does not know
   about the runner.

## Acceptance

- [ ] `tests/test_contest_policy.py`, table-driven on the **recorded
      payloads** (copied from PROBE.md verbatim): the `external_directory`
      event for `/tmp/*` with `tmp_roots = /tmp/kilo/*` → layer `gate`;
      with `tmp_roots = /tmp/*` → `once`, mechanical; a pattern inside the
      worktree → `once`, mechanical, no gate call; a pattern under another
      round worktree → `reject`, mechanical; a symlink inside the worktree
      pointing to `~/.ssh` → `reject`; `doom_loop` → `reject`; the `bash`
      event with `rm -v /tmp/testfile` and `deny_commands = rm -v /tmp/*`
      → `reject`, names the pattern.
- [ ] Gate layer with a stubbed `completion_fn`: returns `allow` JSON →
      `once`/`gate`; `reject` JSON → `reject`/`gate`; `<think>…</think>{"verdict":"allow",…}`
      → `once` (strip_think honoured); prose without JSON → `reject`/`gate-failed`;
      `{"verdict": "maybe"}` → `reject`/`gate-failed`; raises `TimeoutError` →
      `reject`/`gate-failed`; `gate_budget_left = 0` → `reject`/`budget`
      and `completion_fn` **not called**.
- [ ] The user message handed to the stub contains the command, the
      worktree, the ticket title and one recent tool line; the payload
      handed to the default `completion_fn` (assert via monkeypatched
      `request_completion`) carries `temperature`, `max_tokens` and
      `response_format` from `gate_settings`.
- [ ] `grep -c '"always"' tools/contest/policy.py` counts only the
      docstring mention (assert in a test).
- [ ] `record()` writes one line per decision with the listed keys.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Questions (`question.v2.asked`) — rejected and counted by the runner.
- Learning from decisions (`always` rules, caches) — every event is
  judged afresh; the budget is the only state.
- Parsing shell syntax on our side beyond `fnmatch` against
  `deny_commands` — the server rules already deny those patterns before
  we see them; the gate model reads the rest.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test calls a live provider; `completion_fn` is always stubbed.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
