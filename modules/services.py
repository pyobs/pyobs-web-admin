import io
import json
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
    return Path(settings.PYOBS_CONFIG_DIR)


def _log_dir() -> Path:
    return Path(settings.PYOBS_LOG_DIR)


def _run_dir() -> Path:
    return Path(settings.PYOBS_RUN_DIR)


def _pyobs_exec() -> str:
    return settings.PYOBS_EXEC


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
    modules with -- see DEV_JOURNALD_LOGS.md."""
    configured = getattr(settings, "PYOBS_LOG_BACKEND", None)
    if configured:
        return configured
    return "journald" if _pyobsd_config().get("syslog") else "file"


# journald PRIORITY -> pyobs log level. Not the naively-expected {2: CRITICAL, ...} --
# logging.CRITICAL and logging.FATAL are the same int (50) in Python's logging module, so
# logging_journald.JournaldLogHandler.LEVELS's dict literal silently collapses to
# LEVELS[50] == 0, not 2. Verified live against a real emitted CRITICAL record -- see
# DEV_JOURNALD_LOGS.md, Design, for the full trail. 2/1/5 never occur in practice.
_JOURNALD_PRIORITY_TO_LEVEL = {0: "CRITICAL", 3: "ERROR", 4: "WARNING", 6: "INFO", 7: "DEBUG"}


def validate_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
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
    release history, so get_package_overview skips the PyPI lookup for it entirely rather
    than reporting a spurious "unknown"/mismatched result.
    """
    spec = _managed_package_specs().get(_normalize_package_name(name))
    return spec is not None and spec.is_vcs


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


def get_package_overview() -> list[dict]:
    """list_pyobs_packages() plus each package's latest PyPI release, fetched in parallel
    since PyPI's JSON API is one HTTP round-trip per package and this page's whole point is
    showing every pyobs-* package at once. Skips the PyPI lookup entirely for a package
    installed via a PYOBS_MANAGED_PACKAGES git/URL spec (_is_vcs_managed) -- it isn't
    published on PyPI, so the lookup would either fail or (worse) hit an unrelated
    same-named package there; "vcs": True lets the Packages page offer a manual
    reinstall-to-pick-up-latest-commit action instead of a version comparison it can't make.
    """
    installed = list_pyobs_packages()
    if not installed:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(installed))) as pool:
        latest_versions = list(pool.map(
            lambda p: None if _is_vcs_managed(p["name"]) else _pypi_latest_version(p["name"], p["version"]),
            installed,
        ))
    return [
        {
            "name": pkg["name"],
            "installed_version": pkg["version"],
            "latest_version": latest,
            "update_available": _is_update_available(pkg["version"], latest),
            "vcs": _is_vcs_managed(pkg["name"]),
        }
        for pkg, latest in zip(installed, latest_versions)
    ]


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
# Matched on the exact `name` passed in here, not `_active_name(name)`: pyobs's own
# PYOBS_MODULE field is Path(config).stem, and start_module() invokes pyobs against
# `{name}.yaml` verbatim (leading underscore and all, for a deactivated module) -- unlike
# the file backend's log filename, which is deliberately normalized via _active_name() so
# toggling activation doesn't rename the log file. See DEV_JOURNALD_LOGS.md, Current state.

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
    ts = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000)
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


def _get_logs_journald(name: str, lines: int) -> list[str]:
    entries = _journalctl_json(["SYSLOG_IDENTIFIER=pyobs", f"PYOBS_MODULE={name}", "-n", str(lines)])
    return [_journal_entry_to_line(e) for e in entries]


def _get_log_stats_journald(name: str) -> dict:
    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    entries = _journalctl_json(["SYSLOG_IDENTIFIER=pyobs", f"PYOBS_MODULE={name}", "--since", "-24h"])
    for entry in entries:
        level = _JOURNALD_PRIORITY_TO_LEVEL.get(int(entry.get("PRIORITY", -1)))
        if level:
            counts[level] += 1
    return counts


