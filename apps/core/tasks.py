from celery import shared_task
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Notification, NotificationRecipient, Tenant, TenantSubscription


@shared_task
def dispatch_due_notifications():
    now = timezone.now()
    sent = 0
    with transaction.atomic():
        notifications = list(
            Notification.objects.select_for_update(skip_locked=True).filter(
                is_sent=False, send_at__isnull=False, send_at__lte=now,
                tenant__status__in=[Tenant.Status.TRIAL, Tenant.Status.ACTIVE],
            )[:500]
        )
        for notification in notifications:
            recipients = NotificationRecipient.objects.filter(
                notification=notification
            ).select_related("user")
            for recipient in recipients:
                if recipient.user.email:
                    send_mail(
                        notification.title,
                        notification.message or notification.title,
                        None,
                        [recipient.user.email],
                        fail_silently=True,
                    )
                recipient.is_ready = True
                recipient.ready_at = now
                recipient.save(update_fields=["is_ready", "ready_at"])
            notification.is_sent = True
            notification.save(update_fields=["is_sent"])
            sent += 1
    return sent


@shared_task
def enforce_subscription_statuses():
    now = timezone.now()
    expired = TenantSubscription.objects.filter(
        current_period_end__lt=now,
        status__in=[
            TenantSubscription.Status.ACTIVE,
            TenantSubscription.Status.PAST_DUE,
        ],
    ).select_related("tenant")
    count = 0
    for subscription in expired:
        subscription.status = TenantSubscription.Status.PAST_DUE
        subscription.save(update_fields=["status"])
        subscription.tenant.status = Tenant.Status.SUSPENDED
        subscription.tenant.save(update_fields=["status"])
        count += 1
    return count
