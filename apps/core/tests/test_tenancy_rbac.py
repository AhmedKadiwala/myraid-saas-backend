from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BusinessPermission,
    Client,
    Company,
    Lead,
    Product,
    Role,
    RolePermission,
    Source,
    Tenant,
    TenantMembership,
    User,
    UserRole,
)
from apps.core.services import effective_permissions


class TenantRBACTestCase(TestCase):
    def setUp(self):
        self.a = Tenant.objects.create(name="Alpha", slug="alpha", status="active")
        self.b = Tenant.objects.create(name="Beta", slug="beta", status="active")
        self.user = User.objects.create_user(
            email="staff@example.com", phone="1000000001", password="secret12"
        )
        self.other = User.objects.create_user(
            email="other@example.com", phone="1000000002", password="secret12"
        )
        TenantMembership.objects.create(tenant=self.a, user=self.user)
        TenantMembership.objects.create(tenant=self.b, user=self.other)
        self.branch_a = Branch.objects.create(
            tenant=self.a, name="A1", code="a1"
        )
        self.branch_a2 = Branch.objects.create(
            tenant=self.a, name="A2", code="a2"
        )
        self.permission_view = BusinessPermission.objects.create(
            code="lead.view", name="View lead", module="leads"
        )
        self.permission_add = BusinessPermission.objects.create(
            code="lead.add", name="Add lead", module="leads"
        )

    def role(self, code, permission, tenant=None, approved=True):
        role = Role.objects.create(
            tenant=tenant, name=code, code=code,
            approved_for_tenant_assignment=approved,
        )
        RolePermission.objects.create(role=role, permission=permission)
        return role

    def test_one_active_role_per_company_uses_current_assignment(self):
        viewer = self.role("viewer", self.permission_view, self.a)
        creator = self.role("creator", self.permission_add, self.a)
        UserRole.objects.create(tenant=self.a, user=self.user, role=viewer, is_active=False)
        UserRole.objects.create(tenant=self.a, user=self.user, role=creator)
        self.assertEqual(
            effective_permissions(self.user, self.a),
            {"lead.add"},
        )

    def test_temporary_roles_respect_validity_window(self):
        role = self.role("temp", self.permission_add, self.a)
        now = timezone.now()
        UserRole.objects.create(
            tenant=self.a, user=self.user, role=role,
            valid_from=now - timedelta(hours=2),
            valid_to=now - timedelta(hours=1),
        )
        self.assertNotIn("lead.add", effective_permissions(self.user, self.a))

    def test_role_assignment_is_company_scoped_not_branch_scoped(self):
        role = self.role("branch-viewer", self.permission_view, self.a)
        assignment=UserRole(
            tenant=self.a, user=self.user, role=role, branch=self.branch_a
        )
        with self.assertRaises(ValidationError):assignment.full_clean()

    def test_privilege_escalation_to_unapproved_platform_role_is_rejected(self):
        role = self.role(
            "platform-admin", self.permission_add, tenant=None, approved=False
        )
        assignment = UserRole(tenant=self.a, user=self.user, role=role)
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_cross_tenant_api_object_access_returns_404(self):
        viewer = self.role("alpha-viewer", self.permission_view, self.a)
        UserRole.objects.create(tenant=self.a, user=self.user, role=viewer)
        company = Company.objects.create(
            tenant=self.b, name="Hidden", address="Secret"
        )
        client = Client.objects.create(
            tenant=self.b, company=company, first_name="Hidden"
        )
        source = Source.objects.create(tenant=self.b, name="Web")
        product = Product.objects.create(tenant=self.b, name="Rack")
        lead = Lead.objects.create(
            tenant=self.b, company=company, client_detail=client,
            source=source, product=product,
        )
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.get(
            f"/api/v1/leads/get/{lead.pk}",
            HTTP_X_TENANT_ID=str(self.a.pk),
        )
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(response.data["lead"])
