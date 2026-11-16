"""Round 367: percent-bearing API credentials in autonomous mode.

The interactive Orchestrator has read ``api_key`` through a percent-tolerant
reader since round 365. The autonomous stages still used a bare
``config.get``, so ``api_key = fake%value`` raised ``InterpolationSyntaxError``
in the Architect (and in Gate 1, the Coder, the Inner Loop, the AutoTuner,
SummaryMemory, StoryBible and the collector summarizer). These tests pin the
shared reader and every real factory path that now uses it.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import llm_stream
from tools.auto.architect import CandidateTask, CitedLocation, review_clusters
from tools.auto.auto_tuner import make_auto_tuner
from tools.auto.coder import make_coder
from tools.auto.commit_on_success import make_commit_on_success
from tools.auto.controller import run_auto
from tools.auto.gate1_filter import Gate1Filter, filter_candidates
from tools.auto.inner_loop import make_inner_loop, resolve_validator_llm_profile
from tools.auto.llm_profile import LlmSettings, resolve_llm_profile
from tools.auto.plan_validator import run_validate
from tools.auto.repo_ingest import RepoCluster
from tools.auto.state import StateStore, make_task
from tools.collect.summarizer import make_summarizer_call_or_none
from tools.config_safe import ApiKeyInterpolationError, safe_get_api_key


_HELLO_PY = """\
def greet():
    return "hello"


def main():
    print(greet())
"""

_CANDIDATES = [
    {
        "title": "Add module docstring to hello.py",
        "instruction": "Add a module-level docstring to the file.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {
            "file": "hello.py",
            "symbol": "greet",
            "line_start": 1,
            "line_end": 2,
        },
    }
]


def _config(api_key="fake%value", *, task_mode="code", defaults="", extra=""):
    """A minimal interpolating ConfigParser with an ``[api_local]`` profile."""
    lines = []
    if defaults:
        lines.extend(["[DEFAULT]", defaults])
    lines.extend([
        "[api]",
        "active = local",
        "verify_ssl = false",
        "[api_local]",
        "base_url = http://127.0.0.1:1/v1",
    ])
    if api_key is not None:
        lines.append(f"api_key = {api_key}")
    lines.extend([
        "model = offline",
        "api_format = openai",
        "[auto]",
        f"task_mode = {task_mode}",
    ])
    if extra:
        lines.append(extra)
    config = configparser.ConfigParser()
    config.read_string("\n".join(lines) + "\n")
    return config


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text(_HELLO_PY)
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "agent@test"],
        ["git", "-C", str(repo), "config", "user.name", "Agent"],
        ["git", "-C", str(repo), "add", "-A"],
        ["git", "-C", str(repo), "commit", "-m", "init"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    return repo


def _write_ini(tmp_path: Path, api_key: str = "fake%value") -> Path:
    ini = tmp_path / "agents.ini"
    ini.write_text(f"""
[auto]
git_user = agent
git_email = agent@test
max_rounds_per_task = 10
max_attempts_per_task = 5
exec_timeout_sec = 300
max_tasks_per_run = 0
max_runtime_min = 0

[api]
active = local
verify_ssl = false

[api_local]
base_url = http://127.0.0.1:1/v1
api_key = {api_key}
model = offline
api_format = openai

[prompt_optimizer]
enabled = yes
min_runs_before_optimize = 99

