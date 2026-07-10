#!/usr/bin/env python3
"""
Adversarial stub Ollama server — CODE mode: Python app in 3 tasks.

Plan (single architect emission, 3 sequential tasks):
  T1  app.py: hello world (stdlib http.server), acceptance imports app and
      asserts hello() == 'Hello, world!'.
  T2  add Swagger: openapi_spec() + /openapi.json route, acceptance asserts
      the OpenAPI 3.x document and that hello() survived.
  T3  add Prometheus metrics: observe_request()/render_metrics() + /metrics,
      acceptance asserts text exposition format and counters.

Adversarial script (user-ordered coder glitches):
  T1 attempt 1 -> PARROT: instead of the required JSON {"files": [...]}, the
      coder ECHOES the validator's question back as plain prose ("Do you want
      me to..."). No parsable files. Tests the coder-output parser path and
      the no-files-written handling.
      attempt 2 -> correct app_v1.py.
  T2 attempt 1 -> WRONG RESULT: valid JSON, valid Python — but a Fibonacci
      CLI that deletes hello() and has no openapi_spec(). Acceptance demands
      exit 0 on the OpenAPI assertions; the executor MUST fail it (real
      execution, not LLM opinion).
      attempt 2 -> PARROT AGAIN, harder: the coder writes the validator's
      feedback VERBATIM INTO THE FILE as app.py content (valid JSON envelope,
      garbage payload). Import fails -> executor fails.
      attempt 3 -> correct app_v2.py.
  T3 attempt 1 -> SUBTLE WRONG RESULT: metrics endpoint renders the wrong
      metric name (http_requests instead of app_http_requests_total) and
      no TYPE line. Import succeeds, hello survives — only the acceptance
      assertions on the exposition format catch it.
      attempt 2 -> correct app_v3.py.

Gate-2 (code) here plays a WEAK but honest validator: it approves anything
that executed (exit 0) and rejects with a hint when execution failed —
mirroring a real small model that leans on the executor's ground truth.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLANNED = os.path.join(HERE, "planned")
LOG_PATH = os.path.join(HERE, "stub_llm.log")

CODER_ATTEMPTS = {}

VALIDATOR_QUESTION = (
    "Do you want me to keep the existing http.server structure, or should I "
    "rewrite the handler from scratch? Also, which port should the hello "
    "endpoint listen on — 8000 or a random free port?"
)


def _log(kind, system, user, reply):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n===== {kind} =====\n--- system (first 200) ---\n{system[:200]}\n"
                f"--- user (first 1200) ---\n{user[:1200]}\n--- reply (first 600) ---\n{reply[:600]}\n")


def _read(name):
    return open(os.path.join(PLANNED, name), encoding="utf-8").read()


def _files_json(content: str) -> str:
    return json.dumps({"files": [{"path": "app.py", "content": content}]},
                      ensure_ascii=False)


ACCEPT_1 = ("python3 -c \"import app; assert app.hello()=='Hello, world!'\"")
ACCEPT_2 = ("python3 -c \"import app; s=app.openapi_spec(); "
            "assert s['openapi'].startswith('3.'); "
            "assert '/openapi.json' in s['paths']; "
            "assert app.hello()=='Hello, world!'\"")
ACCEPT_3 = ("python3 -c \"import app; "
            "app.observe_request('/'); app.observe_request('/'); "
            "m=app.render_metrics(); "
            "assert '# TYPE app_http_requests_total counter' in m; "
            "assert 'app_http_requests_total' in m; "
            "assert app.hello()=='Hello, world!'; "
            "assert '/metrics' in app.openapi_spec()['paths']\"")


def build_reply(system: str, user: str) -> str:
    if "You are a plan reviewer" in system:
        reply = "APPROVED"
        _log("plan_reviewer", system, user, reply)
        return reply

    # Architect — code mode
    if "senior software architect" in system:
        tasks = [
            {
                "title": "T1: hello-world web app",
                "instruction": ("Create app.py: a stdlib http.server app with a "
                                "hello() function returning 'Hello, world!' and a "
                                "GET / route serving it."),
                "target_files": ["app.py"],
                "acceptance_check": ACCEPT_1,
                "cited_location": {"file": "app.py", "symbol": "hello",
                                   "line_start": None, "line_end": None,
                                   "new_file": True},
            },
            {
                "title": "T2: add Swagger (OpenAPI) endpoint",
                "instruction": ("Extend app.py with openapi_spec() returning an "
                                "OpenAPI 3.0 dict and a GET /openapi.json route. "
                                "hello() must keep working."),
                "target_files": ["app.py"],
                "acceptance_check": ACCEPT_2,
                "cited_location": {"file": "app.py", "symbol": "hello",
                                   "line_start": None, "line_end": None},
            },
            {
                "title": "T3: add Prometheus metrics",
                "instruction": ("Extend app.py with observe_request()/"
                                "render_metrics() (text exposition format, "
                                "counter app_http_requests_total) and a GET "
                                "/metrics route. Everything else must keep "
                                "working."),
                "target_files": ["app.py"],
                "acceptance_check": ACCEPT_3,
                "cited_location": {"file": "app.py", "symbol": "hello",
                                   "line_start": None, "line_end": None},
            },
        ]
        reply = json.dumps(tasks, ensure_ascii=False)
        _log("architect_code", system, user, reply)
        return reply

    # Coder — the adversarial engineer
    if "senior software engineer implementing a targeted code improvement" in system:
        m = re.search(r"T(\d):", user)
        tkey = f"T{m.group(1)}" if m else "T?"
        CODER_ATTEMPTS[tkey] = CODER_ATTEMPTS.get(tkey, 0) + 1
        n = CODER_ATTEMPTS[tkey]

        if tkey == "T1":
            reply = VALIDATOR_QUESTION if n == 1 else _files_json(_read("app_v1.py"))
            tag = "PARROT-QUESTION" if n == 1 else "clean v1"
        elif tkey == "T2":
            if n == 1:
                reply = _files_json(_read("app_v2_wrong.py"))
                tag = "WRONG-RESULT fibonacci"
            elif n == 2:
                # PARROT INTO FILE: validator feedback verbatim as file content
                fb = user.split("FEEDBACK FROM PREVIOUS ATTEMPTS:", 1)[-1][:400]
                reply = _files_json("# validator said:\n" + fb + "\n")
                tag = "PARROT-INTO-FILE"
            else:
                reply = _files_json(_read("app_v2.py"))
                tag = "clean v2"
        elif tkey == "T3":
            if n == 1:
                wrong = _read("app_v3.py").replace(
                    "app_http_requests_total", "http_requests").replace(
                    '        "# TYPE http_requests counter",\n', "")
                reply = _files_json(wrong)
                tag = "WRONG-METRIC-NAME"
            else:
                reply = _files_json(_read("app_v3.py"))
                tag = "clean v3"
        else:
            reply = _files_json("# unknown task\n")
            tag = "unknown"
        _log(f"coder_code({tkey} attempt={n} -> {tag})", system, user, reply)
        return reply

    # Gate-2 code — weak-but-honest JSON validator leaning on exec ground truth
    if "You are a code-change validator" in system:
        exec_failed = bool(re.search(r"exit\s+code:\s*[1-9]|FAIL\(rc=|Traceback|"
                                     r"AssertionError|AttributeError|SyntaxError",
                                     user))
        if exec_failed:
            verdict = {
                "approved": False,
                "feedback": "Acceptance execution failed — the change does not "
                            "satisfy the task's assertions.",
                "hints": ["Make the acceptance_check command exit 0: implement "
                          "exactly the symbols it asserts (hello/openapi_spec/"
                          "render_metrics) in app.py."],
                "suggested_approach": "Start from the previous working app.py "
                                      "and extend it instead of replacing it.",
            }
        else:
            verdict = {"approved": True, "feedback": "Execution passed and the "
                       "code matches the task.", "hints": []}
        reply = json.dumps(verdict)
        _log("gate2_code", system, user, reply)
        return reply

    # Gate-1 presence (false-positive check): the skeleton raises
    # NotImplementedError, so every claimed improvement is genuinely present.
    if "false-positive check" in system:
        reply = json.dumps({"verdict": "confirmed", "reason": "hello() "
                            "raises NotImplementedError; the claimed work is real."})
        _log("gate1_presence", system, user, reply)
        return reply

    # Gate-1 code (existence/presence)
    if "quality check" in system or "gate" in system.lower():
        reply = json.dumps({"present": True, "reason": "content present"})
        _log("gate1_code", system, user, reply)
        return reply

    reply = "APPROVED"
    _log("UNMATCHED", system, user, reply)
    return reply


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        messages = body.get("messages", [])
        system = "".join(m.get("content", "") + "\n" for m in messages if m.get("role") == "system")
        user = "".join(m.get("content", "") + "\n" for m in messages if m.get("role") == "user")
        content = build_reply(system, user)
        resp = {"model": body.get("model", "stub"),
                "message": {"role": "assistant", "content": content}, "done": True}
        payload = (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    open(LOG_PATH, "w", encoding="utf-8").close()
    server = ThreadingHTTPServer(("127.0.0.1", 11434), Handler)
    print("Code-mode adversarial stub listening on 127.0.0.1:11434")
    server.serve_forever()
