import hmac

from django.conf import settings
from django.shortcuts import redirect


def _configured_clients() -> list[dict]:
    """Named external-caller tokens, plus the legacy flat HUB_TOKEN as a "default" client."""
    clients = list(getattr(settings, "HUB_CLIENTS", []))
    legacy = getattr(settings, "HUB_TOKEN", "")
    if legacy:
        clients.append({"name": "default", "token": legacy})
    return clients


class HubTokenMiddleware:
    """Runs before CsrfViewMiddleware — marks hub-authenticated requests so CSRF is skipped."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request.headers.get("X-Hub-Token", "")
        if token:
            for client in _configured_clients():
                if hmac.compare_digest(token, client["token"]):
                    request._dont_enforce_csrf_checks = True
                    request._hub_authenticated = True
                    request._hub_client = client["name"]
                    break
        return self.get_response(request)


class LoginRequiredMiddleware:
    """Two independent logins share this gate: the shared admin/password account (a plain
    session["authenticated"] flag) and Keycloak (a real django.contrib.auth User, via
    request.user - AuthenticationMiddleware must run before this)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, "_hub_authenticated", False):
            return self.get_response(request)
        authenticated = (
            request.session.get("authenticated") or request.user.is_authenticated
        )
        if not authenticated:
            # /admin/ is exempt too - Django's own admin login (is_staff-gated) handles it,
            # not this app's /login/ page, since neither the shared admin/password account nor
            # a plain (non-staff) Keycloak-linked User should get into it.
            exempt = (
                request.path_info.startswith("/login/")
                or request.path_info.startswith("/accounts/keycloak/")
                or request.path_info.startswith("/admin/")
            )
            if not exempt:
                return redirect(f"/login/?next={request.path_info}")
        return self.get_response(request)
