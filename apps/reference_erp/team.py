"""Company roles and phone-verified invitations for the connected workspace."""
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError, NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.api import APIView
from apps.core.models import User, Role, RolePermission, BusinessPermission, Tenant, TenantMembership, UserRole
from apps.core.services import audit
from apps.erp.onboarding import install_permission_catalog
from apps.erp.otp import normalize_phone
from apps.erp.models import Communication, OutboxEvent
from .access import resolve, has_permission, role_for
from .models import CompanyInvitation


PREDEFINED = {
    "Co-owner": None,
    "Accountant": {"workspace.view", "customer.view", "order.view", "invoice.view", "invoice.create", "invoice.export", "payment.view", "payment.record", "expense.view", "expense.create", "report.view"},
    "Sales": {"workspace.view", "customer.view", "customer.create", "customer.edit", "lead.view", "lead.add", "lead.edit", "quotation.view", "quotation.create", "quotation.edit", "quotation.convert", "order.view", "order.create", "order.post", "item.view", "item.create"},
    "Marketing": {"workspace.view", "customer.view", "customer.create", "lead.view", "lead.add", "lead.edit"},
    "Factory Manager": {"workspace.view", "job.view", "job.create", "job.assign", "job.progress", "order.view", "employee.view", "employee.create", "dispatch.view", "dispatch.create"},
    "HR": {"workspace.view", "employee.view", "employee.create", "employee.edit", "employee.view_private", "attendance.view", "attendance.create", "payroll.view", "payroll.create", "payroll.finalize"},
    "Viewer": {"workspace.view", "report.view"},
}

ROLE_DESCRIPTIONS = {
    "Co-owner": "Manage all company operations and staff; the original owner's identity and access remain protected.",
    "Accountant": "Manage invoices, receipts, payments and expenses; view customer and order information.",
    "Sales": "Manage enquiries, customers, quotations, products and confirmed sales orders.",
    "Marketing": "Capture and follow up enquiries and maintain customer contacts.",
    "Factory Manager": "Split and assign jobs, record production progress, maintain worker records and dispatch completed work.",
    "HR": "Maintain employee records, attendance and payroll, including private salary details.",
    "Viewer": "View the dashboard and reports without changing company records.",
}


def install_roles():
    # Seeds are immutable defaults. Company-specific roles are edited separately.
    permissions = install_permission_catalog()
    for name, codes in PREDEFINED.items():
        role, created = Role.objects.get_or_create(tenant=None, code="global-" + slugify(name), defaults={"name": name, "description": ROLE_DESCRIPTIONS[name], "is_system": True})
        if created:
            RolePermission.objects.bulk_create([RolePermission(role=role, permission=p) for p in permissions if codes is None or p.code in codes], ignore_conflicts=True)


def require_team(request):
    tenant, member = resolve(request)
    if not has_permission(request, "staff.manage"):
        raise PermissionDenied("You need team management permission.")
    return tenant, member


def available_roles(tenant):
    return Role.objects.filter(is_active=True).filter(Q(tenant=tenant) | Q(tenant__isnull=True, approved_for_tenant_assignment=True)).exclude(code__in=["tenant-admin", "workspace-owner"]).exclude(Q(name__iexact="Owner") | Q(name__iexact="ERP Owner"))


def check_grant(request, role):
    if role.code == "global-co-owner" and not request.tenant_membership.is_tenant_admin:
        raise PermissionDenied("Only an owner or co-owner can appoint a co-owner.")
    if not request.tenant_membership.is_tenant_admin and any(not has_permission(request, p) for p in role.permission_links.filter(permission__is_active=True).values_list("permission__code", flat=True)):
        raise PermissionDenied("You cannot grant permissions beyond your own access.")


def integer_id(value, label="Role"):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValidationError(f"Select a valid {label.lower()} ID.")
    return int(value)


def role_data(role, tenant=None):
    assignments = role.user_assignments.filter(is_active=True)
    if tenant:
        assignments = assignments.filter(tenant=tenant)
    return {"id": role.pk, "name": role.name, "description": role.description, "scope": "Company-specific" if role.tenant_id else "Global", "protected": role.is_system, "permissions": list(role.permission_links.values_list("permission_id", flat=True)), "people": assignments.count()}


class RolesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant, _ = require_team(request)
        return Response({"roles": [role_data(r, tenant) for r in available_roles(tenant)], "permissions": list(BusinessPermission.objects.filter(is_active=True).order_by("module", "name").values("id", "name", "code", "module", "description"))})

    @transaction.atomic
    def post(self, request):
        tenant, _ = require_team(request)
        if not has_permission(request, "roles.assign"):
            raise PermissionDenied("You need role management permission.")
        name = str(request.data.get("name", "")).strip()
        if not name or len(name) > 100 or name.lower() in {"owner", "erp owner", "co-owner"}:
            raise ValidationError("Enter a role name up to 100 characters. Ownership roles are predefined.")
        selected = request.data.get("permissions", [])
        if not isinstance(selected, list) or any(type(i) is not int for i in selected):
            raise ValidationError("Select valid permission IDs.")
        permissions = list(BusinessPermission.objects.filter(pk__in=selected, is_active=True))
        if len(permissions) != len(set(selected)):
            raise ValidationError("One or more permissions are unavailable.")
        if not request.tenant_membership.is_tenant_admin and any(not has_permission(request, p.code) for p in permissions):
            raise PermissionDenied("You cannot grant permissions beyond your own access.")
        role_id = integer_id(request.data["id"]) if request.data.get("id") else None
        if role_id:
            role = available_roles(tenant).filter(pk=role_id, tenant=tenant, is_system=False).first()
            if not role:
                raise ValidationError("Predefined roles cannot be edited. Create a company-specific role.")
            role.name = name
        else:
            role = Role(tenant=tenant, name=name, code=f"{slugify(name)[:65]}-{secrets.token_hex(4)}")
        role.description = str(request.data.get("description", ""))[:1000]
        role.save()
        role.permission_links.all().delete()
        RolePermission.objects.bulk_create([RolePermission(role=role, permission=p) for p in permissions])
        audit(actor=request.user, tenant=tenant, action="role.updated", resource=role, after=role_data(role))
        return Response(role_data(role), status=200 if role_id else 201)


def send_invitation(invitation):
    if invitation.communication_id:
        Communication.objects.filter(pk=invitation.communication_id, status="queued").update(status="cancelled")
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation.expires_at = timezone.now() + timedelta(days=7)
    origin = getattr(settings, "ERP_PUBLIC_APP_URL", "http://localhost:5173").rstrip("/")
    link = origin + "/?" + urlencode({"invite": token})
    owner = invitation.tenant.owner
    owner_name = (owner.get_full_name() or owner.username) if owner else invitation.invited_by.get_full_name() or invitation.invited_by.username
    content = f"{owner_name}, owner of {invitation.tenant.name}, has invited you to join {invitation.tenant.name} on Myraid ERP as {invitation.role.name}. Create your username and password within 7 days: {link}"
    connected = bool(getattr(settings, "WHATSAPP_API_URL", "") and getattr(settings, "WHATSAPP_ACCESS_TOKEN", ""))
    communication = Communication.objects.create(tenant=invitation.tenant, created_by=invitation.invited_by, channel="whatsapp", recipient=invitation.phone, subject=f"Invitation to {invitation.tenant.name}", content=content, consent_reference=f"Company invitation {invitation.pk}", status="queued" if connected and invitation.phone else "not_configured", error="Use Share invite to send this link in WhatsApp or another messaging app." if not invitation.phone else ("Connect the WhatsApp provider and resend this invitation." if not connected else ""))
    invitation.communication = communication
    invitation.save()
    if connected and invitation.phone:
        OutboxEvent.objects.create(tenant=invitation.tenant, event="communication.queued", payload={"communication_id": str(communication.pk)})
    return link


def invitation_data(inv, share_url=""):
    status = "expired" if inv.status == "pending" and inv.expires_at <= timezone.now() else inv.status
    return {"id": inv.pk, "name": inv.name, "phone": inv.phone, "username": inv.username, "email": inv.email, "role": inv.role.name, "status": status, "expires_at": inv.expires_at.isoformat(), "delivery": inv.communication.status if inv.communication_id else "not_configured", **({"share_url": share_url} if share_url else {})}


