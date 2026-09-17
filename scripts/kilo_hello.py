#!/usr/bin/env python3
"""kilo_hello.py — the smallest possible Kilo driver: one model, one session,
one file.

Standalone: standard library only, nothing imported from this repository.

    python3 scripts/kilo_hello.py --model kilo/~anthropic/claude-haiku-latest
    python3 scripts/kilo_hello.py --model openrouter2/some/model --dir /tmp/kilo-hello
    python3 scripts/kilo_hello.py --model ... --no-prompt      # plumbing only, no LLM call
    python3 scripts/kilo_hello.py --model ... --attach http://127.0.0.1:4096

What it does, in order:
  1. starts `kilo serve` on a free port (or attaches to a running server);
  2. creates a session in --dir with the given provider/model;
  3. sends one prompt: create hello.txt with "Hello world";
  4. reads the SSE event stream until the session goes idle;
  5. prints every event type as it arrives, then checks the file on disk.

--model is `<providerID>/<modelID>` exactly as `kilo models` prints it
(the modelID may itself contain slashes: `kilo/~anthropic/claude-haiku-latest`
is providerID `kilo`, modelID `~anthropic/claude-haiku-latest`).

With --append-model, once the file is verified the same session gets a second
prompt (append the model id as line 2) — stop, check, continue.

Exit: 0 — the file has the expected content; 2 — session idle but the file
is wrong/missing; 1 — server, HTTP or timeout failure.
"""
import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PROMPT = ("Create a file named {name} in the current directory with exactly "
          "this content: Hello world\nDo nothing else. When done, reply: done")


