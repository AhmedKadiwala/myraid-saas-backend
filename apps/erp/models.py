"""Normalized ERP domain records, alongside (not replacing) the existing CRM."""
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT)
    branch = models.ForeignKey("core.Branch", on_delete=models.PROTECT, null=True, blank=True)
    created_by = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    archived = models.BooleanField(default=False)
    custom_fields = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ["-created_at", "id"]

    def clean(self):
        super().clean()
        # Model-level safety also protects management commands and admin writes.
        for field in self._meta.fields:
            if not isinstance(field, models.ForeignKey) or field.name in ("tenant", "created_by"):
                continue
            value = getattr(self, field.attname)
            if value is None:
                continue
            related = getattr(self, field.name)
            related_tenant = getattr(related, "tenant_id", None)
            if related_tenant is not None and related_tenant != self.tenant_id:
                raise ValidationError({field.name: "This record belongs to another company."})


class ErpSettings(models.Model):
    tenant = models.OneToOneField("core.Tenant", on_delete=models.PROTECT, related_name="erp_settings")
    legal_name = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default="INR")
    default_tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal("0"))
    fiscal_year_start = models.PositiveSmallIntegerField(default=4)
    switches = models.JSONField(default=dict, blank=True)
    numbering = models.JSONField(default=dict, blank=True)
    payroll_policy = models.JSONField(default=dict, blank=True)
    expected_expense_categories = models.JSONField(default=list, blank=True)
    version = models.PositiveIntegerField(default=1)


class Entitlement(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="erp_entitlements")
    feature = models.CharField(max_length=80)
    enabled = models.BooleanField(default=False)
    effective_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=500)
    changed_by = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "feature"], name="erp_entitlement_key")]


class NumberSeries(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    year = models.CharField(max_length=10)
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "kind", "year"], name="erp_number_key")]


class Warehouse(Record):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30)
    address = models.TextField(blank=True)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "code"], name="erp_warehouse_code")]


class WarehouseBin(Record):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="bins")
    code = models.CharField(max_length=40)
    rack = models.CharField(max_length=40, blank=True)
    pick_priority = models.PositiveSmallIntegerField(default=100)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["warehouse", "code"], name="erp_bin_code")]


class Item(Record):
    TYPES = [(x, x.replace("_", " ").title()) for x in ["raw_material", "semi_finished", "finished", "consumable", "spare", "service"]]
    sku = models.CharField(max_length=80)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    item_type = models.CharField(max_length=20, choices=TYPES, default="raw_material")
    category = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=20, default="pcs")
    hsn_sac = models.CharField(max_length=20, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    sale_rate = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    purchase_rate = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    reorder_level = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    target_stock = models.DecimalField(max_digits=20, decimal_places=6, default=0)

    class Meta(Record.Meta):
        constraints = [
            models.UniqueConstraint(fields=["tenant", "sku"], name="erp_item_sku"),
            models.UniqueConstraint(fields=["tenant", "barcode"], condition=~Q(barcode=""), name="erp_item_barcode"),
            models.CheckConstraint(condition=Q(sale_rate__gte=0, purchase_rate__gte=0, tax_rate__gte=0, reorder_level__gte=0), name="erp_item_nonnegative"),
        ]


class Supplier(Record):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    gstin = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    payment_terms = models.PositiveSmallIntegerField(default=30)
    notes = models.TextField(blank=True)


class Department(Record):
    name = models.CharField(max_length=100)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="erp_department_name")]


class CostCenter(Record):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "code"], name="erp_costcenter_code")]


