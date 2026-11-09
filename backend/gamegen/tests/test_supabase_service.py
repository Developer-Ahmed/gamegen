from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from gamegen.supabase.service import SupabaseService


@override_settings(
    SUPABASE_URL="https://project.supabase.co",
    SUPABASE_ANON_KEY="anon-secret-value",
    SUPABASE_SERVICE_ROLE_KEY="service-secret-value",
)
class SupabaseServiceLoggingTests(SimpleTestCase):
    def test_request_logs_never_include_credentials_or_payloads(self):
        response = SimpleNamespace(
            status_code=200,
            headers={"sb-request-id": "request-123", "set-cookie": "private-cookie"},
            content=b'{"access_token":"response-secret"}',
            json=lambda: {"access_token": "response-secret"},
            raise_for_status=lambda: None,
        )
        with (
            patch("gamegen.supabase.service.requests.request", return_value=response),
            self.assertLogs("gamegen.supabase.service", level="INFO") as captured,
        ):
            result = SupabaseService()._make_request(
                "POST",
                "/auth/v1/token",
                auth_token="bearer-secret-value",
                data={"password": "payload-secret-value"},
            )

        output = "\n".join(captured.output)
        for secret in (
            "anon-secret-value",
            "service-secret-value",
            "bearer-secret-value",
            "payload-secret-value",
            "response-secret",
            "private-cookie",
        ):
            self.assertNotIn(secret, output)
        self.assertEqual(result, {"access_token": "response-secret"})
