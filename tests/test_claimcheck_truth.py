"""CC-8 (290): votes.json into truth.csv and the stage-3 pending list; run_claim_check.sh --truth/--dry-run."""

from __future__ import annotations

import csv
import io
import json
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.claimcheck import judge
from tools.claimcheck import truth as tr
from tools.claimcheck.model import Anchor, ResolvedAnchor
from tools.claimcheck.pack import PackBudget
from tools.claimcheck.target import Target

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_vote as cv  # noqa: E402
import claimcheck_truth as ct  # noqa: E402
import harvest_report  # noqa: E402
import truth_consensus  # noqa: E402


# ------------------------------------------------------------------ no network, for the whole module

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("CC-8 must not open a socket")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ a real votes.json: a repo, CC-6's chain, noisy fake voters

_GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
            "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}
APP = ("import subprocess\n\n\ndef run_step(cmd):\n    return subprocess.run(cmd, check=False)\n\n\n"
       "class Store:\n    def compact(self):\n        return sorted(self.rows)\n")
LIB = "import subprocess\n\n\ndef run_lib(cmd):\n    return subprocess.run(cmd, check=True)\n"

C_TRUE = "`run_step` in `app.py` calls subprocess.run with check=False."
C_FALSE = "`run_lib` in `lib.py` calls subprocess.run with check=False."
C_DISSENT = "`Store.compact` in `app.py` returns the rows sorted."
C_MISSED = "`run_lib` in `lib.py` takes one argument."
C_WORLD = "The Earth goes around the Sun once a year."
C_SPLIT = "`run_step` in `app.py` is slow."
CLAIMS = [C_TRUE, C_FALSE, C_DISSENT, C_MISSED, C_WORLD, C_SPLIT]
#: each live voter's verdict per claim; None = this voter skips the claim in its reply
VOTERS = {
    "kenary/agnes-3-0-flash:free": ["TRUE", "FALSE", "TRUE", "TRUE", "TRUE", "TRUE"],
    "kenary/hy3:free": ["TRUE", "FALSE", "TRUE", None, "TRUE", "FALSE"],
    "kenary/nemotron-3-ultra-550b-a55b:free": ["TRUE", "FALSE", "FALSE", "TRUE", "TRUE", "TRUE"],
    "tokenharbor/deepseek-v4-flash:free": ["TRUE", "FALSE", "TRUE", "TRUE", "TRUE", "FALSE"],
}
DEAD = "sensenova123/sensenova-6.8-flash-lite"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=_GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    return root, _git(root, "rev-parse", "HEAD")


_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)
_GUTTER = re.compile(r"^ *\d*\| ?")


def _voter(answers: dict):
    """A fake voter in the live reply shape: objects one after another with no list around them,
    each TRUE/FALSE citing the first non-blank code line of the pack's first chunk."""
    def complete(prompt: str) -> str:
        out = []
        for m in _BLOCK.finditer(prompt):
            n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
            verdict = answers.get(claim)
            if verdict is None:
                continue
            row = {"id": n, "verdict": verdict}
            if judge.WORLD_BLOCK not in evidence:
                label = _LABEL.search(evidence)
                if not label:
                    out.append(json.dumps({"id": n, "verdict": "UNSURE"}))
                    continue
                body = evidence[label.end():].strip("\n").splitlines()
                line = next(_GUTTER.sub("", ln, count=1).strip() for ln in body
                            if len(_GUTTER.sub("", ln, count=1).strip()) >= 8)
                row.update(chunk=f"[[{label.group(1)}]]", quote=line)
            out.append(json.dumps(row))
        return "\n".join(out)
    return complete


