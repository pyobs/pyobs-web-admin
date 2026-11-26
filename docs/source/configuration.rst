Configuration
#############

All runtime configuration lives in :file:`pyobs_web_admin/local_settings.py`, which is not
committed to version control (it's in :file:`.gitignore` since it typically holds secrets).
If the file doesn't exist, the defaults in :file:`pyobs_web_admin/settings.py` apply --
enough to run in development, not enough (and not safe) for production.

Django core
***********

::

    SECRET_KEY = "..."           # required in production -- see Installation
    DEBUG = False                # set True only in development
    ALLOWED_HOSTS = ["*"]        # restrict to hostname/IP in production

    # HTTPS (enable once TLS is in place)
    # SESSION_COOKIE_SECURE = True
    # CSRF_COOKIE_SECURE = True

Sessions are signed cookies (``SESSION_ENGINE =
"django.contrib.sessions.backends.signed_cookies"``) -- there is no database anywhere in
this app, by design (see :doc:`architecture`).

Authentication
**************

::

    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD_HASH = "pbkdf2_sha256$..."   # see generation command in Installation

There is exactly one admin identity, no per-user accounts or roles. Anyone who can log in
can do anything the UI exposes, including destructive actions -- see the confirmation-UX
notes in :doc:`ejabberd/user_management` for how the app compensates for having no access
tiers of its own.

pyobs paths
***********

::

    PYOBS_EXEC = "/opt/pyobs/venv/bin/pyobs"   # path to the pyobs executable
    PYOBS_CONFIG_DIR = "/opt/pyobs/config"      # directory containing *.yaml module configs
    PYOBS_LOG_DIR = "/opt/pyobs/log"            # directory containing *.log files
    PYOBS_RUN_DIR = "/opt/pyobs/run"            # directory for PID files
    PYOBS_LOG_LEVEL = "info"                    # log level passed to pyobs on start
    PYOBS_LOG_BACKEND = None                    # None: auto-detect "file" vs "journald"
                                                 # -- see the Logging page
    PYOBSD_EXEC = "/opt/pyobs/venv/bin/pyobsd" # path to pyobsd, only used in systemd mode

systemd mode
************

If pyobsd's config file (:file:`/etc/pyobs.yaml`, written by ``pyobsd setup``) sets ``mode: systemd`` in its
**pyobsd** section, every module runs as the systemd unit ``pyobs@<module>.service``. web-admin detects that from
the same file and then calls ``PYOBSD_EXEC`` for everything, instead of starting pyobs itself and reading PID
files:

- Start, Stop and Restart run ``pyobsd start|stop|restart --no-wait <module>`` and return right away. Kill runs
  ``pyobsd stop --kill <module>``.
- The status comes from ``pyobsd status --json``, once per poll. Besides **running**, **stopping** and
  **stopped**, a module can be **starting** (not open yet) or **failed** (stopped after an error, "restarting"
  while systemd waits to start it again), with the number of restarts. A module still running in PID-file mode
  is shown as "running (pid mode)" and can only be stopped with ``pyobsd --mode pid stop``.
- A config file counts as changed if it was modified after the module started, since modules also start at
  boot, after an error and from a shell.
- With the journald backend, the logs are read by unit (``journalctl -u pyobs@<module>.service``), so they also
  show stderr and systemd's own lines about the module, see :doc:`features/logging`.

It needs pyobs-core 2.30.1 or later on the host. With an older pyobsd, the status shows that as an error and the
buttons don't work. web-admin runs as the pyobs user, which the polkit rule from ``pyobsd setup`` allows to control
the units.

Hub mode
********

::

    HUB_TOKEN = ""      # deprecated single-token form, if it's a spoke -- see HUB_CLIENTS
    HUB_CLIENTS = []    # named tokens this instance accepts from external callers, if it's a spoke
    HUB_HOSTS = []      # remote hosts this instance controls, if it's a hub

See :doc:`hub` for the full picture -- which side sets which of these, and what the token
actually protects.

ejabberd integration (optional)
********************************

::

    EJABBERD_ENABLED = False                       # show XMPP status/management at all
    EJABBERD_HOST = "localhost"                     # "localhost" or a HUB_HOSTS name
    EJABBERD_DOMAIN = ""                            # the XMPP vhost ejabberd serves
    EJABBERD_API_URL = "http://127.0.0.1:5281/api"  # mod_http_api base URL (read path)
    EJABBERDCTL = "ejabberdctl"                     # ejabberdctl binary/wrapper (write path)

See :doc:`ejabberd/index` for what each setting actually gates, the ejabberd-side
configuration required, and the security model for both the read path (``mod_http_api``)
and the write path (``ejabberdctl``).
