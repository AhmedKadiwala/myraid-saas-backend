from django.db import migrations


def explain_roles(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    descriptions = {
        'global-co-owner': "Manage all company operations and staff; the original owner's identity and access remain protected.",
        'global-accountant': 'Manage invoices, receipts, payments and expenses; view customer and order information.',
        'global-sales': 'Manage enquiries, customers, quotations, products and confirmed sales orders.',
        'global-marketing': 'Capture and follow up enquiries and maintain customer contacts.',
        'global-factory-manager': 'Split and assign jobs, record production progress, maintain worker records and dispatch completed work.',
        'global-hr': 'Maintain employee records, attendance and payroll, including private salary details.',
        'global-viewer': 'View the dashboard and reports without changing company records.',
    }
    for code, description in descriptions.items():
        Role.objects.filter(tenant__isnull=True, code=code, description__startswith='Predefined ').update(description=description)


class Migration(migrations.Migration):
    dependencies = [('reference_erp', '0005_roles_and_initial_jobs')]
    operations = [migrations.RunPython(explain_roles, migrations.RunPython.noop)]
