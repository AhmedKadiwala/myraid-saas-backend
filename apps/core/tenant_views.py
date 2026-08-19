import json

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .api import APIView
from .billing import process_razorpay_event, verify_razorpay_webhook
from .models import (
    Branch,
    Invoice,
    SubscriptionPlan,
    TenantMembership,
    TenantSettings,
    TenantSubscription,
)
from .serializers import (
    BranchSerializer,
    InvoiceSerializer,
    SubscriptionPlanSerializer,
    TenantSerializer,
    TenantSettingsSerializer,
    TenantSubscriptionSerializer,
)
from .services import (
    audit,
    enforce_tenant_admin,
    ensure_plan_limit,
    resolve_tenant,
)


class CurrentTenantView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        return Response({"tenant": TenantSerializer(tenant).data})


class TenantSettingsView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        settings_obj, _ = TenantSettings.objects.get_or_create(tenant=tenant)
        return Response(TenantSettingsSerializer(settings_obj).data)

    def put(self, request):
        tenant = enforce_tenant_admin(request)
        settings_obj, _ = TenantSettings.objects.get_or_create(tenant=tenant)
        serializer = TenantSettingsSerializer(
            settings_obj, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        audit(
            actor=request.user, tenant=tenant, action="tenant.settings.updated",
            resource=settings_obj, after=serializer.data, request=request,
        )
        return Response(serializer.data)


class BranchListCreateView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        branches = Branch.objects.filter(tenant=tenant, is_active=True)
        return Response({"branches": BranchSerializer(branches, many=True).data})

    def post(self, request):
        tenant = enforce_tenant_admin(request)
        ensure_plan_limit(
            tenant, "branches", Branch.objects.filter(tenant=tenant, is_active=True).count()
        )
        serializer = BranchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        branch = serializer.save(tenant=tenant)
        audit(
            actor=request.user, tenant=tenant, action="branch.created",
            resource=branch, after=serializer.data, request=request,
        )
        return Response(serializer.data, status=201)


class PlanListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        plans = SubscriptionPlan.objects.filter(is_active=True)
        return Response({"plans": SubscriptionPlanSerializer(plans, many=True).data})


class SubscriptionView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        subscription = TenantSubscription.objects.filter(tenant=tenant).first()
        return Response({
            "subscription": (
                TenantSubscriptionSerializer(subscription).data if subscription else None
            )
        })

    def post(self, request):
        tenant = enforce_tenant_admin(request)
        if not settings.RAZORPAY_KEY_ID:
            return Response(
                {"message": "Razorpay is not configured; set test credentials."},
                status=503,
            )
        plan = SubscriptionPlan.objects.filter(
            pk=request.data.get("plan"), is_active=True
        ).first()
        if not plan:
            return Response({"message": "Plan not found"}, status=404)
        # Network creation is deliberately isolated. In production, the returned
        # external subscription ID is stored only after Razorpay accepts it.
        import razorpay
        client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        external = client.subscription.create({
            "plan_id": request.data["razorpay_plan_id"],
            "total_count": int(request.data.get("total_count", 12)),
            "customer_notify": 1,
        })
        subscription, _ = TenantSubscription.objects.update_or_create(
            tenant=tenant,
            defaults={
                "plan": plan,
                "status": TenantSubscription.Status.CREATED,
                "razorpay_subscription_id": external["id"],
            },
        )
        return Response(TenantSubscriptionSerializer(subscription).data, status=201)

    def delete(self, request):
        tenant = enforce_tenant_admin(request)
        subscription = TenantSubscription.objects.filter(tenant=tenant).first()
        if not subscription:
            return Response({"message": "Subscription not found"}, status=404)
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        return Response({"message": "Cancellation scheduled at period end"})


class InvoiceListView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        invoices = Invoice.objects.filter(tenant=tenant).order_by("-created_at")
        return Response({"invoices": InvoiceSerializer(invoices, many=True).data})


class RazorpayWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        raw = request.body
        signature = request.headers.get("X-Razorpay-Signature")
        if not verify_razorpay_webhook(raw, signature):
            return Response({"message": "Invalid signature"}, status=400)
        process_razorpay_event(json.loads(raw))
        return Response({"message": "Webhook accepted"})
