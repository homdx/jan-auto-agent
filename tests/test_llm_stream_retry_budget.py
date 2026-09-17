"""tests/test_llm_stream_retry_budget.py — RUN-10: the HTTP retry budget of
every auto-mode LLM call is config-driven.

Before this ticket the inner per-request retry budget of
``tools.llm_stream.request_completion`` (60 extra attempts, 10 s apart, a
Retry-After cap of 180 s, AUTO-RATE-1) was the only budget every auto-mode
caller had: architect, Gate 1, coder, Gate 2 validator, story bible and
summary memory all called it and stopped there, so a provider outage in the
plan phase cost up to 60 x 10 s inside one call and the operator could not
see, shorten or lengthen it. The only ini keys with those names sat under
``[collect]`` and changed nothing there.

This file pins down:

* ``retry_kwargs_from_config`` — one reader for ``[loop] error_retries`` /
  ``error_retry_wait_sec`` / ``max_retry_after_sec``: the defaults when the
  keys are absent, the RUN-9 malformed-value warning when one of them is not
  a number (that key falls back, the other two are still honoured), and the
  clamp of a negative value to zero. The request_completion signature
  defaults are byte-for-byte the AUTO-RATE-1 values.
* All eight auto-mode call sites pass the resolved budget through, and none
  of them reads ``[collect]``.
* The two budgets nest: Gate 1's ``[gate1] llm_call_retry_max`` retries wrap
  ``request_completion``'s own retry loop, they do not replace it, and a
  429 storm with no Retry-After ends the candidate as ``unknown`` (RUN-5).
* The collect Pass B summarizer keeps its own ``[collect]`` budget.
* A stubbed ``--auto`` run logs the budget in force exactly once, into
  run.log.

Nothing here opens a socket: the transport is either a recording stub or a
function that raises an HTTP 429 in-memory.
"""

from __future__ import annotations

import configparser
import io
import json
import logging
import types
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as llm  # noqa: E402
from tools.llm_stream import (  # noqa: E402
    DEFAULT_ERROR_RETRIES,
    DEFAULT_ERROR_RETRY_WAIT_SEC,
    DEFAULT_MAX_RETRY_AFTER_SEC,
    request_completion,
    retry_kwargs_from_config,
)

BASE_URL = "http://localhost:1337/v1"
MODEL = "test-model"
API_KEY = "test"

_BUDGET = {"error_retries": "1", "error_retry_wait_sec": "0"}
_DEFAULTS = {
    "error_retries": DEFAULT_ERROR_RETRIES,
    "error_retry_wait_sec": DEFAULT_ERROR_RETRY_WAIT_SEC,
    "max_retry_after_sec": DEFAULT_MAX_RETRY_AFTER_SEC,
}


def _cfg(extra_loop: dict | None = None) -> configparser.ConfigParser:
    """A config that works without a live provider.

    ``extra_loop`` replaces the [loop] block entirely, so a test can prove the
    behaviour of a config that has no [loop] retry keys at all.
    """
    loop = {"timeout_seconds": "10"}
    if extra_loop is not None:
        loop.update(extra_loop)
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": BASE_URL, "api_key": API_KEY,
                      "model": MODEL, "api_format": "openai",
                      "num_ctx": "4096"},
        "architect": {"temperature": "0.2", "max_tokens": "512"},
        "gate1":     {"temperature": "0.0", "max_tokens": "512",
                      "skip_llm": "false"},
        "coder":     {"temperature": "0.2", "max_tokens": "4096"},
        "loop":      loop,
    })
    return cfg


class Recorder:
    """Records the kwargs of every request_completion() call it gets."""

    def __init__(self, *replies: str):
        self.replies = list(replies) or [""]
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        idx = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[idx]


_RETRY_KEYS = ("error_retries", "error_retry_wait_sec", "max_retry_after_sec")


def _retry_subset(kwargs: dict) -> dict:
    """Just the three retry kwargs, so the assertion is about the budget."""
    return {key: kwargs[key] for key in _RETRY_KEYS}


# ─────────────────────────────────────────────────────────────────────────────
# 1. The one reader
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryKwargsFromConfig:
    """`retry_kwargs_from_config` — the single reader for the [loop] budget."""

    def test_absent_config_gives_the_library_defaults(self):
        assert retry_kwargs_from_config(None) == _DEFAULTS

    def test_config_without_the_keys_gives_the_library_defaults(self):
        assert retry_kwargs_from_config(_cfg()) == _DEFAULTS

    def test_empty_config_parser_gives_the_library_defaults(self):
        assert retry_kwargs_from_config(configparser.ConfigParser()) == _DEFAULTS

    def test_all_three_keys_are_read(self):
        cfg = _cfg({"error_retries": "1", "error_retry_wait_sec": "0",
                    "max_retry_after_sec": "30"})
        assert retry_kwargs_from_config(cfg) == {
            "error_retries": 1,
            "error_retry_wait_sec": 0.0,
            "max_retry_after_sec": 30.0,
        }

    def test_absent_section_is_the_same_as_absent_keys(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("loop")
        cfg.set("loop", "timeout_seconds", "10")
        assert retry_kwargs_from_config(cfg) == _DEFAULTS

    def test_negative_values_clamp_to_zero(self):
        cfg = _cfg({"error_retries": "-5", "error_retry_wait_sec": "-1",
                    "max_retry_after_sec": "-180"})
        assert retry_kwargs_from_config(cfg) == {
            "error_retries": 0,
            "error_retry_wait_sec": 0.0,
            "max_retry_after_sec": 0.0,
        }

    def test_negative_clamp_keeps_the_types(self):
        # max(0, 0.0) returns the int 0; the float knobs must stay floats so a
        # caller can't be handed a different type for the same knob.
        cfg = _cfg({"error_retries": "-5", "error_retry_wait_sec": "0",
                    "max_retry_after_sec": "0"})
        out = retry_kwargs_from_config(cfg)
        assert isinstance(out["error_retries"], int)
        assert isinstance(out["error_retry_wait_sec"], float)
        assert isinstance(out["max_retry_after_sec"], float)

    def test_malformed_key_warns_and_falls_back_only_for_that_key(self, caplog):
        cfg = _cfg({"error_retries": "x", "error_retry_wait_sec": "0",
                    "max_retry_after_sec": "30"})
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            out = retry_kwargs_from_config(cfg)
        assert out == {
            "error_retries": DEFAULT_ERROR_RETRIES,
            "error_retry_wait_sec": 0.0,
            "max_retry_after_sec": 30.0,
        }
        messages = [r.message for r in caplog.records]
        assert any("config [loop] error_retries is malformed" in m and
                   "60" in m for m in messages)
        assert not any("error_retry_wait_sec is malformed" in m for m in messages)
        assert not any("max_retry_after_sec is malformed" in m for m in messages)

    @pytest.mark.parametrize("key", ["error_retries", "error_retry_wait_sec",
                                     "max_retry_after_sec"])
    def test_each_key_is_guarded_independently(self, caplog, key):
        cfg = _cfg({key: "not-a-number"})
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            out = retry_kwargs_from_config(cfg)
        assert out == _DEFAULTS
        assert any(f"config [loop] {key} is malformed" in r.message
                   for r in caplog.records)

    def test_a_broken_config_artifact_degrades_to_the_defaults(self):
        # Fail-open: something that is not a ConfigParser must not raise into a
        # run — every caller reads this once in its constructor.
        class Broken:
            def get(self, *args, **kwargs):
                raise OSError("no such file")

        assert retry_kwargs_from_config(Broken()) == _DEFAULTS

    def test_the_section_is_a_parameter(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"other": {"error_retries": "3"}})
        assert retry_kwargs_from_config(cfg, section="other")["error_retries"] == 3
        assert retry_kwargs_from_config(cfg)["error_retries"] == _DEFAULTS["error_retries"]


class TestSignatureDefaultsDidNotMove:
    """The out-of-scope guard: 60 / 10 / 180 is still what a config without the
    keys gets, and it is still the value request_completion falls back to."""

    def test_request_completion_defaults_are_the_autorate1_values(self):
        import inspect

        params = inspect.signature(request_completion).parameters
        assert params["error_retries"].default == 60
        assert params["error_retry_wait_sec"].default == 10.0
        assert params["max_retry_after_sec"].default == 180.0

    def test_the_named_defaults_match_the_signature(self):
        import inspect

        params = inspect.signature(request_completion).parameters
        assert DEFAULT_ERROR_RETRIES == params["error_retries"].default
        assert DEFAULT_ERROR_RETRY_WAIT_SEC == params["error_retry_wait_sec"].default
        assert DEFAULT_MAX_RETRY_AFTER_SEC == params["max_retry_after_sec"].default


# ─────────────────────────────────────────────────────────────────────────────
# 2. Every auto-mode call site passes the budget through
# ─────────────────────────────────────────────────────────────────────────────

def _drive_architect(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.architect import ClusterReviewer
    from tools.auto.repo_ingest import RepoCluster

    (tmp_path / "README.md").write_text("# doc\n", encoding="utf-8")
    cluster = RepoCluster(name="docs", patterns=["*.md"], files=["README.md"])
    reviewer = ClusterReviewer(config=cfg, base_url=BASE_URL, api_key=API_KEY,
                               model=MODEL, api_format="openai",
                               verify_ssl=False, task_mode="code")
    reviewer.review_clusters([cluster], tmp_path, goal="improve this file")


def _drive_task_rewriter(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.architect import TaskRewriter

    rewriter = TaskRewriter(config=cfg, base_url=BASE_URL, api_key=API_KEY,
                            model=MODEL, api_format="openai", verify_ssl=False)
    task = {"id": "RUN10-T1", "title": "T", "instruction": "I",
            "target_files": ["f.py"], "acceptance_check": "true"}
    rewriter.rewrite(task, failure_history=["round 1: failed"])


def _drive_gate1_presence(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.architect import CandidateTask, CitedLocation
    from tools.auto.gate1_filter import Gate1Filter

    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "c0.py").write_text(
        "def parse_config_0(raw):\n    return raw\n", encoding="utf-8")
    candidate = CandidateTask(
        title="candidate 0",
        instruction="fix the problem described in the claim",
        target_files=["tools/c0.py"],
        acceptance_check="python -m pytest tests -q",
        cited_location=CitedLocation(file="tools/c0.py", symbol="parse_config_0"),
        cluster="agents",
    )
    filt = Gate1Filter(config=cfg, base_url=BASE_URL, api_key=API_KEY,
                       model=MODEL, api_format="openai", verify_ssl=False)
    filt._check_presence(
        candidate, "def parse_config_0(raw):\n    return raw\n",
        base_dir=tmp_path, _sleep_fn=lambda s: None,
    )


def _drive_coder(cfg, tmp_path, rec: Recorder) -> None:
    """Both coder call sites: the main call and the context-probe re-call."""
    from tools.auto.coder import Coder

    (tmp_path / "module.py").write_text("x = 1\n", encoding="utf-8")
    coder = Coder(config=cfg, base_url=BASE_URL, api_key=API_KEY, model=MODEL,
                  api_format="openai", verify_ssl=False)

    def _fetch_needed(*args, **kwargs):
        return "### dep: SomeHelper  (from module.py)\nx = 1\n"

    # Bypass the SearchAgent: this test is about the retry kwargs, not search.
    coder._fetch_needed = _fetch_needed
    task = {
        "id": "RUN10-C1", "title": "Smart context test",
        "instruction": "Fix the bug.",
        "target_files": ["module.py"],
        "acceptance_check": "python -m pytest tests/",
        "status": "in_progress", "round": 0, "attempt": 0,
        "dependencies": [],
        "cited_locations": [{"file": "module.py", "symbol": "MyClass",
                             "line_start": None, "line_end": None}],
    }
    coder.generate(task, tmp_path)


def _drive_gate2_validator(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.inner_loop import LLMGate2Validator

    @dataclass
    class FakeExecResult:
        passed: bool = True
        exit_code: int = 0
        stdout: str = "ok"
        stderr: str = ""
        traceback: str = ""
        timed_out: bool = False

    @dataclass
    class FakeCoderResult:
        succeeded: bool = True
        files_written: list = field(default_factory=lambda: ["f.py"])
        files_skipped: list = field(default_factory=list)
        error: str = ""
        raw_response: str = ""

    validator = LLMGate2Validator(base_url=BASE_URL, model=MODEL,
                                  api_format="openai", config=cfg)
    task = {"id": "RUN10-V1", "title": "t", "instruction": "x",
            "target_files": ["f.py"], "acceptance_check": "pytest -q"}
    validator.approve(task, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)


def _drive_story_bible(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.story_bible import make_story_bible

    bible = make_story_bible(cfg, base_url=BASE_URL, api_key=API_KEY,
                             model=MODEL, api_format="openai",
                             base_dir=str(tmp_path))
    assert bible is not None
    bible._llm("system", "user")


def _drive_summary_memory(cfg, tmp_path, rec: Recorder) -> None:
    from tools.auto.summary_memory import make_summary_memory

    mem = make_summary_memory(cfg, base_dir=str(tmp_path), task_mode="creative")
    mem._llm("system", "user")


_SITES = [
    ("architect.review_clusters", _drive_architect, (
        '[{"title": "T", "instruction": "I", "target_files": ["README.md"], '
        '"acceptance_check": "true", "cited_location": {"file": "README.md"}}]',
    )),
    ("architect.TaskRewriter.rewrite", _drive_task_rewriter, (
        '{"title": "T2", "instruction": "I2", "acceptance_check": "true"}',
    )),
    ("gate1._check_presence", _drive_gate1_presence, (
        '{"verdict": "confirmed", "evidence": "def ", "reason": "ok"}',
    )),
    ("coder.generate", _drive_coder, (
        '{"files": [{"path": "module.py", "content": "class MyClass: pass\\n"}], '
        '"missing_context": ["SomeHelper"]}',
        '{"files": [{"path": "module.py", "content": "class MyClass: pass\\n"}]}',
    )),
    ("gate2.LLMGate2Validator.approve", _drive_gate2_validator, (
        '{"approved": true, "feedback": ""}',
    )),
    ("story_bible._llm", _drive_story_bible, ("- a fact\n",)),
    ("summary_memory._llm", _drive_summary_memory, ("APPROVED",)),
]


class TestEveryAutoModeCallSitePassesTheBudget:
    """Parametrised over the eight call sites: the resolved [loop] budget
    arrives at request_completion, and a config without the keys still gets the
    AUTO-RATE-1 defaults.

    coder.generate is one site that makes two calls (main + context probe); the
    assertion covers every call it makes, so both are checked.
    """

    @pytest.mark.parametrize("name,drive,replies", _SITES,
                             ids=[s[0] for s in _SITES])
    def test_the_loop_budget_reaches_the_call(self, tmp_path, monkeypatch,
                                              name, drive, replies):
        cfg = _cfg(dict(_BUDGET))
        rec = Recorder(*replies)
        monkeypatch.setattr(llm, "request_completion", rec)
        drive(cfg, tmp_path, rec)

        assert rec.calls, f"{name}: request_completion was never called"
        for kwargs in rec.calls:
            subset = _retry_subset(kwargs)
            assert subset == {
                "error_retries": 1,
                "error_retry_wait_sec": 0,
                "max_retry_after_sec": _DEFAULTS["max_retry_after_sec"],
            }, f"{name}: {subset}"

    @pytest.mark.parametrize("name,drive,replies", _SITES,
                             ids=[s[0] for s in _SITES])
    def test_no_loop_keys_still_gets_the_library_defaults(self, tmp_path,
                                                          monkeypatch, name,
                                                          drive, replies):
        cfg = _cfg({})  # [loop] with only timeout_seconds
        rec = Recorder(*replies)
        monkeypatch.setattr(llm, "request_completion", rec)
        drive(cfg, tmp_path, rec)

        assert rec.calls, f"{name}: request_completion was never called"
        for kwargs in rec.calls:
            assert _retry_subset(kwargs) == _DEFAULTS, f"{name}: {kwargs}"

    @pytest.mark.parametrize("name,drive,replies", _SITES,
                             ids=[s[0] for s in _SITES])
    def test_a_collect_budget_is_never_read_by_auto_mode(self, tmp_path,
                                                         monkeypatch, name,
                                                         drive, replies):
        # [collect] still owns Pass B's numbers; the auto-mode callers must not
        # pick them up just because the names are identical.
        cfg = _cfg(dict(_BUDGET))
        cfg.add_section("collect")
        cfg.set("collect", "error_retries", "7")
        cfg.set("collect", "error_retry_wait_sec", "60")
        cfg.set("collect", "max_retry_after_sec", "180")
        rec = Recorder(*replies)
        monkeypatch.setattr(llm, "request_completion", rec)
        drive(cfg, tmp_path, rec)

        assert rec.calls
        assert {c["error_retries"] for c in rec.calls} == {1}


class TestCollectPassBKeepsItsOwnBudget:
    """[collect] stays Pass B's: error_retries = 5 still reaches the summarizer,
    and a [loop] budget never does."""

    def test_collect_budget_reaches_pass_b(self, monkeypatch):
        from tools.collect.summarizer import make_summarizer_call

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {"base_url": BASE_URL, "api_key": API_KEY,
                          "model": MODEL, "api_format": "ollama",
                          "num_ctx": "4096"},
            "collect": {"max_tokens": "256", "temperature": "0.1",
                        "think": "false", "error_retries": "5",
                        "error_retry_wait_sec": "60",
                        "max_retry_after_sec": "180"},
            "loop": {"timeout_seconds": "10", "error_retries": "1",
                     "error_retry_wait_sec": "0"},
        })
        rec = Recorder("reply-text")
        monkeypatch.setattr(llm, "request_completion", rec)

        llm_call = make_summarizer_call(cfg, task_mode="code")
        assert llm_call("system prompt", "user prompt") == "reply-text"

        assert len(rec.calls) == 1
        kwargs = _retry_subset(rec.calls[0])
        assert kwargs == {"error_retries": 5, "error_retry_wait_sec": 60,
                          "max_retry_after_sec": 180}

    def test_pass_b_defaults_are_still_collects(self, monkeypatch):
        from tools.collect.summarizer import make_summarizer_call

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {"base_url": BASE_URL, "api_key": API_KEY,
                          "model": MODEL, "api_format": "ollama",
                          "num_ctx": "4096"},
            "loop": {"error_retries": "1", "error_retry_wait_sec": "0"},
        })
        rec = Recorder("reply-text")
        monkeypatch.setattr(llm, "request_completion", rec)

        make_summarizer_call(cfg, task_mode="code")("system", "user")

        kwargs = rec.calls[0]
        assert kwargs["error_retries"] == 2
        assert kwargs["error_retry_wait_sec"] == 60
        assert kwargs["max_retry_after_sec"] == 180


