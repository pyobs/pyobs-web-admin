import io
import json
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

_LOG_LEVEL_RE = re.compile(r'\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]')

import psutil
import requests
import yaml
from django.conf import settings
from packaging.version import InvalidVersion, Version
from ruamel.yaml import YAML as _RuamelYAML

from modules.pyobs_config import pre_process_yaml

# Used only to serialize a *fresh* acl: block for _replace_local_acl_block -- ruamel's
# round-trip dumper reads more like hand-written YAML (indented block sequences, minimal
# quoting) than plain pyyaml's default output. Not used for reading/round-tripping a whole
# config file: the raw file can contain bare {include ...} lines that aren't valid
# standalone YAML (see pyobs_config.pre_process_yaml), so it's never parsed generically.
_ACL_YAML = _RuamelYAML()
_ACL_YAML.indent(mapping=2, sequence=4, offset=2)
_ACL_YAML.default_flow_style = False


def _config_dir() -> Path:
    if getattr(settings, "PYOBS_CONFIG_GIT_ENABLED", False):
        subpath = _git_subpath()
        if subpath:
            repo_dir = _git_repo_dir()
            candidate = repo_dir / subpath
            if candidate.exists():
                return candidate
    return Path(settings.PYOBS_CONFIG_DIR)


def _log_dir() -> Path:
    return Path(settings.PYOBS_LOG_DIR)


def _run_dir() -> Path:
    return Path(settings.PYOBS_RUN_DIR)


def _pyobs_exec() -> str:
    return settings.PYOBS_EXEC


def _git_enabled() -> bool:
    return getattr(settings, "PYOBS_CONFIG_GIT_ENABLED", False)


def _git_root() -> Path | None:
    """Return the explicit git root, or None if not set."""
    root = getattr(settings, "PYOBS_CONFIG_GIT_ROOT", "")
    return Path(root) if root else None


def _git_config_ok() -> tuple[bool, str]:
    """Validate that PYOBS_CONFIG_DIR resolves to a path inside the configured git root.

    Returns (success, error_message). If git is disabled the check is a no-op.
    """
    if not _git_enabled():
        return True, ""

    link_path = Path(settings.PYOBS_CONFIG_DIR)
    repo_dir = _git_repo_dir()

    # Symlink: verify it points inside the repo.
    if link_path.is_symlink():
        resolved = link_path.resolve()
        if resolved.is_relative_to(repo_dir.resolve()):
            return True, ""
        return False, (
            f"PYOBS_CONFIG_DIR ({link_path}) is a symlink pointing to "
            f"{resolved}, which is not inside PYOBS_CONFIG_GIT_ROOT ({repo_dir}). "
            "Configuration files must reside within the git repository tree."
        )

    # No symlink and path doesn't exist: pre-clone state. The clone + _ensure_symlink() will
    # create the proper symlink. Allow through so the admin can reach the "Clone" button.
    if not link_path.exists():
        return True, ""

    # Regular directory (legacy / pre-symlink setup): allow through. The existing dir
    # is where pyobs reads configs, and it works fine. Symlink migration is optional.
    return True, ""


def _git_subpath() -> str:
    return getattr(settings, "PYOBS_CONFIG_GIT_SUBPATH", "")


def _repo_name() -> str:
    """Extract the repo name from PYOBS_CONFIG_GIT_REPO.

    Strips trailing ".git" and takes the last path segment.
    Examples:
        "https://github.com/pyobs/pyobs-config.git" -> "pyobs-config"
        "git@github.com:pyobs/pyobs-config.git" -> "pyobs-config"
        "/absolute/path/to/my-config" -> "my-config"
    """
    repo = getattr(settings, "PYOBS_CONFIG_GIT_REPO", "")
    name = repo.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    return name.rsplit("/", 1)[-1] or name.rsplit(":", 1)[-1]


def _ensure_symlink() -> tuple[bool, str]:
    """Create (or verify) the symlink from PYOBS_CONFIG_DIR to the repo's config subpath.

    Idempotent: no-op if the symlink already exists with the correct target.
    Returns (success, message).
    """
    if not _git_enabled():
        return True, ""

    link_path = Path(settings.PYOBS_CONFIG_DIR)
    subpath = _git_subpath()
    if not subpath:
        return False, "Cannot create symlink: PYOBS_CONFIG_GIT_SUBPATH is not set"

    repo = _git_repo_dir()
    target = repo / subpath

    if not target.exists():
        return False, (
            f"Symlink target does not exist yet: {target}. "
            "Run git clone first, or create the symlink manually."
        )

    if link_path.is_symlink():
        if link_path.resolve() == target.resolve():
            return True, "Symlink already correct"
        return False, (
            f"PYOBS_CONFIG_DIR ({link_path}) is a symlink but points to "
            f"{link_path.resolve()}, expected {target}"
        )

    if link_path.exists():
        return False, (
            f"PYOBS_CONFIG_DIR exists but is not a symlink: {link_path}. "
            "Remove or rename it before cloning."
        )

    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target)
    return True, f"Created symlink {link_path} -> {target}"


def _git_repo_dir() -> Path:
    """Return the directory where .git lives.

    Priority:
    1. Explicit PYOBS_CONFIG_GIT_ROOT when set.
    2. Derived from PYOBS_CONFIG_GIT_SOURCE_DIR / _repo_name().
    3. Fallback: derives from PYOBS_CONFIG_DIR and PYOBS_CONFIG_GIT_SUBPATH.
    """
    explicit = _git_root()
    if explicit:
        return explicit
    if _git_enabled():
        source_dir = getattr(settings, "PYOBS_CONFIG_GIT_SOURCE_DIR", "")
        if source_dir:
            return Path(source_dir) / _repo_name()
    subpath = _git_subpath()
    if subpath:
        parts = Path(subpath).parts
        return Path(settings.PYOBS_CONFIG_DIR).parents[len(parts) - 1]
    return Path(settings.PYOBS_CONFIG_DIR)


def _git_run(args: list[str]) -> tuple[bool, str]:
    """Run a git command inside the repository root.

    Returns (success, output). If git is not enabled returns (True, "").
    """
    if not _git_enabled():
        return True, ""
    ok, msg = _git_config_ok()
    if not ok:
        return False, msg
    repo_dir = _git_repo_dir()
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, "git executable not found"
    except subprocess.TimeoutExpired:
        return False, "git command timed out (60s)"


def _git_auto_stage() -> None:
    """Best-effort stage all changes after a config write. Never raises."""
    if not _git_enabled():
        return
    try:
        subprocess.run(
            ["git", "add", "-A", str(_config_dir())],
            cwd=_git_repo_dir(),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        pass


def _log_level() -> str:
    return getattr(settings, "PYOBS_LOG_LEVEL", "info")


_PYOBSD_CONFIG_CANDIDATES = [
    os.path.expanduser(os.path.join("~", ".config", "pyobs.yaml")),
    os.path.join("/", "etc", "pyobs.yaml"),
    os.path.join("/", "opt", "pyobs", "storage", "pyobs.yaml"),
]


def _pyobsd_config() -> dict:
    """Reads pyobsd's own global config file, if one exists -- same candidate paths and
    "first one found wins" order as pyobs-core's own CLI._load_config
    (pyobs-core/pyobs/cli/_cli.py), so this reads exactly the file pyobsd itself would.
    Returns just the "pyobsd" section (PyobsDaemonCLI.CONFIG_SECTION in
    pyobs-core/pyobs/cli/pyobsd.py), {} if no candidate exists or the file doesn't have that
    section. A malformed file is treated the same as a missing one -- this is a convenience
    auto-detection, not something that should ever crash a page load.
    """
    for path in _PYOBSD_CONFIG_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f)
            except (OSError, yaml.YAMLError):
                return {}
            return (cfg or {}).get("pyobsd") or {}
    return {}


def _log_backend() -> str:
    """"file" or "journald". An explicit PYOBS_LOG_BACKEND setting always wins (admin
    override); otherwise auto-detected from pyobsd's own config (_pyobsd_config): "journald"
    if its syslog key is true, "file" otherwise -- matching pyobsd's own --syslog default of
    False. Auto-detecting instead of requiring this configured a second time removes the
    risk of PYOBS_LOG_BACKEND silently drifting out of sync with what pyobsd actually starts
    modules with -- see journald-logs.md."""
    configured = getattr(settings, "PYOBS_LOG_BACKEND", None)
    if configured:
        return configured
    return "journald" if _pyobsd_config().get("syslog") else "file"


# journald PRIORITY -> pyobs log level. Not the naively-expected {2: CRITICAL, ...} --
# logging.CRITICAL and logging.FATAL are the same int (50) in Python's logging module, so
# logging_journald.JournaldLogHandler.LEVELS's dict literal silently collapses to
# LEVELS[50] == 0, not 2. Verified live against a real emitted CRITICAL record -- see
# journald-logs.md, Design, for the full trail. 2/1/5 never occur in practice.
_JOURNALD_PRIORITY_TO_LEVEL = {0: "CRITICAL", 3: "ERROR", 4: "WARNING", 6: "INFO", 7: "DEBUG"}


