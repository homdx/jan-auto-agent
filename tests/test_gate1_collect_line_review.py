"""Review of CC-13 (295): the collect line says why an artifact is stale, the vote line how many collect facts the voters read."""

from __future__ import annotations

import configparser
import json
import re
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.claimcheck import judge as cc_judge
from tools.collect import cli as cli_mod
from tools.collect import loader as cl
from tools.collect import manifest as mf
from tools.collect.scanner import scan_repo

VOTERS = ("kenary/step-5-preview", "kenary/muse-spark-1-3", "kenary/nemotron-3-ultra")

MODULE = textwrap.dedent("""\
    def load_0(path):
        data = open(path).read()
        return data
""")

# one contract on the cited module, the rest the empty containers the producer always writes
ARTIFACT = {
    "modules": [], "contracts": [{"name": "the contract", "known_edge": "pkg/m0.py", "kind": "seed"}],
    "fail_open_registry": [], "gates": [], "risk_index": [], "config_map": [], "import_edges": {}, "imported_by": {},
}

REPORT = "Checked 2 task(s): 1 confirmed still needed, 1 removed as false positive(s).\n"

#: what contest-bench/cc/gate1_known_answers/README.txt builds the layer-2 artifact with
README_SKIP = "contest-out, rounds, claim-check-out"


def _cfg(**gate1) -> configparser.ConfigParser:
    keys = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false", "llm_call_retry_wait_sec": "0"}
    keys.update(gate1)
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        "gate1": keys, "loop": {"timeout_seconds": "10"}, "collect": {},
    })
    return cfg


def _on(**extra) -> configparser.ConfigParser:
    return _cfg(claim_vote="on", vote_profiles=", ".join(VOTERS), **extra)


def _tree(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m0.py").write_text(MODULE, encoding="utf-8")
    (root / "pkg" / "m1.py").write_text("CONST = 1\n", encoding="utf-8")
    return root


def _artifact(tree: Path, config: "configparser.ConfigParser | None" = None) -> Path:
    """An artifact whose manifest is built the way `main.py --collect` builds it: the file list is the
    scanner's (`scan_repo`), the skip list the config's."""
    out = cli_mod.resolve_collect_dir(tree, config)
    out.mkdir(parents=True, exist_ok=True)
    (out / cli_mod.ARTIFACT_FILENAME).write_text(json.dumps(ARTIFACT), encoding="utf-8")
    files = [m.path for m in scan_repo(tree, config=config)]
    mf.write_manifest(mf.build_manifest(tree, files=files, config=config), out / cli_mod.MANIFEST_FILENAME)
    return out


def _skip(raw: str) -> configparser.ConfigParser:
    cfg = _cfg()
    cfg["collect"]["skip_dirs"] = raw
    return cfg


def _set_manifest(collect_dir: Path, **fields) -> None:
    path = collect_dir / cli_mod.MANIFEST_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _cand() -> CandidateTask:
    return CandidateTask(title="close the file in load_0", instruction="the file is opened and never closed",
                         target_files=["pkg/m0.py"], acceptance_check="true",
                         cited_location=CitedLocation(file="pkg/m0.py", symbol="load_0"), cluster="pkg")


def _voters():
    """Three voters that answer TRUE with a real quote of the first chunk they were shown."""
    def ask(ref, items, run, seed, timeout):
        def render(p):
            m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", p)
            return json.dumps([{"id": 1, "verdict": "TRUE", "chunk": m.group(1), "quote": m.group(2).rstrip()}])
        got = cc_judge.ask_with_packs(items, run, seed, render, batch=1, code_batch=1)
        return {"model": ref, "run": run, "votes": got["votes"], "accepted": got["accepted"],
                "rejected": got["rejected"]}
    return ask


def _filter(cfg):
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)
    if filt._vote is not None:
        filt._vote._ask_fn = _voters()
    filt._check_presence = lambda candidate, block, **kw: (True, "the reader says so", "confirmed")
    return filt


def _run(tree: Path, cfg, capsys) -> tuple:
    counters: dict = {}
    _filter(cfg).filter([_cand()], tree, counters=counters)
    return counters, capsys.readouterr().out


def _line(out: str, start: str) -> str:
    lines = [ln for ln in out.splitlines() if start in ln]
    assert len(lines) == 1, out
    return lines[0]


# ── the collect line: why a stale artifact is stale ─────────────────────────────────────────────

def test_a_stale_line_names_another_collector_not_zero_moved_modules(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    _set_manifest(_artifact(tree, cfg), collector_version="0-older")
    counters, out = _run(tree, cfg, capsys)
    assert _line(out, "collect:") == "📦 collect: stale (collector version) — reader: not used, vote pack: used"


def test_a_stale_line_names_another_skip_dirs_list(tmp_path, capsys):
    tree = _tree(tmp_path)
    _artifact(tree, _skip(README_SKIP))            # built as the README says, read with the default list
    counters, out = _run(tree, _on(vote_collect="auto"), capsys)
    assert _line(out, "collect:") == "📦 collect: stale (skip_dirs changed) — reader: not used, vote pack: used"
    read = cl.load_readonly(tree, config=_on(vote_collect="auto"))
    assert read.causes == ("skip_dirs changed",) and "a tracked file changed" not in read.reason


def test_a_stale_line_names_moved_files_and_the_collector_together(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    _set_manifest(_artifact(tree, cfg), collector_version="0-older")
    (tree / "pkg" / "m1.py").write_text("CONST = 2\n", encoding="utf-8")
    (tree / "pkg" / "m2.py").write_text("X = 1\n", encoding="utf-8")
    counters, out = _run(tree, cfg, capsys)
    assert _line(out, "collect:") == \
        "📦 collect: stale (2 modules changed, collector version) — reader: not used, vote pack: used"


# ── the vote line: how many collect facts the voters were shown ─────────────────────────────────

def test_the_vote_line_says_how_many_collect_chunks_the_voters_read(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto")
    _artifact(tree, cfg)
    counters, out = _run(tree, cfg, capsys)
    n = counters[gv.COUNTER_COLLECT_CHUNKS]
    assert n >= 1
    line = _line(out, "Gate 1 vote:")
    assert line.endswith(f", collect chunks {n}")


def test_the_vote_line_has_no_collect_count_with_vote_collect_off(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on()
    _artifact(tree, cfg)
    counters, out = _run(tree, cfg, capsys)
    assert "collect chunks" not in _line(out, "Gate 1 vote:")


def test_a_pack_past_the_cap_is_not_counted_as_read(tmp_path, capsys):
    tree = _tree(tmp_path)
    cfg = _on(vote_collect="auto", vote_max_per_run="0")   # every pack is past the cap: none is put to the voters
    _artifact(tree, cfg)
    counters, out = _run(tree, cfg, capsys)
    assert counters["votes_asked"] == 0
    assert counters[gv.COUNTER_COLLECT_CHUNKS] == 0
    assert _line(out, "Gate 1 vote:").endswith(", collect chunks 0")
