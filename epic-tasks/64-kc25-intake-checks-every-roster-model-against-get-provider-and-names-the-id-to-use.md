# KC-25 — Intake checks every roster `provider/model` against `GET /provider` and names the id to use; `--provider` sets the default for bare ids

**Status:** open — found 2026-09-20 when the operator asked to run the Sensenova models (the hand-round leader: KC-1, KC-5, KC-12, KC-16 winners) through `run --ticket NN`. They already can — `--models sensenova123/sensenova-6.8-flash-lite,hy3:free` mixes providers today (`agents_from_models` splits on the last `/`) — but nothing checks the ids before the round: `sensenova` is the provider's **display name** in `kilo.jsonc`, its id is `sensenova123`, exactly the `kenari`/`kenary` trap that cost a round earlier; a mistyped model (`agnes-3-0`, `nex-n2-5-pro`, `north-mini-code`, `laguna-xs` in round 52) only fails at the first turn, minutes in, as `session.error` on one agent. Independent of KC-14/15/17 (different functions); can run beside any of them. Confirmed live 2026-09-20, round 58 (`--models agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,hy3:free,agnes-3-0-flash:free,muse-spark-1-3,north-mini-code:free,nex-n2-5-pro:free --max-parallel 8`): intake passed, eight sessions opened, and four were `ERROR` at t+17 s — `session.error` `Model not found: kenary/agnes-3-0-flash:free. Did you mean: agnes-2-0-flash:free, agnes-2-5-flash:free, glm-4-7-flash:free?` (same for `north-mini-code:free` and `nex-n2-5-pro:free`), `muse-spark-1-3` closed its turn with `reason=error`. `contest-out/58/<agent>/events.jsonl`. Half the round's slots were spent before the first tool call; the `Did you mean` list is the same answer `GET /provider` gives up front.
**Severity:** MEDIUM
**File:** `tools/contest/cli.py` (`intake`, `agents_from_models`, `_parser`)
**Symbol:** `intake`, `agents_from_models`, `KiloClient.providers` (new), `roster_on_offer` (new)
**Round:** 64
**Size:** S
**Source:** live `GET /provider` on Kilo 7.6.2, 2026-09-20: `{"all": [provider…], "default": {providerID: modelID}, "connected": [providerID…], "failed": […]}`; each provider `{"id", "name", "source", "models": {modelID: {"id", "providerID", "name", "status", "capabilities": {"reasoning": bool, "toolcall": bool, …}, "variants": {…}, …}}}` — `name` is the display string from `kilo.jsonc` (`"kenary": {"name": "kenari"}`, `"sensenova123": {"name": "sensenova"}`), `id` is what `POST /session`'s `model.providerID` wants. `connected` had 19 ids, `failed` was empty. `KiloServer.spawn` is ready in < 1 s (`docs/kilo-contest/PROBE.md` row 1), so intake can afford to ask.
**Depends on:** KC-16 (`cli.py run`, landed `1304950`); KC-1 (`KiloClient`).
**Also touches:** `tools/contest/kilo_client.py` (`providers()`), `tests/_kilo_fake.py` (`GET /provider`), `tests/test_contest_cli.py`, `tests/test_contest_kilo_client.py`

---

## What happens today

`intake` checks the ticket, the base and that the server *can* be reached
(`find_kilo_binary` for `spawn`, `KiloServer.attach` for a URL). The
roster's `provider_id`/`model_id` pairs are never looked at: `POST /session`
accepts any ids, and the mismatch surfaces as the first turn's
`session.error` (`ProviderModelNotFoundError`, "Model not found") — one
agent of the round gone, the others already running, the operator reading
`events.jsonl` to learn which word was wrong. `agents_from_models` hard-codes
`kenary` as the provider for a bare id, so a Sensenova-only round spells the
provider four times.

## What must change

1. **`KiloClient.providers() -> dict`** — `GET /provider`, the decoded body
   as is (`_check`ed; a non-dict body is a `ValueError` like
   `create_session`'s). Nothing is reshaped here; the fake gets a
   `GET /provider` route answering the scenario's `providers` (default: one
   provider `kenary` named `kenari` with `hy3:free`, `connected: ["kenary"]`).
