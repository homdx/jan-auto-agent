"""CC-11 (293): `--accept committed` — an abstainer does not block a verdict, a dissent does; `all` stays today's rule."""

from __future__ import annotations

import csv
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.auto.llm_profile import LlmSettings
from tools.claimcheck import judge
from tools.claimcheck import truth as tr
from tools.claimcheck.pack import PackBudget
from tools.claimcheck.target import Target

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "contest-bench" / "cc"))
import claim_diff  # noqa: E402
import claim_vote as cv  # noqa: E402
import claimcheck_truth as ct  # noqa: E402
import replay_rules  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("CC-11's tests must not open a socket")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ a target and voters in the live reply shapes

_GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
            "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}
APP = ("import subprocess\n\n\ndef run_a(cmd):\n    return subprocess.run(cmd, check=False)\n\n\n"
       "def run_b(cmd):\n    return subprocess.run(cmd, check=True)\n\n\n"
       "def run_c(cmd):\n    return subprocess.run(cmd, check=False)\n")
#: at head run_a is fixed (check=True): the claim about it is FIXED for claim_diff
APP_HEAD = APP.replace("def run_a(cmd):\n    return subprocess.run(cmd, check=False)",
                       "def run_a(cmd):\n    return subprocess.run(cmd, check=True)")
C_A = "`run_a` in `app.py` calls subprocess.run with check=False."
C_B = "`run_b` in `app.py` calls subprocess.run with check=False."
C_C = "`run_c` in `app.py` calls subprocess.run with check=False."
CLAIMS = [C_A, C_B, C_C]
#: the families of the refs below: agnes, hy, nemotron, step, muse (five families)
AG, HY, NE, ST, MU = ("kenary/agnes-3-0-flash:free", "kenary/hy3:free", "kenary/nemotron-3-ultra-550b-a55b:free",
                      "kenary/step-5-preview:free", "kenary/muse-spark-1-3-contributor:free")
