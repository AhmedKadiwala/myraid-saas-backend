"""Reference workspace storage using the existing SaaS identity and tenant boundary."""
from django.conf import settings
from django.db import models


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
