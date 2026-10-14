"""tests_bugfix/test_collect_loader_is_safe_repo_root_301.py

Bug: `CollectModel._already_safe_index` defaulted its `root` to
`self.collect_dir` — the artifact directory — while the index joins
repo-relative paths onto it. A query with no `root=` of its own therefore
looked for `pkg/widget.py` inside `.collect/`, found nothing, and answered
"unknown" for a location a seed contract covers. The repo root is the one
the model was loaded for and is available at load time; it was simply never
stored.

Field report: HARDEN-4 D6 (latent: the in-repo callers all pass the real
root, so nothing hit it — but a consumer asking plainly got a wrong
"unknown").
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import FunctionRecord, ModuleRecord
from tools.collect.registries import ContractRecord

WIDGET_SOURCE = (
    "class Widget:\n"
    "    def save(self):\n"
    "        return 1\n"
    "\n"
    "    def other(self):\n"
    "        return 2\n"
)


def _model(root: Path) -> CollectModel:
    modules = (
        ModuleRecord(
            path="pkg/widget.py",
            public_symbols=(
                FunctionRecord(
                    qualname="pkg/widget.py:Widget",
                    module="pkg/widget.py",
                    lineno=1,
                    signature="class Widget",
                ),
            ),
        ),
    )
    contracts = (
        ContractRecord(
            name="widget_atomic_save",
            kind="seed",
            known_edge="pkg/widget.py:Widget",
            description="Widget.save writes through atomically.",
        ),
    )
    return CollectModel(
        status=STATUS_FRESH,
        modules=modules,
        contracts=contracts,
        repo_root=root,
    )


def _repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "widget.py").write_text(WIDGET_SOURCE, encoding="utf-8")
    return tmp_path


def test_is_safe_without_root_answers_as_it_does_with_the_repo_root(tmp_path):
    model = _model(_repo(tmp_path))
    with_root = model.is_safe("pkg/widget.py:2", root=tmp_path)
    without_root = model.is_safe("pkg/widget.py:2")
    assert with_root.safe is True
    assert without_root == with_root


def test_the_default_root_is_the_repo_root_not_the_artifact_directory(tmp_path):
    model = _model(_repo(tmp_path))
    # The artifact directory holds no module source, so narrowing against it
    # answers "unknown" — the wrong answer the default used to give.
    assert model.is_safe("pkg/widget.py:2", root=model.collect_dir or tmp_path / ".collect").reason != "contract"
    assert model.is_safe("pkg/widget.py:2").reason == "contract"


def test_an_unrelated_method_of_the_same_class_is_still_not_safe(tmp_path):
    model = _model(_repo(tmp_path))
    assert model.is_safe("pkg/widget.py:5").reason != "contract"


def test_an_absent_model_still_answers_unknown(tmp_path):
    from tools.collect.loader import STATUS_ABSENT

    model = CollectModel(status=STATUS_ABSENT, repo_root=_repo(tmp_path))
    answer = model.is_safe("pkg/widget.py:2")
    assert answer.safe is False
    assert answer.reason == "unknown"
