"""Round 365 regressions for structured replies and INI loading."""

import configparser
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from tools import llm_stream
from tools.auto import architect, coder, controller, gate1_filter, inner_loop, pipeline, plan_validator
from tools.auto.repo_ingest import RepoCluster
from tools.auto.state import StateStore, make_task
from tools.collect import cli as collect_cli
from tools.skills import loader

TAGS = ["a<think>keep</think>b", "a<think>b", "a</think>b"]
DOUBLE_REASON = '<think>First pass. {"files":[{"path":"out.txt","content":"discard"}]}</think>\n<think>Second pass.</think>\n'


def fenced(body):
    return "```json\n" + body + "\n```"


def settings():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local"},
        "api_local": {"base_url": "http://stub.invalid/v1", "api_key": "fake", "model": "offline", "num_ctx": "32768"},
        "architect": {"max_tokens": "1024"},
        "loop": {"timeout_seconds": "1"},
    })
    return cfg


@pytest.fixture
def complete(monkeypatch):
    def register(response):
        monkeypatch.setattr(llm_stream, "request_completion", lambda *a, **k: response)
    return register


@pytest.mark.parametrize(("envelope", "content"), [
    ("plain", TAGS[0]),
    ("double", TAGS[0]),
    ("double", TAGS[1]),
    ("double", TAGS[2]),
    ("double", TAGS),
    ("fenced", TAGS[0]),
    ("lone close", TAGS[2]),
    ("multiple closes", TAGS[2]),
    ("trailing close", TAGS[0]),
])
def test_coder_writes_the_actual_content(tmp_path, complete, envelope, content):
    original = "original\n"
    (tmp_path / "out.txt").write_text(original)
    task = {"id": "T1", "title": "Update sample", "instruction": "Write sample text",
            "target_files": ["out.txt"], "cited_locations": []}
    payload = json.dumps({"files":[{"path":"out.txt","content":content}]})
    response = {
        "plain": payload,
        "double": DOUBLE_REASON + payload,
        "fenced": DOUBLE_REASON + fenced(payload),
        "lone close": "I worked through the specification.</think>\n" + payload,
        "multiple closes": "</think>First pass.</think>\n" + payload,
        "trailing close": DOUBLE_REASON + payload + "</think>",
    }[envelope]
    complete(response)
    impl = coder.Coder(settings(), "http://stub.invalid/v1", "offline-key", "offline")
    result = impl.generate(task, tmp_path)
    assert result.succeeded, result.error
    expected = content if isinstance(content, str) else "\n".join(content)
    assert (tmp_path / "out.txt").read_text() == expected + "\n"


@pytest.mark.parametrize("response", [
    '<think>{"files":[{"path":"out.txt","content":"discard"}]}</think>',
    '<think>reasoning only; no answer yet',
    '{"files":[{"path":"out.txt","content": BROKEN}]}',
    '{"files":[{"path":"out.txt","content":"a<think>"invalid"</think>b"}]}',
    '<think>{"files":[{"path":"out.txt","content":"discard"}]}</think>\n{"files": BROKEN}',
])
def test_coder_refuses_reasoning_only_and_bad_json(tmp_path, complete, response):
    original = "original\n"
    path = tmp_path / "out.txt"
    path.write_text(original)
    task = {"id": "T1", "title": "Update sample", "instruction": "Write sample text",
            "target_files": ["out.txt"], "cited_locations": []}
    complete(response)
    impl = coder.Coder(settings(), "http://stub.invalid/v1", "offline-key", "offline")
    result = impl.generate(task, tmp_path)
    assert not result.succeeded and path.read_text() == original


def test_architect_preserves_a_title_in_candidate_json(tmp_path, complete):
    (tmp_path / "module.py").write_text("def f():\n    return 0\n")
    cluster = RepoCluster(name="code", patterns=["*.py"], files=["module.py"])
    title = 'Preserve "<think>sample</think>" in the file'
    package = [{"title": title, "instruction": "Improve f", "target_files": ["module.py"],
                "acceptance_check": "python3 -m pytest tests", "cited_location":
                {"file": "module.py", "symbol": "f", "line_start": 1, "line_end": 2}}]
    complete(DOUBLE_REASON + fenced(json.dumps(package)))
    impl = architect.ClusterReviewer(settings(), "http://stub.invalid/v1", "fake", "offline")
    out = impl._review_one_cluster(cluster, tmp_path, "Preserve literals")
    assert [row.title for row in out] == [title]


