"""pyobs-auth USER_RESOLVER for web-admin.

Mirrors pyobs-archive/pyobs-portal's resolver: Keycloak's `sub` claim is the join key
(see pyobs-core's shared-auth design doc), stored on KeycloakIdentity. First Keycloak login for
an existing local User (matched by email, falling back to username) links the two rather than
minting a second, disconnected User. Newly-minted accounts are active by default: authorization
is now the PYOBS_AUTH['REQUIRED_GROUPS'] claims gate (Keycloak group membership), not local
activation - see pyobs-core's specs/design/shared-authz-keycloak.md. Granting/revoking a
person's access to web-admin is done in the Keycloak admin console (group membership) rather
than this app's Django admin.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User

from pyobs_web_admin.authentication.models import KeycloakIdentity


def resolve_user(claims: dict[str, Any]) -> User | None:
    sub = claims["sub"]

    try:
        return KeycloakIdentity.objects.get(keycloak_sub=sub).user  # pyrefly: ignore [missing-attribute]
    except KeycloakIdentity.DoesNotExist:  # pyrefly: ignore [missing-attribute]
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
            username=username, email=email or "", is_active=True
        )

    KeycloakIdentity.objects.update_or_create(  # pyrefly: ignore [missing-attribute]
        user=user, defaults={"keycloak_sub": sub}
    )
    return user
