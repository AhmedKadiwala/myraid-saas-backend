"""
Comprehensive verification of Manufacturing (Multi-level BOM, Work Orders, Scheduling, Costing, QC),
Cloudflare R2 Ephemeral Document Retention & Regeneration, and Sales Rep & Subscription Tiers.
"""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import Tenant, TenantMembership, User, SubscriptionPlan, Role, BusinessPermission, RolePermission, UserRole
from apps.reference_erp.models import ERPRecord, GeneratedDocument
from apps.reference_erp.engine import bom_explode, apply
from apps.core.r2_storage import store_generated_document, get_or_regenerate_document, cleanup_expired_r2_documents

BASE = "/api/v1/workspace-ui/"


class ManufacturingAndCustomRequirementsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@mfg-test.example",
            phone="9876500001",
            first_name="Owner",
            last_name="Test",
            department="admin",
            platform_admin=True
        )
        self.sales_rep = User.objects.create_user(
            email="salesrep@mfg-test.example",
            phone="9876500002",
            first_name="Rep",
            last_name="One",
            department="sales",
            platform_sales_rep=True
        )
        self.plan = SubscriptionPlan.objects.create(
            name="Advanced ERP",
            code="advanced",
            price_monthly=Decimal("300.00"),
            user_limit=50
        )
        from apps.erp.onboarding import create_company_workspace
        from apps.erp.catalog import FEATURES
        self.tenant, _, _ = create_company_workspace(
            self.owner,
            "Apex Precision Engineering",
            [k for k in FEATURES if k != "gst_integrations"]
        )
        self.tenant.sales_rep = self.sales_rep
        self.tenant.plan = self.plan
        self.tenant.status = "active"
        self.tenant.save(update_fields=["sales_rep", "plan", "status"])
        self.member = TenantMembership.objects.get(tenant=self.tenant, user=self.owner)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.api.credentials(
            HTTP_AUTHORIZATION="Bearer " + str(RefreshToken.for_user(self.owner).access_token),
            HTTP_X_TENANT_ID=str(self.tenant.pk)
        )

    def command(self, name, fields=None, code="", kind="form"):
        return self.api.post(
            BASE + "commands/",
            {"name": name, "input": fields or {}, "id": code, "kind": kind, "request_id": str(uuid4())},
            format="json"
        )

    # ---------------------------------------------------------
    # 1. MANUFACTURING: MULTI-LEVEL BOM & COSTING
    # ---------------------------------------------------------
    def test_multi_level_bom_and_cost_rollup(self):
        # 1. Create Raw Materials
        r1 = self.command("item", {"sku": "RAW-STEEL-01", "name": "Steel Sheet 2mm", "unit": "Kg", "cost": 80.0, "rate": 0.0, "stock": 500})
        self.assertEqual(r1.status_code, 200, r1.data)
        r2 = self.command("item", {"sku": "RAW-FASTENER-01", "name": "M8 Bolts", "unit": "Nos", "cost": 5.0, "rate": 0.0, "stock": 1000})
        self.assertEqual(r2.status_code, 200, r2.data)

        # 2. Create Sub-Assembly Item & its BOM
        sub_item = self.command("item", {"sku": "SA-FRAME-01", "name": "Welded Frame Base", "unit": "Nos", "cost": 0.0, "rate": 500.0, "stock": 0})
        self.assertEqual(sub_item.status_code, 200, sub_item.data)

        # BOM for Sub-Assembly: 4 Kg steel + 30 mins welding (₹300/hr)
        sub_bom = self.command("bom", {
            "item": "SA-FRAME-01",
            "name": "BOM for Welded Frame Base",
            "qty": 1,
            "components": [{"item": "RAW-STEEL-01", "qty": 4.0, "scrap_percent": 5.0, "unit_cost": 80.0}],
            "operations": [{"stage": "Welding", "workstation": "Weld Booth 1", "hours": 0.5, "hourly_rate": 300.0}]
        })
        self.assertEqual(sub_bom.status_code, 200, sub_bom.data)
        # Material: 4 * 80 * 1.05 = 336.00. Operation: 0.5 * 300 = 150.00. Total = 486.00
        sub_record = ERPRecord.objects.get(tenant=self.tenant, collection="boms", payload__item="SA-FRAME-01").payload
        self.assertAlmostEqual(sub_record["total_cost"], 486.00, places=2)

        # 3. Create Finished Good & Multi-Level Parent BOM
        fg_item = self.command("item", {"sku": "FG-MACHINE-01", "name": "Industrial Sorter Unit", "unit": "Nos", "cost": 0.0, "rate": 2500.0, "stock": 0})
        self.assertEqual(fg_item.status_code, 200, fg_item.data)

        fg_bom = self.command("bom", {
            "item": "FG-MACHINE-01",
            "name": "BOM for Industrial Sorter Unit",
            "qty": 1,
            "components": [
                {"item": "SA-FRAME-01", "qty": 1.0, "scrap_percent": 0.0, "unit_cost": 486.0},
                {"item": "RAW-FASTENER-01", "qty": 12.0, "scrap_percent": 0.0, "unit_cost": 5.0}
            ],
            "operations": [
                {"stage": "Final Assembly", "workstation": "Assembly Line A", "hours": 1.5, "hourly_rate": 250.0},
                {"stage": "Quality Inspection", "workstation": "QC Station", "hours": 0.5, "hourly_rate": 200.0}
            ]
        })
        self.assertEqual(fg_bom.status_code, 200, fg_bom.data)

        # 4. Test Recursive BOM Explosion
        raw_state = self.api.get(BASE + "records/boms/").data
        explosion = self.command("bom-explode", {"item": "FG-MACHINE-01", "qty": 2})
        self.assertEqual(explosion.status_code, 200, explosion.data)
        exp_data = explosion.data["result"]
        self.assertEqual(exp_data["item"], "FG-MACHINE-01")
        self.assertEqual(exp_data["qty"], 2)
        # Verify sub-assembly is exploded
        self.assertEqual(len(exp_data["components"]), 2)
        sub_in_exp = next(c for c in exp_data["components"] if c["item"] == "SA-FRAME-01")
        self.assertFalse(sub_in_exp["is_raw_material"])
        self.assertEqual(len(sub_in_exp["components"]), 1)
        self.assertEqual(sub_in_exp["components"][0]["item"], "RAW-STEEL-01")

    # ---------------------------------------------------------
    # 2. QUALITY CONTROL & INSPECTION
    # ---------------------------------------------------------
    def test_qc_inspection_workflow(self):
        item = self.command("item", {"sku": "QC-TEST-SKU", "name": "Precision Shaft", "cost": 150.0, "stock": 100})
        self.assertEqual(item.status_code, 200)

        # Perform QC Inspection on receipt with 2 scrap rejects
        qc = self.command("qc-inspection", {
            "target_type": "purchase",
            "target_id": "PO-TEST-01",
            "item": "QC-TEST-SKU",
            "inspected_qty": 20,
            "accepted_qty": 18,
            "inspector": "Chief Inspector Sharma",
            "remarks": "2 units exceeded radial runout tolerance"
        })
        self.assertEqual(qc.status_code, 200, qc.data)
        qc_res = ERPRecord.objects.get(tenant=self.tenant, collection="qcInspections").payload
        self.assertEqual(qc_res["status"], "Conditionally Passed")
        self.assertEqual(qc_res["accepted_qty"], 18)
        self.assertEqual(qc_res["rejected_qty"], 2)

        # Verify scrap stock deduction recorded in stock moves
        move = ERPRecord.objects.filter(tenant=self.tenant, collection="stockMoves", payload__item="QC-TEST-SKU").first()
        self.assertIsNotNone(move)
        self.assertEqual(move.payload["delta"], -2)

    # ---------------------------------------------------------
    # 3. PRODUCTION WORK ORDER & INVENTORY CONSUMPTION
    # ---------------------------------------------------------
    def test_work_order_completion_consumes_materials_and_stocks_finished_goods(self):
        # Create raw material and finished product
        self.command("item", {"sku": "RM-ALUM-01", "name": "Aluminium Ingot", "cost": 100.0, "stock": 50})
        self.command("item", {"sku": "FG-PUMP-01", "name": "Water Pump", "cost": 300.0, "stock": 0})
        self.command("customer", {"name": "Industrial Pumps Ltd", "phone": "9876512345"})
        cust = ERPRecord.objects.get(tenant=self.tenant, collection="customers", payload__name="Industrial Pumps Ltd").payload["id"]

        # Create BOM
        self.command("bom", {
            "item": "FG-PUMP-01", "name": "Pump BOM", "qty": 1,
            "components": [{"item": "RM-ALUM-01", "qty": 2.0, "unit_cost": 100.0}],
            "operations": [{"stage": "Machining", "workstation": "Lathe 1", "hours": 1.0, "hourly_rate": 100.0}]
        })

        # Create and confirm order
        order = self.command("order", {"customer": cust, "item": "FG-PUMP-01", "qty": 5, "amount": 10000, "gst": 18, "status": "Confirmed", "due": "2026-12-31"})
        self.assertEqual(order.status_code, 200)

        job = ERPRecord.objects.get(tenant=self.tenant, collection="jobs", payload__item="FG-PUMP-01")
        # Complete work order
        update = self.command("job-update", {"stage": "Machining", "status": "Completed", "progress": 100}, code=job.code)
        self.assertEqual(update.status_code, 200)

        # Check inventory: Raw materials consumed (5 * 2 = 10 units consumed, 50 - 10 = 40 remaining)
        rm_item = next(r.payload for r in ERPRecord.objects.filter(tenant=self.tenant, collection="items") if r.payload.get("sku") == "RM-ALUM-01" or r.payload.get("id") == "RM-ALUM-01")
        self.assertEqual(rm_item["stock"], 40)
        # Finished goods added to stock (+5 units)
        fg_item = next(r.payload for r in ERPRecord.objects.filter(tenant=self.tenant, collection="items") if r.payload.get("sku") == "FG-PUMP-01" or r.payload.get("id") == "FG-PUMP-01")
        self.assertEqual(fg_item["stock"], 5)

    # ---------------------------------------------------------
    # 4. CLOUDFLARE R2 EPHEMERAL DOCUMENT RETENTION & REGENERATION
    # ---------------------------------------------------------
    def test_r2_ephemeral_document_generation_expiry_and_regeneration(self):
        # Create an invoice record
        self.command("customer", {"name": "R2 Client", "phone": "9876599999"})
        cust = ERPRecord.objects.get(tenant=self.tenant, collection="customers", payload__name="R2 Client").payload["id"]
        self.command("item", {"sku": "R2-ITEM", "name": "Valve", "cost": 50.0, "rate": 100.0, "stock": 10})
        order = self.command("order", {"customer": cust, "item": "R2-ITEM", "qty": 2, "amount": 200, "status": "Confirmed", "due": "2026-12-31"})
        self.assertEqual(order.status_code, 200, order.data)
        inv_cmd = self.command("invoice", {"order": order.data["result"]["id"], "due": "2026-12-31"})
        self.assertEqual(inv_cmd.status_code, 200)
        inv_code = inv_cmd.data["result"]["id"]

        # 1. Request PDF via API
        pdf_res = self.api.get(f"{BASE}documents/invoice/{inv_code}/pdf/")
        self.assertEqual(pdf_res.status_code, 200, pdf_res.data)
        self.assertIn("url", pdf_res.data)
        self.assertEqual(pdf_res.data["regenerated"], True)

        # Verify GeneratedDocument in database with 30-day retention
        gen_doc = GeneratedDocument.objects.get(tenant=self.tenant, document_code=inv_code)
        self.assertFalse(gen_doc.is_deleted)
        self.assertGreater(gen_doc.expires_at, timezone.now() + timedelta(days=29))

        # 2. Simulate 31 days passing: File expires
        gen_doc.expires_at = timezone.now() - timedelta(days=1)
        gen_doc.save(update_fields=["expires_at"])

        # Automated Celery cleanup runs
        cleaned = cleanup_expired_r2_documents()
        self.assertGreaterEqual(cleaned, 1)
        gen_doc.refresh_from_db()
        self.assertTrue(gen_doc.is_deleted)

        # 3. Permanent record remains in PostgreSQL! Requesting PDF again regenerates it on-demand
        regen_res = self.api.get(f"{BASE}documents/invoice/{inv_code}/pdf/")
        self.assertEqual(regen_res.status_code, 200)
        self.assertEqual(regen_res.data["regenerated"], True)
        fresh_doc = GeneratedDocument.objects.get(tenant=self.tenant, document_code=inv_code)
        self.assertFalse(fresh_doc.is_deleted)
        self.assertGreater(fresh_doc.expires_at, timezone.now() + timedelta(days=29))

    # ---------------------------------------------------------
    # 5. SALES REP & PER-USER TIERED SUBSCRIPTION MANAGEMENT
    # ---------------------------------------------------------
    def test_sales_rep_dashboard_and_tiered_user_pricing(self):
        self.api.force_authenticate(self.sales_rep)

        # 1. Invite a company via Sales Rep endpoint with 14-day default trial and Advanced plan (₹300/u/m)
        invite_res = self.api.post(
            f"{BASE}platform/sales-rep/invite-company/",
            {
                "company_name": "Zenith Motors",
                "admin_name": "Rohan Verma",
                "phone": "9876543111",
                "plan": "advanced",
                "trial_days": 14
            },
            format="json"
        )
        self.assertEqual(invite_res.status_code, 201, invite_res.data)
        self.assertEqual(invite_res.data["trial_days"], 14)
        self.assertEqual(invite_res.data["monthly_price_per_user"], 300)

        zenith = Tenant.objects.get(name="Zenith Motors")
        self.assertEqual(zenith.sales_rep, self.sales_rep)
        self.assertEqual(zenith.status, "trial")

        # 2. Add users to Zenith: 2 active users, 1 pending invitation
        user2 = User.objects.create_user(email="staff1@zenith.test", phone="9876543112", username="staff1")
        TenantMembership.objects.create(tenant=zenith, user=user2, is_active=True)
        # Pending invitation should NOT be counted in billable users
        from apps.reference_erp.models import CompanyInvitation
        CompanyInvitation.objects.create(
            tenant=zenith,
            phone="+919876543113",
            username="pending_user",
            role=Role.objects.get_or_create(tenant=zenith, code="staff", defaults={"name": "Staff"})[0],
            invited_by=self.owner,
            token_hash="fakehash123",
            expires_at=timezone.now() + timedelta(days=7),
            status="pending"
        )

        # Active billable users = 2 (owner + staff1)
        self.assertEqual(zenith.billable_users_count, 2)
        # Advanced rate = ₹300 * 2 = ₹600/month
        self.assertEqual(zenith.calculate_monthly_price(), Decimal("600.00"))

        # 3. Check Sales Rep Dashboard metrics
        dash = self.api.get(f"{BASE}platform/sales-rep/metrics/")
        self.assertEqual(dash.status_code, 200, dash.data)
        metrics = dash.data["metrics"]
        self.assertGreaterEqual(metrics["assigned_companies"], 1)
        self.assertGreaterEqual(metrics["free_trials"], 1)
        self.assertGreaterEqual(metrics["billable_users"], 2)

        # 4. Verify company data is preserved even after trial expiry
        zenith.trial_ends_at = timezone.now() - timedelta(days=1)
        zenith.save(update_fields=["trial_ends_at"])
        # All memberships and data remain intact
        self.assertEqual(zenith.memberships.count(), 2)
        self.assertTrue(Tenant.objects.filter(pk=zenith.pk).exists())