DEAD = "kenary/laguna-s-2-1:free"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=_GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(root, base sha, head sha): app.py at base, and with run_a fixed at head."""
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "app")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text(APP_HEAD, encoding="utf-8")
    _git(root, "commit", "-q", "-am", "fix run_a")
    return root, base, _git(root, "rev-parse", "HEAD")


def _reply(prompt: str, decide, *, two_lines: bool = False) -> str:
    """The reply of a voter that reads the pack, in the live shapes: prose with a stray `"quote"`
    first, then one object per claim with no list around them, the quote (optionally two lines
    of the chunk, a bare newline inside the string) taken from the pack it was shown.
    *decide(claim, line)*: TRUE / FALSE / UNSURE, or None to leave the claim out."""
    blocks = re.finditer(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", prompt, re.S)
    out = ['Reading the evidence. A "quote": must be verbatim, so here goes.']
    for m in blocks:
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        name = claim.split("`")[1]
        # the source chunk of the tree it was shown (at head the pack also has the diff: not read here)
        chunks = re.split(r"^\[\[(.+?)\]\]  \(\w+\)$", evidence, flags=re.M)[1:]
        found = [(cid, text.splitlines()) for cid, text in zip(chunks[::2], chunks[1::2])
                 if cid.startswith("src:") and f"def {name}(" in text]
        if not found:
            out.append(json.dumps({"id": n, "verdict": "UNSURE"}))
            continue
        cid, lines = found[0]
        at = next(k for k, ln in enumerate(lines) if f"def {name}(" in ln)
        body = [ln.split("| ", 1)[1].strip() if "| " in ln else ln.strip() for ln in lines[at:at + 2]]
        verdict = decide(claim, body[1])
        if verdict is None:
            continue
        row = {"id": n, "verdict": verdict}
        if verdict in ("TRUE", "FALSE"):
            quote = body[1] if not two_lines else body[0] + "\n" + body[1]
            row.update(chunk=f"[[{cid}]]", quote=quote)
        # json.dumps writes the newline as \n; a live model writes it bare, which the parser takes
        out.append(json.dumps(row).replace("\\n", "\n"))
    return "\n".join(out)


def reader(line: str) -> str:
    """The perfect reader: TRUE when the cited line has check=False."""
    return "TRUE" if "check=False" in line else "FALSE"


#: a voter's behaviour: claim, line -> verdict (None: the claim is left out of the reply)
PERFECT = lambda claim, line: reader(line)                                     # noqa: E731
UNSURE_ON = lambda *cs: (lambda claim, line: "UNSURE" if claim in cs else reader(line))   # noqa: E731
SKIPS = lambda *cs: (lambda claim, line: None if claim in cs else reader(line))           # noqa: E731
LIAR_ON = lambda *cs: (lambda claim, line: ({"TRUE": "FALSE", "FALSE": "TRUE"}[reader(line)]
                                            if claim in cs else reader(line)))         # noqa: E731


def make_votes(root, sha, scratch, voters: dict, *, dead=(), accept="all", runs=1) -> dict:
    """votes.json as `claim_vote.main_target` writes it, through a JSON round trip (string keys).
    *voters*: ref -> behaviour; *dead*: refs whose every call is an HTTP 404."""
    claims = [{"claim": c, "truth": None} for c in CLAIMS]
    with Target.open(str(root), sha, scratch=scratch) as target:
        items, meta = cv.prepare_target(claims, target, PackBudget())
        results = []
        for k, (ref, decide) in enumerate(voters.items()):
            for run in range(runs):
                got = judge.ask_with_packs(items, run, 1, lambda p, d=decide, k=k: _reply(p, d, two_lines=k % 2 == 1),
                                           batch=10, code_batch=2)
                results.append({"model": ref, "run": run, "votes": got["votes"],
                                "accepted": got["accepted"], "rejected": got["rejected"]})
        for ref in dead:
            def gone(_p):
                raise RuntimeError("HTTP 404 no such model")
            got = judge.ask_with_packs(items, 0, 1, gone, batch=10, code_batch=2)
            results.append({"model": ref, "run": 0, "votes": got["votes"], "accepted": got["accepted"],
                            "rejected": got["rejected"], "error": got["errors"][0]})
        table = cv.tally(claims, results, meta=meta, accept=accept)
        report = {"target": {"repo": str(root), "ref": sha, "sha": target.sha, "base": None,
                             "warnings": []}, "accept": accept, "results": results, "claims": table}
    return json.loads(json.dumps(report, ensure_ascii=False))


def rows(votes: dict) -> dict:
    return {c["claim"]: c for c in votes["claims"]}


def _both(repo, tmp_path, voters, **kw) -> tuple:
    root, base, _head = repo
    return (make_votes(root, base, tmp_path / "s-all", voters, accept="all", **kw),
            make_votes(root, base, tmp_path / "s-com", voters, accept="committed", **kw))


# ------------------------------------------------------------------ the rule

def test_default_is_all_and_unchanged(repo, tmp_path):
    """No rule given is `all`; under `all` a row has no `abstained`, and the table is today's."""
    root, base, _head = repo
    five = {AG: PERFECT, HY: PERFECT, NE: PERFECT, ST: UNSURE_ON(C_A), MU: SKIPS(C_B)}
    votes = make_votes(root, base, tmp_path / "s", five)
    claims = [{"claim": c, "truth": None} for c in CLAIMS]
    with Target.open(str(root), base, scratch=tmp_path / "s2") as target:
        _items, meta = cv.prepare_target(claims, target, PackBudget())
    default = cv.tally(claims, votes["results"], meta=meta)
    assert default == votes["claims"] == cv.tally(claims, votes["results"], meta=meta, accept="all")
    assert all("abstained" not in r for r in default)
    assert [r["unanimous"] for r in default] == [False, False, True]     # one abstainer blocks under all
    with pytest.raises(ValueError):
        cv.tally(claims, votes["results"], meta=meta, accept="most")


def test_abstainer_does_not_block_under_committed(repo, tmp_path):
    """3 TRUE, one UNSURE, one that left the claim out: accepted under committed, not under all."""
    five = {AG: PERFECT, HY: PERFECT, NE: PERFECT, ST: UNSURE_ON(C_A), MU: SKIPS(C_A)}
    v_all, v_com = _both(repo, tmp_path, five)
    a, c = rows(v_all)[C_A], rows(v_com)[C_A]
    assert (a["verdict"], a["unanimous"]) == ("TRUE", False)
    assert (c["verdict"], c["unanimous"]) == ("TRUE", True)
    assert c["abstained"] == sorted([ST, MU])
    assert c["evidence"] and c["quotes"]                     # accepted on quotes the code verified
    full = rows(v_com)[C_B]                                  # everybody committed: both rules alike
    assert full["unanimous"] is rows(v_all)[C_B]["unanimous"] is True and full["abstained"] == []


