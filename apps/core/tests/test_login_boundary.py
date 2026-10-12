from datetime import timedelta
from django.test import TestCase, override_settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from apps.core.models import Tenant, TenantMembership, User


class LoginBoundaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="login@boundary.test", phone="9876543218", first_name="Login Owner")
        self.tenant = Tenant.objects.create(name="Login Company", slug="login-company", status="active")
        TenantMembership.objects.create(user=self.user, tenant=self.tenant, is_tenant_admin=True)
        self.api = APIClient(enforce_csrf_checks=True)

    def test_expired_token_is_authentication_failure_not_tenant_forbidden(self):
        token = RefreshToken.for_user(self.user).access_token
        token.set_exp(from_time=timezone.now()-timedelta(minutes=20), lifetime=timedelta(minutes=1))
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(token), HTTP_X_TENANT_ID=str(self.tenant.pk))
        for route in ("/api/v1/erp/bootstrap/", "/api/v1/workspace-ui/workspace/"):
            response = self.api.get(route)
            self.assertEqual(response.status_code, 401, response.data)
            self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_profile_does_not_require_stale_tenant_context(self):
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID="999999")
        response = self.api.get("/api/v1/auth/user-info")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["detail"]["memberships"][0]["tenant_id"], self.tenant.pk)
        denied = self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(denied.status_code, 403)

    @override_settings(ERP_OTP_MODE="mock")
    def test_otp_login_ignores_expired_bearer_cookie_and_tenant(self):
        self.api.credentials(HTTP_AUTHORIZATION="Bearer expired.invalid.token", HTTP_X_TENANT_ID="999999",
                             HTTP_ORIGIN="https://myraid-saas-frontend.vercel.app")
        self.api.cookies["access_token"] = "stale-cookie"
        sent = self.api.post("/api/v1/erp/auth/otp/request-otp/", {"phone": self.user.phone}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        verified = self.api.post("/api/v1/erp/auth/otp/verify-otp/", {
            "phone": self.user.phone, "code": sent.data["mock_otp"], "otp_token": sent.data["otp_token"]}, format="json")
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertIn("access", verified.data)

    def test_anonymous_bootstrap_and_permissions_stay_protected(self):
        for route in ("/api/v1/erp/bootstrap/", "/api/v1/auth/user-info"):
            self.assertEqual(self.api.get(route).status_code, 401)
