from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-y*5=%i%$(#iy+p8tmpq)ao0hby$7huewxp2^^zmpvb1y1*nbc!"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "modules",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "modules.middleware.HubTokenMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "modules.middleware.LoginRequiredMiddleware",
]

ROOT_URLCONF = "pyobs_web_admin.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "modules.context_processors.sidebar_modules",
            ],
        },
    },
]

WSGI_APPLICATION = "pyobs_web_admin.wsgi.application"

# Sessions stored in signed cookies — no database needed
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Single-user credentials — set ADMIN_PASSWORD_HASH in local_settings.py:
#   uv run python -c "from django.contrib.auth.hashers import make_password; print(make_password('yourpassword'))"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = ""

# Hub: list of remote hosts this instance can control
# Each entry: {"name": "obs1", "url": "http://obs1:8765", "token": "shared-secret"}
HUB_HOSTS = []
# Token to accept from a hub instance (allows hub to call this instance's API).
# Deprecated in favour of HUB_CLIENTS below, kept for backwards compatibility --
# equivalent to a HUB_CLIENTS entry named "default".
HUB_TOKEN = ""
# Named tokens for external callers (a hub, a script, another service). Each caller
# gets its own secret so it can be revoked/rotated independently of the others.
# Each entry: {"name": "hub-monets", "token": "shared-secret"}
HUB_CLIENTS = []

# pyobs paths
PYOBS_EXEC = "pyobs"
PYOBS_CONFIG_DIR = "/opt/pyobs/config"
PYOBS_LOG_DIR = "/opt/pyobs/log"
PYOBS_RUN_DIR = "/opt/pyobs/run"
PYOBS_LOG_LEVEL = "info"

# Packages page (see modules/services.py's "Package management" section): what's actually
# installed (via `pip list`) is always the primary source of truth for which pyobs-*
# packages exist and what version they're at -- this setting never invents an entry for a
# package that isn't really installed. It's only for two things pip's installed-environment
# metadata has no other way to recover:
#   - Extras: pip never records which extras (if any) a package was originally installed
#     with, so a bare "pyobs-core" upgrade would silently drop a "[full]" extra forever.
#     List "pyobs-core[full]" here and future upgrades keep using that same spec.
#   - Non-"pyobs"-prefixed packages: list a bare name (e.g. "my-custom-driver") to have it
#     show up on the Packages page and be upgradable through it too, alongside the pyobs-*
#     packages it always already covers.
#   - Git/URL-installed packages: a PEP 508 direct reference, e.g.
#     "pyobs-iagvt[gui] @ git+https://gitlab.example.org/iagvt/pyobs-iagvt.git", for a
#     package that isn't published on PyPI at all. The Packages page skips the (futile)
#     PyPI version check for these and instead offers a manual "Reinstall" action that
#     re-runs `pip install --upgrade <spec>` to pick up whatever's newest at that URL/ref.
# PYOBS_MANAGED_PACKAGES = [
#     "pyobs-core[full]",
#     "my-custom-driver",
#     "pyobs-iagvt[gui] @ git+https://gitlab.example.org/iagvt/pyobs-iagvt.git",
# ]
PYOBS_MANAGED_PACKAGES = []

# Where module logs live -- see DEV_JOURNALD_LOGS.md. "file": pyobs writes to PYOBS_LOG_DIR,
# read back with tail. "journald": pyobs is started with --syslog instead of --log-file,
# read back with journalctl. Fleet-wide switch, not per-module -- see that doc's Design
# section for why.
#
# None (the default): auto-detect from pyobsd's own config file instead of requiring this
# set a second time -- pyobsd (pyobs-core's daemon manager) already reads its own global
# config (~/.config/pyobs.yaml, /etc/pyobs.yaml, or /opt/pyobs/storage/pyobs.yaml, first
# found wins) for a "pyobsd: syslog: true/false" key that decides whether *it* starts
# modules with --syslog. Reading that same file here means this can never silently drift
# out of sync with what pyobsd actually does. Set explicitly ("file" or "journald") to
# override auto-detection.
PYOBS_LOG_BACKEND = None

# ejabberd integration -- see DEV_EJABBERD_INTEGRATION.md. Off by default: not every fleet has
# ejabberd co-located. EJABBERD_HOST names whichever host in HUB_HOSTS (or "localhost")
# actually runs it -- every other host proxies through to that one rather than talking to
# ejabberd's HTTP API directly, which stays loopback-only wherever it's configured.
EJABBERD_ENABLED = False
EJABBERD_HOST = "localhost"
EJABBERD_DOMAIN = ""
EJABBERD_API_URL = "http://127.0.0.1:5281/api"
EJABBERDCTL = "ejabberdctl"

try:
    from pyobs_web_admin.local_settings import *  # noqa: F401,F403
except ImportError:
    pass