def test_dissent_is_not_accepted_either_way(repo, tmp_path):
    """TRUE, TRUE, TRUE, FALSE, UNSURE: a plurality, never a settled row, under both rules."""
    five = {AG: PERFECT, HY: PERFECT, NE: PERFECT, ST: LIAR_ON(C_A), MU: UNSURE_ON(C_A)}
    v_all, v_com = _both(repo, tmp_path, five)
    for v in (v_all, v_com):
        r = rows(v)[C_A]
        assert (r["verdict"], r["unanimous"]) == ("TRUE", False)
        assert tr.settled(r) is None
    assert rows(v_com)[C_A]["abstained"] == [MU]             # the liar committed: it is no abstainer


def test_two_committed_are_below_quorum(repo, tmp_path):
    five = {AG: PERFECT, HY: PERFECT, NE: UNSURE_ON(C_A), ST: UNSURE_ON(C_A), MU: SKIPS(C_A)}
    _v_all, v_com = _both(repo, tmp_path, five)
    r = rows(v_com)[C_A]
    assert (r["verdict"], r["unanimous"]) == ("UNSURE", False)
    assert r["abstained"] == sorted([NE, ST, MU])


def test_dead_voters_cost_nothing(repo, tmp_path):
    """5 voters of which 2 return nothing (HTTP 404): the table of the three live ones, both rules."""
    root, base, _head = repo
    three = {AG: PERFECT, HY: PERFECT, NE: UNSURE_ON(C_C)}
    for accept in ("all", "committed"):
        live = make_votes(root, base, tmp_path / f"a-{accept}", three, accept=accept)
        with_dead = make_votes(root, base, tmp_path / f"b-{accept}", three, dead=(DEAD, DEAD + "x"),
                               accept=accept)
        assert with_dead["claims"] == live["claims"]
        assert tr.dead_voters(with_dead) == [DEAD, DEAD + "x"]


# ------------------------------------------------------------------ the oracle: the recorded free-model verdicts

#: the ticket's table: set -> (all, committed) as settled / wrong over the 148 claims with a truth
ORACLE = {"3": ((138, 0), (138, 0)), "5": ((132, 0), (146, 0)), "7": ((108, 0), (146, 0)),
          "9": ((91, 0), (146, 0)), "default3": ((136, 0), (136, 0)), "5weak": ((86, 0), (139, 0))}


def _replay_through_tally(voters: list, accept: str) -> tuple:
    data = json.loads(replay_rules.DATA.read_text(encoding="utf-8"))
    word = {1: "TRUE", -1: "FALSE", 0: "UNSURE"}
    claims = [{"claim": f"claim {row['id']}", "truth": row["truth"]} for row in data["claims"]]
    results = [{"model": v, "run": 0, "votes": {i: word[data["voters"][v][i]] for i in range(len(claims))}}
               for v in voters]
    meta = [{"kind": "world", "dangling": False, "sha": ""} for _ in claims]
    table = cv.tally(claims, results, meta=meta, accept=accept)
    settled = wrong = 0
    for row, t in zip(table, data["claims"]):
        if t["truth"] and row["unanimous"] and row["verdict"] in ("TRUE", "FALSE"):
            settled += 1
            wrong += row["verdict"] != {1: "TRUE", -1: "FALSE"}[t["truth"]]
    return settled, wrong


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_replay_matches_the_oracle(name):
    """`tally` fed `free_votes.json` gives exactly replay_rules.py's counts, under both rules."""
    voters = replay_rules.SETS[name]
    for k, accept in enumerate(("all", "committed")):
        got = _replay_through_tally(voters, accept)
        assert got == replay_rules.replay(voters, accept) == ORACLE[name][k], (name, accept)


# ------------------------------------------------------------------ into truth.csv (CC-8)