_is_module_name_re = re.compile(r"^[a-zA-Z0-9_-]+$")


def _is_valid_module_name(name: str) -> bool:
    return bool(_is_module_name_re.match(name))


def validate_name(name: str) -> None:
    if not _is_valid_module_name(name):
        raise ValueError(f"Invalid module name: {name!r}")


def validate_shared_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        raise ValueError(f"Invalid shared config name: {name!r}")


def _active_name(name: str) -> str:
    """Strip a leading underscore, which marks a module as disabled.

    PID and log files are named after the "active" form of a module, so that
    toggling a module between enabled/disabled (by adding/removing the leading
    underscore on its config file) does not change its PID/log file names.
    """
    return name[1:] if name.startswith("_") else name


def list_shared_configs() -> list[str]:
    d = _config_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.shared.yaml"))


def list_modules() -> list[str]:
    d = _config_dir()
    if not d.exists():
        return []
    # exclude *.shared.yaml (shared config fragments, not runnable modules)
    return sorted(p.stem for p in d.glob("*.yaml") if not p.name.endswith(".shared.yaml"))


# ── Package management ───────────────────────────────────────────────────────

def _pip_exec() -> str:
    """pip from the same environment PYOBS_EXEC runs pyobs in (e.g. PYOBS_EXEC
    "/opt/pyobs/venv/bin/pyobs" -> "/opt/pyobs/venv/bin/pip"), so installed versions and
    upgrades reflect what pyobs itself actually imports -- not whatever environment
    pyobs-web-admin happens to run in. Falls back to a bare "pip" (PATH lookup) when
    PYOBS_EXEC has no directory component (the settings.py default, "pyobs") or no sibling
    pip exists there.
    """
    d = os.path.dirname(_pyobs_exec())
    if d:
        pip_path = os.path.join(d, "pip")
        if os.path.exists(pip_path):
            return pip_path
    return "pip"


# Bare "name" or "name[extras]" (PyPI-resolved), optionally followed by a PEP 508 direct
# URL reference -- "name[extras] @ <url>" -- for a package that isn't on PyPI at all
# (e.g. a git-hosted driver: "pyobs-iagvt[gui] @ git+https://gitlab.example.org/...").
# Group 3 is the URL, used by _managed_package_specs to flag the entry as VCS-installed.
_PACKAGE_SPEC_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(\[[^\]]*\])?(?:\s*@\s*(\S+))?$")


def _normalize_package_name(name: str) -> str:
    """PEP 503 normalization -- lowercase, runs of "-._" collapsed to a single "-" -- so
    "pyobs-core", "pyobs_core", and "Pyobs.Core" all compare equal, the same as pip/PyPI
    themselves treat package names as equivalent regardless of separator/casing. Used to
    match a bare name from `pip list` (list_pyobs_packages) against a name parsed out of
    settings.PYOBS_MANAGED_PACKAGES (_managed_package_specs), which an operator could have
    spelled either way.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


class _ManagedSpec(NamedTuple):
    spec: str  # full entry, passed to pip as-is
    is_vcs: bool  # True for a PEP 508 direct URL reference (git+, http(s) tarball, etc.) --
    # such a package isn't published on PyPI, so it has no "latest version" to look up there.


def _managed_package_specs() -> dict[str, _ManagedSpec]:
    """Parses settings.PYOBS_MANAGED_PACKAGES (e.g. ["pyobs-core[full]", "my-custom-driver",
    "pyobs-iagvt[gui] @ git+https://gitlab.example.org/iagvt/pyobs-iagvt.git"]) into
    {normalized bare name: _ManagedSpec}. See that setting's own settings.py comment for
    what it's for; both list_pyobs_packages and update_package consult this, not just one of
    them, so a name only shows up on the Packages page if it can also actually be updated
    through it, and vice versa.

    Malformed entries are skipped rather than raising -- a typo in local_settings.py
    shouldn't be able to break the whole Packages page.
    """
    specs: dict[str, _ManagedSpec] = {}
    for entry in getattr(settings, "PYOBS_MANAGED_PACKAGES", []):
        m = _PACKAGE_SPEC_RE.match(entry.strip())
        if not m:
            continue
        specs[_normalize_package_name(m.group(1))] = _ManagedSpec(entry.strip(), is_vcs=m.group(3) is not None)
    return specs


def _install_spec_for(name: str) -> str:
    """The exact string update_package passes to `pip install --upgrade` for a managed
    package -- name itself, unless PYOBS_MANAGED_PACKAGES configures a fuller spec for it
    (e.g. "pyobs-core[full]" or a git URL), in which case that's used instead. Without this,
    a package originally installed with an extra would silently lose it on every future
    upgrade, since pip itself never records anywhere which extra (if any) an install
    originally requested -- confirmed by inspecting a real installed distribution's own
    dist-info: METADATA lists which extras a package *offers* (Provides-Extra), never which
    one was *used*. A git-installed package would fare even worse without this: falling back
    to the bare name would have pip try to resolve it against PyPI instead, which either
    fails outright (not published there) or silently installs an unrelated same-named
    package.
    """
    spec = _managed_package_specs().get(_normalize_package_name(name))
    return spec.spec if spec else name


def _is_vcs_managed(name: str) -> bool:
    """Whether `name` is a PYOBS_MANAGED_PACKAGES entry with a PEP 508 direct URL reference
    (e.g. a git+ URL) rather than a plain PyPI-resolved name -- such a package has no PyPI
    release history, so get_package_overview looks at its git remote instead (see
    _vcs_update_status) rather than reporting a spurious "unknown"/mismatched PyPI result.
    """
    spec = _managed_package_specs().get(_normalize_package_name(name))
    return spec is not None and spec.is_vcs


def _python_exec() -> str:
    """python from the same environment as _pip_exec (sibling binary in the same bin/ dir) --
    needed to introspect an installed distribution's own PEP 610 direct_url.json via
    importlib.metadata (see _vcs_direct_url_info), which no pip subcommand surfaces directly.
    """
    d = os.path.dirname(_pyobs_exec())
    if d:
        python_path = os.path.join(d, "python")
        if os.path.exists(python_path):
            return python_path
    return "python3"


def _vcs_direct_url_info(name: str) -> dict | None:
    """The PEP 610 direct_url.json pip recorded for `name` at install time, read via
    importlib.metadata run through pyobs's own environment (_python_exec) -- not this app's
    own interpreter, which may be a completely different environment. Returns None if the
    package isn't installed there, wasn't installed from git, or the metadata can't be read
    (e.g. an old pip that predates direct_url.json).
    """
    script = (
        "import importlib.metadata, sys\n"
        "try:\n"
        "    print(importlib.metadata.distribution(sys.argv[1]).read_text('direct_url.json') or '')\n"
        "except importlib.metadata.PackageNotFoundError:\n"
        "    pass\n"
    )
    try:
        result = subprocess.run(
            [_python_exec(), "-c", script, name],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    vcs_info = data.get("vcs_info")
    if not vcs_info or vcs_info.get("vcs") != "git" or not vcs_info.get("commit_id"):
        return None
    return {"url": data.get("url", ""), "ref": vcs_info.get("requested_revision"), "commit_id": vcs_info["commit_id"]}


def _git_remote_commit(url: str, ref: str | None) -> str | None:
    """The commit SHA `ref` (or the remote's default branch, if no ref was pinned at install
    time) currently points to on `url`, via `git ls-remote` -- doesn't require cloning the
    repo just to check whether a newer commit exists. None on any failure (unreachable remote,
    stale ref, git missing, timeout): same "never fail the whole page over one lookup" policy
    as _pypi_latest_version.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref or "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _vcs_update_status(name: str) -> dict:
    """Compares the commit `name` was installed at (from direct_url.json) against its git
    remote's current commit for the same ref. installed_commit/remote_commit are None (and
    update_available False) whenever either half of that comparison isn't available -- e.g.
    an old pip with no direct_url.json, or an unreachable remote -- rather than guessing.
    """
    info = _vcs_direct_url_info(name)
    if not info:
        return {"ref": None, "installed_commit": None, "remote_commit": None, "update_available": False}
    remote_commit = _git_remote_commit(info["url"], info["ref"])
    return {
        "ref": info["ref"],
        "installed_commit": info["commit_id"],
        "remote_commit": remote_commit,
        "update_available": remote_commit is not None and remote_commit != info["commit_id"],
    }