def make_votes(root, sha, scratch, voters=VOTERS, dead=(DEAD,)) -> dict:
    """votes.json as `claim_vote.main_target` writes it, through a JSON round trip (string keys)."""
    claims = [{"claim": c, "truth": None} for c in CLAIMS]
    with Target.open(str(root), sha, scratch=scratch) as target:
        items, meta = cv.prepare_target(claims, target, PackBudget())
        results = []
        for ref, verdicts in voters.items():
            got = judge.ask_with_packs(items, 0, 1, _voter(dict(zip(CLAIMS, verdicts))),
                                       batch=10, code_batch=2)
            results.append({"model": ref, "run": 0, "votes": got["votes"],
                            "accepted": got["accepted"], "rejected": got["rejected"]})
        for ref in dead:
            results.append({"model": ref, "run": 0, "votes": {}, "accepted": {}, "rejected": [],
                            "error": "HTTP 402 out of free plan"})
        table = cv.tally(claims, results, meta=meta)
        report = {"target": {"repo": str(root), "ref": sha, "sha": target.sha, "base": None,
                             "warnings": []}, "results": results, "claims": table}
    return json.loads(json.dumps(report, ensure_ascii=False))


@pytest.fixture
def votes(repo, tmp_path):
    root, sha = repo
    return make_votes(root, sha, tmp_path / "scratch")


def _row(verdict, unanimous, **kw):
    return {"claim": kw.pop("claim", f"claim {verdict} {unanimous}"), "verdict": verdict,
            "unanimous": unanimous, "kind": kw.pop("kind", "world"), **kw}


def _ra(kind, text, found, path="", qualname=""):
    return ResolvedAnchor(anchor=Anchor(kind=kind, text=text, start=0, end=len(text)),
                          found=found, path=path, qualname=qualname)


# ------------------------------------------------------------------ the chain itself: what the fake run really gave

def test_the_votes_fixture_has_the_live_shapes(votes):
    """The fixture is the real chain's output: unanimous TRUE / FALSE, a dissent, a missed claim,
    a world claim, a split, one dead voter — not a hand-written table."""
    rows = {c["claim"]: c for c in votes["claims"]}
    assert (rows[C_TRUE]["verdict"], rows[C_TRUE]["unanimous"]) == ("TRUE", True)
    assert (rows[C_FALSE]["verdict"], rows[C_FALSE]["unanimous"]) == ("FALSE", True)
    assert (rows[C_DISSENT]["verdict"], rows[C_DISSENT]["unanimous"]) == ("TRUE", False)
    assert (rows[C_MISSED]["verdict"], rows[C_MISSED]["unanimous"]) == ("TRUE", False)
    assert (rows[C_WORLD]["verdict"], rows[C_WORLD]["unanimous"]) == ("TRUE", True)
    assert rows[C_SPLIT]["verdict"] == "SPLIT"
    assert rows[C_TRUE]["evidence"] and rows[C_TRUE]["quotes"]
    assert tr.dead_voters(votes) == [DEAD]


# ------------------------------------------------------------------ rows: only the settled ones

@pytest.mark.parametrize("verdict,unanimous,want", [
    ("TRUE", True, "REAL"), ("FALSE", True, "FALSE"),
    ("TRUE", False, None), ("FALSE", False, None),
    ("SPLIT", False, None), ("UNSURE", False, None), ("CODE-CHECK", False, None),
    ("SPLIT", True, None), ("UNSURE", True, None), ("CODE-CHECK", True, None),
    ("TRUE", "true", None), ("TRUE", 1, None), ("TRUE", None, None),
])
def test_rows_only_for_unanimous_verdicts(verdict, unanimous, want):
    """Settled is `unanimous is True` with TRUE/FALSE; a truthy look-alike is not."""
    assert tr.settled({"verdict": verdict, "unanimous": unanimous}) == want
    rows = tr.to_truth_rows({"claims": [_row(verdict, unanimous)]}, checked_by="x")
    assert [r["truth"] for r in rows] == ([want] if want else [])


def test_truth_values():
    rows = tr.to_truth_rows({"claims": [_row("TRUE", True), _row("FALSE", True)]}, checked_by="x")
    assert [r["truth"] for r in rows] == ["REAL", "FALSE"]
    assert {r["truth"] for r in rows} <= harvest_report.TRUTH_VALUES


