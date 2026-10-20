"""
conftest.py — project root

Handles two things:
  1. sys.path bootstrap so `from tools.x import Y` works in every test file.
  2. Custom collector for standalone script-style test files (test_story_*.py).
     These files run all their checks at import time and call sys.exit(1) on
     failure, which crashes pytest's collection phase with INTERNALERROR.

     Fix: two cooperating hooks:
       - pytest_collect_file     → adds a ScriptTestFile node (runs the script
                                   as a subprocess, maps exit code to PASS/FAIL)
       - pytest_pycollect_makemodule → intercepts the built-in Python Module
                                   collector for the same files and returns an
                                   empty shell so the file is never imported.
"""

import os
import subprocess
import sys
import tempfile


import pytest
# conftest.py  (project root)

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
# ── 1. sys.path bootstrap ──────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep .pyc files out of the checkout: bytecode goes to the temp dir (per repo folder), for this process and its subprocesses.
_PYCACHE_PREFIX = str(Path(tempfile.gettempdir()) / ROOT.name)
sys.pycache_prefix = _PYCACHE_PREFIX
os.environ.setdefault("PYTHONPYCACHEPREFIX", _PYCACHE_PREFIX)


# Cache scan_repo(REPO_ROOT) per test session: only this exact root with no config; tmp_path trees and explicit configs always rescan.
from tools.collect import scanner as _scanner

_real_scan_repo = _scanner.scan_repo
_repo_scan_cache: dict = {}

# Share that cached scan between xdist workers through a pickle in the temp dir, keyed by a fingerprint of the tree.
_SCAN_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "contest-out",
                             "node_modules", ".venv", "venv"})


