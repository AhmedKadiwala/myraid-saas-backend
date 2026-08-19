from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    AuditLog,
    Branch,
    Tenant,
    TenantMembership,
    UserRole,
)

PERMISSION_CACHE_SECONDS = 300


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
    if membership is None and memberships.count() == 1:
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
        request.branch = None
        return None
    branch = Branch.objects.filter(pk=branch_id, tenant=tenant, is_active=True).first()
    if branch is None:
        raise PermissionDenied("Branch does not belong to the active tenant.")
    request.branch = branch
    return branch


def permission_cache_key(user_id, tenant_id, branch_id):
    return f"rbac:v1:{tenant_id}:{user_id}:{branch_id or 'global'}"


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
        role__is_active=True,
        role__permission_links__permission__is_active=True,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=at),
        Q(valid_to__isnull=True) | Q(valid_to__gte=at),
    )
    if branch:
        assignments = assignments.filter(Q(branch__isnull=True) | Q(branch=branch))
    else:
        assignments = assignments.filter(branch__isnull=True)
    codes = set(
        assignments.values_list(
            "role__permission_links__permission__code", flat=True
        ).distinct()
    )
    membership = TenantMembership.objects.filter(
        tenant=tenant, user=user, is_active=True, is_tenant_admin=True
    ).exists()
    if membership:
        codes.update({"tenant.manage", "staff.manage", "roles.assign", "audit.view"})
    cache.set(key, sorted(codes), PERMISSION_CACHE_SECONDS)
    return codes


def has_business_permission(user, tenant, code, branch=None):
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
    if not TenantMembership.objects.filter(
        tenant=tenant, user=request.user, is_active=True, is_tenant_admin=True
    ).exists():
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
