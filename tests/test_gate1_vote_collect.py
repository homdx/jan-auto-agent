"""tests/test_gate1_vote_collect.py — CC-13: collect for the judges.

`[collect] skip_dirs` prunes the walk and the manifest and is recorded on it, so a change of the
setting is a stale artifact rather than a silent drift; `load_readonly` is the only read the vote
does (never a write, never a raise); `[gate1] vote_collect = auto` puts the artifact's facts in the
voters' packs while `off` leaves the packs byte for byte as they were; and the run's counters and
its one `collect:` line say what its judges had.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location("gate1_vote_layer2",
                                               PROJECT_ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
sys.modules["gate1_vote_layer2"] = l2
_spec.loader.exec_module(l2)

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.claimcheck import judge as cc_judge
from tools.collect import cli as cli_mod
from tools.collect import loader as cl
from tools.collect import manifest as mf

VOTERS = ("kenary/step-5-preview", "kenary/muse-spark-1-3", "kenary/nemotron-3-ultra")

MODULE = textwrap.dedent("""\
    def load_0(path):
        data = open(path).read()
        return data
""")

# Smallest artifact the voters can read: one contract on the cited module, plus the empty container.
ARTIFACT = {
    "modules": [],
    "contracts": [{"name": "the contract", "known_edge": "pkg/m0.py", "kind": "seed"}],
    "fail_open_registry": [],
    "gates": [],
    "risk_index": [],
    "config_map": [],
    "import_edges": {},
    "imported_by": {},
}

REPORT = "Checked 2 task(s): 1 confirmed still needed, 1 removed as false positive(s).\n"


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

def _cfg(**gate1) -> configparser.ConfigParser:
    keys = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false", "llm_call_retry_wait_sec": "0"}
    keys.update(gate1)
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        "gate1": keys,
        "loop": {"timeout_seconds": "10"},
        "collect": {},
    })
    return cfg


def _collect_cfg(**collect) -> configparser.ConfigParser:
    cfg = _cfg()
    for key, value in collect.items():
        cfg["collect"][key] = value
    return cfg


def _skip_cfg(raw: str) -> configparser.ConfigParser:
    return _collect_cfg(skip_dirs=raw)


def _on(**extra) -> configparser.ConfigParser:
    return _cfg(claim_vote="on", vote_profiles=", ".join(VOTERS), **extra)


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m0.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "pkg" / "m1.py").write_text("CONST = 1\n", encoding="utf-8")
    return tmp_path


def _artifact(tree: Path, config: configparser.ConfigParser | None = None) -> Path:
    """A minimal but real artifact plus its manifest into the tree's `[collect] dir`; read-only data,
    written the way `main.py --collect` writes it (the manifest is built with the same walk)."""
    out = cli_mod.resolve_collect_dir(tree, config)
    out.mkdir(parents=True, exist_ok=True)
    (out / cli_mod.ARTIFACT_FILENAME).write_text(json.dumps(ARTIFACT), encoding="utf-8")
    mf.write_manifest(mf.build_manifest(tree, config=config), out / cli_mod.MANIFEST_FILENAME)
    return out


def _cand() -> CandidateTask:
    return CandidateTask(title="close the file in load_0",
                         instruction="the file is opened and never closed",
                         target_files=["pkg/m0.py"], acceptance_check="true",
                         cited_location=CitedLocation(file="pkg/m0.py", symbol="load_0"), cluster="pkg")


def _first_chunk(prompt: str) -> tuple:
    """(chunk id, the first code line of it) of the first claim of *prompt*."""
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", prompt)
    return m.group(1), m.group(2).rstrip()


def _voters(packs: list, prompts: list):
    """An `ask_fn` for the three voters, each answering TRUE with a real quote. *packs* and
    *prompts* record the one pack and the one rendered request each voter was asked about, so a
    test can say what the voters read rather than guess at the counter."""
    def ask(ref, items, run, seed, timeout):
        packs.append(items[0][1])
        prompts.append("")

        def render(p):
            prompts[-1] = p
            cid, line = _first_chunk(p)
            return json.dumps([{"id": 1, "verdict": "TRUE", "chunk": cid, "quote": line}])

        got = cc_judge.ask_with_packs(items, run, seed, render, batch=1, code_batch=1)
        return {"model": ref, "run": run, "votes": got["votes"], "accepted": got["accepted"],
                "rejected": got["rejected"]}
    return ask


def _filter(cfg, ask):
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)
    if filt._vote is not None:
        filt._vote._ask_fn = ask
    filt.reader_calls = 0

    def fake_reader(candidate, block, **kw):
        filt.reader_calls += 1
        return True, "the reader says so", "confirmed"

    filt._check_presence = fake_reader
    return filt


def _run(tree: Path, filt, capsys=None):
    counters: dict = {}
    filt.filter([_cand()], tree, counters=counters)
    out = capsys.readouterr().out if capsys is not None else ""
    return counters, out


def _collect_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "collect:" in ln]
    assert len(lines) == 1, out
    return lines[0]


# ── [collect] skip_dirs: the walk, the manifest, the freshness test ──────────────────────────

@pytest.mark.parametrize("raw, want", [
    (None, frozenset(mf.DEFAULT_SKIP_DIRS)),
    ("", frozenset(mf.DEFAULT_SKIP_DIRS)),
    ("contest-out", mf.DEFAULT_SKIP_DIRS | {"contest-out"}),
    (" rounds , contest-out ", mf.DEFAULT_SKIP_DIRS | {"rounds", "contest-out"}),
])
def test_skip_dirs_of_the_collect_section_is_added_to_the_defaults(raw, want):
    cfg = None if raw is None else _skip_cfg(raw)
    assert mf.skip_dirs_from_config(cfg) == want


def test_a_setting_that_is_not_a_string_is_the_defaults_not_an_error():
    class _Broken:
        def get(self, *args, **kwargs):
            raise ValueError("not a parser")

    assert mf.skip_dirs_from_config(_Broken()) == frozenset(mf.DEFAULT_SKIP_DIRS)
    assert mf.skip_dirs_from_config(None) == frozenset(mf.DEFAULT_SKIP_DIRS)
    assert mf.effective_skip_dirs(None, [" ", "", "rounds"]) == mf.DEFAULT_SKIP_DIRS | {"rounds"}


def test_the_walk_and_the_manifest_skip_the_dirs_the_collect_section_names(tmp_path):
    tree = _tree(tmp_path)
    (tree / "contest-out").mkdir()
    (tree / "contest-out" / "extra.py").write_text("X = 1\n", encoding="utf-8")

    assert "contest-out/extra.py" in mf.discover_files(tree)          # today's list walks it
    assert "contest-out/extra.py" not in mf.discover_files(tree, config=_skip_cfg("contest-out"))

    man = mf.build_manifest(tree, config=_skip_cfg("contest-out"))
    assert sorted(set(man.skip_dirs) - set(mf.DEFAULT_SKIP_DIRS)) == ["contest-out"]
    assert ".git" in man.skip_dirs                                     # the defaults are kept, not replaced
    assert "contest-out/extra.py" not in man.file_hashes
    assert "pkg/m0.py" in man.file_hashes

    # the scanner's walk prunes the same way, so the artifact's module list cannot carry the dir either
    from tools.collect import scanner
    assert {m.path for m in scanner.scan_repo(tree, config=_skip_cfg("contest-out"))} == {
        "pkg/m0.py", "pkg/m1.py"}
    assert mf.build_manifest(tree, skip_dirs=["contest-out"]).skip_dirs == tuple(
        sorted(mf.DEFAULT_SKIP_DIRS | {"contest-out"}))


def test_a_change_of_skip_dirs_is_a_stale_manifest(tmp_path):
    tree = _tree(tmp_path)
    (tree / "contest-out").mkdir()
    (tree / "contest-out" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    man = mf.build_manifest(tree, config=_cfg())
    assert mf.is_fresh(man, tree, skip_dirs=mf.skip_dirs_from_config(_cfg())) is True
    assert mf.is_fresh(man, tree, skip_dirs=mf.skip_dirs_from_config(_skip_cfg("contest-out"))) is False


def test_an_older_manifest_without_the_key_reads_back_the_defaults(tmp_path):
    tree = _tree(tmp_path)
    man = mf.Manifest(collector_version=mf.COLLECTOR_VERSION, generated_at="2026-01-01T00:00:00Z",
                      git_sha=None, dirty=False,
                      file_hashes=mf.hash_tree(tree, mf.discover_files(tree)))
    raw = json.loads(man.to_json())
    del raw["skip_dirs"]
    assert mf.Manifest.from_dict(raw).skip_dirs == tuple(sorted(mf.DEFAULT_SKIP_DIRS))


# ── load_readonly: the only read the vote does ────────────────────────────────────────────────

def test_load_readonly_reads_fresh_and_writes_nothing(tmp_path):
    tree = _tree(tmp_path)
    cfg = _cfg()
    collect_dir = _artifact(tree, cfg)
    before = {p.name: p.read_bytes() for p in collect_dir.iterdir()}

    read = cl.load_readonly(tree, config=cfg)
    assert read.status == "fresh" and read.used is True and read.changed == 0
    assert read.model.available
    assert [c.name for c in read.model.contracts_for("pkg/m0.py")] == ["the contract"]

    assert {p.name: p.read_bytes() for p in collect_dir.iterdir()} == before


def test_load_readonly_says_how_many_files_moved(tmp_path):
    tree = _tree(tmp_path)
    cfg = _cfg()
    _artifact(tree, cfg)
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")

    read = cl.load_readonly(tree, config=cfg)
    assert read.status == "stale" and read.changed == 1 and read.used is True
    assert "stale" in read.reason


@pytest.mark.parametrize("staleness, status, used", [
    ("ignore", "stale", False),
    ("refresh", "stale", False),
])
def test_the_staleness_policy_of_a_read_only_reader(tmp_path, staleness, status, used):
    tree = _tree(tmp_path)
    cfg = _collect_cfg(staleness=staleness)
    collect_dir = _artifact(tree, cfg)
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in collect_dir.iterdir()}

    read = cl.load_readonly(tree, config=cfg)
    assert (read.status, read.used) == (status, used)
    assert read.model is None and read.changed == 1
    # the reader does not rebuild: staleness = refresh is honoured by load, not by this one
    assert {p.name: p.read_bytes() for p in collect_dir.iterdir()} == before


def test_a_manifest_of_another_collector_is_not_fresh(tmp_path):
    tree = _tree(tmp_path)
    cfg = _cfg()
    collect_dir = _artifact(tree, cfg)
    man = mf.read_manifest(collect_dir / cli_mod.MANIFEST_FILENAME)
    old = mf.Manifest(collector_version="1", generated_at=man.generated_at, git_sha=man.git_sha,
                      dirty=man.dirty, file_hashes=man.file_hashes, skip_dirs=man.skip_dirs)
    mf.write_manifest(old, collect_dir / cli_mod.MANIFEST_FILENAME)

    read = cl.load_readonly(tree, config=cfg)
    assert read.status == "stale" and "collector_version" in read.reason


@pytest.mark.parametrize("kind", ["no-dir", "no-manifest", "no-artifact", "truncated",
                                  "not-a-dict", "bad-manifest"])
def test_a_broken_artifact_is_absent_and_never_raises(tmp_path, kind):
    tree = _tree(tmp_path)
    cfg = _cfg()
    if kind != "no-dir":
        collect_dir = _artifact(tree, cfg)
        if kind == "no-manifest":
            (collect_dir / cli_mod.MANIFEST_FILENAME).unlink()
        elif kind == "no-artifact":
            (collect_dir / cli_mod.ARTIFACT_FILENAME).unlink()
        elif kind == "truncated":
            (collect_dir / cli_mod.ARTIFACT_FILENAME).write_text('{"modules": [', encoding="utf-8")
        elif kind == "not-a-dict":
            (collect_dir / cli_mod.ARTIFACT_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
        elif kind == "bad-manifest":
            (collect_dir / cli_mod.MANIFEST_FILENAME).write_text("[1]", encoding="utf-8")

    read = cl.load_readonly(tree, config=cfg)
    assert read.status == "absent" and read.model is None and read.used is False
    assert read.reason


# ── [gate1] vote_collect ─────────────────────────────────────────────────────────────────────

def test_vote_collect_is_off_by_default_and_ignores_a_malformed_value():
    assert gv.VoteConfig.from_config(_on()).vote_collect == "off" == gv.DEFAULT_VOTE_COLLECT
    assert gv.VoteConfig.from_config(_cfg()).vote_collect == "off"        # the vote itself is off
    assert gv.VoteConfig.from_config(_cfg()).enabled is False


@pytest.mark.parametrize("raw, want", [
    ("auto", "auto"), ("AUTO", "auto"), ("off", "off"),
    ("", "off"), ("yes", "off"), ("1", "off"), ("maybe", "off"),
])
def test_vote_collect_reads_auto_and_falls_back_to_off(raw, want):
    cfg = _on(vote_collect=raw)
    got = gv.VoteConfig.from_config(cfg)
    assert got.enabled and got.vote_collect == want


def test_vote_collect_off_reads_the_artifact_neither_the_reader_nor_the_voters(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on()                                   # the default: the artifact is there and unused
    _artifact(tree, cfg)
    packs, prompts = [], []
    counters, out = _run(tree, _filter(cfg, _voters(packs, prompts)), capsys)

    assert counters[gv.COUNTER_COLLECT_CHUNKS] == 0
    assert counters[gv.COUNTER_COLLECT_STATE] == "off" == counters["collect_state"]
    assert not [c for c in packs[0].chunks if c.kind == "collect"]
    # neither judge reads the model, so the line does not read it either (no scan): on disk, not checked
    assert _collect_line(out) == "📦 collect: present — reader: not used, vote pack: not used"


def test_vote_collect_auto_puts_the_collect_facts_in_the_voters_packs(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    _artifact(tree, cfg)
    packs, prompts = [], []
    counters, out = _run(tree, _filter(cfg, _voters(packs, prompts)), capsys)

    collect = [c for c in packs[0].chunks if c.kind == "collect"]
    assert len(collect) == 1
    assert collect[0].id == "collect:contracts:pkg/m0.py"
    assert collect[0].text == "contract: the contract"
    assert counters[gv.COUNTER_COLLECT_CHUNKS] == 1
    assert counters[gv.COUNTER_COLLECT_STATE] == "fresh"
    assert "the contract" in prompts[0]           # the fact reached the rendered request
    assert _collect_line(out) == "📦 collect: fresh — reader: not used, vote pack: used"


def test_vote_collect_auto_without_an_artifact_is_the_same_request_as_off(tmp_path):
    tree = _tree(tmp_path)
    packs_on, prompts_on = [], []
    counters_on, _ = _run(tree, _filter(_on(vote_collect="auto"), _voters(packs_on, prompts_on)))
    packs_off, prompts_off = [], []
    _run(tree, _filter(_on(), _voters(packs_off, prompts_off)))

    assert prompts_on == prompts_off              # the voters read exactly what they read before
    assert counters_on[gv.COUNTER_COLLECT_CHUNKS] == 0
    assert counters_on[gv.COUNTER_COLLECT_STATE] == "absent"
    assert not [c for c in packs_on[0].chunks if c.kind == "collect"]


@pytest.mark.parametrize("staleness, state, chunks", [
    ("ignore", "stale", 0),
    ("refresh", "stale", 0),
    ("warn", "stale", 1),
])
def test_vote_collect_auto_honours_the_staleness_policy(tmp_path, staleness, state, chunks):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    cfg["collect"]["staleness"] = staleness
    _artifact(tree, cfg)
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")
    packs = []
    counters, _ = _run(tree, _filter(cfg, _voters(packs, [])))

    assert counters[gv.COUNTER_COLLECT_STATE] == state
    assert counters[gv.COUNTER_COLLECT_CHUNKS] == chunks
    assert len([c for c in packs[0].chunks if c.kind == "collect"]) == chunks


def test_the_read_of_a_tree_is_done_once(tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    cfg = _cfg()
    _artifact(tree, cfg)
    calls, real = [], cl.load_readonly

    def spy(root, config=None):
        calls.append(str(root))
        return real(root, config=config)

    monkeypatch.setattr(cl, "load_readonly", spy)
    gv.clear_collect_reads()
    first = gv.read_collect_model(tree, config=cfg)
    assert first is gv.read_collect_model(tree, config=cfg)
    assert first.status == "fresh" and len(calls) == 1
    gv.clear_collect_reads()
    assert gv.read_collect_model(tree, config=cfg) is not first and len(calls) == 2
    assert gv.read_collect_model(None) is None and gv.read_collect_model("/no/such/tree") is None
    assert len(calls) == 2


# ── the one collect line of a run ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("vote, artifact, pack", [
    ("off", True, "not used"),
    ("auto", True, "used"),
    ("auto", False, "not used"),
    ("off", False, "not used"),
])
def test_the_run_prints_one_line_that_says_what_the_judges_had(tmp_path, capsys, vote, artifact, pack):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect=vote)
    if artifact:
        _artifact(tree, cfg)
    counters, out = _run(tree, _filter(cfg, _voters([], [])), capsys)
    # vote off + an artifact: neither judge reads it, so the line says it is there ("present"), not that it is fresh
    state = ("present" if vote == "off" else "fresh") if artifact else "absent"

    line = _collect_line(out)
    assert line == f"📦 collect: {state} — reader: not used, vote pack: {pack}"
    assert line == out.splitlines()[0]            # at the start of the run, not at the end
    res = l2.read_report(out + REPORT, {})
    assert (res["collect"], res["collect_reader"], res["collect_pack"]) == (state, "not used", pack)


def test_the_line_says_how_many_files_moved(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    _artifact(tree, cfg)
    (tree / "pkg" / "m0.py").write_text("def load_0(path):\n    return open(path).read()\n", encoding="utf-8")
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")

    counters, out = _run(tree, _filter(cfg, _voters([], [])), capsys)
    assert _collect_line(out) == "📦 collect: stale (2 modules changed) — reader: not used, vote pack: used"
    assert counters[gv.COUNTER_COLLECT_STATE] == "stale"


def test_the_line_does_not_need_a_vote(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _cfg()                                   # claim_vote is not set: there is no vote at all
    _artifact(tree, cfg)
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)

    def fake_reader(candidate, block, **kw):
        return True, "the reader says so", "confirmed"

    filt._check_presence = fake_reader
    filt.filter([_cand()], tree)
    assert _collect_line(capsys.readouterr().out) == \
        "📦 collect: present — reader: not used, vote pack: not used"


def test_the_line_says_absent_when_the_artifact_is_broken(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    collect_dir = _artifact(tree, cfg)
    (collect_dir / cli_mod.ARTIFACT_FILENAME).write_text("not json at all", encoding="utf-8")

    _, out = _run(tree, _filter(cfg, _voters([], [])), capsys)
    assert _collect_line(out) == "📦 collect: absent — reader: not used, vote pack: not used"


# ── the layer-2 script's +collect ─────────────────────────────────────────────────────────────

def test_collect_is_a_set_option_and_a_suffix_of_the_variant_name():
    assert l2.parse_sets("7:all+collect,5:committed") == [(7, "all", ("collect",)), (5, "committed", ())]
    assert l2.variant_name(7, ("collect",), "all") == "on-7-all+collect"
    assert l2.variant_name(5, ("collect",)) == "on-5+collect"
    assert l2.variant_name(5, ("notests", "collect")) == "on-5-notests+collect"
    # +collect is a sibling variant, not another vote switch, so --resume cannot confuse the two.
    assert l2.parse_sets("5:all+collect,5:all") == [(5, "all", ("collect",)), (5, "all", ())]
    with pytest.raises(SystemExit, match="named twice"):
        l2.parse_sets("5:all+collect,5:all+collect")
    with pytest.raises(SystemExit, match="options are"):
        l2.parse_sets("5+collects")


def test_build_ini_turns_the_switches_on_for_a_collect_variant(tmp_path):
    local = configparser.ConfigParser()
    local["claim_vote_llm.rd"] = {"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "rd"}
    dest = tmp_path / "variant.ini"

    l2.build_ini(dest, local, PROJECT_ROOT / "agents.ini", "on", [], "all", "rd", opts=("collect",))
    on = configparser.ConfigParser(interpolation=None)
    on.read(dest, encoding="utf-8")
    assert on["gate1"]["vote_collect"] == "auto" and on["collect"]["use_in_auto"] == "true"

    l2.build_ini(dest, local, PROJECT_ROOT / "agents.ini", "on", [], "all", "rd")
    off = configparser.ConfigParser(interpolation=None)
    off.read(dest, encoding="utf-8")
    assert off["gate1"]["vote_collect"] == "off" and off["collect"]["use_in_auto"] == "false"


def test_check_collect_names_the_first_path_that_differs(tmp_path):
    tree = _tree(tmp_path)
    cfg = _cfg()
    artifact = _artifact(tree, cfg)

    assert l2.check_collect(artifact, tree) == ""
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")
    assert l2.check_collect(artifact, tree) == "pkg/m1.py (its bytes differ; 1 file(s) differ in all)"
    (tree / "pkg" / "m0.py").unlink()
    assert l2.check_collect(artifact, tree) == \
        "pkg/m0.py (the manifest has it and the clone does not; 2 file(s) differ in all)"
    (tree / "pkg" / "m0.py").write_text(MODULE, encoding="utf-8")
    (tree / "new.py").write_text("X = 1\n", encoding="utf-8")
    assert l2.check_collect(artifact, tree) == \
        "new.py (the clone has it and the manifest does not; 2 file(s) differ in all)"


def test_check_collect_walks_with_the_list_the_manifest_recorded(tmp_path):
    tree = _tree(tmp_path)
    (tree / "contest-out").mkdir()
    (tree / "contest-out" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    # The manifest skipped contest-out, so the check walks the manifest's own file list.
    assert l2.check_collect(_artifact(tree, _skip_cfg("contest-out")), tree) == ""
    # a manifest of the default list still sees every new file
    default = _artifact(tree, _cfg())
    (tree / "contest-out" / "new.py").write_text("Y = 1\n", encoding="utf-8")
    assert l2.check_collect(default, tree) == \
        "contest-out/new.py (the clone has it and the manifest does not; 1 file(s) differ in all)"


def test_install_collect_copies_the_artifact_into_the_clone(tmp_path):
    tree = _tree(tmp_path)
    artifact = _artifact(tree, _cfg())
    clone = tmp_path / "clone"
    (clone / "pkg").mkdir(parents=True)
    (clone / "pkg" / "m0.py").write_text(MODULE, encoding="utf-8")

    l2.install_collect(artifact, clone)
    out = cli_mod.resolve_collect_dir(clone, None)
    assert out.is_dir()
    for name in (cli_mod.ARTIFACT_FILENAME, cli_mod.MANIFEST_FILENAME):
        assert (out / name).read_bytes() == (artifact / name).read_bytes()


@pytest.mark.parametrize("what, in_dir", [
    ("not a directory", False), ("missing the artifact", True), ("missing the manifest", True),
])
def test_check_collect_refuses_without_raising(tmp_path, what, in_dir):
    tree = _tree(tmp_path)
    if not in_dir:
        assert l2.check_collect(tmp_path / "nope", tree).endswith("is not a directory")
        return
    artifact = _artifact(tree, _cfg())
    if what == "missing the artifact":
        (artifact / cli_mod.ARTIFACT_FILENAME).unlink()
    else:
        (artifact / cli_mod.MANIFEST_FILENAME).unlink()
    assert "has no" in l2.check_collect(artifact, tree)


# ── review findings on CC-13: %, a stale count that was never counted, a scan for a label ────────

def test_a_percent_in_skip_dirs_is_a_directory_name_not_a_crash():
    from tools.collect import manifest as manifest_mod
    for raw, extra in (("x%y", {"x%y"}), ("100%", {"100%"}), ("a, b", {"a", "b"})):
        cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cfg.read_string("[collect]\nskip_dirs = %s\n" % raw)
        assert manifest_mod.skip_dirs_from_config(cfg) == manifest_mod.DEFAULT_SKIP_DIRS | extra


def test_a_stale_reader_with_no_count_says_stale_and_no_count(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _cfg()
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)

    class Bridge:                       # what the real bridge reports for a stale artifact
        usable = False
        status = "stale"

    filt._collect_bridge = Bridge()
    filt._check_presence = lambda candidate, block, **kw: (True, "the reader says so", "confirmed")
    filt.filter([_cand()], tree)
    line = _collect_line(capsys.readouterr().out)
    assert line == "📦 collect: stale — reader: not used, vote pack: not used"
    assert "modules changed" not in line


def test_the_line_does_not_scan_the_tree_for_a_label(tmp_path, capsys, monkeypatch):
    tree = _tree(tmp_path)
    cfg = _cfg()
    _artifact(tree, cfg)

    def boom(*a, **k):
        raise AssertionError("a full scan for a label nobody uses")

    monkeypatch.setattr(cl, "load_readonly", boom)
    monkeypatch.setattr(gv, "read_collect_model", boom)
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)
    filt._check_presence = lambda candidate, block, **kw: (True, "the reader says so", "confirmed")
    filt.filter([_cand()], tree)
    assert _collect_line(capsys.readouterr().out) == "📦 collect: present — reader: not used, vote pack: not used"


def test_artifact_present_is_two_file_checks(tmp_path):
    tree = _tree(tmp_path)
    cfg = _cfg()
    assert cl.artifact_present(tree, config=cfg) is False
    collect_dir = _artifact(tree, cfg)
    assert cl.artifact_present(tree, config=cfg) is True
    (collect_dir / cli_mod.MANIFEST_FILENAME).unlink()
    assert cl.artifact_present(tree, config=cfg) is False


def test_the_layer_2_script_reads_the_bare_stale_and_the_present_line():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate1_vote_layer2_cc13", PROJECT_ROOT / "scripts" / "gate1_vote_layer2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for head in ("fresh", "stale", "stale (3 modules changed)", "present", "absent"):
        m = mod.COLLECT_LINE.search(f"📦 collect: {head} — reader: used, vote pack: not used")
        assert m and m.group(1) == head and m.group(2) == "used" and m.group(3) == "not used"


# ── review findings v3: a +collect variant without the model, the install directory, the read cache ──

def _l2_results(collect_line):
    ids = ["AUTO-T1", "AUTO-T2", "AUTO-T3", "AUTO-T4"]
    truth = {"AUTO-T1": "REAL", "AUTO-T2": "FALSE", "AUTO-T3": "FALSE", "AUTO-T4": "FALSE"}
    base = {"rc": 0, "ok": True, "seconds": 1, "checked": 4, "voters": [], "accept": "all", "opts": [], "removed": {}}
    off = dict(base, variant="off", removed={"AUTO-T2": "x", "AUTO-T3": "x"})
    off2 = dict(base, variant="off-2", removed={"AUTO-T2": "x", "AUTO-T3": "x"})
    # A +collect variant is judged against the baselines that had the model (CC-13).
    had = {"collect": "fresh", "collect_reader": "used", "collect_pack": "not used"}
    offc = dict(base, variant="off+collect", opts=["collect"], removed={"AUTO-T2": "x", "AUTO-T3": "x"}, **had)
    offc2 = dict(base, variant="off-2+collect", opts=["collect"], removed={"AUTO-T2": "x", "AUTO-T3": "x"}, **had)
    on = dict(base, variant="on-3+collect", voters=list("abc"), opts=["collect"],
              removed={"AUTO-T2": "x", "AUTO-T3": "x", "AUTO-T4": "x"},
              votes_asked=4, votes_decided=4, votes_overridden=0, votes_requests=12, votes_skipped=0)
    on.update(collect_line)
    return ids, truth, [off, off2, offc, offc2, on]


def test_a_collect_variant_that_never_had_the_model_is_not_credited(tmp_path):
    ids, truth, results = _l2_results({"collect": "absent", "collect_reader": "not used", "collect_pack": "not used"})
    verdicts = l2.summary(tmp_path, results, ids, truth, {})
    assert verdicts["on-3+collect"].startswith("NO COLLECT") and "absent" in verdicts["on-3+collect"]
    text = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "collect (reader / vote pack)" in text and "absent (not used / not used)" in text


def test_a_collect_variant_that_had_the_model_is_judged_as_any_other(tmp_path):
    ids, truth, results = _l2_results({"collect": "fresh", "collect_reader": "used", "collect_pack": "used"})
    verdicts = l2.summary(tmp_path, results, ids, truth, {})
    assert verdicts["on-3+collect"].startswith("PASS")
    assert "fresh (used / used)" in (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")


def test_a_collect_variant_without_the_line_is_not_credited_either(tmp_path):
    ids, truth, results = _l2_results({})
    assert l2.summary(tmp_path, results, ids, truth, {})["on-3+collect"].startswith("NO COLLECT")


def test_the_artifact_is_installed_where_the_variants_own_ini_says(tmp_path):
    src = tmp_path / "artifact"
    src.mkdir()
    (src / "artifact.json").write_text("{}", encoding="utf-8")
    (src / "collect_manifest.json").write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    ini = tmp_path / "v.ini"
    ini.write_text("[collect]\ndir = build/collect-artifacts\n", encoding="utf-8")
    where = l2.install_collect(src, work, ini)
    assert where == work / "build" / "collect-artifacts" and (where / "artifact.json").is_file()
    assert not (work / ".collect").exists()
    default = l2.install_collect(src, work)                 # no ini: the default directory, as before
    assert default == work / ".collect"


def test_the_collect_read_is_fresh_for_each_gate_1_run(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    filt = _filter(cfg, _voters([], []))
    filt.filter([_cand()], tree)
    assert _collect_line(capsys.readouterr().out).startswith("📦 collect: absent")
    _artifact(tree, cfg)                                    # built after the first run, same process
    filt.filter([_cand()], tree)
    assert _collect_line(capsys.readouterr().out).startswith("📦 collect: fresh")