def create_invitation(request, inputs):
    tenant, _ = require_team(request)
    phone = str(inputs.get("phone", "")).strip()
    if phone:
        phone = normalize_phone(phone)
    username = str(inputs.get("username", "")).strip().lower()
    email = str(inputs.get("email", "")).strip().lower()
    if email:
        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValidationError({"email": "Enter a valid email address."})
    if not username:
        base_name = slugify(inputs.get("name", "")) or (phone.replace("+", "") if phone else "user")
        candidate = (base_name[:30] or "user").lower()
        if len(candidate) < 3:
            candidate = f"user_{candidate}"
        base_candidate = candidate
        suffix = 1
        while User.objects.filter(username__iexact=candidate).exists() or CompanyInvitation.objects.filter(tenant=tenant, username__iexact=candidate, status="pending", expires_at__gt=timezone.now()).exists():
            candidate = f"{base_candidate}_{suffix}"
            suffix += 1
        username = candidate
    else:
        if len(username) < 3 or len(username) > 40 or not all(c.isalnum() or c in "_.-" for c in username):
            raise ValidationError({"username": "Choose a username using 3–40 letters, numbers, dots, dashes or underscores."})
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError({"username": "That username is already in use."})
    if email and User.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": "That email already has an account. Invite that person's existing username instead."})
    label = {"Admin": "Co-owner", "Accounts": "Accountant", "Production": "Factory Manager"}.get(inputs.get("role"), inputs.get("role"))
    role = available_roles(tenant).filter(pk=integer_id(inputs["role_id"])).first() if inputs.get("role_id") else available_roles(tenant).filter(name=label).first()
    if not role:
        raise ValidationError("Select a role available in this company.")
    check_grant(request, role)
    if phone and TenantMembership.objects.filter(tenant=tenant, user__phone_e164=phone).exists():
        raise ValidationError("This phone already belongs to the company. Change the member's role instead.")
    if CompanyInvitation.objects.filter(tenant=tenant, username__iexact=username, status="pending", expires_at__gt=timezone.now()).exists():
        raise ValidationError("This username has a pending invitation. Use resend or revoke.")
    active = TenantMembership.objects.filter(tenant=tenant, is_active=True).count()
    if tenant.plan_id and active >= tenant.plan.user_limit:
        raise ValidationError("Your subscription's active user limit has been reached.")
    if phone and not User.objects.filter(Q(phone_e164=phone) | Q(phone=phone[-10:])).exists():
        first, _, last = str(inputs.get("name", "")).partition(" ")
        placeholder_email = email or f"{phone.replace('+', '')}.{secrets.token_hex(4)}@phone.workspace.invalid"
        User.objects.create_user(
            email=placeholder_email,
            username=username,
            phone=phone,
            first_name=first,
            last_name=last,
            password=None,
            department="sales" if role.name == "Sales" else "admin" if role.name in ("Admin", "Co-owner") else "factory"
        )
    inv = CompanyInvitation.objects.create(tenant=tenant, phone=phone, username=username, email=email, name=str(inputs.get("name", ""))[:200], role=role, invited_by=request.user, token_hash=secrets.token_hex(32), expires_at=timezone.now() + timedelta(days=7))
    share_url = send_invitation(inv)
    audit(actor=request.user, tenant=tenant, action="team.invited", resource=inv, after=invitation_data(inv))
    return inv, share_url


class InvitationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant, _ = require_team(request)
        return Response({"invitations": [invitation_data(i) for i in CompanyInvitation.objects.filter(tenant=tenant).select_related("role", "communication").order_by("-id")]})

    @transaction.atomic
    def post(self, request):
        tenant, _ = require_team(request)
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        if request.data.get("id"):
            inv = CompanyInvitation.objects.select_for_update().filter(tenant=tenant, pk=integer_id(request.data["id"], "Invitation"), status="pending").first()
            if not inv:
                raise ValidationError("This invitation cannot be changed.")
            if request.data.get("action") == "revoke":
                inv.status = "revoked"
                inv.save(update_fields=["status"])
                Communication.objects.filter(pk=inv.communication_id, status="queued").update(status="cancelled")
            elif request.data.get("action") == "resend":
                check_grant(request, inv.role)
                share_url = send_invitation(inv)
            else:
                raise ValidationError("Choose resend or revoke.")
        else:
            inv, share_url = create_invitation(request, request.data)
        return Response(invitation_data(inv, locals().get("share_url", "")))


