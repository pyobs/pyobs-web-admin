How modules are managed
########################

This page documents the mechanics behind the Dashboard/module-detail controls described in
:doc:`dashboard` -- what actually happens on disk and at the process level when you click
Start, Stop, or Restart.

Discovery
*********

Every ``*.yaml`` file directly in ``PYOBS_CONFIG_DIR`` is treated as a module, except
``*.shared.yaml`` files, which are listed separately as shared config fragments (see
:doc:`dashboard`). A module's *name* is its filename stem -- ``camera.yaml`` is the module
``camera``.

Activate / deactivate
*********************

Deactivating a module stops it (if running) and renames its config from ``name.yaml`` to
``_name.yaml``; activating renames it back. Deactivated modules are excluded from *Start
All* and *Restart All*, and grouped under their own heading when the dashboard is sorted by
status.

Start
*****

Runs::

    pyobs --pid-file <run>/<name>.pid --log-file <log>/<name>.log --log-level <level> <config>

``pyobs`` daemonises itself (double-fork, via ``python-daemon``) -- this app runs it as a
plain subprocess and doesn't itself manage a long-lived child process. If the effective log
backend is ``"journald"`` (see :doc:`logging`), ``--syslog`` is passed instead of
``--log-file``, and nothing else about the invocation changes.

Stop
****

Sends ``SIGTERM`` to the PID recorded in the module's PID file; falls back to ``SIGKILL``
after 5 seconds if the process hasn't exited.

Restart
*******

Stop, then start -- no special-cased "reload" path.

Status
******

Checks whether the process for the stored PID is alive via ``os.kill(pid, 0)`` -- a
zero-cost existence probe, not an actual signal delivery.

Resource usage
**************

Uptime, CPU%, and RSS memory are read via ``psutil`` on every status poll (the dashboard's
10 second refresh and the module page's own polling) -- there is no separate, slower
"stats" cadence.

Logs
****

Read from ``PYOBS_LOG_DIR``'s flat files by default, or from the systemd journal via
``journalctl`` if the effective log backend is ``"journald"`` -- see :doc:`logging` for the
full detail. The log viewer and per-level counts work identically either way; nothing above
the read layer (templates, the level/timestamp regexes, filtering) needs to know which
backend produced a given line.

Log counts
**********

Per-level message counts (DEBUG / INFO / WARNING / ERROR / CRITICAL) for the last 24h. On
the file backend this uses a binary search over the log file by byte offset to find the
start of the 24h window, rather than reading the whole file just to count lines.
