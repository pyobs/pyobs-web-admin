from modules import services


def sidebar_modules(request):
    return {
        "sidebar_modules": services.list_modules(),
        "sidebar_shared_configs": services.list_shared_configs(),
    }
