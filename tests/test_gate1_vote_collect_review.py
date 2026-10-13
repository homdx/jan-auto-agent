"""Review of CC-13 (295): layer 2's +collect baselines, the artifact's skip_dirs and collector, what the summary credits."""

from __future__ import annotations

import configparser
import csv
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location("gate1_vote_layer2_cc13_review",
                                               PROJECT_ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l2)

from tools.auto import gate1_vote as gv
from tools.auto.collect_bridge import make_collect_bridge
from tools.auto.gate1_filter import Gate1Filter
from tools.collect import cli as cli_mod
from tools.collect import manifest as mf
from tools.collect.scanner import scan_repo

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












# ── layer 2: the line as the summary reads it ───────────────────────────────────────────────────

def test_the_summary_reads_how_many_collect_chunks_the_voters_read():
    vote = "🗳 Gate 1 vote: asked 4, decided 2, overridden by the reader 0, requests 12, skipped 1"
    res = l2.read_report(vote + ", collect chunks 7\n" + REPORT, {})
    assert res["collect_chunks"] == 7 and res["votes_skipped"] == 1 and res["votes_decided"] == 2
    assert "collect_chunks" not in l2.read_report(vote + "\n" + REPORT, {})
    cell = l2._collect_cell({"variant": "on-3+collect", "opts": ["collect"], "collect": "fresh",
                             "collect_reader": "used", "collect_pack": "used", "collect_chunks": 7})
    assert cell == "fresh (used / used, 7 chunks)"


def test_the_summary_reads_the_state_word_and_keeps_why_apart():
    res = l2.read_report("📦 collect: stale (2 modules changed) — reader: not used, vote pack: used\n" + REPORT, {})
    assert (res["collect"], res["collect_detail"]) == ("stale", "2 modules changed")
    res = l2.read_report("📦 collect: stale (collector version, skip_dirs changed) — reader: not used, "
                         "vote pack: used\n" + REPORT, {})
    assert (res["collect"], res["collect_detail"]) == ("stale", "collector version, skip_dirs changed")
    res = l2.read_report("📦 collect: fresh — reader: used, vote pack: used\n" + REPORT, {})
    assert res["collect"] == "fresh" and "collect_detail" not in res


IDS = ["AUTO-T1", "AUTO-T2", "AUTO-T3", "AUTO-T4"]
TRUTH = {"AUTO-T1": "REAL", "AUTO-T2": "FALSE", "AUTO-T3": "FALSE", "AUTO-T4": "FALSE"}
HAD = {"collect": "fresh", "collect_reader": "used", "collect_pack": "not used"}


def _res(variant, removed, opts=(), **extra):
    r = {"variant": variant, "rc": 0, "ok": True, "seconds": 1, "checked": 4, "voters": [], "accept": "all",
         "opts": list(opts), "removed": {i: "x" for i in removed}}
    if not variant.startswith("off"):
        r.update(voters=list("abc"), votes_asked=4, votes_decided=2, votes_overridden=0, votes_requests=12,
                 votes_skipped=0)
    r.update(extra)
    return r