def test_only_unanimous_rows_are_settled_even_when_the_verdict_says_true():
    """Round 280's real row: verdict TRUE, by_model TRUE/TRUE/FALSE, unanimous false."""
    row = {"claim": "`gates.py` checks the summary line.", "truth": None, "verdict": "TRUE",
           "needs_code": True, "kind": "code", "dangling": False, "unanimous": False,
           "by_model": {"kenary/agnes-3-0-flash:free": "TRUE", "kenary/hy3:free": "TRUE",
                        "kenary/nemotron-3-ultra-550b-a55b:free": "FALSE"},
           "all_votes": {"TRUE": 2, "FALSE": 1}, "sha": "d36d59e" * 5 + "abcde",
           "evidence": ["src:tools/contest/gates.py:10-20"], "quotes": ["x = 1"],
           "rejected": {}, "downgrades": 0}
    votes = {"claims": [row], "results": []}
    assert tr.to_truth_rows(votes, checked_by="x") == []
    assert tr.pending_rows(votes) == [(0, row)]
    assert "`gates.py` checks the summary line." in tr.pending_markdown(votes)


def test_rows_from_the_real_chain(votes, repo, tmp_path):
    """Three settled claims of six (TRUE, FALSE, the world fact); the dissent, the missed claim
    and the split are pending, each once."""
    root, _ = repo
    resolved, packs, _ = tr.rebuild(votes, root, scratch=tmp_path / "s2")
    rows = tr.to_truth_rows(votes, checked_by="c", resolved=resolved)
    assert [(r["finding"], r["truth"]) for r in rows] == [
        ("app.py::run_step", "REAL"), ("lib.py::run_lib", "FALSE"), (tr.claim_hash(C_WORLD), "REAL")]
    assert rows[2]["how"] == "world fact"
    md = tr.pending_markdown(votes, packs)
    for claim in (C_DISSENT, C_MISSED, C_SPLIT):
        assert md.count(claim) == 1
    for claim in (C_TRUE, C_FALSE, C_WORLD):
        assert claim not in md


# ------------------------------------------------------------------ finding

def test_finding_key_forms():
    sym = [_ra("path", "app.py", True, "app.py"), _ra("symbol", "Store.compact", True, "app.py", "Store.compact")]
    assert tr.finding_key("c", sym) == "app.py::Store.compact"
    assert tr.finding_key("c", [_ra("path", "lib/x.py", True, "lib/x.py")]) == "lib/x.py"
    # Symbol not found in an existing file: keyed by file and name (review batch m).
    assert tr.finding_key("c", [_ra("symbol", "gone_fn", False, "app.py")]) == "app.py::gone_fn"
    # a symbol not found anywhere, a dangling path: neither is a key
    lost = [_ra("symbol", "gone_fn", False, ""), _ra("path", "nope.py", False, "")]
    key = tr.finding_key("Some claim  about\nthings.", lost)
    assert re.fullmatch(r"claim:[0-9a-f]{8}", key)
    assert key == tr.finding_key("some claim about things.", [])     # stable, case and spacing aside
    assert key != tr.finding_key("another claim", [])


def test_finding_key_from_evidence_when_there_is_no_target():
    assert tr.path_of_chunk_id("src:tools/a.py:12-30") == "tools/a.py"
    assert tr.path_of_chunk_id("git:a73e389:tools/b.py:88-97") == "tools/b.py"
    assert tr.path_of_chunk_id("diff:1234567..89abcde:tools/c.py:4-9") == "tools/c.py"
    assert tr.path_of_chunk_id("[[src:Makefile:1-3]]") == "Makefile"
    assert tr.path_of_chunk_id("collect:callers:tools/d.py") == "tools/d.py"
    for none in ("git:a73e389:header", "diff:1234567..89abcde:stat", "note:git:x", "ticket:42", ""):
        assert tr.path_of_chunk_id(none) == ""
    assert tr.finding_key("c", None, ["git:a73e389:header", "src:tools/a.py:1-2"]) == "tools/a.py"
    assert tr.finding_key("c", None, ["note:git:x"]) == tr.claim_hash("c")


def test_finding_with_a_comma_survives_the_csv(tmp_path):
    row = _row("TRUE", True, evidence=["src:a,b.py:1-2"], quotes=["x = 1"], kind="code")
    rows = tr.to_truth_rows({"claims": [row]}, checked_by="x")
    tr.write_truth(tmp_path / "t.csv", rows)
    assert harvest_report.load_truth(tmp_path / "t.csv") == {"a,b.py": "REAL"}


