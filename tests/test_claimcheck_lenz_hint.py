"""CC-9 (291): a Lenz comparison hint after a claim check — the commands and the cost, never run."""

from __future__ import annotations

import json
import re
import shlex
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.claimcheck import judge
from tools.claimcheck import lenz_hint as lh
from tools.claimcheck.pack import PackBudget
from tools.claimcheck.target import Target

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_vote as cv  # noqa: E402
import claimcheck_truth as ct  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402

#: the one test that serves fake voters on 127.0.0.1 from this process (port 0, no xdist group);
#: the script under test runs in child processes, which a monkeypatch cannot reach anyway
LOCAL_SERVER = {"test_run_claim_check_prints_the_hint_and_makes_no_call"}


# ------------------------------------------------------------------ no network, for the whole module

@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    if request.node.name in LOCAL_SERVER:
        return

    def refuse(*_a, **_k):
        raise AssertionError("CC-9 must not open a socket")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ a real votes.json: CC-6's chain, noisy fake voters

_GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
            "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}
APP = "import subprocess\n\n\ndef run_step(cmd):\n    return subprocess.run(cmd, check=False)\n"

W_SPLIT = "pytest-xdist runs tests in parallel worker processes when it is given the -n option on the command line."
W_NOWORD = "Python's sorted() is stable: equal keys keep their input order in the output list."
W_DISSENT = "POSIX requires that rename() replaces an existing target file atomically on one filesystem."
W_UNSURE = "git status prints untracked files under their own heading unless a gitignore pattern matches them."
W_SETTLED = "The HTTP status code 404 means that the server did not find the requested resource at all."
C_CODE = "`run_step` in `app.py` calls subprocess.run with check=False."
W_SHORT = "Pytest runs tests in file order."
CLAIMS = [C_CODE, W_SPLIT, W_SETTLED, W_DISSENT, W_UNSURE, W_NOWORD]
#: each live voter's verdict per claim; None = this voter skips the claim in its reply
VOTERS = {
    "kenary/agnes-3-0-flash:free": ["TRUE", "TRUE", "TRUE", "TRUE", None, "TRUE"],
    "kenary/hy3:free": ["FALSE", "TRUE", "TRUE", "TRUE", "UNSURE", "FALSE"],
    "kenary/nemotron-3-ultra-550b-a55b:free": ["TRUE", "FALSE", "TRUE", "FALSE", "UNSURE", "TRUE"],
    "tokenharbor/deepseek-v4-flash:free": ["TRUE", "FALSE", "TRUE", "TRUE", None, "FALSE"],
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
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    return root, _git(root, "rev-parse", "HEAD")


_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)
_GUTTER = re.compile(r"^ *\d*\| ?")


def answer(prompt: str, answers: dict) -> str:
    """A fake voter's reply in the live shape: objects one after another, no list around them;
    a code verdict cites the first non-blank line of the pack's first chunk."""
    out = []
    for m in _BLOCK.finditer(prompt):
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        verdict = answers.get(claim)
        if verdict is None:
            continue
        row = {"id": n, "verdict": verdict}
        if judge.WORLD_BLOCK not in evidence and verdict != "UNSURE":
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


def make_votes(root, sha, scratch, claims=CLAIMS, voters=VOTERS) -> dict:
    """votes.json as `claim_vote.main_target` writes it, through a JSON round trip (string keys)."""
    rows = [{"claim": c, "truth": None} for c in claims]
    with Target.open(str(root), sha, scratch=scratch) as target:
        items, meta = cv.prepare_target(rows, target, PackBudget())
        results = []
        for ref, verdicts in voters.items():
            table = dict(zip(claims, verdicts))
            got = judge.ask_with_packs(items, 0, 1, lambda p, t=table: answer(p, t), batch=10, code_batch=2)
            results.append({"model": ref, "run": 0, "votes": got["votes"],
                            "accepted": got["accepted"], "rejected": got["rejected"]})
        results.append({"model": DEAD, "run": 0, "votes": {}, "accepted": {}, "rejected": [],
                        "error": "HTTP 402 out of free plan"})
        out = {"target": {"repo": str(root), "ref": sha, "sha": target.sha, "base": None, "warnings": []},
               "results": results, "claims": cv.tally(rows, results, meta=meta)}
    return json.loads(json.dumps(out, ensure_ascii=False))


