from django.contrib.auth import authenticate
from django.conf import settings
from django.middleware.csrf import get_token
from django.utils import timezone
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .api import APIView
from .authentication import enforce_csrf
from .models import TenantMembership, User
from .serializers import UserSerializer
from .services import enforce_tenant_admin, ensure_plan_limit, resolve_tenant


def set_auth_cookies(response, refresh):
    response.set_cookie(
        "access_token", str(refresh.access_token), httponly=True,
        secure=not settings.DEBUG, samesite="Lax",
        max_age=int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
    )
    response.set_cookie(
        "refresh_token", str(refresh), httponly=True,
        secure=not settings.DEBUG, samesite="Lax",
        max_age=max(0, int(refresh["exp"] - timezone.now().timestamp())),
    )


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        identifier = str(request.data.get("username") or request.data.get("email") or "").strip()
        password = request.data.get("password")
        if not identifier or not password or len(password) < 8:
            return Response(
                {"message": "Enter your username or email and a password of at least 8 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user_record = User.objects.filter(username__iexact=identifier).first()
        if user_record is None:
            user_record = User.objects.filter(email__iexact=identifier).first()
        user = authenticate(request, email=user_record.email, password=password) if user_record else None
        if user is None:
            return Response({"message": "Username/email or password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        refresh = RefreshToken.for_user(user)
        memberships = list(
            TenantMembership.objects.filter(user=user, is_active=True)
            .values("tenant_id", "tenant__name", "is_tenant_admin")
        )
        payload = {
            "message": "Login successful",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "userData": {
                "platform_admin": bool(user.platform_admin or user.is_superuser),
                "department": user.department,
                "name": user.get_full_name(),
                "email": user.email if not user.email.endswith(".myraid.invalid") else "",
                "username": user.username,
                "code": user.quotation_code,
                "id": user.id,
                "memberships": memberships,
            },
        }
        response = Response(payload)
        set_auth_cookies(response, refresh)
        get_token(request)
        return response


class RefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        raw = request.data.get("refresh") or request.COOKIES.get("refresh_token")
        if request.COOKIES.get("refresh_token") and not request.data.get("refresh"):
            enforce_csrf(request)
        if not raw:
            return Response({"message": "Refresh token required"}, status=401)
        try:
            old = RefreshToken(raw)
            user = User.objects.get(pk=old["user_id"], is_active=True)
            old.blacklist()
            refresh = RefreshToken.for_user(user)
        except Exception:
            return Response({"message": "Invalid or expired refresh token"}, status=401)
        response = Response({
            "message": "Token refreshed",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        })
        set_auth_cookies(response, refresh)
        return response


class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        if request.COOKIES.get("refresh_token") and not request.data.get("refresh"):
            enforce_csrf(request)
        raw = request.data.get("refresh") or request.COOKIES.get("refresh_token")
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except Exception:
                pass
        response = Response({"message": "Logout successful"})
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")
        return response


class SignupView(APIView):
    def post(self, request):
        from apps.erp.otp import normalize_phone
        import uuid
        tenant = enforce_tenant_admin(request)
        ensure_plan_limit(
            tenant, "users",
            TenantMembership.objects.filter(tenant=tenant, is_active=True).count(),
        )
        phone=normalize_phone(request.data.get("phone"));digits=phone[-10:]
        if User.objects.filter(phone_e164=phone).exists() or User.objects.filter(phone=digits).exists():
            return Response({"message":"A user already has this phone number."},status=400)
        first=str(request.data.get("first_name","")).strip();last=str(request.data.get("last_name","")).strip()
        if not first:return Response({"message":"First name is required."},status=400)
        department=request.data.get("department",User.Department.SALES)
        if department not in User.Department.values:return Response({"message":"Choose a valid department."},status=400)
        user=User.objects.create_user(email=f"{digits}.{uuid.uuid4().hex[:8]}@phone.myraid.invalid",phone=phone,password=None,first_name=first,last_name=last,department=department,quotation_code=request.data.get("quotation_code") or None)
        user.set_unusable_password();user.save(update_fields=["password"])
        TenantMembership.objects.create(tenant=tenant, user=user)
        return Response({"message": "User added. They can sign in with phone OTP."})


class UserInfoView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        detail = UserSerializer(request.user).data
        detail["memberships"] = list(TenantMembership.objects.filter(user=request.user, is_active=True)
                                     .values("tenant_id", "tenant__name", "is_tenant_admin"))
        return Response({"message": "User detail fetched successfully", "detail": detail})


class EditUserView(APIView):
    def post(self, request, user_id):
        tenant = enforce_tenant_admin(request)
        if tenant.owner_id == user_id and request.user.pk != user_id:
            return Response({"message": "The company owner's identity is protected."}, status=403)
        if not TenantMembership.objects.filter(tenant=tenant, user_id=user_id).exists():
            return Response({"message": "User not found"}, status=404)
        user = User.objects.get(pk=user_id)
        serializer = UserSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "User details edited successfully"})


class ChangePasswordView(APIView):
    def post(self, request):
        old_password = request.data.get("old_password", "")
        new_password = request.data.get("new_password", "")
        if len(new_password) < 8:
            return Response({"message": "Use at least 8 characters for your new password."}, status=400)
        if not request.user.check_password(old_password):
            return Response(
                {"message": "Incorrect password. Enter correct password"}, status=400
            )
        request.user.set_password(new_password)
        request.user.save(update_fields=["password"])
        return Response({"message": "Password changed successfully"})


class RequestPasswordResetView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = str(request.data.get("email", "")).strip()
        user = User.objects.filter(email__iexact=email, is_active=True).exclude(email__endswith=".myraid.invalid").first()
        if user:
            origin = getattr(settings, "ERP_PUBLIC_APP_URL", "http://localhost:5173").rstrip("/")
            link = f"{origin}/?reset={urlsafe_base64_encode(force_bytes(user.pk))}&token={default_token_generator.make_token(user)}"
            send_mail(
                "Reset your Myraid password",
                f"Use this link to choose a new password: {link}\nIf you did not request this, ignore this message.",
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=True,
            )
        return Response({"message": "If that email is registered, a password reset link has been sent."})


class ConfirmPasswordResetView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        try:
            user_id = urlsafe_base64_decode(str(request.data.get("uid", ""))).decode()
            user = User.objects.get(pk=user_id, is_active=True)
        except Exception:
            user = None
        password = str(request.data.get("password", ""))
        token = str(request.data.get("token", ""))
        if not user or not default_token_generator.check_token(user, token):
            return Response({"message": "This password reset link is invalid or expired."}, status=400)
        if len(password) < 8:
            return Response({"message": "Use at least 8 characters for your new password."}, status=400)
        user.set_password(password)
        user.save(update_fields=["password"])
        return Response({"message": "Password updated. Sign in with your new password."})


class ResetPasswordView(APIView):
    def post(self, request, user_id):
        tenant = enforce_tenant_admin(request)
        membership = TenantMembership.objects.filter(
            tenant=tenant, user_id=user_id, is_active=True
        ).select_related("user").first()
        if not membership:
            return Response({"message": "User not found"}, status=404)
        user = membership.user
        if user.email.endswith(".myraid.invalid"):
            return Response({"message": "This account has no recovery email. Ask the user to change their password after signing in."}, status=400)
        link = f"{getattr(settings, 'ERP_PUBLIC_APP_URL', 'http://localhost:5173').rstrip('/')}/?reset={urlsafe_base64_encode(force_bytes(user.pk))}&token={default_token_generator.make_token(user)}"
        send_mail("Reset your Myraid password", f"Use this link to choose a new password: {link}", settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
        return Response({"message": "Password reset link sent to the user's email."})
