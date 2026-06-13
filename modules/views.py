import json

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from modules import services


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


def _get_module_or_404(name: str) -> str:
    try:
        services.validate_name(name)
    except ValueError:
        raise Http404("Invalid module name")
    if name not in services.list_modules():
        raise Http404(f"Module '{name}' not found")
    return name


def dashboard(request):
    modules = services.list_modules()
    return render(request, "modules/dashboard.html", {"modules": modules})


def module_detail(request, name: str):
    _get_module_or_404(name)
    config = services.get_config(name)
    return render(request, "modules/detail.html", {
        "module_name": name,
        "config": config or "",
        "active_module": name,
        "config_dir": settings.PYOBS_CONFIG_DIR,
        "log_dir": settings.PYOBS_LOG_DIR,
    })


@require_GET
def api_all_statuses(request):
    modules = services.list_modules()
    result = [{"name": m, "status": services.get_module_status(m)} for m in modules]
    return JsonResponse({"modules": result})


@require_GET
def api_status(request, name: str):
    _get_module_or_404(name)
    return JsonResponse({"status": services.get_module_status(name)})


@require_POST
def api_start(request, name: str):
    _get_module_or_404(name)
    success, output = services.start_module(name)
    return JsonResponse({"success": success, "output": output})


@require_POST
def api_stop(request, name: str):
    _get_module_or_404(name)
    success, output = services.stop_module(name)
    return JsonResponse({"success": success, "output": output})


@require_GET
def api_logs(request, name: str):
    _get_module_or_404(name)
    lines = int(request.GET.get("lines", 300))
    filter_str = request.GET.get("filter", "")
    log_lines = services.get_logs(name, lines=min(lines, 2000), filter_str=filter_str)
    return JsonResponse({"lines": log_lines})


def api_config(request, name: str):
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