def test_a_collect_variant_is_judged_against_the_baselines_that_had_the_model(tmp_path):
    # the reader alone does better without the model (0 false positives through) than with it (1): the
    # +collect variant (1) is as good as its own kind, the plain one (1) is worse than its own
    results = [
        _res("off", ["AUTO-T2", "AUTO-T3", "AUTO-T4"]),
        _res("off-2", ["AUTO-T2", "AUTO-T3", "AUTO-T4"]),
        _res("off+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **HAD),
        _res("off-2+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **HAD),
        _res("on-3", ["AUTO-T2", "AUTO-T3"]),
        _res("on-3+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **dict(HAD, collect_pack="used")),
    ]
    verdicts = l2.summary(tmp_path, results, IDS, TRUTH, {})
    assert verdicts["on-3+collect"].startswith("PASS"), verdicts
    assert verdicts["on-3"].startswith("FAIL"), verdicts
    assert verdicts["off+collect"] == "baseline with the collect model"
    assert verdicts["off-2+collect"] == "baseline repeat (the reader's own noise) with the collect model"
    assert verdicts["off"] == "baseline" and verdicts["off-2"] == "baseline repeat (the reader's own noise)"
    text = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "The collect model for the reader alone (the vote off): false positives through 1 with it " \
           "(2 `off+collect` run(s)) against 0 without (2 `off` run(s))" in text
    assert "Noise of the baseline with the collect model: `off+collect` and `off-2+collect`" in text


def test_a_collect_variant_without_collect_baselines_has_no_baseline(tmp_path):
    results = [_res("off", ["AUTO-T2", "AUTO-T3"]),
               _res("on-3+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **dict(HAD, collect_pack="used"))]
    verdicts = l2.summary(tmp_path, results, IDS, TRUTH, {})
    assert verdicts["on-3+collect"].startswith("no baseline with the collect model"), verdicts


@pytest.mark.parametrize("line", [
    {"collect": "fresh", "collect_reader": "not used", "collect_pack": "used"},              # the reader went without
    {"collect": "stale", "collect_detail": "skip_dirs changed", "collect_reader": "not used", "collect_pack": "used"},
    {"collect": "stale", "collect_detail": "2 modules changed", "collect_reader": "not used", "collect_pack": "used"},
    {"collect": "stale", "collect_reader": "not used", "collect_pack": "used"},
])
def test_a_collect_variant_is_credited_only_when_both_judges_had_a_fresh_model(tmp_path, line):
    results = [_res("off+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **HAD),
               _res("on-3+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **line)]
    verdict = l2.summary(tmp_path, results, IDS, TRUTH, {})["on-3+collect"]
    assert verdict.startswith("NO COLLECT") and f"reader: {line['collect_reader']}" in verdict
    if line.get("collect_detail"):
        assert f"({line['collect_detail']})" in verdict


def test_a_collect_baseline_that_never_had_the_model_judges_nothing(tmp_path):
    results = [_res("off+collect", ["AUTO-T2", "AUTO-T3"], ("collect",),
                    collect="absent", collect_reader="not used", collect_pack="not used"),
               _res("on-3+collect", ["AUTO-T2", "AUTO-T3"], ("collect",), **dict(HAD, collect_pack="used"))]
    verdicts = l2.summary(tmp_path, results, IDS, TRUTH, {})
    assert verdicts["off+collect"].startswith("NO COLLECT")
    assert verdicts["on-3+collect"].startswith("no baseline with the collect model")


# ── layer 2: the artifact check ─────────────────────────────────────────────────────────────────

def test_check_collect_refuses_an_artifact_of_another_collector(tmp_path):
    tree = _tree(tmp_path)
    artifact = _artifact(tree, _cfg())
    assert l2.check_collect(artifact, tree) == ""
    _set_manifest(artifact, collector_version="0-older")
    problem = l2.check_collect(artifact, tree)
    assert "collector_version '0-older'" in problem and mf.COLLECTOR_VERSION in problem


def test_check_collect_walks_the_tree_the_way_the_collector_does(tmp_path):
    # the collector's walk (`scan_repo`) leaves `build/` out; a walk of its own called a tracked
    # build/x.py "the clone has it and the manifest does not" and refused the right artifact
    tree = _tree(tmp_path)
    (tree / "build").mkdir()
    (tree / "build" / "gen.py").write_text("X = 1\n", encoding="utf-8")
    artifact = _artifact(tree, _cfg())
    assert "build/gen.py" not in json.loads((artifact / cli_mod.MANIFEST_FILENAME).read_text())["file_hashes"]
    assert l2.check_collect(artifact, tree) == ""


def test_the_artifacts_skip_dirs_is_what_a_collect_variant_reads_with(tmp_path):
    tree = _tree(tmp_path)
    assert l2.collect_skip_dirs(_artifact(tree, _cfg())) == ("", "")
    readme = _artifact(tree, _skip(README_SKIP))
    assert l2.collect_skip_dirs(readme) == ("claim-check-out, contest-out, rounds", "")
    _set_manifest(readme, skip_dirs=["contest-out"])            # no setting gives back a list without the defaults
    extra, problem = l2.collect_skip_dirs(readme)
    assert extra == "" and "lacks the default" in problem


# ── layer 2 end to end: a real repository, a real artifact, the judges' own reads ───────────────

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _known(tmp_path: Path, sha: str) -> Path:
    known = tmp_path / "known"
    known.mkdir()
    (known / "TARGET").write_text(sha + "\n", encoding="utf-8")
    plan = {"tasks": [{"id": "AUTO-T1", "title": "close the file in load_0"},
                      {"id": "AUTO-T2", "title": "make CONST a tuple"}]}
    (known / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with open(known / "truth.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "truth"])
        w.writerows([["AUTO-T1", "REAL"], ["AUTO-T2", "FALSE"]])
    return known


def _local(tmp_path: Path) -> Path:
    path = tmp_path / "local.ini"
    lines = []
    # three families, or the vote stays off ("fewer than 3 families") and no pack is built at all
    models = ["agnes-3-0-flash", "step-5-preview", "muse-spark-1-3", "nemotron-3-ultra"]
    for section, model in zip([l2.READER] + l2.ROSTER[:3], models):
        lines += [f"[{l2.P}{section}]", "base_url = http://127.0.0.1:1/v1", "api_key = k", f"model = {model}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_layer_2_with_a_readme_built_artifact_hands_both_judges_a_fresh_model(tmp_path, monkeypatch):
    repo = _tree(tmp_path / "repo")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "target", cwd=repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                         text=True).stdout.strip()
    build = tmp_path / "build"                                   # the README's worktree at the sha
    _git("clone", "-q", str(repo), str(build), cwd=tmp_path)
    artifact = _artifact(build, _skip(README_SKIP))

    real_run = subprocess.run

    def gate1(cmd, *args, **kw):
        """main.py --validate-plan, as far as the judges go: Gate 1's own line from the variant's own ini."""
        if not (len(cmd) > 1 and str(cmd[1]).endswith("main.py")):
            return real_run(cmd, *args, **kw)
        base = Path(cmd[cmd.index("--base") + 1])
        ini = cmd[cmd.index("--config") + 1]
        cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cfg.read(ini, encoding="utf-8-sig")
        filt = Gate1Filter(config=cfg, base_url="http://127.0.0.1:1/v1", api_key="k", model="m",
                           api_format="openai", verify_ssl=False,
                           collect_bridge=make_collect_bridge(base, cfg, ini))
        gv.clear_collect_reads()
        kw["stdout"].write(filt._collect_line(base) + "\n" + REPORT)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(l2.subprocess, "run", gate1)
    out = tmp_path / "out"
    rc = l2.main(["--known", str(_known(tmp_path, sha)), "--repo", str(repo), "--local-ini", str(_local(tmp_path)),
                  "--off-runs", "1", "--sets", "3+collect", "--collect-from", str(artifact), "--out", str(out)])
    ran = {p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in out.glob("*/result.json")}
    assert sorted(ran) == ["off", "off+collect", "on-3+collect"], rc
    assert ran["off"]["opts"] == [] and ran["off"]["collect"] == "absent"
    assert ran["off+collect"]["opts"] == ["collect"]
    assert (ran["off+collect"]["collect"], ran["off+collect"]["collect_reader"]) == ("fresh", "used")
    on = ran["on-3+collect"]
    assert (on["collect"], on["collect_reader"], on["collect_pack"]) == ("fresh", "used", "used")
    variant_ini = configparser.ConfigParser(interpolation=None)
    variant_ini.read(out / "on-3+collect" / "agents.ini", encoding="utf-8")
    assert variant_ini["collect"]["skip_dirs"] == "claim-check-out, contest-out, rounds"
    assert not (out / ".collect-check").exists()                 # the preflight's ini carried the reader's key


def test_layer_2_refuses_an_artifact_the_judges_would_read_as_stale(tmp_path, monkeypatch, capsys):
    repo = _tree(tmp_path / "repo")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "target", cwd=repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
                         text=True).stdout.strip()
    artifact = _artifact(repo, _cfg())
    _set_manifest(artifact, collector_version="0-older")
    real_run = subprocess.run

    def no_gate1(cmd, *args, **kw):
        assert not (len(cmd) > 1 and str(cmd[1]).endswith("main.py")), "a variant ran on a refused artifact"
        return real_run(cmd, *args, **kw)

    monkeypatch.setattr(l2.subprocess, "run", no_gate1)
    rc = l2.main(["--known", str(_known(tmp_path, sha)), "--repo", str(repo), "--local-ini", str(_local(tmp_path)),
                  "--off-runs", "1", "--sets", "3+collect", "--collect-from", str(artifact),
                  "--out", str(tmp_path / "out")])
    assert rc == 2
    # refused by the preflight, before a single variant started: not a run whose variants all failed
    assert "cannot run, --collect-from does not match the target" in capsys.readouterr().err
    out = tmp_path / "out"
    assert not (out / "progress.log").exists()
    assert not out.exists() or not [p for p in out.iterdir() if p.is_dir()]


def test_check_collect_reads_the_variants_ini_raw(tmp_path):
    # a key with a `%` (API keys are random) must not stop the check with an interpolation error
    tree = _tree(tmp_path)
    artifact = _artifact(tree, _cfg())
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string("[gate1_llm]\napi_key = ab%cd\n[collect]\nskip_dirs =\n")
    assert l2.check_collect(artifact, tree, cfg) == ""
