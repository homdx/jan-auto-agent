import sys
from pathlib import Path
_here = Path(__file__).resolve().parent
_root = _here.parent if (_here.parent / "tools").exists() else _here
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

"""
STORY-3.1 -- Unit tests for PromptOptimizer.
Run with:  python tests/test_story_3_1.py
"""
import configparser, json, sys, urllib.error
from unittest.mock import MagicMock, patch

from tools.prompt_optimizer import PromptOptimizer, OPTIMIZER_META_PROMPT

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []

def check(label, condition):
    if condition:
        print(f"  {PASS}: {label}")
    else:
        print(f"  {FAIL}: {label}")
        _failures.append(label)

SAMPLE_PROMPT = "You are a validator. Return JSON with status and feedback."
SAMPLE_SUMMARY = {
    "total_runs": 10, "avg_iterations": 2.8,
    "json_parse_failure_rate": 0.4,
    "common_feedback": ["missing", "config_loader"],
    "worst_intent": "optimize",
}

def _mock_resp(text):
    raw = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    m = MagicMock()
    m.read.return_value = raw
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


print("\n[1] Constructor defaults")
po = PromptOptimizer()
check("model default",    po.model    == "qwen2.5-14b-instruct")
check("base_url default", po.base_url == "http://localhost:1337/v1")
check("api_key default",  po.api_key  == "jan")
check("timeout default",  po.timeout  == 120)

po2 = PromptOptimizer(model="x", base_url="http://y/v1", api_key="k", timeout=30)
check("custom model",   po2.model == "x")
check("custom timeout", po2.timeout == 30)


print("\n[2] Meta-prompt template")
rendered = OPTIMIZER_META_PROMPT.format(
    current_prompt=SAMPLE_PROMPT,
    failure_summary=json.dumps(SAMPLE_SUMMARY, indent=2),
)
check("contains current_prompt",        SAMPLE_PROMPT in rendered)
check("contains failure_summary JSON",  "worst_intent" in rendered)
check("prompt engineering instruction", "prompt engineering" in rendered.lower())
check("return only new prompt",         "Return only the new prompt" in rendered)


print("\n[3] Successful API response")
CAND = "You are an improved validator. Always return valid JSON."
with patch("urllib.request.urlopen", return_value=_mock_resp(CAND)):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)
check("returns candidate",       result == CAND)
check("differs from original",   result != SAMPLE_PROMPT)


print("\n[4] Whitespace trimming")
with patch("urllib.request.urlopen", return_value=_mock_resp("   \n" + CAND + "\n  ")):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)
check("whitespace stripped", result == CAND)


print("\n[5] HTTP error fallback")
def _http_error(*a, **kw):
    raise urllib.error.HTTPError("http://x", 500, "err", None, MagicMock(read=lambda: b""))
with patch("urllib.request.urlopen", side_effect=_http_error):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)
check("HTTP error -> current_prompt", result == SAMPLE_PROMPT)


print("\n[6] Generic exception fallback")
with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
    result = po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)
check("TimeoutError -> current_prompt", result == SAMPLE_PROMPT)


print("\n[7] agents.ini config")
cfg = configparser.ConfigParser()
cfg.read(str(_root / "agents.ini"))
check("[prompt_optimizer] exists",       cfg.has_section("prompt_optimizer"))
check("min_runs_before_optimize = 5",   cfg.getint("prompt_optimizer",   "min_runs_before_optimize",  fallback=-1) == 5)
check("trigger_avg_iterations in ini",  cfg.has_option("prompt_optimizer", "trigger_avg_iterations"))
check("trigger_avg_iterations = 2.0",   cfg.getfloat("prompt_optimizer",  "trigger_avg_iterations",   fallback=-1) == 2.0)
check("trigger_json_fail_rate = 0.30",  cfg.getfloat("prompt_optimizer",  "trigger_json_fail_rate",   fallback=-1) == 0.30)
check("enabled = true",                  cfg.getboolean("prompt_optimizer","enabled",                  fallback=False))


print("\n[8] Request shape")
captured = {}
def _cap(req, timeout, context=None):
    captured["url"]    = req.full_url
    captured["method"] = req.method
    captured["body"]   = json.loads(req.data.decode())
    return _mock_resp(CAND)

with patch("urllib.request.urlopen", side_effect=_cap):
    po.generate_candidate("validator_agent", SAMPLE_PROMPT, SAMPLE_SUMMARY)

check("POST to /chat/completions",
      captured["url"].endswith("/chat/completions") and captured["method"] == "POST")
check("model in body",          captured["body"]["model"] == po.model)
check("one user message",       len(captured["body"]["messages"]) == 1)
check("current_prompt in msg",  SAMPLE_PROMPT in captured["body"]["messages"][0]["content"])
check("failure_summary in msg", "worst_intent" in captured["body"]["messages"][0]["content"])


print()
if _failures:
    print(f"  {len(_failures)} FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for ln in open(__file__) if ln.strip().startswith("check("))
    print(f"  All {total} checks passed.")
