"""Owner-only tenant provisioning and manual subscription receivables. UPI integration is planned."""
import calendar
import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from apps.core.api import APIView
from apps.core.authentication import CookieOrHeaderJWTAuthentication
from apps.core.admin_access import is_platform_owner
from apps.core.models import Tenant, TenantMembership, User
from apps.core.services import audit
from apps.erp.catalog import FEATURES
from apps.erp.models import Entitlement, ErpSettings
from apps.erp.onboarding import create_company_workspace, owner_role
from apps.erp.otp import normalize_phone
from .models import TenantCharge, TenantPayment
from .reporting import period_range

ZERO = Decimal("0.00")


class PlatformOwnerPermission(BasePermission):
    def has_permission(self, request, view):
        if not is_platform_owner(request.user): return False
        if not settings.DEBUG and (settings.ERP_OTP_MODE != "sms" or not settings.ERP_SMS_ENABLED):
            raise PermissionDenied("Platform-owner production access awaits MSG91 activation. The workflow is ready for the configured owner account.")
        return True


class PlatformAPI(APIView):
    authentication_classes = [CookieOrHeaderJWTAuthentication]
    permission_classes = [PlatformOwnerPermission]


def ensure_charges(tenant):
    if tenant.status == "trial" and tenant.trial_ends_at and timezone.now() < tenant.trial_ends_at:
        return
    if tenant.complimentary_access or not tenant.billing_start_date or not tenant.agreed_monthly_price:
        return
    if timezone.localdate() < tenant.billing_start_date: return
    current = timezone.localdate().replace(day=1)
    period = tenant.billing_start_date.replace(day=1)
    existing=set(tenant.platform_charges.values_list("period",flat=True))
    # Never infer historical obligations before an explicitly entered billing start.
    while period <= current:
        due = period.replace(day=min(tenant.payment_due_day, calendar.monthrange(period.year, period.month)[1]))
        if period == tenant.billing_start_date.replace(day=1): due = max(due, tenant.billing_start_date)
        if period not in existing:
            TenantCharge.objects.get_or_create(tenant=tenant, period=period, defaults={"amount":tenant.agreed_monthly_price,"due_date":due})
        period = date(period.year+1,1,1) if period.month==12 else date(period.year,period.month+1,1)


def billing_data(tenant):
    ensure_charges(tenant)
    charges=[]
    today=timezone.localdate()
    for charge in tenant.platform_charges.order_by("-period").prefetch_related("payments"):
        paid=sum((p.amount for p in charge.payments.all()),ZERO)
        outstanding=ZERO if charge.voided else max(ZERO,charge.amount-paid)
        status="Voided" if charge.voided else "Paid" if outstanding==0 else "Overdue" if charge.due_date<today else "Partial" if paid else "Pending"
        charges.append({"id":charge.pk,"period":charge.period.strftime("%Y-%m"),"amount":float(charge.amount),
                        "due_date":charge.due_date.isoformat(),"paid":float(paid),"outstanding":float(outstanding),"status":status})
    payments=[{"id":p.pk,"amount":float(p.amount),"date":p.date.isoformat(),"mode":p.mode,"reference":p.reference,
               "note":p.note,"charge_id":p.charge_id} for p in tenant.platform_payments.order_by("-date","-pk")]
    billed=sum((Decimal(str(c["amount"])) for c in charges if c["status"]!="Voided"),ZERO)
    paid=sum((Decimal(str(p["amount"])) for p in payments),ZERO)
    return {"tenant_id":tenant.pk,"company":tenant.name,"agreed_monthly_price":float(tenant.agreed_monthly_price) if tenant.agreed_monthly_price is not None else None,
            "status":tenant.status,"complimentary_access":tenant.complimentary_access,
            "trial_ends_at":tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
            "billing_start_date":tenant.billing_start_date.isoformat() if tenant.billing_start_date else None,
            "payment_due_day":tenant.payment_due_day,"charges":charges,"payments":payments,
            "total_billed":float(billed),"total_paid":float(paid),"balance":float(billed-paid),
            "overdue":sum(c["outstanding"] for c in charges if c["status"]=="Overdue"),
            "integration":{"tracking":"Manual records","upi":"Planned","automatic_payments":"Planned"}}


class TenantInput(serializers.Serializer):
    company_name=serializers.CharField(max_length=160)
    admin_name=serializers.CharField(max_length=150)
    phone=serializers.CharField(max_length=16)
    access_mode=serializers.ChoiceField(choices=["trial","complimentary","paid","suspended"],default="trial")
    free_days=serializers.IntegerField(min_value=1,max_value=3650,default=7)
    monthly_price=serializers.DecimalField(max_digits=12,decimal_places=2,min_value=ZERO,allow_null=True,default=None)
    billing_start=serializers.DateField(allow_null=True,default=None)
    due_day=serializers.IntegerField(min_value=1,max_value=31,default=7)

    def validate(self,data):
        data["phone"]=normalize_phone(data["phone"])
        if data["access_mode"]=="paid" and (not data["monthly_price"] or not data["billing_start"]):
            raise serializers.ValidationError("Paid access needs an agreed price and billing start date.")
        if data["billing_start"] and data["billing_start"] < timezone.localdate()-timedelta(days=3660):
            raise serializers.ValidationError("Billing starts must be within the last ten years.")
        return data


