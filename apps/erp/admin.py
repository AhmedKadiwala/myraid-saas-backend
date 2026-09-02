from django.apps import apps
from django.contrib import admin
from django.db import models


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
    "processed_at",
    "sent_at",
    "consumed_at",
    "voided_at",
    "finalized_at",
    "posted_at",
}

ERP_LIST_DISPLAY = {
    "ErpSettings": ("tenant", "legal_name", "gstin", "currency", "default_tax_rate", "fiscal_year_start", "version"),
    "Entitlement": ("tenant", "feature", "enabled", "effective_at", "expires_at", "changed_by", "updated_at"),
    "NumberSeries": ("tenant", "kind", "year", "next_number"),
    "Warehouse": ("tenant", "code", "name", "branch", "archived", "created_at"),
    "WarehouseBin": ("tenant", "warehouse", "code", "rack", "pick_priority", "archived", "created_at"),
    "Item": ("tenant", "sku", "name", "item_type", "category", "unit", "sale_rate", "purchase_rate", "tax_rate", "reorder_level", "target_stock", "archived"),
    "Supplier": ("tenant", "name", "contact_name", "email", "phone", "gstin", "payment_terms", "archived", "created_at"),
    "Department": ("tenant", "name", "branch", "archived", "created_at"),
    "CostCenter": ("tenant", "code", "name", "branch", "archived", "created_at"),
    "Document": ("tenant", "kind", "number", "status", "customer", "supplier", "warehouse", "date", "due_date", "gross", "paid", "document_outstanding", "delivery_status"),
    "DocumentLine": ("tenant", "document", "position", "item", "description", "quantity", "rate", "discount", "tax_rate", "taxable", "tax", "gross"),
    "Job": ("tenant", "number", "name", "customer", "status", "priority", "due_date", "quantity", "completed_quantity", "department", "owner", "archived"),
    "JobStage": ("tenant", "job", "position", "name", "status", "owner", "planned", "completed", "rejected", "rework"),
    "StockBalance": ("tenant", "item", "warehouse", "bucket", "on_hand", "reserved", "available_stock", "value", "updated_at"),
    "StockMovement": ("tenant", "date", "kind", "item", "warehouse", "bucket", "quantity", "unit_cost", "value", "job", "transfer_id", "created_at"),
    "Reservation": ("tenant", "balance", "order", "job", "quantity", "consumed", "status", "created_at"),
    "ExpenseCategory": ("tenant", "name", "classification", "archived", "created_at"),
    "RecurringExpense": ("tenant", "name", "category", "amount", "frequency", "next_due", "cost_center", "active", "archived"),
    "Expense": ("tenant", "title", "category", "amount", "date", "due_date", "status", "supplier", "job", "cost_center", "created_at"),
    "ManagementFact": ("tenant", "date", "kind", "description", "amount", "category", "job", "customer", "cost_center", "source_type", "source_id"),
    "Payment": ("tenant", "direction", "customer", "supplier", "amount", "date", "mode", "account", "reference", "status", "voided_at"),
    "PaymentAllocation": ("tenant", "payment", "document", "amount", "created_at"),
    "Shift": ("tenant", "name", "start_time", "end_time", "grace_minutes", "break_minutes", "archived", "created_at"),
    "Employee": ("tenant", "code", "name", "designation", "department", "shift", "manager", "status", "joining_date", "exit_date", "monthly_salary", "archived"),
    "Attendance": ("tenant", "employee", "date", "status", "check_in", "check_out", "approved_ot_hours", "locked", "created_at"),
    "Holiday": ("tenant", "date", "name", "paid", "archived", "created_at"),
    "LeaveType": ("tenant", "name", "paid", "annual_allowance", "archived", "created_at"),
    "LeaveRequest": ("tenant", "employee", "leave_type", "start_date", "end_date", "half_day", "days", "status", "reviewed_by", "created_at"),
    "SalaryComponent": ("tenant", "employee", "name", "kind", "amount", "prorate", "effective_from", "effective_until", "archived"),
    "EmployeeLoan": ("tenant", "employee", "name", "principal", "recovered", "loan_outstanding", "monthly_recovery", "date", "status"),
    "PayrollRun": ("tenant", "name", "month", "status", "gross", "deductions", "net", "employer_cost", "finalized_at", "created_at"),
    "PayrollResult": ("tenant", "run", "employee", "payable_days", "gross", "deductions", "net", "paid", "payroll_outstanding", "employer_cost"),
    "SalaryPayment": ("tenant", "result", "amount", "date", "mode", "reference", "created_at"),
    "Task": ("tenant", "title", "owner", "due_date", "priority", "status", "job", "archived", "created_at"),
    "ApprovalRule": ("tenant", "name", "resource", "minimum_amount", "allow_self_approval", "archived", "created_at"),
    "Approval": ("tenant", "resource_type", "resource_id", "resource_version", "title", "amount", "current_step", "status", "allow_self", "created_at"),
    "ApprovalDecision": ("tenant", "approval", "step", "decision", "created_by", "created_at"),
    "Configuration": ("tenant", "kind", "name", "entity_type", "status", "version", "archived", "updated_at"),
    "Attachment": ("tenant", "resource_type", "resource_id", "name", "content_type", "size", "sensitivity", "created_by", "created_at"),
    "DataJob": ("tenant", "kind", "resource", "status", "processed", "checksum", "created_by", "created_at", "updated_at"),
    "PeriodLock": ("tenant", "start_date", "end_date", "created_by", "created_at"),
    "CommandReceipt": ("tenant", "actor", "key", "operation", "request_hash", "created_at"),
    "OutboxEvent": ("tenant", "event", "source_type", "source_id", "processed_at", "attempts", "created_at"),
    "CustomerProfile": ("tenant", "customer", "contact_name", "email", "phone", "payment_terms", "credit_limit", "archived", "updated_at"),
    "StockPosition": ("tenant", "balance", "bin", "quantity", "updated_at"),
    "PositionMovement": ("tenant", "balance", "bin", "movement", "quantity", "reason", "created_at"),
    "StockCount": ("tenant", "name", "warehouse", "status", "reason", "created_by", "created_at"),
    "StockCountLine": ("tenant", "count", "balance", "expected", "counted", "count_variance", "balance_version", "created_at"),
    "TaskComment": ("tenant", "task", "created_by", "created_at", "updated_at"),
    "RenderedDocument": ("tenant", "resource_type", "resource_id", "source_version", "template_version", "attachment", "created_at"),
    "ApiCredential": ("tenant", "name", "user", "prefix", "expires_at", "revoked_at", "created_at"),
    "WebhookEndpoint": ("tenant", "name", "url", "active", "created_by", "created_at", "updated_at"),
    "WebhookDelivery": ("tenant", "endpoint", "event", "status", "attempts", "next_attempt_at", "response_code", "created_at"),
    "Communication": ("tenant", "channel", "recipient", "subject", "document", "status", "provider_id", "sent_at", "created_at"),
    "ScheduledExecution": ("tenant", "configuration", "occurrence", "status", "attachment", "created_at", "updated_at"),
    "LoginOTP": ("user", "expires_at", "attempts", "consumed_at", "created_at"),
}


