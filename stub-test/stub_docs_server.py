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
PLANNED = "/home/claude/docsrun/planned"
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



# Acceptance: детерминированная сверка утверждений README с реальным кодом.
ACCEPT_DOC = ("python3 -c \"t = open('README.md', encoding='utf-8').read(); "
              "import main; "
              "assert 'main.run' in t, 'entry point name'; "
              "assert 'execute' not in t, 'stale main.execute claim'; "
              "assert 'launch' not in t, 'invented main.launch claim'; "
              "assert 'records.py' not in t, 'deleted module still documented'; "
              "assert 'FETCHER_MODE' not in t, 'invented env var'; "
              "assert (',' in t and ';' in t), 'both separators documented'; "
              "assert main.run('a=1, b=2') == [('a','1'),('b','2')]\"")


def build_reply(system: str, user: str) -> str:
    if "You are a plan reviewer" in system:
        reply = "APPROVED"
        _log("plan_reviewer", system, user, reply)
        return reply

    # Архитектор docs-режима (реальная персона: documentation review)
    if "senior technical writer performing a documentation review" in system:
        reply = _plan()
        _log("architect_docs", system, user, reply)
        return reply
    if "senior software architect" in system:
        reply = _plan()
        _log("architect_docs(via code persona)", system, user, reply)
        return reply

    # Кодер docs-режима (technical writer)
    if "senior technical writer implementing a targeted documentation" in system:
        CODER_ATTEMPTS["D1"] = CODER_ATTEMPTS.get("D1", 0) + 1
        n = CODER_ATTEMPTS["D1"]
        if n == 1:
            content = _read("README_newlies.md")
            tag = "NEW LIES instead of fixing"
        elif n == 2:
            content = _read("README_mixed.md")
            tag = "MIXED-LANGUAGE correct facts"
        else:
            content = _read("README_clean.md")
            tag = "clean single-language"
        reply = json.dumps({"files": [{"path": "README.md", "content": content}]},
                           ensure_ascii=False)
        _log(f"coder_docs(D1 attempt={n} -> {tag})", system, user, reply)
        return reply

    # Gate-2 docs: честный, но без знания кода — верит связной прозе
    if "You are a documentation change validator" in system:
        exec_failed = bool(re.search(r"exit\s+code:\s*[1-9]|FAIL\(rc=|AssertionError",
                                     user))
        if exec_failed:
            verdict = {"approved": False,
                       "feedback": "Acceptance assertions on README claims failed.",
                       "hints": ["Section 'Использование': the entry point must "
                                 "be documented as main.run returning a list of "
                                 "tuples; drop invented APIs."],
                       "suggested_approach": "Derive every claim from main.py "
                                             "and fetcher.py as shown."}
        else:
            verdict = {"approved": True,
                       "feedback": "README claims match the code.", "hints": []}
        reply = json.dumps(verdict)
        _log("gate2_docs", system, user, reply)
        return reply

    # Gate-1 presence (docs persona)
    if "documentation reviewer performing a false-positive check" in system or \
       "false-positive check" in system:
        reply = json.dumps({"verdict": "confirmed",
                            "reason": "README documents main.execute/records.py/"
                                      "FETCHER_MODE — none of which exist."})
        _log("gate1_presence_docs", system, user, reply)
        return reply

    reply = "APPROVED"
    _log("UNMATCHED", system, user, reply)
    return reply


def _plan() -> str:
    tasks = [{
        "title": "D1: fix README.md lies against the actual code",
        "instruction": ("README.md documents APIs and modules that do not "
                        "exist (main.execute, records.py, FETCHER_MODE, "
                        "colon-only separators). Rewrite it so every claim "
                        "matches main.py and fetcher.py: entry point main.run, "
                        "list of tuples, separators ';' and ',', two modules, "
                        "DEBUG logging."),
        "target_files": ["README.md"],
        "acceptance_check": ACCEPT_DOC,
        "cited_location": {"file": "README.md", "symbol": None,
                           "line_start": 1, "line_end": 5},
    }]
    return json.dumps(tasks, ensure_ascii=False)


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
