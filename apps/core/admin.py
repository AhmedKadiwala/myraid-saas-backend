from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    AuditLog,
    Branch,
    BusinessPermission,
    Role,
    RolePermission,
    SubscriptionPlan,
    Tenant,
    TenantMembership,
    TenantSubscription,
    User,
    UserRole,
)


@admin.register(User)
class MyraidUserAdmin(UserAdmin):
    model = User
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "department", "is_active")
    fieldsets = UserAdmin.fieldsets + (
        ("CRM", {"fields": ("phone", "department", "quotation_code", "platform_admin")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("CRM", {"fields": ("email", "phone", "department")}),
    )


for model in (
    Tenant, Branch, TenantMembership, SubscriptionPlan, TenantSubscription,
    BusinessPermission, Role, RolePermission, UserRole, AuditLog,
):
    admin.site.register(model)