@pytest.fixture
def votes(repo, tmp_path):
    root, sha = repo
    return make_votes(root, sha, tmp_path / "scratch")


def _votes_file(tmp_path, votes, name="votes.json"):
    p = tmp_path / name
    p.write_text(json.dumps(votes, ensure_ascii=False), encoding="utf-8")
    return p


def _row(verdict, unanimous=False, **kw):
    return {"claim": kw.pop("claim", f"A public fact number {verdict} {unanimous} " + "x" * 60),
            "verdict": verdict, "unanimous": unanimous, "kind": kw.pop("kind", "world"),
            "dangling": kw.pop("dangling", False), **kw}


def _run(tmp_path, vf, *extra):
    return ct.main([str(vf), "--out", str(tmp_path / "out" / "truth.csv"), "--scratch", str(tmp_path / "s2"),
                    "--repo-root", str(tmp_path), *map(str, extra)])


# ------------------------------------------------------------------ the chain itself

def test_the_votes_fixture_has_the_live_shapes(votes):
    """What the fake run really gave: the noisy panel makes each group the picker reads."""
    by = {c["claim"]: c for c in votes["claims"]}
    assert by[W_SPLIT]["verdict"] == "SPLIT" and by[W_SPLIT]["kind"] == "world"
    assert by[W_NOWORD]["verdict"] == "SPLIT" and by[W_NOWORD]["kind"] == "world"
    assert by[W_DISSENT]["verdict"] == "TRUE" and by[W_DISSENT]["unanimous"] is False
    assert by[W_UNSURE]["verdict"] == "UNSURE"
    assert by[W_SETTLED]["verdict"] == "TRUE" and by[W_SETTLED]["unanimous"] is True
    assert by[C_CODE]["kind"] == "code" and by[C_CODE]["unanimous"] is False
    assert all(r["votes"] for r in votes["results"] if r["model"] != DEAD)


# ------------------------------------------------------------------ the ticket's tests

def test_only_world_claims_the_voters_did_not_settle_are_picked(votes):
    picked = lh.pick_for_lenz(votes["claims"])
    assert [c["claim"] for c in picked] == [W_SPLIT, W_NOWORD, W_DISSENT, W_UNSURE]   # SPLIT, dissent, UNSURE
    rows = [_row("UNSURE", claim="u"), _row("SPLIT", kind="code", claim="c"), _row("SPLIT", kind="mixed", claim="m"),
            _row("SPLIT", dangling=True, claim="d"), _row("TRUE", True, claim="t"), _row("FALSE", True, claim="f"),
            _row("CODE-CHECK", kind="code", claim="cc"), _row("SPLIT", claim="s"), {"claim": "no kind", "verdict": "SPLIT"}]
    assert [c["claim"] for c in lh.pick_for_lenz(rows)] == ["s", "u"]


def test_a_plurality_with_a_dissenter_is_picked_a_unanimous_one_never():
    rows = [_row("TRUE", False, claim="t1"), _row("FALSE", False, claim="f1"), _row("TRUE", True, claim="t2"),
            _row("TRUE", None, claim="t3")]
    assert [c["claim"] for c in lh.pick_for_lenz(rows)] == ["t1", "f1", "t3"]


def test_limit_is_respected():
    rows = [_row("UNSURE", claim=f"u{i}") for i in range(15)] + [_row("SPLIT", claim=f"s{i}") for i in range(15)]
    picked = lh.pick_for_lenz(rows, limit=12)
    assert len(picked) == 12 and all(c["verdict"] == "SPLIT" for c in picked)
    assert lh.lenz_constants()["DEFAULT_MAX"] == lf.DEFAULT_MAX == 12
    assert len(lh.pick_for_lenz(rows)) == lf.DEFAULT_MAX
    assert lh.pick_for_lenz(rows, limit=0) == []