class Document(Record):
    """Shared typed commercial header; every kind has its own command/state policy."""
    KINDS = [(x, x.replace("_", " ").title()) for x in ["quotation", "sales_order", "requisition", "purchase_order", "goods_receipt", "dispatch", "invoice", "supplier_bill", "credit_note", "supplier_return"]]
    kind = models.CharField(max_length=30, choices=KINDS)
    number = models.CharField(max_length=50)
    title = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=30, default="draft")
    customer = models.ForeignKey("core.Company", on_delete=models.PROTECT, null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, null=True, blank=True)
    source = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="derived_documents")
    crm_order = models.OneToOneField("core.Order", on_delete=models.PROTECT, null=True, blank=True, related_name="erp_document")
    crm_quotation = models.ForeignKey("core.Quotation", on_delete=models.PROTECT, null=True, blank=True)
    date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    tax_mode = models.CharField(max_length=12, choices=[("inclusive", "GST included"), ("exclusive", "GST extra")], default="inclusive")
    tax_jurisdiction = models.CharField(max_length=15, choices=[("intra", "CGST + SGST"), ("inter", "IGST")], default="intra")
    taxable = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    gross = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    paid = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    revision = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict, blank=True)
    transporter = models.CharField(max_length=150, blank=True)
    vehicle = models.CharField(max_length=50, blank=True)
    driver = models.CharField(max_length=100, blank=True)
    lr_number = models.CharField(max_length=100, blank=True)
    delivery_status = models.CharField(max_length=30, default="pending")
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "number"], name="erp_document_number")]
        indexes = [models.Index(fields=["tenant", "kind", "status", "due_date"])]


class DocumentLine(Record):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="lines")
    source_line = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="allocations")
    order_line = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="order_allocations")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=500)
    unit = models.CharField(max_length=20, default="pcs")
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    rate = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    discount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    taxable = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    gross = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    accepted = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    rejected = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    damaged = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    position = models.PositiveIntegerField(default=0)
    class Meta(Record.Meta):
        ordering = ["position", "id"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0, rate__gte=0, discount__gte=0, tax_rate__gte=0, accepted__gte=0, rejected__gte=0, damaged__gte=0), name="erp_document_line_positive")]


class Job(Record):
    number = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    source_order = models.ForeignKey(Document, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.ForeignKey("core.Company", on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, default="pending")
    priority = models.CharField(max_length=15, choices=[(x, x.title()) for x in ["low", "normal", "high", "urgent"]], default="normal")
    due_date = models.DateField()
    quantity = models.DecimalField(max_digits=20, decimal_places=6, default=1)
    completed_quantity = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    owner = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    instructions = models.TextField(blank=True)
    blocker = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "number"], name="erp_job_number")]


class JobStage(Record):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=100)
    position = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    owner = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    planned = models.DecimalField(max_digits=20, decimal_places=6, default=1)
    completed = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    rejected = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    rework = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    note = models.TextField(blank=True)
    class Meta(Record.Meta):
        ordering = ["position", "id"]


class StockBalance(Record):
    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    bucket = models.CharField(max_length=20, default="available")
    on_hand = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    reserved = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    value = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    class Meta(Record.Meta):
        constraints = [
            models.UniqueConstraint(fields=["tenant", "item", "warehouse", "bucket"], name="erp_stock_key"),
            models.CheckConstraint(condition=Q(on_hand__gte=0, reserved__gte=0) & Q(on_hand__gte=models.F("reserved")), name="erp_nonnegative_available"),
        ]


class StockMovement(Record):
    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    bucket = models.CharField(max_length=20, default="available")
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    unit_cost = models.DecimalField(max_digits=20, decimal_places=6)
    value = models.DecimalField(max_digits=20, decimal_places=6)
    reason = models.CharField(max_length=500)
    date = models.DateField(default=timezone.localdate)
    document_line = models.ForeignKey(DocumentLine, on_delete=models.PROTECT, null=True, blank=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, null=True, blank=True)
    transfer_id = models.UUIDField(null=True, blank=True)
    reversal_of = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True)


class Reservation(Record):
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT)
    order = models.ForeignKey(Document, on_delete=models.PROTECT, null=True, blank=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    consumed = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    status = models.CharField(max_length=20, default="active")


class ExpenseCategory(Record):
    name = models.CharField(max_length=100)
    classification = models.CharField(max_length=15, choices=[("opex", "Operating cost"), ("direct", "Direct cost"), ("unclassified", "Unclassified")], default="opex")
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="erp_expense_category_name")]


