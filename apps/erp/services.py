import calendar
import hashlib
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError, APIException

from apps.core.models import Tenant
from apps.core.services import audit
from . import models as m
from .money import calculate_line, decimal, money, allocate_paise, ZERO


class Conflict(APIException):
    status_code = 409
    default_detail = "The record changed. Refresh it before continuing."
    default_code = "CONFLICT"


def snapshot(data):
    return json.loads(json.dumps(data, default=str))


def fingerprint(data):
    return hashlib.sha256(json.dumps(snapshot(data), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(request, operation, handler):
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 100:
        raise ValidationError({"idempotency_key": "A unique Idempotency-Key header is required."})
    digest = fingerprint(request.data)
    with transaction.atomic():
        # All posting commands take the same tenant fence before domain locks.
        Tenant.objects.select_for_update().get(pk=request.tenant.pk)
        existing = m.CommandReceipt.objects.filter(tenant=request.tenant, actor=request.user, key=key, operation=operation).first()
        if existing:
            if existing.request_hash != digest:
                raise Conflict("This action key was already used for different data.")
            return existing.result
        result = snapshot(handler())
        m.CommandReceipt.objects.create(tenant=request.tenant, actor=request.user, key=key, operation=operation, request_hash=digest, result=result)
        return result


def version_check(obj, expected):
    if expected is None:
        raise ValidationError({"version": "The current record version is required."})
    if str(obj.version) != str(expected):
        raise Conflict()


def touch(obj):
    obj.version += 1
    obj.save()


def record_event(obj, actor, event, before=None, after=None):
    audit(actor=actor, tenant=obj.tenant, action=event, resource=obj, before=snapshot(before), after=snapshot(after or {"status": getattr(obj, "status", "saved"), "version": obj.version}))
    m.OutboxEvent.objects.create(tenant=obj.tenant, branch=obj.branch, created_by=actor, event=event, source_type=obj._meta.model_name, source_id=str(obj.pk))


def open_period(tenant, business_date):
    if m.PeriodLock.objects.filter(tenant=tenant, archived=False, start_date__lte=business_date, end_date__gte=business_date).exists():
        raise ValidationError({"date": "This reporting period is closed. Use an authorized amendment in an open period."})


def number(tenant, kind):
    today = timezone.localdate()
    year = str(today.year if today.month >= 4 else today.year - 1)
    series, _ = m.NumberSeries.objects.select_for_update().get_or_create(tenant=tenant, kind=kind, year=year)
    value = series.next_number
    series.next_number += 1
    series.save(update_fields=["next_number"])
    prefix = {"quotation": "QT", "sales_order": "SO", "requisition": "PR", "purchase_order": "PO", "goods_receipt": "GRN", "dispatch": "DSP", "invoice": "INV", "supplier_bill": "BILL", "credit_note": "CN", "supplier_return": "RTN", "job": "JOB"}.get(kind, kind.upper()[:4])
    return f"{prefix}-{year[-2:]}-{value:04d}"


def fact(source, kind, amount, description, category="", job=None, customer=None, key=None, business_date=None, cost_center=None):
    return m.ManagementFact.objects.get_or_create(tenant=source.tenant,
        source_key=key or f"{source._meta.model_name}:{source.pk}:{kind}", defaults={
            "branch": source.branch, "created_by": source.created_by, "kind": kind,
            "source_type": source._meta.model_name, "source_id": str(source.pk),
            "description": description, "amount": money(amount), "date": business_date or getattr(source, "date", timezone.localdate()),
            "category": category, "job": job, "customer": customer, "cost_center": cost_center})[0]


def move_stock(*, tenant, actor, item, warehouse, quantity, kind, reason, unit_cost=None, bucket="available", job=None, line=None, transfer_id=None, business_date=None, bin_id=None):
    quantity = decimal(quantity, "quantity")
    if not quantity:
        raise ValidationError({"quantity": "Quantity cannot be zero."})
    if item.tenant_id != tenant.id or warehouse.tenant_id != tenant.id or item.archived or warehouse.archived:
        raise ValidationError("Choose an active item and warehouse in this company.")
    if item.item_type == "service":
        raise ValidationError({"item": "Services do not have physical stock."})
    open_period(tenant, business_date or timezone.localdate())
    balance, _ = m.StockBalance.objects.select_for_update().get_or_create(tenant=tenant, item=item, warehouse=warehouse, bucket=bucket, defaults={"branch": warehouse.branch})
    if quantity < 0:
        if balance.on_hand - balance.reserved < -quantity:
            raise Conflict(f"Only {balance.on_hand - balance.reserved:g} {item.unit} are available in {warehouse.name}.")
        cost = balance.value / balance.on_hand if balance.on_hand else ZERO
        value = -balance.value if -quantity == balance.on_hand else (quantity * cost).quantize(Decimal(".000001"), rounding=ROUND_HALF_UP)
    else:
        cost = decimal(unit_cost if unit_cost is not None else item.purchase_rate, "unit_cost", 0)
        value = (quantity * cost).quantize(Decimal(".000001"), rounding=ROUND_HALF_UP)
    old_quantity = balance.on_hand
    balance.on_hand += quantity
    balance.value += value
    touch(balance)
    movement = m.StockMovement.objects.create(tenant=tenant, branch=warehouse.branch, created_by=actor, item=item, warehouse=warehouse,
        kind=kind, bucket=bucket, quantity=quantity, value=value, unit_cost=cost, reason=reason,
        document_line=line, job=job, transfer_id=transfer_id, date=business_date or timezone.localdate())
    from .inventory import apply_position_change
    apply_position_change(balance, movement, old_quantity, bin_id)
    if quantity < 0 and kind in ("issue", "dispatch", "adjustment"):
        fact(movement, "direct" if job or kind == "dispatch" else "opex", -value, f"{item.name} · {reason}", "Materials" if job or kind == "dispatch" else "Stock adjustment", job=job)
    record_event(movement, actor, f"stock.{kind}")
    return movement


def total_document(doc):
    values = doc.lines.aggregate(taxable=Sum("taxable"), tax=Sum("tax"), gross=Sum("gross"))
    for key, value in values.items():
        setattr(doc, key, value or ZERO)
    doc.save(update_fields=["taxable", "tax", "gross", "updated_at"])


def source_remaining(line, target_kind, exclude=None):
    qs = m.DocumentLine.objects.filter(source_line=line, document__kind=target_kind).exclude(document__status__in=["cancelled", "void"])
    if exclude:
        qs = qs.exclude(document_id=exclude)
    return line.quantity - (qs.aggregate(qty=Sum("quantity"))["qty"] or ZERO)


def invoice_remaining(order_line, exclude=None):
    qs = m.DocumentLine.objects.filter(order_line=order_line, document__kind="invoice").exclude(document__status__in=["cancelled", "void"])
    if exclude:
        qs = qs.exclude(document_id=exclude)
    return order_line.quantity - (qs.aggregate(qty=Sum("quantity"))["qty"] or ZERO)


def convert_document(source, target, selections, actor, extra=None):
    allowed = {"quotation": ["sales_order"], "sales_order": ["dispatch", "invoice"], "requisition": ["purchase_order"], "purchase_order": ["goods_receipt", "supplier_bill"], "goods_receipt": ["supplier_bill", "supplier_return"], "dispatch": ["invoice"], "invoice": ["credit_note"]}
    if target not in allowed.get(source.kind, []):
        raise ValidationError({"target": "This conversion is not supported."})
    if source.status not in ("confirmed", "posted", "accepted", "issued", "partially_received", "received"):
        raise ValidationError("Confirm or post the source document before converting it.")
    if source.kind == "quotation" and source.status != "accepted":
        raise ValidationError("Record customer acceptance before converting this quotation.")
    if target == "sales_order" and source.derived_documents.filter(kind=target).exclude(status="cancelled").exists():
        raise Conflict("This quotation already has a sales order.")
    rows = list(source.lines.select_for_update().order_by("id"))
    selection = {str(x["id"]): decimal(x["quantity"], "quantity", "0.000001") for x in selections} if selections else {str(x.id): source_remaining(x, target) for x in rows}
    if set(selection) - {str(x.id) for x in rows}:
        raise ValidationError({"lines": "A selected line is not in this source document."})
    extra = extra or {}
    doc = m.Document.objects.create(tenant=source.tenant, branch=source.branch, created_by=actor, kind=target,
        number=number(source.tenant, target), customer=source.customer, supplier=source.supplier, warehouse=source.warehouse,
        source=source, date=timezone.localdate(), due_date=source.due_date, tax_mode=source.tax_mode,
        tax_jurisdiction=source.tax_jurisdiction, notes=extra.get("notes", ""), title=source.title,
        snapshot={"source_id": str(source.pk), "source_number": source.number, "source_version": source.version})
    for line in rows:
        qty = selection.get(str(line.id), ZERO)
        if qty <= 0:
            continue
        if qty > source_remaining(line, target):
            raise Conflict("The selected quantity was already allocated to another document.")
        original = line if source.kind == "sales_order" else line.order_line
        if target == "invoice" and original and qty > invoice_remaining(original):
            raise Conflict("These order quantities have already been invoiced.")
        previous = line.allocations.filter(document__kind=target).exclude(document__status__in=["cancelled", "void"])
        if target == "invoice" and original:
            previous = original.order_allocations.filter(document__kind="invoice").exclude(document__status__in=["cancelled", "void"])
        basis = original if target == "invoice" and original else line
        previous_values = previous.aggregate(quantity=Sum("quantity"), gross=Sum("gross"), taxable=Sum("taxable"), cgst=Sum("cgst"), sgst=Sum("sgst"), igst=Sum("igst"))
        gross = money(basis.gross * ((previous_values["quantity"] or ZERO) + qty) / basis.quantity) - (previous_values["gross"] or ZERO)
        remaining = [int((getattr(basis, f) - (previous_values[f] or ZERO)) * 100) for f in ("taxable", "cgst", "sgst", "igst")]
        parts = [Decimal(v) / 100 for v in allocate_paise(int(gross * 100), remaining)]
        m.DocumentLine.objects.create(tenant=source.tenant, branch=source.branch, document=doc, source_line=line, order_line=original,
            item=line.item, description=line.description, unit=line.unit, quantity=qty, rate=line.rate,
            tax_rate=line.tax_rate, gross=gross, taxable=parts[0], cgst=parts[1], sgst=parts[2], igst=parts[3], tax=sum(parts[1:]),
            accepted=qty if target == "goods_receipt" else ZERO, position=line.position)
    if not doc.lines.exists():
        raise ValidationError("No remaining quantities are available to convert.")
    total_document(doc)
    record_event(doc, actor, f"{target}.created_from_source")
    return doc


def approval_for(obj, actor, resource):
    from .security import features
    if "approvals" not in features(obj.tenant):
        return None
    amount = getattr(obj, "gross", getattr(obj, "amount", ZERO))
    rules = m.ApprovalRule.objects.filter(tenant=obj.tenant, resource=resource, archived=False, minimum_amount__lte=amount).order_by("-minimum_amount")
    rule = rules.first()
    if not rule:
        return None
    approval = m.Approval.objects.create(tenant=obj.tenant, branch=obj.branch, created_by=actor,
        resource_type=obj._meta.model_name, resource_id=obj.id, resource_version=obj.version + 1,
        title=getattr(obj, "number", getattr(obj, "title", getattr(obj, "name", resource))), amount=amount,
        steps=rule.required_roles, allow_self=rule.allow_self_approval)
    obj.status = "pending_approval"
    touch(obj)
    return approval


def post_document(doc, actor, enabled):
    if doc.status not in ("draft", "approved"):
        raise Conflict("Only a draft or approved document can be posted.")
    if not doc.lines.exists():
        raise ValidationError({"lines": "Add at least one line."})
    open_period(doc.tenant, doc.date)
    if doc.status == "draft" and approval_for(doc, actor, doc.kind):
        return doc
    if doc.kind in ("quotation", "sales_order", "invoice", "dispatch") and not doc.customer_id:
        raise ValidationError({"customer": "Select a customer."})
    if doc.kind in ("purchase_order", "supplier_bill") and not doc.supplier_id:
        raise ValidationError({"supplier": "Select a supplier."})
    if doc.kind == "goods_receipt":
        for line in doc.lines.select_related("item", "source_line"):
            if line.accepted + line.rejected + line.damaged != line.quantity:
                raise ValidationError({"lines": "Accepted + rejected + damaged must equal received quantity."})
            if "inventory" in enabled and line.item and line.item.item_type != "service":
                if not doc.warehouse:
                    raise ValidationError({"warehouse": "Select the receiving warehouse."})
                cost = line.taxable / line.quantity
                for bucket, qty in [("available", line.accepted), ("damaged", line.damaged)]:
                    if qty:
                        move_stock(tenant=doc.tenant, actor=actor, item=line.item, warehouse=doc.warehouse, quantity=qty,
                            unit_cost=cost, kind="receipt", bucket=bucket, line=line, reason=doc.number, business_date=doc.date)
        if doc.source and doc.source.kind == "purchase_order":
            po = m.Document.objects.select_for_update().get(pk=doc.source_id)
            po.status = "received" if all(source_remaining(l, "goods_receipt") <= 0 for l in po.lines.all()) else "partially_received"
            touch(po)
    elif doc.kind == "dispatch" and "inventory" in enabled:
        for line in doc.lines.select_related("item"):
            if line.item and line.item.item_type != "service":
                if not doc.warehouse:
                    raise ValidationError({"warehouse": "Choose the dispatch warehouse."})
                from .inventory import consume_order_reservations
                order = doc.source if doc.source and doc.source.kind == "sales_order" else None
                consume_order_reservations(doc.tenant, order, line.item, doc.warehouse, line.quantity)
                move_stock(tenant=doc.tenant, actor=actor, item=line.item, warehouse=doc.warehouse, quantity=-line.quantity,
                    kind="dispatch", line=line, reason=doc.number, business_date=doc.date)
    elif doc.kind == "invoice":
        if doc.tax > 0:
            settings = m.ErpSettings.objects.filter(tenant=doc.tenant).first()
            if not settings or not settings.gstin:
                raise ValidationError("Configure the issuing company's verified GST registration before posting a taxable invoice.")
        fact(doc, "revenue", doc.taxable, f"Invoice {doc.number}", customer=doc.customer)
    elif doc.kind == "credit_note":
        if not doc.source or doc.source.kind != "invoice":
            raise ValidationError("Credit notes must reference an invoice.")
        fact(doc, "revenue", -doc.taxable, f"Credit note {doc.number}", customer=doc.customer)
    elif doc.kind == "supplier_bill":
        if not doc.reference.strip():
            raise ValidationError({"reference": "Record the supplier's invoice number."})
        if m.Document.objects.filter(tenant=doc.tenant, kind="supplier_bill", supplier=doc.supplier, reference__iexact=doc.reference, date__year=doc.date.year, status="posted").exclude(pk=doc.pk).exists():
            raise Conflict("This supplier invoice number has already been posted in this year.")
        # Stock acquisition is not expensed again. Only explicit non-stock/service lines.
        direct = sum((l.taxable for l in doc.lines.select_related("item") if not doc.source_id or "inventory" not in enabled or not l.item or l.item.item_type == "service"), ZERO)
        if direct:
            fact(doc, "direct", direct, f"Direct purchase {doc.number}", "Direct purchases")
    elif doc.kind == "supplier_return":
        if not doc.source or doc.source.kind != "goods_receipt":
            raise ValidationError("A supplier return must reference a goods receipt.")
        if "inventory" in enabled:
            for line in doc.lines.select_related("item"):
                if line.item and line.item.item_type != "service":
                    move_stock(tenant=doc.tenant, actor=actor, item=line.item, warehouse=doc.warehouse, quantity=-line.quantity,
                        kind="return", line=line, reason=doc.number, business_date=doc.date)
    doc.status = {"quotation": "issued", "sales_order": "confirmed", "purchase_order": "issued", "requisition": "confirmed"}.get(doc.kind, "posted")
    doc.posted_at = timezone.now()
    doc.snapshot = {**doc.snapshot, "customer": doc.customer.name if doc.customer else None, "supplier": doc.supplier.name if doc.supplier else None,
                    "taxable": str(doc.taxable), "tax": str(doc.tax), "gross": str(doc.gross), "posted_version": doc.version + 1}
    touch(doc)
    record_event(doc, actor, f"{doc.kind}.posted")
    return doc


def post_expense(expense, actor):
    if expense.status not in ("draft", "approved"):
        raise Conflict("This expense is already posted or awaiting approval.")
    open_period(expense.tenant, expense.date)
    if expense.status == "draft" and approval_for(expense, actor, "expense"):
        return expense
    if expense.category.classification == "unclassified":
        raise ValidationError({"category": "Classify this cost as direct or operating before posting."})
    fact(expense, expense.category.classification, expense.amount, expense.title, expense.category.name, job=expense.job, cost_center=expense.cost_center)
    expense.status = "posted"
    touch(expense)
    record_event(expense, actor, "expense.posted")
    return expense


def generate_recurring(template, actor, until=None):
    until = until or timezone.localdate()
    generated = []
    while template.active and template.next_due <= until:
        if len(generated) >= 24:
            break
        expense, _ = m.Expense.objects.get_or_create(tenant=template.tenant, recurring_template=template, date=template.next_due,
            defaults={"branch": template.branch, "created_by": actor, "title": template.name, "amount": template.amount,
                      "due_date": template.next_due, "category": template.category, "cost_center": template.cost_center})
        generated.append(str(expense.pk))
        old = template.next_due
        if template.frequency == "weekly":
            template.next_due += timedelta(days=7)
        else:
            year, month = (old.year + 1, old.month) if template.frequency == "yearly" else (old.year + (old.month == 12), old.month % 12 + 1)
            template.next_due = date(year, month, min(template.anchor_day, calendar.monthrange(year, month)[1]))
    touch(template)
    record_event(template, actor, "expense.recurring_generated", after={"drafts": generated})
    return generated


def record_payment(tenant, actor, data):
    amount = money(decimal(data.get("amount"), minimum="0.01"))
    direction = data.get("direction", "receipt")
    if direction not in ("receipt", "payment"):
        raise ValidationError({"direction": "Choose receipt or payment."})
    party = "customer" if direction == "receipt" else "supplier"
    payment = m.Payment(tenant=tenant, created_by=actor, direction=direction, amount=amount,
        date=data.get("date", timezone.localdate()), mode=data.get("mode", "NEFT"), account=data.get("account", "Main bank"), reference=data.get("reference", ""), **{party + "_id": data.get(party)})
    payment.full_clean()
    if not getattr(payment, party + "_id"):
        raise ValidationError({party: "Choose a party."})
    open_period(tenant, payment.date)
    payment.save()
    allocated = ZERO
    seen = set()
    for allocation in sorted(data.get("allocations", []), key=lambda x: str(x["document"])):
        if str(allocation["document"]) in seen:
            raise ValidationError({"allocations": "Each invoice can appear once."})
        seen.add(str(allocation["document"]))
        doc = m.Document.objects.select_for_update().filter(tenant=tenant, pk=allocation["document"]).first()
        if not doc or doc.kind != ("invoice" if direction == "receipt" else "supplier_bill") or doc.status != "posted" or getattr(doc, party + "_id") != getattr(payment, party + "_id"):
            raise ValidationError({"allocations": "Choose a posted invoice for the same party."})
        part = money(decimal(allocation["amount"], minimum="0.01"))
        credits = doc.derived_documents.filter(kind="credit_note", status="posted").aggregate(v=Sum("gross"))["v"] or ZERO
        if part > doc.gross - credits - doc.paid or allocated + part > amount:
            raise Conflict("The allocation exceeds the unpaid invoice or available payment.")
        m.PaymentAllocation.objects.create(tenant=tenant, branch=doc.branch, created_by=actor, payment=payment, document=doc, amount=part)
        doc.paid += part
        touch(doc)
        allocated += part
    record_event(payment, actor, "payment.recorded")
    return payment