def test_the_credit_figures_come_from_the_script(tmp_path):
    assert lh.lenz_constants() == {"DEFAULT_MAX": lf.DEFAULT_MAX, "VERIFY_CREDITS": lf.VERIFY_CREDITS}
    fake = tmp_path / "f.py"
    fake.write_text("import os\nDEFAULT_MAX = 5\nVERIFY_CREDITS = 20\nOTHER = 1\n", encoding="utf-8")
    assert lh.lenz_constants(fake) == {"DEFAULT_MAX": 5, "VERIFY_CREDITS": 20}
    assert lh.lenz_constants(tmp_path / "missing.py") == lh.FALLBACK
    fake.write_text("DEFAULT_MAX = 'twelve'\nVERIFY_CREDITS = (\n", encoding="utf-8")
    assert lh.lenz_constants(fake) == lh.FALLBACK


def test_the_public_fact_words_come_from_the_script(tmp_path):
    hints = lh.script_hints()
    assert hints.pattern == lf._HINTS.pattern and hints.flags == lf._HINTS.flags
    assert lh.script_hints(tmp_path / "missing.py") is None
    send, short, no_word = lh.split_sendable([{"claim": W_SPLIT}, {"claim": W_NOWORD}, {"claim": W_SHORT}], hints)
    assert [c["claim"] for c in send] == [W_SPLIT] and no_word == [{"claim": W_NOWORD}]
    assert short == [{"claim": W_SHORT}]
    # an unreadable filter holds nothing back, and the block says the count may be high
    assert len(lh.split_sendable([{"claim": W_NOWORD}], None)[0]) == 1


def test_hint_states_the_cost_and_runs_nothing(votes):
    picked = lh.pick_for_lenz(votes["claims"])
    md = lh.lenz_hint_markdown(picked, "out/lenz-claims.md", claims=votes["claims"])
    assert "nothing below was run" in md
    assert "/assess = at most 3 credits" in md
    assert "Not counted, the script would not send them" in md and "claim 6." in md
    assert "100 a month + 400 extra" in md and "check the billing page" in md
    assert f"= {lf.VERIFY_CREDITS} more (add --verify to command 2)" in md
    cmds = [ln.strip() for ln in md.splitlines() if ln.strip().startswith("python3 ")]
    assert cmds == ["python3 scripts/lenz_claim_filter.py out/lenz-claims.md --dry-run",
                    "python3 scripts/lenz_claim_filter.py out/lenz-claims.md --json lenz.json --max 3",
                    "python3 scripts/claimcheck_truth.py <votes.json> --lenz lenz.json"]
    # the table names the claims by their pending entry, never by their text
    assert "| claim 2 | SPLIT | FALSE 2, TRUE 2 |" in md
    assert "| claim 4 | TRUE (with a dissenter) | TRUE 3, FALSE 1 |" in md
    assert all(c["claim"] not in md for c in votes["claims"])


