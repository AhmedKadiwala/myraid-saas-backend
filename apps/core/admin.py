from django.apps import apps
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import models

from .models import User


admin.site.site_header = "Myraid SaaS Administration"
admin.site.site_title = "Myraid Admin"
admin.site.index_title = "Myraid SaaS / ERP Management"


SENSITIVE_FIELD_NAMES = {
    "password",
    "secret",
    "secret_ciphertext",
    "key_hash",
    "code_hash",
    "otp_token_hash",
    "request_ip_hash",
    "access_token",
    "refresh_token",
    "token",
}

SYSTEM_READONLY_FIELDS = {
    "created_at",
    "updated_at",
    "last_login",
    "date_joined",
    "phone_e164",
}

CORE_LIST_DISPLAY = {
    "SubscriptionPlan": (
        "name", "code", "price_monthly", "price_yearly", "currency",
        "user_limit", "branch_limit", "storage_limit_mb", "is_active",
    ),
    "Tenant": (
        "name", "slug", "status", "plan", "trial_ends_at", "created_at", "updated_at",
    ),
    "TenantSettings": (
        "tenant", "timezone", "currency", "date_format", "quotation_prefix",
    ),
    "Branch": (
        "tenant", "name", "code", "is_active", "created_at", "updated_at",
    ),
    "TenantMembership": (
        "tenant", "user", "is_tenant_admin", "is_active", "default_branch", "created_at",
    ),
    "BusinessPermission": (
        "module", "code", "name", "is_active", "created_at",
    ),
    "Role": (
        "tenant", "name", "code", "is_system", "approved_for_tenant_assignment",
        "is_active", "created_at",
    ),
    "RolePermission": ("role", "permission", "created_at"),
    "UserRole": (
        "tenant", "user", "role", "is_active", "valid_from", "valid_to", "assigned_by",
    ),
    "AuditLog": (
        "created_at", "tenant", "actor", "action", "resource_type", "resource_id", "ip_address",
    ),
    "TenantSubscription": (
        "tenant", "plan", "status", "current_period_start", "current_period_end",
        "cancel_at_period_end", "updated_at",
    ),
    "Invoice": (
        "number", "tenant", "status", "amount", "tax", "currency", "due_at", "paid_at", "created_at",
    ),
    "UsageCounter": (
        "tenant", "key", "value", "period_start", "period_end", "updated_at",
    ),
    "Company": ("tenant", "name", "gst_no", "created_at", "updated_at"),
    "Client": ("tenant", "first_name", "last_name", "company", "created_at", "updated_at"),
    "ClientEmail": ("tenant", "client", "email", "created_at"),
    "ClientPhone": ("tenant", "client", "phone", "created_at"),
    "Source": ("tenant", "name", "created_at", "updated_at"),
    "Product": ("tenant", "name", "created_at", "updated_at"),
    "Lead": (
        "tenant", "company", "client_detail", "source", "product", "is_converted",
        "assigned_users", "created_at", "updated_at",
    ),
    "Deal": (
        "id", "tenant", "deal_status", "company", "client_detail", "product",
        "assigned_users", "updated_by", "last_updated",
    ),
    "Description": ("tenant", "lead", "deal", "updated_by", "created_at", "updated_at"),
    "Notification": (
        "tenant", "type", "title", "is_sent", "send_at", "lead", "deal", "order", "created_at",
    ),
    "NotificationRecipient": (
        "tenant", "notification", "user", "is_read", "is_ready", "read_at", "ready_at", "created_at",
    ),
    "BaseProduct": (
        "tenant", "code", "name", "product_type", "default_height", "default_width",
        "default_depth", "per_bay_qty", "compartment",
    ),
    "Quotation": (
        "tenant", "quotation_no", "deal", "quotation_template", "sub_total", "gst",
        "grand_total", "created_by", "created_at",
    ),
    "QuotationProduct": ("tenant", "quotation", "name", "created_at", "updated_at"),
    "QuotationItem": (
        "tenant", "quotation_product", "item_name", "item_code", "height", "width", "depth",
        "quantity", "provided_rate", "market_rate",
    ),
    "QuotationWorking": (
        "tenant", "quotation_product", "total_weight", "provided_total_cost", "market_total_cost",
        "profit_percent", "discount", "set", "total_body",
    ),
    "Order": (
        "tenant", "order_number", "status", "deal", "quotation", "dispatch_at", "balance",
        "total_body", "powder_coating", "count_order", "created_at",
    ),
    "Advance": ("tenant", "order", "advance_amount", "advance_date", "created_at"),
    "ColourChange": ("tenant", "order", "colour", "user", "changed_on"),
    "Drawing": (
        "tenant", "title", "upload_type", "status", "deal", "order", "uploaded_by",
        "file_type", "file_size", "approved_at", "show_in_order", "created_at",
    ),
}


