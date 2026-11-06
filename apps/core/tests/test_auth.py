from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Tenant, TenantMembership, User

TEST_CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@override_settings(
    GOOGLE_CLIENT_ID=TEST_CLIENT_ID,
    ERP_PUBLIC_APP_URL="http://localhost:5173",
)
class AuthenticationTestCase(TestCase):
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_google_login_refresh_and_user_info(self, mock_verify):
        mock_verify.return_value = {
            "sub": "test-google-sub-999",
            "email": "user@alpha.com",
            "email_verified": True,
            "iss": "https://accounts.google.com",
            "aud": TEST_CLIENT_ID,
            "given_name": "Alpha",
            "family_name": "User",
        }

        tenant = Tenant.objects.create(name="Alpha", slug="alpha", status="active")
        user = User.objects.create_user(
            email="user@alpha.com",
            first_name="Alpha",
            last_name="User",
        )
        TenantMembership.objects.create(tenant=tenant, user=user)

        api = APIClient()

        # Google Sign-In
        login_res = api.post(
            "/api/v1/auth/google/signin",
            {"credential": "mock.google.jwt"},
            format="json",
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertIn("access", login_res.data)
        self.assertIn("refresh", login_res.data)

        csrf = login_res.cookies["csrftoken"].value

        # Refresh
        refreshed = api.post(
            "/api/v1/auth/refresh",
            {"refresh": login_res.data["refresh"]},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data)

        # Profile access
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}",
            HTTP_X_TENANT_ID=str(tenant.pk),
        )
        profile = api.get("/api/v1/auth/user-info")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data["detail"]["email"], user.email)

    def test_superuser_password_login_works_but_standard_is_blocked(self):
        api = APIClient()
        admin_user = User.objects.create_superuser(
            email="superadmin@example.com",
            password="SecureAdminPassword123!",
        )

        # Superuser login succeeds
        res_admin = api.post(
            "/api/v1/auth/login",
            {"email": "superadmin@example.com", "password": "SecureAdminPassword123!"},
            format="json",
        )
        self.assertEqual(res_admin.status_code, 200)

        # Regular user password login is rejected
        normal_user = User.objects.create_user(
            email="standard@example.com",
            password="StandardUserPassword123!",
            is_staff=False,
            is_superuser=False,
        )
        res_normal = api.post(
            "/api/v1/auth/login",
            {"email": "standard@example.com", "password": "StandardUserPassword123!"},
            format="json",
        )
        self.assertEqual(res_normal.status_code, 403)
