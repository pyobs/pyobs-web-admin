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