def list_pyobs_packages() -> list[dict]:
    """Installed pyobs-* packages (name + version), via `pip list --format=json` rather than
    importlib.metadata -- pyobs-web-admin itself may run in a different environment than
    pyobs (see _pip_exec), so introspecting its own imports wouldn't reflect what pyobs
    actually has installed. Also includes any package -- pyobs-prefixed or not -- listed in
    settings.PYOBS_MANAGED_PACKAGES, but only if it's actually installed: that setting can
    extend which installed packages are shown/managed, never invent an entry for one that
    isn't really there.
    """
    try:
        result = subprocess.run(
            [_pip_exec(), "list", "--format=json"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    try:
        installed = json.loads(result.stdout)
    except ValueError:
        return []
    managed = _managed_package_specs()
    return sorted(
        (
            {"name": p["name"], "version": p["version"]}
            for p in installed
            if p["name"].lower().startswith("pyobs") or _normalize_package_name(p["name"]) in managed
        ),
        key=lambda p: p["name"].lower(),
    )


_PYOBS_CORE_VERSION_CACHE_TTL = 60  # seconds
_pyobs_core_version_cache: tuple[float, Version | None] | None = None


def pyobs_core_version() -> Version | None:
    """Installed pyobs-core version in the same environment PYOBS_EXEC runs pyobs in (see
    _pip_exec) -- the version actually producing the logs/behavior this app has to interpret,
    which can differ from host to host and drift out of step with pyobs-web-admin's own
    release. None if pyobs-core isn't listed there or its version string doesn't parse.

    Cached for _PYOBS_CORE_VERSION_CACHE_TTL: this sits on the journald log-fetch hot path
    (polled every few seconds per open browser tab), and list_pyobs_packages() shells out to
    pip each call -- far too slow to redo on every request. Tests that need a fresh read
    should reset the module-level `_pyobs_core_version_cache` global directly.
    """
    global _pyobs_core_version_cache
    now = time.time()
    if _pyobs_core_version_cache is not None and now - _pyobs_core_version_cache[0] < _PYOBS_CORE_VERSION_CACHE_TTL:
        return _pyobs_core_version_cache[1]
    version = None
    for p in list_pyobs_packages():
        if _normalize_package_name(p["name"]) == "pyobs-core":
            try:
                version = Version(p["version"])
            except InvalidVersion:
                version = None
            break
    _pyobs_core_version_cache = (now, version)
    return version


def _is_prerelease(version: str) -> bool:
    try:
        return Version(version).is_prerelease
    except InvalidVersion:
        return False


def _select_latest_version(available: list[str], installed: str) -> str | None:
    """Picks the version _pypi_latest_version reports as "latest" for an `installed`
    version, given every version string PyPI has ever published for the package. Split out
    as a pure, network-free function so this policy has unit test coverage independent of
    PyPI's actual current release history for any real package.

    Mirrors pip's own default pre-release policy for `pip install --upgrade <name>` (no
    version specifier): pre-release candidates are only considered at all if `installed` is
    itself a pre-release -- confirmed live against a real installation via `pip install
    --upgrade --dry-run --report`: for an installed "2.0.0.dev10", pip's resolver reports
    nothing to install at all (not even a "downgrade" to a newer stable "1.54.0") unless
    --pre is passed, in which case it correctly offers a newer "2.0.0.dev13". Just using
    PyPI's own info.version (its "latest stable" field) would report "1.54.0" as latest
    regardless -- wrong in two ways at once: it doesn't surface the real newer prerelease,
    and (before _is_update_available's own PEP 440 comparison) would make an install that's
    actually ahead look like it needs a downgrade. update_package's own --pre gate mirrors
    this exact is_prerelease(installed) check, so the two stay in lockstep -- otherwise the
    UI could advertise an upgrade pip would then silently decline to perform.
    """
    allow_prereleases = _is_prerelease(installed)
    versions = []
    for v in available:
        try:
            parsed = Version(v)
        except InvalidVersion:
            continue
        if parsed.is_prerelease and not allow_prereleases:
            continue
        versions.append(parsed)
    return str(max(versions)) if versions else None


def _pypi_latest_version(name: str, installed: str) -> str | None:
    """None on any failure (package not on PyPI, network error, timeout, bad data) or if no
    comparable version was found -- this only ever feeds a display column, never something
    worth failing the whole page load over. See _select_latest_version for the actual
    "what counts as latest" policy."""
    try:
        resp = requests.get(f"https://pypi.org/pypi/{name}/json", timeout=5)
        resp.raise_for_status()
        releases = resp.json().get("releases", {})
    except Exception:
        return None
    # A version with an empty file list has had every upload deleted/yanked -- nothing left
    # to actually install, so it's not a real candidate.
    available = [v for v, files in releases.items() if files]
    return _select_latest_version(available, installed)


def _is_update_available(installed: str, latest: str | None) -> bool:
    """PEP 440 version comparison, not a plain string inequality -- an installed dev/pre-
    release (e.g. "2.0.0.dev10") can sort *ahead* of PyPI's latest stable release (e.g.
    "1.54.0"), and flagging that as "update available" would invite clicking Update and
    (at best, pip itself still refuses) being confused about why nothing happened. Falls
    back to a plain inequality if either string isn't a version PEP 440 recognizes.
    """
    if latest is None:
        return False
    try:
        return Version(latest) > Version(installed)
    except InvalidVersion:
        return latest != installed


def _package_overview_entry(pkg: dict) -> dict:
    """One get_package_overview() row for an already-installed pkg ({"name", "version"}). Split
    out so the PyPI HTTP call and the git-remote lookup -- both single-package, single-round-trip
    operations -- can be dispatched identically through the same thread pool regardless of which
    kind a given package needs.
    """
    if _is_vcs_managed(pkg["name"]):
        status = _vcs_update_status(pkg["name"])
        return {
            "name": pkg["name"],
            "installed_version": status["installed_commit"][:8] if status["installed_commit"] else pkg["version"],
            "version": pkg["version"],
            "latest_version": status["remote_commit"][:8] if status["remote_commit"] else None,
            "update_available": status["update_available"],
            "vcs": True,
        }
    latest = _pypi_latest_version(pkg["name"], pkg["version"])
    return {
        "name": pkg["name"],
        "installed_version": pkg["version"],
        "latest_version": latest,
        "update_available": _is_update_available(pkg["version"], latest),
        "vcs": False,
    }


def get_package_overview() -> list[dict]:
    """list_pyobs_packages() plus each package's latest available version, fetched in parallel
    since each lookup (a PyPI JSON round-trip, or a `git ls-remote` for a package installed via
    a PYOBS_MANAGED_PACKAGES git/URL spec -- see _is_vcs_managed) is its own network call and
    this page's whole point is showing every pyobs-* package at once.
    """
    installed = list_pyobs_packages()
    if not installed:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(installed))) as pool:
        return list(pool.map(_package_overview_entry, installed))


def build_package_version_matrix(per_host: list[tuple[str, list[dict]]]) -> dict:
    """Turns get_package_overview()-shaped per-host package lists into a package x host
    matrix for the fleet Overview page -- one row per pyobs-* package name (the union across
    every host), one cell per host in the same order as `hosts`, each either that host's
    get_package_overview() entry for the package or None if that host doesn't have it
    installed at all. Mirrors merge_acl_matrices' row["cells"]-is-a-dict-keyed-by-column
    shape turned into a positional list instead (row["cells"][c] there vs. cells[i] here) --
    Django templates can't do a dict lookup keyed by a {% for %} loop variable, only a
    literal attribute/key, so the per-host values need to already be in column order by the
    time they reach the template (see fleet_overview.html's parallel {% for host in
    package_hosts %} / {% for cell in pkg.cells %} loops).

    latest_version is taken from whichever host happened to report one -- PyPI has no notion
    of "per host", so any host's non-None reading is as good as any other's; a package only
    installed on an unreachable host reports None here, same as get_package_overview()'s own
    "latest lookup failed" case.
    """
    host_names = [name for name, _ in per_host]
    by_package: dict[str, dict[str, dict]] = {}
    for host_name, packages in per_host:
        for pkg in packages:
            by_package.setdefault(pkg["name"], {})[host_name] = pkg

    rows = []
    for name in sorted(by_package, key=str.lower):
        entries = by_package[name]
        latest = next((e["latest_version"] for e in entries.values() if e["latest_version"] is not None), None)
        rows.append({
            "name": name,
            "latest_version": latest,
            "cells": [entries.get(host_name) for host_name in host_names],
        })
    return {"hosts": host_names, "packages": rows}


