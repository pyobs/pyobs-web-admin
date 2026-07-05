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

On each **remote host** (a "spoke"), set the matching token so it accepts requests carrying
it::

    HUB_TOKEN = "shared-secret"   # must match the token the hub sends for this host

Authentication
**************

The hub authenticates to a remote instance via an ``X-Hub-Token`` header. A remote instance
that receives a request carrying its configured ``HUB_TOKEN`` bypasses the normal
browser-session/CSRF check for that request -- this is what lets the hub call it without a
login session of its own. The token is a plain pre-shared string with no other layer on top
of it, so treat it like any other bearer credential: long, random, and secret.

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
