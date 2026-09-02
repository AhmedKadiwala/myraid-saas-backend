import hashlib
import json
import secrets
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.db import models, transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.auth_views import set_auth_cookies
from apps.core.models import TenantMembership, User
from apps.core.services import audit

from .models import LoginOTP


class RequestThrottle(AnonRateThrottle):
    rate = "6/hour"


class VerifyThrottle(AnonRateThrottle):
    rate = "20/hour"


class OTPRequestSerializer(serializers.Serializer):
    phone = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
    )

    def validate_phone(self, value):
        return normalize_phone(value)


class OTPVerifySerializer(OTPRequestSerializer):
    code = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
    )
    otp_token = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=128,
    )

    def validate_code(self, value):
        if not value.isdigit() or len(value) != 6:
            raise serializers.ValidationError("Enter the 6-digit OTP code.")
        return value

    def validate_otp_token(self, value):
        if len(value) < 32:
            raise serializers.ValidationError("Enter a valid OTP token.")
        return value


class OTPMessageSerializer(serializers.Serializer):
    message = serializers.CharField()


class OTPRequestResponseSerializer(OTPMessageSerializer):
    user_exists = serializers.BooleanField()
    otp_created = serializers.BooleanField()
    otp_token = serializers.CharField(required=False, allow_null=True)
    delivery_mode = serializers.CharField(required=False)
    mock_otp = serializers.CharField(required=False, allow_null=True)


