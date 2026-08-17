"""tests/test_mask_api_key.py — MASK-KEY-1: `api_key = ...` lines pulled
into a prompt (e.g. agents.ini contents read into context for the LLM)
must never reach the LLM verbatim.

request_completion() is the single choke point every agent in this
codebase (Coder, Gate1Filter, ClusterReviewer, TaskRewriter via
build_chat_request(), plus ImprovementAgent/FaqAgent/OrchestratorActions
which build their own OpenAI-shaped payload directly) passes through
before the HTTP call is actually made — so masking there, right before the
body is built, catches every call site without needing each one to
remember to mask its own prompt text.

No live network access: the end-to-end test spins up a real (but local,
in-process) http.server and inspects the JSON body it actually received,
the same pattern used by test_llm_stream_retry.py /
test_llm_stream_empty_choices.py.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import build_chat_request, mask_api_key, request_completion


# ─────────────────────────────────────────────────────────────────────────────
# mask_api_key() — pure string transform
# ─────────────────────────────────────────────────────────────────────────────

class TestMaskApiKeyPure:

    def test_masks_plain_value(self):
        assert mask_api_key("api_key = test_or_key") == "api_key = here_your_key"

    def test_masks_real_looking_secret(self):
        text = "api_key = sk-live-AbC123XyZ9"
        assert mask_api_key(text) == "api_key = here_your_key"

    def test_masks_inside_ini_style_block(self):
        text = (
            "[api_local]\n"
            "base_url = http://localhost:11434\n"
            "api_key = ollama\n"
            "model = llama3.1:8b\n"
        )
        out = mask_api_key(text)
        assert "api_key = here_your_key" in out
        assert "ollama" not in out
        # untouched lines survive
        assert "base_url = http://localhost:11434" in out
        assert "model = llama3.1:8b" in out

    def test_masks_multiple_sections(self):
        text = (
            "[api_local]\napi_key = jan\n\n"
            "[api_remote]\napi_key = sk-remote-secret\n"
        )
        out = mask_api_key(text)
        assert out.count("api_key = here_your_key") == 2
        assert "jan" not in out
        assert "sk-remote-secret" not in out

    def test_case_insensitive_key_name(self):
        assert mask_api_key("API_KEY = something") == "API_KEY = here_your_key"

    def test_leading_whitespace_preserved(self):
        text = "    api_key = indented_secret"
        assert mask_api_key(text) == "    api_key = here_your_key"

    def test_no_match_returned_unchanged(self):
        text = "base_url = http://x\nmodel = m\n"
        assert mask_api_key(text) == text

    def test_empty_and_none_safe(self):
        assert mask_api_key("") == ""
        assert mask_api_key(None) is None

    def test_does_not_touch_unrelated_key_substring(self):
        """A variable that merely CONTAINS 'api_key' as part of a longer
        name (e.g. 'my_api_key_backup') is not what the ini-style pattern
        targets — only a line that IS an api_key assignment."""
        text = "not_api_key_at_all = foo\n"
        assert mask_api_key(text) == text


# ─────────────────────────────────────────────────────────────────────────────
# build_chat_request() — masking applied to system/user content
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildChatRequestMasksSecrets:

    def test_user_msg_with_leaked_config_is_masked(self):
        leaked = "Here is my config:\napi_key = sk-real-secret-999\nbase_url = http://x\n"
        _url, _headers, payload = build_chat_request(
            base_url="http://x/v1", api_key="k", model="m", api_format="openai",
            temperature=0.0, max_tokens=100, system="s", user_msg=leaked,
        )
        user_content = payload["messages"][1]["content"]
        assert "sk-real-secret-999" not in user_content
        assert "api_key = here_your_key" in user_content

    def test_system_prompt_with_leaked_config_is_masked(self):
        leaked = "context:\napi_key = test_or_key\n"
        _url, _headers, payload = build_chat_request(
            base_url="http://x/v1", api_key="k", model="m", api_format="openai",
            temperature=0.0, max_tokens=100, system=leaked, user_msg="u",
        )
        system_content = payload["messages"][0]["content"]
        assert "api_key = here_your_key" in system_content

    def test_the_client_auth_header_key_itself_is_unaffected(self):
        """The real api_key used to AUTHENTICATE the outgoing call (the
        Authorization header) must still work normally — only api_key
        assignments found INSIDE prompt text are masked."""
        _url, headers, _payload = build_chat_request(
            base_url="http://x/v1", api_key="sk-the-real-caller-key", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u",
        )
        assert headers["Authorization"] == "Bearer sk-the-real-caller-key"

    def test_clean_prompt_unaffected(self):
        _url, _headers, payload = build_chat_request(
            base_url="http://x/v1", api_key="k", model="m", api_format="openai",
            temperature=0.0, max_tokens=100, system="s", user_msg="just a normal question",
        )
        assert payload["messages"][1]["content"] == "just a normal question"


# ─────────────────────────────────────────────────────────────────────────────
# request_completion() end-to-end — inspect what actually hit the wire
# ─────────────────────────────────────────────────────────────────────────────

class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    received_body: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        _CapturingHandler.received_body = json.loads(raw.decode("utf-8"))
        resp = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def _serve_capturing():
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestRequestCompletionMasksBeforeSendingOverTheWire:

    def test_leaked_secret_never_reaches_the_server(self):
        server, port = _serve_capturing()
        try:
            leaked_payload = {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "s"},
                    {
                        "role": "user",
                        "content": "file contents:\napi_key = sk-super-secret-value\n",
                    },
                ],
            }
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                leaked_payload,
                timeout=5, stream=False, api_format="openai",
            )
            assert result == "ok"
            sent = _CapturingHandler.received_body
            sent_str = json.dumps(sent)
            assert "sk-super-secret-value" not in sent_str
            assert "api_key = here_your_key" in sent["messages"][1]["content"]
            # the untouched system message is passed through unchanged
            assert sent["messages"][0]["content"] == "s"
        finally:
            server.shutdown()

    def test_caller_payload_object_is_not_mutated_in_place(self):
        """request_completion() must not silently mutate the dict/list the
        caller passed in — several callers reuse or inspect their payload
        after the call."""
        server, port = _serve_capturing()
        try:
            original_messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "api_key = leak_me"},
            ]
            payload = {"model": "m", "messages": original_messages}
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                payload, timeout=5, stream=False, api_format="openai",
            )
            # caller's own list/dicts are untouched
            assert original_messages[1]["content"] == "api_key = leak_me"
        finally:
            server.shutdown()