def update_package(name: str, installed_version: str) -> tuple[bool, str]:
    """Runs `pip install --upgrade <spec>` in pyobs's own environment (_pip_exec), where
    <spec> is name itself unless PYOBS_MANAGED_PACKAGES configures a fuller spec for it (see
    _install_spec_for). Callers (api_package_update) must already have checked name against
    list_pyobs_packages() -- the name check here is just defense in depth, not the primary
    access control, so that this function alone can never be used to pip-install something
    arbitrary even if a caller forgot that check. Mirrors list_pyobs_packages' own "pyobs-
    prefixed, or explicitly allow-listed via PYOBS_MANAGED_PACKAGES" rule -- a name only
    reachable here if it could also have shown up on the Packages page in the first place.

    Adds --pre when installed_version is itself a pre/dev release, mirroring the exact same
    is_prerelease check _select_latest_version uses to decide what "latest" even means for
    this package -- without it, pip's own resolver leaves an already-installed pre-release
    alone entirely rather than upgrading it, even to a newer pre-release (verified live, see
    _select_latest_version's docstring), so Update would silently do nothing for exactly the
    packages this policy exists to handle. --upgrade-strategy=only-if-needed (pip's own
    default, made explicit here rather than trusted to stay that way under any local pip.conf)
    keeps --pre's effect scoped to resolving *this* package -- already-satisfied dependencies
    aren't re-examined for a newer prerelease of their own just because this install allows
    prereleases in general.
    """
    if not re.match(r"^pyobs[A-Za-z0-9_.-]*$", name, re.IGNORECASE) and _normalize_package_name(name) not in _managed_package_specs():
        return False, f"Refusing to update unmanaged package: {name!r}"
    args = [_pip_exec(), "install", "--upgrade", "--upgrade-strategy=only-if-needed", "--no-input"]
    if _is_prerelease(installed_version):
        args.append("--pre")
    if _is_vcs_managed(name):
        # For a git+ URL, pip's own upgrade check only compares against the version it computed
        # at install time and has no way to notice the remote has moved on -- it silently treats
        # "already installed" as satisfying the requirement, so Update would otherwise no-op
        # even when _vcs_update_status found a newer remote commit. --no-deps confines the
        # forced reinstall to this package alone, not a full dependency re-resolve.
        args += ["--force-reinstall", "--no-deps"]
    args.append(_install_spec_for(name))
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for pip install to finish"
    except FileNotFoundError:
        return False, f"pip executable not found: {_pip_exec()!r}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


# ── PID helpers ───────────────────────────────────────────────────────────────

def _pid_file(name: str) -> Path:
    return _run_dir() / f"{_active_name(name)}.pid"


def _read_pid(name: str) -> int | None:
    pf = _pid_file(name)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def get_module_status(name: str) -> str:
    """Returns 'running', 'stopped', or 'unknown'."""
    validate_name(name)
    pid = _read_pid(name)
    if pid is None:
        return "stopped"
    if _is_alive(pid):
        return "running"
    # stale PID file — clean up silently
    _pid_file(name).unlink(missing_ok=True)
    return "stopped"


def start_module(name: str) -> tuple[bool, str]:
    validate_name(name)

    if get_module_status(name) == "running":
        return False, f"{name} is already running"

    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return False, f"Config file not found: {config_file}"

    pid_file = _pid_file(name)

    _run_dir().mkdir(parents=True, exist_ok=True)

    args = [_pyobs_exec(), "--pid-file", str(pid_file), "--log-level", _log_level()]
    if _log_backend() == "journald":
        args.append("--syslog")
    else:
        log_file = _log_dir() / f"{_active_name(name)}.log"
        _log_dir().mkdir(parents=True, exist_ok=True)
        args += ["--log-file", str(log_file)]
    args.append(str(config_file))

    try:
        # pyobs daemonizes itself (python-daemon double-fork) when --pid-file is given.
        # The immediate child exits quickly; subprocess.run returns with code 0.
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for module to start"
    except FileNotFoundError:
        return False, f"pyobs executable not found: {_pyobs_exec()!r}"

    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()

    # Daemon writes PID file asynchronously — wait up to 3 s for it
    for _ in range(15):
        pid = _read_pid(name)
        if pid and _is_alive(pid):
            return True, f"Started {name} (PID {pid})"
        time.sleep(0.2)

    return False, "Module launched but PID not confirmed — check logs"


def stop_module(name: str) -> tuple[bool, str]:
    validate_name(name)

    pid = _read_pid(name)
    if pid is None or not _is_alive(pid):
        _pid_file(name).unlink(missing_ok=True)
        return False, f"{name} is not running"

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return False, str(e)

    # Wait up to 5 s for graceful exit
    for _ in range(25):
        if not _is_alive(pid):
            _pid_file(name).unlink(missing_ok=True)
            return True, f"Stopped {name}"
        time.sleep(0.2)

    # Force-kill if it didn't respond
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _pid_file(name).unlink(missing_ok=True)
    return True, f"Force-killed {name} (did not exit after SIGTERM)"


_process_cache: dict[str, psutil.Process] = {}


def get_module_stats(name: str) -> dict | None:
    pid = _read_pid(name)
    if pid is None or not _is_alive(pid):
        _process_cache.pop(name, None)
        return None
    try:
        proc = _process_cache.get(name)
        if proc is None or proc.pid != pid:
            proc = psutil.Process(pid)
            _process_cache[name] = proc
        cpu = proc.cpu_percent(interval=None)
        mem = proc.memory_info().rss / 1024 / 1024
        uptime = int(time.time() - proc.create_time())
        return {"pid": pid, "cpu_percent": round(cpu, 1), "memory_mb": round(mem, 1), "uptime_seconds": uptime}
    except psutil.NoSuchProcess:
        _process_cache.pop(name, None)
        return None


def deactivate_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    if name.startswith("_"):
        return False, f"{name} is already deactivated"
    config = _config_dir() / f"{name}.yaml"
    if not config.exists():
        return False, f"Config not found: {config}"
    if get_module_status(name) == "running":
        stop_module(name)
    config.rename(_config_dir() / f"_{name}.yaml")
    return True, f"Deactivated {name}"


def activate_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    if not name.startswith("_"):
        return False, f"{name} is already active"
    config = _config_dir() / f"{name}.yaml"
    if not config.exists():
        return False, f"Config not found: {config}"
    new_name = name[1:]
    new_config = _config_dir() / f"{new_name}.yaml"
    if new_config.exists():
        return False, f"Config already exists: {new_config}"
    config.rename(new_config)
    return True, f"Activated {new_name}"


def restart_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    stopped, msg = stop_module(name)
    ok, start_msg = start_module(name)
    return ok, start_msg


# ── journald log backend ─────────────────────────────────────────────────────
#
# pyobs-core 2.0.0.dev41 (commit f3b20627, "log _test.yaml configs as test") started
# stripping leading underscores off the config filename stem before stamping PYOBS_MODULE, so
# a deactivated module started manually for testing (`_startup.yaml`) is tagged "startup" in
# the journal, not "_startup" -- the same normalization the file backend's log filename
# already applies via _active_name(). Older pyobs-core still tags the raw name, underscore
# and all. _journald_module_tag() picks whichever this host's actually-installed pyobs-core
# does, via pyobs_core_version() -- each host's journal only ever holds entries from its own
# local pyobs-core install (see api_all_logs' hub delegation: a remote host's logs are always
# fetched by *that host's own* instance, never read out of its journal directly), so there's
# no cross-host version mixing to worry about within a single journalctl call. See
# journald-logs.md, Current state.
_PYOBS_CORE_STRIPS_MODULE_UNDERSCORE = Version("2.0.0.dev41")


def _journald_module_tag(name: str) -> str:
    version = pyobs_core_version()
    # Unknown version (pip lookup failed, pyobs-core not found, ...): assume current
    # pyobs-core behavior rather than the pre-f3b20627 one, since that's what any fresh
    # install has -- an operator on a genuinely old pyobs-core should have a resolvable
    # version already, at which point this falls back correctly.
    if version is None or version >= _PYOBS_CORE_STRIPS_MODULE_UNDERSCORE:
        return _active_name(name)
    return name


def _journalctl_json(args: list[str]) -> list[dict]:
    result = subprocess.run(["journalctl", *args, "-o", "json", "--no-pager"], capture_output=True, text=True)
    entries = []
    for raw in result.stdout.splitlines():
        try:
            entries.append(json.loads(raw))
        except ValueError:
            continue
    return entries


def _journal_entry_to_line(entry: dict) -> str:
    # tz=timezone.utc: the reconstructed line's timestamp has no zone marker (matching the
    # file backend's shape), so it must actually *be* UTC regardless of the host OS's local
    # timezone -- the frontend (templates/modules/detail.html, all_logs.html: parseLogTime)
    # assumes exactly that when it parses these lines back out.
    ts = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000, tz=timezone.utc)
    level = _JOURNALD_PRIORITY_TO_LEVEL.get(int(entry.get("PRIORITY", 6)), "INFO")
    module = entry.get("PYOBS_MODULE", "")
    # CODE_FILE is logging_journald's record.pathname (a full path), but pyobs's own journal
    # formatter builds MESSAGE's "<module> <file>:<line> " prefix from %(filename)s (just the
    # basename) -- basename() here so the two actually match. Caught live: without this, a
    # real module's log lines doubled up the file:line info instead of stripping it.
    code_file = os.path.basename(entry.get("CODE_FILE", "?"))
    code_line = entry.get("CODE_LINE", "?")
    message = entry.get("MESSAGE", "")
    prefix = f"{module} {code_file}:{code_line} "
    if message.startswith(prefix):
        message = message[len(prefix):]
    return f"{ts:%Y-%m-%d %H:%M:%S} [{level}] ({module}) {code_file}:{code_line} {message}"


