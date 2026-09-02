import csv
import hashlib
import io
import json
import urllib.request
import socket,ssl,hmac
from datetime import timedelta
from urllib.parse import urlparse
from pathlib import Path
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone
from celery import shared_task
from apps.core.models import TenantMembership
from apps.core.services import effective_permissions
from . import models as m
from .security import features


@shared_task
def process_erp_outbox(limit=100):
    handled=0
    ids=list(m.OutboxEvent.objects.filter(processed_at__isnull=True).order_by("created_at").values_list("id",flat=True)[:limit])
    for event_id in ids:
        with transaction.atomic():
            event=m.OutboxEvent.objects.select_for_update().filter(pk=event_id,processed_at__isnull=True).first()
            if not event:continue
            if event.event!="communication.queued":event.processed_at=timezone.now();event.save(update_fields=["processed_at"]);continue
            communication=m.Communication.objects.select_for_update().filter(pk=event.payload.get("communication_id"),status="queued").first()
            if not communication:event.processed_at=timezone.now();event.save(update_fields=["processed_at"]);continue
            communication.status="sending";communication.save(update_fields=["status"])
        try:
            body=communication.content
            if communication.document_id:
                doc=communication.document
                body+=f"\n\n{doc.kind.replace('_',' ').title()} {doc.number}\nFinal amount: INR {doc.gross:,.2f} (GST included: INR {doc.tax:,.2f})"
            if communication.channel=="email":
                message=EmailMessage(communication.subject,body,settings.DEFAULT_FROM_EMAIL,[communication.recipient])
                if communication.document_id:
                    from .documents import make_pdf
                    doc=communication.document
                    rows=[[x.description,str(x.quantity),x.unit,f"INR {x.rate:,.2f}",f"{x.tax_rate}%",f"INR {x.gross:,.2f}"] for x in doc.lines.all()]
                    pdf=make_pdf(doc.number,communication.tenant.name,[f"Date: {doc.date}",f"Party: {doc.customer.name if doc.customer else doc.supplier.name if doc.supplier else '-'}"],["Description","Qty","Unit","Rate","GST","Total"],rows,[f"Taxable INR {doc.taxable:,.2f}",f"GST included INR {doc.tax:,.2f}",f"Final INR {doc.gross:,.2f}"])
                    message.attach(f"{doc.number}.pdf",pdf,"application/pdf")
                message.send(fail_silently=False);provider_id="smtp"
            else:
                payload=json.dumps({"recipient":communication.recipient,"message":body,"client_reference":str(communication.pk)}).encode()
                request=urllib.request.Request(settings.WHATSAPP_API_URL,data=payload,method="POST",headers={"Authorization":f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}","Content-Type":"application/json","Idempotency-Key":str(communication.pk)})
                with urllib.request.urlopen(request,timeout=15) as response:provider_id=response.headers.get("X-Request-ID","")
            communication.status="sent";communication.sent_at=timezone.now();communication.provider_id=provider_id
        except Exception as exc:
            communication.status="unknown";communication.error=f"Delivery outcome needs review: {type(exc).__name__}"
        communication.save(update_fields=["status","sent_at","provider_id","error"])
        event.processed_at=timezone.now();event.attempts+=1;event.save(update_fields=["processed_at","attempts"]);handled+=1
    return handled


REPORTS={
    "sales_orders":(m.Document,"order.view","basic",{"kind":"sales_order"},["number","date","due_date","status","gross"]),
    "stock":(m.StockBalance,"stock.view","inventory",{},["item_id","warehouse_id","bucket","on_hand","reserved"]),
    "expenses":(m.Expense,"expense.view","expense_management",{},["title","date","status","amount","category_id"]),
    "payroll":(m.PayrollRun,"payroll.view","payroll",{},["name","month","status","gross","deductions","net"]),
    "profitability":(m.ManagementFact,"profitability.view","profitability",{},["date","kind","description","category","amount"]),
}


@shared_task
def run_erp_schedules():
    today=timezone.localdate();count=0
    for config in m.Configuration.objects.filter(kind="report_schedule",status="published",archived=False).select_related("created_by","tenant"):
        definition=config.definition;frequency=definition.get("frequency")
        due=frequency=="daily" or (frequency=="weekly" and today.weekday()==0) or (frequency=="monthly" and today.day==1)
        if not due:continue
        execution,created=m.ScheduledExecution.objects.get_or_create(tenant=config.tenant,configuration=config,occurrence=today,defaults={"branch":config.branch,"created_by":config.created_by})
        if not created:continue
        try:
            if config.created_by_id is None or not TenantMembership.objects.filter(tenant=config.tenant,user=config.created_by,is_active=True).exists():raise ValueError("Schedule owner is no longer active")
            report=definition.get("report");model,permission,feature,filters,fields=REPORTS[report]
            if feature not in features(config.tenant) or permission not in effective_permissions(config.created_by,config.tenant,config.branch):raise ValueError("Schedule owner no longer has current report access")
            qs=model.objects.filter(tenant=config.tenant,**filters)
            if config.branch_id and any(f.name=="branch" for f in model._meta.fields):qs=qs.filter(branch=config.branch)
            stream=io.StringIO();writer=csv.writer(stream);writer.writerow(fields)
            for row in qs.order_by("pk").values_list(*fields)[:10000]:writer.writerow(row)
            content=("\ufeff"+stream.getvalue()).encode("utf-8");key=f"{config.tenant_id}/scheduled/{config.id}-{today}.csv"
            root=Path(getattr(settings,"ERP_PRIVATE_MEDIA_ROOT",settings.BASE_DIR/"private-media"));target=root/key;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
            attachment=m.Attachment.objects.create(tenant=config.tenant,branch=config.branch,created_by=config.created_by,resource_type="configurations",resource_id=str(config.pk),name=f"{report}-{today}.csv",object_key=key,content_type="text/csv",size=len(content),checksum=hashlib.sha256(content).hexdigest())
            execution.attachment=attachment;execution.status="completed";execution.save(update_fields=["attachment","status"]);count+=1
            if definition.get("recipients") and settings.ERP_EMAIL_ENABLED:
                for recipient in definition["recipients"]:
                    comm=m.Communication.objects.create(tenant=config.tenant,branch=config.branch,created_by=config.created_by,channel="email",recipient=recipient,subject=f"Myraid scheduled report · {report}",content=f"Your authorized {report} report for {today} is ready in the ERP workspace.",consent_reference="Company-configured scheduled report",status="queued")
                    m.OutboxEvent.objects.create(tenant=config.tenant,branch=config.branch,created_by=config.created_by,event="communication.queued",source_type="communication",source_id=str(comm.pk),payload={"communication_id":str(comm.pk)})
        except Exception as exc:
            execution.status="failed";execution.error=str(exc)[:500];execution.save(update_fields=["status","error"])
    return count


@shared_task
def generate_due_recurring_expenses():
    count=0
    for template in m.RecurringExpense.objects.filter(active=True,archived=False,next_due__lte=timezone.localdate()).select_related("created_by"):
        if template.created_by_id:
            from .services import generate_recurring
            count+=len(generate_recurring(template,template.created_by))
    return count


@shared_task
def purge_expired_login_otps():
    cutoff=timezone.now()-timedelta(days=1)
    deleted,_=m.LoginOTP.objects.filter(expires_at__lt=cutoff).delete()
    return deleted


def secure_webhook_request(url,body,headers):
    from .api_keys import valid_webhook_url
    valid_webhook_url(url);parsed=urlparse(url);port=parsed.port or 443
    addresses=[item[4][0] for item in socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)]
    last=None
    for address in addresses:
        try:
            raw=socket.create_connection((address,port),timeout=15)
            sock=ssl.create_default_context().wrap_socket(raw,server_hostname=parsed.hostname)
            path=(parsed.path or "/")+(f"?{parsed.query}" if parsed.query else "")
            values={**headers,"Host":parsed.hostname,"Content-Length":str(len(body)),"Connection":"close"}
            request=f"POST {path} HTTP/1.1\r\n"+"".join(f"{k}: {v}\r\n" for k,v in values.items())+"\r\n"
            sock.sendall(request.encode()+body);response=sock.recv(1024);sock.close()
            code=int(response.split(b" ",2)[1])
            if not 200<=code<300:raise RuntimeError(f"HTTP {code}")
            return code
        except Exception as exc:last=exc
    raise last or RuntimeError("Webhook connection failed")


