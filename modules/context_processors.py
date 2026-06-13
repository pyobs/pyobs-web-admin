from modules import services


def sidebar_modules(request):
    return {"sidebar_modules": services.list_modules()}
