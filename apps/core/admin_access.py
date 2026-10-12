from rest_framework.exceptions import PermissionDenied
from .models import TenantMembership


def is_platform_owner(user):
    return bool(user and user.is_authenticated and user.is_active and (user.platform_admin or user.is_superuser))


def can_view_company_billing(user, tenant):
    return is_platform_owner(user) or bool(user and user.is_authenticated and TenantMembership.objects.filter(
        user=user, tenant=tenant, is_active=True, is_tenant_admin=True).exists())


def require_company_admin(request, tenant):
    if not can_view_company_billing(request.user, tenant):
        raise PermissionDenied("Only the company admin can change company details or view subscription payments.")
