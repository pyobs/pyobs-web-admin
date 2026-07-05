XMPP user management
######################

Builds on :doc:`integration` (requires ``EJABBERD_ENABLED = True``) to add write actions on
top of the read-only status it already shows: **register**, **reset password**, **ban** /
**unban**, **unregister**, and **kick** (force-disconnect one session without touching the
account) for any module's ``comm.user``.

Where it surfaces
******************

* The module detail page's existing ejabberd block (Overview tab) -- a *Register* action
  when the account isn't registered yet, *Reset password* / *Ban* / *Unregister* when it
  is.
* A dedicated fleet-wide **Users** page (``/xmpp-users/``), linked from the sidebar whenever
  ``EJABBERD_ENABLED = True`` -- every registered XMPP account across every configured host
  in one mobile-friendly list, cross-referenced against which module(s) use it and which one
  is actually the running/connected session. Unlike the module page, this also covers
  accounts with no owning module at all (e.g. ``admin``) via a manual "register account"
  form, and shows a status dot marking which module is the connected session when an
  identity is shared by more than one.

Confirmation UX
****************

pyobs-web-admin has exactly one admin identity and no role system -- the tiered
confirmation dialogs here are the safety net, not access control:

* **Register / reset password / ban / unban** -- reversible, a single confirmation dialog.
* **Unregister** -- the one action with no undo -- requires retyping the account's bare
  XMPP username (not the module name) before it fires.

Shared ``comm.user`` handling
*******************************

More than one module can legitimately share a single ``comm.user`` (e.g. a ``_test`` copy
of a module reusing a real module's identity) -- every write action accounts for this:

* **Reset password** writes the new password back into *every* module's config that
  resolves to the same ``comm.user``, not just the one the action was triggered from --
  leaving the others with a silently stale password would be a worse outcome than the
  feature not existing.
* **Ban / unregister** name every other module sharing the identity in the confirmation
  dialog before proceeding -- the action still goes through on confirm (blocking outright
  would be wrong, since sharing an identity on purpose is a real, supported case), but the
  operator can't click through without seeing who else is affected.

A password is written back into the module's own YAML the same way the ACL editor writes
an ``acl:`` block -- a text splice of just the changed key, not a full-file YAML
round-trip, refusing (and naming the fragment) if the ``comm:`` block actually lives in a
shared fragment rather than the module's own file.

Transport: ``ejabberdctl``, not ``mod_http_api``
***************************************************

Unlike the read path in :doc:`integration`, writes always go through the ``ejabberdctl``
CLI -- a write's cost is dominated by a human clicking a confirmation dialog, not command
latency, so the ~50-60x speed advantage HTTP has for reads doesn't matter here. This also
means **no ``api_permissions`` change is needed** beyond what :doc:`integration` already
configured -- ejabberd's own default ``"console commands"`` grant (``from: [ejabberd_ctl],
who: all, what: "*"``) already covers anything invoked via ``ejabberdctl``.

Deployment: a sudoers rule for ``ejabberdctl``
*************************************************

``ejabberdctl`` normally refuses to run as anything other than ``root`` or the ``ejabberd``
system user (``"can only be run by root or the user ejabberd"``). If pyobs-web-admin runs as
its own service user (e.g. ``pyobs``), give that user a narrowly-scoped passwordless sudo
rule for just this one binary::

    # /etc/sudoers.d/pyobs-web-admin-ejabberdctl
    pyobs ALL=(root) NOPASSWD: /usr/sbin/ejabberdctl

(adjust the username and binary path for your setup -- check with ``which ejabberdctl``),
then point ``EJABBERDCTL`` at the wrapper script committed at the repo root::

    EJABBERDCTL = "/opt/pyobs/pyobs-web-admin/ejabberdctl-sudo.sh"

``ejabberdctl-sudo.sh`` is a two-line wrapper (``exec sudo -n ejabberdctl "$@"``) -- the
``-n`` flag makes ``sudo`` fail fast instead of hanging on a password prompt if the sudoers
rule above isn't in place. Not needed at all if pyobs-web-admin already runs as ``root`` or
``ejabberd``.

Security note
*************

This is a materially bigger trust step than the read-only integration in :doc:`integration`:
``ejabberdctl`` can do anything an ejabberd administrator can do, not just the small
read-only whitelist ``mod_http_api``'s ``api_permissions`` enforces for reads. There is no
OS-level or ejabberd-level restriction narrowing what the sudo rule allows beyond "run
``ejabberdctl`` as root at all" -- this app's own tiered confirmation dialogs are the only
safety net between a logged-in admin and any ``ejabberdctl`` subcommand this app happens to
call. Acceptable for the same reason as the read path's IP-based trust model: a dedicated,
single-purpose observatory control host with one admin identity, not a shared or
multi-tenant one.
