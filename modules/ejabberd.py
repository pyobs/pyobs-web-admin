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
