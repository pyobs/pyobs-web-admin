import json
import re
from typing import Any

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from modules import ejabberd, proxy, services


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_view(request):
    error = False
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        if (username == settings.ADMIN_USERNAME
                and settings.ADMIN_PASSWORD_HASH
                and check_password(password, settings.ADMIN_PASSWORD_HASH)):
            request.session["authenticated"] = True
            request.session["username"] = username
            return redirect(request.POST.get("next") or "/")
        error = True
    return render(request, "registration/login.html", {
        "error": error,
        "next": request.GET.get("next", "/"),
    })


def logout_view(request):
    if request.method == "POST":
        request.session.flush()
    return redirect("/login/")


# ── Hub host selection ────────────────────────────────────────────────────────

def set_host(request, name: str):
    valid = {h["name"] for h in proxy.all_hosts()}
    if name in valid:
        request.session["active_host"] = name
    next_url = request.GET.get("next")
    return redirect(next_url if next_url and next_url.startswith("/") else "/")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _active_host(request) -> dict | None:
    return proxy.get_host_config(request.session.get("active_host", "localhost"))


def _proxy(host: dict, method: str, path: str, **kwargs) -> JsonResponse:
    try:
        data = proxy.call(host, method, path, **kwargs)
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=502)


def _get_module_or_404(name: str) -> str:
    try:
        services.validate_name(name)
    except ValueError:
        raise Http404("Invalid module name")
    if name not in services.list_modules():
        raise Http404(f"Module '{name}' not found")
    return name


def _get_shared_or_404(name: str) -> str:
    try:
        services.validate_shared_name(name)
    except ValueError:
        raise Http404("Invalid shared config name")
    if name not in services.list_shared_configs():
        raise Http404(f"Shared config '{name}' not found")
    return name


# ── Page views ────────────────────────────────────────────────────────────────

def dashboard(request):
    host = _active_host(request)
    if host:
        try:
            data = proxy.call(host, "GET", "/api/statuses/")
            modules = [m["name"] for m in data.get("modules", [])]
        except Exception:
            modules = []
    else:
        modules = services.list_modules()
    return render(request, "modules/dashboard.html", {
        "modules": modules,
        "ejabberd_enabled": getattr(settings, "EJABBERD_ENABLED", False),
    })


def module_detail(request, name: str):
    host = _active_host(request)
    if host:
        try:
            cfg_data = proxy.call(host, "GET", f"/api/modules/{name}/config/")
            config = cfg_data.get("content", "")
        except Exception:
            config = ""
        try:
            statuses = proxy.call(host, "GET", "/api/statuses/")
            other_modules = [m["name"] for m in statuses.get("modules", []) if m["name"] != name]
        except Exception:
            other_modules = []
        return render(request, "modules/detail.html", {
            "module_name": name,
            "config": config,
            "active_module": name,
            "config_dir": "(remote)",
            "log_dir": "(remote)",
            "other_modules": other_modules,
            "ejabberd_enabled": getattr(settings, "EJABBERD_ENABLED", False),
        })
    _get_module_or_404(name)
    config = services.get_config(name)
    return render(request, "modules/detail.html", {
        "module_name": name,
        "config": config or "",
        "active_module": name,
        "config_dir": settings.PYOBS_CONFIG_DIR,
        "log_dir": settings.PYOBS_LOG_DIR,
        "other_modules": [m for m in services.list_modules() if m != name],
        "ejabberd_enabled": getattr(settings, "EJABBERD_ENABLED", False),
    })


def shared_detail(request, name: str):
    _get_shared_or_404(name)
    return render(request, "modules/shared_detail.html", {
        "config_name": name,
        "config": services.get_shared_config(name) or "",
        "active_shared": name,
        "config_dir": settings.PYOBS_CONFIG_DIR,
    })