# ─────────────────────────────────────────────────────────────────────────────
# 3. The two budgets nest
# ─────────────────────────────────────────────────────────────────────────────

def _always_429(counter: dict):
    """A transport that raises HTTP 429 on every call, with no Retry-After."""

    def fake_urlopen(req, timeout=None, context=None):
        counter["calls"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {},
            io.BytesIO(b"Too Many Requests"),
        )

    return fake_urlopen


class TestGate1BudgetsNest:
    """[gate1] llm_call_retry_max wraps request_completion's own retry loop.

    (2 + 1) inner attempts x (1 + 1) outer attempts = 6 calls, and the
    candidate ends unknown (RUN-5) — no budget replaces the other.
    """

    def test_inner_and_outer_budgets_both_run(self, tmp_path, monkeypatch, caplog):
        from tools.auto.architect import CandidateTask, CitedLocation
        from tools.auto.gate1_filter import Gate1Filter, UNKNOWN_PRESENCE_REASON

        cfg = _cfg({"error_retries": "2", "error_retry_wait_sec": "0"})
        cfg.set("gate1", "llm_call_retry_max", "1")
        cfg.set("gate1", "llm_call_retry_wait_sec", "0")

        counter = {"calls": 0}
        monkeypatch.setattr(urllib.request, "urlopen", _always_429(counter))

        (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tools" / "c0.py").write_text(
            "def parse_config_0(raw):\n    return raw\n", encoding="utf-8")
        candidate = CandidateTask(
            title="candidate 0",
            instruction="fix the problem described in the claim",
            target_files=["tools/c0.py"],
            acceptance_check="python -m pytest tests -q",
            cited_location=CitedLocation(file="tools/c0.py",
                                         symbol="parse_config_0"),
            cluster="agents",
        )
        filt = Gate1Filter(config=cfg, base_url=BASE_URL, api_key=API_KEY,
                           model=MODEL, api_format="openai", verify_ssl=False)
        sleeps: list[float] = []
        with caplog.at_level(logging.WARNING):
            confirmed, reason, verdict = filt._check_presence(
                candidate, "def parse_config_0(raw):\n    return raw\n",
                base_dir=tmp_path, _sleep_fn=sleeps.append,
            )

        # 1 initial + 2 [loop] retries, wrapped by 1 initial + 1 [gate1] retry.
        assert counter["calls"] == (2 + 1) * (1 + 1) == 6, counter
        # The inner loop spent its own budget, not the outer wait.
        inner = [r for r in caplog.records if "and retrying (attempt" in r.message]
        assert len(inner) == 4  # two inner retries per outer attempt
        assert all("attempt" in r.message for r in inner)
        # ... and the outer loop spent its own, once, between the two attempts.
        outer = [r for r in caplog.records
                 if "LLM call failed" in r.message and "retrying in" in r.message]
        assert len(outer) == 1
        assert sleeps == [0.0]

        assert confirmed is False
        assert verdict == "unknown"
        assert reason.startswith(UNKNOWN_PRESENCE_REASON)
        assert "after 0 re-ask(s)" in reason
        assert "LLM call failed:" in reason


