from modules import proxy, services


def sidebar_modules(request):
    active_host = request.session.get("active_host", "localhost")
    host_config = proxy.get_host_config(active_host)

    if host_config:
        try:
            data = proxy.call(host_config, "GET", "/api/statuses/")
            modules = [m["name"] for m in data.get("modules", [])]
        except Exception:
            modules = []
        shared = []  # shared configs are local-only for now
    else:
        modules = services.list_modules()
        shared = services.list_shared_configs()

    return {
        "sidebar_modules": modules,
        "sidebar_shared_configs": shared,
        "hub_hosts": proxy.all_hosts(),
        "active_host": active_host,
    }
