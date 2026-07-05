ejabberd / XMPP integration
############################

pyobs modules that use the XMPP comm layer (``pyobs.comm.xmpp.XmppComm``) connect through
an `ejabberd <https://www.ejabberd.im/>`_ server, usually co-located on the same host as
pyobs-web-admin itself. This app can optionally talk to that ejabberd instance directly --
first read-only (connection status), and, building on that, with write actions (account
management) too.

This closes two visibility gaps this app doesn't otherwise cover:

* **Process running ≠ XMPP connected.** A module's process can be alive (the existing
  status check passes) while its XMPP session is stuck reconnecting after a network blip --
  invisible without this integration.
* **Config vs. reality.** A module's ``comm.user`` might not even be a registered XMPP
  account at all (typo, stale config, account never created) -- a distinct failure mode
  from "not connected right now."

Both pieces are gated behind ``EJABBERD_ENABLED`` and are entirely absent from the UI --
not just hidden -- when it's off.

.. toctree::
   :maxdepth: 2

   integration
   user_management
