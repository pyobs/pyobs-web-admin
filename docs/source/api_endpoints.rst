HTTP API reference
###################

Every action the UI takes (start/stop a module, tail logs, edit config, manage ACLs and
XMPP accounts) goes through a plain JSON endpoint under ``/api/...``. There is no separate
"external" API -- the same endpoints the browser's own JS calls are exactly what an
external caller (a script, another service, or another pyobs-web-admin instance acting as
a hub) calls too, authenticated the same way. See :doc:`hub` for how authentication works
(``X-Hub-Token`` vs. the browser session/CSRF cookie) and how to configure
``HUB_CLIENTS``/``HUB_TOKEN`` for external callers.

Conventions
***********

* All request and response bodies are JSON; ``POST`` endpoints that take a body expect
  ``Content-Type: application/json``.
* Responses are always a JSON object. Most write endpoints return ``{"success": true}`` or
  ``{"success": false, "error": "..."}`` (a few older ones use ``{"ok": ...}``/
  ``{"error": ...}`` instead -- called out below where that's the case). A non-2xx status
  usually accompanies the error case, but check the body's own flag rather than only the
  HTTP status.
* ``404`` means the path parameter (module/shared-config name) doesn't exist; ``502`` means
  a proxied call to another host failed; ``400`` means the request itself was invalid
  (bad JSON, missing field, invalid ACL shape, ...).

Host routing
************

Most endpoints below call the view layer's ``_active_host`` helper, which resolves
``request.session["active_host"]`` (defaulting to ``"localhost"``, i.e. this instance
itself) and, if it names a remote host, transparently proxies the whole request there
instead of answering locally. This is session state, so it only applies to browser
requests -- an external caller authenticating via ``X-Hub-Token`` has no session, and
therefore always gets **this instance's own, local answer**, regardless of what
``HUB_HOSTS`` this instance may itself be configured with. To act on a different host in
the fleet, call that host's own API directly (with a token it accepts) rather than relying
on this instance to route there for you.

A handful of write endpoints (``api_acl``'s ``POST``, and the module-scoped ejabberd write
endpoints) instead accept an explicit ``"host"`` field in the JSON body, defaulting to
``"localhost"`` when absent -- this lets a fleet-wide page (or an external caller) target a
specific host regardless of session state. Where that's supported, it's noted below.

Fleet-wide endpoints (``api_acl_matrix``, ``api_comm_user_map``) have no host-awareness at
all: they always answer for this instance's own local modules only. Aggregating across a
whole fleet is done by whichever caller queries every host's copy of these and merges the
results (this is what the hub's own ACL matrix / Users pages do).

Status
******

.. list-table::
   :header-rows: 1
   :widths: 10 30 60

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/statuses/``
     - All modules. ``{"modules": [{"name", "status", "stats", "comm_user"}, ...]}``.
       ``stats`` is ``null`` unless ``status == "running"``, otherwise
       ``{"pid", "cpu_percent", "memory_mb", "uptime_seconds"}``.
   * - GET
     - ``/api/modules/<name>/status/``
     - One module. ``{"status": ..., "stats": ...|null}``.

Control
*******

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - POST
     - ``/api/modules/<name>/start/``
     - ``{"success": bool, "output": "..."}``.
   * - POST
     - ``/api/modules/<name>/stop/``
     - Same shape as start.
   * - POST
     - ``/api/modules/<name>/restart/``
     - Same shape as start.
   * - POST
     - ``/api/modules/<name>/activate/``
     - Enables the module (survives *Start All*); same response shape as start.
   * - POST
     - ``/api/modules/<name>/deactivate/``
     - Disables the module; same response shape as start.

Logs
****

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/modules/<name>/logs/``
     - Query params: ``lines`` (default 300, capped at 2000), ``filter`` (substring/regex
       applied server-side), ``before`` (ISO-8601 instant -- returns the last ``lines``
       entries at or before it, for the log pane's scroll-to-top "load older logs" feature;
       journald-backed modules only, the file backend returns ``[]``). ``{"lines": [...]}``.
   * - GET
     - ``/api/modules/<name>/log-stats/``
     - 24h level counts. ``{"stats": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}}``.
   * - GET
     - ``/api/logs/``
     - Fleet-wide merged tail, independent of the active host. Query params: ``lines``,
       ``filter``, ``modules`` -- a comma-separated list of ``<host>:<module>`` tokens
       (``host`` is ``"localhost"`` or a ``HUB_HOSTS`` name); omit ``modules`` entirely for
       every module on every configured host -- and ``before`` (same "load older logs"
       semantics as the per-module endpoint above, forwarded unchanged to each configured
       host). ``{"lines": [...], "unreachable_hosts":
       [{"name", "error"}, ...]}``; each line is tagged ``[host]`` when more than one host
       is selected.
   * - GET
     - ``/api/log-stats/``
     - All modules' 24h level counts on the active host. ``{"modules": {name: stats, ...}}``.

Config
******

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/modules/<name>/config/``
     - ``{"content": "<raw YAML>"}``.
   * - POST
     - ``/api/modules/<name>/config/``
     - Body: ``{"content": "<raw YAML>"}``. ``{"success": true}`` or
       ``{"success": false, "error": "..."}`` (400 on bad JSON, 404 if the file disappeared
       between request and write).
   * - GET / POST
     - ``/api/shared/<name>/config/``
     - Same shape as module config, for a shared YAML fragment under
       :doc:`features/module_management`. Not host-routed -- shared config is always local.
   * - POST
     - ``/api/modules/create/``
     - Body: ``{"name": "..."}``. ``{"success": true, "name": "..."}`` (400 if the name is
       missing/invalid, 409 if a config with that name already exists).

ACL
***

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/modules/<name>/acl/``
     - Host-routed via the active host. ``{"acl": {...}|null, "source": "...", "error":
       "..."|null}``.
   * - POST
     - ``/api/modules/<name>/acl/``
     - Body: ``{"acl": {"allow": {caller: [methods]}, ...}|{"deny": [callers]}, "mode":
       "enforce"|"log"}`` plus optional ``"host"`` (defaults to ``"localhost"`` -- **not**
       session-routed, see `Host routing`_). ``acl`` may be ``null`` to clear a local
       override. ``allow`` and ``deny`` are mutually exclusive. ``{"success": true}`` or
       ``{"success": false, "error": "..."}`` (400 on a malformed ``acl``, 409 on a save
       conflict).
   * - GET
     - ``/api/acl-matrix/``
     - This instance's own local ACL matrix only (no host-awareness) -- the raw output of
       ``services.build_acl_matrix()``. Queried by a hub instance to fold this host into its
       fleet-wide matrix; see :doc:`features/acl`.

Packages
********

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/packages/``
     - ``{"packages": [{"name", "installed_version", "latest_version", "update_available",
       "vcs"}, ...]}`` for every installed ``pyobs-*`` package (plus anything listed in
       ``PYOBS_MANAGED_PACKAGES``). ``latest_version`` is ``null`` and ``vcs`` is ``true``
       for a git/URL-installed package (no PyPI lookup is attempted for those).
   * - POST
     - ``/api/packages/<name>/update/``
     - Upgrades one package via ``pip install --upgrade``. ``{"ok": bool, "message":
       "..."}``. 404 if ``name`` isn't actually installed (this endpoint refuses to install
       an arbitrary new package).

