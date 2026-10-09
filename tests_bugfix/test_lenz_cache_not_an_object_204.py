"""Bug 25 (round 204): a cache file that is valid JSON but not an object crashed.

json.loads of "[]" or "null" succeeds, and the next get raised AttributeError.
Anything that is not a dict is an empty cache, rewritten as an object on put.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402


@pytest.mark.parametrize("content", ["[]", "null", "42", '"text"', '[{"a": 1}]'])
def test_a_non_object_cache_is_empty_and_rewritten_as_an_object(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    cache = lf.Cache(path)
    assert cache.get("assess", "A claim.") is None
    cache.put("assess", "A claim.", {"verdict": "True"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert lf.Cache(path).get("assess", "A claim.") == {"verdict": "True"}