def _cross_host_url(host: str, url_name: str, arg: str) -> str:
    """A link to another page that's correct regardless of which host is currently
    "active" in the session -- for localhost it's just the plain URL, for a hub host it
    first switches the session's active host (existing set_host view) via its "next"
    redirect, since module_detail/shared_detail always operate on the session's active
    host rather than taking one as a URL argument."""
    target = reverse(url_name, args=[arg])
    if host == "localhost":
        return target
    return f"{reverse('set_host', args=[host])}?next={target}"


def all_logs(request):
    # Aggregates across every configured hub host (see acl_matrix) regardless of which host
    # is currently "active" in the session -- like ACL Matrix, this page's whole point is
    # fleet-wide visibility, not one host at a time.
    per_host = [("localhost", services.list_modules())]
    unreachable = []
    for host_cfg in getattr(settings, "HUB_HOSTS", []):
        try:
            data = proxy.call(host_cfg, "GET", "/api/statuses/")
            per_host.append((host_cfg["name"], [m["name"] for m in data.get("modules", [])]))
        except Exception as e:
            unreachable.append({"name": host_cfg["name"], "error": str(e)})

    return render(request, "modules/all_logs.html", {
        "hosts": [{"name": name, "modules": modules} for name, modules in per_host],
        "has_modules": any(modules for _, modules in per_host),
        "unreachable_hosts": unreachable,
        "show_host_badges": len(per_host) > 1,
        "active_all_logs": True,
    })


def acl_matrix(request):
    # Aggregates across every configured hub host (see ACL_MATRIX.md, "Hub mode
    # interaction") regardless of which host is currently "active" in the session --
    # unlike the rest of this app's hub-mode views, this page's whole point is to show
    # fleet-wide policy in one place, not to view one host at a time.
    per_host = [("localhost", services.build_acl_matrix())]
    unreachable = []
    for host_cfg in getattr(settings, "HUB_HOSTS", []):
        try:
            per_host.append((host_cfg["name"], proxy.call(host_cfg, "GET", "/api/acl-matrix/")))
        except Exception as e:
            unreachable.append({"name": host_cfg["name"], "error": str(e)})

    matrix = services.merge_acl_matrices(per_host)
    callers = matrix["callers"]

    module_hosts: dict[str, str] = {}
    for row in matrix["targets"]:
        module_hosts.setdefault(row["name"], row["host"])
    caller_headers = [
        {"name": c, "url": _cross_host_url(module_hosts[c], "module_detail", c) if c in module_hosts else None}
        for c in callers
    ]

    source_counts: dict[tuple[str, str], int] = {}
    for row in matrix["targets"]:
        if row["source"]:
            key = (row["host"], row["source"])
            source_counts[key] = source_counts.get(key, 0) + 1

    rows = []
    for row in matrix["targets"]:
        mode = row["acl"].get("mode", "enforce") if row["acl"] else "enforce"
        rows.append({
            **row,
            "mode": mode,
            "source_count": source_counts.get((row["host"], row["source"])) if row["source"] else None,
            "acl_data_id": f"acl-data-{row['host']}-{row['name']}",
            "module_url": _cross_host_url(row["host"], "module_detail", row["name"]),
            # local: jump straight to the Config tab; remote: the set_host detour can't
            # carry a #fragment through the redirect, so this just lands on Overview.
            "module_config_url": (
                reverse("module_detail", args=[row["name"]]) + "#tab-config"
                if row["host"] == "localhost" else _cross_host_url(row["host"], "module_detail", row["name"])
            ),
            "shared_url": _cross_host_url(row["host"], "shared_detail", row["source"]) if row["source"] else None,
            "cell_list": [{"caller": c, **row["cells"][c]} for c in callers],
        })
    return render(request, "modules/acl_matrix.html", {
        "active_acl_matrix": True,
        "targets": rows,
        "callers": callers,
        "caller_headers": caller_headers,
        "unreachable_hosts": unreachable,
        "show_host_badges": len(per_host) > 1,
    })


# ── Status API ────────────────────────────────────────────────────────────────

@require_GET
def api_all_statuses(request):
    host = _active_host(request)
    if host:
        return _proxy(host, "GET", "/api/statuses/")
    modules = services.list_modules()
    result = []
    for m in modules:
        status = services.get_module_status(m)
        stats = services.get_module_stats(m) if status == "running" else None
        result.append({"name": m, "status": status, "stats": stats, "comm_user": services.get_comm_user(m)})
    return JsonResponse({"modules": result})


