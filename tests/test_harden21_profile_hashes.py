"""tests/test_harden21_profile_hashes.py — HARDEN-21 Part C: the `.ini` files
`CONFIG_MAP.md` is built from are tracked, so an edit to a sibling profile is
stale instead of "up to date".

`is_fresh` compared `file_hashes`, which holds modules only, so fixing the gap a
sibling profile was written to close left `--check` and `--collect` answering
"already up to date" and `CONFIG_MAP.md` keeping the old gap until some `.py`
file happened to change. `_profile_paths` is the set those hashes come from —
the primary config file plus every `[collect] sibling_profiles` entry — recorded
in the manifest's own `profile_hashes` map so a profile edit shows as "one file
changed" to nothing but this comparison and never appears as an added or removed
module in a `--refresh` diff.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path

from tools.collect import manifest as manifest_mod
from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    _profile_paths,
    action_check,
    action_collect,
)


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def _manifest(collect_dir: Path) -> manifest_mod.Manifest:
    return manifest_mod.read_manifest(collect_dir / MANIFEST_FILENAME)


def _artifact(collect_dir: Path) -> dict:
    return json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))


# ── the path set ────────────────────────────────────────────────────────────


def test_no_config_path_tracks_no_profile(tmp_path):
    assert _profile_paths(tmp_path, None, None) == []
    assert _profile_paths(tmp_path, _cfg("[collect]\n"), None) == []


def test_a_tree_without_sibling_profiles_tracks_no_profile(tmp_path):
    """Nothing the profiles could change: an edit of `agents.ini` must not make the model stale."""
    assert _profile_paths(tmp_path, _cfg("[collect]\n"), "primary.ini") == []
    assert _profile_paths(tmp_path, _cfg("[collect]\nsibling_profiles =  \n"), "primary.ini") == []


def test_the_sibling_profiles_are_in_the_set(tmp_path):
    cfg = _cfg("[collect]\nsibling_profiles = sibling_a.ini, sibling_b.ini\n")
    assert _profile_paths(tmp_path, cfg, "primary.ini") == [
        "primary.ini", "sibling_a.ini", "sibling_b.ini",
    ]


def test_a_sibling_that_is_the_primary_is_listed_once(tmp_path):
    cfg = _cfg("[collect]\nsibling_profiles = primary.ini, sibling.ini\n")
    assert _profile_paths(tmp_path, cfg, "primary.ini") == ["primary.ini", "sibling.ini"]


def test_blank_and_unlisted_entries_are_skipped(tmp_path):
    cfg = _cfg("[collect]\nsibling_profiles = , ,sibling.ini,,\n")
    assert _profile_paths(tmp_path, cfg, "primary.ini") == ["primary.ini", "sibling.ini"]


def test_a_missing_sibling_still_names_the_path(tmp_path):
    """An absent file is reported by name; `hash_tree` skips it, so the build
    never fails on a typo'd profile path."""
    cfg = _cfg("[collect]\nsibling_profiles = gone.ini\n")
    assert _profile_paths(tmp_path, cfg, "primary.ini") == ["primary.ini", "gone.ini"]
    assert manifest_mod.hash_tree(tmp_path, _profile_paths(tmp_path, cfg, "primary.ini")) == {}


def test_a_config_that_refuses_to_be_read_tracks_nothing(tmp_path):
    """Fail open: a `config` that is not a `ConfigParser` cannot raise into a
    build."""
    assert _profile_paths(tmp_path, [], "primary.ini") == []
    assert _profile_paths(tmp_path, "", "primary.ini") == []
    assert _profile_paths(tmp_path, None, "primary.ini") == []


def test_an_absolute_config_path_is_kept_as_given(tmp_path):
    outside = tmp_path.parent / "primary.ini"
    cfg = _cfg("[collect]\nsibling_profiles = sibling.ini\n")
    assert _profile_paths(tmp_path, cfg, str(outside)) == [str(outside), "sibling.ini"]


def test_paths_are_relative_to_the_root_and_dotted(tmp_path):
    cfg = _cfg("[collect]\nsibling_profiles = sibling.ini\n")
    assert _profile_paths(tmp_path, cfg, "primary.ini")[0] == "primary.ini"
    assert "/" in _profile_paths(tmp_path, cfg, "conf/primary.ini")[0]


# ── the manifest field ──────────────────────────────────────────────────────


def test_a_manifest_without_the_key_reads_empty(tmp_path):
    m = manifest_mod.build_manifest(tmp_path, files=["core.py"])
    (m.to_dict())["profile_hashes"] = 0        # malformed, not just missing
    assert manifest_mod.Manifest.from_json(json.dumps(m.to_dict())).profile_hashes == {}
    assert manifest_mod.build_manifest(tmp_path, files=["core.py"]).profile_hashes == {}