# ------------------------------------------------------------------ the CSV

def test_header_columns(tmp_path):
    tr.write_truth(tmp_path / "t.csv", [])
    assert (tmp_path / "t.csv").read_text(encoding="utf-8") == "finding,truth,checked_by,duplicate_of,how\n"
    (tmp_path / "min.csv").write_text("finding,truth,how\na.py::f,REAL,read it\n", encoding="utf-8")
    assert harvest_report.load_truth(tmp_path / "min.csv") == {"a.py::f": "REAL"}


def test_family_of_table():
    assert {ref: tr.family_of(ref) for ref in (
        "kenary/agnes-3-0-flash:free", "kenary/hy3:free", "tokenharbor/deepseek-v4-flash:free",
        "kenary/nemotron-3-ultra-550b-a55b:free", "sensenova123/sensenova-6.8-flash-lite",
        "sensenova123/sensenova-6.7-flash-lite")} == {
        "kenary/agnes-3-0-flash:free": "agnes", "kenary/hy3:free": "hy",
        "tokenharbor/deepseek-v4-flash:free": "deepseek",
        "kenary/nemotron-3-ultra-550b-a55b:free": "nemotron",
        "sensenova123/sensenova-6.8-flash-lite": "sensenova",
        "sensenova123/sensenova-6.7-flash-lite": "sensenova"}
    assert tr.family_of("claim_vote_llm.a", "kenary/agnes-3-0-flash:free") == "agnes"
    # a trailing version is not a family (review of cc-epic-docs): qwen3 and qwen-2.5 are one
    assert tr.family_of("qwen/qwen3-235b:free") == tr.family_of("qwen/qwen-2.5-72b:free") == "qwen"
    assert tr.family_of("x/4o") == "4o"


def test_checked_by_names_families_and_sha():
    res = lambda m, n=1: {"model": m, "run": 0, "votes": {str(k): "TRUE" for k in range(n)}}  # noqa: E731
    votes = {"target": {"sha": "d36d59e" + "0" * 33},
             "results": [res("kenary/nemotron-3-ultra-550b-a55b:free"), res("kenary/agnes-3-0-flash:free"),
                         res("kenary/hy3:free"), res("kenary/agnes-3-0-flash:free")]}
    assert tr.checked_by(votes) == "claimcheck:agnes+hy+nemotron@d36d59e"
    assert tr.checked_by(votes, base_sha="1234567abc") == "claimcheck:agnes+hy+nemotron@1234567..d36d59e"
    assert tr.checked_by({"results": votes["results"]}) == "claimcheck:agnes+hy+nemotron"


def test_dead_voter_is_not_in_checked_by(votes):
    by = tr.checked_by(votes)
    assert "sensenova" not in by
    assert by == f"claimcheck:agnes+deepseek+hy+nemotron@{votes['target']['sha'][:7]}"


def test_how_is_one_csv_safe_line(tmp_path):
    quote = 'say "hi"; then, ok\n    next line'
    row = _row("TRUE", True, kind="code", evidence=["src:a.py:1-2", "diff:1111111..2222222:a.py:1-2"],
               quotes=[quote])
    how = tr.how_of(row)
    assert how == 'src:a.py:1-2 diff:1111111..2222222:a.py:1-2; "say \\"hi\\"\\; then, ok next line"'
    assert "\n" not in how
    long = tr.how_of(_row("TRUE", True, kind="code", evidence=["src:a.py:1-2"], quotes=["x" * 400]))
    assert len(long.split("; ", 1)[1].strip('"')) == tr.HOW_QUOTE
    rows = tr.to_truth_rows({"claims": [row]}, checked_by="c")
    tr.write_truth(tmp_path / "t.csv", rows)
    text = (tmp_path / "t.csv").read_text(encoding="utf-8")
    assert text.count("\n") == 2                       # header + one record, one line each
    assert list(csv.DictReader(io.StringIO(text)))[0]["how"] == how


