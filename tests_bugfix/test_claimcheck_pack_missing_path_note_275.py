"""CC-5 (275) fix found judging the entries: a file the claim writes out that is not in the repository, nor is its directory, is a note too."""
from __future__ import annotations

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.pack import build_pack


def _pack(tmp_path, claim):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.py").write_text("def alpha():\n    return 1\n")
    view = PathRepoView(tmp_path)
    return build_pack(claim, resolve_anchors(extract_anchors(claim), view), view)


def test_a_path_that_is_nowhere_in_the_repository_is_a_dangling_note(tmp_path):
    """`no/such.py` has no file and no directory, so `path` stayed empty and the pack said "none found": the claim was about code the repository does not have."""
    pack = _pack(tmp_path, "`no/such.py` defines the retry policy")
    notes = [c for c in pack.chunks if c.kind == "note"]
    assert [c.id for c in notes] == ["note:dangling:no/such.py"]
    assert "does not exist" in notes[0].text and "no/such.py" in notes[0].text
    assert pack.render() != "EVIDENCE: none found for this claim."


def test_a_word_that_is_not_a_path_still_gets_no_note(tmp_path):
    """Only a file written out as a path is evidence of a missing file; a bare name that resolves nowhere stays silent."""
    pack = _pack(tmp_path, "the retry policy lives in nothing at all")
    assert not pack.chunks
