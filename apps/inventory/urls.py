from django.urls import path
from .views import batch_barcode_scan_ingest, fast_inventory_snapshot

app_name = "inventory"

urlpatterns = [
    path("snapshot/", fast_inventory_snapshot, name="fast_inventory_snapshot"),
    path("batch-scan/", batch_barcode_scan_ingest, name="batch_barcode_scan_ingest"),
]
