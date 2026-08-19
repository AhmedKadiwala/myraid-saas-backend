from rest_framework.permissions import BasePermission

from .services import has_business_permission, resolve_branch, resolve_tenant


class TenantMembershipPermission(BasePermission):
    def has_permission(self, request, view):
        resolve_tenant(request)
        resolve_branch(request, request.tenant)
        return True


class BusinessPermissionRequired(TenantMembershipPermission):
    def has_permission(self, request, view):
        super().has_permission(request, view)
        code = getattr(view, "required_business_permission", None)
        return not code or has_business_permission(
            request.user, request.tenant, code, getattr(request, "branch", None)
        )


class TenantRBACPermission(BasePermission):
    """Central deny-by-default mapping for business mutations and protected reads."""

    ROUTE_RULES = (
        ("/leads/", {
            "GET": "lead.view", "POST": "lead.add", "PUT": "lead.edit",
            "PATCH": "lead.edit", "DELETE": "lead.delete",
        }),
        ("/deals/", {
            "GET": "deal.view", "POST": "deal.add", "PUT": "deal.edit",
            "PATCH": "deal.edit", "DELETE": "deal.delete",
        }),
        ("/descriptions/", {
            "GET": "description.add", "POST": "description.add",
            "PUT": "description.edit", "DELETE": "description.delete",
        }),
        ("/reminders/", {
            "GET": "meeting.schedule", "POST": "meeting.schedule",
            "PUT": "meeting.schedule", "DELETE": "meeting.schedule",
        }),
        ("/quotations/", {
            "GET": "quotation.view", "POST": "quotation.add",
            "PUT": "quotation.edit", "DELETE": "quotation.delete",
        }),
        ("/orders/", {
            "GET": "order.view", "POST": "order.add",
            "PUT": "order.edit", "PATCH": "order.edit", "DELETE": "order.delete",
        }),
        ("/drawings/", {
            "GET": "drawing.view", "POST": "drawing.upload",
            "PUT": "drawing.upload", "PATCH": "drawing.upload",
            "DELETE": "drawing.delete",
        }),
        ("/sources/", {
            "GET": "lead.view", "POST": "tenant.manage", "PUT": "tenant.manage",
        }),
        ("/products/", {
            "GET": "lead.view", "POST": "tenant.manage", "PUT": "tenant.manage",
        }),
        ("/company/", {
            "GET": "lead.view", "POST": "lead.edit", "PUT": "lead.edit",
        }),
        ("/employees/", {"GET": "lead.view"}),
        ("/rbac/roles", {
            "GET": "roles.assign", "POST": "roles.assign",
            "PUT": "roles.assign", "DELETE": "roles.assign",
        }),
        ("/rbac/assignments", {
            "GET": "roles.assign", "POST": "roles.assign",
            "DELETE": "roles.assign",
        }),
        ("/rbac/permissions", {"GET": "roles.assign"}),
        ("/audit-logs", {"GET": "audit.view"}),
        ("/billing/invoices", {"GET": "billing.view"}),
        ("/billing/subscription", {
            "GET": "billing.view", "POST": "billing.manage",
            "DELETE": "billing.manage",
        }),
        ("/tenants/settings", {
            "GET": None, "PUT": "tenant.manage",
        }),
        ("/tenants/branches", {
            "GET": None, "POST": "tenant.manage",
        }),
        ("/auth/signup", {"POST": "staff.manage"}),
        ("/auth/edit-user", {"POST": "staff.manage"}),
        ("/auth/reset-password", {"POST": "staff.manage"}),
    )

    SPECIAL_RULES = (
        ("/deals/edit/status/", "PUT", "deal.status.edit"),
        ("/deals/getBy/", "GET", "deal.analytics"),
        ("/leads/getBy/", "GET", "lead.analytics"),
        ("/deals/get-only-id", "GET", "quotation.copy"),
        ("/quotations/import/", "POST", "quotation.copy"),
        ("/orders/add/payment/", "POST", "order.payment.manage"),
        ("/orders/edit/payment/", "PUT", "order.payment.manage"),
        ("/orders/delete/payment/", "DELETE", "order.payment.manage"),
        ("/orders/add/colour/", "POST", "order.colour.add"),
        ("/drawings/approve/", "POST", "drawing.approve"),
        ("/drawings/reject/", "POST", "drawing.approve"),
    )

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        tenant = resolve_tenant(request)
        branch = resolve_branch(request, tenant)
        path = request.path
        code = None
        matched = False
        for fragment, method, special_code in self.SPECIAL_RULES:
            if fragment in path and request.method == method:
                code, matched = special_code, True
                break
        if not matched:
            for fragment, methods in self.ROUTE_RULES:
                if fragment in path and request.method in methods:
                    code, matched = methods[request.method], True
                    break
        if not matched or code is None:
            return True
        return has_business_permission(request.user, tenant, code, branch)