@require_GET
def api_status(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "GET", f"/api/modules/{name}/status/")
    _get_module_or_404(name)
    status = services.get_module_status(name)
    stats = services.get_module_stats(name) if status == "running" else None
    return JsonResponse({"status": status, "stats": stats})


# ── Control API ───────────────────────────────────────────────────────────────

@require_POST
def api_start(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "POST", f"/api/modules/{name}/start/")
    _get_module_or_404(name)
    success, output = services.start_module(name)
    return JsonResponse({"success": success, "output": output})


@require_POST
def api_stop(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "POST", f"/api/modules/{name}/stop/")
    _get_module_or_404(name)
    success, output = services.stop_module(name)
    return JsonResponse({"success": success, "output": output})


@require_POST
def api_restart(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "POST", f"/api/modules/{name}/restart/")
    _get_module_or_404(name)
    success, output = services.restart_module(name)
    return JsonResponse({"success": success, "output": output})


@require_POST
def api_activate(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "POST", f"/api/modules/{name}/activate/")
    _get_module_or_404(name)
    success, output = services.activate_module(name)
    return JsonResponse({"success": success, "output": output})


@require_POST
def api_deactivate(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "POST", f"/api/modules/{name}/deactivate/")
    _get_module_or_404(name)
    success, output = services.deactivate_module(name)
    return JsonResponse({"success": success, "output": output})


# ── Logs API ──────────────────────────────────────────────────────────────────

@require_GET
def api_logs(request, name: str):
    host = _active_host(request)
    lines = int(request.GET.get("lines", 300))
    if host:
        return _proxy(host, "GET", f"/api/modules/{name}/logs/", params={"lines": lines})
    _get_module_or_404(name)
    filter_str = request.GET.get("filter", "")
    log_lines = services.get_logs(name, lines=min(lines, 2000), filter_str=filter_str)
    return JsonResponse({"lines": log_lines})


@require_GET
def api_log_stats(request, name: str):
    host = _active_host(request)
    if host:
        return _proxy(host, "GET", f"/api/modules/{name}/log-stats/")
    _get_module_or_404(name)
    return JsonResponse({"stats": services.get_log_stats(name)})


_LOG_TS_PREFIX_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def _tag_host(line: str, host_name: str) -> str:
    """Inserts a "[host]" tag right after the line's own leading timestamp, so a line stays
    parseable by the same client-side "timestamp must lead the line" regex the single-host
    page already relies on (see all_logs.html's parseLogTime) while still showing which host
    it came from once more than one host is merged into one view."""
    m = _LOG_TS_PREFIX_RE.match(line)
    if not m:
        return f"[{host_name}] {line}"
    ts = m.group(1)
    return f"{ts} [{host_name}]{line[len(ts):]}"


