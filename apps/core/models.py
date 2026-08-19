from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, username=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Superusers must be staff and superuser")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Department(models.TextChoices):
        SALES = "sales", "Sales"
        ADMIN = "admin", "Admin"
        FACTORY = "factory", "Factory"
        DRAWING = "drawing", "Drawing"
        ACCOUNTS = "accounts", "Accounts"

    username = models.CharField(max_length=254, blank=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, unique=True)
    quotation_code = models.CharField(max_length=30, blank=True, null=True)
    department = models.CharField(
        max_length=20, choices=Department.choices, default=Department.SALES
    )
    platform_admin = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["phone"]
    objects = UserManager()

    def save(self, *args, **kwargs):
        self.username = self.email
        super().save(*args, **kwargs)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SubscriptionPlan(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    code = models.SlugField(unique=True)
    price_monthly = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price_yearly = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="INR")
    user_limit = models.PositiveIntegerField(default=5)
    branch_limit = models.PositiveIntegerField(default=1)
    storage_limit_mb = models.PositiveBigIntegerField(default=1024)
    feature_flags = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)


class Tenant(TimeStampedModel):
    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL)
    plan = models.ForeignKey(
        SubscriptionPlan, null=True, blank=True, on_delete=models.SET_NULL
    )
    razorpay_customer_id = models.CharField(max_length=100, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)


class TenantSettings(models.Model):
    tenant = models.OneToOneField(Tenant, related_name="settings", on_delete=models.CASCADE)
    timezone = models.CharField(max_length=64, default="Asia/Calcutta")
    currency = models.CharField(max_length=3, default="INR")
    date_format = models.CharField(max_length=30, default="dd/MM/yyyy")
    quotation_prefix = models.CharField(max_length=20, default="QT")
    notification_preferences = models.JSONField(default=dict, blank=True)
    branding = models.JSONField(default=dict, blank=True)


class Branch(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, related_name="branches", on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=50)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="unique_branch_code_tenant")
        ]


class TenantMembership(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, related_name="memberships", on_delete=models.CASCADE)
    user = models.ForeignKey(User, related_name="tenant_memberships", on_delete=models.CASCADE)
    is_tenant_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    default_branch = models.ForeignKey(
        Branch, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_tenant_member")
        ]

    def clean(self):
        if self.default_branch_id and self.default_branch.tenant_id != self.tenant_id:
            raise ValidationError("Default branch must belong to the membership tenant.")


class TenantScopedModel(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    class Meta:
        abstract = True


class BusinessPermission(TimeStampedModel):
    code = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    module = models.CharField(max_length=60)
    is_active = models.BooleanField(default=True)


class Role(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, related_name="roles", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(default=False)
    approved_for_tenant_assignment = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="unique_role_code_tenant"
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=Q(tenant__isnull=True),
                name="unique_platform_role_code",
            ),
        ]

    @property
    def is_platform_role(self):
        return self.tenant_id is None


class RolePermission(TimeStampedModel):
    role = models.ForeignKey(Role, related_name="permission_links", on_delete=models.CASCADE)
    permission = models.ForeignKey(
        BusinessPermission, related_name="role_links", on_delete=models.CASCADE
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"], name="unique_role_permission"
            )
        ]


