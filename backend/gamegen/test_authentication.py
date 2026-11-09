from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import AuthenticationFailed

from .auth.advanced_view import get_session, refresh_session, sign_in_with_email, sign_out, signup
from .auth.middleware import SupabaseJWTAuthentication, _access_token_from_request


class AccessTokenExtractionTests(SimpleTestCase):
    @staticmethod
    def request(*, authorization: str = "", cookie: str = ""):
        return SimpleNamespace(
            headers={"Authorization": authorization} if authorization else {},
            COOKIES={"access_token": cookie} if cookie else {},
        )

    def test_accepts_raw_access_token_cookie_written_by_login(self):
        self.assertEqual(
            _access_token_from_request(self.request(cookie="jwt-token")),
            "jwt-token",
        )

    def test_accepts_legacy_bearer_prefixed_cookie(self):
        self.assertEqual(
            _access_token_from_request(self.request(cookie="Bearer jwt-token")),
            "jwt-token",
        )

    def test_prefers_standard_authorization_header(self):
        self.assertEqual(
            _access_token_from_request(
                self.request(authorization="Bearer header-token", cookie="cookie-token")
            ),
            "header-token",
        )

    def test_returns_none_without_credentials(self):
        self.assertIsNone(_access_token_from_request(self.request()))

    def test_rejects_malformed_authorization_header(self):
        with self.assertRaises(AuthenticationFailed):
            _access_token_from_request(self.request(authorization="Basic credentials"))

    @patch("gamegen.auth.middleware.User.objects.get")
    @patch("gamegen.auth.middleware.auth_service.get_session")
    def test_authentication_delegates_algorithm_independent_verification_to_supabase(
        self,
        get_session_mock,
        get_user_mock,
    ):
        get_session_mock.return_value = {
            "id": "user-id",
            "email": "user@example.com",
            "app_metadata": {"roles": ["player"]},
        }
        user = SimpleNamespace()
        get_user_mock.return_value = user

        authenticated_user, verified = SupabaseJWTAuthentication().authenticate(
            self.request(cookie="asymmetric-jwt")
        )

        get_session_mock.assert_called_once_with("asymmetric-jwt")
        self.assertIs(authenticated_user, user)
        self.assertEqual(verified["id"], "user-id")
        self.assertEqual(user.supabase_roles, ["player"])

    def test_public_auth_views_ignore_stale_default_authentication(self):
        for view in (signup, sign_in_with_email, sign_out, get_session, refresh_session):
            self.assertEqual(view.cls.authentication_classes, [])
