from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from apps.core.models import Tenant, User
from apps.erp.catalog import FEATURES
from apps.erp.models import Entitlement
from apps.erp.security import features
from apps.reference_erp.engine import COLLECTIONS
from apps.reference_erp.models import ERPRecord


class OwnerDemoTests(TestCase):
    def test_demo_is_populated_isolated_and_keeps_changes_on_repeat(self):
        untouched=Tenant.objects.create(name="Customer",slug="customer",status="active")
        call_command("seed_owner_demo",stdout=StringIO())
        tenant=Tenant.objects.get(slug="myraid-owner-demo")
        owner=User.objects.get(phone_e164="+919999900002")
        self.assertFalse(owner.platform_admin or owner.is_superuser)
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(set(ERPRecord.objects.filter(tenant=tenant).values_list("collection",flat=True)),set(COLLECTIONS)-{"users"})
        api=APIClient();api.force_authenticate(owner);api.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
        workspace=api.get("/api/v1/workspace-ui/workspace/")
        self.assertEqual(workspace.status_code,200,workspace.data)
        self.assertEqual(len(workspace.data["user"]["modules"]),11)
        self.assertEqual(len(workspace.data["data"]["users"]),1)
        record=ERPRecord.objects.filter(tenant=tenant,collection="tasks").first()
        record.payload["title"]="My test change";record.save()
        expiry=tenant.trial_ends_at;count=ERPRecord.objects.count()
        call_command("seed_owner_demo",stdout=StringIO())
        record.refresh_from_db();tenant.refresh_from_db()
        self.assertEqual(record.payload["title"],"My test change")
        self.assertEqual(tenant.trial_ends_at,expiry)
        self.assertEqual(ERPRecord.objects.count(),count)
        self.assertFalse(ERPRecord.objects.filter(tenant=untouched).exists())
        api.credentials(HTTP_X_TENANT_ID=str(untouched.pk))
        self.assertEqual(api.get("/api/v1/workspace-ui/workspace/").status_code,403)

    def test_never_reuses_an_existing_phone_or_real_company(self):
        User.objects.create_user(email="existing@test.invalid",phone="9999900002")
        with self.assertRaises(CommandError):call_command("seed_owner_demo",stdout=StringIO())
        self.assertFalse(Tenant.objects.filter(slug="myraid-owner-demo").exists())

    def test_trial_always_has_all_modules_and_expiry_keeps_billing_available(self):
        call_command("seed_owner_demo",stdout=StringIO())
        tenant=Tenant.objects.get(slug="myraid-owner-demo")
        Entitlement.objects.filter(tenant=tenant).update(enabled=False)
        self.assertEqual(features(tenant),set(FEATURES)-{"gst_integrations"}|{"basic"})
        tenant.trial_ends_at=timezone.now()-timedelta(seconds=1);tenant.save()
        self.assertEqual(features(tenant),{"basic"})
        owner=User.objects.get(phone_e164="+919999900002")
        api=APIClient();api.force_authenticate(owner);api.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
        self.assertEqual(api.get("/api/v1/workspace-ui/workspace/").status_code,403)
        self.assertEqual(api.get("/api/v1/workspace-ui/billing/").status_code,200)
