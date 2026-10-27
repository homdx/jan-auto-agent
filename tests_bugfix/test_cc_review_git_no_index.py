"""Review of cc-epic-docs: the git allow-lists refuse --no-index (it reads any path on disk), and claim_diff's progress count loses no job."""

import concurrent.futures
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_diff as cd  # noqa: E402
from tools.claimcheck.anchors import PathRepoView  # noqa: E402
from tools.claimcheck.target import Target, TargetError  # noqa: E402

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    env = {**os.environ, **ENV}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True, env=env)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    return r, outside


@pytest.mark.parametrize("args", [("diff", "--no-index", "/dev/null", "{out}"),
                                  ("grep", "--no-index", "-n", "secret")])
def test_no_index_is_refused_by_the_path_view(repo, args):
    r, out = repo
    with pytest.raises(ValueError, match="--no-index: refused"):
        PathRepoView(str(r)).git(*(a.format(out=out) for a in args))


@pytest.mark.parametrize("args", [("diff", "--no-index", "/dev/null", "{out}"),
                                  ("grep", "--no-index", "-n", "secret")])
def test_no_index_is_refused_by_the_target_view(repo, tmp_path, args):
    r, out = repo
    with Target.open(str(r), "HEAD", scratch=tmp_path / "s") as t:
        with pytest.raises(TargetError, match="--no-index: refused"):
            t.view().git(*(a.format(out=out) for a in args))


def test_the_progress_count_reaches_every_job(capsys):
    voters = [f"v/m{i}" for i in range(40)]
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        vote = cd.make_vote(voters, 5, pool, lambda ref, items, run: {"votes": {}})
        assert len(vote([], "head")) == 200
    seen = sorted(int(m) for m in re.findall(r"job (\d+)/200", capsys.readouterr().err))
    assert seen == list(range(1, 201))