def configure_tenant(tenant, data, actor, new=False):
    mode=data["access_mode"]
    tenant.name=data["company_name"]
    tenant.agreed_monthly_price=data["monthly_price"]
    tenant.payment_due_day=data["due_day"]
    tenant.complimentary_access=mode=="complimentary"
    tenant.billing_start_date=data["billing_start"] if mode=="paid" else None
    tenant.status="trial" if mode=="trial" else "suspended" if mode=="suspended" else "active"
    tenant.trial_ends_at=timezone.now()+timedelta(days=data["free_days"]) if mode=="trial" else None
    if new and mode in ("trial", "paid"):
        tenant.status="trial"
        tenant.trial_ends_at=timezone.now()+timedelta(days=max(7,data["free_days"]))
        if tenant.billing_start_date:
            tenant.billing_start_date=max(tenant.billing_start_date,timezone.localtime(tenant.trial_ends_at).date())
    tenant.save()
    ErpSettings.objects.update_or_create(tenant=tenant, defaults={"legal_name":tenant.name})
    for feature in FEATURES:
        if feature!="gst_integrations":
            Entitlement.objects.update_or_create(tenant=tenant,feature=feature,defaults={"enabled":True,
                "effective_at":timezone.now(),"expires_at":tenant.trial_ends_at,"reason":"Platform admin access agreement","changed_by":actor})
    audit(actor=actor,tenant=tenant,action="platform.tenant.configured",resource=tenant,
          after={"name":tenant.name,"access_mode":mode,"monthly_price":str(tenant.agreed_monthly_price),"trial_ends_at":str(tenant.trial_ends_at)})


class PlatformDashboardView(PlatformAPI):
    def get(self,request):
        start,end,kind=period_range(request.query_params)
        rows=[]
        for tenant in Tenant.objects.select_related("plan").order_by("name"):
            data=billing_data(tenant)
            data["admins"]=list(TenantMembership.objects.filter(tenant=tenant,is_tenant_admin=True,is_active=True)
                                .values("user__id","user__phone","user__first_name","user__last_name"))
            rows.append(data)
        collected=TenantPayment.objects.filter(date__gte=start,date__lte=end).aggregate(total=Sum("amount"))["total"] or ZERO
        return Response({"tenants":rows,"from":start.isoformat(),"to":end.isoformat(),"period":kind,
                         "collected":float(collected),"tenant_count":len(rows),"overdue":sum(r["overdue"] for r in rows),
                         "integration":{"upi":"Planned","automatic_payments":"Planned"}})


class PlatformTenantsView(PlatformAPI):
    @transaction.atomic
    def post(self,request):
        serializer=TenantInput(data=request.data);serializer.is_valid(raise_exception=True);data=serializer.validated_data
        user=User.objects.filter(Q(phone_e164=data["phone"])|Q(phone=data["phone"][-10:])).first()
        if user and not user.is_active:raise ValidationError("The administrator's identity is inactive.")
        if not user:
            first,_,last=data["admin_name"].partition(" ")
            user=User.objects.create_user(email=f"{data['phone'][1:]}.{uuid.uuid4().hex[:8]}@phone.workspace.invalid",
                                          phone=data["phone"],first_name=first,last_name=last,password=None,department="admin")
        tenant,_,_=create_company_workspace(user,data["company_name"],[k for k in FEATURES if k!="gst_integrations"])
        configure_tenant(tenant,data,request.user,new=True)
        return Response(billing_data(tenant),status=201)