[prompt_store]
store_path = {tmp_path}/prompts.json
""")
    return ini


def _fake_transport(seen):
    """A recording transport: canned Architect/Gate 1 replies, no network."""
    def _fake(url, headers, payload, **kwargs):
        messages = payload.get("messages", [])
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"), "",
        )
        if "senior software architect" in system:
            seen.append(("architect", headers.get("Authorization")))
            return json.dumps(_CANDIDATES)
        if "static code reviewer" in system:
            seen.append(("gate1", headers.get("Authorization")))
            user = next(
                (m["content"] for m in messages if m.get("role") == "user"), "",
            )
            match = re.search(r"```\n(.*?)\n```", user, re.S)
            code = match.group(1) if match else ""
            evidence = next(
                (line.strip() for line in code.splitlines() if line.strip()), "def ",
            )
            return json.dumps({
                "verdict": "confirmed",
                "evidence": evidence,
                "reason": "Valid improvement",
            })
        return json.dumps({"files": []})
    return _fake


# ── The shared reader ────────────────────────────────────────────────────────

@pytest.mark.parametrize(("api_key", "fallback", "expected"), [
    ("fake", "", "fake"),                                  # ordinary key
    ("", "", ""),                                          # blank key is allowed
    (None, "jan", "jan"),                                  # absent key: caller's fallback
    ("fake%%value", "", "fake%value"),                     # escaped percent
    ("%(credential)s", "", "fake-key"),                    # working reference
    ("%(credential)s%%ref%value", "", "fake-key%ref%value"),  # reference + literal percent
])
def test_credential_controls(api_key, fallback, expected):
    config = _config(api_key, defaults="credential = fake-key")
    assert safe_get_api_key(config, "api_local", "api_key", fallback=fallback) == expected


def test_reading_the_credential_leaves_other_interpolation_intact():
    config = _config(
        "fake%value",
        defaults="name = world",
        extra="[sample]\nvalue = hello %(name)s",
    )
    assert safe_get_api_key(config, "api_local", "api_key", fallback="") == "fake%value"
    # The parser is not mutated: the raw value is unchanged and unrelated
    # interpolation still expands.
    assert config.get("api_local", "api_key", raw=True) == "fake%value"
    assert config.get("sample", "value") == "hello world"


def test_a_broken_reference_raises_without_the_credential():
    config = _config("%(missing)s-SENTINEL", defaults="credential = fake-key")
    with pytest.raises(ApiKeyInterpolationError) as info:
        safe_get_api_key(config, "api_local", "api_key", fallback="fallback-sentinel")
    message = str(info.value)
    assert "[api_local] api_key" in message
    assert "SENTINEL" not in message
    assert "fallback-sentinel" not in message


def test_a_cyclic_reference_raises_a_safe_error():
    config = configparser.ConfigParser()
    config.read_string(
        "[api_local]\napi_key = %(a)s\na = %(b)s\nb = %(a)s\n"
    )
    with pytest.raises(ApiKeyInterpolationError) as info:
        safe_get_api_key(config, "api_local", "api_key", fallback="")
    assert "[api_local] api_key" in str(info.value)


# ── Real factory paths ───────────────────────────────────────────────────────

def test_architect_factory_reads_a_percent_bearing_credential(tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path)
    config = _config("fake%value")
    seen = []
    monkeypatch.setattr(llm_stream, "request_completion", _fake_transport(seen))
    cluster = RepoCluster(name="sample", patterns=[], files=["hello.py"])
    candidates = review_clusters([cluster], repo, config, goal="review")
    assert candidates
    assert seen == [("architect", "Bearer fake%value")]


def test_gate1_factory_reads_a_percent_bearing_credential(tmp_path, monkeypatch):
    repo = _clean_repo(tmp_path)
    config = _config("fake%value")
    seen = []
    monkeypatch.setattr(llm_stream, "request_completion", _fake_transport(seen))
    candidate = CandidateTask(
        title="Add module docstring to hello.py",
        instruction="Add a module-level docstring to the file.",
        target_files=["hello.py"],
        acceptance_check=f"{sys.executable} -c \"pass\"",
        cited_location=CitedLocation(
            file="hello.py", symbol="greet", line_start=1, line_end=2,
        ),
    )
    filter_candidates([candidate], repo, config)
    assert seen
    assert all(auth == "Bearer fake%value" for _, auth in seen)
    assert any(role == "gate1" for role, _ in seen)


def test_coder_factory_reads_a_percent_bearing_credential():
    assert make_coder(_config("fake%value"))._api_key == "fake%value"


def test_inner_loop_factory_reads_a_percent_bearing_credential(tmp_path):
    loop = make_inner_loop(_config("fake%value"), tmp_path)
    assert loop.coder._api_key == "fake%value"
    assert loop.validator.api_key == "fake%value"
    assert loop.validator._search_agent.api_key == "fake%value"


def test_factories_keep_their_own_absent_key_fallbacks(tmp_path):
    config = _config(None)
    assert make_coder(config)._api_key == ""
    loop = make_inner_loop(config, tmp_path)
    assert loop.coder._api_key == ""
    assert loop.validator.api_key == "jan"


def test_a_blank_key_is_allowed(tmp_path):
    config = _config("")
    assert make_coder(config)._api_key == ""
    assert make_inner_loop(config, tmp_path).validator.api_key == ""


# ── Named profiles keep their own credential ─────────────────────────────────

_PROFILE_CONFIG = """
[api]
active = local
[api_local]
base_url = http://shared.invalid/v1
api_key = shared%key
model = shared-model
[strong]
base_url = http://strong.invalid/v1
api_key = strong%key
model = strong-model
[validator_agent]
validator_llm_profile = strong
[gate1]
presence_llm_profile = strong
"""


def _profile_config():
    config = configparser.ConfigParser()
    config.read_string(_PROFILE_CONFIG)
    return config


def test_named_profile_keeps_its_own_url_model_and_credential():
    defaults = LlmSettings(
        base_url="http://shared.invalid/v1",
        api_key="shared%key",
        model="shared-model",
    )
    settings, name = resolve_llm_profile(
        _profile_config(), "validator_agent", "validator_llm_profile",
        defaults=defaults,
    )
    assert name == "strong"
    assert settings.base_url == "http://strong.invalid/v1"
    assert settings.model == "strong-model"
    assert settings.api_key == "strong%key"


def test_gate2_critique_profile_keeps_its_own_credential():
    kwargs, _verify_ssl, _think = resolve_validator_llm_profile(
        _profile_config(),
        base_url="http://shared.invalid/v1", api_key="shared%key",
        model="shared-model", api_format="openai", num_ctx=0,
        verify_ssl=True, temperature=0.1, max_tokens=512, think=False,
    )
    assert kwargs["base_url"] == "http://strong.invalid/v1"
    assert kwargs["model"] == "strong-model"
    assert kwargs["api_key"] == "strong%key"


def test_gate1_presence_profile_keeps_its_own_credential():
    filt = Gate1Filter(
        _profile_config(), "http://shared.invalid/v1", "shared%key", "shared-model",
    )
    assert filt._presence_base_url == "http://strong.invalid/v1"
    assert filt._presence_model == "strong-model"
    assert filt._presence_api_key == "strong%key"


# ── Optional paths that used to disable themselves ───────────────────────────

def test_auto_tuner_keeps_a_percent_bearing_credential(tmp_path, monkeypatch):
    captured = {}

    class FakeOptimizer:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("tools.prompt_optimizer.PromptOptimizer", FakeOptimizer)
    tuner = make_auto_tuner(_config("fake%value"), tmp_path)
    assert tuner.enabled
    assert captured["api_key"] == "fake%value"


def test_commit_helper_keeps_a_percent_bearing_credential(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "tools.auto.commit_on_success.make_git_manager", lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "tools.auto.summary_memory.make_summary_memory", lambda *a, **k: None,
    )

    def fake_story_bible(config, *, base_url, api_key, model, api_format, base_dir="."):
        captured.update(api_key=api_key, base_url=base_url, model=model)
        return None

    monkeypatch.setattr("tools.auto.story_bible.make_story_bible", fake_story_bible)
    make_commit_on_success(
        _config("fake%value", task_mode="creative"),
        tmp_path,
        StateStore(tmp_path / ".agent"),
    )
    assert captured["api_key"] == "fake%value"


def test_collect_summarizer_keeps_a_percent_bearing_credential(monkeypatch):
    captured = {}

    def fake_request(url, headers, payload, **kwargs):
        captured["auth"] = headers["Authorization"]
        return "ok"

    monkeypatch.setattr(llm_stream, "request_completion", fake_request)
    call = make_summarizer_call_or_none(_config("fake%value"))
    assert call is not None
    call("system", "user")
    assert captured["auth"] == "Bearer fake%value"


def test_collect_summarizer_degrades_on_a_broken_reference():
    # Pass B is optional: a broken credential must not raise into --collect.
    assert make_summarizer_call_or_none(_config("%(missing)s-SENTINEL")) is None


# ── The real --auto --dry-run path ───────────────────────────────────────────

def test_auto_dry_run_uses_a_percent_bearing_credential(
    tmp_path, monkeypatch, caplog,
):
    repo = _clean_repo(tmp_path)
    ini = _write_ini(tmp_path)
    seen = []
    monkeypatch.setattr(llm_stream, "request_completion", _fake_transport(seen))

    with caplog.at_level(logging.WARNING):
        rc = run_auto("review the sample source", repo, str(ini), dry_run=True)

    assert rc == 0
    plan = json.loads((repo / ".agent" / "plan.json").read_text())
    assert plan["tasks"]

    architect_auth = [auth for role, auth in seen if role == "architect"]
    assert architect_auth == ["Bearer fake%value"]
    assert all(auth == "Bearer fake%value" for _, auth in seen)

    messages = [record.getMessage() for record in caplog.records]
    assert not any("interpolation" in message.lower() for message in messages)
    assert not any("fake%value" in message for message in messages)


def test_auto_reports_a_broken_credential_reference(tmp_path, capsys):
    repo = _clean_repo(tmp_path)
    ini = _write_ini(tmp_path, api_key="%(missing)s-SENTINEL")

    rc = run_auto("review the sample source", repo, str(ini), dry_run=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Fatal error" not in err
    assert "[api_local] api_key" in err
    assert "SENTINEL" not in err


@pytest.fixture
def planned_repo(tmp_path):
    repo = _clean_repo(tmp_path)
    candidate = _CANDIDATES[0].copy()
    candidate["cited_locations"] = [candidate.pop("cited_location")]
    state = StateStore(repo / ".agent")
    state.initialise("review the sample source", repo)
    state.upsert_task(make_task("T001", **candidate))
    return repo


def test_validate_plan_sends_the_percent_bearing_key(planned_repo, tmp_path, monkeypatch):
    ini = _write_ini(tmp_path)
    seen = []
    monkeypatch.setattr(llm_stream, "request_completion", _fake_transport(seen))

    assert run_validate(planned_repo, str(ini)) == 0
    assert seen == [("gate1", "Bearer fake%value")]


def test_validate_plan_reports_a_broken_key(planned_repo, tmp_path, monkeypatch, capsys):
    ini = _write_ini(tmp_path, api_key="%(unknown)s-SENTINEL")
    seen = []
    monkeypatch.setattr(llm_stream, "request_completion", _fake_transport(seen))
    plan = planned_repo / ".agent" / "plan.json"
    before = plan.read_bytes()

    assert run_validate(planned_repo, str(ini)) == 1
    output = capsys.readouterr()
    assert "Error:" in output.err and "[api_local] api_key" in output.err
    assert "Traceback" not in output.err and "SENTINEL" not in output.err
    assert seen == []
    assert plan.read_bytes() == before