def test_families_still_required(repo, tmp_path):
    """Three committed voters of one family, two abstaining others: no row, pending under FAMILIES."""
    root, base, _head = repo
    ag2, ag3 = "kenary/agnes-2-5-flash:free", "other/agnes-1-0:free"
    five = {AG: PERFECT, ag2: PERFECT, ag3: PERFECT, HY: UNSURE_ON(C_A), NE: SKIPS(C_A)}
    votes = make_votes(root, base, tmp_path / "s", five, accept="committed")
    r = rows(votes)[C_A]
    assert (r["verdict"], r["unanimous"]) == ("TRUE", True)
    assert tr.families_behind(r) == 1 and tr.settled(r) is None
    assert C_A not in {row["finding"] for row in tr.to_truth_rows(votes, checked_by="x")}
    md = tr.pending_markdown(votes)
    section = md.split("## TRUE / FALSE, unanimous, but from fewer than 3 model families", 1)[1]
    assert C_A in section.split("\n## ", 1)[0]
    assert f"abstained: {', '.join(sorted([HY, NE]))}" in section


def _truth(tmp_path, votes, *extra) -> tuple:
    path = tmp_path / "votes.json"
    path.write_text(json.dumps(votes), encoding="utf-8")
    out = tmp_path / "truth.csv"
    rc = ct.main([str(path), "--out", str(out), "--no-lenz-hint", "--repo-root", str(tmp_path),
                  "--scratch", str(tmp_path / "rebuild"), *extra])
    with out.open(newline="", encoding="utf-8") as fh:
        return rc, list(csv.DictReader(fh)), (tmp_path / "pending.md").read_text(encoding="utf-8")


def test_truth_row_says_how_many_committed(repo, tmp_path):
    """A row accepted over two abstainers: `how` says 3 of 5, `checked_by` names the three families."""
    root, base, _head = repo
    five = {AG: PERFECT, HY: PERFECT, NE: PERFECT, ST: UNSURE_ON(C_A), MU: SKIPS(C_A)}
    votes = make_votes(root, base, tmp_path / "s", five, dead=(DEAD,), accept="committed")
    rc, got, pending = _truth(tmp_path, votes)
    assert rc == 0
    by = {r["finding"]: r for r in got}
    sha7 = votes["target"]["sha"][:7]
    a = next(r for r in got if r["how"].startswith("3 of 5 voters committed; "))
    assert a["truth"] == "REAL" and a["checked_by"] == f"claimcheck:agnes+hy+nemotron@{sha7}"
    assert "src:app.py:" in a["how"] and "check=False" in a["how"]
    full = [r for r in by.values() if r is not a]
    assert full and all(r["how"].startswith("5 of 5 voters committed; ") for r in full)
    assert all(r["checked_by"] == f"claimcheck:agnes+hy+muse+nemotron+step@{sha7}" for r in full)
    # under `all` the same votes give no row for the claim, and the other rows as before
    v_all = make_votes(root, base, tmp_path / "s2", five, dead=(DEAD,), accept="all")
    (tmp_path / "all").mkdir()
    _rc, got_all, _p = _truth(tmp_path / "all", v_all)
    assert len(got_all) == len(got) - 1
    assert all(not r["how"][0].isdigit() for r in got_all)
    assert "laguna" not in a["checked_by"] and f"dead: {DEAD}" in pending


def test_old_votes_json_is_read_as_all(repo, tmp_path):
    """A votes.json written before CC-11 (no `accept`) loads and rebuilds as one with `accept: all`."""
    root, base, _head = repo
    five = {AG: PERFECT, HY: PERFECT, NE: PERFECT, ST: UNSURE_ON(C_A), MU: PERFECT}
    votes = make_votes(root, base, tmp_path / "s", five, accept="all")
    old = {k: v for k, v in votes.items() if k != "accept"}
    assert tr.accept_of(old) == tr.accept_of(votes) == "all"
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    got_old = _truth(tmp_path / "old", old)
    got_new = _truth(tmp_path / "new", votes)
    assert got_old == got_new and got_old[0] == 0 and len(got_old[1]) == 2


# ------------------------------------------------------------------ the command lines