class UserRole(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, related_name="user_roles", on_delete=models.CASCADE)
    user = models.ForeignKey(User, related_name="role_assignments", on_delete=models.CASCADE)
    role = models.ForeignKey(Role, related_name="user_assignments", on_delete=models.CASCADE)
    branch = models.ForeignKey(
        Branch, null=True, blank=True, related_name="user_roles", on_delete=models.CASCADE
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        User, null=True, blank=True, related_name="role_assignments_made",
        on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_from__isnull=True)
                | Q(valid_to__gte=models.F("valid_from")),
                name="role_validity_window",
            ),
            models.UniqueConstraint(
                fields=["tenant", "user", "role", "branch", "valid_from"],
                name="unique_user_role_scope_window",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "user", "branch"]),
            models.Index(fields=["valid_from", "valid_to"]),
        ]

    def clean(self):
        if self.branch_id and self.branch.tenant_id != self.tenant_id:
            raise ValidationError("Branch must belong to the assignment tenant.")
        if self.role.tenant_id not in (None, self.tenant_id):
            raise ValidationError("Role belongs to another tenant.")
        if self.role.tenant_id is None and not self.role.approved_for_tenant_assignment:
            raise ValidationError("This platform role is not approved for tenant assignment.")
        if not TenantMembership.objects.filter(
            tenant_id=self.tenant_id, user_id=self.user_id, is_active=True
        ).exists():
            raise ValidationError("User must be an active member of the tenant.")

    def is_active_at(self, at=None):
        at = at or timezone.now()
        return (
            (self.valid_from is None or self.valid_from <= at)
            and (self.valid_to is None or self.valid_to >= at)
        )


class AuditLog(models.Model):
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, related_name="audit_logs", on_delete=models.CASCADE
    )
    actor = models.ForeignKey(
        User, null=True, blank=True, related_name="audit_events", on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class TenantSubscription(TimeStampedModel):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        AUTHENTICATED = "authenticated", "Authenticated"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    tenant = models.OneToOneField(
        Tenant, related_name="subscription", on_delete=models.CASCADE
    )
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices)
    razorpay_subscription_id = models.CharField(max_length=100, unique=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)


class Invoice(TimeStampedModel):
    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        VOID = "void", "Void"

    tenant = models.ForeignKey(Tenant, related_name="invoices", on_delete=models.CASCADE)
    subscription = models.ForeignKey(
        TenantSubscription, related_name="invoices", on_delete=models.CASCADE
    )
    number = models.CharField(max_length=60)
    razorpay_invoice_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="INR")
    due_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="unique_invoice_tenant")
        ]


class UsageCounter(models.Model):
    tenant = models.ForeignKey(Tenant, related_name="usage", on_delete=models.CASCADE)
    key = models.CharField(max_length=60)
    value = models.PositiveBigIntegerField(default=0)
    period_start = models.DateField()
    period_end = models.DateField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "key", "period_start"], name="unique_usage_period"
            )
        ]


class Company(TenantScopedModel):
    name = models.CharField(max_length=200)
    address = models.TextField()
    gst_no = models.CharField(max_length=30, blank=True, null=True)


class Client(TenantScopedModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    company = models.ForeignKey(Company, related_name="client_details", on_delete=models.CASCADE)


class ClientEmail(TenantScopedModel):
    email = models.EmailField(blank=True, null=True)
    client = models.ForeignKey(Client, related_name="emails", on_delete=models.CASCADE)


class ClientPhone(TenantScopedModel):
    phone = models.CharField(max_length=15)
    client = models.ForeignKey(Client, related_name="phones", on_delete=models.CASCADE)


class Source(TenantScopedModel):
    name = models.CharField(max_length=150)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="unique_source_tenant")
        ]


class Product(TenantScopedModel):
    name = models.CharField(max_length=150)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="unique_product_tenant")
        ]


class Lead(TenantScopedModel):
    company = models.ForeignKey(Company, related_name="leads", on_delete=models.PROTECT)
    client_detail = models.ForeignKey(Client, related_name="leads", on_delete=models.PROTECT)
    source = models.ForeignKey(Source, related_name="leads", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, related_name="leads", on_delete=models.PROTECT)
    is_converted = models.BooleanField(default=False)
    assigned_to = models.ManyToManyField(User, related_name="assigned_leads", blank=True)


