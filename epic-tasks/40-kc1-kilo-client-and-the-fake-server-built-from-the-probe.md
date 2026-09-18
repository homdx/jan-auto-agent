# KC-1 — `tools/contest/kilo_client.py`: the probe's five primitives as a module, and a fake Kilo server to test against

**Status:** landed — ideal patch from a 4-entry contest, `kc1/*.patch`: DeepSeek-Flash-v4-1, LagunaS-2-1, SenSenova6-7-var2, SenSenova6-8 — two more submissions never touched `kilo_client.py` and one failed to apply, both dropped before scoring. Winner SenSenova6-8 taken as-is: own tests, symbol/stdlib/print static checks, and code read on `EventTap.stop()` — the ticket's hardest bullet — found DeepSeek closes the SSE response from the wrong thread (CPython deadlock risk) and LagunaS-2-1 never closes it at all (thread outlives the call, violating "stop() ... closes the response so the thread ends"); only SenSenova6-7-var2 and SenSenova6-8 use a read-side `socket.shutdown()`, and 6-8 also uses `time.monotonic()` for the timeout and keeps `tool_parts()` shaped exactly as spec'd (var2 flattens `state`, off-spec). `tests` + `tests_bugfix` green. Re-verified live against all three real models — see `docs/kilo-contest/PROBE.md`'s "KC-1 module re-verified live" section.  
**Severity:** HIGH  
**File:** `tools/contest/kilo_client.py` (new)  
**Symbol:** `KiloServer`, `KiloClient`, `EventTap`, `find_kilo_binary`, `SessionRef`  
**Round:** 40  
**Size:** M  
**Source:** `scripts/kilo_hello.py` and `docs/kilo-contest/PROBE.md`. The script proved, live, everything this module must do: spawn `kilo serve` and wait for `/global/health`; `POST /session?directory=` with `{"providerID","id"}` and a `permission` rule list; `POST /session/{id}/prompt_async` with `{"providerID","modelID"}`; an SSE tap on `GET /event?directory=` consumed from a cursor; `POST /permission/{id}/reply` (`once|reject`, `message`); `GET /session/{id}/message` and its `tool` parts; `GET /session/{id}/diff`; `POST /session/{id}/abort`. The script is a probe; the contest needs the same calls as a class with no `print`, no `sys.exit`, and a test double.  
**Depends on:** nothing.  
**Also touches:** `tools/contest/__init__.py` (new, empty), `tests/_kilo_fake.py` (new: the fake server), `tests/test_contest_kilo_client.py` (new), `scripts/kilo_hello.py` (unchanged — it stays a standalone probe; do not make it import the module)

---

## What happens today

`scripts/kilo_hello.py` holds the working code inline: `find_kilo`,
`free_port`, `http`, `wait_health`, `EventTap` (with the cursor fix),
`wait_idle`, `send`, `last_assistant_text`, and the message/tool-part
readback in `main`. It prints, it exits, it decides — a probe. There is
no importable client, and there is no way to test a caller of Kilo
without a live `kilo` binary and a live provider.

Two things the probe learned that the module must carry as behaviour, not
as comments (PROBE.md §"Facts", items 5 and 7): every event is consumed
exactly once (a rescan mistakes turn 1's `session.idle` for turn 2's), and
the tool call + its result are read from the `tool` parts of the messages,
not from `/event`.

## What must change

1. **`find_kilo_binary(explicit: str | None) -> str`** — `explicit` if
   given, else the newest `~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo`
   (sorted by the version in the folder name, not lexically: `7.10` > `7.6`),
   else `shutil.which("kilo")`, else `FileNotFoundError` with the three
   places it looked.

2. **`KiloServer`** — `KiloServer.spawn(binary, *, log_path, hostname="127.0.0.1", health_timeout=30.0)`
   picks a free port, starts `kilo serve --port P --hostname H --print-logs`
   with stdout+stderr to `log_path`, polls `GET /global/health` until 200,
   raises `KiloServerError` (with the log tail) on timeout and kills the
   child. `KiloServer.attach(url)` wraps a running server (health checked
   once). `.base_url`, `.close()` (terminate, wait 5 s, kill), context
   manager. `close()` on an attached server does nothing to the process.

3. **`EventTap(base_url, directory, log_path)`** — the probe's tap:
   daemon thread over `GET /event?directory=`, each `data:` line parsed,
   appended to `log_path` as `{"t": <time.time()>, "event": {...}}`, kept
   in memory. `wait(pred, timeout) -> dict | None` consumes from a
   persistent cursor — **every event is examined exactly once across
   calls**. `stop()` sets the flag and closes the response so the thread
   ends. `stream ended` is recorded as a synthetic event
   `{"type": "tap.closed", "properties": {"error": "..."}}` so a caller
   waiting on it wakes up instead of timing out.

