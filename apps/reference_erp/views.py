"""Reference command API, backed by existing JWT login and company memberships."""
import hashlib
import json
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from apps.core.api import APIView
from apps.core.models import Tenant, TenantMembership, TenantSettings, User, Role, RolePermission, UserRole, BusinessPermission
from apps.erp.models import ErpSettings
from apps.erp.onboarding import install_permission_catalog
from .access import resolve, public_user, authorize_command, COMMAND_ACCESS, COLLECTION_ACCESS, role_for
from .engine import COLLECTIONS, COLLECTION_MODULE, ROLE_MODULES, apply, DomainError
from .importer import initialize
from .models import ERPRecord, WorkspaceSettings, AuditEvent, CommandReceipt


def get_state(tenant):
    workspace = initialize(tenant)
    state = {key: [] for key in COLLECTIONS}
    for record in ERPRecord.objects.filter(tenant=tenant).order_by("-id"):
        if record.collection in state and record.collection != "users":
            state[record.collection].append(record.payload)
    for membership in TenantMembership.objects.filter(tenant=tenant).select_related("user", "tenant").order_by("id"):
        state["users"].append({"id": f"U-{membership.pk:03d}", "name": membership.user.get_full_name() or membership.user.email,
                               "phone": membership.user.phone_e164[-10:] if membership.user.phone_e164 else membership.user.phone,
                               "role": role_for(membership), "status": "Active" if membership.is_active and membership.user.is_active else "Inactive"})
    state.update(company={**workspace.company_meta, "name": tenant.name}, toggles=workspace.toggles,
                 version=2, audit=[{"text": e.message, "at": e.created_at.strftime("%d %b %Y %H:%M")}
                                  for e in AuditEvent.objects.filter(tenant=tenant)[:70]])
    return state


def filtered_state(request, member, state):
    user = public_user(request, member)
    result = {**state}
    for collection in COLLECTIONS:
        if collection not in user["collections"]:
            result[collection] = []
    from apps.erp.security import has_permission
    if any(c in user["collections"] for c in ("quotes", "orders", "purchases")) and "items" not in user["collections"]:
        result["items"] = [{k: v for k, v in item.items() if k in ("id", "name", "sku", "unit", "rate", "hsn")}
                           for item in state["items"]]
    if not has_permission(request, "stock.view_cost"):
        result["items"] = [{k: v for k, v in item.items() if k != "cost"} for item in result["items"]]
    if any(c in user["collections"] for c in ("invoices", "payments", "jobs", "dispatches")) and "orders" not in user["collections"]:
        result["orders"] = [{k: v for k, v in order.items() if k in ("id", "customer", "item", "qty", "status", "due")}
                            for order in state["orders"]]
    if any(c in user["collections"] for c in ("orders", "invoices", "jobs", "dispatches")) and "customers" not in user["collections"]:
        result["customers"] = [{"id": c["id"], "name": c["name"]} for c in state["customers"]]
    if not has_permission(request, "employee.view_private"):
        result["employees"] = [{k: v for k, v in e.items() if k not in ("salary", "phone")} for e in result["employees"]]
    if not member.is_tenant_admin:
        result["audit"] = []
    return result


def response_for(request, member, result=None):
    value = {"data": filtered_state(request, member, get_state(member.tenant)), "user": public_user(request, member)}
    if result is not None:
        value["result"] = result
    return Response(value)


def invite(tenant, record, request):
    actor = request.user
    phone = "+91" + record["phone"]
    user = User.objects.filter(Q(phone_e164=phone) | Q(phone=record["phone"])).first()
    if user and TenantMembership.objects.filter(tenant=tenant, user=user).exists():
        raise ValidationError("This mobile number already belongs to the company.")
    if user and not user.is_active:
        raise ValidationError("This identity is inactive. Contact the account administrator.")
    if not user:
        first, _, last = record["name"].partition(" ")
        if len(first) > 150 or len(last) > 150:
            raise ValidationError("First and last names must each be at most 150 characters.")
        user = User.objects.create_user(email=f"{record['phone']}@phone.workspace.invalid", phone=phone,
                                        first_name=first, last_name=last, password=None)
    membership = TenantMembership.objects.create(tenant=tenant, user=user, is_tenant_admin=record["role"] == "Owner")
    install_permission_catalog()
    role, _ = Role.objects.get_or_create(tenant=tenant, code="workspace-" + record["role"].lower(),
                                         defaults={"name": record["role"], "is_system": True})
    allowed_modules = ROLE_MODULES[record["role"]]
    codes = {permission + ".view" for collection, (permission, _) in COLLECTION_ACCESS.items()
             if COLLECTION_MODULE[collection] in allowed_modules and collection != "users"}
    if record["role"] != "Viewer":
        for command, (permission, _) in COMMAND_ACCESS.items():
            from .engine import FORM_MODULE, ACTION_MODULE
            module = FORM_MODULE.get(command) or ACTION_MODULE.get(command)
            if module in allowed_modules:
                codes.add(permission)
        if "inventory" in allowed_modules:
            codes.update({"stock.inward", "stock.issue"})
    if "dashboard" in allowed_modules:
        codes.add("workspace.view")
    if "reports" in allowed_modules:
        codes.add("report.view")
    if record["role"] in ("Owner", "Admin", "HR"):
        codes.add("employee.view_private")
    from apps.erp.security import has_permission
    if not TenantMembership.objects.filter(tenant=tenant, user=actor, is_active=True, is_tenant_admin=True).exists():
        if any(not has_permission(request, code) for code in codes):
            raise PermissionDenied("You cannot invite a role with permissions beyond your own grants.")
    for code in codes:
        permission, _ = BusinessPermission.objects.get_or_create(code=code, defaults={"name": code, "module": code.split(".")[0]})
        RolePermission.objects.get_or_create(role=role, permission=permission)
    UserRole.objects.create(tenant=tenant, user=user, role=role, assigned_by=actor)
    return membership


class WorkspaceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, member = resolve(request)
        return response_for(request, member)


class RecordsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, collection, code=None):
        _, member = resolve(request)
        if collection not in COLLECTIONS:
            return Response({"error": "Unknown collection"}, status=404)
        if collection not in public_user(request, member)["collections"]:
            raise PermissionDenied("You cannot access this collection.")
        records = filtered_state(request, member, get_state(member.tenant))[collection]
        if code:
            record = next((r for r in records if r["id"] == code), None)
            return Response(record if record else {"error": "Not found"}, status=200 if record else 404)
        return Response({"results": records, "count": len(records)})


class CommandsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant, member = resolve(request)
        body = request.data
        if not isinstance(body, dict) or len(json.dumps(body)) > 32768:
            raise ValidationError("Commands must be JSON objects under 32KB.")
        try:
            request_id = UUID(str(body.get("request_id", "")))
        except (ValueError, TypeError):
            raise ValidationError("A valid request_id UUID is required.")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        name = body.get("name", "")
        if not isinstance(name, str) or not isinstance(body.get("id", ""), str):
            raise ValidationError("Command name and record ID must be strings.")
        authorize_command(request, name)
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
            member.tenant = tenant
            receipt = CommandReceipt.objects.filter(tenant=tenant, user=request.user, request_id=request_id).first()
            if receipt:
                if receipt.fingerprint != fingerprint:
                    return Response({"error": "This request ID was already used for a different command."}, status=409)
                return response_for(request, member, receipt.response_data)
            prior = get_state(tenant)
            inputs = body.get("input", {})
            if not isinstance(inputs, dict):
                raise ValidationError("Command input must be an object.")
            # Preserve full multi-line and regulated legacy workflows instead of flattening them.
            referenced = {str(body.get("id", "")), *(str(inputs.get(k, "")) for k in ("order", "invoice"))}
            if any(r.get("read_only") and r["id"] in referenced for c in COLLECTIONS for r in prior[c]):
                raise ValidationError("This imported record uses a legacy multi-line or approval workflow and is read-only here.")
            user = public_user(request, member)
            if name == "user" and len([u for u in prior["users"] if u["status"] == "Active"]) >= user["cap"]:
                raise ValidationError("Your subscription's active user limit has been reached.")
            if name == "user-toggle":
                target = next((u for u in prior["users"] if u["id"] == body.get("id")), None)
                if target and target["status"] == "Inactive" and len([u for u in prior["users"] if u["status"] == "Active"]) >= user["cap"]:
                    raise ValidationError("Your subscription's active user limit has been reached.")
            try:
                after, result = apply(prior, body, user["role"], user["plan"])
            except DomainError as exc:
                raise ValidationError(str(exc))
            except PermissionError as exc:
                raise PermissionDenied(str(exc))
            if name == "user":
                record = next(r for r in after["users"] if r["id"] == result["id"])
                if record["role"] == "Owner" and not member.is_tenant_admin:
                    raise PermissionDenied("Only a company owner can appoint another owner.")
                invited = invite(tenant, record, request)
                result["id"] = f"U-{invited.pk:03d}"
            if name == "user-toggle":
                record = next(r for r in after["users"] if r["id"] == result["id"])
                target = TenantMembership.objects.get(tenant=tenant, pk=int(record["id"].split("-")[-1]))
                if target.is_tenant_admin or target.user_id == request.user.pk:
                    raise ValidationError("Company owners and your own membership cannot be deactivated.")
                target.is_active = record["status"] == "Active"
                target.save(update_fields=["is_active", "updated_at"])
            for collection in COLLECTIONS:
                if collection == "users":
                    continue
                before = {r["id"]: r for r in prior[collection]}
                for record in after[collection]:
                    if record != before.get(record["id"]):
                        ERPRecord.objects.update_or_create(tenant=tenant, collection=collection, code=record["id"], defaults={"payload": record})
            if after["company"] != prior["company"]:
                workspace = WorkspaceSettings.objects.get(tenant=tenant)
                workspace.company_meta = after["company"]
                workspace.save(update_fields=["company_meta"])
                tenant.name = after["company"]["name"]
                tenant.save(update_fields=["name", "updated_at"])
                ErpSettings.objects.update_or_create(tenant=tenant, defaults={"legal_name": tenant.name,
                    "gstin": after["company"].get("gst", ""), "address": after["company"].get("location", "")})
                settings, _ = TenantSettings.objects.get_or_create(tenant=tenant)
                settings.branding = {**settings.branding, "company_name": tenant.name}
                settings.save(update_fields=["branding"])
            AuditEvent.objects.create(tenant=tenant, user=request.user, message=result["message"][:350])
            CommandReceipt.objects.create(tenant=tenant, user=request.user, request_id=request_id,
                                          fingerprint=fingerprint, response_data=result)
            return response_for(request, member, result)
