"""Review of cc-epic-docs: claim_diff --check cannot pass vacuously, an expect on a world claim is refused before voting, run_cc.sh enforces the thresholds it records."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_diff as cd  # noqa: E402
from tools.claimcheck import compare  # noqa: E402
from tools.claimcheck.target import Target  # noqa: E402

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _claims(tmp_path, rows):
    f = tmp_path / "claims.json"
    f.write_text(json.dumps(rows))
    return f


@pytest.mark.parametrize("key", ["expected", "expects", "Expect", "excpect"])
def test_a_misspelt_expect_key_is_refused(tmp_path, key):
    with pytest.raises(ValueError, match=f"`{key}` — did you mean `expect`"):
        cd.load_claims(_claims(tmp_path, [{"claim": "c", key: "fixed"}]))


def test_the_fixtures_own_keys_are_not_taken_for_typos():
    for name in ("claims_fixture.json", "claims_real.json"):
        assert cd.load_claims(ROOT / "contest-bench" / "cc" / name)


def test_check_with_no_expect_is_exit_2_before_anything_runs(tmp_path, capsys):
    f = _claims(tmp_path, [{"claim": "`f` in a.py returns 1"}])
    assert cd.main([str(f), "--target", "/nonexistent", "--base", "a", "--head", "b", "--check",
                    "--models", "a/x", "--repo-root", str(tmp_path)]) == 2
    assert "nothing to check" in capsys.readouterr().err


def test_an_expect_on_a_world_claim_is_refused_before_any_vote(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    env = {**os.environ, **ENV}
    (r / "a.py").write_text("def f():\n    return 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True, env=env)
    calls = []
    claims = cd.load_claims(_claims(tmp_path, [
        {"id": "W1", "claim": "The Earth goes around the Sun.", "expect": "fixed"},
        {"id": "C1", "claim": "`f` in a.py returns 1", "expect": "still"}]))
    with Target.open(str(r), "HEAD", scratch=tmp_path / "s") as t:
        with pytest.raises(ValueError, match=r"expect on a world claim \(W1\)"):
            cd.judge_both(claims, t, t, lambda items, side: calls.append(side) or [])
    assert calls == []


def test_a_world_mismatch_line_says_why():
    d = compare.Delta(claim="w", base=compare.NOT_JUDGED, head={"verdict": "TRUE", "unanimous": True},
                      change=compare.NA, evidence_base=[], evidence_head=[], kind="world", expect="fixed")
    assert "a world claim is judged once" in compare.mismatch_line(d)


def test_run_cc_passes_the_thresholds_on_the_evidence_paths():
    text = (ROOT / "contest-bench" / "cc" / "run_cc.sh").read_text()
    scores = [ln for ln in text.splitlines() if "score_cc.py" in ln or "--thresholds" in ln]
    assert any("--thresholds" in ln for ln in scores if "delta.json" in ln or "thresholds.json" in ln)
    assert 'th=(--thresholds "$here/thresholds.json")' in text and '[ "$target" = 1 ]' in text
    note = json.loads((ROOT / "contest-bench" / "cc" / "thresholds.json").read_text())["note"]
    assert "run_cc.sh --target and --diff pass them" in note


def test_score_cc_exits_1_on_a_miss(tmp_path):
    """The exit run_cc.sh now folds into its own: a votes file that decides nothing misses the coverage target."""
    fixture = json.loads((ROOT / "contest-bench" / "cc" / "claims_fixture.json").read_text())
    votes = {"results": [], "claims": [{"claim": c["claim"], "verdict": "UNSURE", "unanimous": False,
                                        "kind": c.get("kind", "code")} for c in fixture["claims"]]}
    (tmp_path / "v.json").write_text(json.dumps(votes))
    p = subprocess.run([sys.executable, str(ROOT / "contest-bench" / "cc" / "score_cc.py"), str(tmp_path / "v.json"),
                        str(ROOT / "contest-bench" / "cc" / "claims_fixture.json"),
                        "--thresholds", str(ROOT / "contest-bench" / "cc" / "thresholds.json")],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 1 and re.search(r"^MISS fixture-code-coverage", p.stdout, re.M), p.stdout
