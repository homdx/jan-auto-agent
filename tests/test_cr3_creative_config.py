"""tests/test_cr3_creative_config.py — AUTO-CR-3: Creative config profile.

Tests cover the _cfg_mode helper and verify that the three consumers
(Coder, RepoIngestor, Architect) pick up mode-specific keys correctly.
"""

import configparser


from tools.auto.utils import _cfg_mode


# ─────────────────────────────────────────────────────────────────────────────
# _cfg_mode unit tests
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**sections) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    return cfg


class TestCfgMode:

    def test_mode_key_overrides_base(self):
        cfg = _cfg(coder={"max_tokens": "800", "max_tokens_creative": "2048"})
        assert _cfg_mode(cfg, "coder", "max_tokens", "creative", "0") == "2048"

    def test_fallback_to_base_when_creative_missing(self):
        cfg = _cfg(coder={"max_tokens": "800"})
        assert _cfg_mode(cfg, "coder", "max_tokens", "creative", "0") == "800"

    def test_code_mode_ignores_creative_keys(self):
        cfg = _cfg(coder={"max_tokens": "800", "max_tokens_creative": "2048"})
        # In code mode the mode key is max_tokens_code which is absent → base wins
        assert _cfg_mode(cfg, "coder", "max_tokens", "code", "0") == "800"

    def test_fallback_used_when_neither_key_present(self):
        cfg = _cfg(coder={})
        assert _cfg_mode(cfg, "coder", "max_tokens", "creative", "999") == "999"

    def test_missing_section_returns_fallback(self):
        cfg = configparser.ConfigParser()
        assert _cfg_mode(cfg, "nosuchsection", "key", "creative", "42") == "42"

    def test_mode_key_wins_over_base_for_docs(self):
        cfg = _cfg(coder={"max_tokens": "800", "max_tokens_docs": "1024"})
        assert _cfg_mode(cfg, "coder", "max_tokens", "docs", "0") == "1024"

    def test_none_fallback_returned_when_absent(self):
        cfg = _cfg(coder={})
        assert _cfg_mode(cfg, "coder", "missing_key", "creative") is None


# ─────────────────────────────────────────────────────────────────────────────
# Coder picks up max_tokens_creative / num_ctx_creative
# ─────────────────────────────────────────────────────────────────────────────

def _make_coder(task_mode: str, extra_coder: dict | None = None):
    from tools.auto.coder import Coder
    coder_cfg = {
        "temperature": "0.2",
        "max_tokens":  "800",
        "max_tokens_creative": "2048",
        "num_ctx_creative": "8192",
    }
    if extra_coder:
        coder_cfg.update(extra_coder)
    cfg = _cfg(**{
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:11434",
            "api_key":    "test",
            "model":      "llama3.1:8b",
            "api_format": "ollama",
            "num_ctx":    "4096",
        },
        "coder": coder_cfg,
        "loop":  {"timeout_seconds": "30"},
    })
    return Coder(
        config=cfg, base_url="http://localhost:11434",
        api_key="test", model="llama3.1:8b",
        api_format="ollama", verify_ssl=False, task_mode=task_mode,
    )


class TestCoderCreativeConfig:

    def test_creative_max_tokens_override(self):
        c = _make_coder("creative")
        assert c._max_tokens == 2048

    def test_creative_num_ctx_override(self):
        c = _make_coder("creative")
        assert c._num_ctx == 8192

    def test_code_mode_uses_base_max_tokens(self):
        c = _make_coder("code")
        assert c._max_tokens == 800

    def test_code_mode_uses_api_num_ctx(self):
        # In code mode [coder] num_ctx_code is absent, falls back to [api_local] num_ctx = 4096
        c = _make_coder("code")
        assert c._num_ctx == 4096

    def test_fallback_when_creative_max_tokens_absent(self):
        c = _make_coder("creative", extra_coder={
            "max_tokens": "800",
            "max_tokens_creative": "",   # empty → treated as absent by _cfg_mode
        })
        # empty string is falsy for _cfg_mode: it returns "" which int("") raises
        # so let's test the case where the key is simply missing
        from tools.auto.coder import Coder
        cfg = _cfg(**{
            "api":       {"active": "local", "verify_ssl": "false"},
            "api_local": {"base_url": "http://x", "api_key": "t", "model": "m", "api_format": "ollama", "num_ctx": "4096"},
            "coder":     {"temperature": "0.2", "max_tokens": "800"},
            "loop":      {"timeout_seconds": "30"},
        })
        c2 = Coder(config=cfg, base_url="http://x", api_key="t", model="m",
                   api_format="ollama", verify_ssl=False, task_mode="creative")
        assert c2._max_tokens == 800   # falls back to base key


# ─────────────────────────────────────────────────────────────────────────────
# RepoIngestor picks up max_file_kb_creative
# ─────────────────────────────────────────────────────────────────────────────

