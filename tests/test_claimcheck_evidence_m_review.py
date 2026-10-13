"""tests/test_claimcheck_evidence_m_review.py — evidence and votes after review batch m (№68–78): a bare word resolves
to the project's own code only, a package named venv is indexed, a quote is no prefix of a number, a version or a
path, a 0-based reply is read as such, a removed diff line is no evidence at head, a dangling symbol keeps its own
key, a missing ticket gets a note, an empty claims file is refused, and the Lenz splitter cuts at "No." and "etc."
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import claim_vote as cv  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402
from tools.claimcheck import anchors as cc_anchors  # noqa: E402
from tools.claimcheck import judge as cc_judge  # noqa: E402
from tools.claimcheck import pack as cc_pack  # noqa: E402
from tools.claimcheck import truth as cc_truth  # noqa: E402
from tools.claimcheck.anchors import PathRepoView  # noqa: E402
from tools.claimcheck.model import Chunk, Pack  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


# ── №68: a bare word resolves to the project's own code, a private one to one definition ─────

@pytest.fixture
def repo(tmp_path):
    _write(tmp_path, "pkg/queue.py", "def _state(q):\n    return q.ready\n\n\ndef _check_presence(rows):\n"
                                     "    return bool(rows)\n\n\ndef _path(p):\n    return p\n")
    _write(tmp_path, "pkg/other.py", "def _path(p):\n    return str(p)\n")
    _write(tmp_path, "contest-bench/157/acceptance_157.py", "def _lock():\n    pass\n\n\n"
                                                             "def load_rows_bench(path):\n    pass\n")
    _write(tmp_path, "tests/test_cache.py", "def _cache():\n    return {}\n")
    return PathRepoView(tmp_path)


@pytest.mark.parametrize("claim", [
    "The _lock is released after the retry.",                          # defined only in a bench
    "The _state is set to ready when the queue drains.",               # a noun of the sentence
    "The _path must be absolute.",                                     # two definitions, and a noun
    "Every _cache entry expires after an hour.",                       # defined only in a test
    "load_rows_bench never closes the file it opens",                  # a bench's helper, public
])
def test_prose_stays_a_world_claim(repo, claim):
    resolved = cc_anchors.resolve_claim(claim, repo)
    assert cc_anchors.classify(claim, resolved) == "world", [(r.anchor.text, r.path) for r in resolved]


@pytest.mark.parametrize("claim, symbol", [
    ("_check_presence never looks at the rows it was given", "_check_presence"),
    ("_state never looks at the queue", "_state"),                     # no article: the function
])
def test_a_private_name_of_the_projects_own_code_is_code(repo, claim, symbol):
    resolved = cc_anchors.resolve_claim(claim, repo)
    assert [r.qualname for r in resolved if r.found] == [symbol]
    assert cc_anchors.classify(claim, resolved) == "code"


# ── №69: a package named venv ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("git", [True, False], ids=["git-checkout", "plain-tree"])
def test_a_package_named_venv_is_indexed_and_a_virtualenv_is_not(tmp_path, git):
    if git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, "tool/venv/builder.py", "def build_env():\n    pass\n")
    _write(tmp_path, "env/pyvenv.cfg", "home = /usr/bin\n")
    _write(tmp_path, "env/lib/python3.12/helper.py", "x = 1\n")
    _write(tmp_path, "old/bin/activate", "# virtualenv < 20\n")
    _write(tmp_path, "old/lib/python2.7/os.py", "x = 1\n")
    files = PathRepoView(tmp_path).files()
    assert "tool/venv/builder.py" in files
    assert not [f for f in files if f.startswith(("env/", "old/"))]


# ── №70: a quote is no prefix of a number, a version or a path ───────────────────────────────

SRC = Chunk(id="src:conf.py:1-4", kind="source", path="conf.py", start=1, end=4,
            text=" 1| RATE_LIMIT = 0.5\n 2| VERSION = '1.2.3'\n 3| path = 'src/a/b'\n 4| self.items = items.copy()",
            why="t")


@pytest.mark.parametrize("quote, found", [
    ("RATE_LIMIT = 0", False), ("RATE_LIMIT = 0.5", True), ("VERSION = '1.2", False), ("VERSION = '1.2.3'", True),
    ("path = 'src/a", False), ("path = 'src/a/b'", True), ("self.items = items", True), ("0.5 VERSION", True),
])
def test_a_quote_ends_where_its_token_ends(quote, found):
    assert bool(cc_pack.find_all_quote(Pack("c", "s", (SRC,), False), quote)) is found


# ── №71: a reply that numbers the claims from 0 ──────────────────────────────────────────────

def test_a_zero_based_reply_is_read_zero_based():
    order = [7, 8, 9]
    rows = '[{"id": 0, "verdict": "TRUE"}, {"id": 1, "verdict": "FALSE"}, {"id": 2, "verdict": "UNSURE"}]'
    got = cc_judge.parse_votes_v2(rows, order)
    assert {k: v.verdict for k, v in got.items()} == {7: "TRUE", 8: "FALSE", 9: "UNSURE"}
    one_based = rows.replace('"id": 2', '"id": 3').replace('"id": 1', '"id": 2').replace('"id": 0', '"id": 1')
    assert {k: v.verdict for k, v in cc_judge.parse_votes_v2(one_based, order).items()} == \
        {7: "TRUE", 8: "FALSE", 9: "UNSURE"}
    # 0 and the claim count both there: no way to tell, the 0 row is no vote (as before)
    both = '[{"id": 0, "verdict": "TRUE"}, {"id": 3, "verdict": "FALSE"}]'
    assert {k: v.verdict for k, v in cc_judge.parse_votes_v2(both, order).items()} == {9: "FALSE"}


# ── №72: a removed diff line is no evidence at head ──────────────────────────────────────────

HUNK = ("diff --git a/pkg/s.py b/pkg/s.py\n@@ -10,4 +10,4 @@ class Store:\n"
        " 10|      def compact(self):\n"
        "   | -        self.items = {k: v for k, v in self.items.items() if v}\n"
        " 11| +        self.items = {k: v for k, v in self.items.items() if v is not None}\n"
        " 12|          return self.items")


@pytest.mark.parametrize("quote, at_head", [
    ("self.items = {k: v for k, v in self.items.items() if v}", False),
    ("self.items = {k: v for k, v in self.items.items() if v is not None}", True),
    ("def compact(self): + self.items = {k: v", True),
])
def test_the_range_diff_of_a_head_pack_has_no_removed_lines(quote, at_head):
    diff = Chunk(id="diff:aaaaaaa..bbbbbbb:pkg/s.py:10-12", kind="git", path="pkg/s.py", start=10, end=12,
                 text=HUNK, why="range")
    assert bool(cc_pack.find_all_quote(Pack("c", "s", (diff,), False), quote)) is at_head


def test_a_commits_own_hunk_keeps_its_removed_lines():
    """A `git:` hunk is history: its removed line is what that commit took out, and a quote of it stands."""
    hist = Chunk(id="git:bbbbbbb:pkg/s.py:10-12", kind="git", path="pkg/s.py", start=10, end=12, text=HUNK, why="c")
    assert cc_pack.find_all_quote(Pack("c", "s", (hist,), False),
                                  "self.items = {k: v for k, v in self.items.items() if v}")


# ── №73: a symbol the file does not define keeps its own key ─────────────────────────────────

def test_a_dangling_symbol_is_keyed_path_and_name(tmp_path):
    _write(tmp_path, "pkg/rows.py", "def read_rows():\n    return []\n")
    view = PathRepoView(tmp_path)

    def key(claim):
        return cc_truth.finding_key(claim, cc_anchors.resolve_claim(claim, view))

    assert key("`missing_fn` in `pkg/rows.py` drops the last row") == "pkg/rows.py::missing_fn"
    assert key("`read_rows` in `pkg/rows.py` returns a list") == "pkg/rows.py::read_rows"
    assert key("`pkg/rows.py` has no tests") == "pkg/rows.py"
    # an API of the world is no missing symbol of the file: the key stays the file's
    assert key("`pkg/rows.py` calls `os.path.join` with a None") == "pkg/rows.py"
    assert key("`pkg/rows.py` uses `json.loads` on every row") == "pkg/rows.py"


# ── №74: a ticket of ours with no file ───────────────────────────────────────────────────────

def test_a_missing_ticket_gets_a_note_a_vote_can_quote(tmp_path):
    _write(tmp_path, "epic-tasks/10-cc-10-vote.md", "# CC-10\n\n**Status:** open\n")
    view = PathRepoView(tmp_path)
    claim = "CC-77 makes the vote mandatory"
    pack = cc_pack.build_pack(claim, cc_anchors.resolve_claim(claim, view), view)
    assert [c.id for c in pack.chunks] == ["note:dangling:CC-77"]
    assert cc_pack.find_all_quote(pack, "no ticket CC-77 in the repository") == ["note:dangling:CC-77"]


# ── №75: nothing to vote on ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content, says", [
    ([], "no claims in it"), ({"claims": []}, "no claims in it"),
    ([{"claim": ""}], "claim 1 is empty"), (["a real claim", "   "], "claim 2 is empty"),
])
def test_an_empty_claims_file_or_a_blank_claim_is_refused(tmp_path, capsys, content, says):
    f = tmp_path / "claims.json"
    f.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cv.main([str(f), "--models", "a/x-1,b/y-1,c/z-1"])
    assert exc.value.code == 2 and says in capsys.readouterr().err


def test_run_claim_check_ends_cleanly_on_a_report_with_no_claim(tmp_path):
    """claim_vote.py refuses an empty claims file now; the wrapper does not call it then, and says why."""
    import shutil
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "run_claim_check.sh", tmp_path / "scripts" / "run_claim_check.sh")
    (tmp_path / "scripts" / "claim_extract.py").write_text(
        "import json, sys\njson.dump([], open(sys.argv[sys.argv.index('--out') + 1], 'w'))\n", encoding="utf-8")
    (tmp_path / "scripts" / "claim_vote.py").write_text(
        "open('VOTED', 'w').close()\nraise SystemExit(2)\n", encoding="utf-8")
    (tmp_path / "r.md").write_text("report\n", encoding="utf-8")
    p = subprocess.run(["bash", "scripts/run_claim_check.sh", "r.md"], cwd=tmp_path, capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0 and "no claims in r.md" in p.stdout and not (tmp_path / "VOTED").exists(), p.stdout


# ── №76: the recorded live votes carry no local path ─────────────────────────────────────────

def test_the_live_votes_of_290_name_no_local_path():
    text = (ROOT / "contest-bench" / "290" / "votes_live.json").read_text(encoding="utf-8")
    assert not json.loads(text)["target"]["repo"].startswith("/") and "/tmp/" not in text


# ── №77, №78: the Lenz splitter ──────────────────────────────────────────────────────────────

def test_a_longer_fence_is_not_closed_by_a_shorter_one():
    text = "## A\n\n````md\n```bash\n## not a heading\n```\n## nor this\n````\n\n## B\ntext\n"
    assert [p.splitlines()[0] for p in lf._heading_parts(text)] == ["## A", "## B"]


@pytest.mark.parametrize("line, parts", [
    ("Is the cache thread-safe? No. The cache leaks memory on every reload.", 3),
    ("It supports JSON, YAML, etc. The loader then fails on comments.", 2),
    ("e.g. Python is widely used here. Then it fails.", 2),
    ("See item no. 5 for the details of the retry wrapper.", 1),
])
def test_no_and_etc_end_a_sentence(line, parts):
    assert len(lf._sentences(line)) == parts