def _get_logs_journald(name: str, lines: int, before: datetime | None = None) -> list[str]:
    args = ["SYSLOG_IDENTIFIER=pyobs", f"PYOBS_MODULE={_journald_module_tag(name)}"]
    if before is not None:
        args += ["--until", f"{before:%Y-%m-%d %H:%M:%S} UTC"]
    args += ["-n", str(lines)]
    entries = _journalctl_json(args)
    return [_journal_entry_to_line(e) for e in entries]


def _get_log_stats_journald(name: str, since: datetime | None = None) -> dict:
    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    # since (e.g. a per-module "last acknowledged" instant from the dashboard) narrows the
    # window when it's more recent than the standard 24h rollup, but never widens it beyond
    # 24h -- an ack from days ago shouldn't suddenly pull that whole history back in.
    if since is not None:
        cutoff = max(since, datetime.now(timezone.utc) - timedelta(hours=24))
        since_arg = f"{cutoff:%Y-%m-%d %H:%M:%S} UTC"
    else:
        since_arg = "-24h"
    entries = _journalctl_json(["SYSLOG_IDENTIFIER=pyobs", f"PYOBS_MODULE={_journald_module_tag(name)}", "--since", since_arg])
    for entry in entries:
        level = _JOURNALD_PRIORITY_TO_LEVEL.get(int(entry.get("PRIORITY", -1)))
        if level:
            counts[level] += 1
    return counts


def get_logs(name: str, lines: int = 300, filter_str: str = "", before: datetime | None = None) -> list[str]:
    validate_name(name)
    if _log_backend() == "journald":
        log_lines = _get_logs_journald(name, lines, before)
    else:
        # "Load older logs" (before) isn't supported for the file backend yet -- a plain
        # `tail -n` has no seek/offset concept to page further back with (see get_all_logs'
        # analogous file-backend limitation) -- so this reports "nothing older available"
        # rather than re-returning the same tail on every scroll-to-top.
        if before is not None:
            return []
        log_file = _log_dir() / f"{_active_name(name)}.log"
        if not log_file.exists():
            return []
        result = subprocess.run(["tail", "-n", str(lines), str(log_file)], capture_output=True, text=True)
        log_lines = result.stdout.splitlines()
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def _get_all_logs_journald(names: list[str] | None, lines: int, before: datetime | None = None) -> list[str]:
    # names is None means "no PYOBS_MODULE restriction at all" -- broader than "every
    # currently configured module," since it also surfaces entries from a module whose
    # config has since been removed/renamed. names == [] means the caller explicitly
    # deselected every module, which must yield nothing, not fall back to unrestricted.
    if names is not None and not names:
        return []
    args = ["SYSLOG_IDENTIFIER=pyobs"]
    if names:
        # Repeating a field name is journalctl's own OR syntax -- combined with the
        # SYSLOG_IDENTIFIER term via implicit AND, this matches any of the given modules.
        args += [f"PYOBS_MODULE={_journald_module_tag(n)}" for n in names]
    if before is not None:
        args += ["--until", f"{before:%Y-%m-%d %H:%M:%S} UTC"]
    args += ["-n", str(lines)]
    entries = _journalctl_json(args)
    return [_journal_entry_to_line(e) for e in entries]


def merge_log_lines(line_lists: list[list[str]], lines: int) -> list[str]:
    """Merges several already-formatted, already-oldest-first-ordered log line lists into one
    list ordered by each line's own leading timestamp, trimmed to the overall last `lines`.

    Used both for the file backend's per-module tail merge (_get_all_logs_file) and, in
    views.py, for combining each hub host's own already-merged fleet-wide result into one
    cross-host view -- same "no shared time index, so merge-and-trim after the fact" shape
    either way, just one level up in the second case.
    """
    entries: list[tuple[datetime, int, int, str]] = []
    for list_index, line_list in enumerate(line_lists):
        for order, line in enumerate(line_list):
            m = _TS_RE.match(line)
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else datetime.min
            entries.append((ts, list_index, order, line))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    return [line for _, _, _, line in entries[-lines:]]


def _get_all_logs_file(names: list[str], lines: int) -> list[str]:
    # Each module's own file has no cross-module time index, so the merge tails `lines`
    # from every file independently, then sorts the union by each line's own leading
    # timestamp and trims to the overall last `lines` -- an approximation (a module with
    # much higher log volume could in principle push another's tail out of the merged
    # window) rather than a true global tail, but matches this app's existing "good enough,
    # not a from-scratch index" tolerance for the file backend (see get_log_stats's binary
    # search comment).
    line_lists = []
    for name in names:
        log_file = _log_dir() / f"{_active_name(name)}.log"
        if not log_file.exists():
            continue
        result = subprocess.run(["tail", "-n", str(lines), str(log_file)], capture_output=True, text=True)
        line_lists.append(result.stdout.splitlines())
    return merge_log_lines(line_lists, lines)


def get_all_logs(
    names: list[str] | None = None, lines: int = 300, filter_str: str = "", before: datetime | None = None
) -> list[str]:
    if names is not None:
        for name in names:
            validate_name(name)
    if _log_backend() == "journald":
        log_lines = _get_all_logs_journald(names, lines, before)
    else:
        # See get_logs' identical file-backend caveat -- no seek/offset to page further back
        # with, so an older-logs request reports nothing available rather than re-serving
        # the same tail.
        if before is not None:
            return []
        log_lines = _get_all_logs_file(names if names is not None else list_modules(), lines)
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