def test_how_of_a_world_claim_without_evidence():
    assert tr.how_of(_row("TRUE", True, kind="world")) == "world fact"
    assert tr.how_of({"verdict": "TRUE", "unanimous": True}) == "world fact"   # a run without --target


def test_claim_with_a_newline_is_one_record(tmp_path):
    rows = tr.to_truth_rows({"claims": [_row("FALSE", True, claim="two\nlines")]}, checked_by="c")
    tr.write_truth(tmp_path / "t.csv", rows)
    assert len(harvest_report.load_truth(tmp_path / "t.csv")) == 1
    md = tr.pending_markdown({"claims": [_row("SPLIT", False, claim="two\nlines")]})
    assert "> two\n> lines" in md


def test_duplicate_of():
    votes = {"claims": [_row("TRUE", True, claim="a", evidence=["src:x.py:1-2"], kind="code"),
                        _row("TRUE", True, claim="b", evidence=["src:x.py:5-9"], kind="code"),
                        _row("FALSE", True, claim="c", evidence=["src:x.py:5-9"], kind="code")]}
    rows = tr.to_truth_rows(votes, checked_by="c")
    assert [(r["finding"], r["truth"], r["duplicate_of"]) for r in rows] == [
        ("x.py", "REAL", ""), ("x.py", "REAL", "x.py"), ("x.py", "FALSE", "")]
    assert tr.disagreements(rows) == ["x.py"]


