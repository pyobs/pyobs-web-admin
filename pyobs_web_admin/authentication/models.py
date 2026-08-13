from django.conf import settings
from django.db import models


class KeycloakIdentity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Keycloak's `sub` claim - the join key for pyobs-auth's USER_RESOLVER, not username/email
    # (those can change; `sub` doesn't). See pyobs-core's shared-auth design doc.
    keycloak_sub = models.CharField(max_length=255, unique=True)