def get_log_stats(name: str, since: datetime | None = None) -> dict:
    validate_name(name)
    if _log_backend() == "journald":
        return _get_log_stats_journald(name, since)

    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    log_file = _log_dir() / f"{_active_name(name)}.log"
    if not log_file.exists():
        return counts

    cutoff = datetime.now() - timedelta(hours=24)
    if since is not None:
        # File-backend timestamps are naive and assumed UTC (see _journal_entry_to_line);
        # convert the aware `since` the same way before comparing, and only narrow the
        # window, never widen it beyond the standard 24h rollup.
        since_naive = since.astimezone(timezone.utc).replace(tzinfo=None)
        cutoff = max(cutoff, since_naive)

    def _line_ts(line: str) -> datetime | None:
        m = _TS_RE.match(line)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    with open(log_file, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        if file_size == 0:
            return counts

        # Binary search for the byte offset of the first line within the 24 h window.
        lo, hi = 0, file_size
        while lo < hi - 1:
            mid = (lo + hi) // 2
            f.seek(mid)
            f.readline()  # skip partial line at seek point
            line = f.readline().decode("utf-8", errors="replace")
            ts = _line_ts(line)
            if ts is not None and ts < cutoff:
                lo = mid
            else:
                hi = mid

        # Read from the found offset and count matching lines.
        f.seek(lo)
        if lo > 0:
            f.readline()  # skip partial line
        for raw in f:
            line = raw.decode("utf-8", errors="replace")
            ts = _line_ts(line)
            if ts is not None and ts < cutoff:
                continue
            m = _LOG_LEVEL_RE.search(line)
            if m:
                counts[m.group(1)] += 1

    return counts


def get_shared_config(name: str) -> str | None:
    validate_shared_name(name)
    f = _config_dir() / f"{name}.yaml"
    return f.read_text() if f.exists() else None


def save_shared_config(name: str, content: str) -> None:
    validate_shared_name(name)
    f = _config_dir() / f"{name}.yaml"
    if not f.exists():
        raise FileNotFoundError(f"Shared config not found: {f}")
    f.write_text(content)
    _git_auto_stage()


def get_config(name: str) -> str | None:
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return None
    return config_file.read_text()


def save_config(name: str, content: str) -> None:
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")
    config_file.write_text(content)
    _git_auto_stage()


_NEW_MODULE_TEMPLATE = (
    "# class: pyobs.modules.<package>.<ClassName> -- see other modules' configs, or\n"
    "# pyobs-core's own docs, for the class path\n"
    "class: \n"
)


def create_module(name: str) -> None:
    """Creates a brand-new module config with minimal starter YAML -- unlike save_config,
    which refuses to write a file that doesn't exist yet, this is the one path that's
    allowed to. Refuses if a config with this name already exists, same as it would if
    someone tried to hand-create a file that's already there."""
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if config_file.exists():
        raise FileExistsError(f"Module {name!r} already exists")
    _config_dir().mkdir(parents=True, exist_ok=True)
    config_file.write_text(_NEW_MODULE_TEMPLATE)
    _git_auto_stage()


# ── Git-backed config ───────────────────────────────────────────────────────────


def git_repo_exists() -> bool:
    """Check whether a Git working tree exists at the configured repository root."""
    if not _git_enabled():
        return False
    return (_git_repo_dir() / ".git").is_dir()


def git_clone() -> tuple[bool, str]:
    """Clone the configured repository, set up sparse checkout, and create config symlink.

    The clone target is the repository root (`_git_repo_dir()`), never
    `PYOBS_CONFIG_DIR`. Refuses to clone if that directory already
    contains a Git working tree or non-empty contents.

    After a successful clone, creates a symlink from `PYOBS_CONFIG_DIR` to
    `<repo_dir>/<subpath>` so pyobs can read configs via its standard path.

    Returns (success, message).
    """
    repo_dir = _git_repo_dir()
    repo = settings.PYOBS_CONFIG_GIT_REPO
    branch = getattr(settings, "PYOBS_CONFIG_GIT_BRANCH", "main")
    subpath = _git_subpath()

    if not repo:
        return False, "PYOBS_CONFIG_GIT_REPO is not set"
    if git_repo_exists():
        return False, "Repository already exists"
    if repo_dir.exists() and any(repo_dir.iterdir()):
        return False, "Repository root is not empty"

    ok, msg = _git_config_ok()
    if not ok:
        return False, msg

    try:
        repo_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create repository directory {repo_dir}: {e}"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        result = subprocess.run(
            ["git", "clone", "--branch", branch, repo, str(repo_dir)],
            cwd=str(repo_dir.parent),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except FileNotFoundError:
        return False, "git executable not found"
    except subprocess.TimeoutExpired:
        return False, "git clone timed out (120s)"

    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()

    if subpath:
        sp = subprocess.run(
            ["git", "sparse-checkout", "init", "--cone"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if sp.returncode != 0:
            import shutil
            shutil.rmtree(repo_dir, ignore_errors=True)
            return False, sp.stderr.strip()

        ss = subprocess.run(
            ["git", "sparse-checkout", "set", subpath],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if ss.returncode != 0:
            shutil.rmtree(repo_dir, ignore_errors=True)
            return False, ss.stderr.strip()

    # Create symlink from PYOBS_CONFIG_DIR -> repo_dir/subpath
    ok, msg = _ensure_symlink()
    if not ok:
        import shutil
        shutil.rmtree(repo_dir, ignore_errors=True)
        return False, f"Failed to create config symlink: {msg}"

    return True, "Repository cloned successfully"


def git_fetch() -> tuple[bool, str]:
    """Fetch remote updates without modifying the working tree."""
    return _git_run(["fetch", "--tags"])


def git_status() -> dict:
    """Get repository status."""
    status: dict[str, object] = {
        "branch": "",
        "remote": "origin",
        "ahead": 0,
        "behind": 0,
        "clean": True,
        "dirty": False,
        "modified_files": [],
        "new_files": [],
        "deleted_files": [],
        "last_commit": "",
        "last_commit_time": "",
    }

    success, branch = _git_run(["rev-parse", "--abbrev-ref", "HEAD"])
    if success and branch:
        status["branch"] = branch.strip()
        local = branch.strip()
        remote_ref = f"origin/{local}"
        success, ahead = _git_run(["rev-list", "--count", f"{local}..{remote_ref}"])
        success, behind = _git_run(["rev-list", "--count", f"{remote_ref}..{local}"])
        try:
            status["ahead"] = int(ahead.strip())
        except (ValueError, AttributeError):
            pass
        try:
            status["behind"] = int(behind.strip())
        except (ValueError, AttributeError):
            pass

    success, hash_out = _git_run(["rev-parse", "--short", "HEAD"])
    if success and hash_out:
        status["last_commit"] = hash_out.strip()

    success, time_out = _git_run(["log", "-1", "--format=%ci"])
    if success and time_out:
        status["last_commit_time"] = time_out.strip()

    success, porcelain = _git_run(["status", "--porcelain=v2", "--branch"])
    if success and porcelain:
        modified_files: list[str] = []
        new_files: list[str] = []
        deleted_files: list[str] = []
        subpath = _git_subpath()
        for line in porcelain.splitlines():
            if not line.strip() or line.startswith("#") or line.startswith("##"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            x, y = parts[1], parts[2]
            raw_path = parts[-1].strip('"').strip("'")
            if not raw_path:
                continue
            # Strip the git subpath prefix so paths are relative to _config_dir()
            filepath = raw_path
            if subpath and subpath != "/":
                sep = os.sep
                prefix = subpath + sep
                if filepath == subpath or filepath.startswith(prefix):
                    filepath = filepath[len(subpath):].lstrip(sep)
            if not filepath:
                continue
            # y ? = untracked → "new"
            if y == "?":
                new_files.append(filepath)
            # x has D = deleted from git tracking → "deleted"
            elif x.startswith("D"):
                deleted_files.append(filepath)
            # x has A or R = newly added/renamed to git tracking → "new"
            elif x.startswith(("A", "R")):
                new_files.append(filepath)
            # everything else = modified
            elif x != " ":
                modified_files.append(filepath)
        status["modified_files"] = modified_files
        status["new_files"] = new_files
        status["deleted_files"] = deleted_files
        status["dirty"] = bool(modified_files or new_files or deleted_files)
        status["clean"] = not status["dirty"]

    return status


def git_stage_all() -> tuple[bool, str]:
    """Stage all changes."""
    return _git_run(["add", "-A", str(_config_dir())])


def git_commit(message: str) -> tuple[bool, str]:
    """Commit all staged changes.

    Passes an explicit author identity via -c rather than relying on a global `git config
    user.name/user.email` already being set for whichever system user runs pyobs-web-admin --
    without it, git refuses the commit outright with "Author identity unknown" (this is
    exactly what PYOBS_CONFIG_GIT_AUTHOR_NAME/EMAIL exist to avoid), and -c only overrides it
    for this one invocation rather than mutating the repo's/user's actual git config.
    """
    if not message:
        message = "Auto-commit config changes before pull"
    author_name = getattr(settings, "PYOBS_CONFIG_GIT_AUTHOR_NAME", "pyobs-web-admin")
    author_email = getattr(settings, "PYOBS_CONFIG_GIT_AUTHOR_EMAIL", "pyobs-web-admin@localhost")
    return _git_run([
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message, "--allow-empty",
    ])


def git_pull() -> tuple[bool, str]:
    """Pull from origin."""
    return _git_run(["pull"])


def git_push() -> tuple[bool, str]:
    """Push current branch to origin."""
    return _git_run(["push"])


def git_init_if_needed() -> tuple[bool, str]:
    """Lazy init: clone if the repository doesn't exist yet."""
    if not git_repo_exists():
        return git_clone()
    return True, "Repository already exists"


def git_reset() -> tuple[bool, str]:
    """Discard all uncommitted changes (reset working tree to HEAD)."""
    return _git_run(["reset", "--hard", "HEAD"])


# ── ACL resolution ────────────────────────────────────────────────────────────

_TOP_LEVEL_KEY_RE = re.compile(r"^(\S+):(.*)$")
_INCLUDE_RE = re.compile(r"{include (\S+)(?: (\S+))?}")


def _shared_name(filename: str) -> str:
    """Turns an {include ...}'d filename (e.g. "acl.shared.yaml") into the name
    list_shared_configs()/get_shared_config() use (e.g. "acl.shared")."""
    return filename[: -len(".yaml")] if filename.endswith(".yaml") else filename


def _block_source_file(raw: str, key: str) -> str | None:
    """Given a module's raw (unprocessed) config text, determines whether its `<key>:` key's
    value is defined directly in the module's own file or pulled in from a shared fragment
    via {include}. Returns the shared fragment's name (as used by list_shared_configs()),
    or None if the block (if any) is defined locally. Generalized from what used to be
    acl:-only (`_acl_source_file`) so `get_resolved_comm` can reuse the exact same
    detection for `comm:` -- see ejabberd-user-management.md's config write-back, which needs
    the same "is this locally editable or does it live in a shared fragment" answer for
    comm.password that get_resolved_acl already gives for acl:.

    Only recognizes the two patterns pyobs-web-admin's own editor can produce (see
    acl-matrix.md, "Editing from the matrix"): a bare top-level `{include x.shared.yaml}`
    whose target's own top-level content defines `<key>:`, or a `<key>:` key whose entire
    value is a single `{include x.shared.yaml}`. A more deeply nested include structure
    (e.g. an include reaching into a dotted sub-key of a larger fragment) falls back to
    being reported as "own file" -- a conservative default: such a rule just isn't routed
    to a shared-fragment edit yet, and is edited in the module's own file instead.
    """
    lines = raw.splitlines()
    key_block: list[str] | None = None
    bare_includes: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace():
            m = _TOP_LEVEL_KEY_RE.match(line)
            if m and m.group(1) == key:
                block = [line]
                i += 1
                while i < len(lines) and (lines[i] == "" or lines[i][0].isspace()):
                    block.append(lines[i])
                    i += 1
                key_block = block
                continue
            inc = _INCLUDE_RE.fullmatch(line.strip())
            if inc:
                bare_includes.append(inc.group(1))
        i += 1

    if key_block is not None:
        inline_value = key_block[0].split(":", 1)[1].strip()
        body = "\n".join(key_block[1:]).strip() or inline_value
        inc = _INCLUDE_RE.fullmatch(body)
        return _shared_name(inc.group(1)) if inc else None

    for filename in bare_includes:
        included = _config_dir() / filename
        if included.exists() and re.search(rf"(?m)^{key}:", included.read_text()):
            return _shared_name(filename)
    return None


def get_resolved_acl(name: str) -> tuple[dict | None, str | None]:
    """Returns (acl_block, source) for a module's *effective* acl: config, resolving any
    {include} the same way pyobs-core does.

    acl_block is the raw "acl:" dict (with "allow"/"deny"/"mode" keys) or None if the
    module has no acl: key at all (fully open access). source is None if the block is
    defined directly in the module's own config file, or the shared fragment's name (as
    used by list_shared_configs()/get_shared_config()) if pulled in via {include}.
    """
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return None, None
    try:
        resolved = yaml.safe_load(pre_process_yaml(str(config_file))) or {}
    except (OSError, yaml.YAMLError):
        return None, None
    acl = resolved.get("acl")
    if acl is None:
        return None, None
    return acl, _block_source_file(config_file.read_text(), "acl")


def get_resolved_comm(name: str) -> tuple[str | None, str | None, str | None]:
    """Returns (comm_user, comm_password, source) for a module's *effective* comm: block --
    the same resolution get_resolved_acl uses for acl:, via pre_process_yaml +
    yaml.safe_load, since comm: can equally arrive via {include} or a YAML anchor/merge key
    (a real config uses `comm: {<<: *comm, user: camera, password: pyobs}`).

    comm_user/comm_password are None if the module has no comm: block at all (confirmed real
    example: HttpFileCache) or the respective sub-key is missing -- not an error, just "this
    module was never expected to have an XMPP identity" (see ejabberd-integration.md, "Where
    it surfaces"). source is None if comm: is defined directly in the module's own file, or
    the shared fragment's name if pulled in via {include}.

    The password is needed (not just user) for ejabberd-user-management.md's register
    action: it registers a new XMPP account using whatever password the module's config
    *already* declares, rather than prompting for a new one -- the whole point is making an
    existing comm.user/comm.password config actually work, not choosing a fresh credential.
    source is needed for that same doc's config write-back (change_password), which must
    refuse to edit comm.password: when it resolves to a shared fragment, exactly the guard
    save_local_acl already applies to acl:.

    Also returns all-None if resolution itself fails -- e.g. an {include}'d fragment that no
    longer exists -- the same as "no comm: block", rather than raising. Unlike get_resolved_acl
    (whose only caller, resolve_and_validate_acl, already catches this), get_resolved_comm is
    called directly from several views (dashboard status polling, module ejabberd endpoints,
    the Users page), so one module's broken include must not crash the whole fleet view.
    """
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return None, None, None
    try:
        resolved = yaml.safe_load(pre_process_yaml(str(config_file))) or {}
    except (OSError, yaml.YAMLError):
        return None, None, None
    comm = resolved.get("comm")
    if not isinstance(comm, dict):
        return None, None, None
    user = comm.get("user")
    password = comm.get("password")
    return (
        user if isinstance(user, str) else None,
        password if isinstance(password, str) else None,
        _block_source_file(config_file.read_text(), "comm"),
    )


def get_comm_user(name: str) -> str | None:
    """Resolves a module's own XMPP identity -- its comm.user, e.g. "camera" in
    comm: {user: camera, ...}. Display-only convenience wrapper around get_resolved_comm,
    dropping the password/source -- most callers (dashboard, module page) only ever show
    this value, they don't edit it or need its credential. See get_resolved_comm for the
    fuller resolution ejabberd-user-management.md's write actions need.
    """
    return get_resolved_comm(name)[0]


def find_modules_sharing_comm_user(user: str) -> list[str]:
    """Every locally-configured module whose resolved comm.user equals user.

    Needed because ejabberd-user-management.md's write actions (register/change_password/
    ban_account/unban_account/unregister) affect *every* module sharing an XMPP identity,
    not just whichever module's page an action was triggered from -- ejabberd-integration.md's
    own "third bug" documents _test and camera sharing one comm.user for real, in this exact
    fleet, not a hypothetical edge case.
    """
    return [name for name in list_modules() if get_comm_user(name) == user]


def build_comm_user_map() -> dict[str, list[str]]:
    """Maps every local module's resolved comm.user to the list of module names using it --
    the reverse direction of find_modules_sharing_comm_user, built once across all of
    list_modules() rather than queried one identity at a time.

    Feeds the fleet-wide Users page (DEVELOPMENT.md's Ideas -> promoted here): unlike the
    module page's own XMPP row, that page needs "for every registered ejabberd account,
    which module(s) if any use it" -- the reverse lookup, not "for this one identity, which
    modules share it."
    """
    mapping: dict[str, list[str]] = {}
    for name in list_modules():
        user = get_comm_user(name)
        if user:
            mapping.setdefault(user, []).append(name)
    return mapping


def _yaml_scalar(value: str) -> str:
    """Renders value as a single-line YAML scalar suitable for splicing directly after
    "key: " in raw config text -- reuses PyYAML's own quoting rules (handles colons, quotes,
    leading/trailing whitespace, etc. correctly) via a throwaway single-key dict dump,
    rather than hand-rolling escaping logic for a config value as sensitive as a password."""
    dumped = yaml.safe_dump({"_": value}, default_flow_style=False).strip()
    return dumped.split(": ", 1)[1]


def _replace_comm_password(raw: str, new_password: str) -> str:
    """Replaces just the password: sub-value inside a module's top-level comm: block,
    leaving every other line in that block -- including a `<<: *comm` anchor merge key,
    `user:`, or anything else -- byte-for-byte untouched.

    Unlike _replace_local_acl_block, which re-serializes its whole block fresh, comm: can't
    be treated that way without destroying an anchor-merge reference: a real config's actual
    shape is `comm: {<<: *comm, user: telescope, password: pyobs}` in block style (confirmed
    against this box's own telescope.yaml) -- re-dumping the resolved dict from scratch would
    expand `<<: *comm` into a flat copy of every merged-in key instead of preserving the
    merge-key shorthand, a much more destructive change than acl:'s "comments are lost"
    tradeoff.

    Only valid to call when comm: is known to be defined directly in this file (source is
    None, see get_resolved_comm) and already has its own password: sub-key. Raises
    ValueError if no top-level comm: block or no password: sub-key is found -- this doesn't
    handle adding a password: key that doesn't exist yet, matching this feature's scope of
    managing an *existing* comm.user (see ejabberd-user-management.md, "Modules with no
    comm: block").
    """
    lines = raw.splitlines()
    block_start = block_end = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace():
            m = _TOP_LEVEL_KEY_RE.match(line)
            if m and m.group(1) == "comm":
                block_start = i
                i += 1
                while i < len(lines) and lines[i] != "" and lines[i][0].isspace():
                    i += 1
                block_end = i
                break
        i += 1

    if block_start is None:
        raise ValueError("no top-level comm: block found")

    password_re = re.compile(r"^(\s*)password\s*:\s*.*$")
    for j in range(block_start, block_end):
        m = password_re.match(lines[j])
        if m:
            lines[j] = f"{m.group(1)}password: {_yaml_scalar(new_password)}"
            return "\n".join(lines) + "\n"

    raise ValueError("comm: block has no password: sub-key to replace")


def save_comm_password(user: str, new_password: str) -> list[str]:
    """Writes new_password into comm.password: for every local module whose comm.user
    resolves to user, splicing just that sub-key (_replace_comm_password). Returns the list
    of module names updated.

    All-or-nothing: if *any* matching module's comm: resolves to a shared fragment, raises
    before writing to *any* of them -- a partial write (some modules updated, others left
    with a now-stale password) would be a worse outcome than not writing at all, exactly the
    risk ejabberd-user-management.md's Design section calls out for a shared comm.user. If
    verification fails partway through (some files written, a later one doesn't check out),
    rolls back every file this call itself wrote, mirroring save_local_acl's safety net but
    extended across the whole matching set.
    """
    names = find_modules_sharing_comm_user(user)
    if not names:
        raise ValueError(f'no local module has comm.user "{user}"')

    originals: dict[str, str] = {}
    for name in names:
        _, _, source = get_resolved_comm(name)
        if source is not None:
            raise ValueError(
                f'comm: for "{name}" (comm.user "{user}") comes from shared fragment '
                f'"{source}" -- edit it there instead'
            )
        original = get_config(name)
        if original is None:
            raise FileNotFoundError(f"Config file not found for module: {name}")
        originals[name] = original

    written: list[str] = []
    try:
        for name in names:
            save_config(name, _replace_comm_password(originals[name], new_password))
            written.append(name)

        for name in names:
            resolved_user, resolved_password, _ = get_resolved_comm(name)
            if resolved_user != user or resolved_password != new_password:
                raise ValueError(f'could not verify the comm.password: edit for "{name}" after writing')
    except Exception:
        for name in written:
            save_config(name, originals[name])
        raise

    return names


def _dump_acl_block(acl: dict) -> list[str]:
    """Serializes {"acl": acl} via ruamel.yaml into the lines spliced into a module's raw
    config text by _replace_local_acl_block. This only ever generates a *fresh* acl: block
    from scratch -- it isn't a round-trip of the file's previous acl: content, so any
    comments a human had written inside the old block are lost on save (comments elsewhere
    in the file are untouched, since the splice never rewrites those lines)."""
    buf = io.StringIO()
    _ACL_YAML.dump({"acl": acl}, buf)
    return buf.getvalue().rstrip("\n").splitlines()


def _replace_local_acl_block(raw: str, acl: dict | None) -> str:
    """Replaces (or adds, or removes) a module's top-level "acl:" block in its raw config
    text, leaving every other line -- other keys, {include ...} directives, comments, blank
    lines -- byte-for-byte untouched. Only valid to call when the acl: block is known to be
    defined directly in this file rather than pulled in via {include} (callers must check
    get_resolved_acl's source is None first -- see acl-matrix.md, "Editing from the
    matrix", for why writing through a shared fragment must never happen silently).

    Locates the block the same way _block_source_file does (walk top-level keys, a "acl:"
    line plus every following blank-or-indented line is the block), except a blank line
    ends the block here rather than being absorbed into it -- a simplifying assumption
    (an acl: block with an intentional blank line in the middle of it, e.g. between "mode:"
    and "allow:", would confuse this). save_local_acl re-resolves the acl: after writing
    and rolls back on mismatch, which catches this rather than silently corrupting the file.
    """
    lines = raw.splitlines()
    start = end = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace():
            m = _TOP_LEVEL_KEY_RE.match(line)
            if m and m.group(1) == "acl":
                start = i
                i += 1
                while i < len(lines) and lines[i] != "" and lines[i][0].isspace():
                    i += 1
                end = i
                break
        i += 1

    new_block = _dump_acl_block(acl) if acl else []

    if start is not None:
        result = lines[:start] + new_block + lines[end:]
    elif new_block:
        result = lines + ([""] if lines and lines[-1] != "" else []) + new_block
    else:
        return raw

    return "\n".join(result) + "\n"


def save_local_acl(name: str, acl: dict | None) -> None:
    """Writes a structured acl: edit (from the matrix's per-target edit form) into a
    module's own raw config file.

    Splices just the acl: block into the raw text (_replace_local_acl_block) rather than
    doing a full YAML round-trip of the whole file, since the raw file can contain bare
    {include ...} lines that aren't valid standalone YAML on their own (see
    pyobs_config.pre_process_yaml) -- a generic YAML parser can't load it directly.

    Refuses to write if the module's acl: currently comes from a shared fragment; callers
    must route that edit to the fragment's own file instead (get_resolved_acl's source).
    After writing, re-resolves the module's acl: and rolls back to the original content if
    it doesn't match what was requested -- seeing acl-matrix.md's note on the splice's
    simplifying assumption, this is the safety net against a silent bad write rather than
    trying to make the splice logic exhaustively correct up front.
    """
    validate_name(name)
    _, source = get_resolved_acl(name)
    if source is not None:
        raise ValueError(f'acl: for "{name}" comes from shared fragment "{source}" -- edit it there instead')

    original = get_config(name)
    if original is None:
        raise FileNotFoundError(f"Config file not found for module: {name}")

    save_config(name, _replace_local_acl_block(original, acl))

    resolved, new_source = get_resolved_acl(name)
    if new_source is not None or (resolved or None) != (acl or None):
        save_config(name, original)
        raise ValueError("could not verify the acl: edit after writing -- rolled back, no changes made")


# ── ACL matrix ────────────────────────────────────────────────────────────────

_INTERFACE_NAME_RE = re.compile(r"^I[A-Z]\w*$")


def _is_interface_name(entry: str) -> bool:
    """Heuristic for telling an interface-name shorthand entry (e.g. "ICamera") in an acl
    allow list apart from a plain method name, without importing pyobs-core's own
    pyobs.interfaces to check against (see acl-matrix.md, "Interface-name shorthand").
    Relies on pyobs's own naming convention: interfaces are always IPascalCase, method
    names are always snake_case, so the two can never collide.
    """
    return bool(_INTERFACE_NAME_RE.match(entry))


def _acl_cell(acl: dict | None, caller: str) -> dict:
    """Computes one (target, caller) cell's value from the target's resolved acl: block,
    per the table in acl-matrix.md, "What the matrix shows"."""
    if not acl:
        return {"kind": "open", "methods": None, "mode": "enforce"}

    mode = acl.get("mode", "enforce")
    allow: dict[str, Any] | None = acl.get("allow")
    deny = acl.get("deny")

    if allow is not None:
        entries = allow.get(caller)
        if entries is None:
            kind, methods = "denied", None
        elif entries == "*":
            kind, methods = "all", None
        else:
            kind, methods = "methods", [
                {"name": e, "is_interface": _is_interface_name(e)} for e in entries
            ]
    elif deny is not None:
        kind, methods = ("denied", None) if caller in deny else ("all", None)
    else:
        # acl: present but neither allow nor deny set -- pyobs-core's Module._acl_denied()
        # treats this the same as no acl block at all (nothing to check against).
        kind, methods = "all", None

    return {"kind": kind, "methods": methods, "mode": mode}


def resolve_and_validate_acl(name: str) -> tuple[dict | None, str | None, str | None]:
    """Like get_resolved_acl, but also validates the acl:'s shape (allow must be a mapping,
    deny must be a list) and catches any resolution error (bad YAML, broken {include}, ...)
    into a returned message instead of raising. Returns (acl, source, error) -- acl and
    source are None whenever error is set. Shared by build_acl_matrix (one row's error
    shouldn't abort the whole fleet-wide scan) and the single-module ACL tab endpoint
    (api_acl's GET), which need the identical error-handling contract.
    """
    try:
        acl, source = get_resolved_acl(name)
        if acl is not None:
            allow = acl.get("allow")
            deny = acl.get("deny")
            if allow is not None and not isinstance(allow, dict):
                raise ValueError(f'acl "allow" must be a mapping of caller -> methods, got {type(allow).__name__}')
            if deny is not None and not isinstance(deny, list):
                raise ValueError(f'acl "deny" must be a list of callers, got {type(deny).__name__}')
        return acl, source, None
    except Exception as e:
        return None, None, str(e)


def build_acl_matrix() -> dict:
    """Builds the fleet-wide (target x caller) ACL matrix.

    Rows are every module list_modules() returns; columns are that same full module list
    *plus* every caller name mentioned in any module's resolved acl: block ("allow" keys or
    "deny" entries) that isn't itself a managed module (e.g. a human/external caller like
    "scheduler" if it has no config of its own) -- every module is always a column, whether
    or not it's ever actually referenced as a caller anywhere, so "could A reach B" is
    answerable for any pair, not just pairs where B happens to already appear in some acl:
    block. A module whose config/acl can't be resolved (bad YAML, broken {include}, ...) is
    still included as a row, with its "error" set, rather than aborting the whole scan.
    """
    targets = list_modules()
    acls: dict[str, dict | None] = {}
    sources: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    callers: set[str] = set(targets)

    for name in targets:
        acl, source, error = resolve_and_validate_acl(name)
        acls[name] = acl
        sources[name] = source
        if error:
            errors[name] = error
        if acl:
            allow = acl.get("allow")
            deny = acl.get("deny")
            if isinstance(allow, dict):
                callers.update(allow.keys())
            if isinstance(deny, list):
                callers.update(deny)

    caller_names = sorted(callers)
    rows = [
        {
            "name": name,
            "acl": acls[name],
            "source": sources[name],
            "open": acls[name] is None and name not in errors,
            "error": errors.get(name),
            "cells": {caller: _acl_cell(acls[name], caller) for caller in caller_names},
        }
        for name in targets
    ]

    return {"targets": rows, "callers": caller_names}


def merge_acl_matrices(per_host: list[tuple[str, dict]]) -> dict:
    """Combines each host's build_acl_matrix()-shaped result into one fleet-wide matrix --
    see acl-matrix.md, "Hub mode interaction". per_host is a list of (host_name, matrix)
    pairs, e.g. [("localhost", build_acl_matrix()), ("MONETS", <that host's own matrix,
    fetched via the hub proxy>), ...].

    Each host only knows about the callers its own modules' acl: blocks reference, so a
    row fetched from one host is missing cells for callers that only appear on some other
    host. Cells are therefore recomputed here against the union of every host's callers,
    reusing _acl_cell (a pure function of a target's acl: dict + a caller name -- safe to
    call again outside the host that originally resolved that acl:) rather than trusting
    each host's own host-local cells.
    """
    caller_names = sorted({c for _, matrix in per_host for c in matrix["callers"]})
    rows = [
        {**row, "host": host_name, "cells": {c: _acl_cell(row["acl"], c) for c in caller_names}}
        for host_name, matrix in per_host
        for row in matrix["targets"]
    ]
    return {"targets": rows, "callers": caller_names}