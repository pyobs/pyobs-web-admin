# pyobs-web-admin

A web-based administration interface for [pyobs](https://github.com/pyobs/pyobs-core),
the robotic telescope framework. It lets you start, stop, and restart modules, tail and
filter their logs, and view and edit their configuration files — all from a browser.

## Features

- **Dashboard** — sortable list view of all modules with:
  - Running / stopped / total summary counts plus total CPU and RAM
  - Per-module status badge, RAM, CPU, and uptime columns — click any header to sort, reset icon restores default grouping
  - Modules grouped under *Running / Stopped / Deactivated* headers when sorted by status
  - Warning/error log counts for the last 24 h (highlighted in colour if non-zero)
  - Quick start, restart, stop, and activate/deactivate buttons per module
  - *Start All*, *Restart All*, and *Stop All* bulk actions
  - Inactive modules (prefixed with `_`) are excluded from bulk start/restart
  - Responsive: on small screens the table collapses to status dot + name + log counts + actions
- **Module detail** — per-module view with four tabs:
  - *Overview* — current status, PID, uptime, CPU and memory usage, per-level log message counts (last 24 h), XMPP connection state (if enabled), start/restart/stop/activate/deactivate control
  - *Logs* — live log tail with text filter, time-range filter (click a line to set), colour-coded by severity, auto-refresh
  - *Config* — YAML editor with syntax highlighting and colour-coded `{include}` lines; included shared configs are shown as clickable links
  - *ACL* — point-and-click editor for the module's `acl:` block: click to allow/deny known modules, add other callers, toggle enforce/log mode
- **Shared configs** — `*.shared.yaml` config fragments listed in a separate sidebar section with a YAML-highlighted config editor (no start/stop controls)
- **Hub mode** — control multiple remote pyobs hosts from a single browser tab; remote hosts are listed in the sidebar and all actions are proxied transparently
- **ejabberd / XMPP status** (optional) — dashboard summary tile and per-module connected/not-connected indicator, plus a session/last-seen/registered-account block on each module's own page, for modules with a `comm.user` in their config — closes the gap between "the process is running" and "the module is actually reachable over XMPP" (see [ejabberd integration](#ejabberd-integration))
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
| Hub auth | Pre-shared token in `X-Hub-Token` header; CSRF bypassed for hub requests |

---

## Development setup

```bash
git clone https://github.com/pyobs/pyobs-web-admin.git
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
git clone https://github.com/pyobs/pyobs-web-admin.git
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

# Hub (optional — see Hub mode section)
HUB_TOKEN = ""                              # token to accept from a hub instance
HUB_HOSTS = []                              # remote hosts this instance controls

# ejabberd integration (optional — see ejabberd integration section)
EJABBERD_ENABLED = False                       # show XMPP status on the dashboard/module pages
EJABBERD_HOST = "localhost"                     # which host runs ejabberd -- "localhost" or a HUB_HOSTS name
EJABBERD_DOMAIN = ""                            # the XMPP vhost ejabberd serves
EJABBERD_API_URL = "http://127.0.0.1:5281/api"  # mod_http_api base URL
EJABBERDCTL = "ejabberdctl"                     # fallback if EJABBERD_API_URL can't be reached
```

---

## Hub mode

pyobs-web-admin can act as a hub to control multiple remote pyobs hosts from a single
browser session. When remote hosts are configured, a **Hosts** section appears at the
top of the sidebar. Clicking a host switches the active context — all subsequent
actions (start/stop/logs/config) are transparently proxied to that host's API.

### Setting up the hub

On the **hub** (the machine you browse to), add to `local_settings.py`:

```python
HUB_HOSTS = [
    {"name": "obs1", "url": "http://obs1:8765", "token": "shared-secret"},
    {"name": "obs2", "url": "http://obs2:8765", "token": "another-secret"},
]
```

On each **remote host**, set the matching token so it accepts hub requests:

```python
HUB_TOKEN = "shared-secret"   # must match the token the hub sends
```

The hub authenticates to remote instances via an `X-Hub-Token` header. Remote
instances that receive a valid token bypass the normal browser session/CSRF check,
so they can be called from the hub without a login session. The token is a
plain pre-shared string — use a long random value and keep it secret.

---

## ejabberd integration

If the `ejabberd` server `pyobs`'s XMPP comm layer connects through runs on the same host,
pyobs-web-admin can show live connection state alongside the process status it already
tracks — closing the gap between "the module's process is running" and "the module is
actually reachable over XMPP." When enabled: the dashboard gets a summary tile (how many of
*this installation's own* modules, identified by their config's `comm.user`, are currently
XMPP-connected) plus a small icon per module row; each module's own page gets a
session/last-seen/registered-account block in its Overview tab. A module with no `comm:`
block in its config (e.g. a pure HTTP module) is skipped entirely — there's nothing for it
to connect to.

