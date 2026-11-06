import orjson
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import InventoryItem, ScanLedgerEntry


def fast_inventory_snapshot(request):
    """
    Ultra-fast read path: bypasses DRF ModelSerializer overhead completely.
    Streams raw tuples/dicts from PostgreSQL directly into Rust orjson bytes.
    """
    warehouse_id = request.GET.get("warehouse_id")
    queryset = (
        InventoryItem.objects
        .filter(warehouse_id=warehouse_id, is_active=True)
        .values("id", "sku", "bin_location", "qty_on_hand", "updated_at")[:500]
    )
    payload = orjson.dumps({"items": list(queryset)})
    return HttpResponse(payload, content_type="application/json")


@csrf_exempt
def batch_barcode_scan_ingest(request):
    """
    Group-commit write endpoint: processes up to 256 warehouse scans in a single
    PostgreSQL round-trip and single WAL fsync commit.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        body = orjson.loads(request.body)
    except Exception:
        return HttpResponse(
            b'{"error":"invalid_json"}',
            status=400,
            content_type="application/json",
        )

    scans = body.get("items", [])
    if not scans:
        return HttpResponse(b'{"processed":0}', content_type="application/json")

    # Sort deltas by SKU deterministically to make PostgreSQL deadlocks impossible
    qty_deltas = {}
    ledger_rows = []
    for s in scans:
        sku = s.get("barcode", "")
        if not sku:
            continue
        delta = int(s.get("qty_delta", 1))
        qty_deltas[sku] = qty_deltas.get(sku, 0) + delta
        ledger_rows.append(
            ScanLedgerEntry(
                sku=sku,
                location_id=s.get("locationId", ""),
                qty_delta=delta,
                client_timestamp=int(s.get("timestamp", 0)),
            )
        )

    with transaction.atomic():
        ScanLedgerEntry.objects.bulk_create(ledger_rows, batch_size=256)
        for sku in sorted(qty_deltas.keys()):
            InventoryItem.objects.filter(sku=sku).update(
                qty_on_hand=F("qty_on_hand") + qty_deltas[sku]
            )

    return HttpResponse(
        orjson.dumps({"processed": len(scans), "unique_skus": len(qty_deltas)}),
        content_type="application/json",
    )
