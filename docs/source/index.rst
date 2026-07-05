pyobs-web-admin
################

This is a web-based administration interface for `pyobs <https://www.pyobs.org>`_
(`documentation <https://docs.pyobs.org>`_), the robotic telescope framework. Unlike most
``pyobs_*`` packages, it is not itself a pyobs module loaded via a YAML ``class:`` entry --
it is a standalone `Django <https://www.djangoproject.com/>`_ application that manages a
fleet of pyobs module *processes* from the outside, via subprocess and the filesystem. It
has no import-time dependency on ``pyobs-core``.

From a browser it lets you start, stop, and restart modules, tail and filter their logs,
view and edit their YAML configuration files, audit and edit their access-control policy
fleet-wide, and -- optionally -- manage the XMPP accounts they connect to ejabberd with.


Feature overview
****************

* **Dashboard** -- sortable list of every module with status, RAM, CPU, uptime, and recent
  warning/error log counts; quick start/restart/stop/activate/deactivate per module plus
  bulk *Start All* / *Restart All* / *Stop All*. See :doc:`features/dashboard`.
* **Module detail** -- *Overview*, *Logs*, *Config*, and *ACL* tabs for a single module. See
  :doc:`features/dashboard` and :doc:`features/acl`.
* **Module lifecycle** -- how start/stop/restart/status/logs actually work under the hood,
  new-module creation, shared config fragments. See :doc:`features/module_management`.
* **ACL matrix** -- a fleet-wide view of every module's access-control policy, editable
  in place. See :doc:`features/acl`.
* **journald-backed logging** -- an alternative to flat log files for modules run under
  systemd. See :doc:`features/logging`.
* **Hub mode** -- control multiple remote pyobs hosts from a single browser tab. See
  :doc:`hub`.
* **ejabberd / XMPP integration** (optional) -- live connection status and, if wanted,
  account management for modules' XMPP identities. See :doc:`ejabberd/index`.
* **Overview page** and **fleet-wide Users page** -- cross-host summaries built on top of
  hub mode. See :doc:`features/dashboard` and :doc:`ejabberd/user_management`.


Getting started
****************

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   installation
   configuration

.. toctree::
   :maxdepth: 2
   :caption: Features

   features/dashboard
   features/module_management
   features/acl
   features/logging
   hub
   ejabberd/index

.. toctree::
   :maxdepth: 2
   :caption: Project

   architecture
   development

.. toctree::
   :maxdepth: 2
   :caption: API reference

   api/index
