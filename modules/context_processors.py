import tomllib

from django.conf import settings
from django.templatetags.static import static

from modules import proxy, services


def _sort_modules(modules_with_status: list[dict]) -> list[dict]:
    return sorted(modules_with_status, key=lambda m: (0 if m["status"] == "running" else 1, m["name"]))


# Cached for the life of the process: this app isn't installed as a distribution (no
# [build-system] in pyproject.toml, run straight from source via uv), so there's no
# importlib.metadata entry to read -- pyproject.toml itself is the only source of truth, and
# it can't change without a redeploy that restarts the process anyway.
_web_admin_version_cache: str | None = None


def _web_admin_version() -> str | None:
    global _web_admin_version_cache
    if _web_admin_version_cache is None:
        try:
            data = tomllib.loads((settings.BASE_DIR / "pyproject.toml").read_text())
            _web_admin_version_cache = data.get("project", {}).get("version", "") or ""
        except OSError:
            _web_admin_version_cache = ""
    return _web_admin_version_cache or None


def sidebar_modules(request):
    active_host = request.session.get("active_host", "localhost")
    host_config = proxy.get_host_config(active_host)

    if host_config:
        try:
            data = proxy.call(host_config, "GET", "/api/statuses/")
            modules = _sort_modules([
                {"name": m["name"], "status": m.get("status", "unknown")}
                for m in data.get("modules", [])
            ])
        except Exception:
            modules = []
        shared = []
    else:
        raw_names = services.list_modules()
        modules = _sort_modules([
            {"name": n, "status": services.get_module_status(n)}
            for n in raw_names
            if services._is_valid_module_name(n)
        ])
        shared = services.list_shared_configs()

    return {
        "sidebar_modules": modules,
        "sidebar_shared_configs": shared,
        "hub_hosts": proxy.all_hosts(),
        "active_host": active_host,
        # Global so the sidebar's Users link (like the Hosts section already does for
        # HUB_HOSTS) can gate on it from base.html regardless of which page is rendering --
        # individual views (dashboard, module_detail) also set this in their own context,
        # which takes precedence over this processor when both provide it.
        "ejabberd_enabled": getattr(settings, "EJABBERD_ENABLED", False),
        "keycloak_login_enabled": bool(getattr(settings, "PYOBS_AUTH", {}).get("SERVER_URL")),
        # IdP hint/label for the one-click IdP login button - see PYOBS_AUTH in settings.py.
        # The template additionally gates on keycloak_login_enabled, so IDP_HINT without
        # SERVER_URL (Keycloak disabled) degrades to no buttons rather than dead links.
        "keycloak_idp_hint": getattr(settings, "PYOBS_AUTH", {}).get("IDP_HINT", ""),
        "keycloak_idp_label": getattr(settings, "PYOBS_AUTH", {}).get("IDP_LABEL", ""),
        "git_enabled": getattr(settings, "PYOBS_CONFIG_GIT_ENABLED", False),
        "acl_matrix_enabled": getattr(settings, "PYOBS_CONFIG_ACL_MATRIX_ENABLED", False),
        "web_admin_version": _web_admin_version(),
        # Deployments can point these at their own logo via settings/env; default to
        # the bundled pyobs logo. Two variants because the wordmark's "py" is black -
        # invisible on a dark sidebar without a light-on-dark version to swap in.
        "pyobs_logo_light_url": getattr(settings, "PYOBS_LOGO_LIGHT_URL", None) or static("img/pyobs-logo-light.gif"),
        "pyobs_logo_dark_url": getattr(settings, "PYOBS_LOGO_DARK_URL", None) or static("img/pyobs-logo-dark.gif"),
    }

