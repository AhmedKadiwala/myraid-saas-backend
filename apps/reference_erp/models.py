"""Reference workspace storage using the existing SaaS identity and tenant boundary."""
from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal


class CompanyInvitation(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE)
    phone = models.CharField(max_length=16, blank=True, default="")
    username = models.CharField(max_length=40, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    name = models.CharField(max_length=200, blank=True)
    role = models.ForeignKey("core.Role", on_delete=models.PROTECT)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, default="pending", choices=[(s, s.title()) for s in ("pending", "accepted", "revoked")])
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    communication = models.ForeignKey("erp.Communication", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name or self.phone} · {self.tenant.name} · {self.role.name}"


class OrderAttachment(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE)
    order_code = models.CharField(max_length=80)
    name = models.CharField(max_length=200)
    object_key = models.CharField(max_length=200)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.order_code} · {self.name} · {self.tenant.name}"


class WorkspaceSettings(models.Model):
    tenant = models.OneToOneField("core.Tenant", on_delete=models.CASCADE)
    company_meta = models.JSONField(default=dict)
    toggles = models.JSONField(default=dict)
    initialized = models.BooleanField(default=False)
    import_report = models.JSONField(default=dict)


class ERPRecord(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE)
    collection = models.CharField(max_length=32)
    code = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "collection", "code"], name="reference_tenant_record")]
        indexes = [models.Index(fields=["tenant", "collection"])]


class AuditEvent(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    message = models.CharField(max_length=350)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class CommandReceipt(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE, related_name="workspace_command_receipts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_command_receipts")
    request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    response_data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "user", "request_id"], name="reference_command_request")]


class TenantCharge(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="platform_charges")
    period = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    due_date = models.DateField()
    voided = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "period"], name="one_tenant_charge_per_month"),
                       models.CheckConstraint(condition=models.Q(amount__gt=0), name="platform_charge_positive")]


class TenantPayment(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="platform_payments")
    charge = models.ForeignKey(TenantCharge, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    date = models.DateField()
    mode = models.CharField(max_length=30)
    reference = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=300, blank=True)
    request_id = models.UUIDField(unique=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="platform_payment_positive"),
                       models.UniqueConstraint(fields=["reference"], condition=~models.Q(reference=""), name="platform_unique_payment_reference")]


class GeneratedDocument(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.CASCADE)
    document_type = models.CharField(max_length=50)
    document_code = models.CharField(max_length=80)
    filename = models.CharField(max_length=255)
    object_key = models.CharField(max_length=350)
    size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=100, default="application/pdf")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_deleted = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "document_type", "document_code"]),
            models.Index(fields=["expires_at", "is_deleted"]),
        ]

    def __str__(self):
        return f"{self.document_type}:{self.document_code} · {self.tenant.name} · {self.filename}"
