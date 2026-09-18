# Kilo probe — what `scripts/kilo_hello.py` established on 2026-09-17

Everything below was observed live on this machine: Kilo Code VS Code
extension 7.6.2 (`~/.vscode/extensions/kilocode.kilo-code-7.6.2-linux-x64/bin/kilo`,
an OpenCode-based headless server), provider `kenary`, models
`laguna-s-2-1:free`, `mistral-medium-3-5:free`, `hy3:free`. Session directory
`/tmp/kilo-hello`. Raw event log of every run: `/tmp/kilo-hello/events.jsonl`
(not committed). The script is standard-library only and imports nothing from
this repository.

```bash
python3 scripts/kilo_hello.py --model kenary/hy3:free --dir /tmp/kilo-hello --file hello-hy3.txt --append-model
python3 scripts/kilo_hello.py --model kenary/hy3:free --dir /tmp/kilo-hello --scenario rm-outside --gate external_directory --reply reject
```

## The five primitives the contest needs, all verified

| # | primitive | how | result |
|---|---|---|---|
| 1 | start a server | `kilo serve --port P --hostname 127.0.0.1 --print-logs`; ready when `GET /global/health` is 200 | < 1 s; `terminate()` leaves nothing behind |
| 2 | pick provider + model | `POST /session?directory=<dir>` body `{"model": {"providerID": "kenary", "id": "hy3:free"}, "permission": [...]}`; then `POST /session/{id}/prompt_async` body `{"parts": [{"type":"text","text": ...}], "model": {"providerID", "modelID"}}` | accepted for all three models; note the two model shapes (`id` vs `modelID`) |
| 3 | detect completion | SSE `GET /event?directory=<dir>`; wait for `session.idle` with this `sessionID` **after** the prompt was sent | identical sequence on every model: `session.status busy` → `file.edited` → `session.status idle` → `session.idle` → `session.turn.close` |
| 4 | stop, check, continue | after idle: check the file on disk; `prompt_async` again into the **same session**; wait for the next idle | all three models kept context and appended line 2 (`Hello world` / `<model id>`) |
| 5 | the boundary | ask the model to `rm -v /tmp/testfile` (outside the session directory) | a `permission.asked` event arrives over HTTP; `POST /permission/{id}/reply {"reply":"reject"}` blocks it, `"once"` lets it run |

Timings (turn 1 / turn 2, seconds to idle): laguna 10.6 / 17.0, mistral
19.2 / 9.8, hy3 11.6 / 13.6.

## The boundary is the session directory, not the plugin's folder

Writing `hello-*.txt` into `/tmp/kilo-hello` never asked anything because
that directory was the session's `directory`. The same session asked before
touching `/tmp/testfile`. For the contest: **session directory = the agent's
worktree**; everything inside is free, everything outside is a permission
event we answer.

## The permission payloads, verbatim

Session rules sent: `*` allow, `external_directory` ask, `doom_loop` ask.
Prompt: ``Run exactly this shell command and paste its full output: `rm -v /tmp/testfile` ...``

```json
{"type": "permission.asked", "properties": {
  "id": "per_0afb2bf60001KFTnUBoT5JQbJe", "sessionID": "ses_f504d5ef4ffeAbHyyPaAMRYROL",
  "permission": "external_directory", "patterns": ["/tmp/*"],
  "metadata": {"command": "rm -v /tmp/testfile", "description": "Remove test file with verbose output",
               "directories": ["/tmp"], "patterns": ["/tmp/*"]},
  "always": ["/tmp/*"],
  "tool": {"messageID": "msg_0afb2a8f0001UeomCvVgJT2RDL", "callID": "call_chatcmpl-tool-7721747835df45abb0e8b79792ad1114"}}}
```

Session rules sent: `*` allow, `bash` ask, `doom_loop` ask. Same prompt.

```json
{"type": "permission.asked", "properties": {
  "id": "per_0afb32854001mmQjaA39GfX6Jf", "sessionID": "ses_f504d18f0ffehKDcvqpkphdcji",
  "permission": "bash", "patterns": ["rm -v /tmp/testfile"],
  "metadata": {"command": "rm -v /tmp/testfile", "description": "Remove test file at /tmp/testfile"},
  "always": ["rm *"],
  "tool": {"messageID": "msg_0afb3022c001E6ls3tt0MQ6Z4K", "callID": "call_chatcmpl-tool-af873cc4dfef4ad68913fdaa72d6b7de"}}}
```

