from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    AuditLog,
    Branch,
    BusinessPermission,
    Tenant,
    TenantMembership,
    UserRole,
)

PERMISSION_CACHE_SECONDS = 30

VIEW_IMPLYING_ACTIONS = {
    "add",
    "approve",
    "cancel",
    "checkin",
    "convert",
    "copy",
    "create",
    "decide",
    "delete",
    "destroy",
    "edit",
    "export",
    "finalize",
    "inward",
    "issue",
    "manage",
    "post",
    "progress",
    "receive",
    "record",
    "record_payment",
    "reserve",
    "share",
    "transfer",
    "upload",
    "void",
}


def expand_implied_permissions(codes):
    expanded = set(codes)
    if "*" in expanded:
        return expanded
    for code in list(expanded):
        if "." not in code:
            continue
        resource, action = code.rsplit(".", 1)
        if action in VIEW_IMPLYING_ACTIONS:
            expanded.add(f"{resource}.view")
    return expanded


def resolve_tenant(request, required=True):
    if getattr(request.user, "is_superuser", False) or getattr(
        request.user, "platform_admin", False
    ):
        tenant_id = getattr(request, "requested_tenant_id", None)
        tenant = Tenant.objects.filter(pk=tenant_id).first() if tenant_id else None
        request.tenant = tenant
        return tenant

    tenant_id = getattr(request, "requested_tenant_id", None)
    memberships = TenantMembership.objects.select_related("tenant").filter(
        user=request.user, is_active=True
    )
    membership = memberships.filter(tenant_id=tenant_id).first() if tenant_id else None
    if membership is None and not tenant_id and memberships.count() == 1:
        membership = memberships.first()
    if membership is None:
        if required:
            raise PermissionDenied("A valid X-Tenant-ID membership is required.")
        request.tenant = None
        return None
    if membership.tenant.status in (Tenant.Status.SUSPENDED, Tenant.Status.CANCELLED):
        raise PermissionDenied("Tenant access is suspended.")
    request.tenant = membership.tenant
    request.tenant_membership = membership
    return membership.tenant


def resolve_branch(request, tenant=None):
    tenant = tenant or getattr(request, "tenant", None)
    branch_id = getattr(request, "requested_branch_id", None)
    if not branch_id:
        membership=getattr(request,"tenant_membership",None)
        if membership and membership.default_branch_id:
            branch=Branch.objects.filter(pk=membership.default_branch_id,tenant=tenant,is_active=True).first()
            request.branch=branch
            return branch
        request.branch = None
        return None
    branch = Branch.objects.filter(pk=branch_id, tenant=tenant, is_active=True).first()
    if branch is None:
        raise PermissionDenied("Branch does not belong to the active tenant.")
    request.branch = branch
    return branch


def permission_cache_key(user_id, tenant_id, branch_id):
    return f"rbac:v1:{tenant_id}:{user_id}:{branch_id or 'global'}"


def is_tenant_admin(user, tenant):
    if not user or not tenant:
        return False
    return TenantMembership.objects.filter(
        tenant=tenant, user=user, is_active=True, is_tenant_admin=True
    ).exists()


def effective_permissions(user, tenant, branch=None, at=None):
    if user.is_superuser or user.platform_admin:
        return {"*"}
    if tenant is None:
        return set()
    key = permission_cache_key(user.pk, tenant.pk, getattr(branch, "pk", None))
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    at = at or timezone.now()
    assignments = UserRole.objects.filter(
        tenant=tenant,
        user=user,
        is_active=True,
        role__is_active=True,
        role__permission_links__permission__is_active=True,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=at),
        Q(valid_to__isnull=True) | Q(valid_to__gte=at),
    )
    assignments = assignments.filter(branch__isnull=True)
    codes = set(
        assignments.values_list(
            "role__permission_links__permission__code", flat=True
        ).distinct()
    )
    if is_tenant_admin(user, tenant):
        codes.update(
            BusinessPermission.objects.filter(is_active=True).values_list(
                "code", flat=True
            )
        )
        codes.update({"tenant.manage", "staff.manage", "roles.assign", "audit.view"})
    codes = expand_implied_permissions(codes)
    cache.set(key, sorted(codes), PERMISSION_CACHE_SECONDS)
    return codes


def has_business_permission(user, tenant, code, branch=None):
    if is_tenant_admin(user, tenant):
        return True
    codes = effective_permissions(user, tenant, branch)
    return "*" in codes or code in codes


def require_permission(request, code):
    tenant = resolve_tenant(request)
    branch = resolve_branch(request, tenant)
    if not has_business_permission(request.user, tenant, code, branch):
        raise PermissionDenied(f"Missing business permission: {code}")
    return tenant


def enforce_tenant_admin(request):
    tenant = resolve_tenant(request)
    if request.user.is_superuser or request.user.platform_admin:
        return tenant
    if not is_tenant_admin(request.user, tenant):
        raise PermissionDenied("Tenant administrator access required.")
    return tenant


def ensure_plan_limit(tenant, key, current_value, increment=1):
    if tenant.plan_id is None:
        return
    limits = {
        "users": tenant.plan.user_limit,
        "branches": tenant.plan.branch_limit,
        "storage_mb": tenant.plan.storage_limit_mb,
    }
    limit = limits.get(key)
    if limit is not None and current_value + increment > limit:
        raise ValidationError({key: f"Plan limit of {limit} exceeded."})


def audit(*, actor, action, resource, tenant=None, before=None, after=None, request=None):
    return AuditLog.objects.create(
        tenant=tenant,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        resource_type=resource.__class__.__name__ if resource else "",
        resource_id=str(getattr(resource, "pk", "")),
        before=before,
        after=after,
        ip_address=(request.META.get("REMOTE_ADDR") if request else None),
    )
