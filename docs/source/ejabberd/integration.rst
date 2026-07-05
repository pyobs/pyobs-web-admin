Read-only status integration
##############################

Enabling it
***********

::

    EJABBERD_ENABLED = True
    EJABBERD_HOST = "localhost"           # or a HUB_HOSTS name -- see "One shared server" below
    EJABBERD_DOMAIN = "your-xmpp-domain"  # the vhost ejabberd serves, e.g. "pyobs.example.org"
    EJABBERD_API_URL = "http://127.0.0.1:5281/api"

A module's XMPP identity is its ``comm.user`` config key, resolved the same way this app
already resolves ``acl:`` blocks (so it works whether ``comm:`` is defined locally, via
``{include}``, or via a YAML anchor merge key). A module with no ``comm:`` block at all
(e.g. a pure HTTP module) is skipped entirely everywhere below -- there's nothing for it to
connect to, so there's no "should be connected but isn't" mismatch worth surfacing for it.

Where it surfaces
******************

* **Dashboard** -- a summary tile (connected / registered counts, node status as a tooltip)
  alongside the existing Total/Running/Stopped/RAM/CPU tiles, plus a small filled-green
  "connected" / outlined-amber "not connected" icon per module row. The tile's denominator
  is *this installation's own* modules that have a ``comm_user`` and aren't deactivated --
  not ejabberd's fleet-wide registered-account count, which can include unrelated accounts
  (``admin``, other roles) that have nothing to do with any module here.
* **Module detail page** (Overview tab) -- connected-since/IP/connection type if live, or
  last-seen (with the actual disconnect reason, if any) if not, or "not a registered
  account" if ``comm.user`` doesn't correspond to a real ejabberd account at all.

Both are gated on the module's own running status: a *stopped* module never shows a
"connected" state, even if another module happens to share its ``comm.user`` and is
currently connected under that identity (a real, supported configuration -- e.g. a ``_test``
copy of a module reusing a real module's identity for testing).

Data layer
**********

The primary mechanism is ejabberd's HTTP admin API, ``mod_http_api`` -- not ``ejabberdctl``
subprocess calls -- since it's roughly 50-60x faster per call (hits the already-running
node directly instead of booting a fresh Erlang VM per invocation). ``ejabberdctl`` is kept
as a documented fallback for hosts that haven't done the ejabberd-side setup below yet.

.. list-table::
   :header-rows: 1

   * - Command
     - Returns
     - Used for
   * - ``status``
     - Node status string
     - Dashboard: is the XMPP backbone itself healthy
   * - ``stats``
     - A bare integer (registered/online users, uptime)
     - Dashboard summary tile
   * - ``connected_users_info``
     - List of connected sessions (JID, IP, connection type, ...)
     - Cross-referencing against modules for the "connected" indicator
   * - ``registered_users``
     - List of registered account names
     - Sanity-checking ``comm.user`` against real accounts
   * - ``user_sessions_info``
     - Same shape as one ``connected_users_info`` entry
     - Module page: is *this* module's identity connected, since when, from where
   * - ``get_last``
     - Last-seen timestamp + status, or a disconnect reason, or "not found"
     - Module page: "last connected 3h ago (stream reset by peer)"
   * - ``check_account``
     - Whether an account is registered at all
     - Module page: flag a ``comm.user`` that isn't a real XMPP account

ejabberd-side configuration
****************************

Add an HTTP listener with ``mod_http_api``, and a permissions grant limited to the
read-only commands above::

    listen:
      -
        port: 5281
        ip: "127.0.0.1"         # loopback only -- see security note below
        module: ejabberd_http
        request_handlers:
          /api: mod_http_api    # add to an *existing* listener's request_handlers if one's
                                 # already on this port -- ejabberd allows one listener per port

    modules:
      mod_http_api: {}

    api_permissions:
      "console commands":
        from: [ejabberd_ctl]
        who: all
        what: "*"
      "pyobs-web-admin readonly":
        from: [mod_http_api]
        who:
          access:
            allow:
              - acl: loopback
        what:
          - "status"
          - "stats"
          - "connected_users_info"
          - "registered_users"
          - "user_sessions_info"
          - "get_last"
          - "check_account"

Reload ejabberd's config afterward (``ejabberdctl reload_config``, or a restart if that
doesn't pick up a new listener). The ``what:`` list is a deliberate whitelist -- leave it
as-is; ``mod_http_api`` can also expose account-management commands
(``register``/``unregister``/``change_password``) that must never be reachable through this
read-only grant (see :doc:`user_management` for how those are handled instead).

Security model
**************

Access is **IP-based, not credential-based** -- the only gate is ``acl: loopback`` in
``api_permissions``; there's no username/password or bearer-token layer on this endpoint at
all. This is a deliberate, tested choice, not an oversight: both an OAuth bearer token and
HTTP Basic Auth were tried and abandoned (OAuth failed with a missing dependency in one
tested ejabberd build; Basic Auth kept returning an unrelated authorization error despite
valid credentials). What this does and doesn't protect against:

* **Does protect against** any request arriving from outside the machine, over the network
  -- confirmed by testing from a real non-loopback address, which was rejected even for
  commands in the explicit whitelist.
* **Does not protect against** any *other* local process or user account on the same
  machine -- the ACL can't distinguish "pyobs-web-admin specifically" from "anything else
  on this box that can reach 127.0.0.1:5281." This is an accepted tradeoff for a dedicated,
  single-purpose observatory control host, not appropriate for a shared or multi-tenant one.

One shared server, hub-aware
******************************

ejabberd is normally **one** server for the whole fleet, not one per host, so this isn't a
many-hosts-aggregate problem the way the ACL matrix is -- it's "delegate to the one host
that has it." If ``EJABBERD_HOST`` names a ``HUB_HOSTS`` entry instead of ``"localhost"``,
every other instance in the fleet transparently proxies its ejabberd queries to that one
host through the existing hub-token-authenticated proxy (see :doc:`../hub`) -- rather than
pointing ``EJABBERD_API_URL`` at a remote host's IP directly, which would mean widening
ejabberd's own loopback-only ACL to accept a specific remote caller instead. Only the one
host that actually runs ejabberd needs ``EJABBERD_API_URL`` pointed at a real instance;
every other host just needs ``EJABBERD_HOST`` set to that host's name.
