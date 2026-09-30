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

# ── 1a. Keep .pyc cache out of the checkout ─────────────────────────────────────
#
# Without this, every module pytest imports (tools/*, tests/*, and whatever
# test_story_*.py's subprocess imports) writes its .pyc next to the source
# inside this checkout. That's `git status` noise (masked only by
# .gitignore) and, on repos checked out on a slow/networked filesystem,
# real per-file write overhead on every run. Redirect both this process and
# any subprocess it spawns (test_story_*.py scripts run via
# `python main.py ...`, ScriptTestItem.runtest above) to a prefix under the
# system temp dir, keyed by this repo's folder name so it doesn't collide
# with a sibling checkout.
_PYCACHE_PREFIX = str(Path(tempfile.gettempdir()) / ROOT.name)
sys.pycache_prefix = _PYCACHE_PREFIX
os.environ.setdefault("PYTHONPYCACHEPREFIX", _PYCACHE_PREFIX)


# ── 1b. Cache scan_repo(REPO_ROOT) across the test session ─────────────────────
#
# ~30 tests across tests/test_collect_*.py call scan_repo(REPO_ROOT) (this
# repo's own source tree) with no config= override, each re-walking and
# re-parsing every file from scratch -- 2-8s per call, ~100s+ of the
# suite's wall time in aggregate. That specific call is safe to cache:
# this checked-out repo doesn't change mid-run, and scan_repo has no side
# effects (ModuleRecord is a frozen dataclass of tuples; verified no test
# or library call in this suite mutates the returned list in place).
#
# The cache is scoped tightly on purpose -- a blanket
# functools.lru_cache(scan_repo) was tried and reverted, because it broke
# two other things: (1) ConfigParser isn't hashable, so any call passing
# config=<a real ConfigParser> (e.g. tools/collect/loader.py's staleness
# path) raised TypeError; (2) several tests scan a tmp_path repo, mutate
# a file, and scan the *same* tmp_path again expecting the change to show
# up -- caching that call would silently return stale results and defeat
# the test. So this only ever caches root == this repo's own ROOT with
# config is None; every other call (tmp_path roots, explicit configs)
# goes straight to the real, uncached scan_repo (an explicit config on
# ROOT itself reuses the default scan's per-file records, 1b-4).
#
# Only this test session's imported reference is wrapped -- the on-disk
# tools/collect/scanner.py is untouched, so `collect` CLI runs are unaffected.
from tools.collect import scanner as _scanner

_real_scan_repo = _scanner.scan_repo
_repo_scan_cache: dict = {}

# ── 1b-2. ...and share that one scan between the xdist workers ────────────────
#
# The per-process cache above is per *process*: under `-n N` every worker
# re-walks and re-parses the whole tree once (~9 s on a quiet core, several
# times that on a loaded one), so a run pays N scans where it needs one --
# and with `--dist=loadgroup` a file's tests scatter over all N workers, so
# no worker can be spared it. The first worker to need the scan now writes it
# to a pickle under the system temp dir and the others load it (0.04 s).
#
# The cache is only ever a *hit* when the scan's inputs are provably
# unchanged, so it cannot serve a stale result (the failure mode the block
# above warns about): the key is a fingerprint of (relpath, size, mtime_ns)
# for every file in the tree outside .git and the caches, plus the
# interpreter and the optional Java-parser versions, which change what
# `scan_repo` returns. Any edit -- source or test -- is a new key and a fresh
# scan. A flock serialises the writers so N workers starting together do one
# scan, not N; an unreadable or truncated pickle is treated as a miss.
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


def _scan_repo_shared(config=None):
    """`scan_repo(ROOT)` computed once per unchanged tree, across processes."""
    return _shared("scan_repo", lambda: _real_scan_repo(ROOT, config=config))


def _cached_scan_repo(root, *, config=None):
    resolved = Path(root).resolve()
    if resolved != ROOT:
        return _real_scan_repo(root, config=config)
    if config is not None:
        return _scan_root_with_config(root, config)
    if resolved not in _repo_scan_cache:
        _repo_scan_cache[resolved] = _scan_repo_shared(config=config)
    return _repo_scan_cache[resolved]


# ── 1b-4. ...and a config scan of this tree reuses the default scan's files ───
#
# A scan of ROOT with an explicit config (test_collect_config_languages reads
# the shipped agents.ini) walks its own file set -- the config's languages and
# limits decide which files -- but every Python file it shares with the
# default scan gets the same record: `scan_module(source, path)` is a pure
# function of the file's text and path, and the text is the tree 1b pins. So
# for the length of that one scan `scan_module` answers those files from the
# default scan and parses only the rest -- the one uncached ~7 s scan of the
# suite (~20 s under `-n 8`) down to the handful of files the default scan
# does not have. The walk, the language filters and every record the config
# scan adds or drops are still the real scan_repo's. A test that patches
# `scan_module` itself replaces this for its own duration.
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


