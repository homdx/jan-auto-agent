"""Bug 23 (round 204): pick_extracted kept claims about this repository.

The CLI calls pick_extracted, which dropped only ticket ids and hint-less
claims, so a claim naming a repo path (or a repo symbol) went to the paid
/assess.  It now applies is_internal exactly as pick_claims does, and main
passes the repo's own symbols.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402

REPO = "The function tools/contest/gates.py judge_worktree returns a scorecard when pytest passes."
WORLD = "Python raises ValueError when int() gets bad input via pytest."


def test_a_repo_path_claim_is_dropped_a_world_claim_is_kept():
    assert lf.pick_extracted([REPO, WORLD], 5) == [WORLD]


def test_a_claim_naming_a_symbol_of_the_repo_is_dropped(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("def judge_entry():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    claim = "judge_entry exits with code 2 when pytest finds no tests."
    assert lf.pick_extracted([claim, WORLD], 5) == [claim, WORLD]      # nothing known: public
    symbols = lf._repo_symbols(tmp_path)
    assert lf.pick_extracted([claim, WORLD], 5, symbols) == [WORLD]


def test_main_passes_the_repo_symbols(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(lf, "_repo_symbols", lambda root: {"judge_entry"})
    monkeypatch.setattr(lf, "pick_extracted",
                        lambda claims, limit, symbols=None: seen.append(symbols) or [])
    report = tmp_path / "r.md"
    report.write_text("## A\n\n" + "a " * 80 + "\n", encoding="utf-8")
    cache = tmp_path / "c.json"
    lf.Cache(cache).put("extract", lf.split_sections(report.read_text())[0], [WORLD])
    assert lf.main([str(report), "--cache", str(cache), "--repo-root", str(tmp_path)]) == 0
    assert seen == [{"judge_entry"}]
