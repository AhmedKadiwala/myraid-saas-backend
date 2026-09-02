from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import OutboxEvent,WebhookEndpoint,WebhookDelivery


@receiver(post_save,sender=OutboxEvent)
def create_webhook_deliveries(sender,instance,created,**kwargs):
    if not created:return
    for endpoint in WebhookEndpoint.objects.filter(tenant=instance.tenant,active=True,archived=False):
        if "*" in endpoint.events or instance.event in endpoint.events:
            WebhookDelivery.objects.get_or_create(tenant=instance.tenant,branch=instance.branch,created_by=instance.created_by,endpoint=endpoint,event=instance)
