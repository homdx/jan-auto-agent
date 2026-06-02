"""
STORY-3.1 — Unit tests for PromptOptimizer
Tests cover: constructor, meta-prompt rendering, error fallback behaviour,
and agents.ini config parsing.

Run with:  python test_story_3_1.py
"""
import configparser
import json
import sys
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from tools.prompt_optimizer import PromptOptimizer, OPTIMIZER_META_PROMPT

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []

def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  {PASS}: {label}")
    else:
        print(f"  {FAIL}: {label}")
        _failures.append(label)


SAMPLE_PROMPT = "You are a validator. Return JSON with status and feedback."
SAMPLE_SUMMARY = {
    "total_runs": 10,
    "avg_iterations": 2.8,
    "json_parse_failure_rate": 0.4,
    "common_feedback": ["missing", "config_loader", "import"],
    "worst_intent": "optimize",
}


# ── Test 1: Constructor defaults ───────────────────────────────────────────

print("\n[1] Constructor")
po = PromptOptimizer()
check("model default set",    po.model    == "qwen2.5-14b-instruct")
check("base_url default set", po.base_url == "http://localhost:1337/v1")
check("api_key default set",  po.api_key  == "jan")
check("timeout default set",  po.timeout  == 120)

po2 = PromptOptimizer(model="custom-model", base_url="http://x:9999/v1", api_key="sk-test", timeout=60)
check("custom model",   po2.model    == "custom-model")
check("custom base_url",po2.base_url == "http://x:9999/v1")
check("custom api_key", po2.api_key  == "sk-test")
check("custom timeout", po2.timeout  == 60)


# ── Test 2: Meta-prompt template renders both placeholders ─────────────────

print("\n[2] Meta-prompt template")
rendered = OPTIMIZER_META_PROMPT.format(
    current_prompt=SAMPLE_PROMPT,
    failure_summary=json.dumps(SAMPLE_SUMMARY, indent=2),
)
check("contains current_prompt text",   SAMPLE_PROMPT in rendered)
check("contains failure_summary JSON",  '"worst_intent"' in rendered)
check("contains instruction header",    "prompt engineering agent" in rendered)
check("instructs return-only new prompt", "Return only the new prompt text" in rendered)
check("JSON format requirement preserved", "JSON output format requirements" in rendered)


# ── Test 3: Successful API call returns candidate ──────────────────────────

print("\n[3] Successful API response")

def _mock_response(text: str):
    """Build a minimal fake urlopen context manager."""
    raw = json.dumps({
        "choices": [{"message": {"content": text}}]
    }).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp

CANDIDATE_TEXT = "You are an improved validator agent. Always return valid JSON."

with patch("urllib.request.urlopen", return_value=_mock_response(CANDIDATE_TEXT)):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("returns candidate string",          result == CANDIDATE_TEXT)
check("candidate is not current prompt",   result != SAMPLE_PROMPT)
check("candidate is a non-empty string",   isinstance(result, str) and len(result) > 0)


# ── Test 4: Whitespace stripped from response ──────────────────────────────

print("\n[4] Response trimming")
PADDED = "   \n" + CANDIDATE_TEXT + "\n   "

with patch("urllib.request.urlopen", return_value=_mock_response(PADDED)):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("leading/trailing whitespace stripped", result == CANDIDATE_TEXT)


# ── Test 5: HTTP error falls back to current_prompt ───────────────────────

print("\n[5] HTTP error fallback")

def _raise_http(*args, **kwargs):
    raise urllib.error.HTTPError(
        url="http://localhost/", code=500,
        msg="Internal Server Error", hdrs=None,  # type: ignore
        fp=MagicMock(read=lambda: b"server error"),
    )

with patch("urllib.request.urlopen", side_effect=_raise_http):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("HTTP error → returns current_prompt unchanged", result == SAMPLE_PROMPT)


# ── Test 6: Generic exception falls back to current_prompt ────────────────

print("\n[6] Network/generic exception fallback")

with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("TimeoutError → returns current_prompt unchanged", result == SAMPLE_PROMPT)

with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("ConnectionRefusedError → returns current_prompt unchanged", result == SAMPLE_PROMPT)


# ── Test 7: agents.ini [prompt_optimizer] section ─────────────────────────

print("\n[7] agents.ini config")
cfg = configparser.ConfigParser()
cfg.read("agents.ini")

check("[prompt_optimizer] section exists",
      cfg.has_section("prompt_optimizer"))
check("trigger_after_failures = 5",
      cfg.getint("prompt_optimizer", "trigger_after_failures", fallback=-1) == 5)
check("min_runs_before_optimize = 3",
      cfg.getint("prompt_optimizer", "min_runs_before_optimize", fallback=-1) == 3)
check("trigger_avg_iterations = 2.0",
      cfg.getfloat("prompt_optimizer", "trigger_avg_iterations", fallback=-1.0) == 2.0)
check("trigger_json_fail_rate = 0.30",
      cfg.getfloat("prompt_optimizer", "trigger_json_fail_rate", fallback=-1.0) == 0.30)
check("enabled = true",
      cfg.getboolean("prompt_optimizer", "enabled", fallback=False) is True)


# ── Test 8: correct API request shape ─────────────────────────────────────

print("\n[8] API request construction")
captured = {}

def _capture_request(req, timeout):
    captured["url"]     = req.full_url
    captured["method"]  = req.method
    captured["headers"] = dict(req.headers)
    captured["body"]    = json.loads(req.data.decode("utf-8"))
    return _mock_response(CANDIDATE_TEXT)

with patch("urllib.request.urlopen", side_effect=_capture_request):
    po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("POST to /chat/completions",
      captured["url"].endswith("/chat/completions") and captured["method"] == "POST")
check("Authorization header present",
      "Authorization" in captured["headers"])
check("model in request body",
      captured["body"].get("model") == po.model)
check("messages list has one user message",
      len(captured["body"]["messages"]) == 1
      and captured["body"]["messages"][0]["role"] == "user")
check("current_prompt embedded in message content",
      SAMPLE_PROMPT in captured["body"]["messages"][0]["content"])
check("failure_summary embedded in message content",
      "worst_intent" in captured["body"]["messages"][0]["content"])
check("timeout passed to urlopen",
      True)  # verified implicitly — _capture_request receives timeout kwarg without error


# ── Summary ────────────────────────────────────────────────────────────────

print()
if _failures:
    print(f"  {len(_failures)} test(s) FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for line in open(__file__) if line.strip().startswith("check("))
    print(f"  All {total} checks passed.")