@require_GET
def api_all_logs(request):
    # Fleet-wide, like acl_matrix -- queries every configured hub host (plus this instance
    # itself), not just whichever host happens to be "active" in the session. Each token in
    # `modules` is "<host>:<module>" so the same module name on two different hosts can be
    # selected independently; a host with zero tokens present is skipped entirely, matching
    # every one of its checkboxes being unchecked in the UI.
    lines = int(request.GET.get("lines", 300))
    filter_str = request.GET.get("filter", "")
    modules_param = request.GET.get("modules")

    all_host_names = ["localhost"] + [h["name"] for h in getattr(settings, "HUB_HOSTS", [])]
    if modules_param is None:
        # No restriction at all -- every configured host, every module on it.
        host_selections: dict[str, list[str] | None] = {name: None for name in all_host_names}
    else:
        host_selections = {}
        for token in modules_param.split(","):
            if ":" not in token:
                continue
            host_name, _, module_name = token.partition(":")
            if not module_name:
                continue
            host_selections.setdefault(host_name, []).append(module_name)

    line_lists = []
    unreachable = []
    for host_name, names in host_selections.items():
        if host_name == "localhost":
            if names is not None:
                for name in names:
                    _get_module_or_404(name)
            host_lines = services.get_all_logs(names, lines=min(lines, 2000), filter_str=filter_str)
        else:
            host_cfg = proxy.get_host_config(host_name)
            if not host_cfg:
                continue
            try:
                params: dict[str, Any] = {"lines": lines}
                if names is not None:
                    # The remote's own api_all_logs expects "host:module" tokens too, with
                    # "localhost" naming *its* own modules -- not the bare names this
                    # instance uses to key host_selections.
                    params["modules"] = ",".join(f"localhost:{n}" for n in names)
                if filter_str:
                    params["filter"] = filter_str
                data = proxy.call(host_cfg, "GET", "/api/logs/", params=params)
                host_lines = data.get("lines", [])
            except Exception as e:
                unreachable.append({"name": host_name, "error": str(e)})
                continue
        multi_host = len(host_selections) > 1
        line_lists.append([_tag_host(l, host_name) for l in host_lines] if multi_host else host_lines)

    log_lines = services.merge_log_lines(line_lists, lines)
    return JsonResponse({"lines": log_lines, "unreachable_hosts": unreachable})


@require_GET
def api_all_log_stats(request):
    host = _active_host(request)
    if host:
        return _proxy(host, "GET", "/api/log-stats/")
    modules = services.list_modules()
    result = {m: services.get_log_stats(m) for m in modules}
    return JsonResponse({"modules": result})


# ── Config API ────────────────────────────────────────────────────────────────

def api_config(request, name: str):
    host = _active_host(request)
    if host:
        if request.method == "GET":
            return _proxy(host, "GET", f"/api/modules/{name}/config/")
        if request.method == "POST":
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError as e:
                return JsonResponse({"success": False, "error": str(e)}, status=400)
            return _proxy(host, "POST", f"/api/modules/{name}/config/", json=data)
        return JsonResponse({"error": "Method not allowed"}, status=405)
    _get_module_or_404(name)
    if request.method == "GET":
        content = services.get_config(name)
        return JsonResponse({"content": content or ""})
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            services.save_config(name, data.get("content", ""))
            return JsonResponse({"success": True})
        except (json.JSONDecodeError, KeyError) as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except FileNotFoundError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)
    return JsonResponse({"error": "Method not allowed"}, status=405)


def api_shared_config(request, name: str):
    _get_shared_or_404(name)
    if request.method == "GET":
        return JsonResponse({"content": services.get_shared_config(name) or ""})
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            services.save_shared_config(name, data.get("content", ""))
            return JsonResponse({"success": True})
        except (json.JSONDecodeError, KeyError) as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except FileNotFoundError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)
    return JsonResponse({"error": "Method not allowed"}, status=405)


@require_GET
def api_acl_matrix(request):
    """Queried by another pyobs-web-admin instance acting as a hub, to fold this
    installation's own local ACL matrix into its fleet-wide view -- see
    services.merge_acl_matrices and ACL_MATRIX.md, "Hub mode interaction"."""
    return JsonResponse(services.build_acl_matrix())