4. **`KiloClient(server: KiloServer, directory: str)`** — every method
   sends `?directory=<quoted>` and raises `KiloHttpError(status, body, method, path)`
   on a non-2xx:
   - `create_session(provider_id, model_id, *, rules: list[dict], title: str, agent: str | None = None) -> SessionRef`
     (`SessionRef` = `id`, `provider_id`, `model_id`, `agent`, `directory`).
   - `prompt(session: SessionRef, text: str) -> None` — `prompt_async`,
     model as `{"providerID","modelID"}`, 200 and 204 both fine.
   - `reply_permission(permission_id, reply: Literal["once","reject"], message: str) -> None`
     — never `always`; the enum is enforced in code. On 404 fall back to
     `POST /session/{sid}/permissions/{pid}` `{"response": reply}` (needs
     the session id → signature takes `session` too).
   - `reject_question(question_id)` — `POST /question/{id}/reject`.
   - `messages(session) -> list[dict]`, `tool_parts(session) -> list[dict]`
     (every part with `type == "tool"`, in order, each with `tool`,
     `state.status`, `state.input`, `state.output` / `state.error`),
     `last_assistant_text(session) -> str`.
   - `diff(session) -> list[dict]`, `abort(session) -> None`,
     `session_info(session) -> dict` (`GET /session/{id}` — `cost`, `tokens`).
   - `wait_idle(tap, session, timeout, *, on_permission, on_question) -> IdleResult`
     — the probe's `wait_idle` with the decisions delegated: `on_permission(event) -> (reply, message)`,
     `on_question(event) -> None`. Returns `IdleResult(status="idle"|"error"|"timeout"|"closed", error=..., permissions=[events], questions=[events], elapsed=...)`.
     Timeout → `abort` is called before returning.

5. **`tests/_kilo_fake.py`** — an `http.server`-based fake of the routes
   above, scripted per session by a scenario dict, in the style of
   `contest-bench/harness/provider.py`:
   ```python
   {"turns": [                       # one entry per prompt the client sends
       {"events": ["busy", "file.edited", "idle"],         # names → recorded payload shapes from PROBE.md
        "permission": {"permission": "external_directory", "patterns": ["/tmp/*"],
                       "metadata": {"command": "rm -v /tmp/testfile", "directories": ["/tmp"]}},   # optional; emitted before idle, idle waits for the reply
        "on_prompt": callable(directory, text) -> None,     # optional; mutate the session directory (write a file, git commit)
        "tool_parts": [ {...} ],                            # what GET message returns
        "assistant": "done"},
   ]}
   ```
   Emits `session.created` on create, the turn's events on each prompt,
   `permission.asked` then `permission.replied` after the reply, `session.idle`
   last; `session.error` when a turn says `{"error": {...}}`. Records every
   request (method, path, body) for assertions. Serves `/global/health`,
   `/event` (SSE, chunked, keeps the connection open until `stop`),
   `/session`, `/session/{id}/prompt_async`, `/session/{id}/message`,
   `/session/{id}/diff`, `/session/{id}`, `/session/{id}/abort`,
   `/permission/{id}/reply`, `/question/{id}/reject`. Binds an ephemeral
   port on 127.0.0.1. The payload shapes are copied from
   `docs/kilo-contest/PROBE.md` — the fake replays what was recorded.

6. **`tests/test_contest_kilo_client.py`**, marked
   `@pytest.mark.xdist_group("port_bound_http_servers")`:
   - two turns into one session: turn 1's idle is not mistaken for turn
     2's (the exact bug the probe hit — assert `wait_idle` for turn 2
     returns only after the fake emitted turn 2's idle);
   - a permission in the middle: `on_permission` is called with the
     recorded `external_directory` payload, the reply body sent is
     `{"reply": "reject", "message": ...}`, `IdleResult.permissions` has one
     entry, idle still reached;
   - `reply_permission(..., "always")` raises `ValueError` before any HTTP;
   - `session.error` → `IdleResult.status == "error"` with the payload;
   - timeout → `abort` was requested (the fake recorded `POST .../abort`);
   - `tool_parts` returns the fake's parts in order; `last_assistant_text`
     returns `"done"`;
   - `KiloServer.spawn` with a binary path that exits immediately raises
     `KiloServerError` containing the log tail (use a tiny shell script as
     the "binary");
   - `find_kilo_binary` ordering: `7.10.0` beats `7.6.2` (temp dirs).

## Acceptance

- [ ] `tools/contest/kilo_client.py` exposes exactly the names in
      **Symbol** plus `KiloHttpError`, `KiloServerError`, `IdleResult`;
      no `print`, no `sys.exit`, standard library only.
- [ ] `tests/_kilo_fake.py` replays the five PROBE.md shapes
      (`session.created`, `session.status`, `session.idle`, both
      `permission.asked` variants, `permission.replied`) byte-compatible
      with the recorded keys.
- [ ] `tests/test_contest_kilo_client.py` covers the eight cases above;
      all carry the `port_bound_http_servers` group.
- [ ] `scripts/kilo_hello.py` is untouched (matches the version referenced by `docs/kilo-contest/PROBE.md`, no diff).
- [ ] `python3 scripts/sync_test_tiers.py` run; `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (sequentially).

## Out of scope

- Deciding permissions — KC-3; here the decision is a callback.
- Retries/backoff on HTTP errors — the server is local; a failure is
  reported, not retried.
- `session.next.tool.*` events — not observed on `/event` in this build;
  the tool parts are the source of truth.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
