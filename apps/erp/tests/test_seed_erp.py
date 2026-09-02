from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.core.models import AuditLog, Company, Tenant
from apps.erp import models as erp_models


class SeedErpTestCase(TestCase):
    @override_settings(DEBUG=True)
    def test_seed_erp_creates_realistic_connected_demo_data(self):
        call_command("seed_erp", password="ChangeMe123!", verbosity=0)

        tenant = Tenant.objects.get(slug="myraid-erp-demo")
        self.assertEqual(Company.objects.filter(tenant=tenant).count(), 7)
        self.assertEqual(erp_models.CustomerProfile.objects.filter(tenant=tenant).count(), 7)
        self.assertGreaterEqual(erp_models.Document.objects.filter(tenant=tenant, kind="invoice", status="posted").count(), 18)
        self.assertTrue(erp_models.PayrollRun.objects.filter(tenant=tenant, status="finalized").exists())
        self.assertTrue(erp_models.ManagementFact.objects.filter(tenant=tenant, kind="revenue").exists())
        self.assertTrue(erp_models.ManagementFact.objects.filter(tenant=tenant, kind="direct").exists())
        self.assertTrue(erp_models.ManagementFact.objects.filter(tenant=tenant, kind="opex").exists())
        self.assertTrue(AuditLog.objects.filter(tenant=tenant).exists())