class Deal(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DRAWING = "drawing", "Drawing"
        QUOTATION = "quotation", "Quotation"
        HIGH_ORDER_VALUE = "high_order_value", "High order value"
        NEGOTIATION = "negotiation", "Negotiation"
        ORDER_LOST = "order_lost", "Order lost"
        ORDER_CONFIRMED = "order_confirmed", "Order confirmed"

    id = models.CharField(primary_key=True, max_length=80)
    deal_status = models.CharField(max_length=30, choices=Status.choices)
    last_updated = models.DateTimeField(auto_now=True)
    assigned_to = models.ManyToManyField(User, related_name="assigned_deals", blank=True)
    company = models.ForeignKey(Company, related_name="deals", on_delete=models.PROTECT)
    client_detail = models.ForeignKey(Client, related_name="deals", on_delete=models.PROTECT)
    source = models.ForeignKey(Source, related_name="deals", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, related_name="deals", on_delete=models.PROTECT)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, related_name="deals", on_delete=models.SET_NULL
    )
    updated_by = models.ForeignKey(User, related_name="updated_deals", on_delete=models.PROTECT)


class Description(TenantScopedModel):
    lead = models.ForeignKey(
        Lead, null=True, blank=True, related_name="descriptions", on_delete=models.CASCADE
    )
    deal = models.ForeignKey(
        Deal, null=True, blank=True, related_name="descriptions", on_delete=models.CASCADE
    )
    notes = models.TextField()
    updated_by = models.ForeignKey(
        User, related_name="descriptions", on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(lead__isnull=False, deal__isnull=True)
                           | Q(lead__isnull=True, deal__isnull=False)),
                name="description_one_parent",
            )
        ]


class Notification(TenantScopedModel):
    class Type(models.TextChoices):
        COLOR_CHANGED = "color_changed", "Color changed"
        DRAWING_UPLOADED = "drawing_uploaded", "Drawing uploaded"
        DRAWING_APPROVED = "drawing_approved", "Drawing approved"
        DRAWING_REJECTED = "drawing_rejected", "Drawing rejected"
        CLIENT_MEETING = "client_meeting", "Client meeting"
        MENTIONED = "mentioned", "Mentioned"
        LEAD_ASSIGNED = "lead_assigned", "Lead assigned"
        DEAL_ASSIGNED = "deal_assigned", "Deal assigned"
        ADD_QUOTATION = "add_quotation", "Add quotation"

    title = models.CharField(max_length=200)
    message = models.TextField(blank=True, null=True)
    send_at = models.DateTimeField(blank=True, null=True)
    is_sent = models.BooleanField(default=False)
    type = models.CharField(max_length=30, choices=Type.choices)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, related_name="notifications", on_delete=models.CASCADE
    )
    deal = models.ForeignKey(
        Deal, null=True, blank=True, related_name="notifications", on_delete=models.CASCADE
    )
    description = models.ForeignKey(
        Description, null=True, blank=True, related_name="notifications",
        on_delete=models.CASCADE
    )
    order = models.ForeignKey(
        "Order", null=True, blank=True, related_name="notifications", on_delete=models.CASCADE
    )


class NotificationRecipient(TenantScopedModel):
    notification = models.ForeignKey(
        Notification, related_name="recipient_list", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        User, related_name="notification_recipients", on_delete=models.CASCADE
    )
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    is_ready = models.BooleanField(default=False)
    ready_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "user"], name="unique_notification_recipient"
            )
        ]


class BaseProduct(TenantScopedModel):
    product_type = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=100, blank=True, null=True)
    default_height = models.IntegerField(default=0)
    default_width = models.IntegerField(default=0)
    default_depth = models.IntegerField(default=0)
    per_bay_qty = models.IntegerField()
    compartment = models.IntegerField()


class Quotation(TenantScopedModel):
    class Template(models.TextChoices):
        SET_WISE = "set_wise", "Set wise"
        ITEM_WISE = "item_wise", "Item wise"

    quotation_no = models.CharField(max_length=100)
    deal = models.ForeignKey(Deal, related_name="quotations", on_delete=models.CASCADE)
    quotation_template = models.CharField(max_length=20, choices=Template.choices)
    gst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sub_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    show_body_table = models.BooleanField(default=True)
    note = models.TextField(blank=True, null=True)
    terms_and_condition = models.TextField(blank=True, null=True)
    specifications = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, related_name="quotations", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "quotation_no"], name="unique_quotation_no_tenant"
            )
        ]


