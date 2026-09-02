from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    Advance,
    AuditLog,
    BaseProduct,
    Branch,
    BusinessPermission,
    Client,
    ClientEmail,
    ClientPhone,
    ColourChange,
    Company,
    Deal,
    Description,
    Drawing,
    Invoice,
    Lead,
    Notification,
    NotificationRecipient,
    Order,
    Product,
    Quotation,
    QuotationItem,
    QuotationProduct,
    QuotationWorking,
    Role,
    RolePermission,
    Source,
    Tenant,
    TenantMembership,
    TenantSubscription,
    UsageCounter,
    User,
    UserRole,
)

# Customer/tenant employees. These are staff of the tenant company, NOT Django-admin
# staff. Their access is controlled through TenantMembership + UserRole/RBAC.
MOCK_USERS = [
    ("mock.sales@myraid.local", "Aarav", "Sales", "8888800001", User.Department.SALES, "AS"),
    ("mock.sales2@myraid.local", "Meera", "Shah", "8888800002", User.Department.SALES, "MS"),
    ("mock.drawing@myraid.local", "Kabir", "Drawing", "8888800003", User.Department.DRAWING, "KD"),
    ("mock.factory@myraid.local", "Isha", "Factory", "8888800004", User.Department.FACTORY, "IF"),
    ("mock.accounts@myraid.local", "Rohan", "Accounts", "8888800005", User.Department.ACCOUNTS, "RA"),
]

# Myraid/platform employees. Django's `is_staff` means "may access Django admin";
# it does NOT mean "employee of a tenant". These accounts are deliberately kept
# separate from tenant memberships.
MOCK_PLATFORM_STAFF = [
    ("mock.platformops@myraid.local", "Maya", "Platform Ops", "8888899001", "PO"),
]

ROLE_PERMISSION_CODES = {
    "mock-sales": [
        "lead.view", "lead.add", "lead.edit", "lead.analytics",
        "deal.view", "deal.add", "deal.edit", "deal.analytics",
        "description.add", "description.edit", "meeting.schedule",
        "quotation.view", "quotation.add", "quotation.edit",
        "order.view", "drawing.view",
    ],
    "mock-drawing": [
        "deal.view", "drawing.view", "drawing.upload", "drawing.approve",
        "po.view", "pi.view", "general.view",
    ],
    "mock-factory": ["order.view", "order.edit", "drawing.view", "po.view", "pi.view"],
    "mock-accounts": ["order.view", "order.payment.manage", "billing.view"],
}

SOURCES = ["Website", "IndiaMART", "Referral", "Walk-in", "Trade Expo"]
PRODUCTS = ["Mobile Compactor", "Slotted Angle Rack", "Pallet Rack", "Mezzanine Floor"]
BASE_PRODUCTS = [
    ("Compactor Body", "CMP-BODY", 2100, 900, 450, 4, 3),
    ("Compactor Trolley", "CMP-TROLLEY", 150, 900, 450, 1, 1),
    ("Pallet Upright", "PAL-UPRIGHT", 3000, 120, 80, 2, 1),
    ("Mezzanine Deck Panel", "MEZ-DECK", 1200, 600, 50, 8, 2),
]

COMPANIES = [
    ("Acme Pharma Pvt Ltd", "Andheri East, Mumbai", "27AAECA1234F1Z5", "Nisha", "Patel"),
    ("Northstar Logistics", "Bhiwandi, Thane", "27AABCN5678L1Z2", "Vikram", "Rao"),
    ("BrightBooks Archive", "Prahlad Nagar, Ahmedabad", "24AABCB9132H1Z9", "Heena", "Desai"),
    ("Zenith Auto Components", "Chakan MIDC, Pune", "27AAACZ4412K1Z6", "Arjun", "Menon"),
    ("Carewell Hospitals", "Satellite, Ahmedabad", "24AACCC3344D1Z7", "Farah", "Khan"),
    ("Metro Retail Warehousing", "Madhapur, Hyderabad", "36AACCM7712Q1Z8", "Rahul", "Nair"),
    ("Evergreen Textiles", "Sachin GIDC, Surat", "24AABCE9988M1Z1", "Pooja", "Mehta"),
    ("Prism Legal Archives", "Nariman Point, Mumbai", "27AAACP7234R1Z3", "Sameer", "Joshi"),
]