def get_logs(name: str, lines: int = 300, filter_str: str = "") -> list[str]:
    validate_name(name)
    if _log_backend() == "journald":
        log_lines = _get_logs_journald(name, lines)
    else:
        log_file = _log_dir() / f"{_active_name(name)}.log"
        if not log_file.exists():
            return []
        result = subprocess.run(["tail", "-n", str(lines), str(log_file)], capture_output=True, text=True)
        log_lines = result.stdout.splitlines()
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def _get_all_logs_journald(names: list[str] | None, lines: int) -> list[str]:
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
        args += [f"PYOBS_MODULE={n}" for n in names]
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


def get_all_logs(names: list[str] | None = None, lines: int = 300, filter_str: str = "") -> list[str]:
    if names is not None:
        for name in names:
            validate_name(name)
    if _log_backend() == "journald":
        log_lines = _get_all_logs_journald(names, lines)
    else:
        log_lines = _get_all_logs_file(names if names is not None else list_modules(), lines)
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


def get_log_stats(name: str) -> dict:
    validate_name(name)
    if _log_backend() == "journald":
        return _get_log_stats_journald(name)

    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    log_file = _log_dir() / f"{_active_name(name)}.log"
    if not log_file.exists():
        return counts

    cutoff = datetime.now() - timedelta(hours=24)

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
    detection for `comm:` -- see DEV_EJABBERD_USER_MANAGEMENT.md's config write-back, which needs
    the same "is this locally editable or does it live in a shared fragment" answer for
    comm.password that get_resolved_acl already gives for acl:.

    Only recognizes the two patterns pyobs-web-admin's own editor can produce (see
    DEV_ACL_MATRIX.md, "Editing from the matrix"): a bare top-level `{include x.shared.yaml}`
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
    resolved = yaml.safe_load(pre_process_yaml(str(config_file))) or {}
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
    module was never expected to have an XMPP identity" (see DEV_EJABBERD_INTEGRATION.md, "Where
    it surfaces"). source is None if comm: is defined directly in the module's own file, or
    the shared fragment's name if pulled in via {include}.

    The password is needed (not just user) for DEV_EJABBERD_USER_MANAGEMENT.md's register
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
    except OSError:
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
    fuller resolution DEV_EJABBERD_USER_MANAGEMENT.md's write actions need.
    """
    return get_resolved_comm(name)[0]


def find_modules_sharing_comm_user(user: str) -> list[str]:
    """Every locally-configured module whose resolved comm.user equals user.

    Needed because DEV_EJABBERD_USER_MANAGEMENT.md's write actions (register/change_password/
    ban_account/unban_account/unregister) affect *every* module sharing an XMPP identity,
    not just whichever module's page an action was triggered from -- DEV_EJABBERD_INTEGRATION.md's
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
    managing an *existing* comm.user (see DEV_EJABBERD_USER_MANAGEMENT.md, "Modules with no
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
    risk DEV_EJABBERD_USER_MANAGEMENT.md's Design section calls out for a shared comm.user. If
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
    get_resolved_acl's source is None first -- see DEV_ACL_MATRIX.md, "Editing from the
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
    it doesn't match what was requested -- seeing DEV_ACL_MATRIX.md's note on the splice's
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
    pyobs.interfaces to check against (see DEV_ACL_MATRIX.md, "Interface-name shorthand").
    Relies on pyobs's own naming convention: interfaces are always IPascalCase, method
    names are always snake_case, so the two can never collide.
    """
    return bool(_INTERFACE_NAME_RE.match(entry))


def _acl_cell(acl: dict | None, caller: str) -> dict:
    """Computes one (target, caller) cell's value from the target's resolved acl: block,
    per the table in DEV_ACL_MATRIX.md, "What the matrix shows"."""
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
    see DEV_ACL_MATRIX.md, "Hub mode interaction". per_host is a list of (host_name, matrix)
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