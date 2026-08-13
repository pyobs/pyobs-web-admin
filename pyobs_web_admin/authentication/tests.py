from django.contrib.auth.models import User
from django.test import TestCase

from pyobs_web_admin.authentication.keycloak import resolve_user
from pyobs_web_admin.authentication.models import KeycloakIdentity


class ResolveUserTests(TestCase):
    def test_creates_a_new_user_on_first_login(self):
        user = resolve_user(
            {
                "sub": "sub-1",
                "email": "new@example.org",
                "preferred_username": "newperson",
            }
        )

        self.assertEqual(user.username, "newperson")
        self.assertEqual(user.email, "new@example.org")
        self.assertEqual(KeycloakIdentity.objects.get(user=user).keycloak_sub, "sub-1")

    def test_new_user_is_created_inactive(self):
        user = resolve_user({"sub": "sub-2", "email": "pending@example.org"})
        self.assertFalse(user.is_active)

    def test_same_sub_resolves_to_the_same_user_on_a_later_login(self):
        first = resolve_user({"sub": "sub-3", "email": "person@example.org"})
        second = resolve_user({"sub": "sub-3", "email": "person@example.org"})

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(User.objects.filter(email="person@example.org").count(), 1)

    def test_links_an_existing_user_by_email_on_first_keycloak_login(self):
        existing = User.objects.create(username="oldstyle", email="legacy@example.org")

        user = resolve_user({"sub": "sub-4", "email": "legacy@example.org"})

        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(
            KeycloakIdentity.objects.get(user=existing).keycloak_sub, "sub-4"
        )

    def test_links_an_existing_user_by_username_when_email_does_not_match(self):
        existing = User.objects.create(username="noemail")

        user = resolve_user(
            {
                "sub": "sub-5",
                "email": "noemail@example.org",
                "preferred_username": "noemail",
            }
        )

        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(
            KeycloakIdentity.objects.get(user=existing).keycloak_sub, "sub-5"
        )

    def test_falls_back_to_sub_as_username_without_preferred_username(self):
        user = resolve_user({"sub": "sub-6", "email": "no-username@example.org"})
        self.assertEqual(user.username, "sub-6")
