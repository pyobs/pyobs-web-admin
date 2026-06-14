from django.conf import settings
from django.shortcuts import redirect


class HubTokenMiddleware:
    """Runs before CsrfViewMiddleware — marks hub-authenticated requests so CSRF is skipped."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request.headers.get("X-Hub-Token", "")
        configured = getattr(settings, "HUB_TOKEN", "")
        if token and configured and token == configured:
            request._dont_enforce_csrf_checks = True
            request._hub_authenticated = True
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
