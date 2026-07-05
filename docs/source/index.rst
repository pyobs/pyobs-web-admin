pyobs-web-admin
################

This is a web-based administration interface for `pyobs <https://www.pyobs.org>`_
(`documentation <https://docs.pyobs.org>`_), the robotic telescope framework. Unlike most
``pyobs_*`` packages, it is not itself a pyobs module loaded via a YAML ``class:`` entry --
it is a standalone `Django <https://www.djangoproject.com/>`_ application that manages a
fleet of pyobs module *processes* from the outside, via subprocess and the filesystem. It
has no import-time dependency on ``pyobs-core``.

From a browser it lets you start, stop, and restart modules, tail and filter their logs,
and view and edit their YAML configuration files.


Features
********

* **Dashboard** -- sortable list of all modules with status, RAM, CPU, uptime, and recent
  warning/error log counts; quick start/restart/stop/activate/deactivate per module plus
  bulk *Start All* / *Restart All* / *Stop All*.
* **Module detail** -- *Overview*, *Logs* (live tail with text and time-range filtering),
  *Config* (YAML editor with ``{include}`` links), and *ACL* (point-and-click editor for a
  module's ``acl:`` block) tabs.
* **New module** -- creates a minimal starter ``<name>.yaml`` and opens it on the Config tab.
* **Shared configs** -- ``*.shared.yaml`` fragments get their own sidebar section and editor.
* **Overview page** (``/overview/``) -- fleet-wide, one row per configured host.
* **Hub mode** -- control multiple remote pyobs hosts from a single browser tab; see
  `Hub mode`_ below.
* **ejabberd / XMPP status and user management** (optional) -- see `ejabberd integration`_
  below.
* **Responsive** -- usable on mobile, with a slide-in sidebar.

See the project's `README <https://github.com/pyobs/pyobs-web-admin>`_ for a full,
up-to-date feature list.


Installation
************

Development
============

::

    git clone https://github.com/pyobs/pyobs-web-admin.git
    cd pyobs-web-admin
    uv sync
    uv run python manage.py runserver

Then create :file:`pyobs_web_admin/local_settings.py` (see Configuration_ below).

Production
==========

::

    cd /opt/pyobs
    git clone https://github.com/pyobs/pyobs-web-admin.git
    cd pyobs-web-admin
    uv sync

Configure :file:`pyobs_web_admin/local_settings.py`, then install the systemd unit and an
nginx reverse proxy in front of gunicorn::

    cp deploy/pyobs-web-admin.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now pyobs-web-admin

The minimum settings required for production are ``DEBUG``, ``SECRET_KEY``,
``ALLOWED_HOSTS``, ``ADMIN_USERNAME``, ``ADMIN_PASSWORD_HASH``, and ``PYOBS_EXEC`` -- see
Configuration_ below, and the README's *Production setup* section for the nginx config
and the commands to generate a secret key and password hash.


Configuration
**************

All runtime configuration lives in ``pyobs_web_admin/local_settings.py``, which is not
committed to version control::

    # Django
    SECRET_KEY = "..."           # required in production
    DEBUG = False                # set True only in development
    ALLOWED_HOSTS = ["*"]        # restrict to hostname/IP in production

    # Authentication
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD_HASH = "pbkdf2_sha256$..."

    # pyobs paths
    PYOBS_EXEC = "/opt/pyobs/venv/bin/pyobs"   # path to the pyobs executable
    PYOBS_CONFIG_DIR = "/opt/pyobs/config"      # directory containing *.yaml module configs
    PYOBS_LOG_DIR = "/opt/pyobs/log"            # directory containing *.log files
    PYOBS_RUN_DIR = "/opt/pyobs/run"            # directory for PID files
    PYOBS_LOG_LEVEL = "info"                    # log level passed to pyobs on start
    PYOBS_LOG_BACKEND = None                    # None: auto-detect "file" vs "journald"

    # Hub (optional -- see Hub mode section)
    HUB_TOKEN = ""
    HUB_HOSTS = []

    # ejabberd integration (optional -- see ejabberd integration section)
    EJABBERD_ENABLED = False
    EJABBERD_HOST = "localhost"
    EJABBERD_DOMAIN = ""
    EJABBERD_API_URL = "http://127.0.0.1:5281/api"
    EJABBERDCTL = "ejabberdctl"

See the README's *Configuration* section for the full annotated reference, and
`JOURNALD_LOGS.md <https://github.com/pyobs/pyobs-web-admin/blob/main/JOURNALD_LOGS.md>`_
for how ``PYOBS_LOG_BACKEND`` auto-detection works.


Hub mode
********

pyobs-web-admin can act as a hub to control multiple remote pyobs hosts from a single
browser session. On the hub, list the remote hosts in ``local_settings.py``::

    HUB_HOSTS = [
        {"name": "obs1", "url": "http://obs1:8765", "token": "shared-secret"},
        {"name": "obs2", "url": "http://obs2:8765", "token": "another-secret"},
    ]

On each remote host, set the matching ``HUB_TOKEN`` so it accepts requests carrying that
token in the ``X-Hub-Token`` header. A valid token bypasses the normal session/CSRF check
for that request, so keep it a long, secret value.


ejabberd integration
*********************

If the ejabberd server a module's XMPP comm layer connects through runs on the same host,
pyobs-web-admin can show live connection state alongside the process status it already
tracks -- a dashboard summary tile, a per-module connected/not-connected indicator, and a
session/last-seen/registered-account block on each module's own page. A module with no
``comm:`` block is skipped entirely. Enable it in ``local_settings.py``::

    EJABBERD_ENABLED = True
    EJABBERD_HOST = "localhost"           # or a HUB_HOSTS name
    EJABBERD_DOMAIN = "your-xmpp-domain"
    EJABBERD_API_URL = "http://127.0.0.1:5281/api"

This talks to ejabberd's HTTP admin API (``mod_http_api``), which needs a small addition
to ejabberd's own config -- see `EJABBERD_INTEGRATION.md
<https://github.com/pyobs/pyobs-web-admin/blob/main/EJABBERD_INTEGRATION.md>`_ for the
full listener/``api_permissions`` snippet and its security implications.

Building on this, ``EJABBERD_ENABLED = True`` also adds write actions (register, reset
password, ban/unban, unregister, kick) via ``ejabberdctl``, either from a module's own
Overview tab or from the fleet-wide **Users** page (``/xmpp-users/``). See
`EJABBERD_USER_MANAGEMENT.md
<https://github.com/pyobs/pyobs-web-admin/blob/main/EJABBERD_USER_MANAGEMENT.md>`_ for the
sudoers setup this requires and its security implications.


Project layout
***************

::

    pyobs_web_admin/
      settings.py               Django project settings
      local_settings.py.example Template for local overrides (not committed)
      urls.py                   URL config
    modules/
      services.py               All pyobs process and filesystem logic
      views.py                  HTML pages + JSON API endpoints
      proxy.py                  HTTP client for hub -> remote host calls
      ejabberd.py               mod_http_api (status) + ejabberdctl (user management) client
      middleware.py              Login-required redirect + hub token auth
      context_processors.py
    deploy/
      pyobs-web-admin.service    systemd unit file
    templates/
      base.html                 Bootstrap 5 layout with responsive sidebar
      modules/                  dashboard, detail, shared_detail, xmpp_users
      registration/login.html


API reference
**************

The narrative sections above cover configuration and deployment; this section documents
the Python modules directly, generated from their docstrings.

modules.services
==================
.. automodule:: modules.services
   :members:
   :show-inheritance:

modules.views
==============
.. automodule:: modules.views
   :members:
   :show-inheritance:

modules.ejabberd
==================
.. automodule:: modules.ejabberd
   :members:
   :show-inheritance:

modules.proxy
===============
.. automodule:: modules.proxy
   :members:
   :show-inheritance:
