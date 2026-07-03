import json

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
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
    return redirect("/")


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
        return render(request, "modules/detail.html", {
            "module_name": name,
            "config": config,
            "active_module": name,
            "config_dir": "(remote)",
            "log_dir": "(remote)",
        })
    _get_module_or_404(name)
    config = services.get_config(name)
    return render(request, "modules/detail.html", {
        "module_name": name,
        "config": config or "",
        "active_module": name,
        "config_dir": settings.PYOBS_CONFIG_DIR,
        "log_dir": settings.PYOBS_LOG_DIR,
    })


def shared_detail(request, name: str):
    _get_shared_or_404(name)
    return render(request, "modules/shared_detail.html", {
        "config_name": name,
        "config": services.get_shared_config(name) or "",
        "active_shared": name,
        "config_dir": settings.PYOBS_CONFIG_DIR,
    })


def acl_matrix(request):
    host = _active_host(request)
    if host:
        # Hub mode aggregation isn't wired up yet (see DEVELOPMENT.md Work Plan) --
        # build_acl_matrix() only reads the local PYOBS_CONFIG_DIR.
        return render(request, "modules/acl_matrix.html", {
            "active_acl_matrix": True,
            "hub_unsupported": True,
        })
    matrix = services.build_acl_matrix()
    callers = matrix["callers"]
    source_counts: dict[str, int] = {}
    for row in matrix["targets"]:
        if row["source"]:
            source_counts[row["source"]] = source_counts.get(row["source"], 0) + 1
    rows = []
    for row in matrix["targets"]:
        mode = row["acl"].get("mode", "enforce") if row["acl"] else "enforce"
        rows.append({
            **row,
            "mode": mode,
            "source_count": source_counts.get(row["source"]) if row["source"] else None,
            "cell_list": [{"caller": c, **row["cells"][c]} for c in callers],
        })
    return render(request, "modules/acl_matrix.html", {
        "active_acl_matrix": True,
        "targets": rows,
        "callers": callers,
        "module_names": set(services.list_modules()),
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