@pytest.mark.parametrize("marker", TAGS)
def test_task_rewriter_preserves_literal_tag(marker, complete):
    instruction = f"Keep the literal sample {marker} in the output"
    complete(json.dumps({"instruction": instruction, "title": "Update plan"}))
    impl = architect.TaskRewriter(settings(), "http://stub.invalid/v1", "fake", "offline")
    old = {"id": "T1", "title": "Initial task", "instruction": "Try again",
           "target_files": ["module.py"], "acceptance_check": ""}
    assert impl.rewrite(old, ["first attempt failed"])["instruction"] == instruction


@pytest.mark.parametrize("evidence_found", [True, False])
def test_gate1_checks_literal_evidence(evidence_found, complete):
    evidence = 'sample = "<think>literal</think>"'
    source = evidence if evidence_found else 'sample = "different value"'
    candidate = architect.CandidateTask(
        title="Keep the quoted literal", instruction="Find the literal", target_files=["module.py"],
        acceptance_check="", cited_location=architect.CitedLocation(file="module.py", line_start=1),
        cluster="code",
    )
    complete(DOUBLE_REASON + json.dumps({"verdict": "confirmed", "evidence": evidence, "reason": "found"}))
    impl = gate1_filter.Gate1Filter(settings(), "http://stub.invalid/v1", "fake", "offline")
    valid, _, verdict = impl._check_presence(candidate, source, _sleep_fn=lambda _: None)
    assert (valid, verdict) == ((True, "confirmed") if evidence_found else (False, "rejected"))


def test_gate1_does_not_promote_unclosed_reasoning(complete):
    evidence = "sample = 42"
    candidate = architect.CandidateTask(
        title="Candidate", instruction="Check the number", target_files=["module.py"],
        acceptance_check="", cited_location=architect.CitedLocation(file="module.py", line_start=1),
        cluster="code",
    )
    complete("<think>" + fenced(json.dumps({"verdict": "confirmed", "evidence": evidence})))
    impl = gate1_filter.Gate1Filter(settings(), "http://stub.invalid/v1", "fake", "offline")
    valid, _, _ = impl._check_presence(candidate, evidence, _sleep_fn=lambda _: None)
    assert not valid


@pytest.mark.parametrize("marker", TAGS)
def test_gate2_rejection_preserves_feedback(marker, complete):
    feedback = f"Expected literal {marker} in the result"
    complete(DOUBLE_REASON + fenced(json.dumps({"approved": False, "feedback": feedback})))
    impl = inner_loop.LLMGate2Validator(base_url="http://stub.invalid/v1", model="offline")
    task = {"id": "T1", "title": "Update sample", "instruction": "Keep literal output"}
    execution = SimpleNamespace(passed=True, exit_code=0, stdout="ok", stderr="", traceback="", timed_out=False)
    generated = SimpleNamespace(files_written=["module.py"], files_skipped=[], raw_response="", error="")
    approved, message = impl.approve(task, execution, generated)
    assert not approved and not impl.last_unavailable
    assert feedback in message


@pytest.mark.parametrize(("api_key", "expected"), [
    ("fake%value", "fake%value"),
    ("fake%%value", "fake%value"),
    ("%(credential)s", "fake-key"),
    ("%(credential)s%%ref%value", "fake-key%ref%value"),
])
def test_orchestrator_preserves_credentials_and_references(tmp_path, api_key, expected):
    cfg = tmp_path / "agents.ini"
    cfg.write_text(f"[DEFAULT]\ncredential = fake-key\n[api_local]\napi_key = {api_key}\n")
    instance = main.Orchestrator.__new__(main.Orchestrator)
    instance.config = configparser.ConfigParser()
    instance.load_config(str(cfg))
    assert instance.api_key == expected