def test_empty_votes_writes_the_header_only(tmp_path):
    out = tmp_path / "t.csv"
    (tmp_path / "v.json").write_text("[]", encoding="utf-8")
    assert ct.main([str(tmp_path / "v.json"), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == ",".join(tr.TRUTH_HEADER) + "\n"
    assert "Nothing pending" in (tmp_path / "pending.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ --append, conflicts, atomic

def _votes_file(tmp_path, claims, name="v.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"claims": claims, "results": []}), encoding="utf-8")
    return path


def test_append_is_idempotent(tmp_path):
    v = _votes_file(tmp_path, [_row("TRUE", True, claim="a", evidence=["src:x.py:1-2"], kind="code"),
                               _row("FALSE", True, claim="b")])
    out = tmp_path / "t.csv"
    assert ct.main([str(v), "--out", str(out), "--append"]) == 0
    first = out.read_bytes()
    assert ct.main([str(v), "--out", str(out), "--append"]) == 0
    assert out.read_bytes() == first
    assert len(list(csv.DictReader(io.StringIO(first.decode())))) == 2


def test_append_keeps_the_existing_columns(tmp_path):
    out = tmp_path / "t.csv"
    out.write_text("finding,truth,how,severity\nold.py::f,FIXED,by hand,HIGH\n", encoding="utf-8")
    v = _votes_file(tmp_path, [_row("TRUE", True, claim="a", evidence=["src:x.py:1-2"], kind="code")])
    assert ct.main([str(v), "--out", str(out), "--append"]) == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0] == {"finding": "old.py::f", "truth": "FIXED", "how": "by hand", "severity": "HIGH",
                       "checked_by": "", "duplicate_of": ""}
    assert rows[1]["finding"] == "x.py" and rows[1]["truth"] == "REAL"


def test_append_conflict_exits_3_and_keeps_the_old_row(tmp_path, capsys):
    out = tmp_path / "t.csv"
    out.write_text("finding,truth,checked_by,duplicate_of,how\nx.py,FALSE,human,,read it\n", encoding="utf-8")
    before = out.read_bytes()
    v = _votes_file(tmp_path, [_row("TRUE", True, claim="a", evidence=["src:x.py:1-2"], kind="code")])
    assert ct.main([str(v), "--out", str(out), "--append"]) == 3
    assert out.read_bytes() == before
    assert "CONFLICT x.py: " in capsys.readouterr().out


def test_atomic_write(tmp_path, monkeypatch):
    out = tmp_path / "t.csv"
    out.write_text("finding,truth\nkeep,REAL\n", encoding="utf-8")
    real = tr.os.replace

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(tr.os, "replace", boom)
    with pytest.raises(OSError):
        tr.write_truth(out, [{"finding": "new", "truth": "FALSE", "checked_by": "", "duplicate_of": "", "how": ""}])
    monkeypatch.setattr(tr.os, "replace", real)
    assert out.read_text(encoding="utf-8") == "finding,truth\nkeep,REAL\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.csv"]     # no temporary file left


# ------------------------------------------------------------------ pending.md

def test_pending_lists_unsettled_with_chunk_ids(votes, repo, tmp_path):
    root, sha = repo
    _, packs, _ = tr.rebuild(votes, root, scratch=tmp_path / "s2")
    md = tr.pending_markdown(votes, packs)
    assert f"sha: {sha}" in md
    assert "dead: " + DEAD in md
    for v in VOTERS:
        assert v in md
    split, dissent = md.index("## SPLIT"), md.index("## TRUE / FALSE with a dissenter")
    assert split < dissent
    # the dissent's entry names its pack's chunks, with the first lines
    entry = md[dissent:]
    assert "`src:app.py:" in entry
    assert "def compact(self):" in entry
    assert "| kenary/hy3:free | (no vote) |" in md          # the missed claim: hy3 skipped it


def test_pending_order_split_codecheck_unsure():
    votes = {"claims": [_row("UNSURE", False, claim="u"), _row("CODE-CHECK", False, claim="cc", kind="code"),
                        _row("SPLIT", False, claim="s"), _row("TRUE", True, claim="ok")]}
    md = tr.pending_markdown(votes)
    assert md.index("> s") < md.index("> cc") < md.index("> u")
    assert "> ok" not in md
    assert md.count("### claim") == 3


def test_pending_previews_are_cut_at_three_lines():
    from tools.claimcheck.model import Chunk, Pack
    text = "\n".join(f"line {k} " + "y" * 300 for k in range(10))
    pack = Pack(claim="s", sha="0" * 40, chunks=(Chunk("src:a.py:1-10", "source", "a.py", 1, 10, text, "w"),),
                truncated=False)
    md = tr.pending_markdown({"claims": [_row("SPLIT", False, claim="s", kind="code")]}, [pack])
    shown = [ln for ln in md.splitlines() if ln.startswith("      line ")]
    assert len(shown) == tr.PREVIEW_LINES
    assert all(len(ln.strip()) <= tr.PREVIEW_CHARS for ln in shown)


def test_pending_for_500_claims_lists_each_once():
    votes = {"claims": [_row("SPLIT" if k % 2 else "UNSURE", False, claim=f"claim number {k}.")
                        for k in range(500)]}
    md = tr.pending_markdown(votes)
    assert md.count("### claim") == 500
    assert all(md.count(f"> claim number {k}.\n") == 1 for k in range(500))


def test_pending_rebuilds_the_pack_from_the_recorded_sha(votes, repo, tmp_path):
    """The CLI opens the recorded sha (not the repo's moved HEAD), lists the pack, and leaves
    votes.json byte for byte as it was."""
    root, sha = repo
    (root / "app.py").write_text("# moved on\n", encoding="utf-8")
    _git(root, "commit", "-qam", "two")
    path = tmp_path / "votes.json"
    path.write_text(json.dumps(votes, ensure_ascii=False, indent=1), encoding="utf-8")
    before = path.read_bytes()
    out = tmp_path / "out" / "truth.csv"
    assert ct.main([str(path), "--out", str(out), "--scratch", str(tmp_path / "s3"),
                    "--repo-root", str(tmp_path)]) == 0
    assert path.read_bytes() == before
    md = (out.parent / "pending.md").read_text(encoding="utf-8")
    assert "def compact(self):" in md                       # from the recorded sha, not "# moved on"
    assert "moved on" not in md
    keys = harvest_report.load_truth(out)
    assert keys["app.py::run_step"] == "REAL" and keys["lib.py::run_lib"] == "FALSE"


def test_unopenable_target_falls_back_to_the_chunk_ids(votes, tmp_path, capsys):
    path = tmp_path / "votes.json"
    path.write_text(json.dumps(votes), encoding="utf-8")
    out = tmp_path / "t.csv"
    assert ct.main([str(path), "--out", str(out), "--target", str(tmp_path / "gone"),
                    "--scratch", str(tmp_path / "s4"), "--repo-root", str(tmp_path)]) == 0
    assert "warning: cannot rebuild" in capsys.readouterr().err
    assert set(harvest_report.load_truth(out)) == {"app.py", "lib.py", tr.claim_hash(C_WORLD)}
    md = (tmp_path / "pending.md").read_text(encoding="utf-8")
    assert "evidence cited: `src:app.py:" in md            # the dissent's accepted votes cited it


# ------------------------------------------------------------------ the readers downstream

def test_harvest_report_reads_the_csv(votes, repo, tmp_path):
    root, _ = repo
    resolved, _, _ = tr.rebuild(votes, root, scratch=tmp_path / "s2")
    rows = tr.to_truth_rows(votes, checked_by=tr.checked_by(votes), resolved=resolved)
    tr.write_truth(tmp_path / "truth.csv", rows)
    assert harvest_report.load_truth(str(tmp_path / "truth.csv")) == {r["finding"]: r["truth"] for r in rows}


def test_truth_consensus_reads_the_csv(votes, repo, tmp_path):
    root, _ = repo
    resolved, _, _ = tr.rebuild(votes, root, scratch=tmp_path / "s2")
    rows = tr.to_truth_rows(votes, checked_by="c", resolved=resolved)
    tr.write_truth(tmp_path / "truth-claimcheck.csv", rows)
    got = truth_consensus.load([str(tmp_path / "truth-claimcheck.csv")])
    assert [(r["finding"], r["truth"], r["_adj"]) for r in got] == [
        (r["finding"], r["truth"], "claimcheck") for r in rows]


# ------------------------------------------------------------------ run_claim_check.sh

def _dry(tmp_path, *args):
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "run_claim_check.sh"), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=30)
    return proc, [shlex.split(ln) for ln in proc.stdout.splitlines() if ln.startswith("python3 ")]


