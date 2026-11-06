from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import (
    BusinessPermission,
    OrganizationInvitation,
    Role,
    RolePermission,
    Tenant,
    TenantMembership,
    User,
    UserRole,
)

TEST_CLIENT_ID = "test-google-client-id.apps.googleusercontent.com"


@override_settings(
    GOOGLE_CLIENT_ID=TEST_CLIENT_ID,
    ERP_PUBLIC_APP_URL="http://localhost:5173",
    INVITATION_EXPIRY_DAYS=7,
)
class GoogleAuthTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.client_id = TEST_CLIENT_ID
        mail.outbox = []

    def _mock_claims(
        self,
        sub="google-sub-12345",
        email="testuser@example.com",
        email_verified=True,
        iss="https://accounts.google.com",
        aud=None,
        exp=None,
        given_name="Test",
        family_name="User",
    ):
        if aud is None:
            aud = self.client_id
        if exp is None:
            exp = timezone.now().timestamp() + 3600
        return {
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "iss": iss,
            "aud": aud,
            "exp": exp,
            "given_name": given_name,
            "family_name": family_name,
        }

    # --------------------------------------------------------------------------
    # 1. Valid Google ID token
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_valid_google_id_token(self, mock_verify):
        mock_verify.return_value = self._mock_claims()

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "valid.google.jwt"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertIn("userData", response.data)
        self.assertEqual(response.data["userData"]["email"], "testuser@example.com")
        self.assertIn("access_token", response.cookies)
        self.assertIn("refresh_token", response.cookies)

    # --------------------------------------------------------------------------
    # 2. Invalid Google ID token
    # --------------------------------------------------------------------------
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_invalid_google_id_token(self, mock_verify):
        mock_verify.side_effect = ValueError("Invalid signature")

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "corrupt.invalid.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("message", response.data)

    # --------------------------------------------------------------------------
    # 3. Expired ID token
    # --------------------------------------------------------------------------
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_expired_id_token(self, mock_verify):
        # Expiry timestamp in the past
        past_exp = timezone.now().timestamp() - 600
        mock_verify.return_value = self._mock_claims(exp=past_exp)

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "expired.google.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("expired", response.data["message"].lower())

    # --------------------------------------------------------------------------
    # 4. Incorrect audience
    # --------------------------------------------------------------------------
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_incorrect_audience(self, mock_verify):
        mock_verify.return_value = self._mock_claims(aud="different-app.apps.googleusercontent.com")

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "wrong.audience.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("audience", response.data["message"].lower())

    # --------------------------------------------------------------------------
    # 5. Invalid issuer
    # --------------------------------------------------------------------------
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_invalid_issuer(self, mock_verify):
        mock_verify.return_value = self._mock_claims(iss="https://malicious-idp.example.com")

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "wrong.issuer.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("issuer", response.data["message"].lower())

    # --------------------------------------------------------------------------
    # 6. Unverified email
    # --------------------------------------------------------------------------
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_unverified_email(self, mock_verify):
        mock_verify.return_value = self._mock_claims(email_verified=False)

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "unverified.email.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("verified", response.data["message"].lower())

    # --------------------------------------------------------------------------
    # 7. Existing user login
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_existing_user_login(self, mock_verify):
        existing = User.objects.create_user(
            email="existing@example.com",
            first_name="Alice",
            last_name="Smith",
        )
        existing.google_sub = "alice-google-sub"
        existing.save()

        mock_verify.return_value = self._mock_claims(
            sub="alice-google-sub",
            email="existing@example.com",
            given_name="Alice",
            family_name="Smith",
        )

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "alice.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["userData"]["id"], existing.pk)
        self.assertEqual(User.objects.filter(email="existing@example.com").count(), 1)

    # --------------------------------------------------------------------------
    # 8. New user registration
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_new_user_registration(self, mock_verify):
        mock_verify.return_value = self._mock_claims(
            sub="brand-new-sub",
            email="newuser@example.com",
            given_name="New",
            family_name="User",
        )

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "new.token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email="newuser@example.com")
        self.assertEqual(user.google_sub, "brand-new-sub")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(response.data["userData"]["is_new_user"])

    # --------------------------------------------------------------------------
    # 9. Duplicate email prevention
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_duplicate_email_prevention(self, mock_verify):
        claims = self._mock_claims(sub="same-sub", email="repeat@example.com")
        mock_verify.return_value = claims

        # Sign in once
        res1 = self.api.post("/api/v1/auth/google/signin", {"credential": "token"}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Sign in again with same email
        res2 = self.api.post("/api/v1/auth/google/signin", {"credential": "token"}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        self.assertEqual(User.objects.filter(email="repeat@example.com").count(), 1)

    # --------------------------------------------------------------------------
    # 10. Google identity linking
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_google_identity_linking(self, mock_verify):
        # User created prior to Google migration
        legacy = User.objects.create_user(
            email="legacy@example.com",
            first_name="Legacy",
            last_name="Worker",
        )
        self.assertIsNone(legacy.google_sub)

        mock_verify.return_value = self._mock_claims(
            sub="newly-linked-google-sub",
            email="legacy@example.com",
        )

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        legacy.refresh_from_db()
        self.assertEqual(legacy.google_sub, "newly-linked-google-sub")

        # Attempt to link same email with a DIFFERENT sub causes safe conflict rejection
        mock_verify.return_value = self._mock_claims(
            sub="conflicting-sub-99999",
            email="legacy@example.com",
        )
        conflict_res = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "token2"},
            format="json",
        )
        self.assertEqual(conflict_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflict", conflict_res.data["message"].lower())

    # --------------------------------------------------------------------------
    # 11. Organization owner onboarding
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_organization_owner_onboarding(self, mock_verify):
        mock_verify.return_value = self._mock_claims(
            sub="founder-sub-1",
            email="founder@example.com",
            given_name="Frank",
            family_name="Founder",
        )

        response = self.api.post(
            "/api/v1/auth/google/onboard",
            {
                "credential": "founder.token",
                "company_name": "Acme Industrial Tools",
                "features": ["purchase", "inventory"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("access", response.data)
        user = User.objects.get(email="founder@example.com")
        tenant = Tenant.objects.get(name="Acme Industrial Tools")
        self.assertEqual(tenant.owner, user)

        # TenantMembership created
        membership = TenantMembership.objects.get(tenant=tenant, user=user)
        self.assertTrue(membership.is_tenant_admin)

        # Owner role assigned
        user_role = UserRole.objects.get(tenant=tenant, user=user, is_active=True)
        self.assertEqual(user_role.role.code, "tenant-admin")

        # Duplicate onboarding prevention for same user
        dup_response = self.api.post(
            "/api/v1/auth/google/onboard",
            {
                "credential": "founder.token",
                "company_name": "Second Company Attempt",
            },
            format="json",
        )
        self.assertEqual(dup_response.status_code, status.HTTP_409_CONFLICT)

    # --------------------------------------------------------------------------
    # 12. Employee invitation creation & email delivery
    # --------------------------------------------------------------------------
    def test_employee_invitation_creation_and_email(self):
        owner = User.objects.create_user(email="owner@acme.com", first_name="Owner")
        tenant = Tenant.objects.create(name="Acme Tools", slug="acme-tools", owner=owner)
        TenantMembership.objects.create(tenant=tenant, user=owner, is_tenant_admin=True)

        role = Role.objects.create(tenant=tenant, name="Operator", code="operator")

        self.api.force_authenticate(user=owner)
        self.api.credentials(HTTP_X_TENANT_ID=str(tenant.pk))

        response = self.api.post(
            "/api/v1/auth/invitations/",
            {"email": "employee@acme.com", "role_id": role.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("invitation_link", response.data)
        self.assertTrue(response.data["email_sent"])

        inv = OrganizationInvitation.objects.get(tenant=tenant, invited_email="employee@acme.com")
        self.assertEqual(inv.role, role)
        self.assertEqual(inv.invited_by, owner)

        # Email was sent via Django mail backend
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("employee@acme.com", mail.outbox[0].to)
        self.assertIn(inv.token, mail.outbox[0].body)

    # --------------------------------------------------------------------------
    # 13. Employee invitation acceptance
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_employee_invitation_acceptance(self, mock_verify):
        owner = User.objects.create_user(email="boss@acme.com")
        tenant = Tenant.objects.create(name="Acme Tools", slug="acme-t2", owner=owner)
        role = Role.objects.create(tenant=tenant, name="Technician", code="tech")

        inv = OrganizationInvitation.objects.create(
            tenant=tenant,
            invited_email="worker@acme.com",
            role=role,
            invited_by=owner,
            token="secure-invite-token-123",
            expires_at=timezone.now() + timedelta(days=7),
        )

        mock_verify.return_value = self._mock_claims(
            sub="worker-google-sub",
            email="worker@acme.com",
            given_name="John",
            family_name="Doe",
        )

        response = self.api.post(
            f"/api/v1/auth/invitations/{inv.token}/accept/",
            {"credential": "worker.jwt"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email="worker@acme.com")
        self.assertTrue(TenantMembership.objects.filter(tenant=tenant, user=user, is_active=True).exists())
        self.assertTrue(UserRole.objects.filter(tenant=tenant, user=user, role=role, is_active=True).exists())

        inv.refresh_from_db()
        self.assertIsNotNone(inv.accepted_at)
        self.assertEqual(inv.accepted_by, user)

    # --------------------------------------------------------------------------
    # 14. Invitation email mismatch
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_invitation_email_mismatch(self, mock_verify):
        tenant = Tenant.objects.create(name="Beta Corp", slug="beta-corp")
        inv = OrganizationInvitation.objects.create(
            tenant=tenant,
            invited_email="target@example.com",
            token="invite-token-diff-email",
            expires_at=timezone.now() + timedelta(days=7),
        )

        # Authenticate with a different Google account
        mock_verify.return_value = self._mock_claims(
            sub="wrong-person-sub",
            email="intruder@example.com",
        )

        response = self.api.post(
            f"/api/v1/auth/invitations/{inv.token}/accept/",
            {"credential": "intruder.jwt"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target@example.com", response.data["message"])
        inv.refresh_from_db()
        self.assertIsNone(inv.accepted_at)

    # --------------------------------------------------------------------------
    # 15. Invitation expiration
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_invitation_expiration(self, mock_verify):
        tenant = Tenant.objects.create(name="Gamma", slug="gamma")
        inv = OrganizationInvitation.objects.create(
            tenant=tenant,
            invited_email="late@example.com",
            token="expired-token-xyz",
            expires_at=timezone.now() - timedelta(days=1),  # Expired yesterday
        )

        mock_verify.return_value = self._mock_claims(email="late@example.com")

        response = self.api.post(
            f"/api/v1/auth/invitations/{inv.token}/accept/",
            {"credential": "token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_410_GONE)

    # --------------------------------------------------------------------------
    # 16. Invitation reuse prevention (single-use)
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_invitation_reuse_prevention(self, mock_verify):
        tenant = Tenant.objects.create(name="Delta", slug="delta")
        inv = OrganizationInvitation.objects.create(
            tenant=tenant,
            invited_email="once@example.com",
            token="single-use-token",
            expires_at=timezone.now() + timedelta(days=7),
            accepted_at=timezone.now() - timedelta(hours=1),  # Already accepted
        )

        mock_verify.return_value = self._mock_claims(email="once@example.com")

        response = self.api.post(
            f"/api/v1/auth/invitations/{inv.token}/accept/",
            {"credential": "token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --------------------------------------------------------------------------
    # 17. Multiple organization memberships
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_multiple_organization_memberships(self, mock_verify):
        user = User.objects.create_user(email="consultant@example.com")
        t1 = Tenant.objects.create(name="Tenant One", slug="t-one")
        t2 = Tenant.objects.create(name="Tenant Two", slug="t-two")

        TenantMembership.objects.create(tenant=t1, user=user, is_tenant_admin=True)
        TenantMembership.objects.create(tenant=t2, user=user, is_tenant_admin=False)

        mock_verify.return_value = self._mock_claims(
            sub="consultant-sub",
            email="consultant@example.com",
        )

        response = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "jwt"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        memberships = response.data["userData"]["memberships"]
        self.assertEqual(len(memberships), 2)
        tenant_ids = {m["tenant_id"] for m in memberships}
        self.assertEqual(tenant_ids, {t1.pk, t2.pk})

    # --------------------------------------------------------------------------
    # 18. Tenant isolation
    # --------------------------------------------------------------------------
    def test_tenant_isolation(self):
        user = User.objects.create_user(email="member@org-a.com")
        tenant_a = Tenant.objects.create(name="Org A", slug="org-a", status="active")
        tenant_b = Tenant.objects.create(name="Org B", slug="org-b", status="active")

        TenantMembership.objects.create(tenant=tenant_a, user=user)

        self.api.force_authenticate(user=user)

        # Accessing Tenant A is authorized
        self.api.credentials(HTTP_X_TENANT_ID=str(tenant_a.pk))
        res_a = self.api.get("/api/v1/tenants/me")
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)

        # Accessing Tenant B is rejected (tenant isolation)
        self.api.credentials(HTTP_X_TENANT_ID=str(tenant_b.pk))
        res_b = self.api.get("/api/v1/tenants/me")
        self.assertEqual(res_b.status_code, status.HTTP_403_FORBIDDEN)

    # --------------------------------------------------------------------------
    # 19. RBAC enforcement
    # --------------------------------------------------------------------------
    def test_rbac_enforcement(self):
        user = User.objects.create_user(email="staff@org.com")
        tenant = Tenant.objects.create(name="RBAC Org", slug="rbac-org", status="active")
        TenantMembership.objects.create(tenant=tenant, user=user, is_tenant_admin=False)

        self.api.force_authenticate(user=user)
        self.api.credentials(HTTP_X_TENANT_ID=str(tenant.pk))

        # Endpoint requiring admin privileges (e.g. creating invitations)
        res = self.api.post(
            "/api/v1/auth/invitations/",
            {"email": "someone@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # --------------------------------------------------------------------------
    # 20. Session creation and logout
    # --------------------------------------------------------------------------
    @patch("apps.core.google_auth._verify_google_id_token")
    def test_session_creation_and_logout(self, mock_verify):
        mock_verify.return_value = self._mock_claims(
            sub="session-test-sub",
            email="session@example.com",
        )

        login_res = self.api.post(
            "/api/v1/auth/google/signin",
            {"credential": "jwt"},
            format="json",
        )
        self.assertEqual(login_res.status_code, status.HTTP_200_OK)
        refresh_token = login_res.data["refresh"]

        # Logout
        logout_res = self.api.post(
            "/api/v1/auth/logout",
            {"refresh": refresh_token},
            format="json",
        )
        self.assertEqual(logout_res.status_code, status.HTTP_200_OK)

        # Refresh token is now blacklisted
        refresh_attempt = self.api.post(
            "/api/v1/auth/refresh",
            {"refresh": refresh_token},
            format="json",
        )
        self.assertEqual(refresh_attempt.status_code, status.HTTP_401_UNAUTHORIZED)

    # --------------------------------------------------------------------------
    # 21. Administrative superuser access
    # --------------------------------------------------------------------------
    def test_administrative_superuser_access(self):
        # Platform superuser can authenticate with password
        superuser = User.objects.create_superuser(
            email="root@myraid.com",
            password="SuperSecureAdminPassword123!",
        )

        res_super = self.api.post(
            "/api/v1/auth/login",
            {"email": "root@myraid.com", "password": "SuperSecureAdminPassword123!"},
            format="json",
        )
        self.assertEqual(res_super.status_code, status.HTTP_200_OK)
        self.assertIn("access", res_super.data)

        # Ordinary user cannot log in via password endpoint
        regular = User.objects.create_user(
            email="regular@myraid.com",
            password="RegularPassword123!",
            is_staff=False,
            is_superuser=False,
        )
        res_regular = self.api.post(
            "/api/v1/auth/login",
            {"email": "regular@myraid.com", "password": "RegularPassword123!"},
            format="json",
        )
        self.assertEqual(res_regular.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("google", res_regular.data["message"].lower())