def _tree_fingerprint() -> str:
    import hashlib
    from importlib import metadata

    h = hashlib.sha256()
    h.update(sys.version.encode())
    for dist in ("tree-sitter", "tree-sitter-java"):
        try:
            h.update(f"{dist}={metadata.version(dist)}".encode())
        except metadata.PackageNotFoundError:
            h.update(f"{dist}=absent".encode())
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in _SCAN_SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)          # follows the tier symlinks
                h.update(f"{os.path.relpath(full, ROOT)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
            except OSError:
                h.update(f"{os.path.relpath(full, ROOT)}|missing\n".encode())
    return h.hexdigest()[:24]


_tree_key: list = []            # the fingerprint, once per process (1b: no mid-run edits)
_shared_values: dict = {}       # name -> what this process built or loaded


def _shared(name: str, compute):
    """`compute()` once per unchanged tree, across processes.

    `<fingerprint>.<name>.pkl` in one temp dir per checkout, one flock per
    name: N workers asking together build it once, and a worker loading one
    table never queues behind another being built. A miss, a truncated
    pickle or a cache that cannot be written is only a recompute -- the cache
    must never fail a test -- and `compute`'s own exceptions reach the caller
    exactly as they would without it. A pickle is code to load: a cache dir
    another user made, or anyone may write to, is not read at all.
    """
    if name in _shared_values:
        return _shared_values[name]
    import pickle
    try:
        import fcntl
        cache_dir = Path(tempfile.gettempdir()) / f"{ROOT.name}-scan-cache"
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = cache_dir.stat()                # another user's dir in the shared /tmp
        if st.st_uid != os.getuid() or st.st_mode & 0o002:
            raise PermissionError(f"{cache_dir} is not this user's")
        if not _tree_key:
            _tree_key.append(_tree_fingerprint())
        target = cache_dir / f"{_tree_key[0]}.{name}.pkl"
        lock = open(cache_dir / f".{name}.lock", "a+")
    except Exception:                       # non-POSIX / no temp dir: this process only
        _shared_values[name] = compute()
        return _shared_values[name]
    with lock:                              # closing the file drops the flock
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:                     # no locks here (NFS): at worst built twice
            pass
        try:
            value = pickle.loads(target.read_bytes())
        except Exception:                   # missing / truncated / stale class
            value = compute()
            try:
                tmp = target.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_bytes(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
                os.replace(tmp, target)
                _prune_shared(cache_dir)
            except Exception:
                pass
    _shared_values[name] = value
    return value


def _prune_shared(cache_dir: Path, keep: int = 3) -> None:
    """Every table of all but the `keep` newest trees goes."""
    newest: dict = {}
    for f in cache_dir.glob("*.pkl"):
        key = f.name.split(".", 1)[0]
        newest[key] = max(newest.get(key, 0.0), f.stat().st_mtime)
    for key in sorted(newest, key=newest.get, reverse=True)[keep:]:
        for f in cache_dir.glob(f"{key}.*"):
            f.unlink(missing_ok=True)


# The live tests read the repository, not what the runs left in this checkout (CC review fixes 45).
# Until HARDEN-24 B the default walk depth of 2 kept round output out by accident; at 6 a checkout
# that has run rounds scanned 10,346 modules instead of 286 (contest-out/ 9,177, claim-check-out/
# 866), main.py stopped being an entry point (a copied test imports it), five live tests failed
# and two took 15-19 minutes each. The two round-output folders are not walked, and a module git
# does not track (an examples/task*/ sandbox of run_flows.sh) is dropped; a clean checkout loses
# nothing. Without git (an exported tree) every scanned module stays.
_LIVE_SCAN_SKIP_DIRS = "contest-out, claim-check-out"


def _tracked_paths():
    """The paths git tracks in ROOT, or None when git cannot say."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True, timeout=60).stdout
    except Exception:
        return None
    return {p for p in out.decode("utf-8", "surrogateescape").split("\0") if p}


def _live_scan():
    import configparser

    config = configparser.ConfigParser()
    config["collect"] = {"skip_dirs": _LIVE_SCAN_SKIP_DIRS}
    modules = _real_scan_repo(ROOT, config=config)
    tracked = _tracked_paths()
    return modules if tracked is None else [m for m in modules if m.path in tracked]


def _scan_repo_shared():
    """`scan_repo(ROOT)` of the repository's own files, computed once per unchanged tree, across processes."""
    return _shared("scan_repo", _live_scan)


def _cached_scan_repo(root, *, config=None):
    resolved = Path(root).resolve()
    if resolved != ROOT:
        return _real_scan_repo(root, config=config)
    if config is not None:
        return _scan_root_with_config(root, config)
    if resolved not in _repo_scan_cache:
        _repo_scan_cache[resolved] = _scan_repo_shared()
    return _repo_scan_cache[resolved]


# A config scan of this tree reuses the default scan's records for the files both scans share.
_real_scan_module = _scanner.scan_module
_module_seed: dict = {}


def _seeded_scan_module(source, module_path):
    record = _module_seed.get(module_path)
    return record if record is not None else _real_scan_module(source, module_path)


def _scan_root_with_config(root, config):
    from tools.collect.lang import Language

    _module_seed.update((m.path, m) for m in _cached_scan_repo(ROOT)
                        if m.language == Language.PYTHON)
    try:
        return _real_scan_repo(root, config=config)
    finally:
        _module_seed.clear()


_scanner.scan_repo = _cached_scan_repo
_scanner.scan_module = _seeded_scan_module


# Live-tree tables derived from the cached scan, built once per session.
from tools.collect import registries as _registries
from tools.collect import test_map as _test_map

_real_build_test_map = _test_map.build_test_map
_real_build_fail_open_registry = _registries.build_fail_open_registry


def _is_the_live_scan(root, modules) -> bool:
    return (root is not None and modules is not None
            and modules is _repo_scan_cache.get(ROOT)
            and Path(root).resolve() == ROOT)


def _cached_build_test_map(root, modules):
    if not _is_the_live_scan(root, modules):
        return _real_build_test_map(root, modules)
    return dict(_shared("build_test_map", lambda: _real_build_test_map(root, modules)))


def _cached_build_fail_open_registry(modules, root=None):
    if not _is_the_live_scan(root, modules):
        return _real_build_fail_open_registry(modules, root=root)
    return list(_shared("build_fail_open_registry",
                        lambda: _real_build_fail_open_registry(modules, root=root)))


_test_map.build_test_map = _cached_build_test_map
_registries.build_fail_open_registry = _cached_build_fail_open_registry


def _share_main_call_sites() -> None:
    """1b-3's third table. `main.py` is this repo's entry point, not a package
    module, so its call-site scan is wrapped once the tests that import it
    are collected (from `pytest_collection_modifyitems`, section 5); a run
    that never imports it has nothing to wrap."""
    main = sys.modules.get("main")
    real = getattr(main, "_scan_typed_config_call_sites", None)
    if real is None or getattr(real, "_shared_on_root", False):
        return
    if Path(getattr(main, "__file__", "") or "").resolve() != ROOT / "main.py":
        return

    def _cached_typed_config_call_sites(repo_root):
        if Path(repo_root).resolve() != ROOT:
            return real(repo_root)
        return list(_shared("typed_config_call_sites", lambda: real(repo_root)))

    _cached_typed_config_call_sites._shared_on_root = True
    main._scan_typed_config_call_sites = _cached_typed_config_call_sites


# Cache gates._repo_defined_names for this repo's own tree across the session, with the same reasoning as the scan cache.
from tools.collect import gates as _gates

_real_repo_defined_names = _gates._repo_defined_names
_gates_defined_names_cache: dict = {}


def _cached_repo_defined_names(modules, root):
    resolved = Path(root).resolve()
    if resolved != ROOT:
        return _real_repo_defined_names(modules, root)
    # Key on the resolved root only: each call passes a new list, so an id()-based key never hit.
    if resolved not in _gates_defined_names_cache:
        _gates_defined_names_cache[resolved] = _real_repo_defined_names(modules, root)
    return _gates_defined_names_cache[resolved]


_gates._repo_defined_names = _cached_repo_defined_names


# CollectModel indexes are built once per live-tree fixture; the model stays a plain dataclass.
_live_indexes: dict = {}


@pytest.fixture(scope="session")
def live_collect_model():
    """The `CollectModel` class for a model of this tree that is never changed."""
    import weakref
    from tools.collect.loader import CollectModel

    class LiveCollectModel(CollectModel):
        def _live_index(self, name, build):
            slot = _live_indexes.get(id(self))
            if slot is None:
                slot = _live_indexes[id(self)] = {}
                weakref.finalize(self, _live_indexes.pop, id(self), None)
            if name not in slot:
                slot[name] = build()
            return slot[name]

        def _modules_by_path(self):
            return self._live_index("modules_by_path", super()._modules_by_path)

        def _test_paths(self):
            return self._live_index("test_paths", super()._test_paths)

    return LiveCollectModel


# ── 2a. Custom collector ───────────────────────────────────────────────────────

def _is_script_test(p: Path) -> bool:
    return p.suffix == ".py" and p.name.startswith("test_story_")


def pytest_collect_file(parent, file_path):
    """Claim every test_story_*.py file and run it as a subprocess."""
    if _is_script_test(file_path):
        return ScriptTestFile.from_parent(parent, path=file_path)


# ── 2b. Block the built-in Python Module collector for the same files ──────────

class _EmptyModule(pytest.Module):
    """Placeholder Module that collects nothing — never imports the file."""
    def collect(self):
        yield from []


def pytest_pycollect_makemodule(module_path, parent):
    """
    pytest_pycollect_makemodule is firstresult=True.
    Returning a non-None value here stops the built-in Module from being
    created, which prevents the file from being imported (and hitting sys.exit).
    """
    if _is_script_test(module_path):
        return _EmptyModule.from_parent(parent, path=module_path)


# ── 3. ScriptTestFile / ScriptTestItem ────────────────────────────────────────

class ScriptTestFile(pytest.File):
    """Represents one standalone test script as a single collectible node."""

    def collect(self):
        yield ScriptTestItem.from_parent(self, name=self.path.stem)


class ScriptTestItem(pytest.Item):
    """Runs the script in a subprocess; maps exit code to PASSED / FAILED."""

    def runtest(self):
        # Script tests time out after 300 s and are reported like a non-zero exit.
        try:
            result = subprocess.run(
                [sys.executable, str(self.fspath)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            # Decode partial output defensively: it may be None or bytes, so the timeout is reported cleanly.
            def _as_text(b):
                if isinstance(b, (bytes, bytearray)):
                    return b.decode("utf-8", "replace")
                return b or ""
            self._stdout = _as_text(exc.stdout)
            self._stderr = _as_text(exc.stderr) + "\n[TIMEOUT] script exceeded 300s"
            self._returncode = -1
            raise ScriptTestFailed(-1, self._stdout, self._stderr) from exc

        self._stdout = result.stdout
        self._stderr = result.stderr
        self._returncode = result.returncode

        if result.returncode != 0:
            raise ScriptTestFailed(result.returncode, result.stdout, result.stderr)

    def repr_failure(self, excinfo):
        exc = excinfo.value
        lines = []
        if exc.stdout:
            lines.append("--- stdout ---")
            lines.extend(exc.stdout.rstrip().splitlines())
        if exc.stderr:
            lines.append("--- stderr ---")
            lines.extend(exc.stderr.rstrip().splitlines())
        lines.append(f"--- exit code: {exc.returncode} ---")
        return "\n".join(lines)

    def reportinfo(self):
        return self.fspath, None, f"script: {self.fspath.basename}"

    def teardown(self):
        # Write captured stdout so it appears with pytest -s
        if getattr(self, "_stdout", None):
            sys.stdout.write(self._stdout)


class ScriptTestFailed(Exception):
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# Pool size: -n auto waits on sleeps more than CPU, so the floor is 4; JAN_TEST_WORKERS overrides it.
def pytest_xdist_auto_num_workers(config):
    forced = os.environ.get("JAN_TEST_WORKERS", "").strip()
    if forced.isdigit() and int(forced) > 0:
        return int(forced)
    return max(4, os.cpu_count() or 1)


# Heavy tier (tests/SLOW_TESTS.txt) is queued first under xdist, so its long tests start early.
def _heavy_tier_files() -> frozenset:
    try:
        lines = (ROOT / "tests" / "SLOW_TESTS.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(line for line in (raw.strip() for raw in lines)
                     if line and not line.startswith("#"))


def pytest_collection_modifyitems(session, config, items):
    _share_main_call_sites()
    if hasattr(config, "workerinput"):              # an xdist worker
        heavy = _heavy_tier_files()
        items.sort(key=lambda item: item.path.name not in heavy)   # stable
