Hub mode
########

pyobs-web-admin can act as a hub to control multiple remote pyobs hosts from a single
browser session. When remote hosts are configured, a **Hosts** section appears at the top
of the sidebar; clicking a host switches the active context, and most views (dashboard,
config, logs) transparently proxy their actions to that host's own API instead of the local
one.

Setup
*****

On the **hub** (the machine you browse to), list the remote hosts::

    HUB_HOSTS = [
        {"name": "obs1", "url": "http://obs1:8765", "token": "shared-secret"},
        {"name": "obs2", "url": "http://obs2:8765", "token": "another-secret"},
    ]

On each **remote host** (a "spoke"), give it a named client entry matching the token the hub
sends for it::

    HUB_CLIENTS = [
        {"name": "hub", "token": "shared-secret"},   # must match HUB_HOSTS' token above
    ]

Authentication
**************

The hub -- or any other external caller -- authenticates to a remote instance via an
``X-Hub-Token`` header. A remote instance checks that header against every entry in
``HUB_CLIENTS`` (each entry is ``{"name": ..., "token": ...}``); a match bypasses the normal
browser-session/CSRF check for that request, which is what lets an external caller invoke the
API without a login session of its own. Each caller (a hub, a script, another service) should
get its own named entry so it can be revoked or rotated independently, without affecting any
other caller. The token is a plain pre-shared string with no other layer on top of it, so
treat it like any other bearer credential: long, random, and secret.

The older ``HUB_TOKEN`` setting (a single flat token, no name) still works for backwards
compatibility -- it's equivalent to adding ``{"name": "default", "token": HUB_TOKEN}`` to
``HUB_CLIENTS``. New setups should use ``HUB_CLIENTS`` directly so that every caller is
independently identifiable and revocable.

This mechanism isn't hub-specific -- any external client that sends a valid
``X-Hub-Token`` can call the same endpoints the hub does. See :doc:`api_endpoints` for the
full list.

One active host at a time -- and the exception
*************************************************

Most hub-aware views (Dashboard, Config, Logs) follow a "one active host at a time" model:
whichever host the sidebar selector points at is the one all actions target, mirroring a
single-host session. A few pages instead need to aggregate **every** configured host on one
page regardless of the active-host selection -- the :doc:`features/acl` matrix, the
fleet-wide Overview page, and the fleet-wide Users page (:doc:`ejabberd/user_management`)
all fall into this second category, since their entire purpose is a cross-host view. Each of
those pages queries every ``HUB_HOSTS`` entry independently and shows unreachable hosts as a
warning banner rather than failing the whole page or hiding the gap silently.
