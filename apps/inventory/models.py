import uuid
from django.db import models


class InventoryItem(models.Model):
    """
    High-speed inventory stock snapshot model.
    Hot read path queries warehouse_id + sku without loading full ORM models.
    """

    sku = models.CharField(max_length=100, db_index=True)
    warehouse_id = models.CharField(max_length=64, db_index=True)
    bin_location = models.CharField(max_length=64, blank=True, default="")
    qty_on_hand = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory_item"
        indexes = [
            models.Index(
                fields=["warehouse_id", "sku"],
                include=["bin_location", "qty_on_hand", "updated_at"],
                condition=models.Q(is_active=True),
                name="idx_inv_wh_sku",
            ),
        ]

    def __str__(self):
        return f"{self.sku} ({self.warehouse_id}): {self.qty_on_hand}"


class ScanLedgerEntry(models.Model):
    """
    Durable append-only audit trail of shop-floor scans.
    Written via group-commit bulk_create batches.
    """

    sku = models.CharField(max_length=100, db_index=True)
    location_id = models.CharField(max_length=64, blank=True, default="")
    qty_delta = models.IntegerField(default=1)
    client_timestamp = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scan_ledger_entry"

    def __str__(self):
        return f"{self.sku} @ {self.location_id} (delta: {self.qty_delta})"


class ErpStagingScan(models.Model):
    """
    Step 5.3: Ephemeral IoT / scanner staging record for raw PLC machine heartbeats
    or pre-validation staging. Configured as UNLOGGED in PostgreSQL to bypass the WAL
    and achieve 3x to 5x higher write throughput.
    """

    scan_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_id = models.CharField(max_length=64, blank=True, default="")
    barcode = models.CharField(max_length=100)
    raw_payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "erp_staging_scans"

    def __str__(self):
        return f"{self.scan_id} - {self.barcode}"
