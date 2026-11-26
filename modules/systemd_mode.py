"""Module control on a host in pyobsd's systemd mode (``pyobsd.mode: systemd`` in pyobsd's config file).

Every module runs as the unit ``pyobs@<module>.service``. web-admin doesn't manage processes or PID files then,
it calls ``pyobsd`` for everything: pyobsd talks to systemd, resets failed units before a start and refuses
modules still running in PID-file mode. The polkit rule from ``pyobsd setup`` lets the pyobs user (which
web-admin runs as) start, stop, restart, kill and reset-failed these units.

See specs/plans/2026-11-26-pyobsd-systemd-mode.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from django.conf import settings

# start/restart --no-wait came with it; the "mode" field in the status JSON with 2.30.0
MIN_PYOBS_CORE = "2.30.1"

# how long one `pyobsd status --json` is reused: long enough for one request or poll, which asks for status,
# stats, versions and config drift of every module
_STATUS_TTL = 1.0

# a kill waits up to 5 s for the module to be gone, the rest returns once the job is queued
_CONTROL_TIMEOUT = 30


class PyobsdError(Exception):
    """pyobsd failed, is missing, or too old for the systemd mode."""


_status_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_process_cache: dict[str, psutil.Process] = {}


def _pyobsd_exec() -> str:
    return settings.PYOBSD_EXEC


def _run(*args: str, timeout: float = _CONTROL_TIMEOUT) -> subprocess.CompletedProcess[str]:
    cmd = [_pyobsd_exec(), *args]
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PyobsdError(f"pyobsd executable not found: {_pyobsd_exec()!r}")
    except subprocess.TimeoutExpired:
        raise PyobsdError(f"pyobsd {args[0]} did not return within {timeout:.0f} s")


def invalidate() -> None:
    """Drops the cached status, so the next call sees what a control action changed."""
    global _status_cache
    _status_cache = None


def status_all() -> dict[str, dict[str, Any]]:
    """`pyobsd status --json`, by module (config file stem, so "_dome" for a disabled one). Cached for
    _STATUS_TTL. Raises PyobsdError if pyobsd fails or is older than the systemd mode."""
    global _status_cache
    now = time.monotonic()
    if _status_cache is not None and now - _status_cache[0] < _STATUS_TTL:
        return _status_cache[1]
    result = _run("status", "--json", timeout=15)
    if result.returncode != 0:
        raise PyobsdError((result.stderr or result.stdout).strip() or f"pyobsd status failed ({result.returncode})")
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        raise PyobsdError("pyobsd status --json returned no valid JSON")
    if not isinstance(entries, dict) or any(not isinstance(e, dict) or "mode" not in e for e in entries.values()):
        raise PyobsdError(f"systemd mode needs pyobs-core >= {MIN_PYOBS_CORE} on this host")
    _status_cache = (now, entries)
    return entries


def entry(name: str) -> dict[str, Any]:
    """Status of one module, {} if pyobsd doesn't list it."""
    return status_all().get(name, {})


# ── status ───────────────────────────────────────────────────────────────────


def get_module_status(name: str) -> str:
    """'running', 'starting', 'stopping', 'failed' or 'stopped', as pyobsd reports it; 'unknown' if pyobsd
    fails. A module still running in PID-file mode is 'running', with mode "pid" in get_module_info()."""
    try:
        e = entry(name)
    except PyobsdError:
        return "unknown"
    return e.get("state", "stopped")


def get_module_info(name: str) -> dict[str, Any]:
    """What only the systemd mode has, for the status API."""
    try:
        e = entry(name)
    except PyobsdError as err:
        return {"mode": "systemd", "error": str(err)}
    info: dict[str, Any] = {
        "mode": e.get("mode", "systemd"),
        "restarts": e.get("restarts", 0),
        "auto_restart": bool(e.get("restarting")),
    }
    if "error" in e:
        info["error"] = e["error"]
    return info


def get_stop_deadline(name: str) -> float | None:
    try:
        return entry(name).get("stop_deadline")
    except PyobsdError:
        return None


def _start_time(e: dict[str, Any]) -> float | None:
    """Epoch seconds the module's process started, from its uptime."""
    if not e.get("running") or "uptime" not in e:
        return None
    return time.time() - float(e["uptime"])


def get_module_stats(name: str) -> dict | None:
    """Like services.get_module_stats(), from the status JSON. CPU from psutil: a cached Process measures since
    the last poll without waiting, `pyobsd status --cpu-interval` would block every poll."""
    try:
        e = entry(name)
    except PyobsdError:
        return None
    pid = e.get("pid")
    if not e.get("running") or not pid:
        _process_cache.pop(name, None)
        return None
    cpu = 0.0
    try:
        proc = _process_cache.get(name)
        if proc is None or proc.pid != pid:
            proc = psutil.Process(pid)
            _process_cache[name] = proc
        cpu = proc.cpu_percent(interval=None)
    except psutil.Error:
        _process_cache.pop(name, None)
    return {
        "pid": pid,
        "cpu_percent": round(cpu, 1),
        "memory_mb": round(float(e.get("rss_mb", 0.0)), 1),
        "uptime_seconds": int(float(e.get("uptime", 0.0))),
    }


def running_pid_and_start(name: str) -> tuple[int, float] | None:
    """PID and start time of a running module, for the version lookup."""
    try:
        e = entry(name)
    except PyobsdError:
        return None
    start = _start_time(e)
    if not e.get("pid") or start is None:
        return None
    return e["pid"], start


def config_stale(name: str, config_file: Path) -> bool | None:
    """Whether the config file changed since the module started. Modules also start at boot, by an automatic
    restart or by `pyobsd start` from a shell, so web-admin can't snapshot the config at start. A touch without a
    change counts as stale, too."""
    try:
        start = _start_time(entry(name))
    except PyobsdError:
        return None
    if start is None:
        return None
    try:
        return os.path.getmtime(config_file) > start
    except OSError:
        return None


# ── control ──────────────────────────────────────────────────────────────────


def _control(*args: str) -> tuple[bool, str]:
    # a pyobsd without the systemd mode, or one older than MIN_PYOBS_CORE, would do the wrong thing
    try:
        status_all()
        result = _run(*args)
    except PyobsdError as err:
        return False, str(err)
    finally:
        invalidate()
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def start_module(name: str) -> tuple[bool, str]:
    return _control("start", "--no-wait", name)


def stop_module(name: str) -> tuple[bool, str]:
    return _control("stop", "--no-wait", name)


def kill_module(name: str) -> tuple[bool, str]:
    """SIGKILL and a stop, so systemd doesn't restart it."""
    return _control("stop", "--kill", name)


def restart_module(name: str) -> tuple[bool, str]:
    """Returns once the restart is queued. The module shows as stopping, then starting, then running."""
    return _control("restart", "--no-wait", name)
