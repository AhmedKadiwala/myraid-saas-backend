import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny,IsAuthenticated
from rest_framework.throttling import AnonRateThrottle
from rest_framework import serializers
from rest_framework.response import Response
from apps.core.models import User,Tenant,TenantMembership,TenantSettings,SubscriptionPlan,Branch,Role,RolePermission,UserRole,BusinessPermission
from apps.core.management.commands.bootstrap_saas import PERMISSIONS as SALES_PERMISSIONS
from .catalog import PERMISSIONS,price_quote
from .models import ErpSettings,Entitlement
from .otp import normalize_phone


def install_permission_catalog():
    result=[]
    for group,actions in PERMISSIONS.items():
        for action in actions:
            code=f"{group}.{action}";permission,_=BusinessPermission.objects.get_or_create(code=code,defaults={"name":code.replace("."," ").title(),"module":group});result.append(permission)
    for group,codes in SALES_PERMISSIONS.items():
        for code in codes:
            permission,_=BusinessPermission.objects.get_or_create(code=code,defaults={"name":code.replace("."," ").title(),"module":group});result.append(permission)
    return result


def owner_role(tenant,user):
    role,_=Role.objects.get_or_create(tenant=tenant,code="tenant-admin",defaults={"name":"ERP Owner","is_system":True})
    for permission in install_permission_catalog():RolePermission.objects.get_or_create(role=role,permission=permission)
    UserRole.objects.filter(tenant=tenant,user=user,is_active=True).exclude(role=role).update(is_active=False)
    assignment,_=UserRole.objects.get_or_create(tenant=tenant,user=user,role=role,branch=None,defaults={"assigned_by":user})
    if not assignment.is_active:assignment.is_active=True;assignment.save(update_fields=["is_active","updated_at"])


def create_company_workspace(user,company_name,selected):
    quote=price_quote(selected)
    plan,_=SubscriptionPlan.objects.get_or_create(code="erp-basic",defaults={"name":"ERP Basic","price_monthly":2999,"user_limit":5,"branch_limit":1,"storage_limit_mb":1024})
    slug=f"{slugify(company_name)[:45] or 'company'}-{uuid.uuid4().hex[:8]}";expiry=timezone.now()+timedelta(days=getattr(settings,"ERP_TRIAL_DAYS",14))
    tenant=Tenant.objects.create(name=company_name,slug=slug,status="trial",plan=plan,trial_ends_at=expiry)
    branch=Branch.objects.create(tenant=tenant,name="Main branch",code="main")
    TenantMembership.objects.create(tenant=tenant,user=user,is_tenant_admin=True,default_branch=branch)
    TenantSettings.objects.create(tenant=tenant);ErpSettings.objects.create(tenant=tenant,legal_name=company_name);owner_role(tenant,user)
    for feature in set(selected):Entitlement.objects.create(tenant=tenant,feature=feature,enabled=True,expires_at=expiry,reason="User-selected trial capability",changed_by=user)
    return tenant,expiry,quote


class OnboardInput(serializers.Serializer):
    company_name=serializers.CharField(max_length=200);name=serializers.CharField(max_length=150);phone=serializers.CharField(max_length=16);features=serializers.ListField(child=serializers.CharField(),default=list)
    def validate(self,data):
        phone=normalize_phone(data["phone"]);digits=phone[-10:]
        if User.objects.filter(phone_e164=phone).exists() or User.objects.filter(phone=digits).exists():raise serializers.ValidationError("This phone number already has a Myraid identity. Sign in with OTP and choose Add company.")
        data["phone"]=phone;price_quote(data["features"]);return data


class SignupThrottle(AnonRateThrottle):rate="5/hour"
class OnboardView(APIView):
    authentication_classes=[];permission_classes=[AllowAny];throttle_classes=[SignupThrottle]
    @transaction.atomic
    def post(self,request):
        serializer=OnboardInput(data=request.data);serializer.is_valid(raise_exception=True);data=serializer.validated_data
        first,_,last=data["name"].partition(" ");digits="".join(ch for ch in data["phone"] if ch.isdigit())
        user=User.objects.create_user(email=f"{digits}.{uuid.uuid4().hex[:8]}@phone.myraid.invalid",phone=data["phone"],password=None,first_name=first,last_name=last,department="admin")
        user.set_unusable_password();user.save(update_fields=["password"])
        _,expiry,_=create_company_workspace(user,data["company_name"],data["features"])
        return Response({"message":"Workspace created. Verify the phone number with OTP to sign in.","phone":user.phone_e164,"trial_ends_at":expiry},status=201)


class AddCompanyView(APIView):
    permission_classes=[IsAuthenticated]
    @transaction.atomic
    def post(self,request):
        name=str(request.data.get("company_name","")).strip();selected=request.data.get("features",[])
        if not name:raise serializers.ValidationError({"company_name":"Company name is required."})
        tenant,expiry,quote=create_company_workspace(request.user,name,selected)
        return Response({"message":"Company workspace created.","tenant_id":tenant.pk,"tenant_name":tenant.name,"trial_ends_at":expiry,"subscription_quote":quote},status=201)