def test_run_claim_check_dry_run(tmp_path):
    proc, cmds = _dry(tmp_path, "r.md", "--target", ".", "--ref", "HEAD", "--truth", "t.csv", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert [c[1] for c in cmds] == ["scripts/claim_extract.py", "scripts/claim_vote.py",
                                    "scripts/claimcheck_truth.py"]
    vote = cmds[1]
    assert vote[vote.index("--target") + 1] == "." and vote[vote.index("--ref") + 1] == "HEAD"
    out = vote[vote.index("--out") + 1].rsplit("/", 1)[0]
    assert re.fullmatch(r"claim-check-out/\d{8}T\d{6}Z", out)
    assert cmds[2][2:] == [f"{out}/votes.json", "--out", f"{out}/t.csv", "--pending", f"{out}/pending.md"]
    assert list(tmp_path.iterdir()) == []                   # nothing written


def test_dry_run_and_truth_are_not_passed_to_claim_vote(tmp_path):
    _, cmds = _dry(tmp_path, "r.md", "--dry-run", "--profiles", "a", "b", "c", "--truth=x.csv",
                   "--base", "main", "--fetch")
    vote = cmds[1]
    assert not any(a.startswith(("--truth", "--dry-run")) for a in vote)
    assert vote[-7:] == ["--profiles", "a", "b", "c", "--base", "main", "--fetch"]


def test_run_claim_check_old_form_unchanged(tmp_path):
    """No new option: the 03a77c5 command list (extract, then vote with --runs and the options)."""
    proc, cmds = _dry(tmp_path, "r.md", "--profiles", "a", "b", "--dry-run")
    out = cmds[0][-1].rsplit("/", 1)[0]
    assert cmds == [["python3", "scripts/claim_extract.py", "r.md", "--out", f"{out}/claims.json"],
                    ["python3", "scripts/claim_vote.py", f"{out}/claims.json", "--runs", "3",
                     "--out", f"{out}/votes.json", "--profiles", "a", "b"]]
    _, plain = _dry(tmp_path, "r.md", "--dry-run")
    assert plain[1][-2:] == ["--out", plain[1][-1]] and plain[1][-1].endswith("/votes.json")