### Enabling it

In `local_settings.py`:

```python
EJABBERD_ENABLED = True
EJABBERD_HOST = "localhost"           # or a HUB_HOSTS name, if ejabberd runs on a different host
EJABBERD_DOMAIN = "your-xmpp-domain"  # the vhost ejabberd serves, e.g. "pyobs.example.org"
EJABBERD_API_URL = "http://127.0.0.1:5281/api"
```

If `EJABBERD_HOST` names a `HUB_HOSTS` entry instead of `"localhost"`, every instance in the
fleet transparently proxies its ejabberd queries to that one host — only that host needs
`EJABBERD_API_URL` actually pointed at a real ejabberd; every other instance just needs
`EJABBERD_HOST` set to its name.

### ejabberd-side configuration

This talks to ejabberd's HTTP admin API (`mod_http_api`), not `ejabberdctl` — about 50–60x
faster per call, since it hits the already-running node directly instead of spawning a new
Erlang VM per invocation (`ejabberdctl` is used as a fallback only if `EJABBERD_API_URL`
can't be reached). Add this to ejabberd's own config:

```yaml
listen:
  -
    port: 5281
    ip: "127.0.0.1"         # loopback only -- see security note below
    module: ejabberd_http
    request_handlers:
      /api: mod_http_api    # add this to an *existing* listener's request_handlers if one's
                            # already on this port (e.g. for BOSH/WebSocket) -- ejabberd only
                            # allows one listener per port

modules:
  mod_http_api: {}

api_permissions:
  "console commands":
    from: [ejabberd_ctl]
    who: all
    what: "*"
  "pyobs-web-admin readonly":
    from: [mod_http_api]
    who:
      access:
        allow:
          - acl: loopback
    what:
      - "status"
      - "stats"
      - "connected_users_info"
      - "registered_users"
      - "user_sessions_info"
      - "get_last"
      - "check_account"
```

Reload ejabberd's config after adding this (`ejabberdctl reload_config`, or a restart if
that doesn't pick up the new listener). The `what:` list is a deliberate whitelist — leave
it as-is; `mod_http_api` can also expose account-management commands
(`register`/`unregister`/`change_password`) that should never be reachable here.

**Security note.** Access is IP-based, not credential-based — any request from loopback is
trusted, no password or token is involved. This blocks the network (a request from outside
the host is rejected), but **not** other processes on the same machine, which get the same
access pyobs-web-admin does. That's an accepted tradeoff for a dedicated, single-purpose
observatory control host — see `EJABBERD_INTEGRATION.md` if your threat model is different.

---

## How modules are managed

- **Discovery** — all `*.yaml` files in `PYOBS_CONFIG_DIR` (excluding `*.shared.yaml`) are treated as modules. `*.shared.yaml` files are listed separately as shared configs.
- **Activate / Deactivate** — deactivating a module renames its config from `name.yaml` to `_name.yaml` (stopping it first if running); activating renames it back. Deactivated modules are excluded from *Start All* and *Restart All*.
- **Start** — runs `pyobs --pid-file <run>/<name>.pid --log-file <log>/<name>.log --log-level <level> <config>`. pyobs daemonises itself via `python-daemon`.
- **Stop** — sends `SIGTERM` to the PID in the PID file; falls back to `SIGKILL` after 5 s.
- **Restart** — stop followed by start.
- **Status** — checks whether the process with the stored PID is alive (`os.kill(pid, 0)`).
- **Resource usage** — uptime, CPU %, and RSS memory read via `psutil` on every status poll.
- **Log counts** — per-level message counts (DEBUG / INFO / WARNING / ERROR / CRITICAL) for the last 24 h, using binary search on the log file to avoid reading the whole file.

---

## Project layout

```
pyobs_web_admin/
  settings.py               Django project settings
  local_settings.py.example Template for local overrides (not committed)
  urls.py                   URL config
modules/
  services.py               All pyobs process and filesystem logic
  views.py                  HTML pages + JSON API endpoints
  proxy.py                  HTTP client for hub → remote host calls
  ejabberd.py               mod_http_api/ejabberdctl client for XMPP status
  middleware.py             Login-required redirect + hub token auth
  context_processors.py
deploy/
  pyobs-web-admin.service   systemd unit file
templates/
  base.html                 Bootstrap 5 layout with responsive sidebar
  modules/
    dashboard.html
    detail.html
    shared_detail.html      Config editor for *.shared.yaml files
  registration/
    login.html
```
