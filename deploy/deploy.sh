#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pyobs/pyobs-web-admin"
SETTINGS="$INSTALL_DIR/pyobs_web_admin/local_settings.py"

# --- Password ---
while true; do
    read -rsp "Enter admin password: " password; echo
    read -rsp "Confirm admin password: " password2; echo
    [[ "$password" == "$password2" ]] && break
    echo "Passwords do not match, try again."
done

# --- Check for uv ---
if ! command -v uv &>/dev/null; then
    read -rp "uv is not installed. Install it now? [y/N] " yn
    case "$yn" in
        [yY]*)
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # shellcheck disable=SC1091
            source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
            ;;
        *)
            echo "uv is required. Aborting."
            exit 1
            ;;
    esac
fi

# --- Dependencies ---
cd "$INSTALL_DIR"
uv sync

# --- Secret key ---
SECRET_KEY=$(DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
    "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())")

# --- Password hash ---
PASSWORD_HASH=$(DJANGO_SETTINGS_MODULE=pyobs_web_admin.settings uv run python -c \
    "from django.contrib.auth.hashers import make_password; print(make_password('$password'))")
unset password password2

# --- Hostname ---
HOSTNAME=$(hostname -f)

# --- Write local_settings.py ---
cp "$INSTALL_DIR/pyobs_web_admin/local_settings.py.example" "$SETTINGS"

sed -i "s|SECRET_KEY = \"\"|SECRET_KEY = \"$SECRET_KEY\"|" "$SETTINGS"
sed -i "s|ALLOWED_HOSTS = \[\"your-hostname-or-ip\"\]|ALLOWED_HOSTS = [\"$HOSTNAME\"]|" "$SETTINGS"
sed -i "s|ADMIN_PASSWORD_HASH = \"\"|ADMIN_PASSWORD_HASH = \"$PASSWORD_HASH\"|" "$SETTINGS"

echo "Written: $SETTINGS"

# --- systemd service ---
cp "$INSTALL_DIR/deploy/pyobs-web-admin.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pyobs-web-admin

echo ""
systemctl status pyobs-web-admin --no-pager
echo ""
echo "Deployment complete. pyobs-web-admin is running at http://$HOSTNAME:8765"
