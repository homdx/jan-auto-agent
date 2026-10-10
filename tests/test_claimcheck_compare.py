"""CC-7 (285): before and after — classify_delta, compare, claim_diff's two sides, delta.json, --check."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from tools.auto.llm_profile import LlmSettings
from tools.claimcheck import compare as cc
from tools.claimcheck import judge
from tools.claimcheck.judge import ask_with_packs
from tools.claimcheck.model import Anchor, ResolvedAnchor
from tools.claimcheck.target import Target

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "contest-bench" / "cc"))
import claim_diff as cd  # noqa: E402
import claim_vote as cv  # noqa: E402
import score_cc  # noqa: E402


# ------------------------------------------------------------------ no network, for the whole module

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("CC-7 must not open a socket")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_no_network():
    with pytest.raises(AssertionError, match="must not open a socket"):
        socket.create_connection(("example.com", 80))


# ------------------------------------------------------------------ a repository with a before and an after

_GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
            "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}
APP_BASE = ("import subprocess\n\n\ndef run_step(cmd):\n    return subprocess.run(cmd, check=False)\n\n\n"
            "def old_name(cmd):\n    return subprocess.run(cmd, check=False)\n")
APP_HEAD = ("import subprocess\n\n\ndef run_step(cmd):\n    return subprocess.run(cmd, check=True)\n\n\n"
            "def new_name(cmd):\n    return subprocess.run(cmd, check=False)\n")
LIB = "import subprocess\n\n\ndef run_lib(cmd):\n    return subprocess.run(cmd, check=False)\n"

C_FIX = {"id": "c-fix", "claim": "`run_step` in `app.py` calls subprocess.run with check=False.", "expect": "fixed"}
C_STILL = {"id": "c-still", "claim": "`run_lib` in `lib.py` calls subprocess.run with check=False.", "expect": "still"}
C_GONE = {"id": "c-gone", "claim": "`old_name` in `app.py` calls subprocess.run with check=False.", "expect": "gone"}
C_NEW = {"id": "c-new", "claim": "`run_step` in `app.py` calls subprocess.run with check=True.", "expect": "new"}
C_WORLD = {"id": "c-world", "claim": "The Earth goes around the Sun once a year."}
CLAIMS = [C_FIX, C_STILL, C_GONE, C_NEW, C_WORLD]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=_GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(root, base sha, head sha): the head makes `run_step` strict and renames `old_name`."""
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text(APP_BASE, encoding="utf-8")
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text(APP_HEAD, encoding="utf-8")
    _git(root, "commit", "-q", "-am", "strict run_step, rename old_name")
    return root, base, _git(root, "rev-parse", "HEAD")


_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)


def _src_chunks(evidence: str) -> list:
    """(id, text) of each `src:` chunk of a rendered pack: the code as it is on that side."""
    labels = list(_LABEL.finditer(evidence))
    return [(m.group(1), evidence[m.end():labels[k + 1].start() if k + 1 < len(labels) else len(evidence)])
            for k, m in enumerate(labels) if m.group(1).startswith("src:")]


def read_rows(prompt: str, unsure: bool = False) -> list:
    """A voter that reads the pack's source: the line after `def <name>(` decides whether the
    claim's `check=X` is there, and that line is the quote. No such line: UNSURE."""
    rows = []
    for m in _BLOCK.finditer(prompt):
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        if unsure:
            rows.append({"id": n, "verdict": "UNSURE"})
            continue
        if judge.WORLD_BLOCK in evidence:
            rows.append({"id": n, "verdict": "TRUE"})
            continue
        name, token = claim.split("`")[1], re.search(r"check=\w+", claim).group(0)
        row = {"id": n, "verdict": "UNSURE"}
        for cid, text in _src_chunks(evidence):
            lines = text.splitlines()
            at = next((k for k, ln in enumerate(lines) if f"def {name}(" in ln), None)
            if at is not None and at + 1 < len(lines):
                line = lines[at + 1].split("| ", 1)[1].strip()
                row = {"id": n, "verdict": "TRUE" if token in line else "FALSE", "chunk": cid, "quote": line}
                break
        rows.append(row)
    return rows


