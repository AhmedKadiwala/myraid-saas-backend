from django.core.management.base import BaseCommand
from django.db import transaction
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.core.models import (
    Branch,
    BusinessPermission,
    Role,
    RolePermission,
    SubscriptionPlan,
    Tenant,
    TenantMembership,
    TenantSettings,
    User,
    UserRole,
)

PERMISSIONS = {
    "leads": ("lead.view", "lead.add", "lead.edit", "lead.analytics"),
    "deals": ("deal.view", "deal.add", "deal.edit", "deal.status.edit", "deal.analytics"),
    "descriptions": ("description.add", "description.edit", "description.delete"),
    "reminders": ("meeting.schedule",),
    "quotations": (
        "quotation.view", "quotation.add", "quotation.edit",
        "quotation.delete", "quotation.copy",
    ),
    "orders": (
        "order.view", "order.add", "order.edit", "order.delete",
        "order.payment.manage", "order.colour.add",
    ),
    "drawings": (
        "drawing.view", "drawing.upload", "drawing.approve", "drawing.delete",
        "po.view", "po.upload", "pi.view", "pi.upload", "general.view", "general.upload",
    ),
    "tenant": ("tenant.manage", "staff.manage", "roles.assign", "audit.view"),
    "billing": ("billing.view", "billing.manage"),
}


class Command(BaseCommand):
    help = "Create idempotent local plan, first tenant, platform permissions and admin."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-name", default="Myraid CRM")
        parser.add_argument("--tenant-slug", default="myraid")
        parser.add_argument("--admin-email", default="admin@myraid.local")
        parser.add_argument("--admin-phone", default="9999999999")
        parser.add_argument("--admin-password", default="ChangeMe123!")

    @transaction.atomic
    def handle(self, *args, **options):
        plan, _ = SubscriptionPlan.objects.update_or_create(
            code="local-pro",
            defaults={
                "name": "Local Pro", "price_monthly": 0, "price_yearly": 0,
                "user_limit": 100, "branch_limit": 20,
                "storage_limit_mb": 10240,
                "feature_flags": {"all_crm_modules": True},
            },
        )
        tenant, _ = Tenant.objects.get_or_create(
            slug=options["tenant_slug"],
            defaults={
                "name": options["tenant_name"], "plan": plan,
                "status": Tenant.Status.ACTIVE,
            },
        )
        TenantSettings.objects.get_or_create(tenant=tenant)
        branch, _ = Branch.objects.get_or_create(
            tenant=tenant, code="main", defaults={"name": "Main Branch"}
        )
        user, created = User.objects.get_or_create(
            email=options["admin_email"],
            defaults={
                "first_name": "Tenant", "last_name": "Admin",
                "phone": options["admin_phone"], "department": User.Department.ADMIN,
            },
        )
        if created:
            user.set_password(options["admin_password"])
            user.save()
        membership, _ = TenantMembership.objects.update_or_create(
            tenant=tenant, user=user,
            defaults={
                "is_tenant_admin": True, "is_active": True,
                "default_branch": branch,
            },
        )
        permission_objects = []
        for module, codes in PERMISSIONS.items():
            for code in codes:
                permission, _ = BusinessPermission.objects.update_or_create(
                    code=code,
                    defaults={
                        "name": code.replace(".", " ").title(),
                        "module": module,
                        "is_active": True,
                    },
                )
                permission_objects.append(permission)
        role, _ = Role.objects.get_or_create(
            tenant=tenant, code="tenant-admin",
            defaults={"name": "Tenant Admin", "is_system": True},
        )
        RolePermission.objects.bulk_create(
            [
                RolePermission(role=role, permission=permission)
                for permission in permission_objects
                if not RolePermission.objects.filter(
                    role=role, permission=permission
                ).exists()
            ]
        )
        UserRole.objects.filter(tenant=tenant,user=user,is_active=True).exclude(role=role).update(is_active=False)
        assignment, _ = UserRole.objects.get_or_create(
            tenant=tenant, user=user, role=role, branch=None,
            defaults={"assigned_by": user},
        )
        if not assignment.is_active:
            assignment.is_active=True
            assignment.save(update_fields=["is_active","updated_at"])
        every_minute, _ = IntervalSchedule.objects.get_or_create(
            every=1, period=IntervalSchedule.MINUTES
        )
        hourly, _ = IntervalSchedule.objects.get_or_create(
            every=1, period=IntervalSchedule.HOURS
        )
        PeriodicTask.objects.update_or_create(
            name="Dispatch due CRM notifications",
            defaults={
                "interval": every_minute,
                "task": "apps.core.tasks.dispatch_due_notifications",
                "enabled": True,
            },
        )
        PeriodicTask.objects.update_or_create(
            name="Enforce subscription renewal status",
            defaults={
                "interval": hourly,
                "task": "apps.core.tasks.enforce_subscription_statuses",
                "enabled": True,
            },
        )
        PeriodicTask.objects.update_or_create(name="Process ERP outbox",defaults={"interval":every_minute,"task":"apps.erp.tasks.process_erp_outbox","enabled":True})
        PeriodicTask.objects.update_or_create(name="Generate due ERP recurring expenses",defaults={"interval":hourly,"task":"apps.erp.tasks.generate_due_recurring_expenses","enabled":True})
        PeriodicTask.objects.update_or_create(name="Run ERP scheduled reports",defaults={"interval":hourly,"task":"apps.erp.tasks.run_erp_schedules","enabled":True})
        PeriodicTask.objects.update_or_create(name="Deliver ERP webhooks",defaults={"interval":every_minute,"task":"apps.erp.tasks.deliver_erp_webhooks","enabled":True})
        PeriodicTask.objects.update_or_create(name="Purge expired Myraid login OTPs",defaults={"interval":hourly,"task":"apps.erp.tasks.purge_expired_login_otps","enabled":True})
        self.stdout.write(self.style.SUCCESS(
            f"Bootstrapped tenant={tenant.slug} admin={user.email} "
            f"tenant_id={tenant.pk}"
        ))
