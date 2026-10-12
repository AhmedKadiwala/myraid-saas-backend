"""One-time tenant cutover. Preserve source rows and complete document lines."""
from collections import Counter
from django.db import transaction
from django.db.models import Sum
from django.core.serializers.json import DjangoJSONEncoder
import json

from apps.core.models import Company, Lead, Tenant, TenantMembership
from apps.erp import models as old
from .engine import COLLECTIONS, PREFIX
from .models import ERPRecord, WorkspaceSettings


def initialize(tenant):
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
        workspace, _ = WorkspaceSettings.objects.get_or_create(tenant=tenant)
        if workspace.initialized:
            return workspace
        if ERPRecord.objects.filter(tenant=tenant).exists():
            workspace.initialized = True
            workspace.save(update_fields=["initialized"])
            return workspace
        counts = Counter()
        identifiers = {}

        def rows(model):
            return model.objects.filter(tenant=tenant, archived=False).order_by("created_at", "pk")

        def code(collection, obj):
            key = (collection, str(obj.pk))
            if key not in identifiers:
                counts[collection] += 1
                identifiers[key] = f"{PREFIX[collection]}-{counts[collection]:03d}"
            return identifiers[key]

        def linked(collection, obj):
            return code(collection, obj) if obj else ""

        def add(collection, obj, payload):
            # Source metadata preserves traceability without modifying the source database rows.
            payload = {"id": code(collection, obj), **payload,
                       "source_id": str(obj.pk), "source_model": obj._meta.label}
            payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
            ERPRecord.objects.create(tenant=tenant, collection=collection, code=payload["id"], payload=payload)

        for customer in Company.objects.filter(tenant=tenant).order_by("pk"):
            contact = customer.client_details.first()
            add("customers", customer, {"name": customer.name,
                "contact": f"{contact.first_name} {contact.last_name or ''}".strip() if contact else "",
                "phone": (contact.phones.first().phone if contact and contact.phones.exists() else ""),
                "gst": customer.gst_no or "", "city": customer.address, "credit": 30})
        for lead in Lead.objects.filter(tenant=tenant).select_related("company", "client_detail", "product").order_by("pk"):
            add("leads", lead, {"name": lead.company.name,
                "contact": lead.client_detail.first_name, "requirement": lead.product.name,
                "value": 0, "stage": "Won" if lead.is_converted else "New", "follow": "",
                "owner": ", ".join(u.get_full_name() or u.email for u in lead.assigned_to.all())})
        for item in rows(old.Item):
            stock = old.StockBalance.objects.filter(tenant=tenant, item=item, archived=False).aggregate(total=Sum("on_hand"))["total"] or 0
            add("items", item, {"name": item.name, "sku": item.sku, "category": item.category or item.item_type.replace("_", " ").title(),
                "stock": float(stock), "min": float(item.reorder_level), "unit": item.unit,
                "rate": float(item.sale_rate), "cost": float(item.purchase_rate), "hsn": item.hsn_sac})
        for supplier in rows(old.Supplier):
            add("suppliers", supplier, {"name": supplier.name, "city": supplier.address, "phone": supplier.phone})
        document_collections = {"quotation": "quotes", "sales_order": "orders", "purchase_order": "purchases", "dispatch": "dispatches", "invoice": "invoices"}
        documents = list(rows(old.Document).select_related("customer", "supplier", "source").prefetch_related("lines"))
        for doc in documents:
            if doc.kind in document_collections:
                code(document_collections[doc.kind], doc)
        for doc in documents:
            collection = document_collections.get(doc.kind)
            if not collection:
                continue
            lines = list(doc.lines.filter(archived=False).order_by("position"))
            first = lines[0] if lines else None
            source_col = document_collections.get(doc.source.kind) if doc.source else None
            source = linked(source_col, doc.source) if source_col else ""
            common = {"source_number": doc.number, "amount": float(doc.gross), "gst": float(first.tax_rate) if first else 0,
                      "taxable": float(doc.taxable), "gst_amount": float(doc.tax),
                      "date": doc.date.isoformat(), "due": (doc.due_date or doc.date).isoformat(),
                      "customer": linked("customers", doc.customer), "item": linked("items", first.item) if first else "",
                      "qty": float(first.quantity) if first else 0,
                      "source_lines": [{"description": line.description, "item": linked("items", line.item),
                                        "qty": float(line.quantity), "rate": float(line.rate), "gst": float(line.tax_rate),
                                        "amount": float(line.gross)} for line in lines],
                      "read_only": len(lines) != 1 or (first is not None and first.quantity != int(first.quantity))}
            if collection == "quotes":
                common.update(created=doc.date.isoformat(), status={"draft": "Draft", "posted": "Sent", "approved": "Accepted"}.get(doc.status, doc.status.title()))
            elif collection == "orders":
                common.update(quote=source if source_col == "quotes" else "", status={"draft": "Confirmed", "posted": "Confirmed", "completed": "Completed", "cancelled": "Cancelled"}.get(doc.status, "Confirmed"))
            elif collection == "purchases":
                received = float(first.accepted) if first else 0
                common.update(vendor=linked("suppliers", doc.supplier), received=received,
                              unitCost=float(first.rate) if first else 0,
                              status="Received" if first and received >= float(first.quantity) else "Partial" if received else "Pending")
            elif collection == "dispatches":
                common.update(order=source if source_col == "orders" else "", carrier=doc.transporter,
                              tracking=doc.lr_number, status=doc.delivery_status.title())
            elif collection == "invoices":
                common.update(order=source if source_col == "orders" else "")
            add(collection, doc, common)
        for payment in rows(old.Payment).filter(direction="receipt", status="posted").prefetch_related("allocations__document"):
            for allocation in payment.allocations.filter(archived=False, document__kind="invoice"):
                add("payments", allocation, {"invoice": linked("invoices", allocation.document),
                    "amount": float(allocation.amount), "date": payment.date.isoformat(), "mode": payment.mode, "ref": payment.reference})
        for job in rows(old.Job).select_related("source_order", "owner"):
            qty = float(job.quantity)
            add("jobs", job, {"order": linked("orders", job.source_order), "stage": "Production",
                "status": {"pending": "Pending", "completed": "Completed"}.get(job.status, "In Progress"),
                "progress": min(100, round(float(job.completed_quantity) / qty * 100)) if qty else 0,
                "assignee": job.owner.get_full_name() if job.owner else "", "priority": job.priority.title(),
                "due": job.due_date.isoformat()})
        for employee in rows(old.Employee).select_related("department"):
            add("employees", employee, {"name": employee.name, "phone": employee.phone, "salary": float(employee.monthly_salary),
                "department": employee.department.name if employee.department else "", "role": employee.designation, "status": employee.status.title()})
        for attendance in rows(old.Attendance).select_related("employee"):
            add("attendance", attendance, {"employee": linked("employees", attendance.employee),
                "date": attendance.date.isoformat(), "status": attendance.status.replace("_", " ").title()})
        for payroll in rows(old.PayrollRun):
            add("payroll", payroll, {"period": payroll.month.strftime("%b %Y"), "amount": float(payroll.net),
                "status": payroll.status.title(), "read_only": True})
        for expense in rows(old.Expense).select_related("category"):
            add("expenses", expense, {"name": expense.title, "category": expense.category.name,
                "amount": float(expense.amount), "date": expense.date.isoformat(), "recurring": "Monthly" if expense.recurring_template_id else "No"})
        for task in rows(old.Task).select_related("owner"):
            add("tasks", task, {"title": task.title, "owner": task.owner.get_full_name() if task.owner else "",
                "due": task.due_date.isoformat() if task.due_date else "", "status": "Completed" if task.status in ("done", "completed") else "Pending"})
        for approval in rows(old.Approval):
            add("approvals", approval, {"title": approval.title, "type": approval.resource_type.title(),
                "amount": float(approval.amount), "status": approval.status.title(), "by": "", "read_only": True})
        for movement in rows(old.StockMovement).select_related("item"):
            add("stockMoves", movement, {"item": linked("items", movement.item), "delta": float(movement.quantity),
                "reason": movement.reason, "date": movement.date.isoformat()})
        configuration = old.ErpSettings.objects.filter(tenant=tenant).first()
        workspace.company_meta = {"name": tenant.name, "industry": "", "location": configuration.address if configuration else "",
                                  "gst": configuration.gstin if configuration else "", "branch": "Company workspace"}
        workspace.toggles = configuration.switches if configuration else {}
        workspace.import_report = {"collections": dict(counts), "source": "existing ERP", "mode": "one-time copy; original rows retained",
                                   "read_only_documents": ERPRecord.objects.filter(tenant=tenant, payload__read_only=True).count()}
        workspace.initialized = True
        workspace.save()
        return workspace
