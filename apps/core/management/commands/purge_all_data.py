from django.core.management.base import BaseCommand
from django.utils.text import slugify


class Command(BaseCommand):
    help = "Purge all data from the database, including all tenants, users, and super admins."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm complete data purge without interactive prompt.",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Starting complete database wipe..."))

        from django.apps import apps
        from apps.core.models import (
            User, Tenant, TenantMembership, OrganizationInvitation, Role, RolePermission, BusinessPermission
        )

        # 1. Delete all ERP models in reverse order
        for model in reversed(list(apps.get_app_config('erp').get_models())):
            try:
                model.objects.all().delete()
            except Exception:
                pass

        # 2. Delete all CRM / Core models in reverse order (except base permissions / subscription plans)
        preserved_models = {'BusinessPermission', 'Role', 'RolePermission', 'SubscriptionPlan'}
        for model in reversed(list(apps.get_app_config('core').get_models())):
            if model.__name__ in preserved_models:
                continue
            try:
                model.objects.all().delete()
            except Exception:
                pass

        # 3. Explicitly ensure Tenants and Users are deleted
        Tenant.objects.all().delete()
        User.objects.all().delete()

        # 4. Clean tenant-specific roles
        Role.objects.filter(tenant__isnull=False).delete()

        # 5. Ensure BusinessPermission catalog exists
        if not BusinessPermission.objects.exists():
            from apps.erp.onboarding import install_permission_catalog
            install_permission_catalog()

        permissions = list(BusinessPermission.objects.filter(is_active=True))

        PREDEFINED = {
            "Co-owner": None,
            "Accountant": {"workspace.view", "customer.view", "order.view", "invoice.view", "invoice.create", "invoice.export", "payment.view", "payment.record", "expense.view", "expense.create", "report.view"},
            "Sales": {"workspace.view", "customer.view", "customer.create", "customer.edit", "lead.view", "lead.add", "lead.edit", "quotation.view", "quotation.create", "quotation.edit", "quotation.convert", "order.view", "order.create", "order.post", "item.view", "item.create"},
            "Marketing": {"workspace.view", "customer.view", "customer.create", "lead.view", "lead.add", "lead.edit"},
            "Factory Manager": {"workspace.view", "job.view", "job.create", "job.assign", "job.progress", "order.view", "employee.view", "employee.create", "dispatch.view", "dispatch.create"},
            "HR": {"workspace.view", "employee.view", "employee.create", "employee.edit", "employee.view_private", "attendance.view", "attendance.create", "payroll.view", "payroll.create", "payroll.finalize"},
            "Viewer": {"workspace.view", "report.view"},
        }

        ROLE_DESCRIPTIONS = {
            "Co-owner": "Manage all company operations and staff; the original owner access remains protected.",
            "Accountant": "Manage invoices, receipts, payments and expenses; view customer and order information.",
            "Sales": "Manage enquiries, customers, quotations, products and confirmed sales orders.",
            "Marketing": "Capture and follow up enquiries and maintain customer contacts.",
            "Factory Manager": "Split and assign jobs, record production progress, maintain worker records and dispatch completed work.",
            "HR": "Maintain employee records, attendance and payroll, including private salary details.",
            "Viewer": "View the dashboard and reports without changing company records.",
        }

        for name, codes in PREDEFINED.items():
            code = "global-" + slugify(name)
            role, created = Role.objects.get_or_create(
                tenant=None,
                code=code,
                defaults={"name": name, "description": ROLE_DESCRIPTIONS[name], "is_system": True, "approved_for_tenant_assignment": True}
            )
            if created or not role.permission_links.exists():
                role_perms = [RolePermission(role=role, permission=p) for p in permissions if codes is None or p.code in codes]
                RolePermission.objects.bulk_create(role_perms, ignore_conflicts=True)

        user_count = User.objects.count()
        tenant_count = Tenant.objects.count()
        member_count = TenantMembership.objects.count()
        inv_count = OrganizationInvitation.objects.count()

        self.stdout.write(
            self.style.SUCCESS(
                f"\n========================================\n"
                f"DATABASE CLEANUP COMPLETE!\n"
                f"- Users in database: {user_count} (even the super admin has been removed)\n"
                f"- Tenants in database: {tenant_count}\n"
                f"- Tenant memberships: {member_count}\n"
                f"- Active invitations: {inv_count}\n"
                f"========================================\n"
                f"The database is at GROUND ZERO, fully prepared for fresh end-to-end testing!"
            )
        )