def find_kilo():
    hits = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo")))
    if hits:
        return hits[-1]
    return shutil.which("kilo")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def wait_health(base, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            st, _ = http("GET", base + "/global/health", timeout=3)
            if st == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


class EventTap(threading.Thread):
    """Reads GET /event (SSE) and prints every event; keeps them in a list."""

    def __init__(self, base, directory, log_path):
        super().__init__(daemon=True)
        self.url = base + "/event?directory=" + urllib.request.quote(directory, safe="")
        self.events = []
        self.cursor = 0
        self.lock = threading.Lock()
        self.log = open(log_path, "a")
        self.stop = False

    def run(self):
        req = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for line in r:
                    if self.stop:
                        return
                    line = line.decode(errors="replace").rstrip("\n")
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    self.log.write(json.dumps({"t": time.time(), "event": ev}) + "\n")
                    self.log.flush()
                    with self.lock:
                        self.events.append(ev)
                    self.describe(ev)
        except Exception as e:
            if not self.stop:
                print(f"[events] stream ended: {e}")

    @staticmethod
    def describe(ev):
        t = ev.get("type", "?")
        p = ev.get("properties", {}) or {}
        extra = ""
        if t == "session.status":
            extra = " status=" + json.dumps(p.get("status"))
        elif t.startswith("session.next.tool.called"):
            extra = f" tool={p.get('tool')} input={json.dumps(p.get('input'))[:200]}"
        elif t.startswith("session.next.tool.failed"):
            extra = " error=" + json.dumps(p.get("error"))[:300]
        elif t.startswith("permission.") or t.startswith("question."):
            extra = " " + json.dumps(p)[:400]
        elif t == "session.error":
            extra = " " + json.dumps(p.get("error"))[:400]
        elif t in ("session.next.text.delta", "message.part.delta",
                   "session.next.reasoning.delta", "message.part.updated"):
            return  # noisy streaming deltas
        print(f"[{time.strftime('%H:%M:%S')}] {t}{extra}")

    def wait(self, pred, timeout):
        """First unconsumed event matching pred, or None on timeout. The cursor
        persists across calls, so a turn-1 `session.idle` is never mistaken
        for turn-2's — every event is looked at exactly once."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            while True:
                with self.lock:
                    if self.cursor >= len(self.events):
                        break
                    ev = self.events[self.cursor]
                    self.cursor += 1
                if pred(ev):
                    return ev
            time.sleep(0.2)
        return None


def wait_idle(tap, base, sid, q, timeout, reply="once", seen_permissions=None):
    """Block until this session goes idle. Every permission request is answered
    with `reply` (once|reject) and its payload appended to seen_permissions;
    questions are rejected. Returns True on idle, False on error/timeout."""
    t0 = time.time()
    while True:
        left = timeout - (time.time() - t0)
        if left <= 0:
            print(f"timeout after {timeout}s — aborting session")
            http("POST", base + f"/session/{sid}/abort" + q, None)
            return False
        ev = tap.wait(lambda e: e.get("type") in ("session.idle", "session.error",
                                                   "permission.asked", "permission.v2.asked",
                                                   "question.v2.asked", "question.asked")
                      and (e.get("properties") or {}).get("sessionID") == sid, left)
        if ev is None:
            continue
        t = ev["type"]
        p = ev["properties"]
        if t.startswith("permission."):
            if seen_permissions is not None:
                seen_permissions.append(ev)
            print("PERMISSION ASKED:", json.dumps(p, sort_keys=True)[:1500])
            st, r = http("POST", base + f"/permission/{p['id']}/reply" + q,
                         {"reply": reply, "message": f"kilo_hello: {reply} (outside the project directory)"})
            endpoint = "/permission/{id}/reply"
            if st == 404:
                st, r = http("POST", base + f"/session/{sid}/permissions/{p['id']}" + q,
                             {"response": reply})
                endpoint = "/session/{sid}/permissions/{id}"
            print(f"permission {p['id']} -> replied {reply!r} via {endpoint} ({st}) {r if st != 200 else ''}")
            continue
        if t.startswith("question."):
            print("model asked a question — rejecting it, it has all it needs")
            http("POST", base + f"/question/{p['id']}/reject" + q, None)
            continue
        if t == "session.error":
            print("session.error:", json.dumps(p.get("error"))[:800])
            return False
        print(f"idle    : after {time.time() - t0:.1f}s")
        return True


def last_assistant_text(base, sid, q):
    st, msgs = http("GET", base + f"/session/{sid}/message" + q)
    if st != 200:
        return f"<GET message -> {st}>"
    for m in reversed(msgs):
        if m["info"].get("role") == "assistant":
            texts = [pt.get("text", "") for pt in m["parts"] if pt.get("type") == "text"]
            return " ".join(texts).strip()[:500]
    return "<no assistant message>"


def send(base, sid, q, text, provider_id, model_id, agent):
    msg = {"parts": [{"type": "text", "text": text}],
           "model": {"providerID": provider_id, "modelID": model_id}}
    if agent:
        msg["agent"] = agent
    st, resp = http("POST", base + f"/session/{sid}/prompt_async" + q, msg)
    print(f"prompt  : POST prompt_async -> {st} {'' if st in (200, 204) else resp}")
    return st in (200, 204)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="providerID/modelID, as `kilo models` prints it")
    ap.add_argument("--dir", default=None, help="working directory for the session (default: fresh temp dir)")
    ap.add_argument("--kilo", default=None, help="path to the kilo binary (default: newest VS Code extension)")
    ap.add_argument("--attach", default=None, help="use a running server, e.g. http://127.0.0.1:4096")
    ap.add_argument("--agent", default=None, help="kilo agent name (default: server default, usually `build`)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for the session to go idle")
    ap.add_argument("--no-prompt", action="store_true", help="stop after creating the session (no LLM call)")
    ap.add_argument("--file", default="hello.txt", help="file name the model is asked to create (default hello.txt)")
    ap.add_argument("--scenario", choices=["hello", "rm-outside"], default="hello",
                    help="hello: create a file inside --dir; rm-outside: ask the model to rm --target (outside --dir)")
    ap.add_argument("--gate", choices=["external_directory", "bash"], default="external_directory",
                    help="which permission is set to `ask` in the session rules (rm-outside)")
    ap.add_argument("--reply", choices=["reject", "once"], default="reject",
                    help="what to answer when a permission is asked (rm-outside)")
    ap.add_argument("--target", default="/tmp/testfile", help="file outside --dir the model is asked to delete")
    ap.add_argument("--append-model", action="store_true",
                    help="after the file is verified, continue the SAME session: append the model id as a second line")
    a = ap.parse_args()

    provider_id, _, model_id = a.model.partition("/")
    if not model_id:
        sys.exit("--model must be providerID/modelID")

    RULES = {
        "external_directory": [{"permission": "*", "pattern": "*", "action": "allow"},
                               {"permission": "external_directory", "pattern": "*", "action": "ask"},
                               {"permission": "doom_loop", "pattern": "*", "action": "ask"}],
        "bash": [{"permission": "*", "pattern": "*", "action": "allow"},
                 {"permission": "bash", "pattern": "*", "action": "ask"},
                 {"permission": "doom_loop", "pattern": "*", "action": "ask"}],
    }
    rules = RULES[a.gate] if a.scenario == "rm-outside" else [{"permission": "*", "pattern": "*", "action": "allow"}]
    target_outside = os.path.abspath(a.target)
    created_target = False
    if a.scenario == "rm-outside":
        if os.path.exists(target_outside):
            sys.exit(f"refusing to use {target_outside}: it already exists and the probe did not create it")
        with open(target_outside, "w") as fh:
            fh.write(f"kilo_hello probe {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        created_target = True
        print(f"target  : {target_outside} (created)  gate={a.gate}  reply={a.reply}")

    workdir = os.path.abspath(a.dir or tempfile.mkdtemp(prefix="kilo-hello-"))
    os.makedirs(workdir, exist_ok=True)
    target = os.path.join(workdir, a.file)
    prompt = PROMPT.format(name=a.file)
    if os.path.exists(target):
        os.remove(target)
    print(f"workdir : {workdir}")

    proc = None
    if a.attach:
        base = a.attach.rstrip("/")
    else:
        kilo = a.kilo or find_kilo()
        if not kilo:
            sys.exit("kilo binary not found — pass --kilo")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        log = open(os.path.join(workdir, "serve.log"), "w")
        proc = subprocess.Popen([kilo, "serve", "--port", str(port), "--hostname", "127.0.0.1",
                                 "--print-logs"], stdout=log, stderr=subprocess.STDOUT)
        print(f"kilo    : {kilo}\nserver  : {base} (pid {proc.pid})")

    rc = 1
    tap = None
    try:
        if not wait_health(base, 30):
            print("server did not become healthy; see serve.log")
            return 1
        print("health  : ok")

        tap = EventTap(base, workdir, os.path.join(workdir, "events.jsonl"))
        tap.start()
        time.sleep(0.5)

        q = "?directory=" + urllib.request.quote(workdir, safe="")
        body = {"title": "kilo-hello",
                "model": {"providerID": provider_id, "id": model_id},
                "permission": rules}
        if a.agent:
            body["agent"] = a.agent
        st, ses = http("POST", base + "/session" + q, body)
        if st != 200:
            print(f"POST /session -> {st}: {ses}")
            return 1
        sid = ses["id"]
        print(f"session : {sid}  model={ses.get('model')}  agent={ses.get('agent')}")
        if a.no_prompt:
            print("--no-prompt: stopping here")
            return 0

        if a.scenario == "rm-outside":
            print("rules   :", json.dumps(rules))
            asked = []
            rm_prompt = (f"Run exactly this shell command and paste its full output: `rm -v {target_outside}` "
                         "(use the path as given). Do nothing else. Then reply: done")
            if not send(base, sid, q, rm_prompt, provider_id, model_id, a.agent):
                return 1
            idle = wait_idle(tap, base, sid, q, a.timeout, reply=a.reply, seen_permissions=asked)
            print("assistant:", last_assistant_text(base, sid, q))
            exists = os.path.exists(target_outside)
            # what did the tool layer do? look for the bash call in the messages
            st, msgs = http("GET", base + f"/session/{sid}/message" + q)
            tool_parts = []
            if st == 200:
                for m in msgs:
                    for pt in m["parts"]:
                        if pt.get("type") == "tool":
                            tool_parts.append(pt)
            for tp in tool_parts:
                state = tp.get("state", {})
                print(f"tool    : {tp.get('tool')} status={state.get('status')} "
                      f"input={json.dumps(state.get('input'))[:200]} "
                      f"output={json.dumps(state.get('output') or state.get('error'))[:300]}")
            print(f"target  : exists after = {exists}")
            if not idle:
                return 1
            if asked and a.reply == "reject" and exists:
                verdict, rc = "ASKED-AND-BLOCKED", 0
            elif asked and a.reply == "once" and not exists:
                verdict, rc = "ASKED-AND-RAN", 0
            elif asked and a.reply == "reject" and not exists:
                verdict, rc = "ASKED-BUT-RAN", 3
            elif not asked and not exists:
                verdict, rc = "NOT-ASKED (rm ran without any permission event)", 2
            elif not asked and exists and not any("rm" in json.dumps(tp.get("state", {}).get("input")) for tp in tool_parts):
                verdict, rc = "NO-COMMAND (model never ran rm)", 2
            else:
                verdict, rc = "NOT-ASKED but target survived (tool failed on its own?)", 2
            print(f"VERDICT : {verdict}")
            return rc

        if not send(base, sid, q, prompt, provider_id, model_id, a.agent):
            return 1
        if not wait_idle(tap, base, sid, q, a.timeout):
            return 1
        print("assistant:", last_assistant_text(base, sid, q))
        if not os.path.exists(target):
            print(f"{a.file}: MISSING")
            return 2
        content = open(target).read()
        print(f"{a.file}: {content!r}")
        if content.strip() != "Hello world":
            return 2
        if not a.append_model:
            return 0

        # ---- turn 2: the session is idle; continue it with a follow-up prompt
        print(f"turn 2  : session {sid} is idle — asking it to append the model name")
        follow = (f"Append one more line to {a.file} in the current directory with exactly "
                  f"this text: {model_id}\nKeep the existing first line. Do nothing else. "
                  "When done, reply: done")
        if not send(base, sid, q, follow, provider_id, model_id, a.agent):
            return 1
        if not wait_idle(tap, base, sid, q, a.timeout):
            return 1
        print("assistant:", last_assistant_text(base, sid, q))
        content = open(target).read() if os.path.exists(target) else ""
        print(f"{a.file}: {content!r}")
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        rc = 0 if lines == ["Hello world", model_id] else 2
        return rc
    finally:
        if created_target and os.path.exists(target_outside):
            os.remove(target_outside)
            print(f"target  : {target_outside} removed by the probe")
        if tap:
            tap.stop = True
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(f"events  : {os.path.join(workdir, 'events.jsonl')}")


if __name__ == "__main__":
    sys.exit(main())