def fake_vote(models=("a/x", "b/y", "c/z"), log=None, unsure=(), dead=()):
    """The seam's fake: every model x one run through `judge.ask_with_packs` (prompt v2, the
    parser, the quote check), answering with `read_rows`. *unsure*: (side, model) pairs that
    answer UNSURE everywhere; *dead*: models with no vote at all."""
    log = log if log is not None else []

    def vote(items, side):
        rows = []
        for m in models:
            if m in dead:
                rows.append({"model": m, "run": 0, "votes": {}, "accepted": {}, "rejected": [],
                             "error": "HTTP 404"})
                continue

            def complete(prompt, m=m):
                log.append((side, m, prompt))
                return json.dumps(read_rows(prompt, unsure=(side, m) in unsure))
            got = ask_with_packs(items, 0, 1, complete)
            rows.append({"model": m, "run": 0, "votes": got["votes"], "accepted": got["accepted"],
                         "rejected": got["rejected"]})
        return rows
    return vote


def _judge(repo, tmp_path, vote, claims=CLAIMS, base=None, head=None):
    root, b, h = repo
    with ExitStack() as stack:
        bt = stack.enter_context(Target.open(root, base or b, scratch=tmp_path / "s"))
        ht = stack.enter_context(Target.open(root, head or h, scratch=tmp_path / "s"))
        got = cd.judge_both(cd.load_claims(_write(tmp_path, claims)), bt, ht, vote)
        cd.write_outputs(tmp_path / "out", bt, ht, got)
    return got


def _write(tmp_path, claims, name="claims.json"):
    path = tmp_path / name
    path.write_text(json.dumps(claims), encoding="utf-8")
    return path


def _changes(got):
    return {d.id: d.change for d in got["deltas"]}


# ------------------------------------------------------------------ the rule

UT = {"verdict": "TRUE", "unanimous": True}
UF = {"verdict": "FALSE", "unanimous": True}
TN = {"verdict": "TRUE", "unanimous": False}     # a plurality with a dissenter
US = {"verdict": "UNSURE", "unanimous": False}
SP = {"verdict": "SPLIT", "unanimous": False}
STATES = {"UT": UT, "UF": UF, "TN": TN, "US": US, "SP": SP}
# rows: base; columns: head  UT        UF        TN         US         SP
MATRIX = {"UT": ("STILL", "FIXED", "UNCLEAR", "UNCLEAR", "UNCLEAR"),
          "UF": ("NEW", "UNCLEAR", "UNCLEAR", "UNCLEAR", "UNCLEAR"),
          "TN": ("NEW", "UNCLEAR", "UNCLEAR", "UNCLEAR", "UNCLEAR"),
          "US": ("NEW", "UNCLEAR", "UNCLEAR", "UNCLEAR", "UNCLEAR"),
          "SP": ("NEW", "UNCLEAR", "UNCLEAR", "UNCLEAR", "UNCLEAR")}


@pytest.mark.parametrize("b,h", [(b, h) for b in STATES for h in STATES])
def test_classify_delta_table(b, h):
    """Every cell of the 5 x 5 matrix; the anchors found at both sides (no GONE)."""
    want = MATRIX[b][list(STATES).index(h)]
    assert cc.classify_delta(STATES[b], STATES[h]) == want
    # a vanished anchor wins over any votes, a world claim over everything
    assert cc.classify_delta(STATES[b], STATES[h], gone=True) == "GONE"
    assert cc.classify_delta(STATES[b], STATES[h], gone=True, world=True) == "n/a"


def test_false_at_both_is_unclear():
    assert cc.classify_delta(UF, UF) == "UNCLEAR"
    assert cc.classify_delta(None, None) == "UNCLEAR"


def test_world_claim_is_not_applicable(repo, tmp_path):
    got = _judge(repo, tmp_path, fake_vote())
    d = next(d for d in got["deltas"] if d.id == "c-world")
    assert d.change == "n/a" and d.base == cc.NOT_JUDGED
    assert d.head["verdict"] == "TRUE" and d.head["unanimous"]       # judged once, at head
    assert got["base"]["rows"][CLAIMS.index(C_WORLD)] is None
    assert len(got["base"]["table"]) == len(CLAIMS) - 1


def test_gone_when_the_symbol_vanishes(repo, tmp_path):
    """`old_name` is renamed at head: GONE, whatever the head votes say."""
    got = _judge(repo, tmp_path, fake_vote())
    d = next(d for d in got["deltas"] if d.id == "c-gone")
    assert d.change == "GONE" and d.base["verdict"] == "TRUE" and d.base["unanimous"]
    # the primary anchor: first symbol, else first path; found at base, not at head
    sym = lambda found, path="app.py": ResolvedAnchor(Anchor("symbol", "old_name", 1, 9), found, path=path)  # noqa: E731
    pth = lambda found: ResolvedAnchor(Anchor("path", "app.py", 14, 20), found, path="app.py")  # noqa: E731
    assert cc.primary_vanished([pth(True), sym(True)], [pth(True), sym(False)])        # dangling at head
    assert cc.primary_vanished([sym(True)], [sym(False, path="")])                    # file gone too
    assert not cc.primary_vanished([sym(False)], [sym(False)])                        # never there
    assert not cc.primary_vanished([pth(True), sym(True)], [pth(False), sym(True)])   # the symbol decides
    assert cc.primary_vanished([pth(True)], [pth(False)])                             # no symbol: the path
    assert not cc.primary_vanished([], [])
    assert cc.compare(["x"], [UT], [UF], gone=[True])[0].change == "GONE"            # votes say FIXED