Reply and its echo:

```
POST /permission/per_…/reply?directory=…   {"reply": "reject", "message": "kilo_hello: reject (outside the project directory)"}   → 200 true
{"type": "permission.replied", "properties": {"sessionID": "ses_…", "requestID": "per_…", "reply": "reject"}}
```

The legacy endpoint `POST /session/{sid}/permissions/{pid}` was never needed.
The model sees the rejection as a tool error; the `tool` part in
`GET /session/{id}/message` reads:

```
tool=bash status=error input={"command": "rm -v /tmp/testfile", ...}
output="The user rejected permission to use this specific tool call with the following feedback: kilo_hello: reject (outside the project directory)"
```

With `"reply": "once"`: `status=completed output="removed '/tmp/testfile'\n"`.

## Facts that shape the policy

1. **`external_directory` fires on a path in a command argument**, not only
   on an outside `workdir`. The event carries the directory pattern
   (`/tmp/*`) *and* the full command (`metadata.command`). The string in the
   binary ("command-argument path warnings are advisory only") does not
   describe this build's behaviour.
2. **`bash: ask` carries the command text** (`patterns: ["rm -v /tmp/testfile"]`,
   `always: ["rm *"]`) — but then every `pytest` and `git` call is a round
   trip too.
3. **Models react to a rejection differently.** laguna said `done` and
   stopped; hy3 explained the block; **mistral retried once with
   `"workdir": "/tmp"`** and produced a second `permission.asked`. The
   policy must judge every event of a session, not the first one.
4. **Never reply `always`.** Under `external_directory` it would whitelist
   `/tmp/*` for the rest of the session.
5. **Answer permissions from a cursor.** A wait that rescans from the start
   of the event list mistakes turn 1's `session.idle` for turn 2's
   (observed: "idle after 0.0 s", file unchanged). Every event is consumed
   exactly once.
6. **Line-based checks.** mistral wrote the file without a trailing newline,
   the others with one. Compare content by stripped lines.
7. `session.next.tool.*` events did **not** appear on `/event`; the tool
   call and its result live in the `tool` parts of
   `GET /session/{id}/message`, and the fact of an edit shows up as
   `file.edited` + `session.diff`.

## KC-1 module re-verified live (2026-09-18)

`tools/contest/kilo_client.py` (the module built from this probe) was run
live against a real `kilo` server and all three models, independently of
`kilo_hello.py`: same two-turn same-session flow (write `hello.txt`, then
append a line) plus the `rm -v` outside-directory boundary check, using
`KiloClient`/`EventTap`/`wait_idle` as shipped. All three passed —
`session.idle` observed on both turns, file content correct,
`permission.asked` fired and `reject` was honored on the boundary turn.

Timings (turn 1 / turn 2, seconds to idle): laguna 13.8 / 23.2, mistral
15.8 / 15.6, hy3 16.8 / 19.2 — consistent with the original probe numbers
above. `tool_parts()` per model: laguna and hy3 both did
`write → read → edit`, mistral did `write → edit` only. Boundary permission
count: laguna 2, mistral 2, hy3 1 (mistral's retry-with-`workdir` behaviour
noted in fact 3 above still holds).

## KC-2 roster re-verified live (2026-09-18)

`tools/contest/roster.py`'s `load_roster("contest.ini")` was used, unmodified,
to drive real sessions: for each of the three `AgentSpec`s it returned
(`laguna`, `mistral`, `hy3` — provider/model split at the first `/`),
`KiloClient.create_session` was called with that spec's `provider_id`/
`model_id` and `ContestConfig.session_rules()` as the permission list, then
prompted with `"Reply with exactly: OK"`. All three reached `session.idle`
and replied `OK` (laguna 9.2 s, mistral 9.2 s, hy3 7.6 s). No fix needed —
the roster's parsing and rule list work as shipped against a real server.

## Side observations

- The VS Code extension keeps its own `kilo serve --port 0` processes
  (parent: `code`) — nine of them at the time. The probe never touched
  them; the contest runs its own server and may `--attach` to the
  extension's when its port is known.
- Sessions created by the probe share `~/.local/share/kilo/kilo.db` with the
  extension, so they appear in its history.