ejabberd -- fleet-shared status/users (no host-awareness)
**********************************************************

These answer using this instance's own configured ``EJABBERD_API_URL``/``EJABBERDCTL``
directly -- they're the delegation target another instance calls when its own
``EJABBERD_HOST`` names this host (see :doc:`ejabberd/index`), and are also what this
instance's own dashboard calls when ``EJABBERD_HOST == "localhost"``.

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/ejabberd/status/``
     - ``{"node_status", "registered_count", "online_count", "connected": [...]}``.
   * - GET
     - ``/api/ejabberd/user/<user>/``
     - ``{"comm_user", "registered", "sessions", "last", "ban_details"}`` for one bare XMPP
       username.
   * - GET
     - ``/api/ejabberd/users/``
     - ``{"users": [{"user", "connected", "session", "last", "ban_details"}, ...]}`` for
       every registered account.
   * - POST
     - ``/api/ejabberd/user/<user>/register/``
     - Body: ``{"password": "..."}``. ``{"success": true}`` or ``{"error": "..."}`` (400).
   * - POST
     - ``/api/ejabberd/user/<user>/change-password/``
     - Body: ``{"password": "..."}``. Same response shape as register.
   * - POST
     - ``/api/ejabberd/user/<user>/ban/``
     - Body: ``{"reason": "..."}`` (optional). Same response shape as register.
   * - POST
     - ``/api/ejabberd/user/<user>/unban/``
     - No body. Same response shape as register.
   * - POST
     - ``/api/ejabberd/user/<user>/unregister/``
     - No body. Same response shape as register.
   * - POST
     - ``/api/ejabberd/user/<user>/kick/``
     - Body: ``{"resource": "...", "reason": "..."}`` (``resource`` required -- the XMPP
       resource of the session to disconnect). Same response shape as register.