def test_end_to_end_on_a_temporary_repo(repo, tmp_path):
    got = _judge(repo, tmp_path, fake_vote())
    assert _changes(got) == {"c-fix": "FIXED", "c-still": "STILL", "c-gone": "GONE",
                             "c-new": "NEW", "c-world": "n/a"}


def test_head_pack_contains_the_diff_chunks(repo, tmp_path):
    root, base, head = repo
    log = []
    _judge(repo, tmp_path, fake_vote(log=log))
    diff_id = f"diff:{base[:7]}..{head[:7]}:app.py"
    fix_line = C_FIX["claim"]
    at_head = [p for side, _m, p in log if side == "head" and fix_line in p]
    at_base = [p for side, _m, p in log if side == "base" and fix_line in p]
    assert at_head and at_base
    assert all(f"[[{diff_id}" in p for p in at_head)
    assert not any("[[diff:" in p for p in at_base)


def test_same_voters_judge_both_sides(repo, tmp_path):
    log = []
    _judge(repo, tmp_path, fake_vote(log=log))
    by_side = {side: sorted({m for s, m, _p in log if s == side}) for side in ("base", "head")}
    assert by_side["base"] == by_side["head"] == ["a/x", "b/y", "c/z"]


def test_base_equals_head_is_all_still_or_unclear(repo, tmp_path):
    root, base, _head = repo
    got = _judge(repo, tmp_path, fake_vote(), base=base, head=base)
    changes = set(_changes(got).values())
    assert "FIXED" not in changes and "GONE" not in changes and "NEW" not in changes
    assert changes <= {"STILL", "UNCLEAR", "n/a"}


def test_unclear_when_a_voter_is_unsure_on_one_side(repo, tmp_path):
    """Four voters, one UNSURE at head: the head verdict is still FALSE (3 committed), but not
    unanimous — so not FIXED. `verdict` alone would have said FIXED."""
    got = _judge(repo, tmp_path, fake_vote(models=("a/x", "b/y", "c/z", "d/w"), unsure={("head", "d/w")}))
    d = next(d for d in got["deltas"] if d.id == "c-fix")
    assert d.head["verdict"] == "FALSE" and d.head["unanimous"] is False
    assert d.change == "UNCLEAR"


def test_dead_voter_does_not_block_a_side(repo, tmp_path):
    """A model with no vote at all (404, out of its free plan) is not a voter; the other three decide."""
    got = _judge(repo, tmp_path, fake_vote(models=("a/x", "b/y", "c/z", "dead/q"), dead={"dead/q"}))
    assert _changes(got) == {"c-fix": "FIXED", "c-still": "STILL", "c-gone": "GONE",
                             "c-new": "NEW", "c-world": "n/a"}


def test_evidence_ids_are_read_per_side(repo, tmp_path):
    """`lib.py` is untouched: the same `src:` id at both sides is one entry per side, and
    nothing in the row says "unchanged evidence"."""
    got = _judge(repo, tmp_path, fake_vote())
    d = next(d for d in got["deltas"] if d.id == "c-still")
    assert d.evidence_base == d.evidence_head == ["src:lib.py:4-5"]
    row = next(r for r in json.loads((tmp_path / "out" / "delta.json").read_text())["deltas"]
               if r["id"] == "c-still")
    assert row["base"]["evidence"] == ["src:lib.py:4-5"] and row["head"]["evidence"] == ["src:lib.py:4-5"]
    assert not {"same", "unchanged", "evidence"} & set(row)
    fix = next(d for d in got["deltas"] if d.id == "c-fix")
    assert fix.evidence_base == fix.evidence_head == ["src:app.py:4-5"]   # one id, two texts
    assert fix.change == "FIXED"


