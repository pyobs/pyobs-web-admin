# Module shutdown: stopping state, kill, background restart

## Status

Implemented (old PID-file mode). Needs pyobs-core >= 2.29 on the host for the full behavior (stop file and the
module's own watchdog), see `pyobs-core/specs/plans/2026-11-25-pyobsd-systemd-mode.md`. The systemd mode from that
plan is not part of this.

## Motivation

A module's shutdown can take long: a cooled camera warms up in `close()` (`CoolingRampMixin`), 37 min was seen on a
camera host. web-admin used to send SIGTERM, wait 5 s and then SIGKILL, which cut the warm-up. Restart called stop and
then start right away, so with a long stop the start failed with "already running".

## Current state before this

- `stop_module`: SIGTERM, 5 s, SIGKILL.
- `restart_module`: `stop_module` then `start_module`, in the request.
- `get_module_status`: `running` or `stopped`.

## Design

pyobs-core (since 2.29, `pyobs.utils.shutdown`) writes `<pid file>.stopping` on the first signal and removes it at
the end of its shutdown. The file holds a deadline (epoch seconds, float) until which the module may still take.
On the first signal the module also starts a watchdog that ends the process after that deadline (or 30 s,
`--stop-timeout`), plus 5 s. Further signals are ignored. web-admin reads that file and otherwise needs no waiting:

- **Status**: live PID + stop file = `stopping`. The status APIs add `stop_deadline` and `restarting`. `stopping`
  counts as running for stats, the dashboard counters, the fleet overview and the sidebar order.
- **Stop**: SIGTERM and return. No SIGTERM if the stop file already exists (a module with an older pyobs-core
  would get killed by a second one).
- **Kill**: SIGKILL right away, also while stopping. Waits up to 5 s; a process that survives (stuck in a driver
  call) keeps its PID file, so it can't be started twice. The UI shows the button only while stopping, with a
  confirmation.
- **Restart**: a stopped module is just started. Otherwise a detached job, `manage.py restart_module <name>`
  (`services.run_restart`), sends SIGTERM, waits until the module is gone and starts it. It waits for the deadline
  from the stop file, at least 30 s, then sends SIGKILL, as a fallback for modules without the watchdog. The job
  runs in its own session and survives a restart of web-admin (`KillMode=process`). Its lock is
  `<run>/<name>.restart.json` (PID and create time, a zombie counts as done), its output goes to
  `<run>/<name>.restart.log`. `restart_module` checks for a running job and writes the lock under an flock on
  `<run>/<name>.restart.flock`, so two requests at the same moment can't both start a job. A manual `pyobsd restart`
  doesn't know this lock, in that case one of the two starts fails with "already running".
- **Start** is refused while a module is stopping or a restart job runs. It runs pyobs with stdin from
  `/dev/null`: python-daemon doesn't detach if stdin is a socket (e.g. under `manage.py runserver`), and the
  start then runs into its 10 s timeout, which also kills the module.

The stop file format is shared with pyobs-core (`STOP_FILE_SUFFIX`, `read_stop_deadline`). If it changes there,
`services._stop_file`/`_read_stop_deadline` have to follow.

Why not call `pyobsd`: in the old mode, pyobsd reads its own config file for paths and the user to switch to, which
can differ from web-admin's settings, so every call would need `-c/-r/-l --chuid ""`. With the module's own
watchdog, a stop needs no waiter anymore, so there is little to share. The systemd mode will call `pyobsd`.

## Open questions

None currently.
