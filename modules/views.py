import json

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from modules import proxy, services


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
    return render(request, "modules/dashboard.html", {"modules": modules})


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
        result.append({"name": m, "status": status, "stats": stats})
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