def test_module_has_no_network_and_no_key_access():
    src = Path(lh.__file__).read_text(encoding="utf-8")
    for word in ("urllib", "socket", "requests", "LENZ_API_KEY", "subprocess", "import lenz_claim_filter"):
        assert word not in src, word
    # importing it pulls in no HTTP client either (lenz_claim_filter would)
    code = ("import sys; import tools.claimcheck.lenz_hint; "
            "print(sorted(m for m in ('urllib.request', 'http.client', 'lenz_claim_filter') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "[]", out.stderr


def test_lenz_column_is_read_from_the_json(votes, repo, tmp_path, monkeypatch, capsys):
    """The real `lenz_claim_filter.py` over the claims file (its HTTP calls faked: /extract gives back
    each section's claim, /assess a verdict), then `claimcheck_truth.py --lenz` on what it wrote."""
    vf = _votes_file(tmp_path, votes)
    assert _run(tmp_path, vf) == 0
    out = tmp_path / "out"
    plain = (out / "pending.md").read_text(encoding="utf-8")
    assert "| claim | ours | votes |\n" in plain and "lenz |" not in plain
    claims_file = out / "lenz-claims.md"
    sent = []

    def call(method, path, key, body=None, **_kw):
        if path == "/extract":
            # /extract's live shape (what extract_claims reads): the section's claim, no others
            return {"claim": body["text"].split("\n\n", 1)[1].strip(), "identified_claims": []}
        sent.extend(body["claims"])
        return {"claims": [{"claim": c, "verdict": "True" if "xdist" in c else "Mixed",
                            "confidence": "high" if "xdist" in c else "low"} for c in body["claims"]]}
    monkeypatch.setattr(lf, "call", call)
    monkeypatch.setattr(lf, "api_key", lambda: "k")
    lenz_json = tmp_path / "lenz.json"
    assert lf.main([str(claims_file), "--json", str(lenz_json), "--max", "3", "--cache", str(tmp_path / "c.json"),
                    "--repo-root", str(tmp_path)]) == 0
    assert sorted(sent) == sorted([W_SPLIT, W_DISSENT, W_UNSURE])       # exactly the picked claims, 3 credits
    capsys.readouterr()

    assert _run(tmp_path, vf, "--lenz", lenz_json) == 0
    md = (out / "pending.md").read_text(encoding="utf-8")
    assert "| claim | ours | votes | lenz |" in md
    assert "| claim 2 | SPLIT | FALSE 2, TRUE 2 | True/high |" in md
    assert "| claim 4 | TRUE (with a dissenter) | TRUE 3, FALSE 1 | Mixed/low |" in md
    assert "| claim 5 | UNSURE |" in md and md.count("Mixed/low") == 2
    assert "| claim 6 | SPLIT | FALSE 2, TRUE 2 | - |" in md         # not sent: no answer, no guess
    # a missing file: the table as it was, the command works
    assert _run(tmp_path, vf, "--lenz", tmp_path / "gone.json") == 0
    assert "warning: --lenz" in capsys.readouterr().err
    gone = (out / "pending.md").read_text(encoding="utf-8")
    table = [ln for ln in plain.split(lh.TITLE)[1].splitlines() if ln.startswith("| ")]
    assert [ln for ln in gone.split(lh.TITLE)[1].splitlines() if ln.startswith("| ")] == table and len(table) == 5   # header + 4 claims
    assert gone.split(lh.TITLE)[0] == plain.split(lh.TITLE)[0]     # the pending entries too


def test_no_hint_flag(votes, tmp_path, capsys):
    vf = _votes_file(tmp_path, votes)
    assert _run(tmp_path, vf, "--no-lenz-hint") == 0
    md = (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    assert lh.TITLE not in md and lh.TITLE not in capsys.readouterr().out
    assert not (tmp_path / "out" / "lenz-claims.md").exists()
    assert _run(tmp_path, vf) == 0
    assert lh.TITLE in (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    assert lh.TITLE in capsys.readouterr().out


def test_run_claim_check_passes_no_lenz_hint_to_the_truth_step_only(tmp_path):
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "run_claim_check.sh"), "r.md", "--no-lenz-hint",
                           "--target", ".", "--truth", "t.csv", "--dry-run"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    cmds = [shlex.split(ln) for ln in proc.stdout.splitlines() if ln.startswith("python3 ")]
    assert "--no-lenz-hint" not in cmds[1] and cmds[2][-1] == "--no-lenz-hint"
    assert list(tmp_path.iterdir()) == []
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "run_claim_check.sh"), "r.md", "--truth", "t.csv",
                           "--dry-run"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert "--no-lenz-hint" not in proc.stdout


# ------------------------------------------------------------------ the end to end: the script, a fake voter, no Lenz

class _Voters:
    """An OpenAI-style server on 127.0.0.1:0 answering as `VOTERS` by model name."""

    def __init__(self, answers: dict):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                table = answers.get(body.get("model"))
                if table is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "user")
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer(prompt, table)},
                                               "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"


