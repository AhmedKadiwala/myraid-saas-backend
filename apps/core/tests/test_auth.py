from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Tenant, TenantMembership, User


class AuthenticationTestCase(TestCase):
    @override_settings(DEBUG=True, ERP_OTP_MODE="mock", ERP_DEV_FIXED_OTP="654321", ERP_SMS_ENABLED=False)
    def test_login_refresh_and_user_info(self):
        tenant = Tenant.objects.create(name="Alpha", slug="alpha", status="active")
        user = User.objects.create_user(
            email="admin@example.com", phone="9876500009",
            password="secret12", first_name="A", last_name="User",
        )
        TenantMembership.objects.create(tenant=tenant, user=user)
        api = APIClient()
        password_login = api.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": "secret12"},
            format="json",
        )
        self.assertEqual(password_login.status_code, 200)
        requested = api.post("/api/v1/erp/auth/otp/request-otp/", {"phone":user.phone}, format="json")
        self.assertEqual(requested.status_code, 200, requested.data)
        login = api.post("/api/v1/erp/auth/otp/verify-otp/", {
            "phone":user.phone, "code":requested.data["mock_otp"], "otp_token":requested.data["otp_token"],
        }, format="json")
        self.assertEqual(login.status_code, 200)
        self.assertIn("access", login.data)
        self.assertIn("refresh", login.data)
        csrf = login.cookies["csrftoken"].value
        refreshed = api.post(
            "/api/v1/auth/refresh",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data)
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}",
            HTTP_X_TENANT_ID=str(tenant.pk),
        )
        profile = api.get("/api/v1/auth/user-info")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data["detail"]["email"], user.email)
