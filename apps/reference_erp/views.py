"""Reference command API, backed by existing JWT login and company memberships."""
import hashlib
import json
from uuid import UUID

from django.db import transaction
from django.utils import timezone
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
from .models import ERPRecord, WorkspaceSettings, AuditEvent, CommandReceipt, OrderAttachment, CompanyInvitation


def get_state(tenant):
    workspace = initialize(tenant)
    state = {key: [] for key in COLLECTIONS}
    for record in ERPRecord.objects.filter(tenant=tenant).order_by("-id"):
        if record.collection in state and record.collection != "users":
            state[record.collection].append(record.payload)
    for membership in TenantMembership.objects.filter(tenant=tenant).select_related("user", "tenant").order_by("id"):
        state["users"].append({"id": f"U-{membership.pk:03d}", "name": membership.user.get_full_name() or membership.user.username,
                               "username": membership.user.username,
                               "phone": membership.user.phone_e164[-10:] if membership.user.phone_e164 else membership.user.phone,
                               "role": role_for(membership), "status": "Active" if membership.is_active and membership.user.is_active else "Inactive"})
    for order in state["orders"]:
        order["attachments"] = list(OrderAttachment.objects.filter(tenant=tenant, order_code=order["id"]).values("id", "name", "size"))
    from apps.erp.models import Entitlement
    state["inventory_enabled"] = Entitlement.objects.filter(
        tenant=tenant,
        feature="inventory",
        enabled=True,
        expires_at__gt=timezone.now(),
    ).exists()
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
    from .team import invitation_data, available_roles, role_data
    result["roles"] = [role_data(r, member.tenant) for r in available_roles(member.tenant)] if has_permission(request, "staff.manage") else []
    result["invitations"] = [invitation_data(i) for i in CompanyInvitation.objects.filter(tenant=member.tenant).select_related("role", "communication").order_by("-id")] if has_permission(request, "staff.manage") else []
    result["workers"] = [{k:v for k,v in e.items() if k in ("id", "name", "phone", "department", "role", "status")} for e in state["employees"] if e.get("status") == "Active"] if has_permission(request, "job.assign") else []
    if any(c in user["collections"] for c in ("quotes", "orders", "purchases")) and "items" not in user["collections"]:
        result["items"] = [{k: v for k, v in item.items() if k in ("id", "name", "sku", "unit", "rate", "hsn")}
                           for item in state["items"]]
    if not has_permission(request, "stock.view_cost"):
        result["items"] = [{k: v for k, v in item.items() if k != "cost"} for item in result["items"]]
    result["items"] = [{k:v for k,v in item.items() if k not in ("stock", "min")} for item in result["items"]]
    if not has_permission(request, "stock.view_cost"):
        result["invoices"] = [{k:v for k,v in invoice.items() if k not in ("cogs","cost_basis")} for invoice in result["invoices"]]
    if any(c in user["collections"] for c in ("invoices", "payments", "jobs", "dispatches")) and "orders" not in user["collections"]:
        result["orders"] = [{k: v for k, v in order.items() if k in ("id", "customer", "item", "qty", "status", "due", "attachments")}
                            for order in state["orders"]]
    if any(c in user["collections"] for c in ("orders", "invoices", "jobs", "dispatches")) and "customers" not in user["collections"]:
        result["customers"] = [{"id": c["id"], "name": c["name"]} for c in state["customers"]]
    if not has_permission(request, "employee.view_private"):
        result["employees"] = [{k: v for k, v in e.items() if k != "salary"} for e in result["employees"]]
    if "payouts" in user["collections"] and "employees" not in user["collections"]:
        result["employees"] = [{"id": e["id"], "name": e["name"]} for e in state["employees"]]
    if "payouts" in user["collections"] and "payroll" not in user["collections"]:
        result["payroll"] = [{k: v for k,v in p.items() if k in ("id","period","status")} for p in state["payroll"]]
    if not member.is_tenant_admin:
        result["audit"] = []
    return result


def response_for(request, member, result=None):
    value = {"data": filtered_state(request, member, get_state(member.tenant)), "user": public_user(request, member)}
    if result is not None:
        value["result"] = result
    return Response(value)



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
            if name == "user":
                from .team import create_invitation
                invitation, share_url = create_invitation(request, inputs)
                result = {"id": str(invitation.pk), "share_url": share_url, "message": "Invitation created. WhatsApp queued." if invitation.communication and invitation.communication.status == "queued" else "Invitation pending. Connect WhatsApp and resend to deliver it."}
                CommandReceipt.objects.create(tenant=tenant, user=request.user, request_id=request_id, fingerprint=fingerprint, response_data=result)
                return response_for(request, member, result)
            if name == "job-update" and any(k in inputs for k in ("worker", "assignee")):
                raise ValidationError("Use the Assign job action to change the worker.")
            if name == "job-assign":
                body = {**body, "input": {**inputs, "assigned_by": {"id": request.user.pk, "name": request.user.get_full_name() or request.user.phone, "phone": request.user.phone_e164 or request.user.phone}}}
            if name == "employee":
                from .access import has_permission
                if not has_permission(request, "employee.view_private") and inputs.get("salary") not in (None, "", 0, "0"):
                    raise PermissionDenied("You cannot set salary without private employee access.")
            if name == "approval":
                body = {**body, "input": {**inputs, "by": request.user.get_full_name() or request.user.email}}
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
                # Granular permissions have already been enforced above; role names
                # are display labels and must not constrain custom company roles.
                after, result = apply(prior, body, "Admin", user["plan"])
            except DomainError as exc:
                raise ValidationError(str(exc))
            except PermissionError as exc:
                raise PermissionDenied(str(exc))
            if name == "user-toggle":
                record = next(r for r in after["users"] if r["id"] == result["id"])
                target = TenantMembership.objects.get(tenant=tenant, pk=int(record["id"].split("-")[-1]))
                if target.user_id == tenant.owner_id or target.user_id == request.user.pk:
                    raise ValidationError("Company owners and your own membership cannot be deactivated.")
                if target.is_tenant_admin and not member.is_tenant_admin:
                    raise PermissionDenied("Only owners and co-owners can deactivate a co-owner.")
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