# ─────────────────────────────────────────────────────────────────────────────
# 4. One INFO line per run, into run.log
# ─────────────────────────────────────────────────────────────────────────────

_HELLO_PY_INITIAL = '''\
def greet():
    return "hello"


def main():
    print(greet())
'''

_HELLO_PY_IMPROVED = '''\
"""Hello module — RUN-10."""


def greet():
    return "hello"


def main():
    print(greet())
'''

_RETRY_BUDGET_CANDIDATES = [
    {
        "title": "Add module docstring to hello.py",
        "instruction": "Add a module-level docstring to the file.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {"file": "hello.py", "symbol": "greet",
                           "line_start": 1, "line_end": 2},
    },
]


def _git_init(path: Path) -> None:
    for cmd in (["git", "init", str(path)],
                ["git", "-C", str(path), "config", "user.email", "agent@test"],
                ["git", "-C", str(path), "config", "user.name", "Agent"]):
        subprocess.run(cmd, check=True, capture_output=True)
    (path / "hello.py").write_text(_HELLO_PY_INITIAL, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                   check=True, capture_output=True)


def _write_ini(tmp: Path, *, budget: dict | None = None) -> Path:
    # Written line by line, not with an indented f-string: a multi-line
    # substitution would break textwrap.dedent and leave [architect] on the
    # same logical line as the last [loop] value.
    lines = [
        "[auto]",
        "git_user = agent",
        "git_email = agent@test",
        "max_rounds_per_task = 1",
        "max_attempts_per_task = 1",
        "exec_timeout_sec = 30",
        "max_tasks_per_run = 1",
        "max_runtime_min = 0",
        "",
        "[api]",
        "active = local",
        "verify_ssl = false",
        "",
        "[api_local]",
        f"base_url = {BASE_URL}",
        f"api_key = {API_KEY}",
        f"model = {MODEL}",
        "api_format = openai",
        "",
        "[loop]",
        "timeout_seconds = 300",
        *[f"{key} = {value}" for key, value in (budget or {}).items()],
        "",
        "[architect]",
        "temperature = 0.2",
        "max_tokens = 512",
        "",
        "[gate1]",
        "temperature = 0.0",
        "max_tokens = 512",
        "",
        "[coder]",
        "temperature = 0.2",
        "max_tokens = 4096",
        "",
        "[prompt_optimizer]",
        "enabled = yes",
        "min_runs_before_optimize = 99",
        "",
        "[prompt_store]",
        f"store_path = {tmp}/prompts.json",
        "",
    ]
    ini = tmp / "agents.ini"
    ini.write_text("\n".join(lines), encoding="utf-8")
    return ini


