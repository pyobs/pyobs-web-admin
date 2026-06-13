# pyobs-web-admin

A web-based administration interface for [pyobs](https://github.com/pyobs/pyobs-core),
the robotic telescope framework. It lets you start, stop, and restart modules, tail and
filter their logs, and view and edit their configuration files — all from a browser.

## Features

- **Dashboard** — overview of all modules with:
  - Running / stopped / total summary counts
  - Per-module status badge, uptime, CPU usage, and memory usage (RSS)
  - Warning/error log counts for the last 24 h (highlighted in colour if non-zero)
  - Quick start, restart, and stop buttons per module
  - *Start All*, *Restart All*, and *Stop All* bulk actions (modules whose names start with `_` are excluded from start/restart)
- **Module detail** — per-module view with three tabs:
  - *Overview* — current status, uptime, CPU and memory usage, per-level log message counts (last 24 h), start/restart/stop control
  - *Logs* — live log tail with text filter, time-range filter (click a line to set), colour-coded by severity, auto-refresh
  - *Config* — YAML editor with syntax highlighting and colour-coded `{include}` lines; included shared configs are shown as clickable links
- **Shared configs** — `*.shared.yaml` config fragments listed in a separate sidebar section with a YAML-highlighted config editor (no start/stop controls)
- **Responsive** — works on mobile with a slide-in sidebar
- **No pyobs-core dependency** — communicates with `pyobs` directly via subprocess; no Python imports from pyobs-core

## Technology

| Layer | Choice |
|---|---|
| Backend | Python 3.13, Django 6, psutil |
| WSGI server | Gunicorn |
| Frontend | Bootstrap 5 (CDN), CodeMirror 5 (CDN), vanilla JS |
| Package manager | uv |
| Auth | Single-user, password hash in `local_settings.py`, cookie sessions (no database) |

---

## Development setup

```bash
git clone git@github.com:pyobs/pyobs-web-admin.git
cd pyobs-web-admin
uv sync
uv run python manage.py runserver
```

Create `pyobs_web_admin/local_settings.py` (see [Configuration](#configuration) below).

---

## Production setup

### 1. Install the app

```bash
cd /opt/pyobs
git clone git@github.com:pyobs/pyobs-web-admin.git
cd pyobs-web-admin
uv sync
```

### 2. Configure

Copy and edit the local settings file:

```bash
cp pyobs_web_admin/local_settings.py.example pyobs_web_admin/local_settings.py   # or create from scratch
$EDITOR pyobs_web_admin/local_settings.py
```

Minimum required settings for production (see [Configuration](#configuration)):

```python
DEBUG = False
SECRET_KEY = "..."          # generate below
ALLOWED_HOSTS = ["your-hostname-or-ip"]
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = "..." # generate below
PYOBS_EXEC = "/opt/pyobs/venv/bin/pyobs"
```

Generate a secret key:

```bash
DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
  "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Generate a password hash:

```bash
DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
  "from django.contrib.auth.hashers import make_password; print(make_password('yourpassword'))"
```

### 3. Install the systemd service

```bash
cp deploy/pyobs-web-admin.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pyobs-web-admin
```

Check that it started:

```bash
systemctl status pyobs-web-admin
journalctl -u pyobs-web-admin -f
```

### 4. Configure nginx

Add a site configuration that proxies to gunicorn on port 8765:

```nginx
server {
    listen 80;
    server_name your-hostname-or-ip;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

If you add TLS (strongly recommended for any non-private network), also set in
`local_settings.py`:

```python
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
```

---

## Configuration

All runtime configuration lives in `pyobs_web_admin/local_settings.py`, which is not
committed to version control. A full reference:

```python
# Django
SECRET_KEY = "..."           # required in production
DEBUG = False                # set True only in development
ALLOWED_HOSTS = ["*"]        # restrict to hostname/IP in production

# HTTPS (enable once TLS is in place)
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# Authentication
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = "pbkdf2_sha256$..."   # see generation command above

# pyobs paths
PYOBS_EXEC = "/opt/pyobs/venv/bin/pyobs"   # path to the pyobs executable
PYOBS_CONFIG_DIR = "/opt/pyobs/config"      # directory containing *.yaml module configs
PYOBS_LOG_DIR = "/opt/pyobs/log"            # directory containing *.log files
PYOBS_RUN_DIR = "/opt/pyobs/run"            # directory for PID files
PYOBS_LOG_LEVEL = "info"                    # log level passed to pyobs on start
```

---

## How modules are managed

- **Discovery** — all `*.yaml` files in `PYOBS_CONFIG_DIR` (excluding `*.shared.yaml`) are treated as modules. `*.shared.yaml` files are listed separately as shared configs.
- **Start** — runs `pyobs --pid-file <run>/<name>.pid --log-file <log>/<name>.log --log-level <level> <config>`. pyobs daemonises itself via `python-daemon`.
- **Stop** — sends `SIGTERM` to the PID in the PID file; falls back to `SIGKILL` after 5 s.
- **Restart** — stop followed by start.
- **Status** — checks whether the process with the stored PID is alive (`os.kill(pid, 0)`).
- **Resource usage** — uptime, CPU %, and RSS memory read via `psutil` on every status poll.

---

## Project layout

```
pyobs_web_admin/      Django project settings and URL config
modules/
  services.py         All pyobs process and filesystem logic
  views.py            HTML pages + JSON API endpoints
  middleware.py       Login-required redirect
  context_processors.py
deploy/
  pyobs-web-admin.service   systemd unit file
templates/
  base.html                 Bootstrap 5 layout with responsive sidebar
  modules/
    dashboard.html
    detail.html
  registration/
    login.html
```
