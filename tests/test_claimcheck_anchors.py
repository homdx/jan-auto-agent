"""CC-1: claim anchors — extraction, resolution against a repository, code/world/mixed (offline)."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402
from tools.claimcheck.anchors import (  # noqa: E402
    PathRepoView, classify, extract_anchors, is_dangling, resolve_anchors)
from tools.claimcheck.model import Anchor, Chunk, Pack, RepoView, ResolvedAnchor  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "claimcheck" / "anchors_golden.json"

FILES = {
    "tools/contest/gates.py": (
        "def _pytest(args):\n    return args\n\n\n"
        "def run_tests_detail(root):\n    return _pytest([root])\n\n\n"
        "def judge_worktree(root):\n    return root\n\n\n"
        "def _declared_paths(text):\n    return [text]\n"),
    "tools/contest/policy.py": (
        "import functools\n\n\n"
        "class Policy:\n"
        "    def _mechanical(self, cmd):\n        return cmd\n\n"
        "    @staticmethod\n    @functools.lru_cache\n"
        "    def decide(cmd):\n"
        "        def inner(x):\n            return x\n"
        "        return inner(cmd)\n"),
    "tools/contest/workspace.py": "def _dirty_outside_runs(root):\n    return []\n\n\ndef _commits_above(a, b):\n    return 0\n",
    "tools/contest/runner.py": "def _dirty_tree(root):\n    return []\n\n\ndef run():\n    return 1\n",
    "tools/other/runner.py": "def run():\n    return 2\n",
    "tools/third/runner.py": "def run():\n    return 3\n",
    "tests/test_contest_draft.py": (
        "def test_the_prompt_carries_the_source():\n    pass\n\n\n"
        "class TestPolicy:\n    def test_decide(self):\n        pass\n"),
    "contest.ini": "[contest]\n",
    "requirements.txt": "pytest\n",
    "docs/notes.md": "notes\n",
    "epic-tasks/34-a-ticket.md": "a\n",
    "epic-tasks/01-b-ticket.md": "b\n",
    "epic-tasks/123-kc76-the-contest-runs-on-a-target.md": "c\n",
}


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("cc1") / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture(scope="module")
def view(repo):
    return PathRepoView(repo)


def _kinds(claim):
    return [(a.kind, a.text) for a in extract_anchors(claim)]


def _resolve(claim, view):
    return resolve_anchors(extract_anchors(claim), view)


def test_the_view_satisfies_the_protocol(view):
    assert isinstance(view, RepoView)


def test_paths_backticked_bare_and_with_lines():
    assert _kinds("`tools/contest/gates.py` and gates.py and gates.py:12-30, also docs/x.md:7.") == [
        ("path", "tools/contest/gates.py"), ("path", "gates.py"), ("path", "gates.py:12-30"),
        ("path", "docs/x.md:7")]
    for ext in "py md ini json csv sh toml yaml yml txt".split():
        assert _kinds(f"reads cfg/a.{ext} here") == [("path", f"cfg/a.{ext}")]
    assert _kinds("a file named notes with no suffix, and x.pyc") == []


def test_every_anchor_text_is_its_span():
    claim = "`Policy.decide` in **tools/contest/policy.py:3**, commit a73e389 (KC-5)."
    anchors = extract_anchors(claim)
    assert [a.text for a in anchors] == ["Policy.decide", "tools/contest/policy.py:3", "a73e389", "KC-5"]
    for a in anchors:
        assert claim[a.start:a.end] == a.text
    assert anchors == sorted(anchors, key=lambda a: a.start)


def test_rename_form_is_not_a_path():
    assert _kinds("git prints dir/{a => b}.py for it") == []
    assert _kinds("the summary says old => new") == []
    assert _kinds("`a.py => b.py` is how git shows a rename") == []
    assert _kinds("see https://example.com/docs/readme.md and www.example.com/a.py") == []


def test_symbol_forms():
    assert _kinds("`Policy._mechanical`, `_pytest()`, gates.run_tests_detail() and "
                  "`tools.contest.policy.Policy.decide`") == [
        ("symbol", "Policy._mechanical"), ("symbol", "_pytest"), ("symbol", "gates.run_tests_detail"),
        ("symbol", "tools.contest.policy.Policy.decide")]
    assert _kinds("`retry_call`, `TransientError` and `Policy` are names") == [
        ("symbol", "retry_call"), ("symbol", "TransientError"), ("symbol", "Policy")]
    # call arguments are not part of the name
    assert _kinds('`Policy.decide("cat $HOME/.ssh/id_rsa")` is approved') == [("symbol", "Policy.decide")]
    # not backticked and not a call: prose
    assert _kinds("retry_call and gates.run_tests_detail are mentioned") == []


def test_backticked_words_that_are_not_symbols():
    assert _kinds("`--timeout` `True` `pytest` `None` `timeout` `workers` `print()` `os`") == []
    assert _kinds("`git rev-list` `git status` `check=True` `old => new` `[defaults]` `except Exception`") == []
    assert _kinds("the `configparser` and `subprocess` modules") == []


def test_commit_anchor_needs_a_cue_and_resolution(view, repo):
    sha = _git(repo, "rev-parse", "HEAD")
    assert _kinds("deadbeef is a word in prose, so is decade1 and 1234567") == []
    claim = f"commit a73e389 and @afa53f1 and `7b4e5f9` and at {sha[:8]} and sha: 0123abc"
    assert _kinds(claim) == [("commit", "a73e389"), ("commit", "afa53f1"), ("commit", "7b4e5f9"),
                             ("commit", sha[:8]), ("commit", "0123abc")]
    resolved = _resolve(claim, view)
    assert [r.found for r in resolved] == [False, False, False, True, False]
    assert resolved[3].sha == sha and resolved[0].sha == ""
    assert classify(claim, resolved) == "code"     # the one commit that exists makes it ours
    assert classify("commit a73e389 fixed it", _resolve("commit a73e389 fixed it", view)) == "world"


def test_commit_prefix_ambiguity_is_not_found(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    _git(root, "init", "-q")
    stream = []
    for i in range(1, 3001):
        stream.append(f"commit refs/heads/main\ncommitter t <t@t> {1000 + i} +0000\n"
                      f"data 3\nc{i % 10}\nM 644 inline f\ndata {len(str(i))}\n{i}\n")
    subprocess.run(["git", "fast-import", "--quiet"], cwd=root, input="".join(stream).encode(), check=True)
    shas = _git(root, "rev-list", "main").split()
    seen, clash = {}, None
    for s in shas:
        if s[:4] in seen:
            clash = s[:4]
            break
        seen[s[:4]] = s
    assert clash, "3000 commits should hold two with one 4-character prefix"
    assert PathRepoView(root).rev_parse(clash) is None
    assert PathRepoView(root).rev_parse(shas[0][:12]) == shas[0]


def test_ticket_ids_and_numbers():
    assert _kinds("KC-5, AR-25, ticket 123, CC-3, FL-2, SLOW-1, AUTO-9 and GATE1-LEARN-2") == [
        ("ticket", "KC-5"), ("ticket", "AR-25"), ("ticket", "ticket 123"), ("ticket", "CC-3"),
        ("ticket", "FL-2"), ("ticket", "SLOW-1"), ("ticket", "AUTO-9"), ("ticket", "GATE1-LEARN-2")]
    assert _kinds("kc-5, XX-5, ticket 12345 and the ticket system") == []


def test_ticket_resolution_leading_zeros_and_prefixed_ids(view):
    got = _resolve("ticket 1, ticket 34, KC-76, KC-9999 and ticket 8", view)
    assert [(r.found, r.path) for r in got] == [
        (True, "epic-tasks/01-b-ticket.md"), (True, "epic-tasks/34-a-ticket.md"),
        (True, "epic-tasks/123-kc76-the-contest-runs-on-a-target.md"),
        (False, "epic-tasks"), (False, "epic-tasks")]


def test_test_ids_are_not_split():
    claim = ("tests/test_contest_draft.py::test_the_prompt_carries_the_source fails, as does "
             "test_gates::test_pytest_flag and tests/test_contest_draft.py::TestPolicy::test_decide")
    assert _kinds(claim) == [
        ("test", "tests/test_contest_draft.py::test_the_prompt_carries_the_source"),
        ("test", "test_gates::test_pytest_flag"),
        ("test", "tests/test_contest_draft.py::TestPolicy::test_decide")]
    # a `path::name` whose name is no test is a path-qualified symbol, still one anchor
    assert _kinds("`tools/contest/gates.py::_pytest`") == [("symbol", "tools/contest/gates.py::_pytest")]


def test_resolve_test_ids(view):
    got = _resolve("tests/test_contest_draft.py::TestPolicy::test_decide and "
                   "test_contest_draft::test_the_prompt_carries_the_source and "
                   "tests/test_contest_draft.py::test_gone", view)
    assert [(r.found, r.qualname) for r in got] == [
        (True, "TestPolicy.test_decide"), (True, "test_the_prompt_carries_the_source"), (False, "")]
    assert got[2].path == "tests/test_contest_draft.py"    # the file is there, the test is not


def test_resolve_symbol_in_class_and_nested_and_decorated(view):
    got = _resolve("`Policy._mechanical` `Policy.decide` `Policy.decide.inner` `gates.run_tests_detail` "
                   "`tools.contest.policy.Policy.decide` `tools/contest/gates.py::_pytest`", view)
    assert [(r.found, r.path, r.qualname, r.lines) for r in got] == [
        (True, "tools/contest/policy.py", "Policy._mechanical", (5, 6)),
        (True, "tools/contest/policy.py", "Policy.decide", (8, 13)),     # decorators are in the span
        (True, "tools/contest/policy.py", "Policy.decide.inner", (11, 12)),
        (True, "tools/contest/gates.py", "run_tests_detail", (5, 6)),
        (True, "tools/contest/policy.py", "Policy.decide", (8, 13)),
        (True, "tools/contest/gates.py", "_pytest", (1, 2))]
    bare = _resolve("`_mechanical` and `_dirty_outside_runs`", view)
    assert [(r.found, r.qualname) for r in bare] == [(True, "Policy._mechanical"), (True, "_dirty_outside_runs")]


def test_path_resolution_bare_name_unique_and_ambiguous(view):
    got = _resolve("`contest.ini` `policy.py` `runner.py` `contest/gates.py` `docs/gone.md` `gone.py` "
                   "`nodir/gone.md`", view)
    assert [(r.found, r.path) for r in got] == [
        (True, "contest.ini"), (True, "tools/contest/policy.py"), (False, "tools/contest/runner.py"),
        (True, "tools/contest/gates.py"), (False, "docs"), (False, "."), (False, "")]
    assert got[2].candidates == ("tools/contest/runner.py", "tools/other/runner.py", "tools/third/runner.py")
    assert _resolve("gates.py:12-30", view)[0].lines == (12, 30)
    assert _resolve("gates.py:7", view)[0].lines == (7, 7)


def test_ambiguous_symbol_lists_candidates(view):
    one = _resolve("`run()`", view)[0]
    assert (one.found, one.path, one.qualname) == (True, "tools/contest/runner.py", "run")
    assert [c[0] for c in one.candidates] == ["tools/other/runner.py", "tools/third/runner.py"]
    assert len(one.candidates) == 2
    assert _resolve("`_pytest()`", view)[0].candidates == ()


def test_resolve_skips_unparsable_and_huge_files(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n")
    (tmp_path / "big.py").write_text("def huge_one():\n    return 1\n" + "# pad\n" * 90_000)
    (tmp_path / "good.py").write_text("def fine_one():\n    return 1\n")
    assert (tmp_path / "big.py").stat().st_size > 500 * 1024
    got = _resolve("`fine_one()` `huge_one()` `broken()` `bad.py` `big.py`", PathRepoView(tmp_path))
    assert [r.found for r in got] == [True, False, False, True, True]


def test_the_index_is_built_once_per_view(tmp_path, monkeypatch):
    import tools.claimcheck.anchors as anchors
    (tmp_path / "m.py").write_text("def one():\n    pass\n\n\ndef two():\n    pass\n")
    built = []
    real = anchors._Index.__init__
    monkeypatch.setattr(anchors._Index, "__init__", lambda self, v: (built.append(1), real(self, v))[1])
    v = PathRepoView(tmp_path)
    for claim in ("`one()`", "`two()`", "`m.one`"):
        resolve_anchors(extract_anchors(claim), v)
    assert built == [1]


# 40 claims in the style of kc-bug-report.md against FILES: (claim, expected kind, dangling)
CLAIMS = [
    ("`_pytest` in `tools/contest/gates.py` always appends `--timeout=180` to the command.", "code", False),
    ("`gates._pytest` adds the flag only when `importlib.util.find_spec(\"pytest_timeout\")` finds it.", "code", False),
    ("`requirements.txt` lists `pytest-timeout` as a requirement.", "code", False),
    ("pytest exits with code 4 when it is given an option it does not recognise.", "world", False),
    ("Without the plugin every root run by `gates.run_tests_detail` exits with code 4, because "
     "`_pytest` passes `--timeout=180`.", "mixed", False),
    ("`Policy._mechanical` in `tools/contest/policy.py` approves a command before it checks `deny_commands`.",
     "code", False),
    ("`Policy.decide` rejects `sudo rm -rf ./build` when `deny_commands` holds `sudo *`.", "code", False),
    ("`Policy.decide(\"cat $HOME/.ssh/id_rsa\")` is approved with the reason \"no path outside\".", "code", False),
    ("`_dirty_outside_runs` in `tools/contest/workspace.py` never reads the return code of `git status`.",
     "code", False),
    ("`_commits_above` in `tools/contest/workspace.py` returns 0 when `git rev-list` fails.", "code", False),
    ("`runner._dirty_tree` raises `TreeReadError` when `git status` cannot be read.", "code", False),
    ("`gates._declared_paths` reads only the first line of a `**File:**` field.", "code", False),
    ("`gates.judge_worktree` runs `git diff --numstat` with rename detection, so a rename shows as `old => new`.",
     "code", False),
    ("`git diff --numstat -M` prints a renamed file as `a => b`.", "world", False),
    ("`tests/test_contest_draft.py` picks the first non-empty `.py` file of the sorted `rglob`.", "code", False),
    ("`tests/test_contest_draft.py::test_the_prompt_carries_the_source` fails when the first file is empty.",
     "code", False),
    ("`tools/contest/gates.py::_declared_files_v2` merges the `**File:**` and `**Also touches:**` fields.",
     "code", True),
    ("`tools/contest/sandbox.py` runs every agent in its own network namespace.", "code", True),
    ("`gates.vanished_fn()` is called by the runner.", "code", True),
    ("`Policy.vanished` is called by the runner.", "code", True),
    ("`scheduler.py` runs the steps in parallel threads.", "code", True),
    ("Ticket `epic-tasks/123-kc76-the-contest-runs-on-a-target.md` has the status landed.", "code", False),
    ("KC-76 is landed, ticket 34 is open, and KC-9999 was never filed.", "code", True),
    ("Commit {sha} changed only the ticket text.", "code", False),
    ("The subject of commit {sha} is `base`.", "code", False),
    ("`contest.ini` lists `sudo *` in `deny_commands`, and an `fnmatch` `*` matches spaces too.", "mixed", False),
    ("`gates.run_tests_detail` passes `check=False` to `subprocess.run`, which then does not raise.",
     "mixed", False),
    ("`Policy.decide` uses `str.rfind`, which returns -1 when the character is absent.", "mixed", False),
    ("`gates.judge_worktree` uses `time.time` as its default clock.", "code", False),
    ("`gates.judge_worktree` prints with `print()` rather than through `logging`.", "code", False),
    ("In Python's `re`, the `re.M` flag makes `.` match a newline.", "world", False),
    ("`subprocess.run` does not raise on a non-zero exit status unless `check=True` is passed.", "world", False),
    ("`importlib.util.find_spec` returns None for a module that is not installed.", "world", False),
    ("`configparser` section names are case-sensitive.", "world", False),
    ("`dict.get(key, default)` returns the default when the stored value is None.", "world", False),
    ("The branch `kc` is ahead of main, and `origin/kc` has the fix.", "world", False),
    ("Nothing in the report says which root failed first.", "world", False),
    ("git prints `dir/{{a => b}}.py` for a rename, and `old => new` in the summary.", "world", False),
    ("chmod 0o500 makes a folder read-only for everyone but root.", "world", False),
    ("The harness waits sixty seconds and then gives up.", "world", False),
]


def test_classify_code_world_mixed(view, repo):
    sha = _git(repo, "rev-parse", "HEAD")[:9]
    assert len(CLAIMS) == 40
    wrong = []
    for claim, kind, dangling in CLAIMS:
        claim = claim.format(sha=sha)
        resolved = _resolve(claim, view)
        got = (classify(claim, resolved), is_dangling(resolved))
        if got != (kind, dangling):
            wrong.append((claim[:60], got, (kind, dangling)))
    assert not wrong


def test_dangling_symbol_stays_code(view):
    claim = "`gates.vanished_fn()` is called by the runner."
    resolved = _resolve(claim, view)
    assert [(r.found, r.path) for r in resolved] == [(False, "tools/contest/gates.py")]
    assert classify(claim, resolved) == "code" and is_dangling(resolved)
    # a name no module of ours carries is not dangling: it is just a word
    other = _resolve("`vanished_fn()` is called by the runner.", view)
    assert [r.found for r in other] == [False] and classify("x", other) == "world"
    assert not is_dangling(other)


def test_ref_alone_is_world(view):
    claim = "the branch `kc` is ahead of main"
    resolved = _resolve(claim, view)
    assert [(r.anchor.kind, r.anchor.text) for r in resolved] == [("ref", "kc")]
    assert classify(claim, resolved) == "world"
    assert _kinds("on main, HEAD and origin/kc and branch kc-2legs-runbook") == [
        ("ref", "main"), ("ref", "HEAD"), ("ref", "origin/kc"), ("ref", "kc-2legs-runbook")]
    assert _kinds("on the other hand, the branch of the tree") == []
    assert [(r.found, r.sha != "") for r in _resolve("HEAD and origin/gone", view)] == [(True, True), (False, False)]


def test_extract_is_deterministic_and_pure(monkeypatch):
    import socket
    claim = "`Policy.decide` in policy.py:3 at commit a73e389 and KC-5, `tests/test_x.py::test_y`"
    first = extract_anchors(claim)

    def forbidden(*args, **kwargs):
        raise AssertionError("extract_anchors did I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert extract_anchors(claim) == first == extract_anchors(claim)
    assert all(isinstance(a, Anchor) for a in first)


def test_edge_cases():
    assert extract_anchors("") == []
    assert _kinds("Ключ `Policy.decide` не читает contest.ini — юникод ✓") == [
        ("symbol", "Policy.decide"), ("path", "contest.ini")]
    assert classify("", []) == "world"
    quote = "```\ndef f():\n    return 1\n```"
    assert extract_anchors(quote) == []
    long_claim = ("`gates.py` and `Policy._mechanical` hold; commit a73e389 " + "word " * 400 + "a/b/c.py ") * 5
    assert len(long_claim) > 10_000
    started = time.time()
    anchors = extract_anchors(long_claim)
    assert time.time() - started < 1 and len(anchors) >= 20
    for evil in ("a/" * 5000, "a." * 5000, "KC-A-" * 2000, "`" * 5000, "a.py:" * 2000):
        started = time.time()
        extract_anchors(evil)
        assert time.time() - started < 1


def test_path_repo_view(repo, tmp_path):
    v = PathRepoView(repo)
    assert v.exists("contest.ini") and v.exists("docs") and not v.exists("gone.md")
    assert not v.exists("../etc/passwd") and not v.exists("/etc/passwd")
    assert v.read("contest.ini") == "[contest]\n"
    with pytest.raises(FileNotFoundError):
        v.read("../outside.txt")
    files = v.files()
    assert files == sorted(files) and "contest.ini" in files
    assert not any(f.startswith(".git") for f in files)
    assert v.rev_parse("HEAD") == _git(repo, "rev-parse", "HEAD")
    assert v.rev_parse("--all") is None and v.rev_parse("nonesuch") is None
    assert "base" in v.git("log", "--format=%s")
    for bad in (("commit", "-m", "x"), ("checkout", "HEAD"), ("log", "--output=/tmp/x")):
        with pytest.raises(ValueError):
            v.git(*bad)
    plain = PathRepoView(tmp_path)           # a directory without .git
    assert plain.rev_parse("HEAD") is None
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "link.txt").symlink_to(outside)
    assert not PathRepoView(tmp_path / "root").exists("link.txt")


def test_model_types_have_the_epic_shape():
    a = Anchor("path", "gates.py", 0, 8)
    r = ResolvedAnchor(a, True, path="gates.py")
    assert (r.qualname, r.lines, r.sha, r.candidates) == ("", (0, 0), "", ())
    pack = Pack("c", "sha", (Chunk("src:a.py:1-2", "source", "a.py", 1, 2, "x = 1", "why"),
                             Chunk("note:1", "note", "", 0, 0, "second", "why")), False)
    assert pack.render() == "x = 1\n\nsecond"
    assert pack.find("second") == "note:1" and pack.find("x = 1") == "src:a.py:1-2"
    assert pack.find("absent") is None and pack.find("") is None


def test_claim_vote_uses_classify_with_a_view(view):
    prose = "The gate `Policy.decide` never looks at the second argument."
    plain = "The harness gives up after a while and then the second model looks at the second argument."
    results = [{"model": m, "run": 0, "votes": {0: "TRUE", 1: "TRUE"}} for m in ("a", "b", "c")]
    claims = [{"claim": prose}, {"claim": plain}]
    with_view = cv.tally(claims, results, view=view)
    assert [(r["kind"], r["needs_code"], r["verdict"]) for r in with_view] == [
        ("code", True, "CODE-CHECK"), ("world", False, "TRUE")]
    assert with_view[0]["dangling"] is False and with_view[1]["unanimous"] is True
    # no view: the old rule, and a prose claim with no repo word is the world's
    old = cv.tally([{"claim": "`tools/contest/gates.py` holds it."}, {"claim": plain}],
                   results, symbols={"decide_it"})
    assert [(r["kind"], r["needs_code"]) for r in old] == [("code", True), ("world", False)]
    assert all(r["dangling"] is False for r in old)


def test_golden_anchors():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert len(golden) == 60
    for case in golden:
        got = [[a.kind, a.text, a.start, a.end] for a in extract_anchors(case["claim"])]
        assert got == case["anchors"], case["claim"]
