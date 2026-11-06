"""
Google Identity Services authentication for Myraid ERP.

Flow:
  1. Frontend loads the Google Identity Services script.
  2. User clicks "Continue with Google" and picks their account.
  3. Google calls our credential callback with a signed ID token (JWT).
  4. Frontend POSTs the token to /api/v1/auth/google/signin.
  5. This module verifies the token cryptographically and issues
     our own JWTs via the existing cookie+header dual-auth mechanism.

Security guarantees implemented here:
  - Cryptographic signature verification via Google's public keys.
  - Audience (aud) claim matches GOOGLE_CLIENT_ID from settings.
  - Issuer (iss) claim is accounts.google.com or https://accounts.google.com.
  - Token expiry (exp) is validated.
  - email_verified claim must be True.
  - google sub is stored as immutable identity anchor.
  - Raw ID tokens are never logged.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from .mail_service import send_mail_from_user
from .models import BusinessPermission, RolePermission
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .api import APIView
from .authentication import enforce_csrf
from .auth_views import set_auth_cookies
from .models import TenantMembership, User, Role, UserRole, OrganizationInvitation
from .services import audit, resolve_tenant, enforce_tenant_admin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

def _verify_google_id_token(id_token: str) -> dict:
    """
    Verify a Google ID token and return its claims.

    Validates:
      - Cryptographic signature against Google public keys
      - Audience matches settings.GOOGLE_CLIENT_ID
      - Issuer matches accounts.google.com / https://accounts.google.com
      - Expiration time
      - email_verified is True
      - sub and email are present

    Raises ValueError with a safe user-facing message on any failure.
    Never logs the raw token.
    """
    client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise ValueError("Google Sign-In is not configured on this server.")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        request = google_requests.Request()
        claims = google_id_token.verify_oauth2_token(
            id_token,
            request,
            client_id,
        )
    except Exception as exc:
        token_hash = hashlib.sha256(str(id_token).encode()).hexdigest()[:16]
        logger.warning(
            "Google ID token verification failed (token_hash=%s): %s",
            token_hash,
            type(exc).__name__,
        )
        raise ValueError(
            "Authentication failed. Please try signing in with Google again."
        ) from exc

    # Explicit issuer validation
    issuer = claims.get("iss", "")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("Invalid token issuer.")

    # Explicit audience validation
    if claims.get("aud") != client_id:
        raise ValueError("Invalid token audience.")

    # Expiry validation
    exp = claims.get("exp")
    if exp is not None and timezone.now().timestamp() > exp:
        raise ValueError("Google ID token has expired. Please sign in again.")

    # Email verification validation
    if not claims.get("email_verified"):
        raise ValueError(
            "Your Google account's email address has not been verified. "
            "Please verify your email with Google before signing in."
        )

    if not claims.get("sub"):
        raise ValueError("Google ID token is missing the subject identifier.")

    if not claims.get("email"):
        raise ValueError("Google ID token is missing the email address.")

    return claims


def _find_or_create_user(claims: dict) -> User:
    """
    Locate an existing user or safely create a new passwordless user.

    Identity resolution priority:
      1. Existing user with matching google_sub (authoritative).
      2. Existing user with matching normalized email (link and store sub).
      3. Create a new user record with unusable password.
    """
    google_sub = str(claims["sub"]).strip()
    raw_email = str(claims["email"]).strip()
    email = User.objects.normalize_email(raw_email).lower()
    first_name = str(claims.get("given_name", "")).strip()
    last_name = str(claims.get("family_name", "")).strip()

    # 1. Look up by the immutable Google subject identifier.
    try:
        user = User.objects.get(google_sub=google_sub)
        changed = False
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            changed = True
        if email and user.email.lower() != email:
            # If Google email changed, check for conflict
            if not User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                user.email = email
                changed = True
        if changed:
            user.save(update_fields=["first_name", "last_name", "email"])
        return user
    except User.DoesNotExist:
        pass

    # 2. Look up by email and link the Google sub.
    try:
        user = User.objects.get(email__iexact=email)
        if not user.google_sub:
            user.google_sub = google_sub
            if first_name and not user.first_name:
                user.first_name = first_name
            if last_name and not user.last_name:
                user.last_name = last_name
            user.save(update_fields=["google_sub", "first_name", "last_name"])
        elif user.google_sub != google_sub:
            logger.warning(
                "Google sub mismatch for email=%s",
                hashlib.sha256(email.encode()).hexdigest()[:12],
            )
            raise ValueError(
                "Account linking conflict: this email is already linked to another Google account."
            )
        return user
    except User.DoesNotExist:
        pass

    # 3. Create a brand-new passwordless user.
    with transaction.atomic():
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "google_sub": google_sub,
                "first_name": first_name,
                "last_name": last_name,
                "username": email,
                "department": User.Department.ADMIN,
            },
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        elif not user.google_sub:
            user.google_sub = google_sub
            user.save(update_fields=["google_sub"])
    return user


def _build_user_payload(user: User) -> dict:
    memberships = list(
        TenantMembership.objects.filter(user=user, is_active=True).values(
            "tenant_id", "tenant__name", "is_tenant_admin"
        )
    )
    return {
        "platform_admin": bool(user.platform_admin or user.is_superuser),
        "department": user.department,
        "name": user.get_full_name() or user.email.split("@")[0],
        "email": user.email,
        "phone": user.phone_e164 or user.phone or "",
        "code": user.quotation_code or "",
        "id": user.id,
        "memberships": memberships,
        "is_new_user": not bool(memberships),
    }


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

class GoogleAuthThrottle(AnonRateThrottle):
    rate = "30/minute"


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class GoogleSignInSerializer(serializers.Serializer):
    credential = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        help_text="Google ID token credential returned by Google Identity Services.",
    )


class GoogleOnboardSerializer(serializers.Serializer):
    credential = serializers.CharField(required=False, allow_blank=True, default="")
    company_name = serializers.CharField(max_length=200)
    features = serializers.ListField(child=serializers.CharField(), default=list)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class GoogleSignInView(APIView):
    """
    POST /api/v1/auth/google/signin

    Accepts a Google ID token, verifies it, and returns our own JWTs.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [GoogleAuthThrottle]

    @transaction.atomic
    def post(self, request):
        serializer = GoogleSignInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        credential = serializer.validated_data["credential"]

        try:
            claims = _verify_google_id_token(credential)
        except ValueError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user = _find_or_create_user(claims)
        except ValueError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_active:
            return Response(
                {"message": "This account has been deactivated. Contact your administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)
        payload = _build_user_payload(user)

        audit(
            actor=user,
            action="auth.google.signin",
            resource=user,
            tenant=None,
            request=request,
        )

        response = Response({
            "message": "Welcome back." if payload["memberships"] else "Signed in. Set up your workspace.",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "userData": payload,
        })
        set_auth_cookies(response, refresh)
        get_token(request)
        return response


class GoogleOnboardView(APIView):
    """
    POST /api/v1/auth/google/onboard

    Verifies a Google credential or uses current authenticated user,
    creates the tenant workspace, and returns authenticated JWTs.
    """
    permission_classes = [AllowAny]
    throttle_classes = [GoogleAuthThrottle]

    @transaction.atomic
    def post(self, request):
        from apps.erp.onboarding import create_company_workspace

        serializer = GoogleOnboardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        credential = data.get("credential", "").strip()
        company_name = data["company_name"].strip()

        if not company_name:
            return Response({"message": "Company name is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = None
        if request.user and request.user.is_authenticated:
            user = request.user
        elif credential:
            try:
                claims = _verify_google_id_token(credential)
                user = _find_or_create_user(claims)
            except ValueError as exc:
                return Response({"message": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)
        else:
            return Response({"message": "Authentication required to create a workspace."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response(
                {"message": "This account has been deactivated."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Prevent duplicate company creation from the onboard endpoint.
        if TenantMembership.objects.filter(user=user, is_active=True).exists():
            return Response(
                {
                    "message": "You already have a workspace. Sign in to access it.",
                    "has_workspace": True,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            selected = data["features"]
            tenant, expiry, quote = create_company_workspace(user, company_name, selected)
        except Exception as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        payload = _build_user_payload(user)

        audit(
            actor=user,
            action="auth.google.onboard",
            resource=tenant,
            tenant=tenant,
            request=request,
        )

        response = Response({
            "message": "Your workspace is ready.",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "userData": payload,
            "trial_ends_at": expiry,
            "trial_days": getattr(settings, "ERP_TRIAL_DAYS", 14),
            "subscription_quote": quote,
        }, status=status.HTTP_201_CREATED)
        set_auth_cookies(response, refresh)
        get_token(request)
        return response


# ---------------------------------------------------------------------------
# Organization Invitation Views
# ---------------------------------------------------------------------------

class OrganizationInvitationView(APIView):
    """
    Organization employee invitations.

    Endpoints:
      - POST /api/v1/auth/invitations/          : Create invitation with role & permissions (Admin only)
      - GET  /api/v1/auth/invitations/          : List active invitations (Admin only)
      - GET  /api/v1/auth/invitations/<token>/  : Validate invitation token (Public)
      - POST /api/v1/auth/invitations/<token>/accept/ : Accept invitation with Google or password (Public)
      - DELETE /api/v1/auth/invitations/<token>/ : Revoke/delete invitation (Admin only)
    """

    def get_permissions(self):
        if "token" in self.kwargs and self.request.method in ("GET", "POST"):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_authentication_classes(self):
        if "token" in self.kwargs and self.request.method in ("GET", "POST"):
            return []
        return super().get_authentication_classes()

    def get(self, request, token=None):
        if token:
            try:
                inv = OrganizationInvitation.objects.select_related("tenant", "role", "invited_by").get(
                    token=token,
                    accepted_at__isnull=True,
                )
            except OrganizationInvitation.DoesNotExist:
                return Response(
                    {"message": "This invitation is invalid or has already been used."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if inv.expires_at < timezone.now():
                return Response(
                    {"message": "This invitation has expired. Ask your administrator to send a new one."},
                    status=status.HTTP_410_GONE,
                )
            permissions = []
            if inv.role:
                permissions = list(
                    inv.role.permission_links.filter(permission__is_active=True).values_list(
                        "permission__code", flat=True
                    )
                )
            return Response({
                "valid": True,
                "invited_email": inv.invited_email,
                "tenant_name": inv.tenant.name,
                "role_name": inv.role.name if inv.role else "Employee",
                "role_id": inv.role_id,
                "permissions": permissions,
                "invited_by": inv.invited_by.get_full_name() or inv.invited_by.email if inv.invited_by else None,
                "expires_at": inv.expires_at,
            })

        tenant = resolve_tenant(request)
        invitations = OrganizationInvitation.objects.filter(
            tenant=tenant,
            accepted_at__isnull=True,
        ).select_related("invited_by", "role").prefetch_related("role__permission_links__permission").order_by("-created_at")

        base_url = getattr(settings, "ERP_PUBLIC_APP_URL", "http://localhost:5173").rstrip("/")
        now = timezone.now()
        data = [{
            "id": inv.pk,
            "invited_email": inv.invited_email,
            "role": inv.role.name if inv.role else "Employee",
            "role_id": inv.role_id,
            "permissions": list(
                inv.role.permission_links.filter(permission__is_active=True).values_list(
                    "permission__code", flat=True
                )
            ) if inv.role else [],
            "status": "pending" if inv.expires_at > now else "expired",
            "expires_at": inv.expires_at,
            "invited_by": inv.invited_by.get_full_name() or inv.invited_by.email if inv.invited_by else None,
            "invitation_link": f"{base_url}/?invitation={inv.token}",
        } for inv in invitations]
        return Response({"invitations": data})

    def post(self, request, token=None):
        if token:
            return self._accept_invitation(request, token)
        return self._create_invitation(request)

    def delete(self, request, token=None):
        tenant = enforce_tenant_admin(request)
        if token:
            deleted_count, _ = OrganizationInvitation.objects.filter(tenant=tenant, token=token).delete()
            if not deleted_count:
                return Response({"message": "Invitation not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response({"message": "Invitation revoked."}, status=status.HTTP_200_OK)
        inv_id = request.data.get("id") or request.query_params.get("id")
        if inv_id:
            deleted_count, _ = OrganizationInvitation.objects.filter(tenant=tenant, pk=inv_id).delete()
            if not deleted_count:
                return Response({"message": "Invitation not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response({"message": "Invitation revoked."}, status=status.HTTP_200_OK)
        return Response({"message": "Invitation identifier required."}, status=status.HTTP_400_BAD_REQUEST)

    @transaction.atomic
    def _create_invitation(self, request):
        tenant = enforce_tenant_admin(request)
        email = User.objects.normalize_email(str(request.data.get("email", "")).strip()).lower()
        if not email or "@" not in email:
            return Response({"message": "A valid email address is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user is already an active member of this organization
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user and TenantMembership.objects.filter(tenant=tenant, user=existing_user, is_active=True).exists():
            return Response(
                {"message": "This user is already an active member of this organization."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        role_mode = str(request.data.get("role_mode", "existing")).strip().lower()
        role = None

        if role_mode == "new":
            # Mode C: New role with permissions
            role_name = str(request.data.get("role_name", "")).strip()
            if not role_name:
                return Response({"message": "A role name is required for the new role."}, status=status.HTTP_400_BAD_REQUEST)
            from django.utils.text import slugify
            role_code = f"{slugify(role_name)[:60]}-{secrets.token_hex(3)}"
            description = str(request.data.get("description", "")).strip()

            role = Role.objects.create(
                tenant=tenant,
                name=role_name,
                code=role_code,
                description=description,
                is_system=False,
                approved_for_tenant_assignment=True,
            )

            perm_ids = request.data.get("permission_ids", [])
            perm_codes = request.data.get("permission_codes", [])
            query = BusinessPermission.objects.filter(is_active=True)
            if perm_ids:
                perms = list(query.filter(pk__in=perm_ids))
            elif perm_codes:
                perms = list(query.filter(code__in=perm_codes))
            else:
                perms = []

            for perm in perms:
                RolePermission.objects.get_or_create(role=role, permission=perm)

        elif role_mode == "custom_existing":
            # Mode B: Existing role with modified/new permissions
            role_id = request.data.get("role_id")
            if not role_id:
                return Response({"message": "Please select an existing role to customize."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                base_role = Role.objects.get(pk=role_id, is_active=True)
                if base_role.tenant_id is not None and base_role.tenant_id != tenant.pk:
                    return Response({"message": "Invalid role selected."}, status=status.HTTP_400_BAD_REQUEST)
            except Role.DoesNotExist:
                return Response({"message": "Selected role does not exist."}, status=status.HTTP_400_BAD_REQUEST)

            perm_ids = request.data.get("permission_ids", [])
            perm_codes = request.data.get("permission_codes", [])
            query = BusinessPermission.objects.filter(is_active=True)
            if perm_ids:
                perms = list(query.filter(pk__in=perm_ids))
            elif perm_codes:
                perms = list(query.filter(code__in=perm_codes))
            else:
                perms = []

            # If tenant-owned and not system, update directly. Otherwise create tenant-scoped custom role.
            if base_role.tenant_id == tenant.pk and not base_role.is_system:
                role = base_role
                role.permission_links.all().delete()
                for perm in perms:
                    RolePermission.objects.create(role=role, permission=perm)
            else:
                from django.utils.text import slugify
                custom_name = request.data.get("role_name") or f"{base_role.name} (Custom)"
                role = Role.objects.create(
                    tenant=tenant,
                    name=custom_name,
                    code=f"{slugify(custom_name)[:60]}-{secrets.token_hex(3)}",
                    description=base_role.description or "Customized company role",
                    is_system=False,
                    approved_for_tenant_assignment=True,
                )
                for perm in perms:
                    RolePermission.objects.create(role=role, permission=perm)

        else:
            # Mode A: Existing role with existing permissions
            role_id = request.data.get("role_id")
            if role_id:
                try:
                    role = Role.objects.get(pk=role_id, is_active=True)
                    if role.tenant_id is not None and role.tenant_id != tenant.pk:
                        return Response({"message": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)
                except Role.DoesNotExist:
                    return Response({"message": "Invalid role selected."}, status=status.HTTP_400_BAD_REQUEST)

        # Invalidate previous unaccepted invitations for this email in this tenant
        OrganizationInvitation.objects.filter(
            tenant=tenant,
            invited_email__iexact=email,
            accepted_at__isnull=True,
        ).delete()

        expiry_days = int(getattr(settings, "INVITATION_EXPIRY_DAYS", 7))
        token = secrets.token_urlsafe(32)
        inv = OrganizationInvitation.objects.create(
            tenant=tenant,
            invited_email=email,
            role=role,
            invited_by=request.user,
            token=token,
            expires_at=timezone.now() + timedelta(days=expiry_days),
        )

        base_url = getattr(settings, "ERP_PUBLIC_APP_URL", "http://localhost:5173").rstrip("/")
        invitation_link = f"{base_url}/?invitation={inv.token}"

        # Send invitation email from the inviter (using Google mail API if authorized, or mail backend)
        inviter_name = request.user.get_full_name() or request.user.email
        subject = f"Invitation to join {tenant.name} on Myraid ERP"
        body = f"Hello,\n\n{inviter_name} has invited you to join {tenant.name} on Myraid ERP" + (f" as {role.name}" if role else "") + f".\n\nClick the link below to accept the invitation and access your workspace:\n{invitation_link}\n\nThis invitation is single-use and expires in {expiry_days} days.\n\nBest regards,\n{inviter_name}\n{tenant.name}"

        email_sent = send_mail_from_user(
            actor=request.user,
            recipient_email=email,
            subject=subject,
            body=body,
        )

        audit(
            actor=request.user,
            action="invitation.created",
            resource=inv,
            tenant=tenant,
            request=request,
            after={"role": role.name if role else None, "role_mode": role_mode},
        )

        return Response({
            "message": "Invitation created." + (" Email invitation sent." if email_sent else " Copy and share the link below."),
            "invitation_link": invitation_link,
            "email_sent": email_sent,
            "role": role.name if role else "Employee",
            "role_id": role.id if role else None,
            "expires_at": inv.expires_at,
        }, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def _accept_invitation(self, request, token: str):
        credential = request.data.get("credential", "")
        password = str(request.data.get("password", "")).strip()
        full_name = str(request.data.get("name", "")).strip()

        if not credential and not password:
            return Response({"message": "Please provide either Google sign-in credential or a password."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            inv = OrganizationInvitation.objects.select_for_update().get(token=token, accepted_at__isnull=True)
        except OrganizationInvitation.DoesNotExist:
            return Response(
                {"message": "This invitation is invalid or has already been used."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if inv.expires_at < timezone.now():
            return Response(
                {"message": "This invitation has expired. Ask your administrator for a new invitation."},
                status=status.HTTP_410_GONE,
            )

        user = None
        if credential:
            try:
                claims = _verify_google_id_token(credential)
            except ValueError as exc:
                return Response({"message": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

            google_email = User.objects.normalize_email(claims["email"]).lower()
            invited_email = User.objects.normalize_email(inv.invited_email).lower()

            if google_email != invited_email:
                return Response({
                    "message": (
                        f"This invitation was sent to {inv.invited_email}. "
                        f"You authenticated as {claims['email']}. "
                        "Please sign in with the invited Google account."
                    )
                }, status=status.HTTP_400_BAD_REQUEST)

            try:
                user = _find_or_create_user(claims)
            except ValueError as exc:
                return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        else:
            # Password acceptance
            invited_email = User.objects.normalize_email(inv.invited_email).lower()
            if len(password) < 6:
                return Response({"message": "Password must be at least 6 characters."}, status=status.HTTP_400_BAD_REQUEST)

            first_name, _, last_name = full_name.partition(" ")
            user = User.objects.filter(email__iexact=invited_email).first()
            if user:
                user.set_password(password)
                if first_name and not user.first_name:
                    user.first_name = first_name
                    user.last_name = last_name
                user.save()
            else:
                user = User.objects.create_user(
                    email=invited_email,
                    username=invited_email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    department=User.Department.ADMIN if (inv.role and inv.role.code in ("tenant-admin", "global-co-owner")) else User.Department.SALES,
                )

        if not user.is_active:
            return Response(
                {"message": "This account has been deactivated."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Mark single-use invitation as accepted
        inv.accepted_at = timezone.now()
        inv.accepted_by = user
        inv.save(update_fields=["accepted_at", "accepted_by"])

        # Create or activate TenantMembership
        membership, created = TenantMembership.objects.get_or_create(
            tenant=inv.tenant,
            user=user,
            defaults={"is_active": True},
        )
        if not membership.is_active:
            membership.is_active = True
            membership.save(update_fields=["is_active"])

        # Assign role if configured
        if inv.role:
            UserRole.objects.filter(
                tenant=inv.tenant,
                user=user,
                is_active=True,
            ).exclude(role=inv.role).update(is_active=False)

            assignment, _ = UserRole.objects.get_or_create(
                tenant=inv.tenant,
                user=user,
                role=inv.role,
                branch=None,
                defaults={"assigned_by": inv.invited_by or user, "is_active": True},
            )
            if not assignment.is_active:
                assignment.is_active = True
                assignment.save(update_fields=["is_active"])

        refresh = RefreshToken.for_user(user)
        payload = _build_user_payload(user)

        audit(
            actor=user,
            action="invitation.accepted",
            resource=inv,
            tenant=inv.tenant,
            request=request,
        )

        response = Response({
            "message": f"Welcome to {inv.tenant.name}!",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "userData": payload,
            "tenant_id": inv.tenant.pk,
            "tenant_name": inv.tenant.name,
        })
        set_auth_cookies(response, refresh)
        get_token(request)
        return response
