from django.urls import path

from modules import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("set-host/<str:name>/", views.set_host, name="set_host"),
    path("modules/<str:name>/", views.module_detail, name="module_detail"),
    path("shared/<str:name>/", views.shared_detail, name="shared_detail"),
    path("acl/", views.acl_matrix, name="acl_matrix"),
    path("logs/", views.all_logs, name="all_logs"),
    # API
    path("api/statuses/", views.api_all_statuses, name="api_all_statuses"),
    path("api/logs/", views.api_all_logs, name="api_all_logs"),
    path("api/modules/<str:name>/status/", views.api_status, name="api_status"),
    path("api/modules/<str:name>/start/", views.api_start, name="api_start"),
    path("api/modules/<str:name>/stop/", views.api_stop, name="api_stop"),
    path("api/modules/<str:name>/activate/", views.api_activate, name="api_activate"),
    path("api/modules/<str:name>/deactivate/", views.api_deactivate, name="api_deactivate"),
    path("api/modules/<str:name>/restart/", views.api_restart, name="api_restart"),
    path("api/modules/<str:name>/logs/", views.api_logs, name="api_logs"),
    path("api/modules/<str:name>/log-stats/", views.api_log_stats, name="api_log_stats"),
    path("api/log-stats/", views.api_all_log_stats, name="api_all_log_stats"),
    path("api/modules/<str:name>/config/", views.api_config, name="api_config"),
    path("api/shared/<str:name>/config/", views.api_shared_config, name="api_shared_config"),
    path("api/modules/<str:name>/acl/", views.api_acl, name="api_acl"),
    path("api/acl-matrix/", views.api_acl_matrix, name="api_acl_matrix"),
    path("api/ejabberd/status/", views.api_ejabberd_status, name="api_ejabberd_status"),
    path("api/ejabberd/user/<str:user>/", views.api_ejabberd_user, name="api_ejabberd_user"),
    path("api/ejabberd-summary/", views.api_ejabberd_summary, name="api_ejabberd_summary"),
    path("api/modules/<str:name>/ejabberd/", views.api_module_ejabberd, name="api_module_ejabberd"),
]