def api_acl(request, name: str):
    """Reads or saves a module's structured acl: edit -- used by both the matrix page's
    per-row modal and module_detail's own ACL tab.

    GET follows the session's active host, like every other module_detail-feeding endpoint
    (api_config, api_logs, ...) -- module_detail only ever shows one host at a time.

    POST instead trusts an explicit "host" field in the request body, defaulting to
    "localhost" when absent. The matrix page aggregates every configured host on one page
    (see acl_matrix), so it can't rely on "the" active host the way GET does; module_detail's
    own ACL tab sends its page's active host explicitly too, for the same reason GET can't
    just be reused for POST here -- a POST with no "host" must mean "localhost" even if the
    session happens to have switched to a remote host elsewhere, since silently consulting
    session state here was a real footgun during the matrix's hub-mode work (see
    ACL_MATRIX.md, Work Plan item 8).
    """
    if request.method == "GET":
        host = _active_host(request)
        if host:
            return _proxy(host, "GET", f"/api/modules/{name}/acl/")
        _get_module_or_404(name)
        acl, source, error = services.resolve_and_validate_acl(name)
        return JsonResponse({"acl": acl, "source": source, "error": error})

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    host_name = data.get("host") or "localhost"
    if host_name != "localhost":
        host = proxy.get_host_config(host_name)
        if not host:
            return JsonResponse({"success": False, "error": f"Unknown host: {host_name!r}"}, status=400)
        return _proxy(host, "POST", f"/api/modules/{name}/acl/", json={"acl": data.get("acl")})

    _get_module_or_404(name)
    acl = data.get("acl")
    if acl is not None:
        if not isinstance(acl, dict):
            return JsonResponse({"success": False, "error": "acl must be an object or null"}, status=400)
        allow, deny = acl.get("allow"), acl.get("deny")
        if allow is not None and deny is not None:
            return JsonResponse({"success": False, "error": "acl cannot have both allow and deny"}, status=400)
        if allow is not None and not isinstance(allow, dict):
            return JsonResponse({"success": False, "error": "allow must be a mapping of caller -> methods"}, status=400)
        if deny is not None and not isinstance(deny, list):
            return JsonResponse({"success": False, "error": "deny must be a list of callers"}, status=400)
        if acl.get("mode") not in (None, "enforce", "log"):
            return JsonResponse({"success": False, "error": "mode must be 'enforce' or 'log'"}, status=400)

    try:
        services.save_local_acl(name, acl)
        return JsonResponse({"success": True})
    except ValueError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=409)
    except FileNotFoundError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=404)


# ── ejabberd hub-mode delegation ────────────────────────────────────────────────

def _ejabberd_host_config() -> dict | None:
    """Resolves EJABBERD_HOST into a proxy host dict -- None means "query this instance's
    own ejabberd.py directly," matching how _active_host resolves the session's active host
    for the rest of this app's hub-mode views. Unlike _active_host, this isn't session
    state -- EJABBERD_HOST is fixed config, since ejabberd is normally one shared server for
    the whole fleet, not something an admin switches between per browser tab."""
    return proxy.get_host_config(getattr(settings, "EJABBERD_HOST", "localhost"))


def _ejabberd_status() -> dict:
    """The fleet's ejabberd snapshot, delegating to wherever EJABBERD_HOST points (see
    EJABBERD_INTEGRATION.md, Hub-mode delegation) -- calls this instance's own ejabberd.py
    directly if EJABBERD_HOST is "localhost", otherwise proxies to that host's own
    api_ejabberd_status. Never calls a remote host's EJABBERD_API_URL directly -- mod_http_api
    stays loopback-only wherever it's configured; only the existing hub-token-authenticated
    proxy mechanism crosses host boundaries."""
    host = _ejabberd_host_config()
    if host:
        return proxy.call(host, "GET", "/api/ejabberd/status/")
    return {
        "node_status": ejabberd.status(),
        "registered_count": ejabberd.stats("registeredusers"),
        "online_count": ejabberd.stats("onlineusers"),
        "connected": ejabberd.connected_users_info(),
    }


def _ejabberd_user(user: str) -> dict:
    """Live ejabberd state for one JID local-part, delegating to wherever EJABBERD_HOST
    points -- this can be a *different* host than whichever one actually runs the module
    this JID belongs to (see EJABBERD_INTEGRATION.md, Hub-mode delegation): a module's
    comm.user is always resolved locally on the host that runs it (services.get_comm_user),
    but the live ejabberd query for that identity always goes through this function
    instead, regardless of which host asked."""
    host = _ejabberd_host_config()
    if host:
        return proxy.call(host, "GET", f"/api/ejabberd/user/{user}/")
    return {
        "comm_user": user,
        "registered": ejabberd.check_account(user),
        "sessions": ejabberd.user_sessions_info(user),
        "last": ejabberd.get_last(user),
    }


# ── ejabberd API ──────────────────────────────────────────────────────────────

