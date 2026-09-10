"""tests/test_collect_metrics_script.py — EPIC M1: `scripts/collect_metrics.py`.

M1's acceptance list, as tests, against `tests/fixtures/collect_mini_repo`:

* the script measures a `.collect/` and changes nothing on disk;
* two consecutive `--json` runs are byte-identical;
* an absent or corrupt `.collect/` is a clear message and exit 1;
* the emitted metrics reconcile with the artifact;
* the JSON is flat, so a later diff is key-by-key;
* the script's `tools/` imports are the renderer and the loader's artifact-only
  read path — never `loader.load()`, which scans the repo to judge freshness;
* the block section is rendered over the real `CollectModel`, so what is
  measured is the block that ships, and a later change to the loader's read
  side (V5, L4–L6) is measured rather than shadowed by a copy.

The mini artifact is produced by `tools.collect.cli.action_collect` with the
seed data neutralized exactly as `tests/test_collect_cli.py` does it: the seeds
cite symbols from this repository and have nothing to say about a fixture with
six modules.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "collect_metrics.py"
MINI_REPO = REPO_ROOT / "tests" / "fixtures" / "collect_mini_repo"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collect_metrics as cm  # noqa: E402

EXPECTED_PATHS = {
    "pkg/__init__.py",
    "pkg/config_reader.py",
    "pkg/error_handling.py",
    "pkg/prompt_store.py",
    "pkg/unguarded.py",
    "pkg/view_trace.py",
}


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Seed data cites real-repo symbols; neutralize it for the fixture, the
    same way `tests/test_collect_cli.py` does."""
    from tools.collect import cli as cli_mod

    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_collect(tmp_path: Path) -> Path:
    """A `.collect/` for the mini fixture repo, built by the real producer."""
    import shutil

    from tools.collect import cli as cli_mod

    repo = tmp_path / "repo"
    shutil.copytree(MINI_REPO, repo)
    result = cli_mod.action_collect(repo)
    assert result.wrote is True
    return result.collect_dir


