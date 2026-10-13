"""scripts/gate1_vote_layer2.py run as a script from another directory can import the repo's tools package."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_script_puts_repo_root_on_sys_path(tmp_path):
    code = (
        "import runpy, sys\n"
        f"runpy.run_path({str(ROOT / 'scripts' / 'gate1_vote_layer2.py')!r}, run_name='not_main')\n"
        "from tools.collect import manifest\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