def _standard_llm():
    """A fake LLM that answers every role in the plan and task phases."""

    def _fake(url, headers, payload, **kwargs):
        import re

        messages = payload.get("messages", [])
        system = next((m["content"] for m in messages
                       if m.get("role") == "system"), "")
        if "senior software architect" in system:
            return json.dumps(_RETRY_BUDGET_CANDIDATES)
        if "static code reviewer" in system:
            user = " ".join(m.get("content", "") for m in messages
                            if m.get("role") == "user")
            match = re.search(r"```\n(.*?)\n```", user, re.S)
            code = match.group(1) if match else ""
            evidence = next((ln.strip() for ln in code.splitlines() if ln.strip()),
                            "def ")
            return json.dumps({"verdict": "confirmed", "evidence": evidence,
                               "reason": "Valid improvement"})
        if "code-change validator" in system:
            return json.dumps({"approved": True, "feedback": ""})
        return json.dumps({"files": [{"path": "hello.py",
                                      "content": _HELLO_PY_IMPROVED}]})

    return _fake


class TestPipelineLogsTheBudgetOnce:
    """A stubbed --auto run says which budget is in force, exactly once."""

    @pytest.fixture()
    def _log(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path, budget={"error_retries": "3",
                                           "error_retry_wait_sec": "0",
                                           "max_retry_after_sec": "90"})
        from unittest.mock import patch

        from tools.auto.controller import AutoController

        with patch("tools.llm_stream.request_completion",
                   side_effect=_standard_llm()):
            ctrl = AutoController(goal="improve docstrings", base_dir=repo,
                                  config_path=str(ini))
            ctrl.run()
        return (Path(ctrl.agent_dir) / "run.log").read_text(encoding="utf-8")

    def test_exactly_one_budget_line(self, _log):
        lines = [ln for ln in _log.splitlines() if "LLM retry budget [loop]:" in ln]
        assert len(lines) == 1, lines

    def test_the_budget_line_names_the_resolved_numbers(self, _log):
        lines = [ln for ln in _log.splitlines() if "LLM retry budget [loop]:" in ln]
        line = lines[0]
        assert "error_retries=3" in line
        assert "wait=0.0s" in line
        assert "retry_after_cap=90s" in line
        assert "timeout=300s" in line

    def test_the_budget_line_precedes_the_architect(self, _log):
        budget_at = _log.find("LLM retry budget [loop]:")
        assert budget_at >= 0
        # Everything in run.log before the budget line is banner/init output;
        # the plan phase's first log line comes after it.
        plan_at = _log.find("plan")
        assert plan_at > budget_at

    def test_a_config_without_the_keys_logs_the_defaults(self, tmp_path):
        from unittest.mock import patch

        from tools.auto.controller import AutoController

        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path, budget=None)
        with patch("tools.llm_stream.request_completion",
                   side_effect=_standard_llm()):
            ctrl = AutoController(goal="improve docstrings", base_dir=repo,
                                  config_path=str(ini))
            ctrl.run()
        log = (Path(ctrl.agent_dir) / "run.log").read_text(encoding="utf-8")
        lines = [ln for ln in log.splitlines() if "LLM retry budget [loop]:" in ln]
        assert len(lines) == 1
        assert "error_retries=60" in lines[0]
        assert "wait=10.0s" in lines[0]
        assert "retry_after_cap=180s" in lines[0]


class TestBudgetLineIsFailOpen:
    """The budget line is diagnostic: no config, no run.log, no state — it may
    not stop the run."""

    def test_broken_config_and_broken_state_do_not_raise(self, caplog):
        from tools.auto.pipeline import _log_retry_budget

        class Broken:
            def get(self, *args, **kwargs):
                raise OSError("no such file")

        class BrokenState:
            def log(self, msg):
                raise OSError("disk full")

        ctrl = types.SimpleNamespace(state=BrokenState())
        with caplog.at_level(logging.INFO, logger="tools.auto.pipeline"):
            _log_retry_budget(Broken(), ctrl)

        recorded = [r.message for r in caplog.records]
        assert any("error_retries=60" in m and "wait=10.0s" in m
                   and "retry_after_cap=180s" in m for m in recorded)
        assert any("could not log the retry budget line" in m for m in recorded)

    def test_none_config_and_none_state_do_not_raise(self):
        from tools.auto.pipeline import _log_retry_budget

        _log_retry_budget(None, object())  # must not raise