class SmartCoreAdmin(admin.ModelAdmin):
    """Useful default admin for every Core model."""

    list_per_page = 50
    save_on_top = True
    empty_value_display = "-"

    @admin.display(description="Assigned users")
    def assigned_users(self, obj):
        manager = getattr(obj, "assigned_to", None)
        if manager is None:
            return "-"
        values = []
        for user in manager.all()[:8]:
            values.append(getattr(user, "email", str(user)))
        if not values:
            return "-"
        suffix = " …" if manager.count() > len(values) else ""
        return ", ".join(values) + suffix

    def get_list_display(self, request):
        override = CORE_LIST_DISPLAY.get(self.model.__name__)
        if override:
            return override

        fields = []
        priority = (
            "id", "tenant", "name", "title", "code", "number", "email", "phone",
            "status", "is_active", "created_at", "updated_at",
        )
        concrete = {
            field.name: field
            for field in self.model._meta.get_fields()
            if getattr(field, "concrete", False) and not getattr(field, "many_to_many", False)
        }
        for name in priority:
            if name in concrete and name not in SENSITIVE_FIELD_NAMES and name not in fields:
                fields.append(name)
        for name, field in concrete.items():
            if len(fields) >= 10:
                break
            if name in fields or name in SENSITIVE_FIELD_NAMES:
                continue
            if isinstance(field, (models.TextField, models.JSONField, models.BinaryField, models.FileField)):
                continue
            fields.append(name)
        return tuple(fields or ["__str__"])

    def get_search_fields(self, request):
        result = []
        text_types = (models.CharField, models.TextField, models.EmailField, models.SlugField)

        for field in self.model._meta.get_fields():
            if len(result) >= 10:
                break
            if not getattr(field, "concrete", False) or getattr(field, "many_to_many", False):
                continue
            if field.name in SENSITIVE_FIELD_NAMES:
                continue
            if isinstance(field, text_types):
                result.append(field.name)
                continue
            if isinstance(field, (models.ForeignKey, models.OneToOneField)) and field.related_model:
                related_names = {f.name: f for f in field.related_model._meta.get_fields()}
                for candidate in ("email", "name", "title", "code", "number", "slug"):
                    related_field = related_names.get(candidate)
                    if isinstance(related_field, text_types):
                        result.append(f"{field.name}__{candidate}")
                        break
        return tuple(result)

    def get_list_filter(self, request):
        filters = []
        for field in self.model._meta.get_fields():
            if len(filters) >= 8:
                break
            if not getattr(field, "concrete", False) or getattr(field, "many_to_many", False):
                continue
            if field.name in SENSITIVE_FIELD_NAMES:
                continue
            if (
                isinstance(field, (models.BooleanField, models.DateField, models.DateTimeField))
                or bool(getattr(field, "choices", None))
                or isinstance(field, (models.ForeignKey, models.OneToOneField))
            ):
                filters.append(field.name)
        return tuple(filters)

    def get_readonly_fields(self, request, obj=None):
        readonly = []
        for field in self.model._meta.get_fields():
            if not getattr(field, "concrete", False):
                continue
            if field.name in SYSTEM_READONLY_FIELDS or field.name in SENSITIVE_FIELD_NAMES:
                readonly.append(field.name)
        return tuple(readonly)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        related = [
            field.name
            for field in self.model._meta.get_fields()
            if getattr(field, "concrete", False)
            and isinstance(field, (models.ForeignKey, models.OneToOneField))
        ]
        if related:
            qs = qs.select_related(*related)
        if any(field.name == "assigned_to" for field in self.model._meta.many_to_many):
            qs = qs.prefetch_related("assigned_to")
        return qs


@admin.register(User)
class MyraidUserAdmin(UserAdmin):
    model = User
    ordering = ("email",)
    list_per_page = 50
    save_on_top = True

    list_display = (
        "email",
        "first_name",
        "last_name",
        "phone",
        "department",
        "platform_admin",
        "is_staff",
        "is_superuser",
        "is_active",
        "last_login",
        "date_joined",
    )
    list_filter = (
        "department",
        "platform_admin",
        "is_staff",
        "is_superuser",
        "is_active",
        "date_joined",
        "last_login",
    )
    search_fields = (
        "email",
        "first_name",
        "last_name",
        "phone",
        "phone_e164",
        "quotation_code",
    )
    readonly_fields = ("phone_e164", "last_login", "date_joined")

    fieldsets = UserAdmin.fieldsets + (
        (
            "Myraid / CRM",
            {
                "fields": (
                    "phone",
                    "phone_e164",
                    "department",
                    "quotation_code",
                    "platform_admin",
                    "phone_verified_at",
                )
            },
        ),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "Myraid / CRM",
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "phone",
                    "department",
                    "platform_admin",
                    "is_staff",
                    "is_superuser",
                    "is_active",
                ),
            },
        ),
    )


# Register every concrete model in apps.core that does not already have a custom admin.
core_app = apps.get_app_config("core")
for model in core_app.get_models():
    if model is User or model in admin.site._registry:
        continue
    admin.site.register(model, SmartCoreAdmin)
