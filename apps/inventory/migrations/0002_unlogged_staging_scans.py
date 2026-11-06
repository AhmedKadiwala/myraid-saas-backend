from django.db import migrations


def set_unlogged_postgres(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE IF EXISTS erp_staging_scans SET UNLOGGED;")


def set_logged_postgres(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE IF EXISTS erp_staging_scans SET LOGGED;")


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(
            code=set_unlogged_postgres,
            reverse_code=set_logged_postgres,
        ),
    ]