class QuotationProduct(TenantScopedModel):
    name = models.CharField(max_length=200)
    quotation = models.ForeignKey(
        Quotation, related_name="quotation_products", on_delete=models.CASCADE
    )


class QuotationItem(TenantScopedModel):
    quotation_product = models.ForeignKey(
        QuotationProduct, related_name="quotation_item", on_delete=models.CASCADE
    )
    item_name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    item_code = models.CharField(max_length=100, blank=True, null=True)
    height = models.IntegerField()
    width = models.IntegerField()
    depth = models.IntegerField()
    provided_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    market_rate = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    quantity = models.IntegerField()
    per_bay_qty = models.IntegerField()


class QuotationWorking(TenantScopedModel):
    quotation_product = models.ForeignKey(
        QuotationProduct, related_name="quotation_working", on_delete=models.CASCADE
    )
    total_weight = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    ss_material = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    trolley_material = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    powder_coating = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    labour_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    installation = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    transport = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    accomodation = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    provided_total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    market_total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_body = models.IntegerField()
    metal_rate = models.CharField(max_length=100)
    set = models.IntegerField(default=1)
    profit_percent = models.IntegerField()
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)


class Order(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        DISPATCHED = "dispatched", "Dispatched"
        FABRICATION_READY = "fabrication_ready", "Fabrication ready"
        READY = "ready", "Ready"

    order_number = models.IntegerField()
    dispatch_at = models.DateTimeField()
    status = models.CharField(max_length=30, choices=Status.choices)
    po_number = models.CharField(max_length=100, blank=True, null=True)
    pi_number = models.BooleanField(default=False)
    bill_number = models.CharField(max_length=100, blank=True, null=True)
    fitted_by = models.CharField(max_length=200, blank=True, null=True)
    powder_coating = models.BooleanField(default=False)
    count_order = models.BooleanField(default=False)
    balance = models.IntegerField()
    height = models.CharField(max_length=100)
    total_body = models.IntegerField()
    deal = models.OneToOneField(Deal, related_name="order", on_delete=models.PROTECT)
    quotation = models.OneToOneField(
        Quotation, related_name="order", on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "order_number"], name="unique_order_no_tenant"
            )
        ]


class Advance(TenantScopedModel):
    advance_amount = models.IntegerField()
    advance_date = models.DateTimeField()
    order = models.ForeignKey(Order, related_name="advance", on_delete=models.CASCADE)


class ColourChange(TenantScopedModel):
    colour = models.CharField(max_length=100)
    changed_on = models.DateTimeField(auto_now_add=True)
    order = models.ForeignKey(
        Order, related_name="colour_change", on_delete=models.CASCADE
    )
    user = models.ForeignKey(User, related_name="colour_changes", on_delete=models.PROTECT)


class Drawing(TenantScopedModel):
    class UploadType(models.TextChoices):
        DRAWING = "drawing", "Drawing"
        PO = "po", "PO"
        PI = "pi", "PI"
        GENERAL = "general", "General"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    file_url = models.CharField(max_length=500)
    file = models.FileField(upload_to="drawings/%Y/%m/", blank=True, null=True)
    title = models.CharField(max_length=200)
    upload_type = models.CharField(max_length=20, choices=UploadType.choices)
    file_type = models.CharField(max_length=100)
    file_size = models.PositiveBigIntegerField()
    version = models.CharField(max_length=10, blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    approved_at = models.DateTimeField(blank=True, null=True)
    note = models.TextField(blank=True, null=True)
    deal = models.ForeignKey(
        Deal, null=True, blank=True, related_name="drawings", on_delete=models.CASCADE
    )
    order = models.ForeignKey(
        Order, null=True, blank=True, related_name="drawings", on_delete=models.CASCADE
    )
    uploaded_by = models.ForeignKey(
        User, related_name="drawings", on_delete=models.PROTECT
    )
    show_in_order = models.BooleanField(default=False)

