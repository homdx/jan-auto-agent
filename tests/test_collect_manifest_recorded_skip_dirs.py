"""A manifest built with skip_dirs is refreshed and freshness-checked under the same skip_dirs."""
from tools.collect import manifest as m


def _tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "out" / "b.py").write_text("y = 2\n", encoding="utf-8")
    skip = m.effective_skip_dirs(None, list(m.DEFAULT_SKIP_DIRS) + ["out"])
    return m.build_manifest(tmp_path, m.discover_files(tmp_path, skip_dirs=skip), skip_dirs=skip)


def test_refresh_keeps_the_recorded_skip_dirs(tmp_path):
    prev = _tree(tmp_path)
    assert sorted(prev.file_hashes) == ["src/a.py"]
    (tmp_path / "src" / "c.py").write_text("z = 3\n", encoding="utf-8")
    refreshed = m.refresh_manifest(tmp_path, prev)
    assert sorted(refreshed.file_hashes) == ["src/a.py", "src/c.py"]     # out/b.py came back
    assert refreshed.skip_dirs == prev.skip_dirs


def test_an_unchanged_tree_is_fresh_with_no_files_given(tmp_path):
    prev = _tree(tmp_path)
    assert m.is_fresh(prev, tmp_path)                                     # was stale
    (tmp_path / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert not m.is_fresh(prev, tmp_path)


def test_a_manifest_that_recorded_no_skip_dirs_walks_the_defaults(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    prev = m.build_manifest(tmp_path, ["a.py"])
    old = m.Manifest(**{**prev.__dict__, "skip_dirs": ()})                # an older manifest
    assert m.is_fresh(old, tmp_path)
    assert sorted(m.refresh_manifest(tmp_path, old).file_hashes) == ["a.py"]