def test_profile_hashes_round_trip_and_compare():
    import tempfile

    root = Path(tempfile.mkdtemp())
    (root / "core.py").write_text("def f():\n    return 1\n")
    m = manifest_mod.build_manifest(root, files=["core.py"], profile_hashes={"primary.ini": "a"})
    assert m.profile_hashes == {"primary.ini": "a"}
    clone = manifest_mod.Manifest.from_json(m.to_json())
    assert clone.profile_hashes == m.profile_hashes
    assert manifest_mod.is_fresh(clone, root, files=["core.py"],
                                 hashes=manifest_mod.hash_tree(root, ["core.py"]),
                                 profile_hashes={"primary.ini": "a"}) is True
    assert manifest_mod.is_fresh(clone, root, files=["core.py"],
                                 hashes=manifest_mod.hash_tree(root, ["core.py"]),
                                 profile_hashes={"primary.ini": "b"}) is False
    # Omitting it keeps today's behaviour exactly.
    assert manifest_mod.is_fresh(clone, root, files=["core.py"],
                                 hashes=manifest_mod.hash_tree(root, ["core.py"])) is True


# ── the verdict, end to end ─────────────────────────────────────────────────


def _repo(tmp_path: Path):
    (tmp_path / "core.py").write_text("def core():\n    return 1\n")
    (tmp_path / "primary.ini").write_text("[collect]\ndir = .collect\nsibling_profiles = sibling.ini\n")
    (tmp_path / "sibling.ini").write_text("[collect]\n")
    return tmp_path


def test_editing_a_sibling_profile_makes_the_verdict_stale(tmp_path):
    root = _repo(tmp_path)
    cfg = _cfg((root / "primary.ini").read_text())

    first = action_collect(root, config=cfg, config_path="primary.ini")
    assert first.wrote is True

    collect_dir = root / ".collect"
    man = _manifest(collect_dir)
    assert set(man.profile_hashes) == {"primary.ini", "sibling.ini"}
    assert man.profile_hashes["primary.ini"] == manifest_mod.hash_tree(
        root, _profile_paths(root, cfg, "primary.ini"))["primary.ini"]
    # The profile files are not modules, so a profile edit is not a module edit.
    assert "primary.ini" not in man.file_hashes
    assert "sibling.ini" not in man.file_hashes

    assert action_check(root, config=cfg, config_path="primary.ini").fresh is True

    (root / "sibling.ini").write_text("[collect]\ndir = .collect2\n")

    stale = action_check(root, config=cfg, config_path="primary.ini")
    assert stale.fresh is False
    assert "profile config changed" in stale.message, stale.message
    assert "tracked file" not in stale.message, stale.message

    rebuilt = action_collect(root, config=cfg, config_path="primary.ini")
    assert rebuilt.wrote is True

    assert action_check(root, config=cfg, config_path="primary.ini").fresh is True
    assert _manifest(collect_dir).profile_hashes["sibling.ini"] == manifest_mod.hash_tree(
        root, ["sibling.ini"])["sibling.ini"]


def test_the_sibling_gap_in_the_artifact_follows_the_edit(tmp_path):
    """The reason the verdict is stale: `CONFIG_MAP.md`'s gaps come from these
    same files, so they are recomputed on the same run."""
    root = _repo(tmp_path)
    cfg = _cfg((root / "primary.ini").read_text())
    collect_dir = root / ".collect"

    action_collect(root, config=cfg, config_path="primary.ini")
    before = _artifact(collect_dir)["sibling_gaps"]
    assert {g["key"] for g in before} >= {"dir", "sibling_profiles"}, before
    assert all(g["missing_in"] == ["sibling.ini"] for g in before)
    md_before = (collect_dir / "CONFIG_MAP.md").read_text(encoding="utf-8")
    assert "collect.dir" in md_before and "sibling.ini" in md_before

    (root / "sibling.ini").write_text("[collect]\ndir = .collect2\nsibling_profiles = sibling.ini\n")
    action_collect(root, config=cfg, config_path="primary.ini")

    after = _artifact(collect_dir)["sibling_gaps"]
    assert after == [], after
    md_after = (collect_dir / "CONFIG_MAP.md").read_text(encoding="utf-8")
    assert md_before != md_after
    assert "_No gaps detected._" in md_after
    assert "collect.dir" not in md_after


def test_a_tree_without_a_profile_keeps_its_old_manifest_shape(tmp_path):
    (tmp_path / "core.py").write_text("def core():\n    return 1\n")
    result = action_collect(tmp_path)
    assert result.wrote is True
    man = _manifest(tmp_path / ".collect")
    assert man.profile_hashes == {}
    assert action_check(tmp_path).fresh is True


def test_config_path_default_keeps_the_legacy_verdict(tmp_path):
    """A caller that does not know its own `config_path` — every older call
    site — keeps today's answer: nothing extra is tracked."""
    root = _repo(tmp_path)
    cfg = _cfg((root / "primary.ini").read_text())
    action_collect(root, config=cfg)
    assert _manifest(root / ".collect").profile_hashes == {}
    (root / "sibling.ini").write_text("[collect]\ndir = .collect2\n")
    # Without `config_path` the verdict cannot see the edit and stays fresh.
    assert action_check(root, config=cfg).fresh is True