def test_delta_json_shape(repo, tmp_path):
    root, base, head = repo
    _judge(repo, tmp_path, fake_vote())
    data = json.loads((tmp_path / "out" / "delta.json").read_text())
    assert set(data) == {"base", "head", "deltas"} and (data["base"], data["head"]) == (base, head)
    assert [r["id"] for r in data["deltas"]] == [c["id"] for c in CLAIMS]
    row = data["deltas"][0]
    assert {"id", "claim", "change", "base", "head"} <= set(row)
    assert set(row["base"]) == set(row["head"]) == {"verdict", "unanimous", "evidence"}
    for side, sha in (("base", base), ("head", head)):
        report = json.loads((tmp_path / "out" / f"votes_{side}.json").read_text())
        assert report["target"]["sha"] == sha and report["claims"] and report["results"]
    assert (tmp_path / "out" / "delta.txt").read_text().startswith(f"before/after {base[:7]}..{head[:7]}")


def test_delta_json_is_read_by_score_cc(repo, tmp_path):
    """score_cc matches a delta to its key row by `id` (the key's text is different on purpose)."""
    _judge(repo, tmp_path, fake_vote())
    key = {"set": "t", "claims": [{**c, "claim": f"key text {c['id']}"} for c in CLAIMS]}
    table = score_cc.score_deltas(json.loads((tmp_path / "out" / "delta.json").read_text()), key)
    assert table["delta"] == {"fix_right": 1.0, "still_as_fixed": 0}
    assert table["confusion"] == {"fixed": {"FIXED": 1}, "still": {"STILL": 1},
                                  "gone": {"GONE": 1}, "new": {"NEW": 1}}


def test_commit_only_at_head_is_not_judged_blind_at_base(repo, tmp_path):
    """A claim about a commit that exists at head alone is `world` at base by CC-1; it still gets
    a pack there (empty), so a TRUE voted blind does not stand: not STILL, NEW or UNCLEAR."""
    root, base, head = repo
    claim = {"id": "c-commit", "claim": f"Commit `{head[:7]}` makes the step strict."}
    with ExitStack() as stack:
        bt = stack.enter_context(Target.open(root, base, scratch=tmp_path / "s"))
        ht = stack.enter_context(Target.open(root, head, scratch=tmp_path / "s"))
        sides = cd.prepare_sides([claim], bt, ht, cd.PackBudget())
        assert sides["world"] == [False] and sides["items_b"][0][1] is not None

        def lazy(items, side):   # TRUE on everything, quoting the claim
            def complete(prompt):
                return json.dumps([{"id": int(n), "verdict": "TRUE", "chunk": "x", "quote": claim["claim"]}
                                   for n in re.findall(r"=== CLAIM (\d+) ===", prompt)])
            return [{"model": m, "run": 0, **ask_with_packs(items, 0, 1, complete)} for m in ("a/x", "b/y", "c/z")]
        got = cd.judge_both([claim], bt, ht, lazy)
    assert got["deltas"][0].change == "UNCLEAR"
    assert got["deltas"][0].base["verdict"] == "UNSURE"


def test_two_worktrees_and_cleanup(repo, tmp_path):
    root, base, head = repo
    status_before = _git(root, "status", "--porcelain")
    _judge(repo, tmp_path, fake_vote())
    assert _git(root, "rev-parse", "HEAD") == head and _git(root, "status", "--porcelain") == status_before
    assert len(_git(root, "worktree", "list").splitlines()) == 1        # both trees removed
    trees = tmp_path / "s" / "claimcheck"
    assert not any(p.name in (base, head) for p in trees.rglob("*") if p.is_dir())


# ------------------------------------------------------------------ the command, through the real ask_v2

@pytest.fixture
def http(monkeypatch):
    """Only the HTTP call is faked: claim_vote's ask_v2, pacer, parser and quote check run.
    The replies come in the shapes real models were seen to send (round 280's live runs):
    rows one after another with no list around them, and a quote with a bare newline."""
    calls = []
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url=f"http://{ref.split('/')[0]}.invalid/v1", api_key="k", model=ref,
        temperature=0.3, max_tokens=100))

    def completion(url, headers, payload, timeout, **_k):
        prompt = payload["messages"][-1]["content"]
        calls.append((payload["model"], prompt))
        if payload["model"] == "dead/q":
            raise RuntimeError("HTTP 404: no free plan")
        rows = read_rows(prompt)
        if payload["model"].startswith("b/"):     # one object a line, no list
            return "Here are my votes:\n" + "\n".join(json.dumps(r) for r in rows)
        if payload["model"].startswith("c/"):     # a raw newline in a string
            for r in rows:
                if r.get("quote"):
                    r["quote"] = "\n" + r["quote"]
            return "```json\n" + json.dumps(rows).replace("\\n", "\n") + "\n```"
        return json.dumps(rows)
    monkeypatch.setattr(cv, "request_completion", completion)
    return calls


