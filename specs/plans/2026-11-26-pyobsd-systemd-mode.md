# Plan: web-admin on hosts in pyobsd's systemd mode

Status: implemented. Released in pyobs-web-admin 2.5.0, needs pyobs-core 2.30.1 (`pyobsd start/restart
--no-wait`, see Prerequisite). Control and status (#126) and logs by unit (#127), checked on a host (#128). Open:
a hub with one host in each mode, not checked yet.
Repos: pyobs-web-admin, pyobs-core

Part of `../../../pyobs-core/specs/plans/2026-11-25-pyobsd-systemd-mode.md` (systemd mode of pyobsd, released
in pyobs-core 2.30.0). That plan has the design of the mode itself. This one is the web-admin side, which the
core plan only outlines in its section "pyobs-web-admin (deferred)".

## Problem

On a host in systemd mode, web-admin can't start modules: its Start runs `pyobs --pid-file`, which pyobs-core
refuses when `/etc/pyobs.yaml` sets `pyobsd.mode: systemd`. Status, stop, kill and restart read and signal PIDs
from PID files that don't exist in that mode. So web-admin shows every module as stopped and can't control
them.

## Approach

In systemd mode (`_pyobsd_config().get("mode") == "systemd"`), web-admin calls `pyobsd` for everything instead
of managing processes itself. pyobsd talks to systemd, so the reset of failed units, the refusal of modules
still running in PID-file mode and the polkit rule are all in one place. The PID-file code path stays as it is.

web-admin runs as the pyobs user, which the polkit rule allows to start, stop, restart, kill and reset-failed
`pyobs@*.service`. `systemctl` uses the system bus, so no extra environment is needed.

### Prerequisite: `--no-wait` for start and restart (pyobs-core)

In systemd mode, `pyobsd start` waits until the module sent `READY=1` (up to the unit's 120 s
`TimeoutStartSec`), and `pyobsd restart` also waits for the whole stop first, a camera warm-up included.
web-admin calls pyobsd from a gunicorn request, and gunicorn kills a worker after 30 s by default
(`deploy/pyobs-web-admin.service` doesn't change it). So pyobs-core gets `--no-wait` for `start` and `restart`,
like `stop --no-wait`: in systemd mode it adds `--no-block` to `systemctl`, so pyobsd returns once the job is
queued. In PID-file mode, `--no-wait` is accepted and changes nothing for now (web-admin doesn't call pyobsd
in that mode). Released as pyobs-core 2.30.1.

Considered and rejected:

- web-admin runs `pyobsd start/restart` as a detached job, like today's restart job: no core change, but
  web-admin then has to collect exit codes and errors from the job and keep its locks.
- web-admin calls `systemctl start --no-block` itself: duplicates pyobsd's reset-failed and its refusal of
  PID-file modules.

With `--no-block`, a start that fails later (e.g. the module crashes in `open()`) isn't reported by the call.
web-admin shows it through the state (**failed**) and the log, like any other failure.

### Settings

- `PYOBSD_EXEC` (default `"pyobsd"`), like `PYOBS_EXEC`. Only used in systemd mode.

### Status

`pyobsd status --json` once per request or poll cycle, cached, so the module list makes one subprocess call
instead of one per module. Per module it has `mode`, `enabled`, `running`, `state`, `stop_deadline`, `pid`,
`restarts`, `restarting`, `uptime` and `rss_mb`. Not `--cpu-interval`: it samples for that interval on every
call, which would slow down every poll.

- `get_module_status` maps `state` directly: `running`, `starting`, `stopping`, `failed`, `stopped`. A module
  with `"mode": "pid"` (still running from before the switch) is **running (pid mode)**.
- The UI gets the new states **starting** and **failed** (with "restarting" and the number of restarts).
  `views._ALIVE` gets `starting`. The hub shows what each host reports, so a hub in PID-file mode with hosts in
  systemd mode must accept the new states, and vice versa.
- `get_stop_deadline` reads `stop_deadline` from the JSON.
- `get_module_stats` takes `pid`, `rss_mb` and `uptime` from the JSON instead of the PID file. CPU stays with
  psutil's cached `Process` (by that `pid`), which measures since the last poll without waiting.
- No version comparison: if the JSON has no `mode` field, pyobsd on the host is older than 2.30.0. web-admin
  then shows an error ("systemd mode needs pyobs-core >= 2.30.1") and refuses start, stop, kill and restart.
  2.30.0 (has `mode`, no `--no-wait`) isn't detected. pyobsd fails with an unknown option, and web-admin shows
  that error. The docs state 2.30.1 as minimum.

### Control

- `start_module`: `PYOBSD_EXEC start --no-wait <name>`, returns its exit code and output.
- `stop_module`: `PYOBSD_EXEC stop --no-wait <name>`.
- `kill_module`: `PYOBSD_EXEC stop --kill <name>` (SIGKILL plus a stop, so systemd doesn't restart it).
- `restart_module`: `PYOBSD_EXEC restart --no-wait <name>`. No detached job and no restart lock in this mode,
  `restart_in_progress` is the `stopping`/`starting` state.
- `activate_module`/`deactivate_module`: rename as today. The generator picks up the boot state at the next
  boot, nothing else to do. `deactivate_module` stops a running module with `stop --no-wait`, as today.
- Module names with characters that aren't valid in a unit name: pyobsd skips them and the JSON has an `error`
  for them. web-admin shows that error and offers no controls.

### Config drift and versions

- `config_stale`: web-admin's hash snapshot is only written when web-admin starts a module. In systemd mode,
  modules are also started at boot, by automatic restarts and by `pyobsd start` from a shell. So in this mode,
  compare the config file's mtime with the process start time (now minus `uptime`). That flags a touch without
  a change as stale, which is acceptable.
- `get_module_versions`: cache by `pid` from the JSON, bound the journal search with `--since` at the process
  start time (now minus `uptime`), as today with psutil's `create_time()`.

### Logs: match by unit

Today the journald backend matches `SYSLOG_IDENTIFIER=pyobs` and `PYOBS_MODULE=<name>`, plus the comm name
(#59). In systemd mode, the module and All Logs views match by unit instead: `journalctl -u
pyobs@<name>.service`, one `-u` per selected module. "All" is one `-u` per module from the status JSON (every
config plus every running unit), not the glob `-u 'pyobs@*.service'`: journalctl resolves a glob against every
unit name in the journal, which took 79 s on a desktop with a 4 GB journal, while three explicit `-u` took 63 ms
(also 30 ms with `--grep` and `--since`). A unit whose config is gone and that doesn't run anymore isn't in
"all" then. `--since`, `--until`, `--grep` and `-n` stay as they are.

That also shows what only the unit has: tracebacks and other output on stderr (e.g. a crash before logging is
set up), output of C libraries, and systemd's own lines ("Main process exited, status=1", "Scheduled restart
job", "Start request repeated too quickly"). These are what you need for a **failed** module. Matching by unit
also makes the merge of config name and comm name unneeded in this mode, since every line of the process has
the unit.

- `_journal_entry_to_line`: lines without `PYOBS_MODULE` take the module name from `_SYSTEMD_UNIT` or `UNIT`
  and have no `file:line`.
- Lines from stderr come with priority INFO (6). A traceback on stderr shows as INFO and isn't counted as an
  error by `get_log_stats`. Acceptable for now.
- Log lines from before the switch (PID-file mode) don't have the unit and don't show in this view anymore.
- Log backend `file` (pyobsd's `syslog: false`) in systemd mode: the module writes its log file as before and
  the file backend reads it. stderr and systemd's lines are only in the journal then. Not covered, the docs
  recommend journald for systemd mode.

## Steps

pyobs-core:

- [x] `pyobsd start --no-wait`, `pyobsd restart --no-wait` (systemd: `systemctl --no-block`), tests, docs,
      CHANGELOG.
- [x] Release pyobs-core 2.30.1 (pyobs-core#1120).

pyobs-web-admin:

- [x] Check the log assumptions (on a desktop, systemd 259, 4 GB journal): all 300 natively sent lines of the
      last day carry `_SYSTEMD_UNIT`. `-u` with a glob works but takes 79 s, so "all" uses explicit units (see
      "Logs: match by unit"). The same glob in `pyobsd logs` without a module: pyobs-core#1122. Still to
      check on a host with a pyobs module.
- [x] `PYOBSD_EXEC` setting, mode check, cached `pyobsd status --json`, error for a pyobsd without `mode`.
- [x] Status, stats, stop deadline from the JSON. States **starting**, **failed** (restarts), **running (pid
      mode)** in the module list, detail page and hub.
- [x] Start, stop, kill, restart through pyobsd. No restart job in this mode.
- [x] `config_stale` by mtime and start time, `get_module_versions` by `pid` and start time.
- [x] Logs by unit for the module view, All Logs and log stats. Lines without `PYOBS_MODULE`.
- [ ] Tests: each service function in both modes, with `subprocess.run` mocked (pyobsd's JSON and exit codes).
- [ ] Docs (`docs/source/installation.rst`): systemd mode, `PYOBSD_EXEC`, minimum pyobs-core 2.30.1. CHANGELOG.
- [x] Check on a host in systemd mode (Debian 13, systemd 257, 921 MB journal; pyobs-core 2.30.1 in a test
      venv, web-admin develop at #127 run by `runserver` as the pyobs user, dummy modules; the host's own
      modules stayed in PID-file mode, all removed afterwards except polkitd):
      - Start (4 modules), stop, restart and kill return in 140-410 ms. States: running, starting (a module
        that needs 60 s to open, with Kill), stopping with its deadline ("stopping (up to 84 s)"), "failed
        (restarting)" with the restart count, stopped after kill without an automatic restart.
      - A module crashing in `open()`: restarts until the start limit (5), then Start from web-admin resets
        it (polkit, as pyobs) and starts it again. Stop works while it waits for a restart, and on a
        starting module.
      - Logs by unit: pyobs's lines, the stderr line (INFO, no file:line), the traceback and systemd's lines
        ("Main process exited", "Failed with result" as WARNING, "Failed to start" as ERROR, which the log
        stats count). systemd's lines carry their own CODE_FILE (e.g. `job.c:818`).
      - Config drift after a touch, versions from the journal, deactivate stops and renames.
      - All Logs over 4 units: 83 ms, with a search 62 ms. For comparison, `journalctl -u 'pyobs@*.service'`
        took 1.3 s there and 7 ms with explicit units.
      - Not checked: a hub with one host in each mode.
- [x] Release pyobs-web-admin (2.5.0).