def test_run_claim_check_prints_the_hint_and_makes_no_call(repo, tmp_path):
    """`run_claim_check.sh --truth` end to end with fake voters and LENZ_API_KEY unset: the extract
    step is served from the disk cache, the hint is in the output, and neither `lenz_claim_filter.py`
    nor any other python3 command but the pipeline's three was started."""
    root, _sha = repo
    box = tmp_path / "box"
    (box / "scripts").mkdir(parents=True)
    for name in ("run_claim_check.sh", "claim_extract.py", "claim_vote.py", "claimcheck_truth.py"):
        (box / "scripts" / name).symlink_to(ROOT / "scripts" / name)
    trip = tmp_path / "lenz-was-run"
    (box / "scripts" / "lenz_claim_filter.py").write_text(
        f"import pathlib\npathlib.Path({str(trip)!r}).write_text('run')\n", encoding="utf-8")
    log = tmp_path / "python3.log"
    (tmp_path / "bin").mkdir()
    shim = tmp_path / "bin" / "python3"
    shim.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
                    f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    sections = [f"## Finding {i}\n\n{text} " + "It was seen in a review of the code and of the docs. " * 2
                for i, text in enumerate(("About the code and sorting.", "About git and http."))]
    (box / "r.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    assert len(lf.split_sections((box / "r.md").read_text(encoding="utf-8"))) == 2
    home = tmp_path / "home"
    cache = {lf.Cache.key("extract", s.strip()): part
             for s, part in zip(sections, ([C_CODE, W_SPLIT, W_DISSENT], [W_UNSURE, W_SETTLED, W_NOWORD]))}
    (home / ".cache" / "lenz").mkdir(parents=True)
    (home / ".cache" / "lenz" / "claims.json").write_text(json.dumps(cache), encoding="utf-8")
    srv = _Voters({f"m{i}": dict(zip(CLAIMS, v)) for i, v in enumerate(VOTERS.values())})
    ini = "[claim_vote]\ninterval_sec = 0\nper_provider = 8\n"
    for i in range(len(VOTERS)):
        ini += f"\n[v{i}]\nbase_url = {srv.url}\napi_key = x\nmodel = m{i}\napi_format = openai\nmax_tokens = 4000\n"
    (box / "contest.local.ini").write_text(ini, encoding="utf-8")
    env = {**_GIT_ENV, "HOME": str(home), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "RUNS": "1",
           "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
           "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": "127.0.0.1"}
    try:
        proc = subprocess.run(["bash", "scripts/run_claim_check.sh", "r.md", "--target", str(root), "--ref", "HEAD",
                               "--truth", "truth.csv", "--profiles", "v0", "v1", "v2", "v3"],
                              cwd=box, env=env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
    finally:
        srv.srv.shutdown()
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert lh.TITLE in proc.stdout and "Worth comparing" in proc.stdout and "3 claims" in proc.stdout
    assert not trip.exists()
    ran = log.read_text(encoding="utf-8").splitlines()
    assert [ln.split()[0] for ln in ran] == ["scripts/claim_extract.py", "scripts/claim_vote.py",
                                             "scripts/claimcheck_truth.py"]
    assert not any("lenz_claim_filter" in ln for ln in ran)
    out = next(p for p in (box / "claim-check-out").iterdir() if re.fullmatch(r"\d{8}T\d{6}Z", p.name))
    md = (out / "pending.md").read_text(encoding="utf-8")
    assert lh.TITLE in md
    assert [ln for ln in (out / "lenz-claims.md").read_text(encoding="utf-8").splitlines() if ln.startswith("### ")] \
        == [f"### {W_SPLIT}", f"### {W_DISSENT}", f"### {W_UNSURE}"]
    third = next(ln.strip() for ln in md.splitlines() if "claimcheck_truth.py" in ln and "--lenz" in ln)
    assert "--append" in third and f"--lenz {out.relative_to(box)}/lenz.json" in third


# ------------------------------------------------------------------ the dry run lists what the block counts

def test_the_scripts_dry_run_lists_exactly_the_counted_claims(votes, tmp_path):
    vf = _votes_file(tmp_path, votes)
    assert _run(tmp_path, vf) == 0
    md = (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    counted = int(re.search(r"were unsure about\): (\d+) claims", md).group(1))
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "lenz_claim_filter.py"),
                           str(tmp_path / "out" / "lenz-claims.md"), "--dry-run"],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    listed = [ln[len("  - ### "):] for ln in proc.stdout.splitlines() if ln.startswith("  - ")]
    assert proc.stdout.startswith(f"{counted} sections would go to the free /extract")
    assert listed == [W_SPLIT[:96], W_DISSENT[:96], W_UNSURE[:96]] and counted == 3


def test_a_claim_too_short_for_a_section_is_named_not_counted(tmp_path):
    rows = [_row("SPLIT", claim=W_SPLIT), _row("SPLIT", claim=W_SHORT)]
    assert not lh.sendable(W_SHORT) and lh.sendable(W_SPLIT)
    md = lh.lenz_hint_markdown(lh.pick_for_lenz(rows), "c.md", claims=rows)
    assert ": 1 claim." in md and "--max 1" in md and "too short" in md and "claim 2 —" in md
    text = lh.claims_file_text(lh.pick_for_lenz(rows))
    assert W_SHORT not in text and lf.split_sections(text) == [text.strip()]
    only_short = lh.lenz_hint_markdown([rows[1]], "c.md", claims=rows)
    assert "too short" in only_short and "would send none" in only_short and "python3 " not in only_short


def test_nothing_the_script_would_send_prints_no_commands():
    rows = [_row("SPLIT", claim=W_NOWORD)]
    md = lh.lenz_hint_markdown(lh.pick_for_lenz(rows), "c.md", claims=rows)
    assert "would send none of them" in md and "claim 1." in md
    assert "python3 " not in md and "/assess" not in md
    assert lh.claims_file_text(lh.pick_for_lenz(rows)) == ""


def test_pending_still_names_each_unsettled_claim_once(votes, tmp_path):
    """CC-8's black box counts every unsettled claim's text in pending.md: exactly once, and no
    settled claim at all. The hint must not quote them again."""
    vf = _votes_file(tmp_path, votes)
    lenz_json = tmp_path / "l.json"
    lenz_json.write_text(json.dumps({"rows": [{"claim": W_SETTLED, "verdict": "True", "confidence": "high"}]}),
                         encoding="utf-8")
    assert _run(tmp_path, vf, "--lenz", lenz_json) == 0
    md = (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    for c in votes["claims"]:
        assert md.count(c["claim"]) == (0 if c["unanimous"] else 1), c["claim"]
    assert "| claim 3 | TRUE (unanimous) | TRUE 4 | True/high |" in md       # a settled match is shown by number
    assert "\n### claim " not in md.split(lh.TITLE, 1)[1]


def test_votes_file_is_not_changed(votes, tmp_path):
    vf = _votes_file(tmp_path, votes)
    before = vf.read_bytes()
    assert _run(tmp_path, vf) == 0
    assert vf.read_bytes() == before


# ------------------------------------------------------------------ edge cases

def test_no_claim_qualifies(tmp_path, capsys):
    rows = [_row("TRUE", True), _row("SPLIT", kind="code"), _row("UNSURE", kind="mixed")]
    md = lh.lenz_hint_markdown(lh.pick_for_lenz(rows), "c.md", claims=rows)
    assert "Nothing worth sending to Lenz" in md and "python3 " not in md and "/assess" not in md
    vf = _votes_file(tmp_path, {"claims": rows, "results": []})
    assert _run(tmp_path, vf, "--no-rebuild") == 0
    assert "Nothing worth sending to Lenz" in capsys.readouterr().out
    assert not (tmp_path / "out" / "lenz-claims.md").exists()


def test_every_world_claim_settled(repo, tmp_path):
    root, sha = repo
    agree = {v: ["TRUE", "TRUE", "TRUE", "FALSE", "TRUE", "TRUE"] for v in VOTERS}
    votes = make_votes(root, sha, tmp_path / "scratch", voters=agree)
    assert all(c["unanimous"] for c in votes["claims"] if c["kind"] == "world")
    assert lh.pick_for_lenz(votes["claims"]) == []
    vf = _votes_file(tmp_path, votes)
    assert _run(tmp_path, vf) == 0
    assert "Nothing worth sending to Lenz" in (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")


def test_empty_lenz_json_gives_an_empty_column_and_no_warning(votes, tmp_path, capsys):
    vf = _votes_file(tmp_path, votes)
    empty = tmp_path / "lenz.json"
    empty.write_text("[]\n", encoding="utf-8")
    assert lh.read_lenz(empty) == []
    assert _run(tmp_path, vf, "--lenz", empty) == 0
    md = (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    assert "| claim | ours | votes | lenz |" in md and "| claim 2 | SPLIT | FALSE 2, TRUE 2 | - |" in md
    assert "warning" not in capsys.readouterr().err


def test_a_lenz_row_for_a_claim_not_in_votes_is_ignored_and_counted(votes, tmp_path, capsys):
    vf = _votes_file(tmp_path, votes)
    lenz_json = tmp_path / "lenz.json"
    lenz_json.write_text(json.dumps({"source": "x", "rows": [
        {"claim": W_SPLIT, "verdict": "True", "confidence": "high"},
        {"claim": "The moon is made of cheese and orbits Mars every day.", "verdict": "False", "confidence": "high"},
    ]}), encoding="utf-8")
    assert _run(tmp_path, vf, "--lenz", lenz_json) == 0
    assert "1 of 2 rows of" in capsys.readouterr().err
    md = (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")
    assert "True/high" in md and "False/high" not in md


def test_a_bad_lenz_file_is_a_warning(votes, tmp_path, capsys):
    vf = _votes_file(tmp_path, votes)
    bad = tmp_path / "lenz.json"
    bad.write_text('{"rows": 3}', encoding="utf-8")
    assert _run(tmp_path, vf, "--lenz", bad) == 0
    assert "warning: --lenz" in capsys.readouterr().err
    assert "lenz |" not in (tmp_path / "out" / "pending.md").read_text(encoding="utf-8")


def test_match_lenz_rewording_and_ambiguity():
    claims = [{"claim": W_SPLIT}, {"claim": W_DISSENT}, {"claim": "Same text."}, {"claim": "Same text."}]
    rows = [{"claim": W_SPLIT.upper() + "  ", "verdict": "True", "confidence": "high"},
            {"claim": W_DISSENT.replace("requires that", "requires"), "verdict": "Mixed", "confidence": None},
            {"claim": "same text", "verdict": "True"},
            {"claim": "Something else entirely about cooking pasta in salted water.", "verdict": "True"}]
    got, unmatched = lh.match_lenz(claims, rows)
    assert got == {0: "True/high", 1: "Mixed"} and unmatched == 2


def test_compare_command_appends_and_replaces_lenz():
    cmd = shlex.split(ct.compare_command(["v.json", "--out", "t.csv", "--target", "/r x", "--lenz", "old.json",
                                          "--no-lenz-hint"], "new.json"))
    assert cmd[0] == "python3" and cmd[1].endswith("claimcheck_truth.py")
    assert cmd[2:] == ["v.json", "--out", "t.csv", "--target", "/r x", "--append", "--lenz", "new.json"]
    again = shlex.split(ct.compare_command(["v.json", "--append", "--lenz=a.json"], "b.json"))
    assert again[2:] == ["v.json", "--append", "--lenz", "b.json"]


def test_hint_is_deterministic(votes):
    picked = lh.pick_for_lenz(votes["claims"])
    a = lh.lenz_hint_markdown(picked, "c.md", claims=votes["claims"])
    assert a == lh.lenz_hint_markdown(lh.pick_for_lenz(votes["claims"]), "c.md", claims=votes["claims"])
    assert lh.claims_file_text(picked) == lh.claims_file_text(lh.pick_for_lenz(votes["claims"]))
