"""Regression tests for scripts/merge_validations.py --csv output.

The on-screen report groups findings but cannot be sorted or pivoted. --csv
writes a spreadsheet-style pivot instead: one row per finding, one column per
reviewer holding that reviewer's verdict, with severity / agreement / vote-tally
columns in front. Reading a row left to right shows who said what.
"""
import csv
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "merge_validations",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "merge_validations.py",
)
merge_validations = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(merge_validations)


def _row(reviewer, verdict, severity, **kw):
    r = {c: "" for c in merge_validations.COLUMNS}
    r.update(task_id="AUTO-T1", file="tools/x.py", symbol="C.m",
             verdict=verdict, severity=severity)
    r.update(kw)
    r["_reviewer"] = reviewer
    return r


def _write(tmp_path, rows):
    groups = {}
    for r in rows:
        groups.setdefault(merge_validations.key_of(r), []).append(r)
    reviewers = sorted({r["_reviewer"] for r in rows})

    def worst_sev(rs):
        sevs = [(r.get("severity") or "").strip().upper() for r in rs]
        return sorted(sevs, key=lambda s: merge_validations.SEVERITY_ORDER.get(s, 5))[0]

    out = tmp_path / "merged.csv"
    merge_validations.write_merged_csv(str(out), groups, reviewers, worst_sev)
    return list(csv.DictReader(out.open()))


def test_one_row_per_finding_with_reviewer_columns(tmp_path):
    rows = _write(tmp_path, [
        _row("glm", "CONFIRMED", "MEDIUM"),
        _row("hy3", "FALSE_POSITIVE", "NONE"),
        _row("kilo", "CONFIRMED", "LOW", file="tools/y.py", symbol="D.n"),
    ])
    assert len(rows) == 2                                   # two findings, not three judgements
    head = list(rows[0].keys())
    assert head[:9] == ["finding", "severity", "agreement", "reviewers",
                        "confirmed", "dismissed", "fixed", "task_ids", "title"]
    assert head[9:] == ["glm", "hy3", "kilo"]               # every reviewer is a column
    r = next(r for r in rows if r["finding"] == "tools/x.py::C.m")
    assert (r["glm"], r["hy3"], r["kilo"]) == ("CONFIRMED", "FALSE_POSITIVE", "")


def test_agreement_and_tallies(tmp_path):
    rows = _write(tmp_path, [
        _row("glm", "CONFIRMED", "MEDIUM"),
        _row("hy3", "FALSE_POSITIVE", "NONE"),
        _row("glm", "CONFIRMED", "HIGH", file="tools/z.py", symbol="E.o"),
        _row("hy3", "CONFIRMED", "MEDIUM", file="tools/z.py", symbol="E.o"),
        _row("kilo", "CONFIRMED", "LOW", file="tools/y.py", symbol="D.n"),
    ])
    by = {r["finding"]: r for r in rows}
    assert by["tools/x.py::C.m"]["agreement"] == "SPLIT"
    assert by["tools/x.py::C.m"]["confirmed"] == "1"
    assert by["tools/x.py::C.m"]["dismissed"] == "1"
    assert by["tools/z.py::E.o"]["agreement"] == "UNANIMOUS"
    assert by["tools/y.py::D.n"]["agreement"] == "SOLO"


def test_self_disagreement_is_visible_and_explains_the_tally(tmp_path):
    """One reviewer, one finding, two list task-ids, two different verdicts:
    the reviewer column shows 'A|B' and the tally exceeds `reviewers`."""
    rows = _write(tmp_path, [
        _row("agnes", "ALREADY_FIXED", "MEDIUM", task_id="AUTO-T18"),
        _row("agnes", "OUT_OF_SCOPE", "MEDIUM", task_id="AUTO-T19"),
        _row("glm", "FALSE_POSITIVE", "MEDIUM", task_id="AUTO-T18"),
    ])
    r = rows[0]
    assert r["reviewers"] == "2"
    assert r["agnes"] == "ALREADY_FIXED|OUT_OF_SCOPE"
    assert r["task_ids"] == "AUTO-T18 AUTO-T19"
    assert int(r["dismissed"]) + int(r["fixed"]) == 3       # 3 judgements from 2 reviewers


def test_worst_severity_and_row_ordering(tmp_path):
    rows = _write(tmp_path, [
        _row("glm", "FALSE_POSITIVE", "NONE", file="tools/a.py", symbol="A"),
        _row("hy3", "CONFIRMED", "CRITICAL", file="tools/b.py", symbol="B"),
        _row("glm", "CONFIRMED", "MEDIUM", file="tools/c.py", symbol="C"),
        _row("hy3", "FALSE_POSITIVE", "MEDIUM", file="tools/c.py", symbol="C"),
    ])
    assert rows[0]["finding"] == "tools/b.py::B"            # CRITICAL first
    assert rows[0]["severity"] == "CRITICAL"
    # among equal severity, SPLIT sorts before a lone dismissal
    assert [r["finding"] for r in rows[1:]] == ["tools/c.py::C", "tools/a.py::A"]
    assert rows[1]["agreement"] == "SPLIT"


def test_dismissed_bucket_covers_all_three_dismissing_verdicts(tmp_path):
    rows = _write(tmp_path, [
        _row("a", "FALSE_POSITIVE", "NONE"),
        _row("b", "OUT_OF_SCOPE", "NONE"),
        _row("c", "UNVERIFIABLE", "NONE"),
    ])
    assert rows[0]["dismissed"] == "3"
    assert rows[0]["agreement"] == "SPLIT"                  # three distinct verdicts