class PlatformTenantDetailView(PlatformAPI):
    @transaction.atomic
    def patch(self,request,tenant_id):
        tenant=get_object_or_404(Tenant.objects.select_for_update(),pk=tenant_id)
        admin=TenantMembership.objects.filter(tenant=tenant,is_tenant_admin=True,is_active=True).select_related("user").first()
        defaults={"company_name":tenant.name,"admin_name":admin.user.get_full_name() if admin else "Admin",
            "phone":admin.user.phone if admin else "","access_mode":"complimentary" if tenant.complimentary_access else "trial" if tenant.status=="trial" else "suspended" if tenant.status=="suspended" else "paid",
            "monthly_price":tenant.agreed_monthly_price,"billing_start":tenant.billing_start_date,"due_day":tenant.payment_due_day,"free_days":7}
        serializer=TenantInput(data={**defaults,**request.data});serializer.is_valid(raise_exception=True)
        data=serializer.validated_data
        if admin and admin.user.phone_e164 == data["phone"] and "admin_name" in request.data:
            first,_,last=data["admin_name"].partition(" ")
            admin.user.first_name=first;admin.user.last_name=last
            admin.user.save(update_fields=["first_name","last_name"])
        if not admin or admin.user.phone_e164 != data["phone"]:
            user=User.objects.filter(Q(phone_e164=data["phone"])|Q(phone=data["phone"][-10:])).first()
            if user and not user.is_active:raise ValidationError("The administrator's identity is inactive.")
            if not user:
                first,_,last=data["admin_name"].partition(" ")
                user=User.objects.create_user(email=f"{data['phone'][1:]}.{uuid.uuid4().hex[:8]}@phone.workspace.invalid",phone=data["phone"],first_name=first,last_name=last,password=None,department="admin")
            membership,_=TenantMembership.objects.get_or_create(tenant=tenant,user=user)
            membership.is_tenant_admin=True;membership.is_active=True
            membership.save(update_fields=["is_tenant_admin","is_active"])
            owner_role(tenant,user)
            if admin:
                admin.is_active=False;admin.is_tenant_admin=False
                admin.save(update_fields=["is_active","is_tenant_admin"])
        configure_tenant(tenant,data,request.user)
        return Response(billing_data(tenant))


class PlatformPaymentView(PlatformAPI):
    @transaction.atomic
    def post(self,request,tenant_id):
        tenant=get_object_or_404(Tenant.objects.select_for_update(),pk=tenant_id)
        try:
            request_id=uuid.UUID(str(request.data["request_id"]))
            amount=Decimal(str(request.data["amount"])).quantize(Decimal("0.01"))
            day=date.fromisoformat(request.data["date"])
            if not amount.is_finite() or amount<=0 or amount>Decimal("9999999999.99") or day>timezone.localdate():raise ValueError()
        except (KeyError,ValueError,TypeError,ArithmeticError):raise ValidationError("Enter a positive payment, date and unique request ID.")
        mode=str(request.data.get("mode","UPI"));reference=str(request.data.get("reference","")).strip()
        if mode not in ("UPI","Cash","Bank transfer","Cheque"):raise ValidationError("Choose a valid payment method.")
        if mode!="Cash" and not reference:raise ValidationError("Record the UTR or payment reference.")
        if len(reference)>100 or len(str(request.data.get("note","")))>300:raise ValidationError("Reference or note is too long.")
        existing=TenantPayment.objects.filter(request_id=request_id).first()
        try:charge_id=int(request.data["charge_id"]) if request.data.get("charge_id") else None
        except (TypeError,ValueError):raise ValidationError("Select a valid charge.")
        if existing:
            if existing.tenant_id!=tenant.pk or existing.amount!=amount or existing.date!=day or existing.reference!=reference or existing.mode!=mode or existing.charge_id!=charge_id:
                raise ValidationError("This request ID belongs to a different payment.")
            return Response(billing_data(tenant))
        if reference and TenantPayment.objects.filter(reference=reference).exists():raise ValidationError("This payment reference was already recorded.")
        charge=None
        if request.data.get("charge_id"):
            charge=get_object_or_404(TenantCharge.objects.select_for_update(),tenant=tenant,pk=charge_id,voided=False)
            paid=charge.payments.aggregate(total=Sum("amount"))["total"] or ZERO
            if amount>charge.amount-paid:raise ValidationError("Payment exceeds the selected charge's outstanding balance. Record excess as an unallocated credit.")
        receipt=TenantPayment.objects.create(tenant=tenant,charge=charge,amount=amount,date=day,mode=mode,reference=reference,
            note=str(request.data.get("note","")),request_id=request_id,recorded_by=request.user)
        audit(actor=request.user,tenant=tenant,action="platform.payment.recorded",resource=receipt,after={"amount":str(amount),"reference":reference,"date":str(day)})
        return Response(billing_data(tenant),status=201)


class CompanyBillingView(APIView):
    authentication_classes=[CookieOrHeaderJWTAuthentication]
    permission_classes=[IsAuthenticated]

    def get(self,request):
        # Billing is visible to the company's admin even when operational access expired.
        tenant_id=request.headers.get("X-Tenant-ID")
        try:tenant_id=int(tenant_id)
        except (ValueError,TypeError):raise PermissionDenied("Select a company to view its billing.")
        member=TenantMembership.objects.select_related("tenant").filter(user=request.user,tenant_id=tenant_id,is_active=True,is_tenant_admin=True).first()
        if not member:raise PermissionDenied("Company-admin billing access is required.")
        return Response(billing_data(member.tenant))