@pytest.fixture
def http(monkeypatch):
    """Five voters behind the real ask/ask_v2 (the HTTP call is the only thing faked): s4 says
    UNSURE on C_A, s5 leaves C_A out, `dead/…` is a 404."""
    behave = {"s1": PERFECT, "s2": PERFECT, "s3": PERFECT, "s4": UNSURE_ON(C_A), "s5": SKIPS(C_A)}
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url=f"http://{ref.split('/')[0]}.invalid/v1", api_key="k", model=ref,
        temperature=0.3, max_tokens=100))

    def completion(url, headers, payload, timeout, **_k):
        model = payload["model"]
        if model.startswith("dead"):
            raise RuntimeError("HTTP 404 no such model")
        return _reply(payload["messages"][-1]["content"], behave[model.split("/")[1]],
                      two_lines=model.endswith("s2"))
    monkeypatch.setattr(cv, "request_completion", completion)


VOTERS5 = ["x/s1", "x/s2", "x/s3", "x/s4", "x/s5", "dead/s6"]


def _vote(tmp_path, root, sha, *extra, ini=""):
    (tmp_path / "contest.local.ini").write_text(ini, encoding="utf-8")
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([{"id": f"c{i}", "claim": c} for i, c in enumerate(CLAIMS)]), encoding="utf-8")
    out = tmp_path / "votes.json"
    out.unlink(missing_ok=True)
    rc = cv.main([str(claims), "--target", str(root), "--ref", sha, "--profiles", *VOTERS5,
                  "--runs", "1", "--interval", "0", "--repo-root", str(tmp_path),
                  "--scratch", str(tmp_path / "scratch"), "--out", str(out), *extra])
    return rc, (json.loads(out.read_text()) if out.is_file() else None)


def test_accept_option_and_ini(repo, tmp_path, http, capsys):
    root, base, head = repo
    rc, v = _vote(tmp_path, root, base)
    assert rc == 0 and v["accept"] == "all" and not rows(v)[C_A]["unanimous"]
    assert all("abstained" not in r for r in v["claims"])
    rc, v = _vote(tmp_path, root, base, ini="[claim_vote]\naccept = committed\n")
    assert rc == 0 and v["accept"] == "committed" and rows(v)[C_A]["unanimous"] is True
    assert rows(v)[C_A]["abstained"] == ["x/s4", "x/s5"]                # the dead voter is no voter
    rc, v = _vote(tmp_path, root, base, "--accept", "all", ini="[claim_vote]\naccept = committed\n")
    assert rc == 0 and v["accept"] == "all"                              # the option beats the ini
    capsys.readouterr()
    for extra, ini in ((["--accept", "most"], ""), ([], "[claim_vote]\naccept = most\n")):
        rc, v = _vote(tmp_path, root, base, *extra, ini=ini)
        err = capsys.readouterr().err.strip().splitlines()
        assert rc == 2 and v is None and len(err) == 1 and "must be all or committed" in err[0]

    # claim_diff.py --accept reaches both sides, and delta.json records the rule
    def diff(*extra):
        out = tmp_path / f"diff{len(extra)}"
        rc = claim_diff.main([str(tmp_path / "claims.json"), "--target", str(root), "--base", base,
                              "--head", head, "--profiles", *VOTERS5, "--runs", "1", "--interval", "0",
                              "--repo-root", str(tmp_path), "--scratch", str(tmp_path / "scratch"),
                              "--no-collect", "--out", str(out), *extra])
        sides = {s: json.loads((out / f"votes_{s}.json").read_text()) for s in ("base", "head")}
        return rc, json.loads((out / "delta.json").read_text()), sides
    (tmp_path / "contest.local.ini").write_text("", encoding="utf-8")
    rc, d_all, s_all = diff()
    rc2, d_com, s_com = diff("--accept", "committed")
    assert rc == rc2 == 0
    assert "accept" not in d_all and d_com["accept"] == "committed"   # `all` writes delta.json as before
    assert {s["accept"] for s in s_all.values()} == {"all"} and {s["accept"] for s in s_com.values()} == {"committed"}
    change = {d["claim"]: d["change"] for d in d_com["deltas"]}
    assert change[C_A] == "FIXED" and {d["claim"]: d["change"] for d in d_all["deltas"]}[C_A] == "UNCLEAR"
    assert all(r.get("abstained") is not None for s in s_com.values() for r in s["claims"])
    assert claim_diff.main([str(tmp_path / "claims.json"), "--target", str(root), "--base", base,
                            "--head", head, "--profiles", *VOTERS5, "--accept", "bogus",
                            "--repo-root", str(tmp_path), "--out", str(tmp_path / "bad")]) == 2
    assert not (tmp_path / "bad").exists()