class TestRepoIngestorCreativeConfig:

    def _make_ingestor(self, task_mode: str, search_cfg: dict | None = None):
        from tools.auto.repo_ingest import RepoIngestor
        s = {"max_file_kb": "200", "max_file_kb_creative": "400",
             "max_depth": "8", "skip_dirs": ".git"}
        if search_cfg:
            s.update(search_cfg)
        cfg = _cfg(search=s)
        return RepoIngestor("/tmp", config=cfg, task_mode=task_mode)

    def test_creative_max_file_kb_override(self):
        r = self._make_ingestor("creative")
        assert r._max_file_kb == 400

    def test_code_mode_uses_base_max_file_kb(self):
        r = self._make_ingestor("code")
        assert r._max_file_kb == 200

    def test_fallback_when_creative_key_absent(self):
        from tools.auto.repo_ingest import RepoIngestor
        cfg = _cfg(search={"max_file_kb": "200", "max_depth": "8", "skip_dirs": ".git"})
        r = RepoIngestor("/tmp", config=cfg, task_mode="creative")
        assert r._max_file_kb == 200   # no creative override → base wins

    def test_no_config_uses_default(self):
        from tools.auto.repo_ingest import RepoIngestor
        r = RepoIngestor("/tmp", config=None, task_mode="creative")
        assert r._max_file_kb == 500   # built-in default


# ─────────────────────────────────────────────────────────────────────────────
# Architect creative_acceptance_default is config-driven
# ─────────────────────────────────────────────────────────────────────────────

def _make_cluster_reviewer(task_mode: str, auto_cfg: dict | None = None):
    from tools.auto.architect import ClusterReviewer
    auto = {
        "git_user": "test", "git_email": "test@test",
        "max_runtime_min": "0", "max_tasks_per_run": "0",
        "exec_timeout_sec": "30", "task_mode": task_mode,
        "creative_acceptance_default": "true",
    }
    if auto_cfg:
        auto.update(auto_cfg)
    cfg = _cfg(**{
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://x", "api_key": "t", "model": "m",
                      "api_format": "ollama", "num_ctx": "4096"},
        "architect": {"temperature": "0.2", "max_tokens": "512",
                      "max_file_chars": "1500", "max_files_per_review": "3",
                      "rewrite_max_tokens": "256"},
        "search":    {"max_file_kb": "200", "max_depth": "8", "skip_dirs": ".git"},
        "loop":      {"timeout_seconds": "30"},
        "auto":      auto,
    })
    return ClusterReviewer(
        config=cfg, base_url="http://x", api_key="t",
        model="m", api_format="ollama", verify_ssl=False, task_mode=task_mode,
    )


class TestArchitectCreativeAcceptanceDefault:

    def test_missing_acceptance_in_creative_defaults_to_true(self):
        """creative_acceptance_default=true → missing acceptance_check becomes 'true'."""
        arch = _make_cluster_reviewer("creative")
        # Build a minimal candidate JSON without acceptance_check
        import json
        candidates_json = json.dumps([{
            "title": "Write chapter 1",
            "instruction": "Write the opening chapter.",
            "target_files": ["chapter_01.md"],
            "cited_location": {"file": "chapter_01.md", "symbol": None},
            # no acceptance_check key
        }])
        tasks = arch._parse_candidates(candidates_json, "test-cluster")
        assert len(tasks) == 1
        assert tasks[0].acceptance_check == "true"

    def test_missing_acceptance_in_code_mode_is_rejected(self):
        """In code mode, missing acceptance_check → task rejected (no default)."""
        arch = _make_cluster_reviewer("code")
        import json
        candidates_json = json.dumps([{
            "title": "Fix bug",
            "instruction": "Fix the bug.",
            "target_files": ["src/foo.py"],
            "cited_location": {"file": "src/foo.py", "symbol": "my_func"},
            # no acceptance_check
        }])
        tasks = arch._parse_candidates(candidates_json, "test-cluster")
        assert len(tasks) == 0   # rejected

    def test_creative_acceptance_default_false_rejects(self):
        """creative_acceptance_default=false → even creative mode rejects missing acceptance."""
        arch = _make_cluster_reviewer("creative", auto_cfg={"creative_acceptance_default": "false"})
        import json
        candidates_json = json.dumps([{
            "title": "Write chapter 1",
            "instruction": "Write the opening chapter.",
            "target_files": ["chapter_01.md"],
            "cited_location": {"file": "chapter_01.md", "symbol": None},
        }])
        tasks = arch._parse_candidates(candidates_json, "test-cluster")
        assert len(tasks) == 0   # rejected when default disabled


# ─────────────────────────────────────────────────────────────────────────────
# agents.ini has the new keys
# ─────────────────────────────────────────────────────────────────────────────

def test_agents_ini_has_creative_coder_keys():
    cfg = configparser.ConfigParser()
    cfg.read("agents.ini")
    assert cfg.has_option("coder", "num_ctx_creative"),    "missing [coder] num_ctx_creative"
    assert cfg.has_option("coder", "max_tokens_creative"), "missing [coder] max_tokens_creative"
    assert int(cfg.get("coder", "num_ctx_creative"))    == 8192
    assert int(cfg.get("coder", "max_tokens_creative")) == 2048


def test_agents_ini_has_creative_search_key():
    cfg = configparser.ConfigParser()
    cfg.read("agents.ini")
    assert cfg.has_option("search", "max_file_kb_creative"), "missing [search] max_file_kb_creative"
    assert int(cfg.get("search", "max_file_kb_creative")) == 400


def test_agents_ini_has_auto_creative_keys():
    cfg = configparser.ConfigParser()
    cfg.read("agents.ini")
    assert cfg.has_option("auto", "creative_acceptance_default")
    assert cfg.getboolean("auto", "creative_acceptance_default") is True
    # Also verify the bounded-loop caps added alongside (consumed by CR-5 / CR-7)
    for key in ("max_compression_passes", "max_fidelity_rounds",
                "canon_check_every", "max_canon_revisions"):
        assert cfg.has_option("auto", key), f"missing [auto] {key}"