class SmartErpAdmin(admin.ModelAdmin):
    """Shared admin behavior for all ERP models."""

    list_per_page = 50
    save_on_top = True
    empty_value_display = "-"

    @admin.display(description="Outstanding")
    def document_outstanding(self, obj):
        return obj.gross - obj.paid

    @admin.display(description="Available")
    def available_stock(self, obj):
        return obj.on_hand - obj.reserved

    @admin.display(description="Outstanding")
    def loan_outstanding(self, obj):
        return obj.principal - obj.recovered

    @admin.display(description="Outstanding")
    def payroll_outstanding(self, obj):
        return obj.net - obj.paid

    @admin.display(description="Variance")
    def count_variance(self, obj):
        return obj.counted - obj.expected

    def get_list_display(self, request):
        override = ERP_LIST_DISPLAY.get(self.model.__name__)
        if override:
            return override

        fields = []
        priority = (
            "id", "tenant", "branch", "name", "title", "code", "number", "kind",
            "status", "date", "due_date", "amount", "archived", "created_at", "updated_at",
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
            if len(fields) >= 12:
                break
            if name in fields or name in SENSITIVE_FIELD_NAMES:
                continue
            if isinstance(field, (models.TextField, models.JSONField, models.BinaryField, models.FileField)):
                continue
            fields.append(name)
        return tuple(fields or ["__str__"])

    def get_search_fields(self, request):
        result = []
        text_types = (models.CharField, models.TextField, models.EmailField, models.SlugField, models.URLField)

        for field in self.model._meta.get_fields():
            if len(result) >= 12:
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
                for candidate in ("email", "name", "title", "code", "number", "sku", "slug"):
                    related_field = related_names.get(candidate)
                    if isinstance(related_field, text_types):
                        result.append(f"{field.name}__{candidate}")
                        break
        return tuple(result)

    def get_list_filter(self, request):
        filters = []
        preferred_names = {
            "tenant", "branch", "kind", "status", "direction", "item_type", "category",
            "classification", "priority", "date", "due_date", "archived", "active", "enabled",
        }
        all_fields = [
            field for field in self.model._meta.get_fields()
            if getattr(field, "concrete", False) and not getattr(field, "many_to_many", False)
        ]

        # Add the most useful business filters first.
        for field in all_fields:
            if field.name in preferred_names and field.name not in SENSITIVE_FIELD_NAMES:
                filters.append(field.name)
                if len(filters) >= 8:
                    return tuple(filters)

        for field in all_fields:
            if len(filters) >= 8:
                break
            if field.name in filters or field.name in SENSITIVE_FIELD_NAMES:
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
        return qs


# Register every concrete model in apps.erp. This includes any future ERP model too.
erp_app = apps.get_app_config("erp")
for model in erp_app.get_models():
    if model in admin.site._registry:
        continue
    admin.site.register(model, SmartErpAdmin)
