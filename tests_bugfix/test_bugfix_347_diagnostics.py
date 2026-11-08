"""tests_bugfix/test_bugfix_347_diagnostics.py — AGNES-8.

The claim scorer validates its input instead of reporting success without
checks, and nine small diagnostics stop being silent:

  1. T148 — a thresholds file with no ``targets`` is a clear error and exit 2.
  2. T147 — a target with a missing/unknown ``op`` or a missing required field
     is a configuration error, not an ``n/a`` row or a traceback.
  3. T146 — a malformed claim row is a clear error and exit 2 (skipping it
     would change the score).
  4. T144 — a vote outside -1, 0, 1 is refused before it is counted.
  5. T3   — ``load_config`` on an absent path warns and keeps the loaded config.
  6. T8   — ``RunLimits.__repr__`` names ``workspace_retain_count``.
  7. T12  — ``_tree_names`` warns on a nonzero git result and still returns [].
  8. T65  — ``summarize_chapter`` on blank input returns "" with no LLM call.
  9. T69  — an absent ``related_code`` reaches both validator prompts as ``{}``;
     another falsy value is not rewritten into a dict.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BENCH = PROJECT_ROOT / "contest-bench" / "cc"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"diag_347_{name}", BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sc = _load("score_cc")
rr = _load("replay_rules")

from main import Orchestrator  # noqa: E402
from tools.auto.controller import RunLimits  # noqa: E402
from tools.auto.summary_memory import SummaryMemory  # noqa: E402
from tools.contest.cli import _tree_names  # noqa: E402
from tools.validator_agent import ValidatorAgent  # noqa: E402


# ── the scorer's inputs ──────────────────────────────────────────────────────

KEY = {"set": "toy", "claims": [
    {"id": "a", "claim": "A", "kind": "code", "truth_base": True, "truth_head": False},
]}

VOTES = {
    "results": [{"model": m, "run": 0, "votes": {"0": "TRUE"}} for m in ("m1", "m2", "m3")],
    "claims": [{"id": "a", "claim": "A", "verdict": "TRUE", "unanimous": True,
                "by_model": {"m1": "TRUE", "m2": "TRUE", "m3": "TRUE"}}],
}

GOOD_TARGET = {"id": "cov", "set": "toy", "metric": "overall.coverage", "op": ">=", "value": 0.5}


def _score_cli(tmp_path: Path, thresholds=None, *, claims=KEY, votes=VOTES):
    v, k = tmp_path / "votes.json", tmp_path / "key.json"
    v.write_text(json.dumps(votes), encoding="utf-8")
    k.write_text(json.dumps(claims), encoding="utf-8")
    args = [sys.executable, str(BENCH / "score_cc.py"), str(v), str(k)]
    if thresholds is not None:
        th = tmp_path / "thresholds.json"
        th.write_text(json.dumps(thresholds), encoding="utf-8")
        args += ["--thresholds", str(th)]
    return subprocess.run(args, capture_output=True, text=True)


def test_a_valid_thresholds_file_still_passes(tmp_path):
    """The validation must not move the normal result: the target is met, exit 0."""
    proc = _score_cli(tmp_path, {"targets": [GOOD_TARGET]})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok   cov" in proc.stdout


# 1. T148

@pytest.mark.parametrize("thresholds", [
    {"note": "no targets key"},
    {"targets": []},
    {"targets": "not-a-list"},
])
def test_thresholds_without_targets_is_an_error(tmp_path, thresholds):
    """T148: no targets list means nothing was checked, so the run must not say success."""
    proc = _score_cli(tmp_path, thresholds)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "targets" in proc.stderr
    assert "score_cc:" in proc.stderr


# 2. T147

@pytest.mark.parametrize("target,needle", [
    ({**GOOD_TARGET, "op": "~~"}, "unknown op"),
    ({k: v for k, v in GOOD_TARGET.items() if k != "op"}, "missing required field"),
    ({k: v for k, v in GOOD_TARGET.items() if k != "value"}, "missing required field"),
    ({k: v for k, v in GOOD_TARGET.items() if k != "metric"}, "missing required field"),
    ({k: v for k, v in GOOD_TARGET.items() if k != "id"}, "missing required field"),
])
def test_a_malformed_target_is_a_configuration_error(tmp_path, target, needle):
    """T147: a missing or unknown field cannot be reported n/a — rendering reads it too."""
    proc = _score_cli(tmp_path, {"targets": [target]})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert needle in proc.stderr


# 3. T146

@pytest.mark.parametrize("claims", [
    {"set": "toy", "claims": [{"id": "a", "kind": "code", "truth_base": True}]},
    {"set": "toy", "claims": [{"id": "a", "claim": "", "kind": "code", "truth_base": True}]},
    {"set": "toy", "claims": ["not-an-object"]},
])
def test_a_malformed_claim_row_is_an_error(tmp_path, claims):
    """T146: the row is not skipped — a dropped truth row would change the score."""
    proc = _score_cli(tmp_path, None, claims=claims)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "claim" in proc.stderr.lower()


# 4. T144

class TestVoteRefusal:
    """T144: a vote outside -1, 0, 1 is refused, not counted as "not accepted"."""

    @pytest.mark.parametrize("votes", [[2, 2, 2], [2, 1, 1], [-2, 0, 1], [1, 1, 1, 5]])
    @pytest.mark.parametrize("rule", ["all", "committed"])
    def test_a_vote_outside_the_domain_is_refused(self, votes, rule):
        with pytest.raises(ValueError, match="outside"):
            rr.accept(votes, rule)

    def test_valid_votes_are_unchanged(self):
        assert rr.accept([1, 1, 1], "all") == 1
        assert rr.accept([-1, -1, -1], "committed") == -1
        assert rr.accept([1, 1, 1, 1], "all") == 1
        assert rr.accept([1, 0, 1, 1], "committed") == 1
        assert rr.accept([1, 0], "all") == 0        # below the quorum
        assert rr.accept([0, 0, 0], "committed") == 0

    def test_main_refuses_a_recorded_bad_vote(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad_votes.json"
        bad.write_text(json.dumps({"claims": [{"truth": 1}],
                                   "voters": {"a": [2], "b": [2], "c": [2]}}),
                       encoding="utf-8")
        monkeypatch.setattr(rr, "DATA", bad)
        rc = rr.main(["--voters", "a", "b", "c"])
        assert rc == 2 and "outside -1, 0, 1" in capsys.readouterr().err

    def test_the_recorded_votes_replay_as_before(self):
        data = json.loads(rr.DATA.read_text(encoding="utf-8"))
        assert rr.replay(list(data["voters"])[:3], "all", data) == (119, 0)


# 5. T3

def test_load_config_warns_on_an_absent_path_and_keeps_the_old_config(tmp_path, caplog):
    """T3: /reload of an absent file keeps the last-good config, and says so."""
    orc = Orchestrator.__new__(Orchestrator)
    orc.config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    orc.config.read_string("[loop]\nmax_iterations = 7")
    missing = str(tmp_path / "absent.ini")
    with caplog.at_level(logging.WARNING):
        orc.load_config(missing)
    assert orc.max_iterations == 7
    messages = [r.getMessage() for r in caplog.records]
    assert any("not found" in m and missing in m for m in messages)
    assert not any("built-in defaults" in m for m in messages)


# 6. T8

def test_run_limits_repr_names_the_workspace_retain_count():
    """T8: the repr is how an operator reads the effective caps."""
    text = repr(RunLimits(workspace_retain_count=7))
    assert "workspace_retain_count=7" in text


# 7. T12

def test_tree_names_warns_on_a_nonzero_git_result(caplog):
    """T12: a base that cannot be read still means "no ticket there", but not silently."""
    with caplog.at_level(logging.WARNING, logger="tools.contest.cli"):
        assert _tree_names(PROJECT_ROOT, "0" * 40, "epic-tasks") == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("failed" in m or "ls-tree" in m for m in messages)


# 8. T65

@pytest.mark.parametrize("text", ["", "   ", "\n\t\n  \n"])
def test_summarize_chapter_on_blank_input_makes_no_call(text, tmp_path):
    """T65: blank text is an empty summary; the short-input path must not reach the model."""
    calls = []

    def llm(system: str, user: str) -> str:
        calls.append(user)
        return "- a fact"

    mem = SummaryMemory(llm, base_dir=tmp_path)
    assert mem.summarize_chapter(text) == ""
    assert calls == []


# 9. T69

class _BadTemplateStore:
    """A prompt store whose template has an extra placeholder, forcing the fallback."""

    def get_current(self, _name):
        return ("{task} {iteration} {max_iter} {target_block} {imports} "
                "{related_code} {missing_refs} {not_a_real_field}")


def _captured_prompt(monkeypatch, payload, *, prompt_store=None) -> str:
    import tools.validator_agent as va

    seen = {}

    def fake_completion(url, headers, request_payload, timeout, **kw):
        seen["prompt"] = request_payload["messages"][-1]["content"]
        return '{"status": "approved", "feedback": "ok"}'

    monkeypatch.setattr(va, "request_completion", fake_completion)
    ValidatorAgent(prompt_store=prompt_store).validate(dict(payload))
    return seen["prompt"]


def _section(prompt: str, header: str) -> str:
    body = prompt.split(header + "\n", 1)[1]
    return body.split("\n\n", 1)[0].strip()


def _payload(**over):
    base = {"task": "t", "iteration": 1, "target_block": "c",
            "imports": [], "missing_refs": []}
    base.update(over)
    return base


@pytest.mark.parametrize("store", [None, _BadTemplateStore()])
def test_absent_related_code_is_sent_as_an_empty_object(monkeypatch, store):
    """T69: both the normal and the fallback template see ``{}``, not ``null``."""
    prompt = _captured_prompt(monkeypatch, _payload(), prompt_store=store)
    assert _section(prompt, "[RESOLVED CROSS-REFERENCES]") == "{}"


def test_explicit_none_related_code_is_sent_as_an_empty_object(monkeypatch):
    """T69: ``None`` is the absent value; it is sent as ``{}`` too."""
    prompt = _captured_prompt(monkeypatch, _payload(related_code=None))
    assert _section(prompt, "[RESOLVED CROSS-REFERENCES]") == "{}"


@pytest.mark.parametrize("value,shown", [
    ([], "[]"),
    ("", '""'),
    (0, "0"),
    (False, "false"),
])
def test_another_falsy_related_code_is_not_turned_into_a_dict(monkeypatch, value, shown):
    """T69: only absent/None becomes ``{}``; another falsy value is sent as itself."""
    prompt = _captured_prompt(monkeypatch, _payload(related_code=value))
    assert _section(prompt, "[RESOLVED CROSS-REFERENCES]") == shown