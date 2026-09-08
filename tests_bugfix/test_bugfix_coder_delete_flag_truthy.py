r"""tests_bugfix/test_bugfix_coder_delete_flag_truthy.py

Bug A5 (consolidated bug report):

  tools/auto/coder.py has two sites that gate the file-delete branch
  on the strict identity check:

      if item.get("delete") is True:
          parsed.append({"path": path, "delete": True})

  Only the JSON literal `true` is accepted. Models regularly emit
  truthy variants — `"delete": "true"` (string), `"delete": 1`,
  `"delete": "yes"`, `"delete": "True"` — and every one of them
  falls through to the "missing content" warning and the file
  silently stays in place while the model thinks it deleted it.

Fix:

  Add a small `_is_truthy_delete(value)` static helper that accepts
  JSON `True`, the string "true"/"True"/"TRUE"/"yes"/"Yes"/"1", and
  the integer 1. Update both call sites in
  tools/auto/coder.py:_parse_response (~line 1322) and
  tools/auto/coder.py:_write_files (~line 1857) to use the helper.

Tests:

  tests_bugfix/test_bugfix_coder_delete_flag_truthy.py — direct
  helper unit tests plus an end-to-end test through
  Coder._parse_response confirming the JSON string "true" is now
  recognised as a delete.
"""

from __future__ import annotations

import configparser

import pytest


# ── Coder construction helper (mirrors tests/test_coder_safety_domain.py) ──

def _minimal_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
        "coder":     {"temperature": "0.2", "max_tokens": "1024"},
        "loop":      {"timeout_seconds": "60"},
    })
    return cfg


def _make_coder():
    from tools.auto.coder import Coder
    return Coder(_minimal_config(), "http://localhost:9999", "", "x")


# ── Unit tests for the helper ────────────────────────────────────────────

class TestIsTruthyDeleteHelper:
    """Direct unit tests for the static helper that both call sites
    use to decide whether a 'delete' key signals a file deletion."""

    def test_json_true_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete(True) is True

    def test_json_false_is_not_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete(False) is False

    def test_string_true_lowercase_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("true") is True

    def test_string_true_capitalised_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("True") is True

    def test_string_true_uppercase_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("TRUE") is True

    def test_string_yes_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("yes") is True

    def test_string_yes_capitalised_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("Yes") is True

    def test_integer_one_is_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete(1) is True

    def test_integer_zero_is_not_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete(0) is False

    def test_string_false_is_not_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("false") is False

    def test_none_is_not_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete(None) is False

    def test_unrelated_string_is_not_truthy(self):
        from tools.auto.coder import _is_truthy_delete
        assert _is_truthy_delete("please delete") is False


# ── End-to-end through _parse_response ───────────────────────────────────

class TestParseResponseDeleteBranch:
    """End-to-end: a model reply with {"delete": "true"} (string) must
    be classified as a delete, not as a missing-content item."""

    def _parse(self, payload_obj):
        import json
        coder = _make_coder()
        text = json.dumps(payload_obj)
        parsed, err = coder._parse_response(text, task_id="t")
        return parsed, err

    def test_string_delete_true_is_treated_as_delete(self):
        parsed, err = self._parse({"files": [{"path": "old_module.py",
                                              "delete": "true"}]})
        assert err == "", f"unexpected parse error: {err!r}"
        assert len(parsed) == 1
        assert parsed[0].get("delete") is True
        assert parsed[0].get("path") == "old_module.py"
        assert "content" not in parsed[0], (
            "delete branch should not carry content; got "
            f"{parsed[0]!r}"
        )

    def test_json_bool_delete_true_is_treated_as_delete(self):
        parsed, err = self._parse({"files": [{"path": "old_module.py",
                                              "delete": True}]})
        assert err == ""
        assert len(parsed) == 1
        assert parsed[0].get("delete") is True

    def test_integer_one_is_treated_as_delete(self):
        parsed, err = self._parse({"files": [{"path": "old_module.py",
                                              "delete": 1}]})
        assert err == ""
        assert len(parsed) == 1
        assert parsed[0].get("delete") is True

    def test_string_yes_is_treated_as_delete(self):
        parsed, err = self._parse({"files": [{"path": "old_module.py",
                                              "delete": "yes"}]})
        assert err == ""
        assert len(parsed) == 1
        assert parsed[0].get("delete") is True

    def test_missing_delete_falls_through_to_content_branch(self):
        """When 'delete' is absent and 'content' is provided, the
        item must be classified as a content write (not a delete)."""
        parsed, err = self._parse({"files": [
            {"path": "new_module.py", "content": "print('hi')\n"}
        ]})
        assert err == ""
        assert len(parsed) == 1
        assert "delete" not in parsed[0]
        assert parsed[0].get("content") == "print('hi')\n"

    def test_string_false_is_NOT_treated_as_delete(self):
        parsed, err = self._parse({"files": [
            {"path": "f.py", "delete": "false"},
        ]})
        # No content provided, no delete branch taken → empty parse
        # with a parse-error message (model said "do not delete" but
        # also gave no content). The point is: we must NOT silently
        # treat "false" as a delete.
        assert parsed == [], (
            f"string 'false' must NOT trigger the delete branch, "
            f"got parsed={parsed!r}"
        )