# ── 1b-3. ...and the tables the live-tree tests derive from that scan ─────────
#
# The scan is not the only answer this suite rebuilds from scratch file after
# file: nine files' live fixtures build `build_test_map(ROOT, scan)`, five
# build `build_fail_open_registry(scan, root=ROOT)`, and every in-process
# `main.main()` with a config re-parses tools/ for its typed-config call sites
# (`_scan_typed_config_call_sites`, 33 times a full run) -- 0.5-1 s each on a
# quiet core, two to three times that under `-n 8`, the same answer every
# time. They go through 1b-2's per-tree pickle: one worker builds, the rest
# load. Scoped like 1b: only ROOT, only the session's own scan list (checked
# by identity -- a filtered or rebuilt list is another question and goes to
# the real function), and every caller gets its own copy of the container.
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


def pytest_collection_modifyitems(session, config, items):
    """1b-3's third table. `main.py` is this repo's entry point, not a package
    module, so its call-site scan is wrapped once the tests that import it
    are collected; a run that never imports it has nothing to wrap."""
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


# ── 1c. Cache gates._repo_defined_names(modules, ROOT) across the session ──────
#
# tests/test_collect_gates.py builds GATES against this repo's own real
# scan several times over (the session-scoped `gates_entries` fixture,
# plus test_bad_module_citation_raises / test_bad_parser_citation_raises,
# each with their own one-entry seed). Every one of those calls, after
# the gates.py fix that turned build_gates_map's per-seed-entry re-parse
# into a single per-call `_repo_defined_names` pass, still does that one
# full-repo ast.parse pass again from scratch -- ~3-4s each, on the same
# modules list (scan_repo(ROOT) above already returns the same cached
# list object every time) and the same root. Same safe-to-cache
# reasoning as 1b: this checked-out repo doesn't change mid-run, and
# `_repo_defined_names` returns a fresh, unshared set with no side
# effects. Scoped the same way -- only root == this repo's own ROOT is
# cached; a tmp_path repo (e.g. test_foreign_repo_skips_seed_instead_of_raising)
# always goes to the real, uncached function.
from tools.collect import gates as _gates

_real_repo_defined_names = _gates._repo_defined_names
_gates_defined_names_cache: dict = {}


def _cached_repo_defined_names(modules, root):
    resolved = Path(root).resolve()
    if resolved != ROOT:
        return _real_repo_defined_names(modules, root)
    # Keyed on `resolved` alone (not the `modules` object's identity):
    # `build_gates_map` passes its own `list(modules)` copy on every
    # call, a fresh object each time even for the same underlying scan,
    # so an id()-based key never hit. Safe the same way 1b's scan_repo
    # cache is: this repo's own tree doesn't change mid-run, so every
    # call for `resolved == ROOT` answers the same question.
    if resolved not in _gates_defined_names_cache:
        _gates_defined_names_cache[resolved] = _real_repo_defined_names(modules, root)
    return _gates_defined_names_cache[resolved]


_gates._repo_defined_names = _cached_repo_defined_names


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
        # AUTO-FIX (medium-priority audit, DeepSeek-plan finding): no
        # timeout= was passed to subprocess.run — a hung test_story_*.py
        # script (an infinite loop, a stuck network call inside the
        # script) blocked the entire test suite indefinitely with no way
        # to recover short of killing the process manually. 300s covers
        # every legitimate script test currently in the suite with room to
        # spare; TimeoutExpired is reported the same way a non-zero exit
        # already is, via ScriptTestFailed, so repr_failure's existing
        # stdout/stderr/exit-code formatting handles it without changes.
        try:
            result = subprocess.run(
                [sys.executable, str(self.fspath)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            # BUGFIX: with text=True, TimeoutExpired.stdout/stderr are
            # documented as str — but can still be None (no output captured
            # before the kill) or, on Python builds older than 3.7.10, bytes
            # on a partial-read boundary. Decode defensively so a slow
            # script is reported as a clean timeout instead of crashing the
            # whole pytest run with an uncaught TypeError (str + bytes).
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


# ── 4. `-n auto` sizes the pool for waiting, not for CPUs ──────────────────────
#
# pytest.ini asks for `-n auto`, which xdist resolves to the CPU count. This
# suite is not CPU-bound: the contest tests deliberately sit out real silence
# windows (a 3 s `idle_event_timeout_sec`, FL-1, kept wide so a loaded box does
# not flake), and ~40 % of the wall time of a run is a process asleep. With one
# worker per core those sleeps queue up behind each other; with more workers
# than cores they overlap. Measured on ONE core, full suite: 580 s at -n 0 /
# `-n auto`, 353 s at -n 6 -- no flake attributable to the load.
#
# The floor is 4 (the hook only affects `-n auto`; an explicit `-n N` or
# `-n 0` is honoured exactly). JAN_TEST_WORKERS overrides it for a machine
# that wants fewer (a shared CI runner) or more.
def pytest_xdist_auto_num_workers(config):
    forced = os.environ.get("JAN_TEST_WORKERS", "").strip()
    if forced.isdigit() and int(forced) > 0:
        return int(forced)
    return max(4, os.cpu_count() or 1)
