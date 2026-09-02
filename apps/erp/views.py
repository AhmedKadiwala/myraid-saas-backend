import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction, IntegrityError
from django.db.models import Q, Sum, F
from django.core.exceptions import ValidationError as ModelValidationError
from django.utils import timezone
from rest_framework import viewsets, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from apps.core.models import Branch, Company, Tenant, TenantMembership, Role, RolePermission, BusinessPermission, UserRole
from . import models as m, serializers as s, services as svc, workforce
from .catalog import DOCUMENT_FEATURES, DOCUMENT_PERMISSION, FEATURES, price_quote
from .security import context, authorize, scope, features, require_feature, has_permission, assignments
from .money import decimal, money, ZERO


class Page(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 200


class ERPViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = Page
    feature = "basic"
    permission_prefix = "workspace"
    action_permissions = {}
    search_fields = ("name", "title", "code", "number", "sku")
    immutable_statuses = ("posted", "finalized", "issued", "confirmed", "approved", "pending_approval", "accepted")

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        context(request)
        require_feature(request.tenant, self.required_feature())
        scope(request, self.permission_code())

    def required_feature(self):
        return self.feature

    def permission_code(self):
        verb = self.action_permissions.get(self.action) or {"list": "view", "retrieve": "view", "create": "create", "update": "edit", "partial_update": "edit", "destroy": "edit"}.get(self.action, self.action)
        return f"{self.permission_prefix}.{verb}"

    def get_queryset(self):
        qs = self.queryset.filter(tenant=self.request.tenant)
        if any(f.name == "branch" for f in qs.model._meta.fields):
            qs = qs.filter(scope(self.request, self.permission_code()))
            if self.request.branch: qs = qs.filter(Q(branch=self.request.branch) | Q(branch__isnull=True))
        fields = {f.name for f in qs.model._meta.fields}
        if "archived" in fields: qs = qs.filter(archived=False)
        query = self.request.query_params.get("search", "").strip()[:200]
        if query:
            expression = Q()
            for f in self.search_fields:
                if f.split("__")[0] in fields: expression |= Q(**{f + "__icontains": query})
            qs = qs.filter(expression)
        for key in ("status", "kind", "employee", "warehouse", "category", "department", "job", "supplier", "customer", "date", "month"):
            if key in fields and self.request.query_params.get(key):
                qs = qs.filter(**{key: self.request.query_params[key]})
        return qs

    def checked_branch(self, serializer):
        branch = serializer.validated_data.get("branch") or self.request.branch
        if not branch:
            branch = Branch.objects.filter(tenant=self.request.tenant, is_active=True).filter(
                Q(pk__in=assignments(self.request, self.permission_code()).values("branch_id"))
                if not assignments(self.request, self.permission_code()).filter(branch__isnull=True).exists() else Q()
            ).first()
        if not assignments(self.request, self.permission_code()).filter(Q(branch__isnull=True) | Q(branch=branch)).exists():
            raise PermissionDenied("You cannot create records in this branch.")
        return branch

    def perform_create(self, serializer):
        values = {"tenant": self.request.tenant}
        if issubclass(serializer.Meta.model, m.Record):
            values.update(created_by=self.request.user, branch=self.checked_branch(serializer))
        obj = serializer.save(**values)
        if isinstance(obj, m.Record): svc.record_event(obj, self.request.user, f"{obj._meta.model_name}.created")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        Tenant.objects.select_for_update().get(pk=request.tenant.pk)
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        Tenant.objects.select_for_update().get(pk=request.tenant.pk)
        obj = self.get_queryset().select_for_update().get(pk=kwargs["pk"])
        if hasattr(obj, "version"):
            svc.version_check(obj, request.headers.get("If-Match", request.data.get("version")))
        if getattr(obj, "status", "") in self.immutable_statuses or getattr(obj, "locked", False):
            raise svc.Conflict("This record is locked. Use its authorized action or create a revision.")
        if isinstance(obj, m.Attendance):
            check_employee_period(obj.employee, obj.date)
        before = self.get_serializer(obj).data
        serializer = self.get_serializer(obj, data=request.data, partial=kwargs.pop("partial", False))
        serializer.is_valid(raise_exception=True)
        obj = serializer.save(**({"version": obj.version + 1} if hasattr(obj, "version") else {}))
        if isinstance(obj, m.Record): svc.record_event(obj, request.user, f"{obj._meta.model_name}.updated", before=before)
        return Response(serializer.data)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        if not isinstance(obj, m.Record):
            raise ValidationError("Existing customer history cannot be deleted here.")
        svc.version_check(obj, request.headers.get("If-Match", request.data.get("version")))
        if getattr(obj, "status", "") in self.immutable_statuses or getattr(obj, "locked", False):
            raise svc.Conflict("Posted and finalized records cannot be archived.")
        obj.archived = True
        if isinstance(obj, m.Document): obj.status = "cancelled"
        svc.touch(obj)
        svc.record_event(obj, request.user, f"{obj._meta.model_name}.archived")
        return Response(status=204)

    def handle_exception(self, exc):
        if isinstance(exc, (ModelValidationError, IntegrityError)):
            exc = ValidationError(getattr(exc, "message_dict", None) or "This change conflicts with an existing record or a required data rule.")
        return super().handle_exception(exc)

    def run_command(self, request, handler):
        obj = self.get_object()
        operation = f"{obj._meta.model_name}:{obj.pk}:{self.action}"
        def execute():
            fresh = self.get_queryset().select_for_update().get(pk=obj.pk)
            svc.version_check(fresh, request.data.get("version"))
            result = handler(fresh)
            return self.get_serializer(result).data if isinstance(result, self.queryset.model) else result
        return Response(svc.command(request, operation, execute))


class ItemViewSet(ERPViewSet):
    queryset = m.Item.objects.all(); serializer_class = s.ItemSerializer; permission_prefix = "item"


class CustomerViewSet(ERPViewSet):
    queryset = Company.objects.all().order_by("name"); serializer_class = s.CustomerSerializer; permission_prefix = "customer"


class SupplierViewSet(ERPViewSet):
    queryset = m.Supplier.objects.all(); serializer_class = s.SupplierSerializer; feature = "purchase"; permission_prefix = "supplier"


class WarehouseViewSet(ERPViewSet):
    queryset = m.Warehouse.objects.all(); serializer_class = s.WarehouseSerializer; feature = "inventory"; permission_prefix = "warehouse"
    def perform_create(self, serializer):
        if m.Warehouse.objects.filter(tenant=self.request.tenant, archived=False).exists():
            require_feature(self.request.tenant, "multi_warehouse")
        super().perform_create(serializer)


class BinViewSet(ERPViewSet):
    queryset = m.WarehouseBin.objects.select_related("warehouse"); serializer_class = s.BinSerializer; feature = "inventory"; permission_prefix = "warehouse"


class DepartmentViewSet(ERPViewSet):
    queryset = m.Department.objects.all(); serializer_class = s.DepartmentSerializer; feature = "hrms"; permission_prefix = "employee"


class CostCenterViewSet(ERPViewSet):
    queryset = m.CostCenter.objects.all(); serializer_class = s.CostCenterSerializer; feature = "expense_management"; permission_prefix = "expense"


class DocumentViewSet(ERPViewSet):
    queryset = m.Document.objects.select_related("customer", "supplier", "warehouse", "source").prefetch_related("lines__item")
    serializer_class = s.DocumentSerializer
    search_fields = ("number", "title", "reference", "customer__name", "supplier__name")
    action_permissions = {"post": "post", "accept": "edit", "convert": "convert", "receive_quantities": "receive", "revise": "edit", "delivery": "edit", "void": "void"}

    def doc_kind(self):
        if self.kwargs.get("pk"):
            return m.Document.objects.filter(pk=self.kwargs["pk"], tenant=self.request.tenant).values_list("kind", flat=True).first() or "invoice"
        return self.request.data.get("kind", self.request.query_params.get("kind", "sales_order"))

    def required_feature(self):
        kind = self.doc_kind()
        if kind not in DOCUMENT_FEATURES: raise ValidationError({"kind": "Unknown document type."})
        return DOCUMENT_FEATURES[kind]

    def permission_code(self):
        self.permission_prefix = DOCUMENT_PERMISSION[self.doc_kind()]
        return super().permission_code()

    def get_queryset(self):
        return super().get_queryset().filter(kind=self.doc_kind())

    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        doc = self.get_object()
        if doc.kind in ("goods_receipt", "dispatch") and "inventory" in features(request.tenant):
            authorize(request, "stock.inward" if doc.kind == "goods_receipt" else "stock.issue", feature="inventory")
        return self.run_command(request, lambda obj: svc.post_document(obj, request.user, features(request.tenant)))

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        def accept(obj):
            if obj.kind != "quotation" or obj.status != "issued": raise svc.Conflict("Only an issued quotation can be accepted.")
            if not request.data.get("reference"): raise ValidationError({"reference": "Record how the customer accepted this quotation."})
            obj.status = "accepted"; obj.snapshot["acceptance"] = {"reference": request.data["reference"], "at": str(timezone.now())}
            svc.touch(obj); svc.record_event(obj, request.user, "quotation.accepted"); return obj
        return self.run_command(request, accept)

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        target = request.data.get("target", "")
        if target not in DOCUMENT_FEATURES: raise ValidationError({"target": "Choose a target document."})
        authorize(request, f"{DOCUMENT_PERMISSION[target]}.create", feature=DOCUMENT_FEATURES[target])
        return self.run_command(request, lambda obj: svc.convert_document(obj, target, request.data.get("lines"), request.user, request.data))

    @action(detail=True, methods=["post"], url_path="receipt-quantities")
    def receive_quantities(self, request, pk=None):
        def update(obj):
            if obj.kind != "goods_receipt" or obj.status != "draft": raise svc.Conflict("Only a draft receipt can be changed.")
            for row in request.data.get("lines", []):
                line = obj.lines.filter(pk=row["id"]).first()
                if not line: raise ValidationError("Receipt line not found.")
                line.accepted = decimal(row.get("accepted", 0), minimum=0)
                line.rejected = decimal(row.get("rejected", 0), minimum=0)
                line.damaged = decimal(row.get("damaged", 0), minimum=0)
                if line.accepted + line.rejected + line.damaged != line.quantity: raise ValidationError("Receipt quantities must reconcile.")
                line.save()
            if request.data.get("warehouse"):
                obj.warehouse = m.Warehouse.objects.get(tenant=request.tenant, pk=request.data["warehouse"], archived=False)
            svc.touch(obj); return obj
        return self.run_command(request, update)

    @action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        def revise(obj):
            if obj.kind != "quotation" or obj.status == "draft": raise ValidationError("Only an issued quotation needs a revision.")
            if obj.derived_documents.filter(kind="sales_order").exists(): raise ValidationError("Amend the sales order after conversion; do not revise accepted history.")
            data = s.DocumentSerializer(obj, context={"request": request}).data
            data["source"] = None
            serializer = s.DocumentSerializer(data=data, context={"request": request}); serializer.is_valid(raise_exception=True)
            new = serializer.save(tenant=request.tenant, branch=obj.branch, created_by=request.user)
            new.source = obj; new.revision = obj.revision + 1; new.save()
            svc.record_event(new, request.user, "quotation.revised"); return new
        return self.run_command(request, revise)

    @action(detail=True, methods=["post"])
    def delivery(self, request, pk=None):
        def update(obj):
            if obj.kind != "dispatch" or obj.status != "posted": raise ValidationError("Post the dispatch before recording delivery.")
            target = request.data.get("delivery_status")
            if target not in ("in_transit", "delivered", "partial", "failed", "pod_pending"): raise ValidationError("Choose a delivery status.")
            obj.delivery_status = target
            svc.touch(obj); svc.record_event(obj, request.user, "dispatch.delivery_updated"); return obj
        return self.run_command(request, update)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        def reverse(obj):
            if obj.kind != "invoice" or obj.status != "posted": raise ValidationError("Only a posted customer invoice can be voided here.")
            if obj.paid or obj.derived_documents.exists(): raise ValidationError("This invoice has settlements or credits. Use a controlled credit note.")
            reason = request.data.get("reason", "").strip()
            if not reason: raise ValidationError({"reason": "A void reason is required."})
            svc.open_period(obj.tenant, timezone.localdate())
            svc.fact(obj, "revenue", -obj.taxable, f"Void {obj.number}: {reason}", customer=obj.customer, key=f"invoice:{obj.pk}:void", business_date=timezone.localdate())
            obj.status = "void"; svc.touch(obj); svc.record_event(obj, request.user, "invoice.voided", after={"reason": reason}); return obj
        return self.run_command(request, reverse)


class JobViewSet(ERPViewSet):
    queryset = m.Job.objects.select_related("customer", "source_order", "department", "owner").prefetch_related("stages")
    serializer_class = s.JobSerializer; feature = "work_orders"; permission_prefix = "job"
    action_permissions = {"transition": "progress", "stage": "progress"}

    def perform_create(self, serializer):
        source = serializer.validated_data.get("source_order")
        if source and (source.kind != "sales_order" or source.status != "confirmed"):
            raise ValidationError({"source_order": "Choose a confirmed sales order."})
        obj = serializer.save(tenant=self.request.tenant, branch=self.checked_branch(serializer), created_by=self.request.user, number=svc.number(self.request.tenant, "job"))
        names = ["Preparation", "Production", "Quality check", "Packing"]
        if "custom_workflows" in features(obj.tenant):
            config = m.Configuration.objects.filter(tenant=obj.tenant,kind="workflow",entity_type="job",status="published",archived=False).order_by("-version").first()
            if config: names = config.definition["stages"]
        for i, name in enumerate(names):
            if not isinstance(name, str): raise ValidationError("Provide stage names as text.")
            m.JobStage.objects.create(tenant=obj.tenant, branch=obj.branch, created_by=self.request.user, job=obj, name=name[:100], position=i, planned=obj.quantity)
        svc.record_event(obj, self.request.user, "job.created")

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        def update(obj):
            target = request.data.get("status")
            transitions = {"pending": ["in_progress", "cancelled"], "in_progress": ["on_hold", "completed", "cancelled"], "on_hold": ["in_progress", "cancelled"]}
            if target not in transitions.get(obj.status, []): raise svc.Conflict("This status change is not allowed.")
            if target == "completed":
                if obj.stages.exclude(status="completed").exists(): raise ValidationError("Complete all job stages first.")
                obj.completed_quantity = obj.quantity; obj.completed_at = timezone.now()
            if target in ("on_hold", "cancelled") and not request.data.get("reason"): raise ValidationError("A reason is required.")
            obj.status = target; obj.blocker = request.data.get("reason", ""); svc.touch(obj); svc.record_event(obj, request.user, "job.status_changed"); return obj
        return self.run_command(request, update)

    @action(detail=True, methods=["post"])
    def stage(self, request, pk=None):
        require_feature(request.tenant, "production_tracking")
        def update(obj):
            if obj.status in ("completed", "cancelled"): raise svc.Conflict("This job is closed.")
            stage = obj.stages.select_for_update().filter(pk=request.data.get("stage_id")).first()
            if not stage: raise ValidationError("Stage not found.")
            completed = decimal(request.data.get("completed", stage.completed), "completed", 0)
            rejected = decimal(request.data.get("rejected", stage.rejected), "rejected", 0)
            rework = decimal(request.data.get("rework", stage.rework), "rework", 0)
            if completed + rejected > stage.planned or rework > rejected: raise ValidationError("Completed/rejected quantities exceed the plan, or rework exceeds rejected quantity.")
            stage.completed, stage.rejected, stage.rework = completed, rejected, rework
            stage.status = "completed" if completed == stage.planned else "in_progress"
            stage.note = request.data.get("note", ""); svc.touch(stage)
            if obj.status == "pending": obj.status = "in_progress"
            svc.touch(obj); svc.record_event(stage, request.user, "job.stage_updated"); return obj
        return self.run_command(request, update)


class StockViewSet(ERPViewSet):
    queryset = m.StockBalance.objects.select_related("item", "warehouse")
    serializer_class = s.StockSerializer; feature = "inventory"; permission_prefix = "stock"
    search_fields = ("item__name", "item__sku", "item__barcode", "warehouse__name")
    http_method_names = ["get", "post", "head", "options"]
    action_permissions = {"create": "inward", "movement": "issue", "transfer": "transfer", "reserve": "reserve", "release": "reserve"}

    def create(self, request, *args, **kwargs):
        raise ValidationError("Use a stock movement to change balances.")

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        for row in response.data["results"]:
            row["available"] = str(decimal(row["on_hand"]) - decimal(row["reserved"]))
            item = m.Item.objects.get(pk=row["item"])
            row["unit"], row["sku"], row["reorder_level"] = item.unit, item.sku, str(item.reorder_level)
            if not has_permission(request, "stock.view_cost"): row.pop("value", None)
        return response

    @action(detail=False, methods=["post"])
    def movement(self, request):
        kind = request.data.get("kind", "inward")
        permission = {"inward": "stock.inward", "issue": "stock.issue", "adjustment": "stock.adjust"}.get(kind)
        if not permission: raise ValidationError("Choose inward, issue or adjustment.")
        authorize(request, permission, feature="inventory")
        def execute():
            item = m.Item.objects.filter(tenant=request.tenant, pk=request.data.get("item"), archived=False).first()
            warehouse = m.Warehouse.objects.filter(tenant=request.tenant, pk=request.data.get("warehouse"), archived=False).first()
            if not item or not warehouse: raise ValidationError("Choose a valid item and warehouse.")
            authorize(request, permission, warehouse, "inventory")
            qty = decimal(request.data.get("quantity"), "quantity")
            if kind in ("inward", "issue") and qty <= 0: raise ValidationError("Quantity must be positive.")
            if kind == "issue": qty = -qty
            reason = request.data.get("reason", "").strip()
            if not reason: raise ValidationError({"reason": "A movement reason is required."})
            job = None
            if request.data.get("job"):
                require_feature(request.tenant, "work_orders")
                job = m.Job.objects.filter(tenant=request.tenant, pk=request.data["job"]).first()
                if not job: raise ValidationError("Job not found.")
                authorize(request, "job.view", job, "work_orders")
            movement = svc.move_stock(tenant=request.tenant, actor=request.user, item=item, warehouse=warehouse, quantity=qty,
                unit_cost=request.data.get("unit_cost"), kind=kind, reason=reason, job=job)
            return s.MovementSerializer(movement, context={"request": request}).data
        return Response(svc.command(request, "stock:movement", execute), status=201)

    @action(detail=False, methods=["post"])
    def transfer(self, request):
        require_feature(request.tenant, "multi_warehouse")
        def execute():
            source = m.Warehouse.objects.filter(tenant=request.tenant, pk=request.data.get("from_warehouse")).first()
            dest = m.Warehouse.objects.filter(tenant=request.tenant, pk=request.data.get("to_warehouse")).first()
            item = m.Item.objects.filter(tenant=request.tenant, pk=request.data.get("item")).first()
            if not source or not dest or not item or source == dest: raise ValidationError("Choose an item and two different warehouses.")
            authorize(request, "stock.transfer", source, "inventory"); authorize(request, "stock.transfer", dest, "inventory")
            qty = decimal(request.data.get("quantity"), "quantity", ".000001"); transfer_id = uuid.uuid4()
            out = svc.move_stock(tenant=request.tenant, actor=request.user, item=item, warehouse=source, quantity=-qty, kind="transfer", reason=f"Transfer to {dest.name}", transfer_id=transfer_id)
            incoming = svc.move_stock(tenant=request.tenant, actor=request.user, item=item, warehouse=dest, quantity=qty, unit_cost=-out.value / qty, kind="transfer", reason=f"Transfer from {source.name}", transfer_id=transfer_id)
            return {"id": transfer_id, "outgoing": out.pk, "incoming": incoming.pk}
        return Response(svc.command(request, "stock:transfer", execute))

    @action(detail=True, methods=["post"])
    def reserve(self, request, pk=None):
        def execute(balance):
            qty = decimal(request.data.get("quantity"), "quantity", ".000001")
            if balance.on_hand - balance.reserved < qty: raise svc.Conflict("There is not enough unreserved stock.")
            order = m.Document.objects.filter(tenant=request.tenant, kind="sales_order", pk=request.data.get("order")).first() if request.data.get("order") else None
            if not order: raise ValidationError("Choose an existing sales order.")
            authorize(request, "order.view", order)
            reservation = m.Reservation.objects.create(tenant=request.tenant, branch=balance.branch, created_by=request.user, balance=balance, order=order, quantity=qty)
            balance.reserved += qty; svc.touch(balance); svc.record_event(reservation, request.user, "stock.reserved")
            return {"id": reservation.pk, "status": "active"}
        return self.run_command(request, execute)


class MovementViewSet(ERPViewSet):
    queryset = m.StockMovement.objects.select_related("item", "warehouse", "job")
    serializer_class = s.MovementSerializer; feature = "inventory"; permission_prefix = "stock"; http_method_names = ["get", "post", "head", "options"]
    search_fields = ("item__name", "item__sku", "reason", "warehouse__name")
    def create(self, request, *args, **kwargs): raise ValidationError("Use a stock posting command.")
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        from .inventory import reverse_movement
        reason = request.data.get("reason", "").strip()
        if not reason: raise ValidationError("A reversal reason is required.")
        return self.run_command(request, lambda obj: reverse_movement(obj, request.user, reason))


class ExpenseCategoryViewSet(ERPViewSet):
    queryset = m.ExpenseCategory.objects.all(); serializer_class = s.CategorySerializer; feature = "expense_management"; permission_prefix = "expense"


class ExpenseViewSet(ERPViewSet):
    queryset = m.Expense.objects.select_related("category", "supplier", "job", "cost_center")
    serializer_class = s.ExpenseSerializer; feature = "expense_management"; permission_prefix = "expense"
    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        return self.run_command(request, lambda obj: svc.post_expense(obj, request.user))
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        def reverse(obj):
            if obj.status != "posted" or not request.data.get("reason"): raise ValidationError("A posted expense and reversal reason are required.")
            svc.open_period(obj.tenant, timezone.localdate())
            svc.fact(obj, obj.category.classification, -obj.amount, f"Reversal: {obj.title}", obj.category.name, job=obj.job, key=f"expense:{obj.pk}:void", business_date=timezone.localdate())
            obj.status = "void"; svc.touch(obj); svc.record_event(obj, request.user, "expense.voided"); return obj
        return self.run_command(request, reverse)


class RecurringViewSet(ERPViewSet):
    queryset = m.RecurringExpense.objects.select_related("category", "cost_center")
    serializer_class = s.RecurringSerializer; feature = "expense_management"; permission_prefix = "expense"
    action_permissions = {"generate": "create"}
    @action(detail=True, methods=["post"])
    def generate(self, request, pk=None):
        return self.run_command(request, lambda obj: {"draft_ids": svc.generate_recurring(obj, request.user)})


class PaymentViewSet(ERPViewSet):
    queryset = m.Payment.objects.select_related("customer", "supplier"); serializer_class = s.PaymentSerializer; permission_prefix = "payment"
    http_method_names = ["get", "post", "head", "options"]; action_permissions = {"create": "record"}
    def create(self, request, *args, **kwargs):
        if request.data.get("direction") == "payment": require_feature(request.tenant, "purchase")
        for a in request.data.get("allocations", []):
            doc = m.Document.objects.filter(tenant=request.tenant, pk=a.get("document")).first()
            if not doc: raise NotFound("Invoice not found.")
            authorize(request, "payment.record", doc)
        return Response(svc.command(request, "payment:record", lambda: s.PaymentSerializer(svc.record_payment(request.tenant, request.user, request.data), context={"request": request}).data), status=201)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        def reverse(obj):
            if obj.status != "posted": raise svc.Conflict("This payment has already been reversed.")
            reason = request.data.get("reason", "").strip()
            if not reason: raise ValidationError("A reversal reason is required.")
            svc.open_period(obj.tenant, timezone.localdate())
            for allocation in obj.allocations.select_related("document").order_by("document_id"):
                invoice = m.Document.objects.select_for_update().get(pk=allocation.document_id)
                authorize(request, "payment.void", invoice)
                invoice.paid -= allocation.amount; svc.touch(invoice)
            obj.status = "void"; obj.voided_at = timezone.now(); svc.touch(obj)
            svc.record_event(obj, request.user, "payment.voided", after={"reason": reason}); return obj
        return self.run_command(request, reverse)


def check_employee_period(employee, day):
    if m.PayrollResult.objects.filter(employee=employee, run__month=day.replace(day=1), run__status="finalized").exists():
        raise svc.Conflict("Finalized payroll has locked this period.")


class EmployeeViewSet(ERPViewSet):
    queryset = m.Employee.objects.select_related("department", "shift", "manager"); serializer_class = s.EmployeeSerializer; feature = "hrms"; permission_prefix = "employee"
    immutable_statuses = ()
    def perform_create(self, serializer):
        if "monthly_salary" in self.request.data and not has_permission(self.request, "employee.view_private"):
            raise PermissionDenied("Private employee data permission is required.")
        super().perform_create(serializer)


class ShiftViewSet(ERPViewSet):
    queryset = m.Shift.objects.all(); serializer_class = s.ShiftSerializer; feature = "attendance_hr"; permission_prefix = "attendance"


class HolidayViewSet(ERPViewSet):
    queryset = m.Holiday.objects.all(); serializer_class = s.HolidaySerializer; feature = "attendance_hr"; permission_prefix = "attendance"


class AttendanceViewSet(ERPViewSet):
    queryset = m.Attendance.objects.select_related("employee"); serializer_class = s.AttendanceSerializer; feature = "attendance_hr"; permission_prefix = "attendance"
    action_permissions = {"punch": "checkin"}
    def perform_create(self, serializer):
        check_employee_period(serializer.validated_data["employee"], serializer.validated_data.get("date", timezone.localdate()))
        if serializer.validated_data.get("approved_ot_hours", ZERO) and not has_permission(self.request, "attendance.approve"):
            raise PermissionDenied("Overtime approval permission is required.")
        super().perform_create(serializer)
    @action(detail=False, methods=["post"])
    def punch(self, request):
        def execute():
            employee = m.Employee.objects.filter(tenant=request.tenant, user=request.user, archived=False).first()
            if not employee: raise ValidationError("Your login has not been linked to an employee profile.")
            now = timezone.now(); today = timezone.localdate()
            open_day = m.Attendance.objects.filter(employee=employee, check_in__gte=now-timedelta(hours=24), check_out__isnull=True).order_by("-date").first()
            kind = request.data.get("kind")
            day = open_day.date if kind == "out" and open_day else today
            check_employee_period(employee, day)
            attendance, _ = m.Attendance.objects.get_or_create(tenant=request.tenant, employee=employee, date=day, defaults={"branch": employee.branch, "created_by": request.user})
            if kind == "in":
                if attendance.check_in: raise svc.Conflict("You are already checked in for this work day.")
                attendance.check_in = now
            elif kind == "out":
                if not attendance.check_in or attendance.check_out: raise svc.Conflict("There is no open check-in to close.")
                attendance.check_out = now
            else: raise ValidationError("Choose in or out.")
            svc.touch(attendance); svc.record_event(attendance, request.user, f"attendance.check_{kind}")
            return s.AttendanceSerializer(attendance, context={"request": request}).data
        return Response(svc.command(request, "attendance:punch", execute))


class LeaveTypeViewSet(ERPViewSet):
    queryset = m.LeaveType.objects.all(); serializer_class = s.LeaveTypeSerializer; feature = "attendance_hr"; permission_prefix = "leave"


class LeaveViewSet(ERPViewSet):
    queryset = m.LeaveRequest.objects.select_related("employee", "employee__shift", "leave_type")
    serializer_class = s.LeaveSerializer; feature = "attendance_hr"; permission_prefix = "leave"
    action_permissions = {"review": "approve", "cancel": "edit"}
    immutable_statuses = ("approved", "rejected", "cancelled")
    def perform_create(self, serializer):
        obj = serializer.save(tenant=self.request.tenant, branch=self.checked_branch(serializer), created_by=self.request.user)
        obj.days = workforce.leave_days(obj)
        if obj.days <= 0: raise ValidationError("Select at least one working day.")
        obj.save(); svc.record_event(obj, self.request.user, "leave.requested")
    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        return self.run_command(request, lambda obj: workforce.review_leave(obj, request.user, request.data.get("decision")))
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        def cancel(obj):
            if obj.status not in ("pending", "approved"): raise svc.Conflict("This request is already closed.")
            check_employee_period(obj.employee, obj.start_date); check_employee_period(obj.employee, obj.end_date)
            obj.status = "cancelled"; svc.touch(obj); svc.record_event(obj, request.user, "leave.cancelled"); return obj
        return self.run_command(request, cancel)


class SalaryComponentViewSet(ERPViewSet):
    queryset = m.SalaryComponent.objects.select_related("employee"); serializer_class = s.SalaryComponentSerializer; feature = "payroll"; permission_prefix = "payroll"


class LoanViewSet(ERPViewSet):
    queryset = m.EmployeeLoan.objects.select_related("employee"); serializer_class = s.LoanSerializer; feature = "payroll"; permission_prefix = "payroll"


class PayrollViewSet(ERPViewSet):
    queryset = m.PayrollRun.objects.prefetch_related("results__employee"); serializer_class = s.PayrollSerializer; feature = "payroll"; permission_prefix = "payroll"
    action_permissions = {"calculate": "edit", "approve": "approve", "finalize": "finalize", "pay": "record_payment"}
    @action(detail=True, methods=["post"])
    def calculate(self, request, pk=None):
        return self.run_command(request, lambda obj: workforce.calculate_payroll(obj, request.user, request.data.get("manual_inputs")))
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        def approve(obj):
            if obj.status != "review" or not obj.results.exists(): raise ValidationError("Calculate and review payroll first.")
            if any(r.warnings for r in obj.results.all()): raise ValidationError("Resolve all payroll exceptions before approval.")
            obj.status = "approved"; svc.touch(obj); svc.record_event(obj, request.user, "payroll.approved"); return obj
        return self.run_command(request, approve)
    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        return self.run_command(request, lambda obj: workforce.finalize_payroll(obj, request.user))
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        def pay(obj):
            if obj.status != "finalized": raise ValidationError("Finalize payroll before recording salary payment.")
            result = obj.results.select_for_update().filter(pk=request.data.get("result_id")).first()
            if not result: raise ValidationError("Employee result not found.")
            amount = money(decimal(request.data.get("amount"), minimum=".01"))
            if result.paid + amount > result.net: raise svc.Conflict("Payment exceeds this employee's net salary balance.")
            reference = request.data.get("reference", "").strip()
            if not reference: raise ValidationError("A payment reference is required.")
            payment = m.SalaryPayment.objects.create(tenant=request.tenant, branch=result.branch, created_by=request.user, result=result, amount=amount, reference=reference)
            result.paid += amount; svc.touch(result); svc.touch(obj); svc.record_event(payment, request.user, "payroll.salary_paid"); return obj
        return self.run_command(request, pay)


class TaskViewSet(ERPViewSet):
    queryset = m.Task.objects.select_related("owner", "job"); serializer_class = s.TaskSerializer; feature = "tasks"; permission_prefix = "task"
    immutable_statuses = ()
    action_permissions = {"comment":"edit", "checklist":"edit"}
    @action(detail=True, methods=["post"])
    def comment(self,request,pk=None):
        def add(obj):
            content=request.data.get("content","").strip()
            if not content or len(content)>2000:raise ValidationError("Write a comment up to 2,000 characters.")
            m.TaskComment.objects.create(tenant=obj.tenant,branch=obj.branch,created_by=request.user,task=obj,content=content)
            svc.touch(obj);svc.record_event(obj,request.user,"task.commented");return obj
        return self.run_command(request,add)
    @action(detail=True, methods=["post"])
    def checklist(self,request,pk=None):
        def change(obj):
            action_name=request.data.get("operation");items=list(obj.checklist);item_id=str(request.data.get("item_id") or uuid.uuid4())
            if action_name=="add":
                text=request.data.get("text","").strip()
                if not text or len(text)>300 or len(items)>=50:raise ValidationError("Write a checklist item up to 300 characters; maximum 50 items.")
                items.append({"id":item_id,"text":text,"done":False})
            elif action_name=="toggle":
                found=False
                for item in items:
                    if item["id"]==item_id:item["done"]=not item["done"];found=True
                if not found:raise NotFound("Checklist item not found.")
            elif action_name=="remove":items=[item for item in items if item["id"]!=item_id]
            else:raise ValidationError("Choose add, toggle or remove.")
            obj.checklist=items;svc.touch(obj);svc.record_event(obj,request.user,"task.checklist_changed");return obj
        return self.run_command(request,change)


class ApprovalRuleViewSet(ERPViewSet):
    queryset = m.ApprovalRule.objects.all(); serializer_class = s.ApprovalRuleSerializer; feature = "approvals"; permission_prefix = "approval"
    action_permissions = {"create": "manage", "update": "manage", "partial_update": "manage", "destroy": "manage"}


class ApprovalViewSet(ERPViewSet):
    queryset = m.Approval.objects.prefetch_related("decisions"); serializer_class = s.ApprovalSerializer; feature = "approvals"; permission_prefix = "approval"
    http_method_names = ["get", "post", "head", "options"]
    def create(self, request, *args, **kwargs): raise ValidationError("Submit a business record to request approval.")
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        def decide(obj):
            if obj.status != "pending": raise svc.Conflict("This approval has already been decided.")
            if obj.created_by_id == request.user.pk and not obj.allow_self: raise PermissionDenied("Self-approval is not allowed by this policy.")
            if obj.steps:
                role_code = obj.steps[obj.current_step]
                if not assignments(request, "approval.decide").filter(role__code=role_code).exists(): raise PermissionDenied("You are not an approver for the current step.")
            model = {"document": m.Document, "expense": m.Expense}.get(obj.resource_type)
            if not model: raise ValidationError("Unsupported approval resource.")
            resource = model.objects.select_for_update().get(tenant=request.tenant, pk=obj.resource_id)
            if resource.version != obj.resource_version: raise svc.Conflict("The source changed. Resubmit it for approval.")
            required_feature = DOCUMENT_FEATURES[resource.kind] if isinstance(resource, m.Document) else "expense_management"
            require_feature(request.tenant, required_feature)
            prefix = DOCUMENT_PERMISSION[resource.kind] if isinstance(resource, m.Document) else "expense"
            authorize(request, f"{prefix}.approve", resource, required_feature)
            decision = request.data.get("decision")
            if decision not in ("approve", "reject"): raise ValidationError("Choose approve or reject.")
            note = request.data.get("note", "").strip()
            if decision == "reject" and not note: raise ValidationError("Explain why this request is rejected.")
            m.ApprovalDecision.objects.create(tenant=request.tenant, branch=obj.branch, created_by=request.user, approval=obj, step=obj.current_step, decision=decision, note=note)
            if decision == "reject": obj.status = "rejected"; resource.status = "draft"
            elif obj.current_step + 1 < len(obj.steps): obj.current_step += 1
            else: obj.status = "approved"; resource.status = "approved"
            if obj.status != "pending": svc.touch(resource)
            svc.touch(obj); svc.record_event(obj, request.user, f"approval.{decision}"); return obj
        return self.run_command(request, decide)


class ConfigurationViewSet(ERPViewSet):
    queryset = m.Configuration.objects.all(); serializer_class = s.ConfigSerializer; permission_prefix = "settings"
    action_permissions = {"create": "manage", "update": "manage", "partial_update": "manage", "destroy": "manage", "publish": "manage"}
    def required_feature(self):
        kind = self.request.data.get("kind", self.request.query_params.get("kind", "workflow"))
        if self.kwargs.get("pk"):
            kind = m.Configuration.objects.filter(tenant=self.request.tenant, pk=self.kwargs["pk"]).values_list("kind", flat=True).first() or kind
        return {"workflow": "custom_workflows", "custom_field": "custom_fields", "print_template": "custom_templates", "report_schedule": "scheduled_reports", "integration": "communications", "saved_filter": "basic"}.get(kind, "basic")
    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        def publish(obj):
            from .configuration import validate_configuration
            validate_configuration(obj)
            obj.status = "published"; svc.touch(obj); svc.record_event(obj, request.user, "configuration.published"); return obj
        return self.run_command(request, publish)


class PeriodViewSet(ERPViewSet):
    queryset = m.PeriodLock.objects.all(); serializer_class = s.PeriodSerializer; permission_prefix = "settings"
    action_permissions = {"create": "manage", "update": "manage", "partial_update": "manage", "destroy": "manage"}


class CommunicationViewSet(ERPViewSet):
    queryset=m.Communication.objects.select_related("document")
    serializer_class=s.CommunicationSerializer;feature="communications";permission_prefix="document"
    http_method_names=["get","post","head","options"]
    action_permissions={"create":"share"}
    def get_queryset(self):
        qs=super().get_queryset()
        return qs if has_permission(self.request,"settings.manage") else qs.filter(created_by=self.request.user)
    def perform_create(self,serializer):
        from django.conf import settings
        import re
        channel=serializer.validated_data["channel"];recipient=serializer.validated_data["recipient"]
        if not serializer.validated_data.get("consent_reference","").strip():raise ValidationError({"consent_reference":"Record the recipient consent or business communication basis."})
        if channel=="email":
            from django.core.validators import validate_email
            try:validate_email(recipient)
            except Exception:raise ValidationError({"recipient":"Enter a valid email address."})
            if not getattr(settings,"ERP_EMAIL_ENABLED",False):raise ValidationError("Email delivery is not enabled on this server.")
        else:
            if not re.fullmatch(r"\+[1-9]\d{7,14}",recipient):raise ValidationError({"recipient":"Use an international WhatsApp number, for example +919876543210."})
            if not getattr(settings,"WHATSAPP_API_URL","") or not getattr(settings,"WHATSAPP_ACCESS_TOKEN",""):raise ValidationError("The official WhatsApp provider is not connected on this server.")
        document=serializer.validated_data.get("document")
        if document:
            authorize(self.request,f"{DOCUMENT_PERMISSION[document.kind]}.view",document,DOCUMENT_FEATURES[document.kind])
        super().perform_create(serializer)
        transaction.on_commit(lambda:m.OutboxEvent.objects.create(tenant=self.request.tenant,branch=serializer.instance.branch,created_by=self.request.user,event="communication.queued",source_type="communication",source_id=str(serializer.instance.pk),payload={"communication_id":str(serializer.instance.pk)}))


class ApiCredentialViewSet(ERPViewSet):
    queryset=m.ApiCredential.objects.select_related("user");serializer_class=s.ApiCredentialSerializer;feature="api_access";permission_prefix="settings"
    http_method_names=["get","post","head","options"]
    action_permissions={"list":"manage","retrieve":"manage","create":"manage","revoke":"manage"}
    def create(self,request,*args,**kwargs):
        from .api_keys import create_key
        name=request.data.get("name","").strip();permissions=request.data.get("permissions",[])
        if not name or not isinstance(permissions,list) or not permissions:raise ValidationError("Name the key and select at least one permission.")
        obj,raw=create_key(request.tenant,request.user,name,permissions,request.data.get("days",90))
        svc.record_event(obj,request.user,"api_key.created")
        data=s.ApiCredentialSerializer(obj,context={"request":request}).data;data["secret"]=raw
        return Response(data,status=201)
    @action(detail=True,methods=["post"])
    def revoke(self,request,pk=None):
        def revoke(obj):
            if obj.revoked_at:raise svc.Conflict("This API key is already revoked.")
            obj.revoked_at=timezone.now();svc.touch(obj);svc.record_event(obj,request.user,"api_key.revoked");return obj
        return self.run_command(request,revoke)


class WebhookViewSet(ERPViewSet):
    queryset=m.WebhookEndpoint.objects.all();serializer_class=s.WebhookSerializer;feature="api_access";permission_prefix="settings"
    action_permissions={"list":"manage","retrieve":"manage","create":"manage","update":"manage","partial_update":"manage","destroy":"manage"}
    def perform_create(self,serializer):
        from .api_keys import valid_webhook_url
        valid_webhook_url(serializer.validated_data["url"]);super().perform_create(serializer)
    def perform_update(self,serializer):
        from .api_keys import valid_webhook_url
        valid_webhook_url(serializer.validated_data.get("url",serializer.instance.url));super().perform_update(serializer)


class ReservationViewSet(ERPViewSet):
    queryset = m.Reservation.objects.select_related("balance__item", "balance__warehouse", "order")
    serializer_class = s.ReservationSerializer; feature = "inventory"; permission_prefix = "stock"
    http_method_names = ["get", "post", "head", "options"]
    action_permissions = {"release": "reserve"}
    def create(self, request, *args, **kwargs): raise ValidationError("Reserve a stock balance using its reserve action.")
    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        from .inventory import release_reservation
        return self.run_command(request, lambda obj: release_reservation(obj, request.user))


class PositionViewSet(ERPViewSet):
    queryset = m.StockPosition.objects.select_related("balance__item", "bin__warehouse")
    serializer_class = s.PositionSerializer; feature = "inventory"; permission_prefix = "stock"
    http_method_names = ["get", "post", "head", "options"]
    action_permissions = {"relocate": "transfer"}
    def create(self, request, *args, **kwargs): raise ValidationError("Positions are maintained by stock movements.")
    @action(detail=True, methods=["post"])
    def relocate(self, request, pk=None):
        def relocate(obj):
            amount = decimal(request.data.get("quantity"), "quantity", ".000001")
            destination = m.WarehouseBin.objects.filter(tenant=request.tenant, warehouse=obj.bin.warehouse, pk=request.data.get("bin"), archived=False).first()
            if not destination or destination == obj.bin: raise ValidationError("Choose a different bin within this warehouse.")
            if amount > obj.quantity: raise svc.Conflict("This bin does not contain that quantity.")
            target, _ = m.StockPosition.objects.select_for_update().get_or_create(tenant=request.tenant, balance=obj.balance, bin=destination, defaults={"branch":obj.branch})
            obj.quantity -= amount; target.quantity += amount; svc.touch(obj); svc.touch(target)
            for position, quantity in ((obj, -amount), (target, amount)):
                m.PositionMovement.objects.create(tenant=request.tenant, branch=obj.branch, created_by=request.user, balance=obj.balance, bin=position.bin, quantity=quantity, reason="Internal bin relocation")
            svc.record_event(obj, request.user, "stock.bin_relocated"); return obj
        return self.run_command(request, relocate)


class StockCountViewSet(ERPViewSet):
    queryset = m.StockCount.objects.select_related("warehouse").prefetch_related("lines__balance__item")
    serializer_class = s.StockCountSerializer; feature = "inventory"; permission_prefix = "stock"
    action_permissions = {"create":"adjust", "update":"adjust", "partial_update":"adjust", "destroy":"adjust", "quantities":"adjust", "post":"adjust"}
    def perform_create(self, serializer):
        from .inventory import open_count
        super().perform_create(serializer); open_count(serializer.instance)
    @action(detail=True, methods=["post"])
    def quantities(self, request, pk=None):
        def update(obj):
            if obj.status != "draft": raise svc.Conflict("Only a draft count accepts quantities.")
            for row in request.data.get("lines", []):
                line = obj.lines.filter(pk=row.get("id")).first()
                if not line: raise ValidationError("Count line not found.")
                line.counted = decimal(row.get("counted"), "counted", 0); line.save(update_fields=["counted"])
            svc.touch(obj); svc.record_event(obj, request.user, "stock.count_quantities_saved"); return obj
        return self.run_command(request, update)
    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        from .inventory import post_count
        return self.run_command(request, lambda obj: post_count(obj, request.user))
