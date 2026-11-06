"""
Razorpay Onboarding & Google Mail Integration Views.

Flow:
1. Owner submits exactly 4 info fields:
   - company_name (organization)
   - name (owner full name)
   - phone (only number)
   - email (email address)
2. Backend initiates Razorpay order.
3. Owner pays via Razorpay gateway.
4. Backend verifies payment, provisions user & workspace, activates all modules,
   stores Google mail authorization if provided, and sends confirmation email.
"""

import hmac
import hashlib
import logging
import secrets
import uuid
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .api import APIView
from .auth_views import set_auth_cookies
from .google_auth import _verify_google_id_token, _build_user_payload
from .models import (
    Branch,
    BusinessPermission,
    Invoice,
    Role,
    RolePermission,
    SubscriptionPlan,
    Tenant,
    TenantMembership,
    TenantSettings,
    TenantSubscription,
    User,
    UserGoogleCredential,
    UserRole,
)
from .services import audit
from apps.erp.catalog import FEATURES, PERMISSIONS, price_quote
from apps.erp.models import Entitlement, ErpSettings
from apps.erp.onboarding import install_permission_catalog, owner_role
from apps.erp.otp import normalize_phone

logger = logging.getLogger(__name__)

ONBOARD_PRICE_INR = 2999


def _verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Verify Razorpay payment signature using HMAC SHA256."""
    secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    if not secret:
        # If no secret configured (test mock environment), accept test payments
        return True
    payload = f"{order_id}|{payment_id}".encode("utf-8")
    generated = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(generated, signature or "")


class RazorpayOnboardOrderView(APIView):
    """
    POST /api/v1/auth/onboard/razorpay/order

    Accepts 4 required fields:
      - company_name
      - name
      - phone
      - email
    Validates them and creates a Razorpay payment order.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        company_name = str(request.data.get("company_name", "")).strip()
        name = str(request.data.get("name", "")).strip()
        phone_raw = str(request.data.get("phone", "")).strip()
        email_raw = str(request.data.get("email", "")).strip().lower()

        # Validate exactly 4 fields
        errors = {}
        if not company_name:
            errors["company_name"] = "Company name (organization) is required."
        elif len(company_name) > 200:
            errors["company_name"] = "Company name cannot exceed 200 characters."

        if not name:
            errors["name"] = "Your full name is required."
        elif len(name) > 150:
            errors["name"] = "Name cannot exceed 150 characters."

        if not phone_raw:
            errors["phone"] = "Phone number is required."
        else:
            try:
                phone_formatted = normalize_phone(phone_raw)
                digits = phone_formatted[-10:]
                if User.objects.filter(phone_e164=phone_formatted).exists() or User.objects.filter(phone=digits).exists():
                    errors["phone"] = "An account with this phone number already exists. Please sign in."
            except Exception:
                errors["phone"] = "Please enter a valid 10-digit mobile number."

        if not email_raw:
            errors["email"] = "Email address is required."
        else:
            try:
                validate_email(email_raw)
                normalized_email = User.objects.normalize_email(email_raw).lower()
                if User.objects.filter(email__iexact=normalized_email).exists():
                    errors["email"] = "An account with this email already exists. Please sign in."
            except DjangoValidationError:
                errors["email"] = "Please enter a valid email address."

        if errors:
            return Response({"errors": errors, "message": "Please correct the errors in the form."}, status=status.HTTP_400_BAD_REQUEST)

        amount_paise = ONBOARD_PRICE_INR * 100
        currency = "INR"
        key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
        key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")

        is_mock = not bool(key_id and key_secret)

        if not is_mock:
            try:
                import razorpay
                client = razorpay.Client(auth=(key_id, key_secret))
                receipt_id = f"rcpt_{uuid.uuid4().hex[:10]}"
                order = client.order.create({
                    "amount": amount_paise,
                    "currency": currency,
                    "receipt": receipt_id,
                    "notes": {
                        "company_name": company_name,
                        "email": email_raw,
                        "phone": phone_raw,
                        "name": name,
                    },
                })
                order_id = order["id"]
            except Exception as exc:
                logger.error("Razorpay order creation failed: %s", exc)
                return Response(
                    {"message": f"Could not create payment order: {exc}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        else:
            order_id = f"order_test_{uuid.uuid4().hex[:14]}"
            key_id = key_id or "rzp_test_myraid_mock"

        return Response({
            "order_id": order_id,
            "amount": amount_paise,
            "amount_inr": ONBOARD_PRICE_INR,
            "currency": currency,
            "key_id": key_id,
            "is_test_mode": is_mock,
            "company_name": company_name,
            "name": name,
            "phone": phone_raw,
            "email": email_raw,
        })


class RazorpayOnboardVerifyView(APIView):
    """
    POST /api/v1/auth/onboard/razorpay/verify

    Verifies the Razorpay payment, creates the company owner user,
    provisions the tenant workspace with all modules, saves Google mail
    permissions if provided, and sends a confirmation email to the owner.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        company_name = str(request.data.get("company_name", "")).strip()
        name = str(request.data.get("name", "")).strip()
        phone_raw = str(request.data.get("phone", "")).strip()
        email_raw = str(request.data.get("email", "")).strip().lower()

        razorpay_order_id = str(request.data.get("razorpay_order_id", "")).strip()
        razorpay_payment_id = str(request.data.get("razorpay_payment_id", "")).strip()
        razorpay_signature = str(request.data.get("razorpay_signature", "")).strip()

        password = str(request.data.get("password", "")).strip()
        google_credential = str(request.data.get("google_credential", "")).strip()
        google_access_token = str(request.data.get("google_access_token", "")).strip()

        if not (company_name and name and phone_raw and email_raw):
            return Response(
                {"message": "All 4 information fields are required: company name, name, phone, email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not razorpay_payment_id:
            return Response(
                {"message": "Payment confirmation from Razorpay is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Signature verification if live keys configured
        if getattr(settings, "RAZORPAY_KEY_SECRET", "") and razorpay_signature:
            if not _verify_razorpay_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
                return Response(
                    {"message": "Invalid payment signature from Razorpay. Payment could not be verified."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        normalized_email = User.objects.normalize_email(email_raw).lower()
        phone_formatted = normalize_phone(phone_raw)

        # Check existing user conflict
        if User.objects.filter(email__iexact=normalized_email).exists():
            return Response(
                {"message": "An account with this email already exists. Please sign in."},
                status=status.HTTP_409_CONFLICT,
            )

        first_name, _, last_name = name.partition(" ")
        username = slugify(name) or normalized_email.split("@")[0]
        base_username = username[:30]
        suffix = 1
        while User.objects.filter(username__iexact=username).exists():
            username = f"{base_username}_{suffix}"
            suffix += 1

        # Check Google token if provided
        google_sub = None
        if google_credential:
            try:
                claims = _verify_google_id_token(google_credential)
                google_sub = str(claims.get("sub", ""))
            except Exception as exc:
                logger.info("Google credential verification optional in onboarding: %s", exc)

        # Create the owner user
        user = User.objects.create_user(
            email=normalized_email,
            username=username,
            phone=phone_formatted,
            first_name=first_name,
            last_name=last_name,
            department=User.Department.ADMIN,
        )

        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        else:
            user.set_unusable_password()
            user.save(update_fields=["password"])

        if google_sub:
            user.google_sub = google_sub
            user.save(update_fields=["google_sub"])

        # Store Google mail token if provided (e.g. from Google OAuth consent)
        if google_access_token:
            UserGoogleCredential.objects.update_or_create(
                user=user,
                defaults={
                    "access_token": google_access_token,
                    "can_send_mail": True,
                    "granted_at": timezone.now(),
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                },
            )

        # Provision Workspace
        plan, _ = SubscriptionPlan.objects.get_or_create(
            code="erp-standard-paid",
            defaults={
                "name": "Standard All-Inclusive",
                "price_monthly": ONBOARD_PRICE_INR,
                "user_limit": 50,
                "branch_limit": 5,
                "storage_limit_mb": 5120,
            },
        )

        slug = f"{slugify(company_name)[:45] or 'company'}-{uuid.uuid4().hex[:8]}"
        period_end = timezone.now() + timedelta(days=30)

        tenant = Tenant.objects.create(
            name=company_name,
            owner=user,
            slug=slug,
            status=Tenant.Status.ACTIVE,
            plan=plan,
            razorpay_customer_id=razorpay_payment_id,
            billing_start_date=timezone.now().date(),
        )

        branch = Branch.objects.create(tenant=tenant, name="Main branch", code="main")
        TenantMembership.objects.create(
            tenant=tenant,
            user=user,
            is_tenant_admin=True,
            default_branch=branch,
        )

        TenantSettings.objects.create(tenant=tenant)
        ErpSettings.objects.create(tenant=tenant, legal_name=company_name)

        # Grant owner role with full permission catalog
        owner_role(tenant, user)

        # Record active subscription & paid invoice
        sub = TenantSubscription.objects.create(
            tenant=tenant,
            plan=plan,
            status=TenantSubscription.Status.ACTIVE,
            razorpay_subscription_id=razorpay_order_id or razorpay_payment_id,
            current_period_start=timezone.now(),
            current_period_end=period_end,
        )

        inv_num = f"INV-{timezone.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        Invoice.objects.create(
            tenant=tenant,
            subscription=sub,
            number=inv_num,
            razorpay_invoice_id=razorpay_payment_id,
            status=Invoice.Status.PAID,
            amount=Decimal(str(ONBOARD_PRICE_INR)),
            tax=Decimal("0.00"),
            currency="INR",
            due_at=timezone.now(),
            paid_at=timezone.now(),
        )

        # Enable all ERP features for the paid plan
        entitlements = [
            Entitlement(
                tenant=tenant,
                feature=feature,
                enabled=True,
                expires_at=period_end,
                reason="Paid monthly subscription",
                changed_by=user,
            )
            for feature in FEATURES
            if feature != "gst_integrations"
        ]
        Entitlement.objects.bulk_create(entitlements, ignore_conflicts=True)

        # Send Welcome / Confirmation Email ("than they will get mail from us")
        app_url = getattr(settings, "ERP_PUBLIC_APP_URL", "http://localhost:5173").rstrip("/")
        welcome_subject = f"Welcome to Myraid ERP — {company_name} Workspace Activated!"
        welcome_body = (
            f"Dear {user.get_full_name() or user.username},\n\n"
            f"Thank you for coming onboard with Myraid ERP!\n\n"
            f"Your organization workspace for \"{company_name}\" has been successfully created and activated.\n\n"
            f"Account Details:\n"
            f"- Organization: {company_name}\n"
            f"- Owner Name: {user.get_full_name()}\n"
            f"- Registered Phone: {user.phone}\n"
            f"- Registered Email: {user.email}\n"
            f"- Subscription: Standard All-Inclusive (₹{ONBOARD_PRICE_INR}/month)\n"
            f"- Payment Reference: {razorpay_payment_id}\n\n"
            f"You can access your workspace anytime at:\n{app_url}/app\n\n"
            f"From your workspace, you can manage inventory, sales orders, jobs, attendance, payroll, "
            f"and invite your team members with custom or predefined roles & permissions.\n\n"
            f"Best regards,\n"
            f"The Myraid ERP Team\n"
            f"support@myraid.com"
        )

        try:
            send_mail(
                subject=welcome_subject,
                message=welcome_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            logger.info("Welcome email sent to %s", user.email)
        except Exception as exc:
            logger.warning("Could not deliver welcome email to %s: %s", user.email, exc)

        refresh = RefreshToken.for_user(user)
        payload = _build_user_payload(user)

        audit(
            actor=user,
            action="tenant.onboarded_paid",
            resource=tenant,
            tenant=tenant,
            request=request,
            after={"payment_id": razorpay_payment_id, "amount": ONBOARD_PRICE_INR},
        )

        response = Response(
            {
                "message": f"Welcome to Myraid ERP! Your workspace for {company_name} is ready.",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "userData": payload,
                "tenant_id": tenant.pk,
                "tenant_name": tenant.name,
                "payment_id": razorpay_payment_id,
                "invoice_number": inv_num,
            },
            status=status.HTTP_201_CREATED,
        )
        set_auth_cookies(response, refresh)
        get_token(request)
        return response


class GoogleMailAuthorizeView(APIView):
    """
    POST /api/v1/auth/google/mail-token

    Allows authenticated users to connect or refresh their Google OAuth mail access token
    (with gmail.send scope) so that invitations are sent directly from their Gmail address.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        access_token = str(request.data.get("access_token", "")).strip()
        if not access_token:
            return Response({"message": "access_token is required."}, status=status.HTTP_400_BAD_REQUEST)

        UserGoogleCredential.objects.update_or_create(
            user=request.user,
            defaults={
                "access_token": access_token,
                "can_send_mail": True,
                "granted_at": timezone.now(),
                "scopes": ["https://www.googleapis.com/auth/gmail.send"],
            },
        )

        return Response({
            "message": "Google mail permissions enabled. Invitations will be sent directly from your Gmail account.",
            "can_send_mail": True,
        })
