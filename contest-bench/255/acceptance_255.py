"""Black-box bench for ticket 255 (CC-1, anchors), from the ticket alone.

Through the public names only — `extract_anchors`, `resolve_anchors`, `classify`,
`PathRepoView` and the `model` dataclasses:

* the acceptance numbers: `classify` against the `kind` of the 80 fixture claims (base
  sha of the CC-0 fixture repository) ≥ 95 %, every dangling claim `code`, no `world`
  claim `code`; the 30 real claims against this repository at `real_sha` ≥ 90 %;
* the extraction table of the ticket (each kind, rename forms, cues, non-symbols);
* resolution (decorated/nested spans, ambiguity, skipped files, commit prefixes, tickets);
* the edge cases (git output format, unicode, empty, a 10 KB claim under a second).

A fixture claim's `kind` maps to the expected class: `code`, `dangling`, `ticket`,
`commit` → `code`; `world` → `world`; `mixed` → `mixed`.

Run from a checkout root:  python3 -m pytest contest-bench/255/acceptance_255.py -n 0 -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CC = ROOT / "contest-bench" / "cc"

from tools.claimcheck.anchors import (  # noqa: E402
    PathRepoView, classify, extract_anchors, resolve_anchors)
from tools.claimcheck.model import Anchor, Chunk, Pack, ResolvedAnchor  # noqa: E402

EXPECT = {"code": "code", "dangling": "code", "ticket": "code", "commit": "code",
          "world": "world", "mixed": "mixed"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _kinds(claim):
    return [(a.kind, a.text) for a in extract_anchors(claim)]


def _classify(claim, view):
    return classify(claim, resolve_anchors(extract_anchors(claim), view))


# ── the acceptance numbers ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fixture_view(tmp_path_factory):
    mf = _load("cc_make_fixture_255", CC / "make_fixture.py")
    fx = mf.build_fixture(tmp_path_factory.mktemp("fx") / "repo")
    _git(fx.root, "checkout", "-q", fx.base_sha)
    return PathRepoView(fx.root)


@pytest.fixture(scope="module")
def real_view(tmp_path_factory):
    sha = json.loads((CC / "claims_real.json").read_text())["real_sha"]
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT).returncode:
        pytest.skip(f"{sha[:12]} not in this repository")
    dest = tmp_path_factory.mktemp("real") / "repo"
    # a shared clone: nothing is registered in this checkout (no worktree, no ref)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(dest)], check=True)
    _git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", sha)
    return PathRepoView(dest)


def _agreement(claims, view):
    misses = {}
    for c in claims:
        got = _classify(c["claim"], view)
        if got != EXPECT[c["kind"]]:
            misses[c["id"]] = (c["kind"], got)
    return misses


def test_fixture_claims_95_percent(fixture_view):
    claims = json.loads((CC / "claims_fixture.json").read_text())["claims"]
    misses = _agreement(claims, fixture_view)
    assert len(claims) - len(misses) >= 0.95 * len(claims), misses


def test_fixture_every_dangling_claim_is_code(fixture_view):
    claims = json.loads((CC / "claims_fixture.json").read_text())["claims"]
    bad = {c["id"]: _classify(c["claim"], fixture_view) for c in claims if c["kind"] == "dangling"}
    assert {k: v for k, v in bad.items() if v != "code"} == {}


def test_fixture_no_world_claim_is_code(fixture_view):
    claims = json.loads((CC / "claims_fixture.json").read_text())["claims"]
    bad = {c["id"]: _classify(c["claim"], fixture_view) for c in claims if c["kind"] == "world"}
    assert {k: v for k, v in bad.items() if v == "code"} == {}


def test_real_claims_90_percent(real_view):
    claims = json.loads((CC / "claims_real.json").read_text())["claims"]
    misses = _agreement(claims, real_view)
    assert len(claims) - len(misses) >= 0.90 * len(claims), misses


# ── extraction ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("claim,want", [
    ("see `tools/contest/gates.py` for it", ("path", "tools/contest/gates.py")),
    ("gates.py decides", ("path", "gates.py")),
    ("look at gates.py:12-30 closely", ("path", "gates.py:12-30")),
    ("`Policy._mechanical` refuses", ("symbol", "Policy._mechanical")),
    ("_pytest() builds the line", ("symbol", "_pytest")),
    ("`gates.run_tests_detail` runs", ("symbol", "gates.run_tests_detail")),
    ("`tools.contest.policy.Policy.decide` asks", ("symbol", "tools.contest.policy.Policy.decide")),
    ("commit a73e389 fixed it", ("commit", "a73e389")),
    ("landed @afa53f1 yesterday", ("commit", "afa53f1")),
    ("the sha `7b4e5f9` is old", ("commit", "7b4e5f9")),
    ("KC-5 says so", ("ticket", "KC-5")),
    ("AR-25 too", ("ticket", "AR-25")),
    ("and CC-3", ("ticket", "CC-3")),
])
def test_extract_table(claim, want):
    kinds = _kinds(claim)
    assert any(k == want[0] and want[1] in t for k, t in kinds), kinds


def test_ticket_number_form():
    assert any(k == "ticket" and "123" in t for k, t in _kinds("ticket 123 is open"))


def test_test_id_is_one_anchor():
    kinds = _kinds("`tests/test_x.py::test_y` fails")
    assert [k for k, _ in kinds] == ["test"], kinds


@pytest.mark.parametrize("claim", ["git prints `dir/{a => b}.py`", "it shows old => new"])
def test_rename_form_is_not_a_path(claim):
    assert not [t for k, t in _kinds(claim) if k == "path"]


@pytest.mark.parametrize("word", ["--timeout", "True", "None", "pytest"])
def test_backticked_non_symbols(word):
    assert [k for k, _ in _kinds(f"pass `{word}` here") if k == "symbol"] == []


def test_shell_command_in_backticks_is_not_a_symbol():
    assert [k for k, _ in _kinds("run `git rev-list` first") if k == "symbol"] == []


def test_hex_in_prose_is_not_a_commit():
    assert [k for k, _ in _kinds("the value deadbeef42 is magic") if k == "commit"] == []


def test_spans_index_the_original_and_text_is_stripped():
    claim = "**Policy** uses `gates.run_tests_detail`."
    for a in extract_anchors(claim):
        assert isinstance(a, Anchor)
        assert a.text in claim[a.start:a.end]
        assert "`" not in a.text and "*" not in a.text and not a.text.endswith(".")


def test_anchors_do_not_overlap():
    spans = sorted((a.start, a.end) for a in extract_anchors(
        "`tools/contest/gates.py::_declared_files` in gates.py:3 and commit a73e389 (KC-5)"))
    assert all(e <= s2 for (_, e), (s2, _) in zip(spans, spans[1:])), spans


def test_extract_deterministic():
    claim = "`Policy.decide` at commit a73e389 in gates.py:4 (KC-3), branch main"
    assert extract_anchors(claim) == extract_anchors(claim)


# ── resolution ────────────────────────────────────────────────────────────

@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(
        "import functools\n\n\n"
        "@functools.lru_cache()\n"
        "def run():\n    return 1\n\n\n"
        "class Box:\n    @staticmethod\n    def open():\n        return 2\n\n\n"
        "def outer():\n    def inner():\n        return 3\n    return inner\n")
    (tmp_path / "pkg" / "b.py").write_text("def run():\n    return 4\n")
    (tmp_path / "pkg" / "c.py").write_text("def run():\n    pass\n")
    (tmp_path / "pkg" / "broken.py").write_text("def nope(:\n")
    (tmp_path / "pkg" / "huge.py").write_text("x = 1\n" * 90000 + "def huge_fn():\n    pass\n")
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks" / "07-something.md").write_text("# KC-7\n")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True, env=env)
    return tmp_path


def _one(view, claim, kind):
    res = [r for r in resolve_anchors(extract_anchors(claim), view) if r.anchor.kind == kind]
    assert res, (claim, _kinds(claim))
    assert isinstance(res[0], ResolvedAnchor)
    return res[0]


def test_decorated_span_and_method(repo):
    view = PathRepoView(repo)
    r = _one(view, "`pkg.a.run` caches", "symbol")
    assert r.found and r.path == "pkg/a.py" and r.lines == (4, 6)
    m = _one(view, "`Box.open` opens", "symbol")
    assert m.found and m.path == "pkg/a.py" and m.lines[0] == 10


def test_nested_function_by_dotted_name(repo):
    r = _one(PathRepoView(repo), "`outer.inner` returns 3", "symbol")
    assert r.found and r.path == "pkg/a.py" and r.lines[0] == 16


def test_ambiguous_bare_name(repo):
    r = _one(PathRepoView(repo), "`run()` returns", "symbol")
    assert r.found and r.path == "pkg/a.py"
    assert len(r.candidates) == 2


def test_unparsable_and_huge_files_do_not_raise(repo):
    view = PathRepoView(repo)
    assert not _one(view, "`huge_fn` is defined", "symbol").found
    assert not _one(view, "`nope()` is defined", "symbol").found


def test_path_and_bare_name(repo):
    view = PathRepoView(repo)
    assert _one(view, "`pkg/b.py` holds it", "path").found
    assert _one(view, "b.py holds it", "path").found
    assert not _one(view, "`pkg/zz.py` holds it", "path").found


def test_commit_resolves_and_unknown_does_not(repo):
    view = PathRepoView(repo)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    r = _one(view, f"commit {sha[:9]} added it", "commit")
    assert r.found and r.sha == sha
    assert not _one(view, "commit 1234abc added it", "commit").found


def test_ticket_file(repo):
    assert _one(PathRepoView(repo), "KC-7 asks", "ticket").found
    assert not _one(PathRepoView(repo), "KC-8 asks", "ticket").found


def test_view_files_sorted_without_git(repo):
    files = PathRepoView(repo).files()
    assert files == sorted(files) and not any(f.startswith(".git/") for f in files)


# ── classification ────────────────────────────────────────────────────────

def test_dangling_symbol_stays_code(repo):
    claim = "`pkg.b.vanished_fn()` deletes the cache"
    res = resolve_anchors(extract_anchors(claim), PathRepoView(repo))
    assert not any(r.found for r in res)
    assert classify(claim, res) == "code"


def test_ref_alone_is_world(repo):
    assert _classify("the branch `kc` is ahead of main", PathRepoView(repo)) == "world"


def test_world_and_mixed(repo):
    view = PathRepoView(repo)
    assert _classify("pytest exits with code 4 on an unknown option", view) == "world"
    # naming what the code calls is a code claim; a clause about how that world thing behaves makes it mixed
    assert _classify("`pkg.b.run` exits with code 4 when the plugin is missing", view) == "code"
    assert _classify("`pkg.b.run` calls `subprocess.run`, which does not raise on a non-zero exit", view) == "mixed"
    assert _classify("`pkg.b.run` exits with code 4, because pytest exits so on an unknown option", view) == "mixed"


# ── edge cases and the model ──────────────────────────────────────────────

@pytest.mark.parametrize("claim", ["", "   ", "ünïcødé `Box.open` — ok", "```\nx = 1\n```",
                                   "git diff --numstat prints `a/{x => y}.py`"])
def test_edge_claims_do_not_raise(repo, claim):
    assert _classify(claim, PathRepoView(repo)) in ("code", "world", "mixed")


def test_long_claim_under_a_second(repo):
    claim = ("`Box.open` in pkg/a.py at commit a73e389 calls run() and KC-7; " * 160)[:10240]
    t = time.monotonic()
    _classify(claim, PathRepoView(repo))
    assert time.monotonic() - t < 1.0


def test_pack_minimal_bodies():
    ch = Chunk(id="src:a.py:1-2", kind="source", path="a.py", start=1, end=2, text="def f():\n    pass", why="f")
    pack = Pack(claim="c", sha="0" * 40, chunks=(ch,), truncated=False)
    assert "def f()" in pack.render()
    assert pack.find("pass") == "src:a.py:1-2" and pack.find("absent") is None
