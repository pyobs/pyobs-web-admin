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
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, "_hub_authenticated", False):
            return self.get_response(request)
        if not request.session.get("authenticated"):
            if not request.path_info.startswith("/login/"):
                return redirect(f"/login/?next={request.path_info}")
        return self.get_response(request)