class RecurringExpense(Record):
    name = models.CharField(max_length=200)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    frequency = models.CharField(max_length=12, choices=[("monthly", "Monthly"), ("weekly", "Weekly"), ("yearly", "Yearly")], default="monthly")
    next_due = models.DateField()
    anchor_day = models.PositiveSmallIntegerField(default=1)
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True)
    active = models.BooleanField(default=True)


class Expense(Record):
    title = models.CharField(max_length=200)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default="draft")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, null=True, blank=True)
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True)
    recurring_template = models.ForeignKey(RecurringExpense, on_delete=models.PROTECT, null=True, blank=True)
    notes = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="erp_expense_positive"),
            models.UniqueConstraint(fields=["recurring_template", "date"], condition=Q(recurring_template__isnull=False), name="erp_recurring_once"),
        ]


class ManagementFact(Record):
    kind = models.CharField(max_length=15, choices=[("revenue", "Net revenue"), ("direct", "Direct cost"), ("opex", "Operating cost")])
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=80)
    source_key = models.CharField(max_length=180)
    description = models.CharField(max_length=250)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    date = models.DateField()
    category = models.CharField(max_length=100, blank=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.ForeignKey("core.Company", on_delete=models.PROTECT, null=True, blank=True)
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "source_key"], name="erp_fact_source_once")]
        indexes = [models.Index(fields=["tenant", "date", "kind"])]


class Payment(Record):
    direction = models.CharField(max_length=10, choices=[("receipt", "Customer receipt"), ("payment", "Supplier payment")])
    customer = models.ForeignKey("core.Company", on_delete=models.PROTECT, null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    mode = models.CharField(max_length=30, default="NEFT")
    account = models.CharField(max_length=100, default="Main bank")
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, default="posted")
    voided_at = models.DateTimeField(null=True, blank=True)


class PaymentAllocation(Record):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["payment", "document"], name="erp_payment_allocation_key"), models.CheckConstraint(condition=Q(amount__gt=0), name="erp_allocation_positive")]


class Shift(Record):
    name = models.CharField(max_length=100)
    start_time = models.TimeField()
    end_time = models.TimeField()
    grace_minutes = models.PositiveSmallIntegerField(default=10)
    break_minutes = models.PositiveSmallIntegerField(default=30)
    weekly_offs = models.JSONField(default=list, blank=True)


class Employee(Record):
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, null=True, blank=True)
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, null=True, blank=True)
    user = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="erp_profiles")
    manager = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True)
    joining_date = models.DateField(default=timezone.localdate)
    exit_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[(x, x.title()) for x in ["active", "probation", "notice", "exited"]], default="active")
    monthly_salary = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "code"], name="erp_employee_code"), models.UniqueConstraint(fields=["tenant", "user"], condition=Q(user__isnull=False), name="erp_employee_membership")]


class Attendance(Record):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="attendance")
    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=15, choices=[(x, x.replace("_", " ").title()) for x in ["present", "absent", "half_day", "leave", "weekly_off", "holiday"]], default="present")
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    approved_ot_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    locked = models.BooleanField(default=False)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["employee", "date"], name="erp_employee_day"), models.CheckConstraint(condition=Q(approved_ot_hours__gte=0), name="erp_ot_nonnegative")]


class Holiday(Record):
    name = models.CharField(max_length=100)
    date = models.DateField()
    paid = models.BooleanField(default=True)


class LeaveType(Record):
    name = models.CharField(max_length=100)
    paid = models.BooleanField(default=True)
    annual_allowance = models.DecimalField(max_digits=7, decimal_places=2, default=12)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="erp_leave_type_name")]


class LeaveRequest(Record):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    half_day = models.BooleanField(default=False)
    days = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    reason = models.TextField()
    status = models.CharField(max_length=20, default="pending")
    reviewed_by = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="erp_leave_dates")]


