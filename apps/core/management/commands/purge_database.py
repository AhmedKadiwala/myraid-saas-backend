from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


CONFIRMATION_TEXT = "DELETE_ALL_DATA"


class Command(BaseCommand):
    help = (
        "Delete all database rows while preserving the database schema and "
        "migration history. Intended for resetting a development/demo database "
        "before reseeding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f"Required safety confirmation: --confirm {CONFIRMATION_TEXT}",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help=(
                "Required when DEBUG=False. This prevents accidentally wiping "
                "a production-configured database."
            ),
        )

    def handle(self, *args, **options):
        confirmation = options["confirm"]
        allow_production = options["allow_production"]

        if confirmation != CONFIRMATION_TEXT:
            raise CommandError(
                "Database purge cancelled. "
                f"Run again with --confirm {CONFIRMATION_TEXT}"
            )

        if not settings.DEBUG and not allow_production:
            raise CommandError(
                "DEBUG=False. Refusing to purge this database without "
                "--allow-production."
            )

        db = connection.settings_dict
        engine = db.get("ENGINE", "")
        name = db.get("NAME", "")
        host = db.get("HOST", "") or "(local/default)"

        self.stdout.write(self.style.WARNING(""))
        self.stdout.write(self.style.WARNING("DANGER: FULL DATA PURGE"))
        self.stdout.write(self.style.WARNING(f"Database engine: {engine}"))
        self.stdout.write(self.style.WARNING(f"Database name:   {name}"))
        self.stdout.write(self.style.WARNING(f"Database host:   {host}"))
        self.stdout.write(
            self.style.WARNING(
                "All rows will be deleted, including users, OTPs, tenants, "
                "CRM/ERP data, sessions, tokens, and Django admin/superusers."
            )
        )
        self.stdout.write(
            "The schema/tables and migration history will be preserved."
        )

        # Django's flush command deletes application data while leaving the
        # schema and django_migrations intact, which is exactly what we want
        # before running seed_mock_data again.
        call_command(
            "flush",
            interactive=False,
            verbosity=options.get("verbosity", 1),
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Database data purged successfully. Schema and migrations remain."
            )
        )
        self.stdout.write(
            "Next: python manage.py seed_mock_data"
        )
        self.stdout.write(
            "If you need Django Admin access afterward, recreate a superuser "
            "with: python manage.py createsuperuser"
        )