2. **`roster_on_offer(providers: dict, agents) -> list[str]`** in `cli.py` —
   pure, one failure line per agent that is not on offer, in roster order:
   - provider id unknown, but some provider's `name` equals it
     (case-insensitive):
     `[<agent>] <provider>/<model>: no provider '<provider>' — that is the display name of provider '<id>'; use <id>/<model>`;
   - provider id unknown, no such name:
     `[<agent>] <provider>/<model>: no provider '<provider>' — connected: <ids, sorted, comma-separated>`;
   - provider known but not in `connected`:
     `[<agent>] <provider>/<model>: provider '<provider>' has no credentials (not connected)`;
   - model unknown under a known provider:
     `[<agent>] <provider>/<model>: no model '<model>' under '<provider>' — on offer: <model ids, sorted>`,
     plus ` (did you mean <m>?)` when `difflib.get_close_matches(model, ids, n=1)` finds one.
   A model whose `status` is not `active` is *not* a failure (the field's
   other values are unverified) — nothing is inferred from `capabilities`.
3. **Intake asks.** After the existing server check passes: with
   `server = spawn`, intake spawns a throwaway server
   (`with KiloServer.spawn(find_kilo_binary(config.kilo_bin), log_path=<tempfile>)`,
   the log file removed after) and reads `KiloClient(server, str(repo)).providers()`;
   with a URL, the attached server. `roster_on_offer(...)` lines join
   `failures`. A `KiloHttpError`/`KiloServerError`/`ValueError` from the
   call is one failure line (`GET /provider failed: …`) — the check never
   crashes intake. `cmd_run`'s own `_start_server` afterwards is unchanged
   (the second spawn is the price of leaving `cmd_run`'s order alone).
4. **`--provider ID`** on `run` (default `kenary`, help text names the
   `provider/model` spelling): the default for bare ids in
   `agents_from_models(models, provider=args.provider)`. `--roster` agents
   are unaffected.
5. `_print_plan` shows the roster as `provider/model` per agent (it already
   prints the names; add the pair) so the operator sees what intake
   checked.

## Acceptance

- [ ] `tests/test_contest_kilo_client.py`: `providers()` returns the fake's
      dict; a 500 is a `KiloHttpError`.
- [ ] `tests/test_contest_cli.py`, `roster_on_offer` alone (no server):
      the four failure shapes above with the exact wording, the
      display-name hint for `kenari/hy3:free` → `use kenary/hy3:free`,
      the `did you mean` for `hy3` vs `hy3:free`, an empty list for a
      roster entirely on offer, one line per bad agent in roster order.
- [ ] `run --ticket NN --models kenari/hy3:free` in the sandbox (fake
      server via the roster's `server = <url>`) → `intake: [hy3] kenari/hy3:free: no provider 'kenari' — that is the display name of provider 'kenary'; use kenary/hy3:free`,
      exit `EXIT_FAILED`, no worktree created; the same roster with
      `kenary/hy3:free` passes intake.
- [ ] `--provider sensenova123 --models sensenova-6.8-flash-lite` →
      `AgentSpec(provider_id="sensenova123", model_id="sensenova-6.8-flash-lite", name="sensenova-6-8-flash-lite")`;
      an id with its own `x/` prefix still wins over `--provider`.
- [ ] `GET /provider` answering 500 → one `intake: GET /provider failed: …`
      line, exit `EXIT_FAILED`.
- [ ] Every existing intake test unmodified and green (the default fake
      answers `/provider` with the roster those tests use — adjust the
      fake's default, not the tests).
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Probing that the model *answers* (that is KC-11, which reads the same
  `providers()` dict for `capabilities.reasoning` and `variants`).
- Reading `kilo.jsonc` ourselves — the server's view is the only one that
  matters.
- Moving `_start_server` ahead of intake to save the second spawn.
- Rate limits and `429 Server is busy` on the Sensenova endpoint — KC-19.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.cli, tools.contest.kilo_client"`
      from the repo root. No backslash and no nested same-quote inside an
      f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/cli.py`,
      `tools/contest/kilo_client.py`, `tests/_kilo_fake.py`,
      `tests/test_contest_cli.py`, `tests/test_contest_kilo_client.py`
      (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `git diff <base>..HEAD -- tests/test_contest_kilo_client.py | grep -c '^-[^-]'`
      is 0 — KC-1's tests gain, never lose, lines.
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
