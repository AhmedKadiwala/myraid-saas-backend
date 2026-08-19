from django.contrib.auth import authenticate
from django.conf import settings
from django.middleware.csrf import get_token
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .api import APIView
from .authentication import enforce_csrf
from .models import TenantMembership, User
from .serializers import SignupSerializer, UserSerializer
from .services import enforce_tenant_admin, ensure_plan_limit, resolve_tenant


def set_auth_cookies(response, refresh):
    response.set_cookie(
        "access_token", str(refresh.access_token), httponly=True,
        secure=not settings.DEBUG, samesite="Lax", max_age=15 * 60,
    )
    response.set_cookie(
        "refresh_token", str(refresh), httponly=True,
        secure=not settings.DEBUG, samesite="Lax", max_age=7 * 24 * 60 * 60,
    )


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        if not email or not password or len(password) < 6:
            return Response(
                {"message": "Input validation error", "error": [
                    {"path": ["password"], "message": "Password should be atleast 6 characters long"}
                ]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = authenticate(request, email=email, password=password)
        if user is None:
            message = (
                "Email not found. Enter correct email"
                if not User.objects.filter(email__iexact=email).exists()
                else "Incorrect password"
            )
            return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)
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
                "department": user.department,
                "name": user.get_full_name(),
                "email": user.email,
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
        tenant = enforce_tenant_admin(request)
        ensure_plan_limit(
            tenant, "users",
            TenantMembership.objects.filter(tenant=tenant, is_active=True).count(),
        )
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        TenantMembership.objects.create(tenant=tenant, user=user)
        return Response({"message": "Account created successfully"})


class UserInfoView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request, required=False)
        detail = UserSerializer(request.user).data
        if tenant:
            detail["tenant_id"] = tenant.pk
            detail["tenant_name"] = tenant.name
        return Response({"message": "User detail fetched successfully", "detail": detail})


class EditUserView(APIView):
    def post(self, request, user_id):
        tenant = enforce_tenant_admin(request)
        if not TenantMembership.objects.filter(tenant=tenant, user_id=user_id).exists():
            return Response({"message": "User not found"}, status=404)
        user = User.objects.get(pk=user_id)
        serializer = UserSerializer(user, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "User details edited successfully"})


class ChangePasswordView(APIView):
    def post(self, request):
        old_password = request.data.get("old_password", "")
        new_password = request.data.get("new_password", "")
        if len(new_password) < 6:
            return Response({"message": "Input validation error"}, status=400)
        if not request.user.check_password(old_password):
            return Response(
                {"message": "Incorrect password. Enter correct password"}, status=400
            )
        request.user.set_password(new_password)
        request.user.save(update_fields=["password"])
        return Response({"message": "Password changed successfully"})


class ResetPasswordView(APIView):
    def post(self, request, user_id):
        tenant = enforce_tenant_admin(request)
        membership = TenantMembership.objects.filter(
            tenant=tenant, user_id=user_id, is_active=True
        ).select_related("user").first()
        if not membership:
            return Response({"message": "User not found"}, status=404)
        membership.user.set_password("123456")
        membership.user.save(update_fields=["password"])
        return Response({"message": "Password reset successfully"})
