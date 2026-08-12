"""pyobs-auth USER_RESOLVER for web-admin.

Mirrors pyobs-archive/pyobs-robotic-backend's resolver: Keycloak's `sub` claim is the join key
(see pyobs-core's shared-auth design doc), stored on KeycloakIdentity. First Keycloak login for
an existing local User (matched by email, falling back to username) links the two rather than
minting a second, disconnected User. Newly-minted accounts default to is_active=False -
pyobs-auth's CallbackView/KeycloakAuthentication refuse an inactive user, so a fresh Keycloak
login needs local activation (Django admin, or `manage.py shell`) before it can do anything -
this is also the revocation mechanism: flip is_active back to False to cut a single person's
access without touching anyone else or the shared admin/password login.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User

from pyobs_web_admin.authentication.models import KeycloakIdentity


def resolve_user(claims: dict[str, Any]) -> User | None:
    sub = claims["sub"]

    try:
        return KeycloakIdentity.objects.get(keycloak_sub=sub).user
    except KeycloakIdentity.DoesNotExist:
        pass

    email = claims.get("email")
    username = claims.get("preferred_username") or sub

    user = User.objects.filter(email=email).first() if email else None
    if user is None:
        # Falls back to username since email matching alone misses accounts that predate
        # having an email address set - without this, User.objects.create() below hits a
        # UNIQUE constraint on username instead of linking the existing account.
        user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.create(
            username=username, email=email or "", is_active=False
        )

    KeycloakIdentity.objects.update_or_create(user=user, defaults={"keycloak_sub": sub})
    return user
