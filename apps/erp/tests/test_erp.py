import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from django.db.models import Sum
from unittest.mock import patch
from django.contrib.auth.hashers import check_password
from rest_framework.test import APIClient
from apps.core.models import User,Tenant,TenantMembership,Branch,Role,RolePermission,BusinessPermission,UserRole,Company
from apps.erp import models as m, services as svc, workforce
from apps.erp.catalog import FEATURES,PERMISSIONS,price_quote
from apps.erp.money import calculate_line,allocate_paise


class ERPTestCase(TestCase):
    def setUp(self):
        self.tenant=Tenant.objects.create(name="Factory A",slug="factory-a",status="active")
        self.other=Tenant.objects.create(name="Factory B",slug="factory-b",status="active")
        self.branch=Branch.objects.create(tenant=self.tenant,name="Main",code="main")
        self.user=User.objects.create_user(email="owner@example.test",phone="1000000011",password="test-password",first_name="Owner")
        TenantMembership.objects.create(tenant=self.tenant,user=self.user,is_tenant_admin=True)
        self.role=Role.objects.create(tenant=self.tenant,name="Owner",code="owner")
        for group,actions in PERMISSIONS.items():
            for action in actions:
                code=f"{group}.{action}";p,_=BusinessPermission.objects.get_or_create(code=code,defaults={"name":code,"module":group})
                RolePermission.objects.create(role=self.role,permission=p)
        UserRole.objects.create(tenant=self.tenant,user=self.user,role=self.role)
        for feature in FEATURES:
            if feature!="gst_integrations":m.Entitlement.objects.create(tenant=self.tenant,feature=feature,enabled=True,reason="Test fixture")
        self.base={"tenant":self.tenant,"branch":self.branch,"created_by":self.user}
        self.customer=Company.objects.create(tenant=self.tenant,name="Customer A",address="Pune")
        self.supplier=m.Supplier.objects.create(**self.base,name="Supplier A")
        self.item=m.Item.objects.create(**self.base,sku="STEEL",name="Steel",unit="kg",purchase_rate=100)
        self.warehouse=m.Warehouse.objects.create(**self.base,name="Main store",code="MAIN")
        self.api=APIClient();self.api.force_authenticate(self.user)
        self.api.credentials(HTTP_X_TENANT_ID=str(self.tenant.id))

    def post(self,path,data=None,key=None):
        return self.api.post("/api/v1/erp/"+path,data or {},format="json",HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def doc(self,kind="sales_order",qty="3",rate="100",tax="5",status="draft",item=None):
        d=m.Document.objects.create(**self.base,kind=kind,number=svc.number(self.tenant,kind),customer=self.customer,supplier=self.supplier,warehouse=self.warehouse,status=status)
        m.DocumentLine.objects.create(**self.base,document=d,item=item or self.item,description="Steel",**calculate_line({"quantity":qty,"rate":rate,"tax_rate":tax}))
        svc.total_document(d);return d

    def stock(self,qty=10,cost=100):
        return svc.move_stock(tenant=self.tenant,actor=self.user,item=self.item,warehouse=self.warehouse,quantity=qty,unit_cost=cost,kind="opening",reason="Opening stock")

    def test_gst_examples_and_rate_change(self):
        for tax,base,total_tax in [("5","95238.10","4761.90"),("10","90909.09","9090.91")]:
            result=calculate_line({"quantity":1,"rate":100000,"tax_rate":tax})
            self.assertEqual(result["gross"],Decimal("100000.00"));self.assertEqual(result["taxable"],Decimal(base));self.assertEqual(result["tax"],Decimal(total_tax))
            self.assertEqual(result["cgst"]+result["sgst"],result["tax"])

    def test_tax_preview_api_ignores_client_totals(self):
        r=self.post("calculate/",{"lines":[{"quantity":1,"rate":100000,"tax_rate":5,"tax":0,"gross":1}]})
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(Decimal(r.data["gross"]),100000)

    def test_nan_and_negative_quantity_rejected(self):
        for value in ("NaN","Infinity","-2"):
            r=self.post("calculate/",{"lines":[{"quantity":value,"rate":100}]});self.assertEqual(r.status_code,400,r.data)

    def test_create_document_with_server_totals(self):
        r=self.post("documents/",{"kind":"quotation","customer":self.customer.id,"lines":[{"description":"Service","quantity":"1","rate":"100000","tax_rate":"5"}],"gross":"1"})
        self.assertEqual(r.status_code,201,r.data);self.assertEqual(Decimal(r.data["gross"]),100000)

    def test_feature_off_blocks_api_even_with_grant(self):
        m.Entitlement.objects.filter(tenant=self.tenant,feature="inventory").update(enabled=False)
        self.assertEqual(self.api.get("/api/v1/erp/stock/").status_code,403)

    def test_missing_permission_denied(self):
        RolePermission.objects.filter(role=self.role,permission__code="item.view").delete()
        self.assertEqual(self.api.get("/api/v1/erp/items/").status_code,403)

    def test_cross_tenant_item_detail_hidden(self):
        hidden=m.Item.objects.create(tenant=self.other,sku="HIDDEN",name="Hidden")
        self.assertEqual(self.api.get(f"/api/v1/erp/items/{hidden.id}/").status_code,404)

    def test_explicit_wrong_tenant_is_not_silently_replaced(self):
        self.api.credentials(HTTP_X_TENANT_ID=str(self.other.id))
        r=self.api.get("/api/v1/erp/items/");self.assertEqual(r.status_code,403)

    def test_cross_tenant_nested_line_rejected(self):
        item=m.Item.objects.create(tenant=self.other,sku="BAD",name="Other")
        r=self.post("documents/",{"kind":"quotation","customer":self.customer.id,"lines":[{"item":str(item.id),"description":"Bad","quantity":"1","rate":"100"}]})
        self.assertEqual(r.status_code,400,r.data);self.assertFalse(m.Document.objects.exists())

    def test_branch_scope_is_applied_to_actual_rows(self):
        branch2=Branch.objects.create(tenant=self.tenant,name="Other",code="other")
        hidden=m.Item.objects.create(tenant=self.tenant,branch=branch2,sku="OTHER",name="Other branch")
        self.api.credentials(HTTP_X_TENANT_ID=str(self.tenant.id),HTTP_X_BRANCH_ID=str(self.branch.id))
        r=self.api.get("/api/v1/erp/items/");self.assertEqual(r.status_code,200,r.data)
        self.assertNotIn(str(hidden.pk),[x["id"] for x in r.data["results"]])

    def test_stock_wac_and_no_negative_stock(self):
        self.stock();self.stock(10,140)
        out=svc.move_stock(tenant=self.tenant,actor=self.user,item=self.item,warehouse=self.warehouse,quantity=-5,kind="issue",reason="Production")
        self.assertEqual(out.value,Decimal("-600"))
        balance=m.StockBalance.objects.get(item=self.item);self.assertEqual(balance.on_hand,15);self.assertEqual(balance.value,1800)
        r=self.post("stock/movement/",{"kind":"issue","item":str(self.item.id),"warehouse":str(self.warehouse.id),"quantity":"16","reason":"Too much"})
        self.assertEqual(r.status_code,409,r.data);balance.refresh_from_db();self.assertEqual(balance.on_hand,15)

    def test_stock_retry_posts_once(self):
        data={"kind":"inward","item":str(self.item.id),"warehouse":str(self.warehouse.id),"quantity":"5","unit_cost":"100","reason":"Test receipt"}
        first=self.post("stock/movement/",data,"same-action");second=self.post("stock/movement/",data,"same-action")
        self.assertEqual(first.status_code,201,first.data);self.assertEqual(second.status_code,201,second.data)
        self.assertEqual(m.StockMovement.objects.count(),1);self.assertEqual(m.StockBalance.objects.get().on_hand,5)
        altered=self.post("stock/movement/",{**data,"quantity":"6"},"same-action");self.assertEqual(altered.status_code,409)

    def test_transfer_conserves_value(self):
        self.stock();other=m.Warehouse.objects.create(**self.base,name="Other",code="OTHER")
        r=self.post("stock/transfer/",{"item":str(self.item.id),"from_warehouse":str(self.warehouse.id),"to_warehouse":str(other.id),"quantity":"3"})
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(sum(m.StockBalance.objects.values_list("value",flat=True)),1000)
        self.assertEqual(m.ManagementFact.objects.count(),0)

    def test_reservation_blocks_general_issue(self):
        self.stock();order=self.doc(status="confirmed");balance=m.StockBalance.objects.get()
        r=self.post(f"stock/{balance.id}/reserve/",{"version":balance.version,"order":str(order.id),"quantity":"8"});self.assertEqual(r.status_code,200,r.data)
        r=self.post("stock/movement/",{"kind":"issue","item":str(self.item.id),"warehouse":str(self.warehouse.id),"quantity":"3","reason":"Unreserved issue"});self.assertEqual(r.status_code,409,r.data)

    def test_partial_receipt_and_retry(self):
        po=self.doc("purchase_order",qty="100",rate="100",tax="0",status="issued")
        line=po.lines.get()
        receipt=svc.convert_document(po,"goods_receipt",[{"id":str(line.id),"quantity":"40"}],self.user)
        r=self.post(f"documents/{receipt.id}/post/",{"version":receipt.version},"receipt-once")
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(m.StockBalance.objects.get().on_hand,40)
        r2=self.post(f"documents/{receipt.id}/post/",{"version":receipt.version},"receipt-once");self.assertEqual(r2.status_code,200,r2.data)
        self.assertEqual(m.StockBalance.objects.get().on_hand,40)
        po.refresh_from_db();self.assertEqual(po.status,"partially_received")

    def test_receipt_partition_damaged_not_available(self):
        po=self.doc("purchase_order",qty="10",rate="100",tax="0",status="issued")
        receipt=svc.convert_document(po,"goods_receipt",None,self.user);line=receipt.lines.get();line.accepted=7;line.rejected=1;line.damaged=2;line.save()
        r=self.post(f"documents/{receipt.id}/post/",{"version":receipt.version});self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(m.StockBalance.objects.get(bucket="available").on_hand,7);self.assertEqual(m.StockBalance.objects.get(bucket="damaged").on_hand,2)

    def test_quote_requires_acceptance_before_conversion(self):
        quote=self.doc("quotation",status="issued")
        r=self.post(f"documents/{quote.id}/convert/",{"version":quote.version,"target":"sales_order"});self.assertEqual(r.status_code,400,r.data)

    def test_partial_invoice_components_preserved(self):
        order=self.doc(qty="3",rate="33.333333",tax="5",status="confirmed");line=order.lines.get()
        for i in range(3):svc.convert_document(order,"invoice",[{"id":str(line.pk),"quantity":"1"}],self.user)
        invoices=m.Document.objects.filter(kind="invoice")
        self.assertEqual(sum(d.gross for d in invoices),order.gross);self.assertEqual(sum(d.tax for d in invoices),order.tax)
        self.assertEqual(sum(d.taxable for d in invoices),order.taxable)

    def test_direct_and_dispatch_invoice_share_order_limit(self):
        order=self.doc(qty="3",rate="100",tax="0",status="confirmed");line=order.lines.get()
        dispatch=svc.convert_document(order,"dispatch",None,self.user);dispatch.status="posted";dispatch.save()
        svc.convert_document(order,"invoice",None,self.user)
        r=self.post(f"documents/{dispatch.id}/convert/",{"version":dispatch.version,"target":"invoice"});self.assertEqual(r.status_code,409,r.data)

    def test_payment_retry_and_overallocation(self):
        invoice=self.doc("invoice",qty="1",rate="1000",tax="0",status="posted")
        data={"direction":"receipt","customer":self.customer.pk,"amount":"800","allocations":[{"document":str(invoice.pk),"amount":"800"}]}
        r=self.post("payments/",data,"payment-once");self.assertEqual(r.status_code,201,r.data)
        self.post("payments/",data,"payment-once");self.assertEqual(m.Payment.objects.count(),1)
        r=self.post("payments/",data);self.assertEqual(r.status_code,409,r.data);self.assertEqual(m.Payment.objects.count(),1)
        invoice.refresh_from_db();self.assertEqual(invoice.paid,800)

    def test_posted_document_is_immutable(self):
        invoice=self.doc("invoice",status="posted")
        r=self.api.patch(f"/api/v1/erp/documents/{invoice.pk}/",{"title":"Tampered","version":invoice.version},format="json")
        self.assertEqual(r.status_code,409,r.data)

    def test_stale_version_rejected(self):
        r=self.api.patch(f"/api/v1/erp/items/{self.item.pk}/",{"name":"Changed","version":99},format="json")
        self.assertEqual(r.status_code,409,r.data)

    def test_period_lock_blocks_expense(self):
        today=timezone.localdate();m.PeriodLock.objects.create(**self.base,start_date=today,end_date=today,reason="Closed")
        cat=m.ExpenseCategory.objects.create(**self.base,name="Rent")
        exp=m.Expense.objects.create(**self.base,title="Rent",category=cat,amount=100,date=today)
        r=self.post(f"expenses/{exp.id}/post/",{"version":1});self.assertEqual(r.status_code,400,r.data);self.assertFalse(m.ManagementFact.objects.exists())

    def test_recurring_month_end_drafts_once(self):
        cat=m.ExpenseCategory.objects.create(**self.base,name="Rent")
        template=m.RecurringExpense.objects.create(**self.base,name="Rent",category=cat,amount=500,next_due=date(2026,1,31),anchor_day=31)
        svc.generate_recurring(template,self.user,until=date(2026,3,31));svc.generate_recurring(template,self.user,until=date(2026,3,31))
        self.assertEqual(m.Expense.objects.count(),3);self.assertTrue(m.Expense.objects.filter(date=date(2026,2,28)).exists());self.assertFalse(m.ManagementFact.objects.exists())

    def test_payroll_finalization_cost_once(self):
        employee=m.Employee.objects.create(**self.base,name="Worker",code="E1",joining_date=date(2025,1,1),monthly_salary=30000)
        for day in range(1,31):m.Attendance.objects.create(**self.base,employee=employee,date=date(2026,6,day),status="present")
        run=m.PayrollRun.objects.create(**self.base,name="June",month=date(2026,6,1))
        workforce.calculate_payroll(run,self.user);self.assertEqual(run.gross,30000);self.assertFalse(run.results.get().warnings)
        run.status="approved";run.save();version=run.version
        r=self.post(f"payroll/{run.pk}/finalize/",{"version":version},"payroll-once");self.assertEqual(r.status_code,200,r.data)
        self.post(f"payroll/{run.pk}/finalize/",{"version":version},"payroll-once")
        self.assertEqual(m.ManagementFact.objects.filter(source_type="payrollresult").count(),1)
        self.assertTrue(m.Attendance.objects.filter(employee=employee,locked=True).exists())

    def test_payroll_stale_inputs_block_finalization(self):
        e=m.Employee.objects.create(**self.base,name="Worker",code="E1",joining_date=date(2025,1,1),monthly_salary=30000)
        run=m.PayrollRun.objects.create(**self.base,name="June",month=date(2026,6,1));workforce.calculate_payroll(run,self.user,{str(e.id):{"payable_days":30,"reason":"Reviewed"}})
        run.status="approved";run.save();e.monthly_salary=35000;e.version+=1;e.save()
        r=self.post(f"payroll/{run.pk}/finalize/",{"version":run.version});self.assertEqual(r.status_code,409,r.data)

    def test_leave_overlap_and_balance(self):
        e=m.Employee.objects.create(**self.base,name="Worker",code="E1",joining_date=date(2025,1,1))
        lt=m.LeaveType.objects.create(**self.base,name="Annual",annual_allowance=2)
        a=m.LeaveRequest.objects.create(**self.base,employee=e,leave_type=lt,start_date=date(2026,6,1),end_date=date(2026,6,2),reason="Family",days=2)
        workforce.review_leave(a,self.user,"approved")
        b=m.LeaveRequest.objects.create(**self.base,employee=e,leave_type=lt,start_date=date(2026,6,2),end_date=date(2026,6,3),reason="Other",days=2)
        r=self.post(f"leave-requests/{b.pk}/review/",{"version":1,"decision":"approved"});self.assertEqual(r.status_code,409,r.data)

    def test_pricing_cap_and_dependencies(self):
        self.assertEqual(price_quote([])["monthly"],2999)
        self.assertEqual(price_quote([k for k in FEATURES if k!="gst_integrations"])["monthly"],11999)
        r=self.post("entitlements/",{"features":["multi_warehouse"]});self.assertEqual(r.status_code,400,r.data)

    def test_tenant_admin_cannot_grant_paid_entitlement(self):
        r=self.api.patch("/api/v1/erp/entitlements/",{"features":[],"reason":"Test"},format="json",HTTP_IDEMPOTENCY_KEY="one")
        self.assertEqual(r.status_code,403)

    def test_pdf_generation_uses_source_values(self):
        invoice=self.doc("invoice",status="posted")
        r=self.api.get(f"/api/v1/erp/print/documents/{invoice.id}/")
        self.assertEqual(r.status_code,200);self.assertTrue(b"".join(r.streaming_content).startswith(b"%PDF"));self.assertEqual(m.RenderedDocument.objects.count(),1)

    @override_settings(ERP_EMAIL_ENABLED=True,EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_document_email_outbox_and_delivery(self):
        from django.core import mail
        from apps.erp.tasks import process_erp_outbox
        invoice=self.doc("invoice",status="posted")
        with self.captureOnCommitCallbacks(execute=True):
            r=self.post("communications/",{"channel":"email","recipient":"customer@example.test","subject":"Invoice","content":"Attached document","document":str(invoice.pk),"consent_reference":"Customer requested invoice by email"})
        self.assertEqual(r.status_code,201,r.data);self.assertEqual(m.Communication.objects.get().status,"queued")
        process_erp_outbox();communication=m.Communication.objects.get();self.assertEqual(communication.status,"sent");self.assertEqual(len(mail.outbox),1);self.assertEqual(mail.outbox[0].attachments[0][2],"application/pdf")

    def test_import_preview_and_commit(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f=SimpleUploadedFile("customers.csv",b"name,address\nCustomer Two,Pune\n",content_type="text/csv")
        r=self.api.post("/api/v1/erp/imports/",{"resource":"customers","file":f},format="multipart")
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data["status"],"validated")
        commit=self.post(f"imports/{r.data['id']}/commit/",{},"import-once");self.assertEqual(commit.status_code,200,commit.data)
        self.post(f"imports/{r.data['id']}/commit/",{},"import-once");self.assertEqual(Company.objects.filter(name="Customer Two").count(),1)

    def test_dashboard_works_without_fabricated_data(self):
        r=self.api.get("/api/v1/erp/dashboard/");self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data["stats"]["active_jobs"],0)

    def test_bootstrap_includes_erp_catalog(self):
        r=self.api.get("/api/v1/erp/bootstrap/");self.assertEqual(r.status_code,200,r.data);self.assertEqual(len(r.data["catalog"]),24)

    def test_positions_reconcile_and_move_between_bins(self):
        self.stock(); balance=m.StockBalance.objects.get(); position=balance.positions.get()
        target=m.WarehouseBin.objects.create(**self.base,warehouse=self.warehouse,code="A-01")
        r=self.post(f"positions/{position.pk}/relocate/",{"version":position.version,"bin":str(target.pk),"quantity":"4"})
        self.assertEqual(r.status_code,200,r.data);balance.refresh_from_db()
        self.assertEqual(balance.positions.aggregate(v=Sum("quantity"))["v"],balance.on_hand)
        self.assertEqual(balance.positions.get(bin=target).quantity,4)

    def test_reservation_release_restores_availability(self):
        self.stock();order=self.doc(status="confirmed");balance=m.StockBalance.objects.get()
        r=self.post(f"stock/{balance.id}/reserve/",{"version":balance.version,"order":str(order.id),"quantity":"7"});reservation=m.Reservation.objects.get()
        release=self.post(f"reservations/{reservation.pk}/release/",{"version":reservation.version});self.assertEqual(release.status_code,200,release.data)
        balance.refresh_from_db();self.assertEqual(balance.reserved,0);self.assertEqual(m.Reservation.objects.get().status,"released")

    def test_stock_count_posts_delta_and_detects_intervening_movement(self):
        self.stock();count=m.StockCount.objects.create(**self.base,name="Cycle count",warehouse=self.warehouse)
        from apps.erp.inventory import open_count,post_count
        open_count(count);line=count.lines.get();line.counted=8;line.save();post_count(count,self.user)
        self.assertEqual(m.StockBalance.objects.get().on_hand,8)
        count2=m.StockCount.objects.create(**self.base,name="Stale count",warehouse=self.warehouse);open_count(count2)
        svc.move_stock(tenant=self.tenant,actor=self.user,item=self.item,warehouse=self.warehouse,quantity=1,unit_cost=100,kind="inward",reason="After count")
        stale=count2.lines.get();stale.counted=8;stale.save()
        with self.assertRaises(svc.Conflict):post_count(count2,self.user)

    def test_customer_profile_round_trip(self):
        r=self.post("customers/",{"name":"Profile customer","address":"Pune","contact_name":"Aarav","email":"aarav@example.test","credit_limit":"50000"})
        self.assertEqual(r.status_code,201,r.data);self.assertEqual(r.data["contact_name"],"Aarav");self.assertEqual(r.data["credit_limit"],"50000.00")


class OnboardingTestCase(TestCase):
    def test_signup_creates_isolated_owner_and_selected_trial_modules(self):
        api=APIClient()
        r=api.post("/api/v1/erp/onboard/",{"company_name":"New Industrial Co","name":"New Owner","phone":"8880000000","features":["purchase","inventory","hrms","payroll"]},format="json")
        self.assertEqual(r.status_code,201,r.data)
        tenant=Tenant.objects.get(name="New Industrial Co");owner=User.objects.get(phone_e164="+918880000000")
        self.assertTrue(TenantMembership.objects.filter(tenant=tenant,user=owner,is_tenant_admin=True).exists())
        self.assertTrue(UserRole.objects.filter(tenant=tenant,user=owner,role__code="tenant-admin").exists())
        self.assertEqual(set(m.Entitlement.objects.filter(tenant=tenant,enabled=True).values_list("feature",flat=True)),{"purchase","inventory","hrms","payroll"})

    def test_signup_rejects_dependency_violation(self):
        api=APIClient();r=api.post("/api/v1/erp/onboard/",{"company_name":"Bad","name":"Owner","phone":"7770000000","features":["payroll"]},format="json")
        self.assertEqual(r.status_code,400,r.data);self.assertFalse(Tenant.objects.filter(name="Bad").exists())


class PhoneOTPTestCase(TestCase):
    def setUp(self):
        self.tenant=Tenant.objects.create(name="OTP Factory",slug="otp-factory",status="active")
        self.branch1=Branch.objects.create(tenant=self.tenant,name="Branch One",code="one")
        self.branch2=Branch.objects.create(tenant=self.tenant,name="Branch Two",code="two")
        self.user=User.objects.create_user(email="internal@phone.myraid.invalid",phone="9876543210",password=None,first_name="Multi Role")
        TenantMembership.objects.create(tenant=self.tenant,user=self.user,default_branch=self.branch1)

    def test_otp_request_requires_phone_body(self):
        api=APIClient()
        request=api.post("/api/v1/erp/auth/otp/request-otp/",{},format="json")
        self.assertEqual(request.status_code,400,request.data)
        self.assertIn("phone",request.data["error"])
        self.assertFalse(m.LoginOTP.objects.exists())

    def test_otp_verify_requires_phone_and_code_body(self):
        api=APIClient()
        missing_all=api.post("/api/v1/erp/auth/otp/verify-otp/",{},format="json")
        self.assertEqual(missing_all.status_code,400,missing_all.data)
        self.assertIn("phone",missing_all.data["error"])
        self.assertIn("code",missing_all.data["error"])
        missing_code=api.post("/api/v1/erp/auth/otp/verify-otp/",{"phone":"+919876543210"},format="json")
        self.assertEqual(missing_code.status_code,400,missing_code.data)
        self.assertIn("code",missing_code.data["error"])

    def test_otp_schema_documents_request_bodies(self):
        api=APIClient()
        schema=api.get("/api/schema/").data
        request_body=schema["paths"]["/api/v1/erp/auth/otp/request-otp/"]["post"]["requestBody"]
        verify_body=schema["paths"]["/api/v1/erp/auth/otp/verify-otp/"]["post"]["requestBody"]
        self.assertIn("application/json",request_body["content"])
        self.assertIn("application/json",verify_body["content"])
        self.assertEqual(schema["components"]["schemas"]["OTPRequest"]["required"],["phone"])
        self.assertEqual(set(schema["components"]["schemas"]["OTPVerify"]["required"]),{"phone","code"})

    @override_settings(ERP_SMS_ENABLED=True,DEBUG=True,MSG91_AUTH_KEY="")
    @patch("apps.erp.otp.secrets.randbelow",return_value=654321)
    def test_phone_otp_is_hashed_single_use_and_sets_session(self,_random):
        api=APIClient();request=api.post("/api/v1/erp/auth/otp/request-otp/",{"phone":"98765 43210"},format="json")
        self.assertEqual(request.status_code,200,request.data);otp=m.LoginOTP.objects.get();self.assertNotIn("654321",otp.code_hash);self.assertTrue(check_password("654321",otp.code_hash))
        bad=api.post("/api/v1/erp/auth/otp/verify-otp/",{"phone":"+919876543210","code":"000000"},format="json");self.assertEqual(bad.status_code,400)
        verified=api.post("/api/v1/erp/auth/otp/verify-otp/",{"phone":"+919876543210","code":"654321"},format="json");self.assertEqual(verified.status_code,200,verified.data);self.assertIn("access_token",verified.cookies)
        reused=api.post("/api/v1/erp/auth/otp/verify-otp/",{"phone":"+919876543210","code":"654321"},format="json");self.assertEqual(reused.status_code,400)

    def test_same_phone_user_has_different_role_in_different_companies(self):
        for code,module in [("workspace.view","workspace"),("item.view","item"),("payment.record","payment")]:
            BusinessPermission.objects.create(code=code,name=code,module=module)
        other=Tenant.objects.create(name="Second Company",slug="second-company",status="active")
        other_branch=Branch.objects.create(tenant=other,name="Only Branch",code="main")
        TenantMembership.objects.create(tenant=other,user=self.user,default_branch=other_branch)
        manager=Role.objects.create(tenant=self.tenant,name="Manager",code="manager");cashier=Role.objects.create(tenant=other,name="Cashier",code="cashier")
        RolePermission.objects.create(role=manager,permission=BusinessPermission.objects.get(code="workspace.view"));RolePermission.objects.create(role=manager,permission=BusinessPermission.objects.get(code="item.view"))
        RolePermission.objects.create(role=cashier,permission=BusinessPermission.objects.get(code="workspace.view"));RolePermission.objects.create(role=cashier,permission=BusinessPermission.objects.get(code="payment.record"))
        UserRole.objects.create(tenant=self.tenant,user=self.user,role=manager);UserRole.objects.create(tenant=other,user=self.user,role=cashier)
        api=APIClient();api.force_authenticate(self.user)
        one=api.get("/api/v1/erp/bootstrap/",HTTP_X_TENANT_ID=str(self.tenant.id),HTTP_X_BRANCH_ID=str(self.branch1.id));self.assertEqual(one.status_code,200,one.data);self.assertIn("item.view",one.data["permissions"]);self.assertNotIn("payment.record",one.data["permissions"])
        two=api.get("/api/v1/erp/bootstrap/",HTTP_X_TENANT_ID=str(other.id),HTTP_X_BRANCH_ID=str(other_branch.id));self.assertEqual(two.status_code,200,two.data);self.assertIn("payment.record",two.data["permissions"]);self.assertNotIn("item.view",two.data["permissions"])

    def test_authenticated_phone_identity_can_add_and_switch_company(self):
        api=APIClient();api.force_authenticate(self.user)
        created=api.post("/api/v1/erp/companies/",{"company_name":"Third Company","features":[]},format="json",HTTP_X_TENANT_ID=str(self.tenant.id))
        self.assertEqual(created.status_code,201,created.data)
        third=Tenant.objects.get(pk=created.data["tenant_id"])
        self.assertEqual(TenantMembership.objects.filter(user=self.user,is_active=True).count(),2)
        self.assertEqual(UserRole.objects.filter(tenant=third,user=self.user,is_active=True).count(),1)
        switched=api.get("/api/v1/erp/bootstrap/",HTTP_X_TENANT_ID=str(third.id))
        self.assertEqual(switched.status_code,200,switched.data)
        self.assertEqual(switched.data["tenant"]["name"],"Third Company")
        self.assertEqual(switched.data["user"]["phone"],"+919876543210")

    @override_settings(DEBUG=False,ERP_SMS_ENABLED=True,MSG91_AUTH_KEY="auth",MSG91_OTP_FLOW_ID="flow",MSG91_SENDER_ID="MYRAID",MSG91_OTP_VARIABLE="OTP",MSG91_FLOW_URL="https://api.msg91.com/api/v5/flow/")
    @patch("apps.erp.otp.urllib.request.urlopen")
    def test_msg91_flow_receives_server_generated_otp(self,urlopen):
        import json
        from apps.erp.otp import dispatch_sms
        response=urlopen.return_value.__enter__.return_value;response.status=200;response.read.return_value=b'{"type":"success"}'
        dispatch_sms("+919876543210","654321",uuid.uuid4())
        request=urlopen.call_args.args[0];payload=json.loads(request.data)
        self.assertEqual(payload["flow_id"],"flow");self.assertEqual(payload["recipients"][0]["mobiles"],"919876543210");self.assertEqual(payload["recipients"][0]["OTP"],"654321");self.assertEqual(request.headers["Authkey"],"auth")
