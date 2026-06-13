from django.shortcuts import redirect


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.session.get("authenticated"):
            if not request.path_info.startswith("/login/"):
                return redirect(f"/login/?next={request.path_info}")
        return self.get_response(request)