class SalaryComponent(Record):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="salary_components")
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=[("earning", "Earning"), ("deduction", "Deduction"), ("employer", "Employer contribution")])
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    prorate = models.BooleanField(default=True)
    effective_from = models.DateField(default=timezone.localdate)
    effective_until = models.DateField(null=True, blank=True)


class EmployeeLoan(Record):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="loans")
    name = models.CharField(max_length=100, default="Salary advance")
    principal = models.DecimalField(max_digits=20, decimal_places=2)
    recovered = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    monthly_recovery = models.DecimalField(max_digits=20, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, default="active")


class PayrollRun(Record):
    name = models.CharField(max_length=150)
    month = models.DateField()
    status = models.CharField(max_length=20, default="draft")
    gross = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    employer_cost = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    input_hash = models.CharField(max_length=64, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant", "month"], name="erp_payroll_month")]


class PayrollResult(Record):
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="results")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)
    payable_days = models.DecimalField(max_digits=7, decimal_places=2)
    gross = models.DecimalField(max_digits=20, decimal_places=2)
    deductions = models.DecimalField(max_digits=20, decimal_places=2)
    net = models.DecimalField(max_digits=20, decimal_places=2)
    employer_cost = models.DecimalField(max_digits=20, decimal_places=2)
    paid = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    components = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    input_snapshot = models.JSONField(default=dict)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["run", "employee"], name="erp_payroll_result_key")]


class SalaryPayment(Record):
    result = models.ForeignKey(PayrollResult, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    reference = models.CharField(max_length=100)
    mode = models.CharField(max_length=30, default="NEFT")


class Task(Record):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    due_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=15, default="normal")
    status = models.CharField(max_length=20, default="open")
    job = models.ForeignKey(Job, on_delete=models.PROTECT, null=True, blank=True)
    checklist = models.JSONField(default=list, blank=True)


class ApprovalRule(Record):
    name = models.CharField(max_length=100)
    resource = models.CharField(max_length=30)
    minimum_amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    required_roles = models.JSONField(default=list)
    allow_self_approval = models.BooleanField(default=False)


class Approval(Record):
    resource_type = models.CharField(max_length=30)
    resource_id = models.UUIDField()
    resource_version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    steps = models.JSONField(default=list)
    current_step = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    allow_self = models.BooleanField(default=False)


class ApprovalDecision(Record):
    approval = models.ForeignKey(Approval, on_delete=models.PROTECT, related_name="decisions")
    step = models.PositiveSmallIntegerField()
    decision = models.CharField(max_length=10)
    note = models.TextField()
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["approval", "step"], name="erp_approval_step_once")]


class Configuration(Record):
    """Versioned safe configuration only; never stores operational ledger data."""
    kind = models.CharField(max_length=30, choices=[(x, x.replace("_", " ").title()) for x in ["workflow", "custom_field", "print_template", "report_schedule", "integration", "saved_filter"]])
    name = models.CharField(max_length=150)
    entity_type = models.CharField(max_length=50, blank=True)
    definition = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="draft")


class Attachment(Record):
    resource_type = models.CharField(max_length=30)
    resource_id = models.CharField(max_length=80)
    name = models.CharField(max_length=200)
    object_key = models.CharField(max_length=300)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    checksum = models.CharField(max_length=64)
    sensitivity = models.CharField(max_length=20, default="business")


class DataJob(Record):
    kind = models.CharField(max_length=20)
    resource = models.CharField(max_length=40)
    status = models.CharField(max_length=20, default="draft")
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    processed = models.PositiveIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)


class PeriodLock(Record):
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField()


class CommandReceipt(models.Model):
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT)
    actor = models.ForeignKey("core.User", on_delete=models.PROTECT)
    key = models.CharField(max_length=100)
    operation = models.CharField(max_length=160)
    request_hash = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "actor", "operation", "key"], name="erp_idempotency_key")]


class OutboxEvent(Record):
    event = models.CharField(max_length=80)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)


