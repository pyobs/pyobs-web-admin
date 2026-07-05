journald-backed logging
########################

By default, modules started by this app log to a flat file under ``PYOBS_LOG_DIR`` (via
``pyobs --log-file``). As an alternative, modules can instead log directly into the
systemd journal (via ``pyobs --syslog``, a flag ``pyobs-core`` already supports) -- one
switch changes both where modules are *started* to log and where the log viewer *reads*
them back from, so the two can never drift out of sync with each other.

Choosing a backend
*******************

::

    PYOBS_LOG_BACKEND = None       # None (default): auto-detect from pyobsd's own config
                                    # "file" / "journald": explicit override

If unset, the effective backend is auto-detected from ``pyobsd``'s own global config file
(``~/.config/pyobs.yaml``, ``/etc/pyobs.yaml``, or ``/opt/pyobs/storage/pyobs.yaml``, first
one found wins) -- the same file ``pyobsd`` (``pyobs-core``'s daemon manager) reads to
decide whether *it* starts modules with ``--syslog``, so this app's own choice can't
silently disagree with what ``pyobsd`` does for modules it manages itself. An explicit
``"file"``/``"journald"`` setting always wins over auto-detection.

This is a global, fleet-wide switch, not a per-module setting -- consistent with
``PYOBS_LOG_DIR``/``PYOBS_LOG_LEVEL`` already being global. Switching backends is a clean
cutover: a module's log history written under the old backend becomes invisible to the log
viewer once the switch flips. There's no dual-read or merge-both-backends logic, and log
retention/rotation is unmanaged by this app either way -- ``logrotate`` for file logs,
journald's own retention settings (``SystemMaxUse=`` etc.) for the journal.

What changes, and what doesn't
*******************************

Only the log-related pieces change: ``start_module()`` passes ``--syslog`` instead of
``--log-file``, and the log viewer shells out to ``journalctl`` instead of ``tail``ing a
file. Process management -- PID files, start/stop/restart, status checks, ``psutil``-based
resource stats -- is entirely unaffected either way.

Journal entries are queried by ``SYSLOG_IDENTIFIER=pyobs`` (the same for every module) plus
``PYOBS_MODULE=<name>`` (the module-distinguishing field), and reconstructed into the exact
same text shape the file backend already produces (timestamp, ``[LEVEL]``, module name,
``file:line``, message) -- so the level/timestamp parsing, filtering, and templates
downstream need no per-backend branching at all.

Deployment note: reading the journal cross-user requires the account running
pyobs-web-admin to have journal read access -- typically satisfied by membership in the
``adm`` or ``systemd-journal`` group on Debian/Ubuntu-family systems. A dedicated,
minimal-privilege service account may need to be added to one of those groups explicitly.
