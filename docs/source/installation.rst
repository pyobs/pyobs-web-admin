Installation
############

Development
***********

::

    git clone https://github.com/pyobs/pyobs-web-admin.git
    cd pyobs-web-admin
    uv sync
    uv run python manage.py runserver

Then create :file:`pyobs_web_admin/local_settings.py` -- see :doc:`configuration`. In
development you can skip most of it; the defaults in
:file:`pyobs_web_admin/settings.py` (``DEBUG = True``, permissive ``ALLOWED_HOSTS``) are
enough to run the app locally against a scratch ``PYOBS_CONFIG_DIR``.

Tests run with::

    uv run python manage.py test modules


Production
**********

1. Install the app
===================

::

    cd /opt/pyobs
    git clone https://github.com/pyobs/pyobs-web-admin.git
    cd pyobs-web-admin
    uv sync

2. Configure
=============

Create :file:`pyobs_web_admin/local_settings.py` -- see :doc:`configuration` for the full
reference. At minimum, production needs::

    DEBUG = False
    SECRET_KEY = "..."          # generate below
    ALLOWED_HOSTS = ["your-hostname-or-ip"]
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD_HASH = "..." # generate below
    PYOBS_EXEC = "/opt/pyobs/venv/bin/pyobs"

Generate a secret key::

    DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
      "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

Generate a password hash::

    DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
      "from django.contrib.auth.hashers import make_password; print(make_password('yourpassword'))"

3. Install the systemd service
================================

::

    cp deploy/pyobs-web-admin.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now pyobs-web-admin

Check that it started::

    systemctl status pyobs-web-admin
    journalctl -u pyobs-web-admin -f

4. Configure nginx
===================

Add a site configuration that proxies to gunicorn on port 8765::

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

If you add TLS (strongly recommended for any non-private network), also set in
:file:`local_settings.py`::

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
