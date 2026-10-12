"""Translate the existing granular grants and paid capabilities to reference UI areas."""
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.models import TenantMembership, UserRole
from apps.erp.security import context, features, has_permission
from .engine import COLLECTION_MODULE, PLANS, ROLE_MODULES

COLLECTION_ACCESS = {
    "customers": ("customer", "basic"), "leads": ("lead", "basic"),
    "quotes": ("quotation", "basic"), "orders": ("order", "basic"),
    "jobs": ("job", "work_orders"), "suppliers": ("supplier", "purchase"),
    "purchases": ("purchase", "purchase"), "items": ("item", "inventory"),
    "stockMoves": ("stock", "inventory"), "dispatches": ("dispatch", "dispatch"),
    "invoices": ("invoice", "basic"), "payments": ("payment", "basic"),
    "expenses": ("expense", "expense_management"), "employees": ("employee", "hrms"),
    "attendance": ("attendance", "attendance_hr"), "payroll": ("payroll", "payroll"),
    "approvals": ("approval", "approvals"), "tasks": ("task", "tasks"),
    "users": ("staff", "basic"),
}
COMMAND_ACCESS = {
    "lead": ("lead.add", "basic"), "customer": ("customer.create", "basic"),
    "quote": ("quotation.create", "basic"), "order": ("order.create", "basic"),
    "job": ("job.create", "work_orders"), "job-update": ("job.progress", "work_orders"),
    "supplier": ("supplier.create", "purchase"), "purchase": ("purchase.create", "purchase"),
    "receive": ("purchase.receive", "purchase"), "item": ("item.create", "inventory"),
    "stock": ("stock.adjust", "inventory"), "dispatch": ("dispatch.create", "dispatch"),
    "invoice": ("invoice.create", "basic"), "payment": ("payment.record", "basic"),
    "expense": ("expense.create", "expense_management"), "employee": ("employee.create", "hrms"),
    "attendance": ("attendance.create", "attendance_hr"), "payroll": ("payroll.create", "payroll"),
    "task": ("task.create", "tasks"), "user": ("staff.manage", "basic"),
    "quote-mark-sent": ("quotation.edit", "basic"), "quote-convert": ("quotation.convert", "basic"),
    "lead-convert": ("lead.edit", "basic"), "invoice-from-order": ("invoice.create", "basic"),
    "dispatch-invoice": ("invoice.create", "dispatch"),
    "approval-approve": ("approval.decide", "approvals"), "approval-reject": ("approval.decide", "approvals"),
    "task-done": ("task.edit", "tasks"), "user-toggle": ("staff.manage", "basic"),
    "payroll-finalize": ("payroll.finalize", "payroll"), "company-save": ("settings.manage", "basic"),
}


def role_for(membership):
    if membership.is_tenant_admin:
        return "Owner"
    now = timezone.now()
    assignment = UserRole.objects.filter(
        tenant=membership.tenant, user=membership.user, is_active=True, role__is_active=True,
        branch__isnull=True,
    ).filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now),
             Q(valid_to__isnull=True) | Q(valid_to__gt=now)).select_related("role").first()
    if not assignment:
        return "Viewer"
    label = (assignment.role.name + " " + assignment.role.code).lower()
    for role, aliases in {
        "Owner": ("owner",), "Admin": ("admin",), "Production": ("production", "factory"),
        "Purchase": ("purchase", "procurement"), "Store": ("store", "warehouse"),
        "Accounts": ("account", "finance", "cashier"), "HR": ("hr", "human", "people"),
        "Sales": ("sales",), "Viewer": ("viewer", "read-only"),
    }.items():
        if any(alias in label for alias in aliases):
            return role
    # A custom role retains only its real granular permissions below.
    return "Admin"


def plan_for(tenant):
    code = tenant.plan.code if tenant.plan_id else ""
    if code in PLANS:
        return code
    if "full" in code:
        return "full"
    if features(tenant) - {"basic"} or any(x in code for x in ("growth", "recommended")):
        return "recommended"
    return "basic"


def resolve(request):
    tenant = context(request)
    if request.headers.get("X-Branch-ID"):
        raise PermissionDenied("The connected workspace uses company-wide records. Clear the branch filter first.")
    membership = TenantMembership.objects.select_related("tenant", "user").get(
        tenant=tenant, user=request.user, is_active=True,
    )
    return tenant, membership


def can_collection(request, collection):
    permission, feature = COLLECTION_ACCESS[collection]
    if feature not in features(request.tenant):
        return False
    code = "staff.manage" if collection == "users" else permission + ".view"
    return has_permission(request, code)


def public_user(request, membership):
    tenant = membership.tenant
    role, plan = role_for(membership), plan_for(tenant)
    collections = [c for c in COLLECTION_MODULE if can_collection(request, c)
                   and COLLECTION_MODULE[c] in ROLE_MODULES[role]
                   and COLLECTION_MODULE[c] in PLANS[plan]["modules"]]
    modules = {COLLECTION_MODULE[c] for c in collections}
    if has_permission(request, "workspace.view"):
        modules.add("dashboard")
    if has_permission(request, "report.view") or has_permission(request, "profitability.view"):
        modules.add("reports")
    if has_permission(request, "settings.view") or has_permission(request, "settings.manage"):
        modules.add("settings")
    modules &= ROLE_MODULES[role] & PLANS[plan]["modules"]
    return {
        "name": membership.user.get_full_name() or membership.user.email,
        "phone": membership.user.phone, "role": role, "company": tenant.name,
        "tenant": tenant.slug, "tenant_id": tenant.pk, "plan": plan,
        "cap": tenant.plan.user_limit if tenant.plan_id else PLANS[plan]["cap"],
        "price": float(tenant.agreed_monthly_price) if tenant.agreed_monthly_price is not None else None,
        "pricing_mode": "negotiated", "status": tenant.status,
        "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        "modules": sorted(modules), "collections": collections,
        "can_profitability": "profitability" in features(tenant) and has_permission(request, "profitability.view") and has_permission(request, "stock.view_cost"),
        "memberships": list(TenantMembership.objects.filter(user=request.user, is_active=True)
                            .exclude(tenant__status__in=["suspended", "cancelled"])
                            .values("tenant_id", "tenant__name")),
    }


def authorize_command(request, name):
    if name in ("reset", "set-plan"):
        raise PermissionDenied("Demo resets and subscription changes are unavailable in the company workspace.")
    if name not in COMMAND_ACCESS:
        raise PermissionDenied("Unsupported workspace command.")
    permission, feature = COMMAND_ACCESS[name]
    if feature not in features(request.tenant) or not has_permission(request, permission):
        raise PermissionDenied("Your company role or subscription does not allow this action.")
    # Commands combine resources; enforce permissions on every changed ledger.
    extra = {
        "quote-convert": ("order.create",), "lead-convert": ("customer.create",),
        "receive": ("stock.inward",), "dispatch": ("stock.issue",),
    }
    if any(not has_permission(request, code) for code in extra.get(name, ())):
        raise PermissionDenied("Your role cannot change the linked records for this action.")
    if name in ("receive", "dispatch") and "inventory" not in features(request.tenant):
        raise PermissionDenied("Inventory must be enabled for this stock operation.")