@require_GET
def api_ejabberd_status(request):
    """This instance's own local ejabberd snapshot -- queried directly when EJABBERD_HOST
    == "localhost" for whichever instance is rendering the dashboard, or by another
    pyobs-web-admin instance acting as a hub when its own EJABBERD_HOST names this host
    (see EJABBERD_INTEGRATION.md, Hub-mode delegation). Always answers using this
    instance's own ejabberd.py calls -- like api_acl_matrix, it has no host-awareness of
    its own; the caller decides, via EJABBERD_HOST, that this is the right instance to ask.
    """
    return JsonResponse({
        "node_status": ejabberd.status(),
        "registered_count": ejabberd.stats("registeredusers"),
        "online_count": ejabberd.stats("onlineusers"),
        "connected": ejabberd.connected_users_info(),
    })


@require_GET
def api_ejabberd_user(request, user: str):
    """Live ejabberd state for one JID local-part -- the delegation target for whichever
    instance actually hosts a module's config once it has resolved that module's
    comm.user (see EJABBERD_INTEGRATION.md, Hub-mode delegation: the module's own host and
    EJABBERD_HOST can be two different hosts entirely). No host-awareness here either, same
    reasoning as api_ejabberd_status."""
    return JsonResponse({
        "comm_user": user,
        "registered": ejabberd.check_account(user),
        "sessions": ejabberd.user_sessions_info(user),
        "last": ejabberd.get_last(user),
    })


# ── ejabberd browser-facing API ─────────────────────────────────────────────────
#
# Unlike api_ejabberd_status/api_ejabberd_user above (dumb, hub-facing, always local), these
# two are what the dashboard/module page's own JS calls directly -- they delegate per
# EJABBERD_HOST via _ejabberd_status/_ejabberd_user, so they're correct to call regardless
# of where ejabberd actually lives (see EJABBERD_INTEGRATION.md, Hub-mode delegation).

@require_GET
def api_ejabberd_summary(request):
    """Fleet-wide ejabberd summary for the dashboard's tile + per-module indicator. Not
    host-aware via _active_host like most of this app's other API views -- ejabberd is
    normally one shared server for the whole fleet (see EJABBERD_INTEGRATION.md), so this
    answers the same regardless of which host's dashboard is currently being viewed."""
    if not getattr(settings, "EJABBERD_ENABLED", False):
        return JsonResponse({"enabled": False})
    try:
        return JsonResponse({"enabled": True, **_ejabberd_status()})
    except Exception as e:
        return JsonResponse({"enabled": True, "error": str(e)}, status=502)


@require_GET
def api_module_ejabberd(request, name: str):
    """Per-module ejabberd state for the module page's Overview tab. Host-aware in two
    separate layers: which instance actually runs module `name` (session's active host,
    like every other module_detail-feeding endpoint -- proxies the whole request there if
    remote), and, once resolved locally, which host ejabberd itself lives on (EJABBERD_HOST,
    via _ejabberd_user) -- these can be two different hosts entirely. A module with no
    comm.user answers {"comm_user": None} without attempting any ejabberd query at all.

    Also reports module_running (this module's own process, via get_module_status) -- two
    modules can share the same comm.user (e.g. a "_test" copy reusing a real module's
    identity), so a live ejabberd session for that JID doesn't necessarily belong to *this*
    module; the caller must not present it as "connected" unless this module is actually the
    one running."""
    host = _active_host(request)
    if host:
        return _proxy(host, "GET", f"/api/modules/{name}/ejabberd/")
    _get_module_or_404(name)
    comm_user = services.get_comm_user(name)
    if comm_user is None:
        return JsonResponse({"comm_user": None})
    running = services.get_module_status(name) == "running"
    if not running:
        return JsonResponse({"comm_user": comm_user, "module_running": False})
    try:
        return JsonResponse({"module_running": True, **_ejabberd_user(comm_user)})
    except Exception as e:
        return JsonResponse({"comm_user": comm_user, "module_running": True, "error": str(e)}, status=502)