@shared_task
def deliver_erp_webhooks(limit=100):
    from .crypto import decrypt
    count=0
    ids=list(m.WebhookDelivery.objects.filter(status__in=["queued","retry"],next_attempt_at__lte=timezone.now()).order_by("created_at").values_list("id",flat=True)[:limit])
    for delivery_id in ids:
        with transaction.atomic():
            delivery=m.WebhookDelivery.objects.select_for_update().select_related("endpoint","event").filter(pk=delivery_id,status__in=["queued","retry"]).first()
            if not delivery:continue
            delivery.status="sending";delivery.attempts+=1;delivery.save(update_fields=["status","attempts"])
        event=delivery.event;payload=json.dumps({"event_id":str(event.pk),"event":event.event,"occurred_at":event.created_at.isoformat(),"source":{"type":event.source_type,"id":event.source_id},"data":event.payload},separators=(",",":"),sort_keys=True).encode()
        timestamp=str(int(timezone.now().timestamp()));signature=hmac.new(decrypt(delivery.endpoint.secret_ciphertext).encode(),timestamp.encode()+b"."+payload,hashlib.sha256).hexdigest()
        try:
            code=secure_webhook_request(delivery.endpoint.url,payload,{"Content-Type":"application/json","X-ERP-Event-ID":str(event.pk),"X-ERP-Timestamp":timestamp,"X-ERP-Signature":f"v1={signature}","Idempotency-Key":str(delivery.pk)})
            delivery.status="delivered";delivery.response_code=code;delivery.error=""
        except Exception as exc:
            delivery.error=f"{type(exc).__name__}: {str(exc)[:300]}"
            if delivery.attempts>=5:delivery.status="failed"
            else:
                delivery.status="retry";delivery.next_attempt_at=timezone.now()+timedelta(minutes=2**delivery.attempts)
        delivery.save(update_fields=["status","response_code","error","next_attempt_at"]);count+=1
    return count
