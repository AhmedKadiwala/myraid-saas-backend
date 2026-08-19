import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import (
    BaseProduct,
    Client,
    ClientEmail,
    ClientPhone,
    Company,
    Product,
    Source,
    Tenant,
    TenantSettings,
    User,
)


class Command(BaseCommand):
    help = (
        "Import an exported legacy CRM JSON file as one tenant. "
        "Re-runs are safe for lookup and user records."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--tenant-name", required=True)
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        source_path = Path(options["file"]).resolve()
        if not source_path.is_file():
            raise CommandError(f"Import file does not exist: {source_path}")
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        tenant, _ = Tenant.objects.get_or_create(
            slug=options["tenant_slug"],
            defaults={"name": options["tenant_name"], "status": Tenant.Status.ACTIVE},
        )
        TenantSettings.objects.get_or_create(tenant=tenant)
        counts = {}
        for model, key in ((Source, "sources"), (Product, "products")):
            counts[key] = 0
            for row in payload.get(key, []):
                model.objects.update_or_create(
                    tenant=tenant, name=row["name"], defaults={}
                )
                counts[key] += 1
        counts["base_products"] = 0
        for row in payload.get("base_products", []):
            BaseProduct.objects.update_or_create(
                tenant=tenant, product_type=row["product_type"], name=row["name"],
                defaults={
                    "code": row.get("code"),
                    "default_height": row.get("default_height", 0),
                    "default_width": row.get("default_width", 0),
                    "default_depth": row.get("default_depth", 0),
                    "per_bay_qty": row.get("per_bay_qty", 1),
                    "compartment": row.get("compartment", 1),
                },
            )
            counts["base_products"] += 1
        counts["users"] = 0
        for row in payload.get("users", []):
            user, created = User.objects.get_or_create(
                email=row["email"],
                defaults={
                    "first_name": row["first_name"], "last_name": row["last_name"],
                    "phone": row["phone"], "department": row["department"],
                    "quotation_code": row.get("quotation_code"),
                },
            )
            if created:
                user.set_unusable_password()
                user.save()
            counts["users"] += 1
        counts["companies"] = 0
        for row in payload.get("companies", []):
            company = Company.objects.create(
                tenant=tenant, name=row["name"], address=row["address"],
                gst_no=row.get("gst_no"),
            )
            for client_row in row.get("client_details", []):
                client = Client.objects.create(
                    tenant=tenant, company=company,
                    first_name=client_row["first_name"],
                    last_name=client_row.get("last_name"),
                )
                ClientEmail.objects.bulk_create([
                    ClientEmail(tenant=tenant, client=client, email=e.get("email"))
                    for e in client_row.get("emails", [])
                ])
                ClientPhone.objects.bulk_create([
                    ClientPhone(tenant=tenant, client=client, phone=p["phone"])
                    for p in client_row.get("phones", [])
                ])
            counts["companies"] += 1
        if options["dry_run"]:
            transaction.set_rollback(True)
        self.stdout.write(json.dumps({
            "tenant": tenant.slug, "counts": counts, "dry_run": options["dry_run"]
        }, indent=2))
