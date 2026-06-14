import requests as _http

from django.conf import settings


def all_hosts() -> list[dict]:
    extra = [{"name": h["name"]} for h in getattr(settings, "HUB_HOSTS", [])]
    return [{"name": "localhost"}] + extra


def get_host_config(name: str) -> dict | None:
    """Returns the host dict from HUB_HOSTS, or None for localhost."""
    if name == "localhost":
        return None
    return next(
        (h for h in getattr(settings, "HUB_HOSTS", []) if h["name"] == name),
        None,
    )


def call(host: dict, method: str, path: str, json=None, params=None, timeout: int = 10):
    url = host["url"].rstrip("/") + path
    headers = {"X-Hub-Token": host["token"]}
    resp = _http.request(method, url, headers=headers, json=json, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
