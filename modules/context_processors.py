from modules import proxy, services


def _sort_modules(modules_with_status: list[dict]) -> list[dict]:
    return sorted(modules_with_status, key=lambda m: (0 if m["status"] == "running" else 1, m["name"]))


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
        names = services.list_modules()
        modules = _sort_modules([
            {"name": n, "status": services.get_module_status(n)}
            for n in names
        ])
        shared = services.list_shared_configs()

    return {
        "sidebar_modules": modules,
        "sidebar_shared_configs": shared,
        "hub_hosts": proxy.all_hosts(),
        "active_host": active_host,
    }