ejabberd -- browser-facing (fleet-shared, EJABBERD_HOST-routed)
******************************************************************

Unlike the endpoints above, these delegate to wherever ``EJABBERD_HOST`` points and answer
the same regardless of which host's page called them -- see :doc:`ejabberd/index`.

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/ejabberd-summary/``
     - ``{"enabled": false}`` if ``EJABBERD_ENABLED`` is off, else ``{"enabled": true,
       "node_status", "registered_count", "online_count", "connected"}``.
   * - GET
     - ``/api/xmpp-users/``
     - ``{"enabled": false}`` or ``{"enabled": true, "users": [...]}}`` (same per-user shape
       as ``/api/ejabberd/users/``).
   * - GET
     - ``/api/comm-user-map/``
     - This instance's own local ``comm.user -> modules`` map (no host-awareness).
       ``{"map": {user: [{"name", "status"}, ...]}}``.
   * - POST
     - ``/api/xmpp-users/<user>/register/``
     - Body: ``{"password": "..."}`` (required, no config to read a default from here).
       ``{"success": bool, "error": "..."}``.
   * - POST
     - ``/api/xmpp-users/<user>/ban/``
     - Body: ``{"reason": "..."}`` (optional). Same response shape as register.
   * - POST
     - ``/api/xmpp-users/<user>/unban/``
     - No body. Same response shape as register.
   * - POST
     - ``/api/xmpp-users/<user>/unregister/``
     - No body. Same response shape as register.
   * - POST
     - ``/api/xmpp-users/<user>/kick/``
     - Body: ``{"resource": "...", "reason": "..."}`` (``resource`` required). Same response
       shape as register.

Module-scoped ejabberd (session/host-routed)
*********************************************

Per-module identity view/actions, resolving the module's own ``comm.user`` first and then
delegating the actual ejabberd call per ``EJABBERD_HOST`` -- see :doc:`ejabberd/index`.

.. list-table::
   :header-rows: 1
   :widths: 10 35 55

   * - Method
     - Path
     - Notes
   * - GET
     - ``/api/modules/<name>/ejabberd/``
     - ``{"comm_user": null}`` if the module has none, else ``{"comm_user",
       "module_running", "shared_with", "registered", "ban_details"}`` plus ``"sessions"``/
       ``"last"`` only when ``module_running`` is true.
   * - POST
     - ``/api/modules/<name>/ejabberd/register/``
     - No body needed (uses the module's own configured ``comm.password``). Accepts an
       optional ``"host"`` field (see `Host routing`_). ``{"success": bool, "error":
       "..."}``.
   * - POST
     - ``/api/modules/<name>/ejabberd/change-password/``
     - No body; generates and writes back a fresh password to every local module sharing
       that identity. Accepts an optional ``"host"`` field. ``{"success": bool,
       "updated_modules": [...], "error": "..."}``.
   * - POST
     - ``/api/modules/<name>/ejabberd/ban/``
     - Body: ``{"reason": "..."}`` (optional). Host-routed via the active host (no
       ``"host"`` field override). ``{"success": bool, "error": "..."}``.
   * - POST
     - ``/api/modules/<name>/ejabberd/unban/``
     - No body. Same response shape as ban.
   * - POST
     - ``/api/modules/<name>/ejabberd/unregister/``
     - No body. Same response shape as ban.
