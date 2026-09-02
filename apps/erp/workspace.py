import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum, Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied

from apps.core.models import Company, Branch, AuditLog, UserRole, TenantMembership, Role, BusinessPermission, Tenant
from . import models as m, services as svc
from .catalog import FEATURES, price_quote, DOCUMENT_FEATURES, DOCUMENT_PERMISSION
from .security import context, features, authorize, scope, has_permission
from .money import calculate_line, ZERO


class WorkspaceAPI(APIView):
    permission_classes = [IsAuthenticated]
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        context(request)


def records(request, model, permission, feature="basic"):
    if feature not in features(request.tenant) or not has_permission(request, permission):
        return model.objects.none()
    qs = model.objects.filter(tenant=request.tenant, archived=False).filter(scope(request, permission))
    if request.branch: qs = qs.filter(Q(branch=request.branch) | Q(branch__isnull=True))
    return qs


def period(request):
    value = request.query_params.get("month", timezone.localdate().strftime("%Y-%m"))
    try:
        start = date.fromisoformat(value + "-01")
    except (ValueError, TypeError):
        raise ValidationError({"month": "Use YYYY-MM."})
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


class BootstrapView(WorkspaceAPI):
    def get(self, request):
        enabled = features(request.tenant)
        now = timezone.now()
        assignments = UserRole.objects.filter(tenant=request.tenant, user=request.user, role__is_active=True).filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now), Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        from apps.core.services import effective_permissions
        codes = list(effective_permissions(request.user,request.tenant,request.branch))
        settings, _ = m.ErpSettings.objects.get_or_create(tenant=request.tenant)
        branches = Branch.objects.filter(tenant=request.tenant, is_active=True)
        if not assignments.filter(branch__isnull=True).exists(): branches = branches.filter(pk__in=assignments.values("branch_id"))
        lookups = {}
        for key, model, permission, feature, field in [
            ("items", m.Item, "item.view", "basic", "name"), ("suppliers", m.Supplier, "supplier.view", "purchase", "name"),
            ("warehouses", m.Warehouse, "warehouse.view", "inventory", "name"), ("departments", m.Department, "employee.view", "hrms", "name"),
            ("cost-centers", m.CostCenter, "expense.view", "expense_management", "name"), ("expense-categories", m.ExpenseCategory, "expense.view", "expense_management", "name"),
            ("employees", m.Employee, "employee.view", "hrms", "name"), ("shifts", m.Shift, "attendance.view", "attendance_hr", "name"),
            ("leave-types", m.LeaveType, "leave.view", "attendance_hr", "name"), ("jobs", m.Job, "job.view", "work_orders", "name")]:
            lookups[key] = [{"id": str(x.pk), "label": getattr(x, field), **({"rate": str(x.sale_rate), "tax_rate": str(x.tax_rate), "unit": x.unit, "sku": x.sku} if key == "items" else {})} for x in records(request, model, permission, feature).order_by(field)[:300]]
        lookups["bins"] = [{"id":str(x.pk),"label":f"{x.warehouse.name} · {x.code}","warehouse":str(x.warehouse_id)} for x in records(request,m.WarehouseBin,"warehouse.view","inventory").select_related("warehouse").order_by("warehouse__name","code")[:500]]
        lookups["customers"] = [{"id": c.pk, "label": c.name} for c in Company.objects.filter(tenant=request.tenant).order_by("name")[:300]] if has_permission(request, "customer.view") else []
        lookups["users"] = [{"id": v.user_id, "label": v.user.get_full_name() or v.user.email} for v in TenantMembership.objects.filter(tenant=request.tenant, is_active=True).select_related("user")[:200]] if has_permission(request, "employee.view") or has_permission(request, "task.create") else []
        for kind in DOCUMENT_FEATURES:
            qs = records(request, m.Document, f"{DOCUMENT_PERMISSION[kind]}.view", DOCUMENT_FEATURES[kind]).filter(kind=kind)
            lookups[kind] = [{"id": str(d.pk), "label": f"{d.number} · {d.title or (d.customer.name if d.customer else d.supplier.name if d.supplier else '')}", "status": d.status} for d in qs.select_related("customer", "supplier")[:200]]
        return Response({
            "tenant": {"id": request.tenant.pk, "name": request.tenant.name, "slug": request.tenant.slug, "status": request.tenant.status},
            "active_branch": getattr(request.branch,"pk",None),
            "user": {"id": request.user.pk, "name": request.user.get_full_name(), "phone": request.user.phone_e164 or request.user.phone},
            "features": sorted(enabled), "permissions": sorted(set(codes)), "branches": list(branches.values("id", "name", "code")),
            "lookups": lookups, "settings": {"currency": settings.currency, "default_tax_rate": str(settings.default_tax_rate), "switches": settings.switches},
            "memberships": list(TenantMembership.objects.filter(user=request.user, is_active=True).values("tenant_id", "tenant__name", "is_tenant_admin")),
            "catalog": [{"key": key, "name": value[0], "monthly_price": value[1], "dependencies": value[2], "enabled": key in enabled, "available": key != "gst_integrations"} for key, value in FEATURES.items()],
            "is_platform_admin": bool(request.user.platform_admin),
            "custom_field_definitions": {
                entity:[{"key":x.definition.get("key"),"label":x.definition.get("label") or x.name,"type":x.definition.get("type"),"options":x.definition.get("options",[]),"required":bool(x.definition.get("required"))}
                        for x in m.Configuration.objects.filter(tenant=request.tenant,kind="custom_field",entity_type=entity,status="published",archived=False).order_by("created_at")]
                for entity in set(m.Configuration.objects.filter(tenant=request.tenant,kind="custom_field",status="published",archived=False).values_list("entity_type",flat=True))
            } if "custom_fields" in enabled else {},
        })


