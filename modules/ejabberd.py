import subprocess

import requests as _http
from django.conf import settings

# Fields returned by ejabberdctl's connected_users_info/user_sessions_info, in order --
# connected_users_info's raw text has "jid" as an extra leading field these two commands
# otherwise share (see EJABBERD_INTEGRATION.md, Data layer). The HTTP API returns the same
# fields as JSON keys directly; this is only used to normalize the ejabberdctl fallback into
# the same shape.
_SESSION_FIELDS = ["connection", "ip", "port", "priority", "node", "uptime", "status", "resource", "statustext"]
_SESSION_INT_FIELDS = ("port", "priority", "uptime")


def _use_http() -> bool:
    """ejabberdctl is a documented fallback for hosts that haven't set up mod_http_api yet
    (see EJABBERD_INTEGRATION.md) -- not a fallback for any HTTP failure, which should
    surface as a real error rather than being silently masked by a slower, different path."""
    return bool(getattr(settings, "EJABBERD_API_URL", ""))


def _http_call(command: str, args: dict):
    url = f"{settings.EJABBERD_API_URL.rstrip('/')}/{command}"
    resp = _http.post(url, json=args, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _ctl_call(command: str, *args: str) -> str:
    """Runs one ejabberdctl command, returning raw, unstripped stdout -- a trailing tab can
    be a legitimately empty last field (e.g. connected_users_info's statustext), so callers
    strip whitespace themselves rather than have it stripped away here. Callers that need
    the exit code directly (check_account) use _ctl_returncode instead."""
    result = subprocess.run(
        [settings.EJABBERDCTL, command, *args],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout


def _ctl_returncode(command: str, *args: str) -> int:
    result = subprocess.run(
        [settings.EJABBERDCTL, command, *args],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode


def _parse_session_line(line: str, has_jid: bool) -> dict:
    parts = line.split("\t")
    if has_jid:
        jid, *rest = parts
        session = {"jid": jid, **dict(zip(_SESSION_FIELDS, rest))}
    else:
        session = dict(zip(_SESSION_FIELDS, parts))
    for field in _SESSION_INT_FIELDS:
        session[field] = int(session[field])
    return session


def status() -> str:
    if _use_http():
        return _http_call("status", {})
    return _ctl_call("status").strip()


def stats(name: str) -> int:
    """name is one of "registeredusers", "onlineusers", "uptimeseconds" (see
    EJABBERD_INTEGRATION.md, Data layer)."""
    if _use_http():
        return int(_http_call("stats", {"name": name}))
    return int(_ctl_call("stats", name).strip())


def connected_users_info() -> list[dict]:
    if _use_http():
        return _http_call("connected_users_info", {})
    raw = _ctl_call("connected_users_info")
    return [_parse_session_line(line, has_jid=True) for line in raw.splitlines() if line]


def registered_users() -> list[str]:
    domain = settings.EJABBERD_DOMAIN
    if _use_http():
        return _http_call("registered_users", {"host": domain})
    raw = _ctl_call("registered_users", domain)
    return [line for line in raw.splitlines() if line]


def user_sessions_info(user: str) -> list[dict]:
    domain = settings.EJABBERD_DOMAIN
    if _use_http():
        return _http_call("user_sessions_info", {"user": user, "host": domain})
    raw = _ctl_call("user_sessions_info", user, domain)
    return [_parse_session_line(line, has_jid=False) for line in raw.splitlines() if line]


def get_last(user: str) -> dict:
    domain = settings.EJABBERD_DOMAIN
    if _use_http():
        return _http_call("get_last", {"user": user, "host": domain})
    line = _ctl_call("get_last", user, domain).splitlines()[0]
    timestamp, _, last_status = line.partition("\t")
    return {"timestamp": timestamp, "status": last_status}


def check_account(user: str) -> bool:
    """True if user is a registered account on EJABBERD_DOMAIN. The HTTP API and
    ejabberdctl signal this two different ways (see EJABBERD_INTEGRATION.md, Data layer):
    HTTP always returns 200 with a bare 0/1 body; ejabberdctl instead uses its process exit
    code (0/1), with no reliable stdout content to parse either way."""
    domain = settings.EJABBERD_DOMAIN
    if _use_http():
        return _http_call("check_account", {"user": user, "host": domain}) == 0
    return _ctl_returncode("check_account", user, domain) == 0


# ── Write commands -- ejabberdctl only, never mod_http_api ───────────────────
#
# See EJABBERD_USER_MANAGEMENT.md, Design "Transport": a write's cost is dominated by a
# human clicking a confirmation dialog, not command latency, so there's no reason to widen
# the loopback mod_http_api ACL just for these -- every one of them stays on the ejabberdctl
# subprocess path unconditionally, no _use_http() branch.
#
# Every one of these commands writes only to stdout on both success and failure --
# ejabberdctl never uses stderr for them (verified live, see that doc's "Verified live"
# table; an earlier pass at that table wrongly attributed failure messages to stderr, an
# artifact of testing with merged streams). The exit code is the only reliable
# success/failure signal, unlike the read commands above, none of which needed one.

def _ctl_write(command: str, *args: str) -> tuple[bool, str]:
    result = subprocess.run(
        [settings.EJABBERDCTL, command, *args],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0, result.stdout.strip()


def register(user: str, password: str) -> None:
    """Registers a new XMPP account for user on EJABBERD_DOMAIN. Raises ValueError on
    failure (e.g. the account already exists -- verified live: exit 1, stdout "Error:
    conflict: User <user>@<host> already registered")."""
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("register", user, domain, password)
    if not ok:
        raise ValueError(message or f"Failed to register {user}@{domain}")


def change_password(user: str, new_password: str) -> None:
    """Changes user's XMPP password on EJABBERD_DOMAIN. Prints nothing on success (verified
    live -- contradicts ejabberdctl's own help text example, which shows a printed 'ok'; this
    ejabberd version prints nothing), so an empty message alongside a zero exit code is the
    expected success case, not a sign anything went wrong. Raises ValueError on failure (e.g.
    the account doesn't exist -- verified live: exit 1, stdout a raw Erlang tuple literal
    `{not_found,"unknown_user"}`)."""
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("change_password", user, domain, new_password)
    if not ok:
        raise ValueError(message or f"Failed to change password for {user}@{domain}")


def ban_account(user: str, reason: str) -> None:
    """Puts user into ejabberd's account-disabled state on EJABBERD_DOMAIN, recording reason
    -- reversible via unban_account, which restores the account's original password (verified
    live; not a "swap in a random password" as ejabberd's own docs might suggest -- see
    EJABBERD_USER_MANAGEMENT.md's Current state). Note check_password must never be used to
    detect this state (it throws an unhandled exception against a banned account) -- use
    get_ban_details instead."""
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("ban_account", user, domain, reason)
    if not ok:
        raise ValueError(message or f"Failed to ban {user}@{domain}")


def unban_account(user: str) -> None:
    """Lifts a ban placed by ban_account, restoring the account's original password
    (verified live)."""
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("unban_account", user, domain)
    if not ok:
        raise ValueError(message or f"Failed to unban {user}@{domain}")


def unregister(user: str) -> None:
    """Permanently deletes user's account -- auth, roster, and vcard data -- on
    EJABBERD_DOMAIN. Not reversible. Verified live: silently succeeds (exit 0, empty output)
    even if the account was never registered in the first place -- ejabberd itself doesn't
    distinguish "removed" from "was never there," so a caller that needs to know which
    happened must call check_account first, not infer it from this function's result."""
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("unregister", user, domain)
    if not ok:
        raise ValueError(message or f"Failed to unregister {user}@{domain}")


def get_ban_details(user: str) -> dict | None:
    """Returns a dict of ban details (reason/bandate/lastdate/lastreason) if user is
    currently banned on EJABBERD_DOMAIN, or None if not. The safe way to check ban status --
    check_password throws an unhandled exception against a banned account instead of
    returning a clean answer (verified live, see EJABBERD_USER_MANAGEMENT.md's "Verified
    live"). Read-only, but ejabberdctl-only like the write commands above rather than
    mod_http_api, since it exists purely to support them and isn't in the existing
    mod_http_api whitelist either."""
    domain = settings.EJABBERD_DOMAIN
    raw = _ctl_call("get_ban_details", user, domain)
    lines = [line for line in raw.splitlines() if line]
    if not lines:
        return None
    details = {}
    for line in lines:
        key, _, value = line.partition("\t")
        details[key] = value
    return details


def kick_session(user: str, resource: str, reason: str) -> None:
    """Force-disconnects one specific session of user (identified by its XMPP resource,
    always "pyobs" for a pyobs module -- confirmed live, see EJABBERD_INTEGRATION.md's Data
    layer) on EJABBERD_DOMAIN, recording reason in the disconnect message. Doesn't touch the
    account itself (still registered, same password), unlike ban/unregister.

    Uses kick_session rather than kick_user deliberately: kick_user takes no reason at all
    and reports a generic "policy-violation" cause, which the module side can't distinguish
    from any other disconnect. Verified live against a real connected session: exit 0, empty
    stdout on success (same silent-rescode pattern as change_password, despite ejabberdctl's
    own help text example showing a printed 'ok') -- and the reason text does appear
    verbatim afterward in the module's own get_last ("Stream closed by local host: <reason>
    (conflict)"), confirming the module side can actually key off of it. Raises ValueError
    on failure.
    """
    domain = settings.EJABBERD_DOMAIN
    ok, message = _ctl_write("kick_session", user, domain, resource, reason)
    if not ok:
        raise ValueError(message or f"Failed to kick {user}@{domain}/{resource}")
