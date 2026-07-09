#!/usr/bin/env python3
"""Adversarial stub — SELF-HOSTING pilot: agent adds a helper to its own utils.

Choreography:
  S1 attempt 1 -> CODE WITHOUT TESTS (only utils.py) -> the new deterministic
      tests-mandate gate must REJECT before any execution round is spent.
  S1 attempt 2 -> code + tests, but the implementation is broken (unit
      subtraction skipped) -> pytest exit 1 -> executor FAIL, gate2 hints.
  S1 attempt 3 -> correct implementation + tests -> PASS.

The write target is the agent's own tools/auto/utils.py, which legitimately
contains os.unlink inside atomic_write_text — without the pre-existing-pattern
grandfathering in the safety scanner every attempt would be blocked (proven
live in run #1 of this pilot).
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLANNED = os.path.join(HERE, "planned")
TARGET = os.path.join(HERE, "target")
LOG_PATH = os.path.join(HERE, "stub_llm.log")

CODER_ATTEMPTS = {}

ACCEPT = "python3 -m pytest tests/test_human_duration.py -q"


def _log(kind, system, user, reply):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n===== {kind} =====\n--- system (first 200) ---\n{system[:200]}\n"
                f"--- user (first 800) ---\n{user[:800]}\n--- reply (first 300) ---\n{reply[:300]}\n")


def _utils_with(patch_name: str) -> str:
    base = open(os.path.join(TARGET, "tools/auto/utils.py"), encoding="utf-8").read()
    cut = base.find("\ndef human_duration(")
    if cut != -1:
        base = base[:cut].rstrip() + "\n"
    patch = open(os.path.join(PLANNED, patch_name), encoding="utf-8").read()
    return base.rstrip() + "\n\n\n" + patch


def build_reply(system: str, user: str) -> str:
    if "You are a plan reviewer" in system:
        reply = "APPROVED"
        _log("plan_reviewer", system, user, reply)
        return reply

    if "senior software architect" in system:
        tasks = [{
            "title": "S1: add human_duration() helper to tools/auto/utils.py",
            "instruction": ("Add human_duration(seconds) to tools/auto/utils.py "
                            "formatting seconds as '2m 5s'/'1h 2m 5s' etc., and "
                            "add tests/test_human_duration.py covering sub-second, "
                            "minutes, hours, days, zero, negative and exact-minute "
                            "cases. Acceptance runs those tests."),
            "target_files": ["tools/auto/utils.py", "tests/test_human_duration.py"],
            "acceptance_check": ACCEPT,
            "cited_location": {"file": "tools/auto/utils.py",
                               "symbol": "atomic_write_text",
                               "line_start": None, "line_end": None},
        }]
        reply = json.dumps(tasks, ensure_ascii=False)
        _log("architect_selfhost", system, user, reply)
        return reply

    if "senior software engineer implementing a targeted code improvement" in system:
        CODER_ATTEMPTS["S1"] = CODER_ATTEMPTS.get("S1", 0) + 1
        n = CODER_ATTEMPTS["S1"]
        if n == 1:
            files = [{"path": "tools/auto/utils.py",
                      "content": _utils_with("utils_patch_correct.py")}]
            tag = "CODE WITHOUT TESTS"
        elif n == 2:
            files = [{"path": "tools/auto/utils.py",
                      "content": _utils_with("utils_patch_broken.py")},
                     {"path": "tests/test_human_duration.py",
                      "content": open(os.path.join(PLANNED, "test_human_duration.py"),
                                      encoding="utf-8").read()}]
            tag = "BROKEN IMPL + tests"
        else:
            files = [{"path": "tools/auto/utils.py",
                      "content": _utils_with("utils_patch_correct.py")},
                     {"path": "tests/test_human_duration.py",
                      "content": open(os.path.join(PLANNED, "test_human_duration.py"),
                                      encoding="utf-8").read()}]
            tag = "clean impl + tests"
        reply = json.dumps({"files": files}, ensure_ascii=False)
        _log(f"coder_selfhost(S1 attempt={n} -> {tag})", system, user, reply)
        return reply

    if "You are a code-change validator" in system:
        # Судим ТОЛЬКО по exit-коду текущего прогона; слова "failed" из
        # истории фидбека прошлых попыток не должны заражать вердикт.
        failed = bool(re.search(r"exit\s+code:\s*[1-9]", user))
        verdict = ({"approved": False,
                    "feedback": "human_duration tests failed.",
                    "hints": ["'2m 5s' means remaining seconds must be reduced "
                              "after each unit — subtract with s %= size."],
                    "suggested_approach": "Fix the unit-subtraction loop."}
                   if failed else
                   {"approved": True, "feedback": "Tests pass, change matches "
                    "the task.", "hints": []})
        reply = json.dumps(verdict)
        _log("gate2_selfhost", system, user, reply)
        return reply

    if "false-positive check" in system:
        reply = json.dumps({"verdict": "confirmed",
                            "reason": "utils.py has no human_duration yet."})
        _log("gate1_presence", system, user, reply)
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
    print("Selfhost adversarial stub listening on 127.0.0.1:11434")
    server.serve_forever()