class Command(BaseCommand):
    help = "Seed a deterministic, broad local CRM dataset for testing frontend flows."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="myraid")
        parser.add_argument("--reset-seeded", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        call_command("bootstrap_saas", tenant_slug=options["tenant_slug"], verbosity=0)
        tenant = Tenant.objects.select_related("plan").get(slug=options["tenant_slug"])
        admin = User.objects.get(email="admin@myraid.local")

        if options["reset_seeded"]:
            self._reset_seeded(tenant)

        branches = self._seed_branches(tenant)
        users = self._seed_users(tenant, branches, admin)
        platform_staff = self._seed_platform_staff()
        source_map = self._seed_lookups(tenant, Source, SOURCES)
        product_map = self._seed_lookups(tenant, Product, PRODUCTS)
        self._seed_base_products(tenant)
        companies, clients = self._seed_companies_and_clients(tenant)
        leads = self._seed_leads(tenant, companies, clients, source_map, product_map, users)
        deals = self._seed_deals(tenant, leads, source_map, product_map, users)
        quotations = self._seed_quotations(tenant, deals, users)
        orders = self._seed_orders(tenant, deals, quotations, users)
        self._seed_descriptions(tenant, leads, deals, users)
        self._seed_drawings(tenant, deals, orders, users)
        self._seed_notifications(tenant, leads, deals, orders, users)
        self._seed_billing(tenant)
        self._seed_audit_and_usage(tenant, admin)

        self.stdout.write(self.style.SUCCESS("Mock CRM data seeded successfully."))
        self.stdout.write(f"Tenant: {tenant.slug}")
        self.stdout.write("Authentication: phone number + OTP only.")
        self.stdout.write(
            "Tenant/company staff phones: " + ", ".join(row[3] for row in MOCK_USERS)
        )
        self.stdout.write(
            "Myraid/platform staff phones: " + ", ".join(row[3] for row in MOCK_PLATFORM_STAFF)
        )
        self.stdout.write(
            f"Created/updated: {len(users)} tenant staff, {len(platform_staff)} platform staff, "
            f"{len(leads)} leads, {len(deals)} deals, "
            f"{len(quotations)} quotations, {len(orders)} orders."
        )

    def _reset_seeded(self, tenant):
        Order.objects.filter(tenant=tenant, order_number__gte=9000).delete()
        Quotation.objects.filter(tenant=tenant, quotation_no__startswith="MOCK-QT-").delete()
        Deal.objects.filter(tenant=tenant, id__startswith="MOCK-DEAL-").delete()
        Lead.objects.filter(tenant=tenant, company__name__in=[row[0] for row in COMPANIES]).delete()
        Company.objects.filter(tenant=tenant, name__in=[row[0] for row in COMPANIES]).delete()
        BaseProduct.objects.filter(tenant=tenant, code__startswith="CMP-").delete()
        BaseProduct.objects.filter(tenant=tenant, code__startswith="PAL-").delete()
        BaseProduct.objects.filter(tenant=tenant, code__startswith="MEZ-").delete()
        Source.objects.filter(tenant=tenant, name__in=SOURCES).delete()
        Product.objects.filter(tenant=tenant, name__in=PRODUCTS).delete()
        User.objects.filter(email__startswith="mock.").delete()

    def _seed_branches(self, tenant):
        branch_data = [
            ("main", "Main Branch", "Ahmedabad HQ"),
            ("mumbai", "Mumbai Sales Office", "Andheri East, Mumbai"),
            ("factory", "Factory Unit", "Sanand GIDC, Ahmedabad"),
        ]
        branches = {}
        for code, name, address in branch_data:
            branch, _ = Branch.objects.update_or_create(
                tenant=tenant, code=code,
                defaults={"name": name, "address": address, "is_active": True},
            )
            branches[code] = branch
        return branches

    def _seed_users(self, tenant, branches, admin):
        users = []
        for email, first, last, phone, department, quotation_code in MOCK_USERS:
            user, created = User.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "phone": phone,
                    "department": department,
                    "quotation_code": quotation_code,
                    "is_active": True,
                    # Tenant employees are application users. They are NOT
                    # Django-admin staff; access is provided by tenant RBAC.
                    "is_staff": False,
                    "is_superuser": False,
                    "platform_admin": False,
                },
            )
            # This project authenticates application users with phone + OTP only.
            # Keep email as required account/contact metadata, but do not create a
            # usable password for seeded staff accounts. A full save also ensures
            # User.save() persists phone_e164 from the seeded phone number.
            user.set_unusable_password()
            user.save()
            default_branch = branches["factory"] if department == User.Department.FACTORY else branches["main"]
            TenantMembership.objects.update_or_create(
                tenant=tenant, user=user,
                defaults={"is_active": True, "is_tenant_admin": False, "default_branch": default_branch},
            )
            role_code = {
                User.Department.SALES: "mock-sales",
                User.Department.DRAWING: "mock-drawing",
                User.Department.FACTORY: "mock-factory",
                User.Department.ACCOUNTS: "mock-accounts",
            }[department]
            role = self._ensure_role(tenant, role_code)
            UserRole.objects.get_or_create(
                tenant=tenant, user=user, role=role, branch=None,
                defaults={"assigned_by": admin},
            )
            users.append(user)
        return users

    def _seed_platform_staff(self):
        """Seed Myraid's own platform/Django-admin staff separately from tenant staff."""
        platform_staff = []

        for email, first, last, phone, quotation_code in MOCK_PLATFORM_STAFF:
            user, _ = User.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "phone": phone,
                    "department": User.Department.ADMIN,
                    "quotation_code": quotation_code,
                    "is_active": True,
                    "is_staff": True,
                    "is_superuser": False,
                    "platform_admin": True,
                },
            )

            # Application authentication remains phone + OTP only.
            user.set_unusable_password()
            user.save()
            platform_staff.append(user)

        return platform_staff

    def _ensure_role(self, tenant, code):
        role, _ = Role.objects.update_or_create(
            tenant=tenant, code=code,
            defaults={
                "name": code.replace("mock-", "").title(),
                "description": "Seeded role for local flow testing",
                "is_system": False,
                "approved_for_tenant_assignment": True,
                "is_active": True,
            },
        )
        permissions = list(BusinessPermission.objects.filter(code__in=ROLE_PERMISSION_CODES[code]))
        RolePermission.objects.bulk_create(
            [
                RolePermission(role=role, permission=permission)
                for permission in permissions
                if not RolePermission.objects.filter(role=role, permission=permission).exists()
            ]
        )
        return role

    def _seed_lookups(self, tenant, model, names):
        objects = {}
        for name in names:
            obj, _ = model.objects.update_or_create(tenant=tenant, name=name, defaults={})
            objects[name] = obj
        return objects

    def _seed_base_products(self, tenant):
        for product_type, code, height, width, depth, per_bay_qty, compartment in BASE_PRODUCTS:
            BaseProduct.objects.update_or_create(
                tenant=tenant, code=code,
                defaults={
                    "product_type": product_type.split()[0],
                    "name": product_type,
                    "default_height": height,
                    "default_width": width,
                    "default_depth": depth,
                    "per_bay_qty": per_bay_qty,
                    "compartment": compartment,
                },
            )

    def _seed_companies_and_clients(self, tenant):
        companies = []
        clients = []
        for index, (company_name, address, gst_no, first_name, last_name) in enumerate(COMPANIES, start=1):
            company, _ = Company.objects.update_or_create(
                tenant=tenant, name=company_name,
                defaults={"address": address, "gst_no": gst_no},
            )
            client, _ = Client.objects.update_or_create(
                tenant=tenant, company=company, first_name=first_name,
                defaults={"last_name": last_name},
            )
            ClientPhone.objects.update_or_create(
                tenant=tenant, client=client, phone=f"77777{index:05d}",
                defaults={},
            )
            ClientEmail.objects.update_or_create(
                tenant=tenant, client=client,
                email=f"{first_name.lower()}.{last_name.lower()}@example.local",
                defaults={},
            )
            companies.append(company)
            clients.append(client)
        return companies, clients

    def _seed_leads(self, tenant, companies, clients, sources, products, users):
        leads = []
        source_list = list(sources.values())
        product_list = list(products.values())
        sales_users = [user for user in users if user.department == User.Department.SALES]
        for index, (company, client) in enumerate(zip(companies, clients), start=1):
            lead, _ = Lead.objects.update_or_create(
                tenant=tenant, company=company,
                defaults={
                    "client_detail": client,
                    "source": source_list[index % len(source_list)],
                    "product": product_list[index % len(product_list)],
                    "is_converted": index <= 5,
                },
            )
            lead.assigned_to.set([sales_users[(index - 1) % len(sales_users)]])
            leads.append(lead)
        return leads

    def _seed_deals(self, tenant, leads, sources, products, users):
        deals = []
        statuses = list(Deal.Status.values)
        sales_users = [user for user in users if user.department == User.Department.SALES]
        source_list = list(sources.values())
        product_list = list(products.values())
        updater = sales_users[0]
        for index, lead in enumerate(leads[:7], start=1):
            deal, _ = Deal.objects.update_or_create(
                tenant=tenant, id=f"MOCK-DEAL-{index:03d}",
                defaults={
                    "deal_status": statuses[index % len(statuses)],
                    "company": lead.company,
                    "client_detail": lead.client_detail,
                    "source": source_list[index % len(source_list)],
                    "product": product_list[index % len(product_list)],
                    "lead": lead,
                    "updated_by": updater,
                },
            )
            deal.assigned_to.set([sales_users[(index - 1) % len(sales_users)]])
            deals.append(deal)
        return deals

    def _seed_quotations(self, tenant, deals, users):
        quotations = []
        creator = users[0]
        for index, deal in enumerate(deals[:5], start=1):
            subtotal = Decimal(85000 + index * 17500)
            gst = (subtotal * Decimal("0.18")).quantize(Decimal("0.01"))
            quotation, _ = Quotation.objects.update_or_create(
                tenant=tenant, quotation_no=f"MOCK-QT-2026-{index:03d}",
                defaults={
                    "deal": deal,
                    "quotation_template": Quotation.Template.SET_WISE if index % 2 else Quotation.Template.ITEM_WISE,
                    "gst": gst,
                    "round_off": Decimal("0.00"),
                    "sub_total": subtotal,
                    "grand_total": subtotal + gst,
                    "show_body_table": True,
                    "note": "Seeded quotation for local testing.",
                    "terms_and_condition": "Payment 50% advance, balance before dispatch.",
                    "specifications": "Powder coated storage system with standard warranty.",
                    "created_by": creator,
                },
            )
            qp, _ = QuotationProduct.objects.update_or_create(
                tenant=tenant, quotation=quotation, name="Storage System",
                defaults={},
            )
            QuotationItem.objects.update_or_create(
                tenant=tenant, quotation_product=qp, item_name="Main Body",
                defaults={
                    "description": "Powder coated fabricated body",
                    "item_code": f"BODY-{index:03d}",
                    "height": 2100,
                    "width": 900,
                    "depth": 450,
                    "provided_rate": Decimal(22000 + index * 1000),
                    "market_rate": Decimal(25000 + index * 1000),
                    "quantity": 4 + index,
                    "per_bay_qty": 2,
                },
            )
            QuotationWorking.objects.update_or_create(
                tenant=tenant, quotation_product=qp,
                defaults={
                    "total_weight": Decimal("185.50"),
                    "ss_material": Decimal("12000.00"),
                    "trolley_material": Decimal("8500.00"),
                    "powder_coating": Decimal("4500.00"),
                    "labour_cost": Decimal("9000.00"),
                    "installation": Decimal("6500.00"),
                    "transport": Decimal("7500.00"),
                    "accomodation": Decimal("3000.00"),
                    "provided_total_cost": Decimal("51000.00"),
                    "market_total_cost": Decimal("62000.00"),
                    "total_body": 5 + index,
                    "metal_rate": "76/kg",
                    "set": 1,
                    "profit_percent": 18,
                    "discount": Decimal("2500.00"),
                },
            )
            quotations.append(quotation)
        return quotations

    def _seed_orders(self, tenant, deals, quotations, users):
        orders = []
        now = timezone.now()
        accounts_user = next(user for user in users if user.department == User.Department.ACCOUNTS)
        for index, quotation in enumerate(quotations[:3], start=1):
            deal = deals[index - 1]
            deal.deal_status = Deal.Status.ORDER_CONFIRMED
            deal.save(update_fields=["deal_status", "updated_at"])
            order, _ = Order.objects.update_or_create(
                tenant=tenant, deal=deal,
                defaults={
                    "quotation": quotation,
                    "order_number": 9000 + index,
                    "dispatch_at": now + timedelta(days=10 + index * 3),
                    "status": list(Order.Status.values)[index % len(Order.Status.values)],
                    "po_number": f"PO-MOCK-{index:03d}",
                    "pi_number": bool(index % 2),
                    "bill_number": f"BILL-MOCK-{index:03d}",
                    "fitted_by": "Myraid installation team",
                    "powder_coating": index % 2 == 0,
                    "count_order": True,
                    "balance": 25000 * index,
                    "height": "2100",
                    "total_body": 6 + index,
                },
            )
            Advance.objects.update_or_create(
                tenant=tenant, order=order,
                defaults={"advance_amount": 50000 * index, "advance_date": now - timedelta(days=index)},
            )
            ColourChange.objects.update_or_create(
                tenant=tenant, order=order, colour="RAL 7035 Light Grey",
                defaults={"user": accounts_user},
            )
            orders.append(order)
        return orders

    def _seed_descriptions(self, tenant, leads, deals, users):
        writer = users[0]
        for index, lead in enumerate(leads[:5], start=1):
            Description.objects.update_or_create(
                tenant=tenant, lead=lead,
                defaults={"notes": f"Mock lead note {index}: client asked for layout and quote.", "updated_by": writer},
            )
        for index, deal in enumerate(deals[:5], start=1):
            Description.objects.update_or_create(
                tenant=tenant, deal=deal,
                defaults={"notes": f"Mock deal note {index}: follow-up scheduled with procurement.", "updated_by": writer},
            )

    def _seed_drawings(self, tenant, deals, orders, users):
        drawing_user = next(user for user in users if user.department == User.Department.DRAWING)
        for index, deal in enumerate(deals[:4], start=1):
            Drawing.objects.update_or_create(
                tenant=tenant, deal=deal, title=f"Mock GA Drawing v{index}",
                defaults={
                    "file_url": f"mock/drawings/deal-{index}.pdf",
                    "upload_type": Drawing.UploadType.DRAWING,
                    "file_type": "application/pdf",
                    "file_size": 512000 + index * 1000,
                    "version": f"v{index}",
                    "status": Drawing.Status.APPROVED if index % 2 else Drawing.Status.PENDING,
                    "approved_at": timezone.now() if index % 2 else None,
                    "note": "Seed drawing placeholder; no real file uploaded.",
                    "uploaded_by": drawing_user,
                    "show_in_order": bool(index % 2),
                },
            )
        for index, order in enumerate(orders, start=1):
            Drawing.objects.update_or_create(
                tenant=tenant, order=order, title=f"Mock PO Copy {index}",
                defaults={
                    "file_url": f"mock/orders/po-{index}.pdf",
                    "upload_type": Drawing.UploadType.PO,
                    "file_type": "application/pdf",
                    "file_size": 256000 + index * 1000,
                    "version": "final",
                    "status": Drawing.Status.APPROVED,
                    "approved_at": timezone.now(),
                    "note": "Seed PO placeholder; no real file uploaded.",
                    "uploaded_by": drawing_user,
                    "show_in_order": True,
                },
            )

    def _seed_notifications(self, tenant, leads, deals, orders, users):
        recipients = users[:3]
        events = [
            ("lead_assigned", "Lead assigned", "You have a new seeded lead.", leads[0], None, None),
            ("deal_assigned", "Deal assigned", "You have a seeded deal to review.", None, deals[0], None),
            ("client_meeting", "Client meeting", "Demo meeting scheduled tomorrow.", leads[1], None, None),
            ("drawing_uploaded", "Drawing uploaded", "Seed drawing uploaded for review.", None, deals[1], None),
            ("color_changed", "Colour changed", "Order colour changed to RAL 7035.", None, deals[2], orders[0] if orders else None),
        ]
        for index, (event_type, title, message, lead, deal, order) in enumerate(events, start=1):
            notification, _ = Notification.objects.update_or_create(
                tenant=tenant, title=f"Mock {title}",
                defaults={
                    "message": message,
                    "send_at": timezone.now() + timedelta(hours=index),
                    "is_sent": index < 3,
                    "type": event_type,
                    "lead": lead,
                    "deal": deal,
                    "order": order,
                },
            )
            for user in recipients:
                NotificationRecipient.objects.update_or_create(
                    tenant=tenant, notification=notification, user=user,
                    defaults={
                        "is_read": user == recipients[0],
                        "read_at": timezone.now() if user == recipients[0] else None,
                        "is_ready": True,
                        "ready_at": timezone.now(),
                    },
                )

    def _seed_billing(self, tenant):
        now = timezone.now()
        if tenant.plan:
            subscription, _ = TenantSubscription.objects.update_or_create(
                tenant=tenant,
                defaults={
                    "plan": tenant.plan,
                    "status": TenantSubscription.Status.ACTIVE,
                    "razorpay_subscription_id": f"sub_mock_{tenant.slug}",
                    "current_period_start": now - timedelta(days=7),
                    "current_period_end": now + timedelta(days=23),
                    "cancel_at_period_end": False,
                },
            )
            for index, status in enumerate([Invoice.Status.PAID, Invoice.Status.ISSUED], start=1):
                Invoice.objects.update_or_create(
                    tenant=tenant, number=f"MOCK-INV-{index:03d}",
                    defaults={
                        "subscription": subscription,
                        "razorpay_invoice_id": f"inv_mock_{tenant.slug}_{index}",
                        "status": status,
                        "amount": Decimal("25000.00") * index,
                        "tax": Decimal("4500.00") * index,
                        "currency": "INR",
                        "due_at": now + timedelta(days=7 * index),
                        "paid_at": now - timedelta(days=2) if status == Invoice.Status.PAID else None,
                    },
                )

    def _seed_audit_and_usage(self, tenant, admin):
        today = timezone.localdate()
        month_start = today.replace(day=1)
        for key, value in {"users": 6, "leads": 8, "deals": 7, "storage_mb": 42}.items():
            UsageCounter.objects.update_or_create(
                tenant=tenant, key=key, period_start=month_start,
                defaults={"value": value, "period_end": month_start + timedelta(days=31)},
            )
        AuditLog.objects.update_or_create(
            tenant=tenant, actor=admin, action="mock.seeded", resource_type="tenant",
            resource_id=str(tenant.pk),
            defaults={"metadata": {"command": "seed_mock_data"}},
        )
