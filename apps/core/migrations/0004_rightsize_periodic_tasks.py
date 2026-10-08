from django.db import migrations


def rightsize_periodic_tasks(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_five_minutes, _ = IntervalSchedule.objects.get_or_create(
        every=5,
        period="minutes",
    )
    daily, _ = IntervalSchedule.objects.get_or_create(
        every=1,
        period="days",
    )
    monthly, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="1",
        month_of_year="*",
    )

    def schedule_task(name, task, *, interval=None, crontab=None, enabled=False):
        PeriodicTask.objects.update_or_create(
            name=name,
            defaults={
                "task": task,
                "interval": interval,
                "crontab": crontab,
                "solar": None,
                "clocked": None,
                "enabled": enabled,
            },
        )

    PeriodicTask.objects.filter(name="Run low-cost SaaS maintenance").delete()
    schedule_task(
        "Dispatch due CRM notifications",
        "apps.core.tasks.dispatch_due_notifications",
        interval=every_five_minutes,
        enabled=False,
    )
    schedule_task(
        "Enforce subscription renewal status",
        "apps.core.tasks.enforce_subscription_statuses",
        interval=daily,
        enabled=False,
    )
    schedule_task(
        "Process ERP outbox",
        "apps.erp.tasks.process_erp_outbox",
        interval=every_five_minutes,
        enabled=False,
    )
    schedule_task(
        "Generate due ERP recurring expenses",
        "apps.erp.tasks.generate_due_recurring_expenses",
        interval=daily,
        enabled=True,
    )
    schedule_task(
        "Run ERP scheduled reports",
        "apps.erp.tasks.run_erp_schedules",
        interval=daily,
        enabled=True,
    )
    schedule_task(
        "Deliver ERP webhooks",
        "apps.erp.tasks.deliver_erp_webhooks",
        interval=every_five_minutes,
        enabled=False,
    )
    schedule_task(
        "Purge expired Myraid login OTPs",
        "apps.erp.tasks.purge_expired_login_otps",
        crontab=monthly,
        enabled=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        ("core", "0003_userrole_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(rightsize_periodic_tasks, migrations.RunPython.noop),
    ]
