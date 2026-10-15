"""Review p: the saved patch of a failed commit, Pass C, the claims of a drafted ticket, IMPROVEMENTS.md, stdout."""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "p@example.com")
    _git(tmp_path, "config", "user.name", "P")
    return tmp_path


def test_the_saved_patch_of_a_failed_commit_applies_after_the_discard(tmp_path):
    from tools.auto.git_manager import GitManager
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".agent/\n*.coder.bak\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / ".agent").mkdir()
    (root / ".agent" / "plan.json").write_text("{}")
    (root / ".collect").mkdir()
    (root / ".collect" / "artifact.json").write_text('{"modules": []}\n')
    (root / "a.py").write_text("x = 2\n")
    (root / "b.py").write_text("y = 1\n")
    gm = GitManager(root, configparser.ConfigParser())
    patch = gm.diff_working_tree()
    assert ".collect/" not in patch
    (tmp_path.parent / "work.patch").write_text(patch)
    gm.discard_working_changes()
    assert (root / ".collect" / "artifact.json").exists() and not (root / "b.py").exists()
    applied = _git(root, "apply", str(tmp_path.parent / "work.patch"))
    assert applied.returncode == 0, applied.stderr
    assert (root / "a.py").read_text() == "x = 2\n" and (root / "b.py").read_text() == "y = 1\n"


def test_a_fabricated_citation_in_an_over_length_sentence_is_dropped():
    from tools.collect import verifier as V
    from tools.collect.model import ModuleRecord
    text = ("The loader is defined at pkg/a.py:9999 where it reads the file"
            + " and it also keeps the cache warm while the worker runs" * 45 + ".")
    claims = V.extract_claims(text, "pkg/a.py")
    kept, dropped = V.verify_claims(claims, module=ModuleRecord(path="pkg/a.py"), known_symbols=frozenset(),
                                    line_counts={"pkg/a.py": 10}, fail_open_locs=frozenset())
    assert kept == [] and [d.claim.location for d in dropped] == ["pkg/a.py:9999"]


def _judge():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("judge_epic_round_p", ROOT / "scripts" / "judge_epic_round.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["judge_epic_round_p"] = module
    spec.loader.exec_module(module)
    return module


def test_a_ticket_written_the_way_the_draft_spec_shows_has_its_claims():
    from tools.contest.draft import CLAIM_WORDS, ticket_format_spec
    spec = ticket_format_spec()
    section = spec[spec.index("## Claims"):spec.index("Every header")]
    ticket = "# T-1 — x\n\n## Acceptance\n\n```\npytest\n```\n\n" + section
    assert [c["expect"] for c in _judge().ticket_claims(ticket, "t.md")] == list(CLAIM_WORDS)


def test_claims_only_inside_a_code_block_are_a_refusal_that_names_the_line():
    judge = _judge()
    with pytest.raises(ValueError, match=r"t\.md:6: the claims are inside a code block"):
        judge.ticket_claims("# T\n\n## Claims\n\n```\n- [new] `f` returns 1.\n```\n", "t.md")
    assert judge.ticket_claims("# T\n\n## Claims\n\n- [new] `f` returns 1.\n\n```\n- [still] an example\n```\n",
                               "t.md") == [{"claim": "`f` returns 1.", "expect": "new"}]


def _entry(task_id, instruction):
    return (f"### {task_id}: title\n\n**Cluster:** c  \n**Location:** `a.py`  \n**Target files:** `a.py`  \n"
            f"**Dependencies:** none  \n**Acceptance check:**\n```\ntrue\n```\n\n**Instruction:**\n\n{instruction}"
            f"\n\n---\n\n")


@pytest.mark.parametrize("ids", [["AUTO-T1", "AUTO-T2"], ["alpha", "beta"]])
@pytest.mark.parametrize("heading", ["### Notes: keep the old name", "### Example: the call"])
def test_a_sub_heading_after_a_rule_goes_with_its_entry(tmp_path, ids, heading):
    import tools.auto.plan_validator as pv
    doc = lambda *e: "# IMPROVEMENTS.md\n## Autonomous Tasks\n\n" + "".join(e) + "## Manual Suggestions\n\nnone\n"
    first, second = _entry(ids[0], f"intro\n\n---\n\n{heading}\n\nmore"), _entry(ids[1], "x")
    (tmp_path / pv.IMPROVEMENTS_FILENAME).write_text(doc(first, second))
    pv._remove_from_improvements_md(tmp_path, [ids[0]], ids)
    assert (tmp_path / pv.IMPROVEMENTS_FILENAME).read_text() == doc(second)


def test_a_banner_on_a_stdout_that_is_not_utf8_is_written_as_it_can_be(monkeypatch):
    from tools.ui import safe_print
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="latin-1"))
    safe_print("\n🧠 [LIVE ARCHITECT STREAMING THINKING & RESPONSE]:")
    sys.stdout.flush()
    assert raw.getvalue() == b"\n? [LIVE ARCHITECT STREAMING THINKING & RESPONSE]:\n"


def test_a_changed_module_left_without_a_summary_is_queued_once(tmp_path, monkeypatch, capsys):
    from tools.collect import cli as cli_mod
    summarize_repo = cli_mod.summarize_repo
    monkeypatch.setattr(cli_mod, "summarize_repo",
                        lambda *a, **k: summarize_repo(*a, **{"sleep_fn": lambda _s: None, **k}))
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])
    root = _repo(tmp_path)
    (root / "store.py").write_text("class AlphaStore:\n    pass\n")
    (root / ".gitignore").write_text(".collect/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "i")
    asked = []

    def llm(refuse):
        def call(system, user):
            asked.append(re.search(r"^path:\s*(\S+)", user, re.M).group(1))
            if refuse:
                raise RuntimeError("HTTP 500")
            return json.dumps({"purpose": "A store.", "notes": ""})
        return call

    cli_mod.action_collect(root, llm_call=llm(False))
    for name, refuse in (("BetaStore", True), ("GammaStore", False)):
        (root / "store.py").write_text(f"class {name}:\n    pass\n")
        _git(root, "commit", "-qam", name)
        capsys.readouterr()
        asked.clear()
        cli_mod.action_collect(root, llm_call=llm(refuse))
    assert asked == ["store.py"]
    assert "[1/1] summarized store.py" in capsys.readouterr().out