class InvitationAcceptView(APIView):
    permission_classes = [AllowAny]

    def invitation(self, token):
        inv = CompanyInvitation.objects.select_related("tenant", "role", "invited_by").filter(token_hash=hashlib.sha256(str(token).encode()).hexdigest(), status="pending", expires_at__gt=timezone.now()).first()
        if not inv:
            raise NotFound("Invitation expired, revoked or already accepted.")
        return inv

    def get(self, request):
        inv = self.invitation(request.query_params.get("token", ""))
        return Response({"company": inv.tenant.name, "role": inv.role.name, "invited_by": inv.invited_by.get_full_name() or inv.invited_by.username, "username": inv.username, "email": inv.email})

    @transaction.atomic
    def post(self, request):
        inv = self.invitation(request.data.get("token", ""))
        tenant = Tenant.objects.select_for_update().get(pk=inv.tenant_id)
        inv = self.invitation(request.data.get("token", ""))
        name = str(request.data.get("name", "") or inv.name).strip()
        first, _, last = name.partition(" ")
        if not name:
            raise ValidationError({"name": "Enter your full name."})
        if not inv.role.is_active or inv.role.tenant_id not in (None, tenant.pk) or (inv.role.tenant_id is None and not inv.role.approved_for_tenant_assignment):
            raise ValidationError("The invited role is no longer available.")
        if tenant.status not in ("active", "trial") or (tenant.status == "trial" and tenant.trial_ends_at and tenant.trial_ends_at <= timezone.now()):
            raise ValidationError("This company is currently inactive.")
        if tenant.plan_id and TenantMembership.objects.filter(tenant=tenant, is_active=True).count() >= tenant.plan.user_limit:
            raise ValidationError("The company has no available user seats.")

        if request.user.is_authenticated:
            if inv.phone and request.user.phone_e164 != inv.phone and request.user.phone != inv.phone[-10:]:
                raise PermissionDenied("You must sign in with the invited phone number.")
            if not request.user.phone_verified_at:
                raise PermissionDenied("Verify your phone number with an OTP before accepting this invitation.")
            user = request.user
            if name:
                user.first_name = first
                user.last_name = last
                user.save(update_fields=["first_name", "last_name"])
        else:
            username = str(request.data.get("username", inv.username)).strip().lower()
            email = str(request.data.get("email", inv.email)).strip().lower()
            password = str(request.data.get("password", ""))
            if len(password) < 8:
                raise ValidationError({"password": "Choose a password with at least 8 characters."})
            user = User.objects.filter(Q(phone_e164=inv.phone) | Q(username__iexact=username) | Q(email__iexact=email)).first()
            if user:
                user.set_password(password)
                user.first_name = first
                user.last_name = last
                user.save()
            else:
                account_email = email or f"{username.lower()}.{secrets.token_hex(5)}@account.myraid.invalid"
                user = User.objects.create_user(email=account_email, username=username, phone=inv.phone or None, password=password, first_name=first, last_name=last)

        membership, _ = TenantMembership.objects.get_or_create(tenant=tenant, user=user)
        membership.is_tenant_admin = (inv.role.code == "global-co-owner")
        membership.is_active = True
        membership.save(update_fields=["is_tenant_admin", "is_active"])
        UserRole.objects.update_or_create(tenant=tenant, user=user, defaults={"role": inv.role, "assigned_by": inv.invited_by, "is_active": True})
        inv.status, inv.accepted_at, inv.name = "accepted", timezone.now(), name
        inv.save(update_fields=["status", "accepted_at", "name"])
        audit(actor=user, tenant=tenant, action="team.invitation_accepted", resource=inv)
        return Response({"tenant_id": tenant.pk, "message": f"Welcome to {tenant.name}.", "username": user.username})


class MemberRoleView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, membership_id):
        tenant, _ = require_team(request)
        if not has_permission(request, "roles.assign"):
            raise PermissionDenied("You need role assignment permission.")
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        member = TenantMembership.objects.select_for_update().filter(pk=membership_id, tenant=tenant).first()
        if not member:
            raise NotFound()
        if member.user_id == tenant.owner_id or member.user_id == request.user.pk:
            raise ValidationError("The owner and your own access cannot be changed here.")
        if member.is_tenant_admin and not request.tenant_membership.is_tenant_admin:
            raise PermissionDenied("Only an owner or co-owner can change a co-owner.")
        role = available_roles(tenant).filter(pk=integer_id(request.data.get("role_id"))).first()
        if not role:
            raise ValidationError("Choose a role available to this company.")
        check_grant(request, role)
        UserRole.objects.filter(tenant=tenant, user=member.user, is_active=True).update(is_active=False)
        member.is_tenant_admin = role.code == "global-co-owner"
        member.save(update_fields=["is_tenant_admin", "updated_at"])
        UserRole.objects.create(tenant=tenant, user=member.user, role=role, assigned_by=request.user)
        audit(actor=request.user, tenant=tenant, action="team.role_changed", resource=member, after={"role": role.name})
        return Response({"message": "Company role updated."})