def test_ini_interpolation_stays_enabled_for_other_fields(tmp_path):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[sample]\nvalue = 50%\n")
    instance = main.Orchestrator.__new__(main.Orchestrator)
    instance.config = configparser.ConfigParser()
    instance.load_config(str(cfg))
    with pytest.raises(configparser.InterpolationSyntaxError):
        instance.config.get("sample", "value")


def test_orchestrator_defaults_an_invalid_typed_percent(tmp_path, caplog):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[loop]\nmax_iterations = 50%\n")
    instance = main.Orchestrator.__new__(main.Orchestrator)
    instance.config = configparser.ConfigParser()
    instance.load_config(str(cfg))
    assert instance.max_iterations == 3 and "max_iterations" in caplog.text


def test_preflight_reports_an_interpolated_blank(tmp_path, capsys):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[DEFAULT]\nblank =\n[auto]\nmax_tasks_per_run = %(blank)s\n")
    with pytest.raises(SystemExit) as exit_info:
        main._validate_typed_config_values(str(cfg), str(tmp_path))
    assert exit_info.value.code == 1
    assert "left blank" in capsys.readouterr().err


def test_preflight_handles_malformed_numeric_percent_without_traceback(tmp_path, capsys):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[auto]\nmax_tasks_per_run = 50%\n")
    try:
        main._validate_typed_config_values(str(cfg), str(tmp_path))
    except SystemExit as exc:
        message = capsys.readouterr().err
        assert exc.code != 0 and "max_tasks_per_run" in message and "Traceback" not in message


def test_auto_refuses_invalid_interpolation_without_starting(tmp_path, capsys):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[auto]\nmax_tasks_per_run = 50%\n")
    assert controller.run_auto("check", tmp_path, str(cfg), dry_run=True) == 1
    stderr = capsys.readouterr().err
    assert "agents.ini" in stderr and "max_tasks_per_run" in stderr
    assert "Traceback" not in stderr and not (tmp_path / ".agent").exists()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_collect_cli_reads_ini(tmp_path, encoding):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[collect]\nenabled = false\n", encoding=encoding)
    assert collect_cli.main(["--check"], str(tmp_path), str(cfg)) == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_pipeline_cold_config_reads_ini(tmp_path, encoding):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[auto]\nmax_tasks_per_run = 3\n", encoding=encoding)
    actual = pipeline._load_config(SimpleNamespace(config_path=str(cfg)))
    assert actual.getint("auto", "max_tasks_per_run") == 3


def test_validate_plan_keeps_bom_settings(tmp_path, monkeypatch):
    cfg = tmp_path / "agents.ini"
    cfg.write_text("[auto]\ntask_mode = creative\n", encoding="utf-8-sig")
    state = StateStore(tmp_path / ".agent")
    state.initialise("goal", tmp_path)
    state.upsert_task(make_task("T1", "Modify file", "Instructions"))
    seen = []
    def filter_candidates(candidates, *args, **kwargs):
        seen.append(kwargs["task_mode"])
        return candidates, []
    monkeypatch.setattr(plan_validator, "filter_candidates", filter_candidates)
    assert plan_validator.validate_plan(tmp_path, str(cfg)).checked == 1
    assert seen == ["creative"]


@pytest.mark.parametrize("bad", [False, True])
def test_skill_adapters_support_bom_and_report_invalid_bytes(tmp_path, bad):
    cfg = tmp_path / "demo.skill.ini"
    (tmp_path / "demo.md").write_text("---\nname: demo\n---\n\nA very short skill.\n")
    adapter = "[skill]\nname = demo\nsource = demo.md\nbase = code\nmin_num_ctx = 1024\n[skill.inject]\ncoder = body\n"
    cfg.write_bytes(b"[skill]\nname = \xff" if bad else adapter.encode("utf-8-sig"))
    if bad:
        with pytest.raises(loader.SkillFormatError, match="demo.skill.ini"):
            loader.load_skill(str(cfg), settings(), base_dir=tmp_path)
    else:
        assert loader.load_skill(str(cfg), settings(), base_dir=tmp_path).base == "code"