class OTPVerifyResponseSerializer(OTPMessageSerializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    userData = serializers.JSONField()


def user_payload(user):
    return {
        "department": user.department,
        "name": user.get_full_name(),
        "email": user.email,
        "phone": user.phone_e164 or user.phone,
        "code": user.quotation_code,
        "id": user.id,
        "memberships": list(
            TenantMembership.objects.filter(
                user=user,
                is_active=True,
            ).values(
                "tenant_id",
                "tenant__name",
                "is_tenant_admin",
            )
        ),
    }


def normalize_phone(value):
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits

    if len(digits) == 10:
        return "+91" + digits

    raise ValidationError(
        {
            "phone": (
                "Enter a valid 10-digit Indian mobile number or an "
                "international number with country code."
            )
        }
    )


def find_active_user_by_phone(phone):
    """
    Find an active user using the normalized phone number.

    New/updated users should have phone_e164 populated by core.User.save().
    The legacy phone checks are kept for older records.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    indian_local_number = digits[-10:] if phone.startswith("+91") and len(digits) == 12 else None

    query = models.Q(phone_e164=phone) | models.Q(phone=phone)

    if indian_local_number:
        query |= models.Q(phone=indian_local_number)

    return User.objects.filter(is_active=True).filter(query).first()


def hash_otp_token(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def otp_fail_key(phone):
    return "otp-verify-fail:" + hashlib.sha256(phone.encode()).hexdigest()


def record_otp_failure(phone):
    key = otp_fail_key(phone)
    count = cache.get(key, 0) + 1
    cache.set(key, count, 15 * 60)
    return count


def reset_otp_failures(phone):
    cache.delete(otp_fail_key(phone))


def dispatch_sms(phone, code, otp_id):
    if settings.DEBUG and not settings.MSG91_AUTH_KEY:
        print(f"[Myraid local OTP] {phone}: {code}")
        return

    if (
        not settings.MSG91_AUTH_KEY
        or not settings.MSG91_OTP_FLOW_ID
        or not settings.MSG91_SENDER_ID
    ):
        raise ValidationError(
            "MSG91 OTP flow credentials are not configured on this server."
        )

    if not settings.MSG91_FLOW_URL.startswith("https://"):
        raise ValidationError("MSG91 flow URL must use HTTPS.")

    recipient = {
        "mobiles": "".join(ch for ch in phone if ch.isdigit()),
        settings.MSG91_OTP_VARIABLE: code,
        "CRQID": str(otp_id).replace("-", "")[:52],
    }

    payload = json.dumps(
        {
            "flow_id": settings.MSG91_OTP_FLOW_ID,
            "sender": settings.MSG91_SENDER_ID,
            "recipients": [recipient],
        }
    ).encode()

    request = urllib.request.Request(
        settings.MSG91_FLOW_URL,
        data=payload,
        method="POST",
        headers={
            "authkey": settings.MSG91_AUTH_KEY,
            "Content-Type": "application/json",
            "Idempotency-Key": str(otp_id),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read() or b"{}")
            if not 200 <= response.status < 300:
                raise RuntimeError()
            if result.get("type") not in (None, "success"):
                raise RuntimeError()
    except Exception as exc:
        raise ValidationError(
            "The OTP provider could not accept this code. Please try again."
        ) from exc


class RequestOTPView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RequestThrottle]

    @extend_schema(
        request=OTPRequestSerializer,
        responses={200: OTPRequestResponseSerializer},
    )
    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"]

        phone_key = "otp-phone:" + hashlib.sha256(phone.encode()).hexdigest()
        count = cache.get(phone_key, 0)

        if count >= 10:
            return Response(
                {
                    "message": (
                        "Too many OTP requests. Please wait before trying again."
                    ),
                    "user_exists": False,
                    "otp_created": False,
                    "otp_token": None,
                },
                status=429,
            )

        cache.set(phone_key, count + 1, 60 * 60)

        user = find_active_user_by_phone(phone)

        if not user:
            return Response(
                {
                    "message": "No active user exists with this phone number.",
                    "user_exists": False,
                    "otp_created": False,
                    "otp_token": None,
                },
                status=404,
            )

        recent_otp = LoginOTP.objects.filter(
            user=user,
            consumed_at__isnull=True,
            created_at__gt=timezone.now() - timedelta(seconds=60),
        ).exists()

        if recent_otp:
            return Response(
                {
                    "message": (
                        "A code was created recently. Please wait before "
                        "requesting another."
                    ),
                    "user_exists": True,
                    "otp_created": False,
                    "otp_token": None,
                },
                status=429,
            )

        delivery_mode = getattr(settings, "ERP_OTP_MODE", "mock").strip().lower()

        if delivery_mode == "mock":
            # Mock mode uses the exact same OTP persistence/verification flow
            # as SMS mode. The only difference is delivery: instead of calling
            # an SMS provider, the generated code is returned in the response.
            code = f"{secrets.randbelow(1_000_000):06d}"

        elif delivery_mode == "sms":
            if not settings.ERP_SMS_ENABLED:
                return Response(
                    {
                        "message": (
                            "SMS OTP mode is selected but SMS delivery is disabled."
                        ),
                        "user_exists": True,
                        "otp_created": False,
                        "otp_token": None,
                        "delivery_mode": "sms",
                    },
                    status=503,
                )

            code = (
                settings.ERP_DEV_FIXED_OTP
                or f"{secrets.randbelow(1_000_000):06d}"
            )

        else:
            return Response(
                {
                    "message": (
                        "Invalid ERP_OTP_MODE. Use 'mock' for development "
                        "or 'sms' for real SMS delivery."
                    ),
                    "user_exists": True,
                    "otp_created": False,
                    "otp_token": None,
                },
                status=503,
            )

        otp_token = secrets.token_urlsafe(32)

        # Invalidate previous unconsumed OTPs for the same user.
        LoginOTP.objects.filter(
            user=user,
            consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())

        ip_hash = hashlib.sha256(
            (
                request.META.get("REMOTE_ADDR", "")
                + settings.SECRET_KEY
            ).encode()
        ).hexdigest()

        otp = LoginOTP.objects.create(
            user=user,
            code_hash=make_password(code),
            otp_token_hash=hash_otp_token(otp_token),
            expires_at=timezone.now() + timedelta(minutes=7),
            request_ip_hash=ip_hash,
        )

        if delivery_mode == "sms":
            try:
                dispatch_sms(phone, code, otp.pk)
            except Exception:
                # Keep the failed record for audit/debugging but make it unusable.
                otp.consumed_at = timezone.now()
                otp.save(update_fields=["consumed_at"])
                raise
        else:
            # No SMS gateway is called in mock mode.
            print(f"[Myraid mock OTP] {phone}: {code}")

        audit(
            actor=user,
            action="auth.otp.requested",
            resource=otp,
            tenant=None,
            request=request,
        )

        response_payload = {
            "message": (
                "Mock OTP created successfully."
                if delivery_mode == "mock"
                else "A sign-in code has been sent."
            ),
            "user_exists": True,
            "otp_created": True,
            "otp_token": otp_token,
            "delivery_mode": delivery_mode,
        }

        # Return the actual code only in explicit mock mode.
        if delivery_mode == "mock":
            response_payload["mock_otp"] = code

        return Response(response_payload)


class VerifyOTPView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [VerifyThrottle]

    @extend_schema(
        request=OTPVerifySerializer,
        responses={200: OTPVerifyResponseSerializer},
    )
    @transaction.atomic
    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"]
        code = serializer.validated_data["code"]
        otp_token = serializer.validated_data["otp_token"]

        if cache.get(otp_fail_key(phone), 0) >= 5:
            return Response(
                {
                    "message": (
                        "Too many failed OTP attempts. Request a new code "
                        "and try again."
                    )
                },
                status=429,
            )

        user = find_active_user_by_phone(phone)

        otp = None
        if user:
            otp = (
                LoginOTP.objects.select_for_update()
                .filter(
                    user=user,
                    otp_token_hash=hash_otp_token(otp_token),
                    consumed_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
                .order_by("-created_at")
                .first()
            )

        if not otp or otp.attempts >= 5:
            record_otp_failure(phone)
            raise ValidationError(
                "The code is invalid or expired. Request a new one."
            )

        otp.attempts += 1

        if not check_password(code, otp.code_hash):
            otp.save(update_fields=["attempts"])
            record_otp_failure(phone)

            audit(
                actor=user,
                action="auth.otp.failed",
                resource=otp,
                tenant=None,
                request=request,
            )

            raise ValidationError(
                "The code is invalid or expired. Request a new one."
            )

        otp.consumed_at = timezone.now()
        otp.save(update_fields=["attempts", "consumed_at"])

        reset_otp_failures(phone)

        audit(
            actor=user,
            action="auth.otp.verified",
            resource=otp,
            tenant=None,
            request=request,
        )

        if not user.phone_verified_at:
            user.phone_verified_at = timezone.now()
            user.save(update_fields=["phone_verified_at"])

        refresh = RefreshToken.for_user(user)

        response = Response(
            {
                "message": "Welcome back.",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "userData": user_payload(user),
            }
        )

        set_auth_cookies(response, refresh)
        get_token(request)

        return response
