Architecture
############

Technology
**********

.. list-table::
   :header-rows: 1

   * - Layer
     - Choice
   * - Backend
     - Python 3.13, Django 6, psutil
   * - WSGI server
     - Gunicorn
   * - Frontend
     - Bootstrap 5 (CDN), CodeMirror 5 (CDN), vanilla JS
   * - Package manager
     - uv
   * - Auth
     - Single-user, password hash in ``local_settings.py``, cookie sessions (no database)
   * - Hub auth
     - Pre-shared token in ``X-Hub-Token`` header; CSRF bypassed for hub requests

Design principles
*****************

* **No pyobs-core dependency.** This app communicates with ``pyobs`` directly via
  subprocess and the filesystem -- it never imports ``pyobs-core``. Where a feature would
  otherwise need something ``pyobs-core`` already implements (config ``{include}``
  resolution -- see :doc:`features/acl`), the narrow piece of logic is vendored with a note
  on which upstream version it was synced against, rather than taking the dependency.
* **No database.** Sessions are signed cookies
  (``SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"``); all persistent
  state is either a pyobs module's own YAML config or the filesystem (PID files, log
  files). A feature that needs its own persisted, app-local state has to make its own
  storage decision explicitly rather than reaching for a database that doesn't exist here.
* **No silent fallback across backends or transports.** Where a feature has two ways to do
  something -- ``mod_http_api`` vs. ``ejabberdctl`` (:doc:`ejabberd/integration`), file logs
  vs. journald (:doc:`features/logging`) -- the choice is deterministic and explicit, never
  "try one, silently fall back to the other on failure." A real failure surfaces as a real
  error.
* **One active host at a time, with explicit exceptions.** Hub-mode views default to
  operating on whichever host the sidebar's selector points at. A few pages instead
  aggregate every configured host on one page, because their entire point is a cross-host
  view (:doc:`features/acl`, the fleet-wide Overview, the fleet-wide Users page) -- see
  :doc:`hub`.

Project layout
**************

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
      pyobs_config.py           Vendored {include}/anchor resolution from pyobs-core
      middleware.py              Login-required redirect + hub token auth
      context_processors.py
      tests.py                   plain unittest.TestCase -- no database to wrap
    deploy/
      pyobs-web-admin.service    systemd unit file
    ejabberdctl-sudo.sh          sudo wrapper for EJABBERDCTL -- see ejabberd/user_management
    templates/
      base.html                  Bootstrap 5 layout with responsive sidebar
      modules/
        dashboard.html
        detail.html
        shared_detail.html       Config editor for *.shared.yaml files
        acl_matrix.html          Fleet-wide ACL matrix
        xmpp_users.html          Fleet-wide XMPP account list + write actions
      registration/
        login.html