def profit_data(request):
    authorize(request, "profitability.view", feature="profitability")
    start, end = period(request)
    facts = records(request, m.ManagementFact, "profitability.view", "profitability").filter(date__range=(start, end))
    totals = {x["kind"]: x["total"] for x in facts.values("kind").annotate(total=Sum("amount"))}
    revenue, direct, opex = [totals.get(k, ZERO) for k in ("revenue", "direct", "opex")]
    gross, operating = revenue - direct, revenue - direct - opex
    warnings = []
    enabled = features(request.tenant)
    if "payroll" in enabled and not m.PayrollRun.objects.filter(tenant=request.tenant, month=start, status="finalized").exists(): warnings.append("Payroll is not finalized for this month.")
    pending_expenses = records(request, m.Expense, "expense.view", "expense_management").filter(date__range=(start, end), status__in=["draft", "pending_approval"]).count()
    if pending_expenses: warnings.append(f"{pending_expenses} expense drafts are not included in posted costs.")
    if "expense_management" not in enabled: warnings.append("Operating expense capture is not enabled; profit may be incomplete.")
    if "inventory" not in enabled: warnings.append("Inventory material costs are not being captured.")
    settings = m.ErpSettings.objects.filter(tenant=request.tenant).first()
    expected = settings.expected_expense_categories if settings else []
    categories = set(facts.values_list("category", flat=True))
    for category in expected:
        if category not in categories: warnings.append(f"Expected cost category is missing: {category}.")
    if not expected: warnings.append("Expected cost categories have not been configured.")
    monthly = []
    for offset in range(5, -1, -1):
        absolute = start.year * 12 + start.month - 1 - offset
        month = date(absolute // 12, absolute % 12 + 1, 1)
        row = {"month": month.strftime("%b"), "revenue": ZERO, "direct": ZERO, "opex": ZERO}
        for value in records(request, m.ManagementFact, "profitability.view", "profitability").filter(date__year=month.year, date__month=month.month).values("kind").annotate(total=Sum("amount")):
            row[value["kind"]] = value["total"]
        monthly.append(row)
    return {"revenue": revenue, "direct": direct, "gross_profit": gross, "opex": opex, "operating_profit": operating,
        "gross_margin": round(gross / revenue * 100, 2) if revenue > 0 else None,
        "operating_margin": round(operating / revenue * 100, 2) if revenue > 0 else None,
        "categories": list(facts.exclude(kind="revenue").values("category", "kind").annotate(total=Sum("amount")).order_by("-total")),
        "trend": monthly, "warnings": warnings, "completeness": "partial" if warnings else "complete",
        "basis": "Invoice-date revenue, issue-date direct costs and posted expenses/payroll. Management view, not statutory accounts.", "as_of": timezone.now(), "period": start.strftime("%B %Y")}


class DashboardView(WorkspaceAPI):
    def get(self, request):
        authorize(request, "workspace.view")
        start, end = period(request)
        jobs = records(request, m.Job, "job.view", "work_orders")
        employees = records(request, m.Employee, "employee.view", "hrms").exclude(status="exited")
        attendance = records(request, m.Attendance, "attendance.view", "attendance_hr").filter(date=timezone.localdate())
        invoices = records(request, m.Document, "invoice.view").filter(kind="invoice", status="posted")
        orders = records(request, m.Document, "order.view").filter(kind="sales_order")
        stocks = records(request, m.StockBalance, "stock.view", "inventory").select_related("item", "warehouse")
        low_stock = [s for s in stocks if s.bucket == "available" and s.on_hand - s.reserved <= s.item.reorder_level]
        approval_count = records(request, m.Approval, "approval.view", "approvals").filter(status="pending").count()
        profit = profit_data(request) if "profitability" in features(request.tenant) and has_permission(request, "profitability.view") else None
        outstanding = sum((d.gross - d.paid - (d.derived_documents.filter(kind="credit_note", status="posted").aggregate(v=Sum("gross"))["v"] or ZERO) for d in invoices), ZERO)
        alerts = []
        overdue = jobs.filter(due_date__lt=timezone.localdate()).exclude(status__in=["completed", "cancelled"]).count()
        if overdue: alerts.append({"title": f"{overdue} jobs need a little attention", "detail": "Past their promised completion date", "route": "/app/jobs", "tone": "amber"})
        if low_stock: alerts.append({"title": f"{len(low_stock)} items are running low", "detail": "Review availability before your next dispatch", "route": "/app/inventory", "tone": "amber"})
        if approval_count: alerts.append({"title": f"{approval_count} approvals are waiting", "detail": "Keep your team's work moving", "route": "/app/approvals", "tone": "green"})
        return Response({
            "period": start.strftime("%B %Y"), "today": timezone.localdate(), "profit": profit,
            "stats": {"order_value": orders.filter(date__range=(start, end)).aggregate(v=Sum("gross"))["v"] or ZERO,
                "outstanding": outstanding, "active_jobs": jobs.exclude(status__in=["completed", "cancelled"]).count(),
                "employees": employees.count(), "present": attendance.filter(status__in=["present", "half_day"]).count(),
                "low_stock": len(low_stock), "pending_approvals": approval_count},
            "orders": [{"id": str(o.pk), "number": o.number, "customer": o.customer.name if o.customer else "-", "date": o.date, "due_date": o.due_date, "gross": o.gross, "status": o.status} for o in orders.select_related("customer")[:5]],
            "jobs": [{"id": str(j.pk), "name": j.name, "number": j.number, "status": j.status, "due_date": j.due_date, "priority": j.priority,
                      "progress": round(sum(float(s.completed / s.planned) for s in j.stages.all() if s.planned) / max(1, j.stages.count()) * 100)} for j in jobs.exclude(status__in=["completed", "cancelled"]).prefetch_related("stages")[:5]],
            "alerts": alerts, "activity": list(AuditLog.objects.filter(tenant=request.tenant, actor=request.user).order_by("-created_at").values("id", "action", "resource_type", "created_at")[:6]),
        })


class ProfitabilityView(WorkspaceAPI):
    def get(self, request): return Response(profit_data(request))


class CalculationView(WorkspaceAPI):
    def post(self, request):
        kind = request.data.get("kind", "quotation")
        if kind not in DOCUMENT_FEATURES: raise ValidationError("Unknown document kind.")
        authorize(request, f"{DOCUMENT_PERMISSION[kind]}.create", feature=DOCUMENT_FEATURES[kind])
        if not isinstance(request.data.get("lines"), list) or not 1 <= len(request.data["lines"]) <= 500: raise ValidationError("Provide 1 to 500 lines.")
        lines = [calculate_line(row, request.data.get("tax_mode", "inclusive"), request.data.get("tax_jurisdiction", "intra")) for row in request.data["lines"]]
        return Response({"lines": lines, "gross": sum((r["gross"] for r in lines), ZERO), "taxable": sum((r["taxable"] for r in lines), ZERO), "tax": sum((r["tax"] for r in lines), ZERO)})


class SettingsView(WorkspaceAPI):
    def get(self, request):
        authorize(request, "settings.view")
        obj, _ = m.ErpSettings.objects.get_or_create(tenant=request.tenant)
        return Response({f.name: getattr(obj, f.name) for f in obj._meta.fields if f.name not in ("id", "tenant")})

    @transaction.atomic
    def patch(self, request):
        authorize(request, "settings.manage")
        obj, _ = m.ErpSettings.objects.select_for_update().get_or_create(tenant=request.tenant)
        svc.version_check(obj, request.data.get("version"))
        allowed = {"legal_name", "gstin", "address", "default_tax_rate", "switches", "numbering", "payroll_policy", "expected_expense_categories"}
        for key in allowed:
            if key in request.data: setattr(obj, key, request.data[key])
        obj.full_clean(); obj.version += 1; obj.save()
        from apps.core.services import audit
        audit(actor=request.user, tenant=request.tenant, action="erp.settings_changed", resource=obj, after={"version": obj.version})
        return self.get(request)


class EntitlementView(WorkspaceAPI):
    def post(self, request):
        authorize(request, "settings.view")
        return Response(price_quote(request.data.get("features", [])))

    def patch(self, request):
        if not request.user.platform_admin: raise PermissionDenied("Only the platform administrator can change purchased modules.")
        def execute():
            selected = request.data.get("features", [])
            result = price_quote(selected)
            reason = request.data.get("reason", "").strip()
            if not reason: raise ValidationError("A change reason is required.")
            before = sorted(features(request.tenant))
            for key in FEATURES:
                m.Entitlement.objects.update_or_create(tenant=request.tenant, feature=key,
                    defaults={"enabled": key in selected, "effective_at": timezone.now(), "reason": reason, "changed_by": request.user})
            from apps.core.services import audit
            audit(actor=request.user, tenant=request.tenant, action="erp.entitlements_changed", resource=request.tenant, before={"features": before}, after={"features": selected, "reason": reason})
            return result
        return Response(svc.command(request, "entitlements:update", execute))


class SearchView(WorkspaceAPI):
    def get(self, request):
        authorize(request, "workspace.view")
        query = request.query_params.get("q", "").strip()[:100]
        if len(query) < 2: return Response({"results": []})
        found = []
        for model, name, permission, feature, field, route in [
            (m.Item, "Item", "item.view", "basic", "name", "items"),
            (m.Job, "Job", "job.view", "work_orders", "name", "jobs"),
            (m.Supplier, "Supplier", "supplier.view", "purchase", "name", "suppliers"),
            (m.Employee, "Employee", "employee.view", "hrms", "name", "employees")]:
            for obj in records(request, model, permission, feature).filter(**{field+"__icontains": query})[:5]:
                found.append({"id": str(obj.pk), "label": getattr(obj, field), "type": name, "route": f"/app/{route}?record={obj.pk}"})
        for kind, route in [("sales_order", "sales-orders"), ("quotation", "quotations"), ("invoice", "invoices"), ("purchase_order", "purchase-orders"), ("dispatch", "dispatches")]:
            for obj in records(request, m.Document, f"{DOCUMENT_PERMISSION[kind]}.view", DOCUMENT_FEATURES[kind]).filter(kind=kind).filter(Q(number__icontains=query) | Q(title__icontains=query))[:4]:
                found.append({"id": str(obj.pk), "label": obj.number, "type": kind.replace("_", " "), "route": f"/app/{route}?record={obj.pk}"})
        return Response({"results": found[:25]})


class FactsView(WorkspaceAPI):
    def get(self, request):
        authorize(request, "profitability.view", feature="profitability")
        start, end = period(request)
        qs = records(request, m.ManagementFact, "profitability.view", "profitability").filter(date__range=(start,end))
        if request.query_params.get("kind"): qs = qs.filter(kind=request.query_params["kind"])
        rows = []
        for fact in qs[:500]:
            private = fact.source_type == "payrollresult" and not has_permission(request, "payroll.view")
            rows.append({"id": str(fact.pk), "description": fact.description, "kind": fact.kind, "amount": fact.amount, "date": fact.date, "category": fact.category,
                         "source_type": fact.source_type, "source_id": None if private else fact.source_id})
        return Response({"results": rows, "count": qs.count()})


class AgeingView(WorkspaceAPI):
    def get(self,request):
        party=request.query_params.get("party","customer")
        if party not in ("customer","supplier"):raise ValidationError("Choose customer or supplier ageing.")
        as_of=request.query_params.get("as_of",str(timezone.localdate()))
        try:as_of=date.fromisoformat(as_of)
        except ValueError:raise ValidationError({"as_of":"Use YYYY-MM-DD."})
        if party=="customer":
            authorize(request,"invoice.view")
            docs=records(request,m.Document,"invoice.view").filter(kind="invoice",status="posted",date__lte=as_of).select_related("customer")
        else:
            authorize(request,"payable.view",feature="purchase")
            docs=records(request,m.Document,"payable.view","purchase").filter(kind="supplier_bill",status="posted",date__lte=as_of).select_related("supplier")
        rows=[];buckets={"not_due":ZERO,"1_30":ZERO,"31_60":ZERO,"61_90":ZERO,"91_plus":ZERO}
        for doc in docs:
            allocations=m.PaymentAllocation.objects.filter(document=doc,payment__status="posted",payment__date__lte=as_of).aggregate(v=Sum("amount"))["v"] or ZERO
            credits=doc.derived_documents.filter(kind="credit_note",status="posted",date__lte=as_of).aggregate(v=Sum("gross"))["v"] or ZERO
            balance=max(ZERO,doc.gross-allocations-credits)
            if not balance:continue
            overdue=(as_of-(doc.due_date or doc.date)).days
            key="not_due" if overdue<=0 else "1_30" if overdue<=30 else "31_60" if overdue<=60 else "61_90" if overdue<=90 else "91_plus"
            buckets[key]+=balance
            rows.append({"id":str(doc.pk),"number":doc.number,"party":doc.customer.name if party=="customer" else doc.supplier.name,"date":doc.date,"due_date":doc.due_date,"days_overdue":max(0,overdue),"balance":balance,"bucket":key})
        return Response({"party":party,"as_of":as_of,"buckets":buckets,"total":sum(buckets.values(),ZERO),"results":rows})
