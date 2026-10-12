import json
from pathlib import Path
from uuid import uuid4
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from apps.core.models import Tenant, User, TenantMembership, SubscriptionPlan, Company, Role, UserRole, BusinessPermission, RolePermission
from apps.erp.models import Item, StockBalance, Warehouse, ErpSettings, Entitlement
from apps.erp.catalog import FEATURES
from .test_engine import FIXTURE
from ..models import ERPRecord, WorkspaceSettings, AuditEvent
from ..engine import COLLECTIONS


class WorkspaceAPITests(TestCase):
    def setUp(self):
        self.plan = SubscriptionPlan.objects.create(name="Growth", code="recommended", user_limit=30, price_monthly=9999)
        self.tenant = Tenant.objects.create(name="Tenant Factory A", slug="tenant-a", status="active", plan=self.plan)
        self.other = Tenant.objects.create(name="Tenant Factory B", slug="tenant-b", status="active", plan=self.plan)
        self.user = User.objects.create_user(email="owner@a.test", phone="9876500001", first_name="Actual Owner")
        self.member = TenantMembership.objects.create(tenant=self.tenant, user=self.user, is_tenant_admin=True)
        TenantMembership.objects.create(tenant=self.other, user=self.user, is_tenant_admin=True)
        for tenant in (self.tenant, self.other):
            for feature in FEATURES:
                if feature != "gst_integrations":
                    Entitlement.objects.create(tenant=tenant, feature=feature, enabled=True, reason="Test fixture")
        sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
        WorkspaceSettings.objects.create(tenant=self.tenant, company_meta={**sample["company"], "name": "Ignored template company"}, initialized=True)
        for collection in COLLECTIONS:
            if collection != "users":
                for record in sample[collection]:
                    ERPRecord.objects.create(tenant=self.tenant, collection=collection, code=record["id"], payload=record)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID=str(self.tenant.pk))

    def command(self, name, fields=None, code="", kind="form", request_id=None):
        return self.api.post("/api/v1/workspace-ui/commands/", {"name": name, "input": fields or {}, "id": code,
                            "kind": kind, "request_id": request_id or str(uuid4())}, format="json")

    def test_real_jwt_company_brand_and_switch(self):
        first = self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["user"]["company"], self.tenant.name)
        self.assertEqual(first.data["data"]["company"]["name"], self.tenant.name)
        self.assertEqual(first.data["user"]["name"], "Actual Owner")
        self.assertEqual(len(first.data["user"]["memberships"]), 2)
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID=str(self.other.pk))
        second = self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(second.data["data"]["company"]["name"], self.other.name)
        self.assertEqual(second.data["data"]["customers"], [])

    def test_rename_saved_to_existing_tenant_and_no_cross_tenant_change(self):
        result = self.command("company-save", {"name": "Customer's Company", "location": "Pune"}, kind="action")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["user"]["company"], "Customer's Company")
        self.tenant.refresh_from_db(); self.other.refresh_from_db()
        self.assertEqual(self.tenant.name, "Customer's Company")
        self.assertEqual(self.other.name, "Tenant Factory B")
        self.assertEqual(ErpSettings.objects.get(tenant=self.tenant).legal_name, "Customer's Company")

    def test_receipt_idempotence_and_conflicting_reuse(self):
        identifier = str(uuid4())
        first = self.command("receive", {"qty": 10}, "PO-001", request_id=identifier)
        repeat = self.command("receive", {"qty": 10}, "PO-001", request_id=identifier)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(repeat.data["data"]["items"], first.data["data"]["items"])
        self.assertEqual(AuditEvent.objects.filter(tenant=self.tenant).count(), 1)
        conflict = self.command("receive", {"qty": 20}, "PO-001", request_id=identifier)
        self.assertEqual(conflict.status_code, 409)

    def test_quote_to_invoice_payment_round_trip(self):
        quote = self.command("quote", {"customer": "C-001", "item": "SKU-001", "qty": 2, "amount": "100000", "gst": 5})
        self.assertEqual(quote.status_code, 200, quote.data)
        order = self.command("quote-convert", code=quote.data["result"]["id"], kind="action")
        invoice = self.command("invoice-from-order", code=order.data["result"]["id"], kind="action")
        self.assertEqual(invoice.status_code, 200, invoice.data)
        payment = self.command("payment", {"invoice": invoice.data["result"]["id"], "amount": 2500, "date": "2026-10-12", "mode": "UPI"})
        self.assertEqual(payment.status_code, 200, payment.data)
        saved = ERPRecord.objects.get(tenant=self.tenant, collection="invoices", code=invoice.data["result"]["id"]).payload
        self.assertEqual(saved["amount"], 100000)
        self.assertEqual(saved["taxable"], 95238.10)

    def test_invite_actual_account_role_and_sales_lookup_permissions(self):
        invited = self.command("user", {"name": "Sales Person", "phone": "9876543222", "role": "Sales"})
        self.assertEqual(invited.status_code, 200, invited.data)
        sales = User.objects.get(phone_e164="+919876543222")
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(sales).access_token), HTTP_X_TENANT_ID=str(self.tenant.pk))
        workspace = self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(workspace.data["user"]["role"], "Sales")
        self.assertTrue(workspace.data["data"]["items"])
        self.assertNotIn("cost", workspace.data["data"]["items"][0])
        self.assertNotIn("stock", workspace.data["data"]["items"][0])
        self.assertEqual(self.command("receive", {"qty": 1}, "PO-001").status_code, 403)
        self.assertEqual(self.api.get("/api/v1/workspace-ui/records/items/").status_code, 403)
        self.assertEqual(self.command("quote", {"customer": "C-001", "item": "SKU-001", "qty": 1, "amount": 100, "gst": 5}).status_code, 200)

    def test_unknown_tenant_anonymous_and_subscription_restrictions(self):
        stranger = Tenant.objects.create(name="Private company", slug="private")
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID=str(stranger.pk))
        self.assertEqual(self.api.get("/api/v1/workspace-ui/workspace/").status_code, 403)
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID=str(self.tenant.pk))
        Entitlement.objects.filter(tenant=self.tenant, feature="purchase").update(enabled=False)
        self.assertEqual(self.command("receive", {"qty": 1}, "PO-001").status_code, 403)
        self.assertEqual(self.command("reset", kind="action").status_code, 403)
        self.assertEqual(self.command("set-plan", {"plan": "full"}, kind="action").status_code, 403)
        self.api.credentials()
        self.assertEqual(self.api.get("/api/v1/workspace-ui/workspace/").status_code, 401)

    def test_new_tenant_copies_current_records_once_without_sample_data(self):
        customer = Company.objects.create(tenant=self.other, name="Existing Customer", address="Pune")
        item = Item.objects.create(tenant=self.other, sku="EXISTING", name="Existing Stock", sale_rate=100)
        warehouse = Warehouse.objects.create(tenant=self.other, code="MAIN", name="Main")
        StockBalance.objects.create(tenant=self.other, item=item, warehouse=warehouse, on_hand=17)
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.user).access_token), HTTP_X_TENANT_ID=str(self.other.pk))
        first = self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["data"]["customers"][0]["name"], customer.name)
        self.assertEqual(first.data["data"]["items"][0]["stock"], 17)
        self.assertEqual(first.data["data"]["invoices"], [])
        self.api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(ERPRecord.objects.filter(tenant=self.other, collection="items").count(), 1)
        self.assertTrue(Company.objects.filter(pk=customer.pk).exists())

    def test_read_only_multiline_and_user_cap(self):
        record = ERPRecord.objects.get(tenant=self.tenant, collection="orders", code="SO-001")
        record.payload = {**record.payload, "read_only": True}; record.save()
        self.assertEqual(self.command("invoice-from-order", code="SO-001", kind="action").status_code, 400)
        self.plan.user_limit = 1; self.plan.save()
        self.assertEqual(self.command("user", {"name": "Another", "phone": "9876543233", "role": "Sales"}).status_code, 400)

    def test_staff_manager_cannot_grant_permissions_they_do_not_hold(self):
        self.member.is_tenant_admin = False; self.member.save()
        role = Role.objects.create(tenant=self.tenant, code="admin-assistant", name="Admin assistant")
        permission = BusinessPermission.objects.create(code="staff.manage", name="Manage staff", module="staff")
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(tenant=self.tenant, user=self.user, role=role)
        response = self.command("user", {"name": "New Administrator", "phone": "9876543234", "role": "Admin"})
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(User.objects.filter(phone_e164="+919876543234").exists())
