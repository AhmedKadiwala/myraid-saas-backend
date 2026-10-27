"""Explicit, repeatable sample company; never seed into a customer's company."""
import json
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Tenant, User
from apps.erp.onboarding import create_company_workspace
from apps.erp.otp import normalize_phone
from apps.reference_erp.engine import COLLECTIONS
from apps.reference_erp.models import AuditEvent, ERPRecord, WorkspaceSettings


class Command(BaseCommand):
    help = "Create a separate demo owner and fictional company with a full seven-day trial."

    def add_arguments(self, parser):
        parser.add_argument("--phone", default="9999900002")

    @transaction.atomic
    def handle(self, *args, **options):
        phone = normalize_phone(options["phone"])
        slug = "myraid-owner-demo"
        existing = Tenant.objects.filter(slug=slug).first()
        if existing:
            workspace = WorkspaceSettings.objects.filter(tenant=existing).first()
            if not workspace or workspace.import_report.get("seed") != "owner-demo-v1":
                raise CommandError("The demo slug belongs to another company; no data was changed.")
            if not existing.memberships.filter(user__phone_e164=phone, is_tenant_admin=True).exists():
                raise CommandError("The demo belongs to a different phone; no data was changed.")
            self.stdout.write(f"Demo already exists; kept your changes and trial expiry. tenant_id={existing.pk}")
            return
        if User.objects.filter(Q(phone_e164=phone) | Q(phone=phone[-10:])).exists():
            raise CommandError("Choose a separate, unused demo-owner phone; no existing identity was changed.")
        user = User.objects.create_user(email=f"owner-demo-{phone[1:]}@myraid.invalid", phone=phone,
                                        first_name="Demo", last_name="Owner", department="admin")
        tenant, expiry, _ = create_company_workspace(user, "Meridian Seals · Demo", [])
        tenant.slug = slug
        tenant.save(update_fields=["slug"])
        sample = json.loads((Path(__file__).resolve().parents[2] / "fixtures/demo_workspace.json").read_text(encoding="utf-8"))
        shift = timezone.localdate() - date(2026, 10, 13)

        def current_dates(value):
            if isinstance(value, dict):
                return {k: current_dates(v) for k, v in value.items()}
            if isinstance(value, list):
                return [current_dates(v) for v in value]
            if isinstance(value, str) and len(value) == 10:
                try:
                    return (date.fromisoformat(value) + shift).isoformat()
                except ValueError:
                    pass
            return value

        sample = current_dates(sample)
        previous_month = timezone.localdate().replace(day=1) - timedelta(days=1)
        sample["payroll"][0].update(period=previous_month.strftime("%b %Y"), month=previous_month.strftime("%Y-%m"), date=previous_month.isoformat())
        items = {r["id"]: r for r in sample["items"]}
        orders = {r["id"]: r for r in sample["orders"]}
        for invoice in sample["invoices"]:
            order = orders.get(invoice["order"])
            if order:
                invoice.update(item=order["item"], qty=order["qty"],
                               cogs=items[order["item"]]["cost"] * order["qty"], cost_basis="snapshot")
            else:
                invoice.update(item="SKU-001", qty=20, cogs=78000, cost_basis="snapshot")
        sample["expenses"][0].update(payment_status="Unpaid", gst=0)
        sample["payouts"] = [{"id": "OUT-001", "kind": "expense", "expense": "EXP-001",
                              "payee": "Demo factory landlord", "amount": 48000, "mode": "Bank transfer",
                              "date": timezone.localdate().isoformat(), "note": "Fictional rent settlement"}]
        WorkspaceSettings.objects.create(tenant=tenant, initialized=True,
            company_meta={**sample["company"], "name": tenant.name}, import_report={"seed": "owner-demo-v1"})
        records = [ERPRecord(tenant=tenant, collection=collection, code=row["id"], payload=row)
                   for collection in COLLECTIONS if collection != "users" for row in sample.get(collection, [])]
        ERPRecord.objects.bulk_create(records)
        AuditEvent.objects.create(tenant=tenant, user=user, message="Fictional demo records added for module testing.")
        self.stdout.write(self.style.SUCCESS(f"Demo ready: phone={phone}, company={tenant.name}, "
                                            f"tenant_id={tenant.pk}, records={len(records)}, trial_ends_at={expiry.isoformat()}"))
