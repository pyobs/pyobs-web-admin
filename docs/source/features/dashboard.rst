Dashboard and module detail
############################

Dashboard
*********

The Dashboard (``/``) is the per-host operational control surface -- a sortable table of
every module ``list_modules()`` finds in ``PYOBS_CONFIG_DIR``, refreshed on a 10 second
poll:

* **Status** -- a colour-coded dot (running / stopped / deactivated), derived from
  ``psutil``-checking the PID stored in that module's PID file.
* **RAM / CPU / uptime** -- read live via ``psutil`` on every poll; blank for a stopped
  module.
* **Warning/error log counts** -- per-module count of WARNING+ messages in the last 24h,
  highlighted in colour if non-zero. See :doc:`logging` for how these counts are computed
  without reading a whole log file.
* **XMPP indicator** (only if ``EJABBERD_ENABLED``) -- a small connected/not-connected icon
  per module row, and a summary tile alongside the Total/Running/Stopped/RAM/CPU tiles. See
  :doc:`../ejabberd/integration`.
* Sorting by any column header groups rows under *Running / Stopped / Deactivated*
  headings; a reset icon restores the default (config-file) order.
* **Quick actions** per row -- start, restart, stop, activate/deactivate -- plus bulk
  *Start All*, *Restart All*, *Stop All* across every non-deactivated module. Modules whose
  config filename starts with ``_`` (i.e. deactivated) are excluded from the bulk actions.
* Responsive: on a narrow viewport the table collapses to status dot + name + log counts +
  actions, wrapped in a horizontally-scrolling container rather than overflowing the page.

Module detail
*************

Clicking a module opens its detail page, with four tabs:

Overview
========

Current status, PID, uptime, CPU and memory usage, per-level (DEBUG/INFO/WARNING/ERROR/
CRITICAL) message counts for the last 24h, and the start/restart/stop/activate/deactivate
controls. If ``EJABBERD_ENABLED`` and the module has a ``comm.user``, a session block shows
connected-since/IP/connection type (live) or last-seen (not connected) -- see
:doc:`../ejabberd/integration`.

Logs
====

A live log tail with a free-text filter and a time-range filter (clicking a line sets the
range), colour-coded by severity, auto-refreshing. Reads from either flat log files or the
systemd journal depending on the effective log backend -- see :doc:`logging`; the viewer
behaves identically either way.

Config
======

A YAML editor (CodeMirror, syntax-highlighted) for the module's own config file, with
``{include ...}`` lines rendered as clickable links to the referenced shared fragment (see
"Shared configs" below). Saves write the raw text back as-is -- this app never round-trips
a whole config file through a generic YAML parser, since a config can contain bare
``{include ...}`` lines that aren't valid standalone YAML on their own.

ACL
===

A point-and-click editor for the module's ``acl:`` block -- see :doc:`acl` for the full
picture, including the fleet-wide matrix view this tab is one of two editing surfaces for.


New module
**********

A "+" icon next to the sidebar's *Modules* heading opens ``/modules/new/`` -- a single name
field. On submit, it writes a minimal starter ``<name>.yaml`` (just a ``class:`` key) and
navigates straight to that module's own Config tab to fill in the rest. ``PYOBS_CONFIG_DIR``
is created automatically if it doesn't exist yet. Creating a module under a name that
already exists is rejected rather than overwriting the existing file.


Shared configs
**************

``*.shared.yaml`` files in ``PYOBS_CONFIG_DIR`` are config *fragments* meant to be pulled
into one or more modules' own configs via ``{include name.shared.yaml}``, rather than
modules in their own right -- they're excluded from module discovery and never get
start/stop controls. They get their own sidebar section and their own editor (same
YAML-highlighted CodeMirror view, no lifecycle controls), since they're a first-class
editing target even though nothing runs them directly.


Fleet-wide Overview page
*************************

``/overview/`` is a separate page from the per-host Dashboard: one row per host configured
in ``HUB_HOSTS`` (plus the local host), each showing whether it's reachable, its
running/stopped/total module counts, and aggregate CPU/RAM -- with the host's name linking
into *that host's own* Dashboard. An unreachable host is shown as a warning banner and
excluded from the aggregate numbers, rather than silently hidden. This page deliberately has
**no bulk or per-module actions of its own** -- a fleet-wide "Stop All" button one click away
from a summary view is a real footgun; anything you want to *do* to a module happens on that
host's own Dashboard, one click further in. See :doc:`../hub` for how the underlying
cross-host querying works.
