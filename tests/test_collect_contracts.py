"""tests/test_collect_contracts.py — COLLECT-10.

* Every seed contract in `contracts_seed.yaml` cites a `known_edge` that
  resolves to a real top-level symbol in the actual repo (AC: "each seed
  contract passes a citation-check against the Pass A index").
* `build_seed_contracts` returns one static/seed `ContractRecord` per entry,
  sorted by name.
* A seed whose `known_edge` cites a symbol that doesn't exist (renamed,
  removed, typo'd) is a hard failure, not a silent drop — a seed contract
  can never quietly go stale.
* A seed entry missing `name`/`description`/`known_edge` is likewise a
  citation failure, not a coincidentally-valid contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.collect.model import ContractRecord, Provenance
from tools.collect.registries import (
    DEFAULT_CONTRACTS_SEED_PATH,
    ContractCitationError,
    build_seed_contracts,
)
from tools.collect.scanner import scan_repo

REPO_ROOT = Path(__file__).parent.parent


# ── the real seed file loads and cites real symbols ────────────────────────


def test_default_seed_file_loads_against_real_repo():
    modules = scan_repo(REPO_ROOT)
    contracts = build_seed_contracts(modules, seed_path=DEFAULT_CONTRACTS_SEED_PATH)

    assert len(contracts) >= 4
    for c in contracts:
        assert isinstance(c, ContractRecord)
        assert c.kind == "seed"
        assert c.provenance == Provenance.STATIC
        assert c.known_edge  # every seed contract in the shipped file cites something

    # sorted by name (COLLECT-3 determinism)
    names = [c.name for c in contracts]
    assert names == sorted(names)


def test_known_seed_contracts_present_by_name():
    modules = scan_repo(REPO_ROOT)
    contracts = build_seed_contracts(modules, seed_path=DEFAULT_CONTRACTS_SEED_PATH)
    by_name = {c.name: c for c in contracts}

    assert "build_chat_request_two_message_list" in by_name
    assert by_name["build_chat_request_two_message_list"].known_edge == (
        "tools/llm_stream.py:build_chat_request"
    )

    assert "ollama_max_tokens_is_num_predict" in by_name
    assert by_name["ollama_max_tokens_is_num_predict"].known_edge == (
        "tools/llm_stream.py:build_chat_request"
    )

    assert "parse_verdict_soft_fail_open" in by_name
    assert by_name["parse_verdict_soft_fail_open"].known_edge == (
        "tools/auto/inner_loop.py:_parse_verdict_soft"
    )

    assert "prompt_store_atomic_save" in by_name
    assert by_name["prompt_store_atomic_save"].known_edge == (
        "tools/prompt_store.py:PromptStore"
    )


# ── citation-check failures ─────────────────────────────────────────────────


def test_seed_with_nonexistent_symbol_raises(tmp_path):
    modules = scan_repo(REPO_ROOT)
    bad_seed = tmp_path / "contracts_seed.yaml"
    bad_seed.write_text(
        """
- name: bogus_contract
  description: cites a function that has never existed
  known_edge: "tools/llm_stream.py:this_function_does_not_exist"
""",
        encoding="utf-8",
    )

    with pytest.raises(ContractCitationError):
        build_seed_contracts(modules, seed_path=bad_seed)


def test_seed_with_wrong_path_raises(tmp_path):
    modules = scan_repo(REPO_ROOT)
    bad_seed = tmp_path / "contracts_seed.yaml"
    bad_seed.write_text(
        """
- name: bogus_path_contract
  description: right symbol name, wrong/renamed module path
  known_edge: "tools/no_such_module.py:build_chat_request"
""",
        encoding="utf-8",
    )

    with pytest.raises(ContractCitationError):
        build_seed_contracts(modules, seed_path=bad_seed)


def test_seed_entry_missing_known_edge_raises(tmp_path):
    modules = scan_repo(REPO_ROOT)
    bad_seed = tmp_path / "contracts_seed.yaml"
    bad_seed.write_text(
        """
- name: no_citation_contract
  description: an assertion with nothing to check it against
""",
        encoding="utf-8",
    )

    with pytest.raises(ContractCitationError):
        build_seed_contracts(modules, seed_path=bad_seed)


def test_seed_entry_missing_name_raises(tmp_path):
    modules = scan_repo(REPO_ROOT)
    bad_seed = tmp_path / "contracts_seed.yaml"
    bad_seed.write_text(
        """
- description: forgot the name field
  known_edge: "tools/llm_stream.py:build_chat_request"
""",
        encoding="utf-8",
    )

    with pytest.raises(ContractCitationError):
        build_seed_contracts(modules, seed_path=bad_seed)


# ── absent/empty seed file is not an error ──────────────────────────────────


def test_absent_seed_file_yields_no_contracts(tmp_path):
    modules = scan_repo(REPO_ROOT)
    missing = tmp_path / "does_not_exist.yaml"
    assert build_seed_contracts(modules, seed_path=missing) == []


def test_empty_seed_file_yields_no_contracts(tmp_path):
    modules = scan_repo(REPO_ROOT)
    empty = tmp_path / "contracts_seed.yaml"
    empty.write_text("", encoding="utf-8")
    assert build_seed_contracts(modules, seed_path=empty) == []