def _run_script(*args: str) -> subprocess.CompletedProcess:
    """The script as a human runs it, from a neutral working directory."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT / "tests"),
        capture_output=True,
        text=True,
    )


def _tree_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            h.update(str(path.relative_to(root)).encode("utf-8"))
            h.update(path.read_bytes())
    return h.hexdigest()


# ── AC: read-only ────────────────────────────────────────────────────────────


def test_the_script_changes_nothing_on_disk(mini_collect, tmp_path: Path):
    repo = mini_collect.parent
    before = _tree_fingerprint(repo)

    out = _run_script("--collect-dir", str(mini_collect), "--json", str(tmp_path / "out" / "baseline.json"))

    assert out.returncode == 0, out.stderr
    assert _tree_fingerprint(repo) == before
    assert (tmp_path / "out" / "baseline.json").is_file()
    assert out.stdout.startswith("collect dir")


# ── AC: deterministic JSON ───────────────────────────────────────────────────


def test_two_runs_produce_byte_identical_json(mini_collect, tmp_path: Path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for path in (first, second):
        result = _run_script("--collect-dir", str(mini_collect), "--json", str(path))
        assert result.returncode == 0, result.stderr

    assert first.read_bytes() == second.read_bytes()


def test_json_is_flat_and_sorted(mini_collect, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out_path = tmp_path / "baseline.json"
    payload, meta = cm.read_collect_dir(mini_collect)
    metrics = cm.compute_metrics(payload, meta)

    text = json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    cm.write_json(metrics, str(out_path))

    written = out_path.read_text(encoding="utf-8")
    assert written == text, "output must be the canonical sorted flat form"
    assert all(not isinstance(v, (dict, list, tuple, set)) for v in metrics.values()), (
        f"nested value: {[(k, type(v)) for k, v in metrics.items() if isinstance(v, (dict, list, tuple, set))]}"
    )


def test_json_carries_no_clock_value(mini_collect, tmp_path: Path):
    """The determinism contract: nothing in the JSON may come from a timestamp,
    so two runs on the same artifact diff empty."""
    out = tmp_path / "b.json"
    result = _run_script("--collect-dir", str(mini_collect), "--json", str(out))
    assert result.returncode == 0
    written = out.read_text(encoding="utf-8")

    assert "mtime" not in written
    assert cm._utc_stamp(0.0) not in written
    # the header has the mtime; the JSON must not
    table = _run_script("--collect-dir", str(mini_collect)).stdout
    assert "mtime" in table


# ── AC: absent / corrupt .collect ────────────────────────────────────────────


def test_absent_collect_dir_is_exit_1_with_a_message(tmp_path: Path):
    result = _run_script("--collect-dir", str(tmp_path / "nope"))
    assert result.returncode == 1
    assert result.stderr.strip().startswith("collect_metrics:")
    assert "no such directory" in result.stderr


def test_dir_without_an_artifact_is_exit_1(tmp_path: Path):
    (tmp_path / ".collect").mkdir()
    result = _run_script("--collect-dir", str(tmp_path / ".collect"))
    assert result.returncode == 1
    assert "artifact.json: missing" in result.stderr


@pytest.mark.parametrize(
    "payload_text,needle",
    [
        ("{", "unreadable"),
        ("[]", "not an artifact"),
        ('{"modules": 5}', "not an artifact"),
        ('{"modules": {}}', "not an artifact"),
    ],
)
def test_corrupt_artifact_is_exit_1(tmp_path: Path, payload_text: str, needle: str):
    (tmp_path / ".collect").mkdir()
    (tmp_path / ".collect" / "artifact.json").write_text(payload_text, encoding="utf-8")
    result = _run_script("--collect-dir", str(tmp_path / ".collect"))
    assert result.returncode == 1
    assert needle in result.stderr
    # nothing was written: the failure happened before any metric existed
    assert result.stdout == ""


# ── the metric values ────────────────────────────────────────────────────────


def test_the_fixture_scanned_as_expected(mini_collect):
    payload, _ = cm.read_collect_dir(mini_collect)
    assert {m["path"] for m in payload["modules"]} == EXPECTED_PATHS


def test_table_metrics_reconcile_with_the_artifact(mini_collect):
    """The aggregation, re-derived independently from the same JSON: if the two
    disagree, the table is wrong, not the artifact."""
    payload, _ = cm.read_collect_dir(mini_collect)
    metrics = cm.compute_metrics(payload, {"collect_dir": str(mini_collect), "artifact_bytes": 0, "collector_version": "1"})
    modules = payload["modules"]

    by_location: dict = {}
    for m in modules:
        for g in m.get("guarded_accesses") or []:
            by_location.setdefault(g["location"], set()).add(g["status"])

    assert metrics["modules"] == len(modules)
    assert metrics["public_symbols"] == sum(len(m["public_symbols"]) for m in modules)
    assert metrics["signatures_total"] == metrics["public_symbols"]
    assert metrics["signatures_elided"] == sum(
        1 for m in modules for s in m["public_symbols"] if s["signature"].endswith("(...)")
    )
    assert metrics["methods_indexed"] == sum(
        1 for m in modules for s in m["public_symbols"] if "." in s["qualname"].split(":", 1)[-1]
    )
    assert metrics["guarded_accesses_records"] == sum(len(m.get("guarded_accesses") or []) for m in modules)
    assert metrics["guarded_accesses_locations"] == len(by_location)
    assert metrics["guarded_accesses_all_guarded"] == sum(1 for s in by_location.values() if s == {"GUARDED"})
    assert metrics["guarded_accesses_all_unguarded"] == sum(
        1 for s in by_location.values() if s == {"UNGUARDED"}
    )
    assert metrics["guarded_accesses_mixed"] == (
        len(by_location) - metrics["guarded_accesses_all_guarded"] - metrics["guarded_accesses_all_unguarded"]
    )
    assert metrics["modules_with_guarded_access"] == len(
        {m["path"] for m in modules if any(g.get("status") == "GUARDED" for g in m.get("guarded_accesses") or [])}
    )
    assert metrics["except_sites"] == sum(len(m.get("except_sites") or []) for m in modules)
    assert metrics["summaries_purpose_present"] + metrics["summaries_purpose_empty"] == metrics["modules"]
    assert metrics["test_map_entries"] == len(payload["test_map"])
    assert metrics["test_map_modules_covered"] == len(
        {k for k, v in payload["test_map"].items() if v} & {m["path"] for m in modules}
    )


def test_block_metrics_reconcile_with_the_rendered_blocks(mini_collect):
    payload, _ = cm.read_collect_dir(mini_collect)
    metrics = cm.compute_metrics(
        payload, {"collect_dir": str(mini_collect), "artifact_bytes": 0, "collector_version": "1"}
    )
    model = cm.load_model(mini_collect)
    assert model.available is True
    blocks = [cm._rendered(model, m.path, None) for m in model.modules]

    assert metrics["blocks_asked"] == len(blocks)
    assert metrics["blocks_non_empty"] == sum(1 for b in blocks if b)
    assert metrics["blocks_over_budget"] == sum(1 for b in blocks if len(b) > cm.DEFAULT_MAX_CONTEXT_CHARS)

    new_rows = sum(
        1
        for b in blocks
        for line in b.split("\n")
        if (kind := cm._kind_of(line)) is not None and cm.ROW_KIND_CLASS[kind] == "new"
    )
    assert metrics["blocks_rows_new"] == new_rows
    assert metrics["blocks_rows_unknown"] == 0


def test_an_unannounced_symbol_cut_is_counted(mini_collect, monkeypatch):
    """`symbols_silently_cut` is the canary for the old `[:20]`: if the renderer
    drops symbols without announcing them, the metric must move."""
    payload, _ = cm.read_collect_dir(mini_collect)
    real_builder = cm.build_collect_context_block

    def _truncate_silently(model, target_file, **kwargs):
        """A renderer that keeps the first symbol and drops the rest, without
        saying so — the PLAN-v2 V2.3 bug this metric exists to catch."""
        out = []
        for line in real_builder(model, target_file, **kwargs).split("\n"):
            if line.startswith("public_symbols: "):
                head, _, body = line.partition("public_symbols: ")
                symbols = [s for s in body.split(",") if s.strip()]
                line = head + "public_symbols: " + ", ".join(symbols[:1])
            out.append(line)
        return "\n".join(out)

    monkeypatch.setattr(cm, "build_collect_context_block", _truncate_silently)

    metrics = cm.compute_metrics(
        payload, {"collect_dir": str(mini_collect), "artifact_bytes": 0, "collector_version": "1"}
    )
    assert metrics["symbols_silently_cut"] > 0
    assert metrics["symbols_silently_cut_modules"] > 0
    assert metrics["symbols_cut_announced"] == 0


def test_the_script_read_only_contract_holds_in_source():
    """The script imports the renderer and the loader's artifact-only reader,
    nothing else from `tools/` — in particular never `loader.load` (repo scan)
    or `scanner` / `cli.action_collect` (the producer)."""
    source = SCRIPT.read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("from tools") or line.strip().startswith("import tools")
    ]
    assert imports == [
        "from tools.auto.context_assembler import build_collect_context_block  # noqa: E402",
        "from tools.collect.loader import (  # noqa: E402",
    ]
    assert "STATUS_FRESH, CollectModel, _load_from_dir," in source
    # Prose may name the forbidden paths; code may not call them.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    code = code.split('"""', 2)[-1]  # past the module docstring
    for forbidden in ("loader.load(", "scan_repo(", "action_collect("):
        assert forbidden not in code, forbidden
    # and it never opens a path for writing except the one `--json` names
    assert source.count('write_text(') == 1
