from django.contrib import admin
from django.urls import include, path

from modules.views import login_view, logout_view

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("accounts/keycloak/", include("pyobs_auth.urls")),
    # Django's own admin: only place to activate/deactivate a Keycloak-linked User's is_active
    # (see pyobs_web_admin.authentication.keycloak.resolve_user). Reachable with the shared
    # admin/password login (login_view logs that in as a real superuser User too, see there) or
    # any other superuser User - is_staff-gated by Django itself rather than
    # LoginRequiredMiddleware (see that middleware's exemption for "/admin/").
    path("admin/", admin.site.urls),
    path("", include("modules.urls")),
]
