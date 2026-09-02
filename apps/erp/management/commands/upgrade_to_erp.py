"""Idempotent sales-data conversion; original CRM records remain immutable sources."""
from decimal import Decimal
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.utils import timezone
from apps.core.models import Tenant,Branch,Quotation,Order,TenantMembership
from apps.core.serializers import QuotationSerializer as LegacyQuotationSerializer
from apps.erp import models as m,services as svc
from apps.erp.onboarding import owner_role
from apps.erp.money import calculate_line,allocate_paise,money


class Command(BaseCommand):
    help="Convert a tenant's existing sales history into ERP documents. Dry-run unless --apply is given."
    def add_arguments(self,parser):
        parser.add_argument("--tenant",required=True)
        parser.add_argument("--apply",action="store_true")

    @transaction.atomic
    def handle(self,*args,**opts):
        tenant=Tenant.objects.filter(slug=opts["tenant"]).first()
        if not tenant:raise CommandError("Tenant not found.")
        quotes=Quotation.objects.filter(tenant=tenant).select_related("deal","deal__company").prefetch_related("quotation_products__quotation_item","quotation_products__quotation_working")
        orders=Order.objects.filter(tenant=tenant).select_related("quotation","deal")
        self.stdout.write(f"Found {quotes.count()} quotations and {orders.count()} orders. Existing rows and customer IDs will be retained.")
        if not opts["apply"]:
            self.stdout.write("Dry-run only. Review product quantities and commercial totals, then use --apply.");return
        branch=Branch.objects.filter(tenant=tenant,is_active=True).first()
        admins=TenantMembership.objects.filter(tenant=tenant,is_active=True,is_tenant_admin=True).select_related("user")
        if not admins:raise CommandError("An active tenant owner is required before upgrading.")
        actor=admins.first().user
        for membership in admins:owner_role(tenant,membership.user)
        m.ErpSettings.objects.get_or_create(tenant=tenant,defaults={"legal_name":tenant.name})
        converted=0
        for old in quotes:
            if m.Document.objects.filter(tenant=tenant,kind="quotation",crm_quotation=old).exists():continue
            if old.grand_total<0:raise CommandError(f"Negative quote total needs review: {old.quotation_no}")
            # Set-wise products keep their sold 'set' quantity. Item-wise keeps the printed item quantity.
            lines=[]
            for product in old.quotation_products.all():
                working=product.quotation_working.first()
                if old.quotation_template=="set_wise":
                    qty=Decimal(max(1,working.set if working else 1));weight=working.provided_total_cost if working else Decimal(1)
                    lines.append((product.name,qty,"set",max(Decimal(1),weight*qty)))
                else:
                    for item in product.quotation_item.all():
                        qty=Decimal(item.quantity)
                        if qty<=0:raise CommandError(f"Invalid printed quantity in {old.quotation_no}; correct before conversion.")
                        desc=f"{item.item_name} {item.item_code or ''}".strip()
                        lines.append((desc,qty,"pcs",max(Decimal(1),item.provided_rate*qty)))
            if not lines:lines=[(f"Existing quotation {old.quotation_no}",Decimal(1),"lot",Decimal(1))]
            # Allocate the already-agreed customer amount, never add GST a second time.
            weights=[int(money(x[3])*100) for x in lines]
            total=int(old.grand_total*100)
            if total>sum(weights):weights=[w*(total//sum(weights)+1) for w in weights]
            shares=allocate_paise(total,weights)
            doc=m.Document.objects.create(tenant=tenant,branch=branch,created_by=actor,kind="quotation",number=svc.number(tenant,"quotation"),
                crm_quotation=old,customer=old.deal.company,title=old.quotation_no,status="issued",date=timezone.localdate(old.created_at),
                notes=old.note or "",terms=old.terms_and_condition or "",snapshot={"legacy_id":old.pk,"legacy_number":old.quotation_no,"legacy_snapshot":svc.snapshot(LegacyQuotationSerializer(old).data),"migration_note":"Printed quantities retained; original final gross allocated across lines. Original quote snapshot retained for reconciliation."})
            for pos,((description,quantity,unit,_),share) in enumerate(zip(lines,shares)):
                gross=Decimal(share)/100;parts=calculate_line({"quantity":1,"rate":gross,"tax_rate":old.gst})
                parts.update(quantity=quantity,rate=gross/quantity)
                m.DocumentLine.objects.create(tenant=tenant,branch=branch,created_by=actor,document=doc,description=description[:500],unit=unit,position=pos,**parts)
            svc.total_document(doc);svc.record_event(doc,actor,"sales_history.converted_to_erp");converted+=1
        for old in orders:
            if m.Document.objects.filter(tenant=tenant,crm_order=old).exists():continue
            source=m.Document.objects.get(tenant=tenant,kind="quotation",crm_quotation=old.quotation)
            source.status="accepted";source.save(update_fields=["status"])
            doc=svc.convert_document(source,"sales_order",None,actor)
            doc.crm_order=old;doc.status="confirmed";doc.reference=old.po_number or "";doc.due_date=timezone.localdate(old.dispatch_at)
            doc.snapshot["legacy_order"]={"id":old.pk,"order_number":old.order_number,"status":old.status,"balance":old.balance,"advance_records":svc.snapshot(list(old.advance.values()))}
            doc.save();svc.record_event(doc,actor,"order_history.converted_to_erp")
        self.stdout.write(self.style.SUCCESS(f"ERP upgrade complete: {converted} new quotations converted. Existing commercial totals retained; advances kept as migration evidence, not falsely recorded as new receipts."))