def _cli(repo, tmp_path, claims, *extra, models=("a/x", "b/y", "c/z")):
    root, base, head = repo
    return cd.main([str(_write(tmp_path, claims)), "--target", str(root), "--base", base, "--head", head,
                    "--profiles", *models, "--runs", "1", "--interval", "0.001",
                    "--repo-root", str(tmp_path), "--scratch", str(tmp_path / "s"),
                    "--out", str(tmp_path / "out"), *extra])


def test_check_exit_codes(repo, tmp_path, http, capsys):
    assert _cli(repo, tmp_path, CLAIMS, "--check") == 0
    assert "check: 4 of 4 claims with an expect match" in capsys.readouterr().out
    wrong = [{**C_FIX, "expect": "still"}, C_STILL]
    assert _cli(repo, tmp_path, wrong, "--check") == 1
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("MISMATCH"))
    assert "c-fix" in line and "expect still, got FIXED" in line and "src:app.py:4-5" in line
    assert _cli(repo, tmp_path, wrong) == 0                       # without --check: a report, not a gate


def test_claims_without_expect_never_fail_check(repo, tmp_path, http):
    plain = [{k: v for k, v in c.items() if k != "expect"} for c in CLAIMS]
    assert _cli(repo, tmp_path, plain, "--check") == 0


def test_an_unknown_expect_is_a_refusal(repo, tmp_path, http, capsys):
    assert _cli(repo, tmp_path, [{**C_FIX, "expect": "fxed"}], "--check") == 2
    assert "fxed" in capsys.readouterr().err and http == []


def test_a_ref_that_does_not_open_is_a_refusal(repo, tmp_path, http, capsys):
    root, base, _head = repo
    rc = cd.main([str(_write(tmp_path, CLAIMS)), "--target", str(root), "--base", base, "--head", "no-such-ref",
                  "--profiles", "a/x", "--repo-root", str(tmp_path), "--scratch", str(tmp_path / "s")])
    assert rc == 2 and "--target refused" in capsys.readouterr().err and http == []
    assert len(_git(root, "worktree", "list").splitlines()) == 1   # the base tree it opened is gone


def test_live_shaped_replies_and_a_dead_voter_still_decide(repo, tmp_path, http):
    """Through the real ask_v2: one voter answers without a list, one with a bare newline in a
    quote, one is dead — the three live ones still decide every change."""
    assert _cli(repo, tmp_path, CLAIMS, "--check", models=("a/x", "b/y", "c/z", "dead/q")) == 0
    data = json.loads((tmp_path / "out" / "delta.json").read_text())
    assert {r["id"]: r["change"] for r in data["deltas"]} == {
        "c-fix": "FIXED", "c-still": "STILL", "c-gone": "GONE", "c-new": "NEW", "c-world": "n/a"}
    for side in ("base", "head"):
        results = json.loads((tmp_path / "out" / f"votes_{side}.json").read_text())["results"]
        by = {r["model"]: r for r in results}
        # every live shape was read, nothing of it lost to the parser or the quote check
        assert all(len(by[m]["votes"]) == len(CLAIMS) - (side == "base") for m in ("a/x", "b/y", "c/z"))
        assert all(not by[m]["rejected"] for m in ("a/x", "b/y", "c/z"))
        assert by["dead/q"]["votes"] == {} and "404" in by["dead/q"]["error"]


def test_progress_lines_on_stderr(repo, tmp_path, http, capsys):
    assert _cli(repo, tmp_path, CLAIMS) == 0
    err = capsys.readouterr().err
    requests = [ln for ln in err.splitlines() if re.match(r"\[progress\] \S+ run0 request \d+ ", ln)]
    assert len(requests) == len(http)                              # one line a request
    jobs = [ln for ln in err.splitlines() if re.match(r"\[progress\] (base|head) \S+ run0 done:", ln)]
    assert len(jobs) == 6                                          # 3 voters x 2 sides
    plans = dict(re.findall(r"\[plan\] (base|head): \d+ claims, (\d+) requests", err))
    assert int(plans["base"]) + int(plans["head"]) == len(http)    # the plan is the bill


def test_help_says_the_cost(capsys):
    with pytest.raises(SystemExit):
        cd.main(["--help"])
    out = capsys.readouterr().out
    assert "36 requests a side" in out and "[progress]" in out