class CustomerProfile(Record):
    customer = models.OneToOneField("core.Company", on_delete=models.PROTECT, related_name="erp_profile")
    contact_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    shipping_address = models.TextField(blank=True)
    payment_terms = models.PositiveSmallIntegerField(default=30)
    credit_limit = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    notes = models.TextField(blank=True)


class StockPosition(Record):
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT, related_name="positions")
    bin = models.ForeignKey(WarehouseBin, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["balance", "bin"], name="erp_position_key"),models.CheckConstraint(condition=Q(quantity__gte=0),name="erp_position_nonnegative")]


class PositionMovement(Record):
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT)
    bin = models.ForeignKey(WarehouseBin, on_delete=models.PROTECT)
    movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    reason = models.CharField(max_length=250)


class StockCount(Record):
    name = models.CharField(max_length=150)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="draft")
    reason = models.CharField(max_length=300, default="Physical stock count")


class StockCountLine(Record):
    count = models.ForeignKey(StockCount, on_delete=models.PROTECT, related_name="lines")
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT)
    expected = models.DecimalField(max_digits=20, decimal_places=6)
    counted = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    balance_version = models.PositiveIntegerField()
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["count", "balance"], name="erp_count_line_key")]


class TaskComment(Record):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="comments")
    content = models.TextField()


class RenderedDocument(Record):
    resource_type = models.CharField(max_length=40)
    resource_id = models.CharField(max_length=80)
    source_version = models.PositiveIntegerField()
    template_version = models.PositiveIntegerField(default=0)
    attachment = models.ForeignKey(Attachment, on_delete=models.PROTECT)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["tenant","resource_type","resource_id","source_version","template_version"],name="erp_document_snapshot_key")]


class ApiCredential(Record):
    name = models.CharField(max_length=100)
    user = models.ForeignKey("core.User", on_delete=models.PROTECT, related_name="erp_api_credentials")
    prefix = models.CharField(max_length=20, unique=True)
    key_hash = models.CharField(max_length=64)
    permissions = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True,blank=True)


class WebhookEndpoint(Record):
    name = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    events = models.JSONField(default=list)
    secret_ciphertext = models.TextField()
    active = models.BooleanField(default=True)


class WebhookDelivery(Record):
    endpoint = models.ForeignKey(WebhookEndpoint,on_delete=models.PROTECT)
    event = models.ForeignKey(OutboxEvent,on_delete=models.PROTECT)
    status = models.CharField(max_length=20,default="queued")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    response_code = models.PositiveSmallIntegerField(null=True,blank=True)
    error = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["endpoint","event"],name="erp_webhook_delivery_once")]


class Communication(Record):
    channel = models.CharField(max_length=20,choices=[("email","Email"),("whatsapp","WhatsApp")])
    recipient = models.CharField(max_length=320)
    subject = models.CharField(max_length=200)
    content = models.TextField()
    document = models.ForeignKey(Document,on_delete=models.PROTECT,null=True,blank=True)
    consent_reference = models.CharField(max_length=300)
    status = models.CharField(max_length=20,default="queued")
    provider_id = models.CharField(max_length=100,blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True,blank=True)


class ScheduledExecution(Record):
    configuration = models.ForeignKey(Configuration,on_delete=models.PROTECT)
    occurrence = models.DateField()
    status = models.CharField(max_length=20,default="queued")
    attachment = models.ForeignKey(Attachment,on_delete=models.PROTECT,null=True,blank=True)
    error = models.TextField(blank=True)
    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=["configuration","occurrence"],name="erp_schedule_occurrence_once")]


class LoginOTP(models.Model):
    user = models.ForeignKey("core.User",on_delete=models.CASCADE,related_name="login_otps")
    code_hash = models.CharField(max_length=128)
    otp_token_hash = models.CharField(max_length=64,blank=True,db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    request_ip_hash = models.CharField(max_length=64,blank=True)

    class Meta:
        indexes=[models.Index(fields=["user","consumed_at","expires_at"])]
