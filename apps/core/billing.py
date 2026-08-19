import hashlib
import hmac
import json

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import AuditLog, Invoice, Tenant, TenantSubscription


def verify_razorpay_webhook(raw_body, signature):
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise ValidationError("Razorpay webhook secret is not configured.")
    digest = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature or "")


@transaction.atomic
def process_razorpay_event(payload):
    event = payload.get("event", "")
    entity = (
        payload.get("payload", {})
        .get("subscription", {})
        .get("entity", {})
    )
    subscription_id = entity.get("id")
    subscription = TenantSubscription.objects.select_for_update().filter(
        razorpay_subscription_id=subscription_id
    ).first()
    if not subscription:
        return None
    states = {
        "subscription.activated": TenantSubscription.Status.ACTIVE,
        "subscription.authenticated": TenantSubscription.Status.AUTHENTICATED,
        "subscription.pending": TenantSubscription.Status.PAST_DUE,
        "subscription.halted": TenantSubscription.Status.PAST_DUE,
        "subscription.cancelled": TenantSubscription.Status.CANCELLED,
        "subscription.completed": TenantSubscription.Status.COMPLETED,
    }
    if event in states:
        subscription.status = states[event]
        subscription.save(update_fields=["status", "updated_at"])
        subscription.tenant.status = (
            Tenant.Status.ACTIVE
            if subscription.status == TenantSubscription.Status.ACTIVE
            else Tenant.Status.SUSPENDED
            if subscription.status == TenantSubscription.Status.PAST_DUE
            else Tenant.Status.CANCELLED
            if subscription.status == TenantSubscription.Status.CANCELLED
            else subscription.tenant.status
        )
        subscription.tenant.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        tenant=subscription.tenant,
        action=f"billing.{event}",
        resource_type="TenantSubscription",
        resource_id=str(subscription.pk),
        metadata={"razorpay_event_id": payload.get("id")},
    )
    return subscription
