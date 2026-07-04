import io
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_LOG_LEVEL_RE = re.compile(r'\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]')

import psutil
import yaml
from django.conf import settings
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


def _log_backend() -> str:
    return getattr(settings, "PYOBS_LOG_BACKEND", "file")


# journald PRIORITY -> pyobs log level. Not the naively-expected {2: CRITICAL, ...} --
# logging.CRITICAL and logging.FATAL are the same int (50) in Python's logging module, so
# logging_journald.JournaldLogHandler.LEVELS's dict literal silently collapses to
# LEVELS[50] == 0, not 2. Verified live against a real emitted CRITICAL record -- see
# JOURNALD_LOGS.md, Design, for the full trail. 2/1/5 never occur in practice.
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
# toggling activation doesn't rename the log file. See JOURNALD_LOGS.md, Current state.

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
    detection for `comm:` -- see EJABBERD_USER_MANAGEMENT.md's config write-back, which needs
    the same "is this locally editable or does it live in a shared fragment" answer for
    comm.password that get_resolved_acl already gives for acl:.

    Only recognizes the two patterns pyobs-web-admin's own editor can produce (see
    ACL_MATRIX.md, "Editing from the matrix"): a bare top-level `{include x.shared.yaml}`
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
    module was never expected to have an XMPP identity" (see EJABBERD_INTEGRATION.md, "Where
    it surfaces"). source is None if comm: is defined directly in the module's own file, or
    the shared fragment's name if pulled in via {include}.

    The password is needed (not just user) for EJABBERD_USER_MANAGEMENT.md's register
    action: it registers a new XMPP account using whatever password the module's config
    *already* declares, rather than prompting for a new one -- the whole point is making an
    existing comm.user/comm.password config actually work, not choosing a fresh credential.
    source is needed for that same doc's config write-back (change_password), which must
    refuse to edit comm.password: when it resolves to a shared fragment, exactly the guard
    save_local_acl already applies to acl:.
    """
    validate_name(name)
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return None, None, None
    resolved = yaml.safe_load(pre_process_yaml(str(config_file))) or {}
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
    fuller resolution EJABBERD_USER_MANAGEMENT.md's write actions need.
    """
    return get_resolved_comm(name)[0]


def find_modules_sharing_comm_user(user: str) -> list[str]:
    """Every locally-configured module whose resolved comm.user equals user.

    Needed because EJABBERD_USER_MANAGEMENT.md's write actions (register/change_password/
    ban_account/unban_account/unregister) affect *every* module sharing an XMPP identity,
    not just whichever module's page an action was triggered from -- EJABBERD_INTEGRATION.md's
    own "third bug" documents _test and camera sharing one comm.user for real, in this exact
    fleet, not a hypothetical edge case.
    """
    return [name for name in list_modules() if get_comm_user(name) == user]


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
    managing an *existing* comm.user (see EJABBERD_USER_MANAGEMENT.md, "Modules with no
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
    risk EJABBERD_USER_MANAGEMENT.md's Design section calls out for a shared comm.user. If
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
    get_resolved_acl's source is None first -- see ACL_MATRIX.md, "Editing from the
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
    it doesn't match what was requested -- seeing ACL_MATRIX.md's note on the splice's
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
    pyobs.interfaces to check against (see ACL_MATRIX.md, "Interface-name shorthand").
    Relies on pyobs's own naming convention: interfaces are always IPascalCase, method
    names are always snake_case, so the two can never collide.
    """
    return bool(_INTERFACE_NAME_RE.match(entry))


def _acl_cell(acl: dict | None, caller: str) -> dict:
    """Computes one (target, caller) cell's value from the target's resolved acl: block,
    per the table in ACL_MATRIX.md, "What the matrix shows"."""
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

    Rows are every module list_modules() returns; columns are the union of every caller
    name mentioned in any module's resolved acl: block ("allow" keys or "deny" entries) --
    not the same set as the modules themselves, see ACL_MATRIX.md, "What the matrix
    shows". A module whose config/acl can't be resolved (bad YAML, broken {include}, ...)
    is still included as a row, with its "error" set, rather than aborting the whole scan.
    """
    targets = list_modules()
    acls: dict[str, dict | None] = {}
    sources: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    callers: set[str] = set()

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
    see ACL_MATRIX.md, "Hub mode interaction". per_host is a list of (host_name, matrix)
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