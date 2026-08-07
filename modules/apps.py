from django.apps import AppConfig


class ModulesConfig(AppConfig):
    name = "modules"

    def ready(self) -> None:
        from modules.services import _ensure_symlink, _git_enabled
        if _git_enabled():
            _ensure_symlink()
