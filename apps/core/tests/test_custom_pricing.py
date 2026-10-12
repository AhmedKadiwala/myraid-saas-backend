from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from apps.core.models import User, Tenant, TenantMembership, SubscriptionPlan
from apps.erp.onboarding import create_company_workspace


class CustomPricingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="trial@pricing.test", phone="9876500098", first_name="Owner")

    def test_new_company_receives_exactly_seven_days_without_a_fixed_quote(self):
        before = timezone.now()
        tenant, expiry, quote = create_company_workspace(self.user, "New Trial Company", ["purchase", "inventory"])
        after = timezone.now()
        self.assertGreaterEqual(expiry, before + timedelta(days=7))
        self.assertLessEqual(expiry, after + timedelta(days=7))
        self.assertIsNone(tenant.agreed_monthly_price)
        self.assertEqual(quote["trial_days"], 7)
        self.assertEqual(quote["pricing_mode"], "negotiated")
        self.assertIsNone(quote["monthly"])
        from apps.erp.models import Entitlement
        self.assertTrue(all(e.expires_at == expiry for e in Entitlement.objects.filter(tenant=tenant)))

    def test_same_plan_can_have_independent_customer_prices(self):
        plan = SubscriptionPlan.objects.create(name="Shared capabilities", code="full", price_monthly=9999)
        first = Tenant.objects.create(name="First Customer", slug="first-pricing", status="active", plan=plan, agreed_monthly_price=Decimal("35000.00"))
        second = Tenant.objects.create(name="Second Customer", slug="second-pricing", status="active", plan=plan, agreed_monthly_price=Decimal("18000.00"))
        for tenant in (first, second):
            TenantMembership.objects.create(tenant=tenant, user=self.user, is_tenant_admin=True)
        api = APIClient(); api.force_authenticate(self.user)
        api.credentials(HTTP_X_TENANT_ID=str(first.pk))
        self.assertEqual(api.get("/api/v1/workspace-ui/workspace/").data["user"]["price"], 35000)
        api.credentials(HTTP_X_TENANT_ID=str(second.pk))
        self.assertEqual(api.get("/api/v1/workspace-ui/workspace/").data["user"]["price"], 18000)
        plan.refresh_from_db()
        self.assertEqual(plan.price_monthly, 9999)

    def test_public_signup_announces_one_week_and_custom_pricing(self):
        response = APIClient().post("/api/v1/erp/onboard/", {
            "company_name": "One Week Company", "name": "New Owner", "phone": "9876500099", "features": []}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["trial_days"], 7)
        self.assertEqual(response.data["pricing_mode"], "negotiated")
