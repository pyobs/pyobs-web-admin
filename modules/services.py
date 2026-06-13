import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

_LOG_LEVEL_RE = re.compile(r'\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]')

import psutil
from django.conf import settings


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


def validate_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError(f"Invalid module name: {name!r}")


def validate_shared_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        raise ValueError(f"Invalid shared config name: {name!r}")


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


# ── PID helpers ───────────────────────────────────────────────────────────────

def _pid_file(name: str) -> Path:
    return _run_dir() / f"{name}.pid"


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
    log_file = _log_dir() / f"{name}.log"

    _run_dir().mkdir(parents=True, exist_ok=True)
    _log_dir().mkdir(parents=True, exist_ok=True)

    try:
        # pyobs daemonizes itself (python-daemon double-fork) when --pid-file is given.
        # The immediate child exits quickly; subprocess.run returns with code 0.
        result = subprocess.run(
            [
                _pyobs_exec(),
                "--pid-file", str(pid_file),
                "--log-file", str(log_file),
                "--log-level", _log_level(),
                str(config_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
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
        return {"cpu_percent": round(cpu, 1), "memory_mb": round(mem, 1), "uptime_seconds": uptime}
    except psutil.NoSuchProcess:
        _process_cache.pop(name, None)
        return None


def restart_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    stopped, msg = stop_module(name)
    ok, start_msg = start_module(name)
    return ok, start_msg


def get_logs(name: str, lines: int = 300, filter_str: str = "") -> list[str]:
    validate_name(name)
    log_file = _log_dir() / f"{name}.log"
    if not log_file.exists():
        return []
    result = subprocess.run(["tail", "-n", str(lines), str(log_file)], capture_output=True, text=True)
    log_lines = result.stdout.splitlines()
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def get_log_stats(name: str) -> dict:
    validate_name(name)
    counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    log_file = _log_dir() / f"{name}.log"
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
