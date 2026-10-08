from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.models import TenantMembership, UserRole
from apps.core.services import (
    VIEW_IMPLYING_ACTIONS,
    expand_implied_permissions,
    is_tenant_admin,
    resolve_branch,
    resolve_tenant,
)
from .models import Entitlement


def context(request):
    tenant = resolve_tenant(request)
    if not tenant or not TenantMembership.objects.filter(tenant=tenant, user=request.user, is_active=True).exists():
        # Platform status is not implicit, unaudited tenant impersonation.
        raise PermissionDenied("An active company membership is required.")
    if tenant.status not in ("active", "trial") or (tenant.status == "trial" and tenant.trial_ends_at and tenant.trial_ends_at <= timezone.now()):
        raise PermissionDenied("This company subscription is not active.")
    resolve_branch(request, tenant)
    credential=getattr(request,"api_credential",None)
    if credential:
        require_feature(tenant,"api_access")
        if credential.tenant_id!=tenant.id:raise PermissionDenied("API key is not valid for this company.")
    return tenant


def features(tenant):
    now = timezone.now()
    return set(Entitlement.objects.filter(tenant=tenant, enabled=True, effective_at__lte=now).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).values_list("feature", flat=True)) | {"basic"}


def require_feature(tenant, feature):
    if feature not in features(tenant):
        raise PermissionDenied(f"The {feature.replace('_', ' ')} module is not enabled for this company.", code="FEATURE_NOT_ENTITLED")


def assignments(request, permission):
    now = timezone.now()
    credential=getattr(request,"api_credential",None)
    wanted={permission}
    if permission.endswith(".view"):
        prefix=permission.rsplit(".",1)[0]
        wanted={permission, *(f"{prefix}.{action}" for action in VIEW_IMPLYING_ACTIONS)}
    if credential and permission not in expand_implied_permissions(credential.permissions):return UserRole.objects.none()
    branch_filter=Q(branch__isnull=True)
    if getattr(request,"branch",None):branch_filter|=Q(branch=request.branch)
    rows=UserRole.objects.filter(tenant=request.tenant, user=request.user, is_active=True, role__is_active=True,
        role__permission_links__permission__code__in=wanted, role__permission_links__permission__is_active=True
    ).filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now), Q(valid_to__isnull=True) | Q(valid_to__gt=now))
    rows=rows.filter(branch_filter)
    return rows


def scope(request, permission):
    credential = getattr(request, "api_credential", None)
    if not credential and is_tenant_admin(request.user, request.tenant):
        return Q()
    rows = assignments(request, permission)
    if rows.filter(branch__isnull=True).exists():
        return Q()
    branches = list(rows.values_list("branch_id", flat=True))
    if not branches:
        raise PermissionDenied("You do not have permission for this action.", code="PERMISSION_DENIED")
    return Q(branch_id__in=branches)


def authorize(request, permission, obj=None, feature="basic"):
    require_feature(request.tenant, feature)
    predicate = scope(request, permission)
    if obj is not None:
        if obj.tenant_id != request.tenant.id or not type(obj).objects.filter(pk=obj.pk, tenant=request.tenant).filter(predicate).exists():
            from rest_framework.exceptions import NotFound
            raise NotFound("Record not found.")
    return predicate


def has_permission(request, permission):
    credential = getattr(request, "api_credential", None)
    if not credential and is_tenant_admin(request.user, request.tenant):
        return True
    return assignments(request, permission).exists()
