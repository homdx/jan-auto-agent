"""Review of cc-epic-docs: claim_vote inputs — --add-profiles writes a section once, a bad auth.json is one line, 0 means 0, --target options refuse without it."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_diff as cd  # noqa: E402
import claim_vote as cv  # noqa: E402


@pytest.fixture
def kilo(tmp_path, monkeypatch):
    cfg, auth = tmp_path / "kilo.jsonc", tmp_path / "auth.json"
    cfg.write_text('{"provider": {"openrouter": {"options": {"baseURL": "https://x/v1"}}}}')
    auth.write_text('{"openrouter": {"key": "sk-test"}}')
    monkeypatch.setattr(cv, "KILO_CONFIG", cfg)
    monkeypatch.setattr(cv, "KILO_AUTH", auth)
    return auth


def _sections(root):
    return [s for s in cv.read_ini(root).sections() if s.startswith("claim_vote_llm")]


def test_same_ref_twice_writes_one_section(kilo, tmp_path):
    names = cv.add_profiles(["openrouter/gpt-4.1", "openrouter/gpt-4.1"], tmp_path)
    assert names == ["claim_vote_llm.openrouter_gpt_4_1"]
    assert _sections(tmp_path) == ["claim_vote_llm.openrouter_gpt_4_1"]
    cv.add_profiles(["openrouter/gpt-4.1"], tmp_path)          # a second call keeps the one
    assert _sections(tmp_path) == ["claim_vote_llm.openrouter_gpt_4_1"]


def test_slug_collision_between_two_models_is_refused(kilo, tmp_path, capsys):
    with pytest.raises(ValueError, match="both map to"):
        cv.add_profiles(["openrouter/gpt-4.1", "openrouter/gpt-4-1"], tmp_path)
    assert not (tmp_path / "contest.local.ini").exists()
    assert cv.main(["--add-profiles", "openrouter/gpt-4.1", "openrouter/gpt-4-1",
                    "--repo-root", str(tmp_path)]) == 2
    assert "both map to" in capsys.readouterr().err


@pytest.mark.parametrize("auth", ['{"openrouter": "sk-plain-string"}', '["x"]', '{"openrouter": {}}',
                                  '{"openrouter": {"key": 5}}', "not json"])
def test_a_bad_auth_file_is_one_line_never_the_key(kilo, tmp_path, capsys, auth):
    kilo.write_text(auth)
    assert cv.main(["--add-profiles", "openrouter/gpt-4.1", "--repo-root", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "--add-profiles refused" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert "sk-plain-string" not in err


def _parse_main(argv, monkeypatch):
    """claim_vote.main up to the vote: the pacer and the batch it would use."""
    seen = {}

    def stop(*_a, **_k):
        seen.update(interval=cv.PACER.interval)
        raise SystemExit(0)

    monkeypatch.setattr(cv, "ask", stop)
    with pytest.raises(SystemExit):
        cv.main(argv)
    return seen


def test_interval_zero_is_zero(tmp_path, monkeypatch):
    claims = tmp_path / "c.json"
    claims.write_text(json.dumps(["a claim"]))
    seen = _parse_main([str(claims), "--models", "a/x", "b/y", "c/z", "--interval", "0",
                        "--repo-root", str(tmp_path)], monkeypatch)
    assert seen["interval"] == 0


@pytest.mark.parametrize("opt", [["--batch", "0"], ["--per-provider", "0"], ["--interval", "-1"],
                                 ["--runs", "0"]])
def test_a_count_out_of_range_is_refused(tmp_path, capsys, opt):
    claims = tmp_path / "c.json"
    claims.write_text(json.dumps(["a claim"]))
    with pytest.raises(SystemExit) as exc:
        cv.main([str(claims), "--models", "a/x", "b/y", "c/z", *opt, "--repo-root", str(tmp_path)])
    assert exc.value.code == 2
    assert opt[0] in capsys.readouterr().err


@pytest.mark.parametrize("opt", [["--expect-sha", "abc1234"], ["--ref", "main"], ["--base", "main"],
                                 ["--fetch"], ["--pack-chars", "900"], ["--no-collect"]])
def test_target_options_refuse_without_target(tmp_path, capsys, opt):
    claims = tmp_path / "c.json"
    claims.write_text(json.dumps(["a claim"]))
    with pytest.raises(SystemExit) as exc:
        cv.main([str(claims), "--models", "a/x", "b/y", "c/z", *opt, "--repo-root", str(tmp_path)])
    assert exc.value.code == 2
    assert f"{opt[0]} need --target" in capsys.readouterr().err


def test_claim_diff_refuses_a_zero_batch_too(tmp_path, capsys):
    claims = tmp_path / "c.json"
    claims.write_text(json.dumps([{"claim": "a claim"}]))
    with pytest.raises(SystemExit) as exc:
        cd.main([str(claims), "--target", ".", "--base", "HEAD", "--head", "HEAD", "--batch", "0",
                 "--models", "a/x", "--repo-root", str(tmp_path)])
    assert exc.value.code == 2 and "--batch 0" in capsys.readouterr().err
