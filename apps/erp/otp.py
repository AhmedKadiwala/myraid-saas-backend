import hashlib,json,urllib.request
import secrets
from datetime import timedelta
from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.hashers import make_password,check_password
from django.db import transaction, models
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from apps.core.models import User,TenantMembership
from apps.core.auth_views import set_auth_cookies
from django.middleware.csrf import get_token
from .models import LoginOTP


class RequestThrottle(AnonRateThrottle):rate="6/hour"
class VerifyThrottle(AnonRateThrottle):rate="20/hour"


def user_payload(user):
    return {"department":user.department,"name":user.get_full_name(),"email":user.email,"phone":user.phone_e164 or user.phone,"code":user.quotation_code,"id":user.id,
            "memberships":list(TenantMembership.objects.filter(user=user,is_active=True).values("tenant_id","tenant__name","is_tenant_admin"))}

def normalize_phone(value):
    raw=str(value or "").strip();digits="".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+") and 8<=len(digits)<=15:return "+"+digits
    if len(digits)==10:return "+91"+digits
    raise ValidationError({"phone":"Enter a valid 10-digit Indian mobile number or an international number with country code."})

def dispatch_sms(phone,code,otp_id):
    if settings.DEBUG and not settings.MSG91_AUTH_KEY:
        print(f"[Myraid local OTP] {phone}: {code}");return
    if not settings.MSG91_AUTH_KEY or not settings.MSG91_OTP_FLOW_ID or not settings.MSG91_SENDER_ID:raise ValidationError("MSG91 OTP flow credentials are not configured on this server.")
    if not settings.MSG91_FLOW_URL.startswith("https://"):raise ValidationError("MSG91 flow URL must use HTTPS.")
    recipient={"mobiles":"".join(ch for ch in phone if ch.isdigit()),settings.MSG91_OTP_VARIABLE:code,"CRQID":str(otp_id).replace("-","")[:52]}
    payload=json.dumps({"flow_id":settings.MSG91_OTP_FLOW_ID,"sender":settings.MSG91_SENDER_ID,"recipients":[recipient]}).encode()
    request=urllib.request.Request(settings.MSG91_FLOW_URL,data=payload,method="POST",headers={"authkey":settings.MSG91_AUTH_KEY,"Content-Type":"application/json","Idempotency-Key":str(otp_id)})
    try:
        with urllib.request.urlopen(request,timeout=15) as response:
            result=json.loads(response.read() or b"{}")
            if not 200<=response.status<300 or result.get("type") not in (None,"success"):raise RuntimeError()
    except Exception:raise ValidationError("The OTP provider could not accept this code. Please try again.")


class RequestOTPView(APIView):
    authentication_classes=[];permission_classes=[AllowAny];throttle_classes=[RequestThrottle]
    def post(self,request):
        phone=normalize_phone(request.data.get("phone"));digits=phone[-10:]
        phone_key="otp-phone:"+hashlib.sha256(phone.encode()).hexdigest()
        count=cache.get(phone_key,0)
        if count>=10:return Response({"message":"Too many OTP requests. Please wait before trying again."},status=429)
        cache.set(phone_key,count+1,60*60)
        user=User.objects.filter(is_active=True).filter(models.Q(phone_e164=phone)|models.Q(phone=digits)|models.Q(phone=phone)).first()
        if user and settings.ERP_SMS_ENABLED:
            if LoginOTP.objects.filter(user=user,consumed_at__isnull=True,created_at__gt=timezone.now()-timedelta(seconds=60)).exists():
                return Response({"message":"A code was sent recently. Please wait before requesting another."})
            code=settings.ERP_DEV_FIXED_OTP or f"{secrets.randbelow(1_000_000):06d}"
            LoginOTP.objects.filter(user=user,consumed_at__isnull=True).update(consumed_at=timezone.now())
            ip_hash=hashlib.sha256((request.META.get("REMOTE_ADDR","")+settings.SECRET_KEY).encode()).hexdigest()
            otp=LoginOTP.objects.create(user=user,code_hash=make_password(code),expires_at=timezone.now()+timedelta(minutes=7),request_ip_hash=ip_hash)
            try:dispatch_sms(phone,code,otp.pk)
            except Exception:
                otp.consumed_at=timezone.now();otp.save(update_fields=["consumed_at"]);raise
        elif user and not settings.ERP_SMS_ENABLED:return Response({"message":"Phone OTP delivery is not configured on this server."},status=503)
        return Response({"message":"If an active account matches that phone number, a sign-in code has been sent."})


class VerifyOTPView(APIView):
    authentication_classes=[];permission_classes=[AllowAny];throttle_classes=[VerifyThrottle]
    @transaction.atomic
    def post(self,request):
        phone=normalize_phone(request.data.get("phone"));code=str(request.data.get("code","")).strip()
        if not code.isdigit() or len(code)!=6:raise ValidationError({"code":"Enter the 6-digit code."})
        digits=phone[-10:]
        user=User.objects.filter(is_active=True).filter(models.Q(phone_e164=phone)|models.Q(phone=digits)|models.Q(phone=phone)).first()
        otp=LoginOTP.objects.select_for_update().filter(user=user,consumed_at__isnull=True,expires_at__gt=timezone.now()).order_by("-created_at").first() if user else None
        if not otp or otp.attempts>=5:raise ValidationError("The code is invalid or expired. Request a new one.")
        otp.attempts+=1
        if not check_password(code,otp.code_hash):otp.save(update_fields=["attempts"]);raise ValidationError("The code is invalid or expired. Request a new one.")
        otp.consumed_at=timezone.now();otp.save(update_fields=["attempts","consumed_at"])
        if not user.phone_verified_at:user.phone_verified_at=timezone.now();user.save(update_fields=["phone_verified_at"])
        refresh=RefreshToken.for_user(user);response=Response({"message":"Welcome back.","userData":user_payload(user)})
        set_auth_cookies(response,refresh);get_token(request);return response
