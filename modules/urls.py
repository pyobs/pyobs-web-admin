from django.urls import path

from modules import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("modules/<str:name>/", views.module_detail, name="module_detail"),
    path("shared/<str:name>/", views.shared_detail, name="shared_detail"),
    # API
    path("api/statuses/", views.api_all_statuses, name="api_all_statuses"),
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
]
