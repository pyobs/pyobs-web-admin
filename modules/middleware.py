from django.shortcuts import redirect

_EXEMPT = ("/login/", "/admin/")


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            if not any(request.path_info.startswith(p) for p in _EXEMPT):
                return redirect(f"/login/?next={request.path_info}")
        return self.get_response(request)
