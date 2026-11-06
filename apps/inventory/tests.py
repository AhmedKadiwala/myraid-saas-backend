import asyncio
import json
from unittest.mock import MagicMock, patch
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.inventory.models import InventoryItem, ScanLedgerEntry
from apps.core.performance import OrjsonRenderer, AdaptiveBackpressureMiddleware

User = get_user_model()


class HighConcurrencyInventoryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="erp_operator",
            email="operator@test.local",
            phone="+919999999999",
            password="securePassword123!",
        )

    def test_fast_inventory_snapshot_returns_json_stream(self):
        # Create test items
        InventoryItem.objects.create(
            sku="SKU-1001",
            warehouse_id="WH-01",
            bin_location="BIN-A12",
            qty_on_hand=50,
            is_active=True,
        )
        InventoryItem.objects.create(
            sku="SKU-1002",
            warehouse_id="WH-01",
            bin_location="BIN-A13",
            qty_on_hand=25,
            is_active=True,
        )
        InventoryItem.objects.create(
            sku="SKU-9999",
            warehouse_id="WH-02",
            bin_location="BIN-B01",
            qty_on_hand=100,
            is_active=True,
        )

        response = self.client.get("/api/v1/inventory/snapshot/?warehouse_id=WH-01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        data = json.loads(response.content)
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 2)
        skus = [item["sku"] for item in data["items"]]
        self.assertIn("SKU-1001", skus)
        self.assertIn("SKU-1002", skus)
        self.assertNotIn("SKU-9999", skus)

    def test_batch_barcode_scan_ingest_group_commit_and_deadlock_prevention(self):
        # Create initial inventory items
        InventoryItem.objects.create(
            sku="SKU-A",
            warehouse_id="WH-01",
            bin_location="BIN-01",
            qty_on_hand=10,
        )
        InventoryItem.objects.create(
            sku="SKU-B",
            warehouse_id="WH-01",
            bin_location="BIN-02",
            qty_on_hand=20,
        )

        # Batch payload with multiple scans in arbitrary order
        batch_payload = {
            "items": [
                {"barcode": "SKU-B", "locationId": "BIN-02", "qty_delta": 5, "timestamp": 1700000000000},
                {"barcode": "SKU-A", "locationId": "BIN-01", "qty_delta": 2, "timestamp": 1700000000100},
                {"barcode": "SKU-B", "locationId": "BIN-02", "qty_delta": 3, "timestamp": 1700000000200},
            ]
        }

        response = self.client.post(
            "/api/v1/inventory/batch-scan/",
            data=json.dumps(batch_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        res_data = json.loads(response.content)
        self.assertEqual(res_data["processed"], 3)

        # Verify durable scan ledger entries were created
        self.assertEqual(ScanLedgerEntry.objects.count(), 3)

        # Verify inventory balances were atomically updated:
        # SKU-A had 10 + 2 = 12
        # SKU-B had 20 + 5 + 3 = 28
        item_a = InventoryItem.objects.get(sku="SKU-A")
        item_b = InventoryItem.objects.get(sku="SKU-B")
        self.assertEqual(item_a.qty_on_hand, 12)
        self.assertEqual(item_b.qty_on_hand, 28)

    def test_orjson_renderer(self):
        renderer = OrjsonRenderer()
        rendered = renderer.render({"message": "success", "count": 100})
        self.assertIsInstance(rendered, bytes)
        parsed = json.loads(rendered.decode("utf-8"))
        self.assertEqual(parsed["message"], "success")
        self.assertEqual(parsed["count"], 100)

    @patch("apps.core.storage_views.get_r2_client")
    def test_r2_presigned_upload_endpoint(self, mock_get_client):
        from rest_framework_simplejwt.tokens import RefreshToken
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://r2.example.com/presigned-put-url"
        mock_get_client.return_value = mock_s3

        token = str(RefreshToken.for_user(self.user).access_token)
        response = self.client.post(
            "/api/v1/storage/presign-upload/",
            data=json.dumps({
                "filename": "qa_inspection_batch_42.pdf",
                "content_type": "application/pdf",
                "category": "qa_photos"
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        res = response.json()
        self.assertEqual(res["upload_url"], "https://r2.example.com/presigned-put-url")
        self.assertTrue(res["object_key"].startswith("erp/qa_photos/"))
        self.assertTrue(res["object_key"].endswith("_qa_inspection_batch_42.pdf"))

    def test_adaptive_backpressure_middleware_429_on_saturation(self):
        async def dummy_app(request):
            await asyncio.sleep(0.1)
            from django.http import HttpResponse
            return HttpResponse(b"ok")

        middleware = AdaptiveBackpressureMiddleware(dummy_app)
        middleware.MAX_CONCURRENT_PER_WORKER = 1
        middleware.ACQUIRE_TIMEOUT_SEC = 0.010

        async def run_scenario():
            req = MagicMock()
            sem = middleware._get_semaphore()
            # Artificially exhaust semaphore
            await sem.acquire()
            try:
                # Next request should trigger backpressure TimeoutError and return 429
                resp = await middleware.__acall__(req)
                return resp
            finally:
                sem.release()

        resp = asyncio.run(run_scenario())
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "0.25")
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "server_busy")
        self.assertEqual(data["retry_ms"], 250)
