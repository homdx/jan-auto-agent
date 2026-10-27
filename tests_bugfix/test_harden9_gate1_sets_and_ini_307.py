"""tests_bugfix/test_harden9_gate1_sets_and_ini_307.py — HARDEN-9 D: the operator script
`scripts/gate1_vote_layer2.py` takes a repeated option as one, and a base ini without a
section is a message naming the file and the section instead of a KeyError and a traceback.

`parse_sets("3+veto,3+veto+veto")` used to read two variants, because the duplicate check
compared the sorted option tuples, which differ (`('veto',)` and `('veto', 'veto')`), so
both ran — and both paid — as the same variant. And `build_ini` indexed
`cfg["collect"]`, `cfg["gate1"]`, `cfg["gate1_llm"]` and `cfg["loop"]` without looking.
"""

from __future__ import annotations

import configparser
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gate1_vote_layer2", ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
sys.modules["gate1_vote_layer2"] = l2
_spec.loader.exec_module(l2)

SECTIONS = ("gate1_llm", "gate1", "loop", "collect")


def _local() -> configparser.ConfigParser:
    local = configparser.ConfigParser()
    local[l2.P + "rd"] = {"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "rd"}
    return local


class TestRepeatedOptionsAreOne:
    def test_a_repeated_option_is_read_once(self):
        assert l2.parse_sets("3+veto+veto") == [(3, "all", ("veto",))]

    def test_the_repeated_option_is_one_variant(self):
        sets = l2.parse_sets("5:all+veto+veto+notests")
        assert sets == [(5, "all", ("notests", "veto"))]
        assert l2.variant_name(sets[0][0], sets[0][2], sets[0][1]) == "on-5-all-notests-veto"

    def test_two_sets_that_collapse_to_one_name_are_the_existing_refusal(self):
        with pytest.raises(SystemExit, match="named twice"):
            l2.parse_sets("3+veto,3+veto+veto")

    def test_the_refusal_names_the_variant_that_is_twice(self):
        with pytest.raises(SystemExit) as exc:
            l2.parse_sets("3+veto,3+veto+veto")
        assert "on-3-veto" in str(exc.value)

    def test_a_repeated_option_does_not_duplicate_the_run(self):
        # One variant, one name: what used to launch twice.
        assert len(l2.parse_sets("5:all+veto+veto")) == 1

    def test_distinct_options_are_still_distinct(self):
        assert l2.parse_sets("5+veto+notests") == [(5, "committed", ("notests", "veto"))]

    def test_the_unknown_option_is_still_refused(self):
        with pytest.raises(SystemExit, match="options are"):
            l2.parse_sets("5+loud")

    def test_the_duplicate_check_comparing_tuples_is_gone(self):
        # `('veto',)` and `('veto', 'veto')` are one set of options, whatever they are sorted to.
        a = l2.parse_sets("3+veto")[0]
        b = l2.parse_sets("3+veto+veto")[0]
        assert a == b


class TestABaseIniWithoutASection:
    @pytest.mark.parametrize("missing", SECTIONS)
    def test_the_missing_section_is_named_and_so_is_the_file(self, tmp_path, missing):
        base = tmp_path / "base.ini"
        base.write_text(
            "\n".join(f"[{section}]" for section in SECTIONS if section != missing) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            l2.build_ini(tmp_path / "variant.ini", _local(), base, "on", [], "all", "rd")
        message = str(exc.value)
        assert f"[{missing}]" in message
        assert "base.ini" in message

    @pytest.mark.parametrize("missing", SECTIONS)
    def test_it_is_not_a_keyerror(self, tmp_path, missing):
        base = tmp_path / "base.ini"
        base.write_text(
            "\n".join(f"[{section}]" for section in SECTIONS if section != missing) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            l2.build_ini(tmp_path / "variant.ini", _local(), base, "on", [], "all", "rd")

    @pytest.mark.parametrize("missing", SECTIONS)
    def test_nothing_is_written_when_a_section_is_missing(self, tmp_path, missing):
        base = tmp_path / "base.ini"
        base.write_text(
            "\n".join(f"[{section}]" for section in SECTIONS if section != missing) + "\n",
            encoding="utf-8",
        )
        dest = tmp_path / "variant.ini"
        with pytest.raises(SystemExit):
            l2.build_ini(dest, _local(), base, "on", [], "all", "rd")
        assert not dest.exists()

    def test_a_base_ini_with_every_section_still_builds(self, tmp_path):
        base = tmp_path / "base.ini"
        base.write_text("\n".join(f"[{section}]" for section in SECTIONS) + "\n", encoding="utf-8")
        dest = tmp_path / "variant.ini"
        l2.build_ini(dest, _local(), base, "on", [], "all", "rd", opts=("collect",))
        written = configparser.ConfigParser(interpolation=None)
        written.read(dest, encoding="utf-8")
        assert written["gate1"]["vote_collect"] == "auto"
        assert written["collect"]["use_in_auto"] == "true"
        assert written["gate1_llm"]["model"] == "rd"

    def test_the_repository_own_base_ini_still_builds(self, tmp_path):
        dest = tmp_path / "variant.ini"
        l2.build_ini(dest, _local(), ROOT / "agents.ini", "on", [], "all", "rd")
        assert dest.exists()
