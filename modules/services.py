import re
import subprocess
from pathlib import Path

from django.conf import settings


def _config_dir() -> Path:
    return Path(settings.PYOBS_CONFIG_DIR)


def _log_dir() -> Path:
    return Path(settings.PYOBS_LOG_DIR)


def _cmd() -> str:
    return settings.PYOBSD_COMMAND


def validate_name(name: str) -> None:
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(f"Invalid module name: {name!r}")


def list_modules() -> list[str]:
    d = _config_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def get_module_status(name: str) -> str:
    """Returns 'running', 'stopped', or 'unknown'."""
    validate_name(name)
    try:
        result = subprocess.run(
            [_cmd(), "status", name],
            capture_output=True, text=True, timeout=5,
        )
        output = (result.stdout + result.stderr).lower()
        if result.returncode == 0:
            return "stopped" if "stopped" in output or "not running" in output else "running"
        return "stopped"
    except subprocess.TimeoutExpired:
        return "unknown"
    except FileNotFoundError:
        return "unknown"


def start_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    try:
        result = subprocess.run(
            [_cmd(), "start", name],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, f"Command not found: {_cmd()!r}"


def stop_module(name: str) -> tuple[bool, str]:
    validate_name(name)
    try:
        result = subprocess.run(
            [_cmd(), "stop", name],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, f"Command not found: {_cmd()!r}"


def get_logs(name: str, lines: int = 300, filter_str: str = "") -> list[str]:
    validate_name(name)
    log_file = _log_dir() / f"{name}.log"
    if not log_file.exists():
        return []
    result = subprocess.run(
        ["tail", "-n", str(lines), str(log_file)],
        capture_output=True, text=True,
    )
    log_lines = result.stdout.splitlines()
    if filter_str:
        log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]
    return log_lines


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
