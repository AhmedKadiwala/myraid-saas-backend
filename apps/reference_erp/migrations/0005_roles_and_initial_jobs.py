from django.db import migrations


def seed(apps, schema_editor):
    PERMISSIONS = {'financial_statement': ['view', 'export'], 'workspace': ['view'], 'item': ['view', 'create', 'edit', 'export'], 'customer': ['view', 'create', 'edit', 'export'], 'supplier': ['view', 'create', 'edit', 'export'], 'warehouse': ['view', 'create', 'edit'], 'stock': ['view', 'view_cost', 'inward', 'issue', 'adjust', 'transfer', 'reserve', 'export', 'reverse'], 'quotation': ['view', 'create', 'edit', 'approve', 'post', 'convert', 'export'], 'order': ['view', 'create', 'edit', 'approve', 'post', 'convert', 'export'], 'purchase': ['view', 'create', 'edit', 'approve', 'post', 'receive', 'convert', 'export'], 'job': ['view', 'create', 'edit', 'assign', 'progress', 'export'], 'dispatch': ['view', 'create', 'edit', 'approve', 'post', 'convert', 'export'], 'invoice': ['view', 'create', 'edit', 'approve', 'post', 'convert', 'void', 'export'], 'payment': ['view', 'record', 'void', 'export'], 'payable': ['view', 'create', 'edit', 'approve', 'post', 'export'], 'expense': ['view', 'create', 'edit', 'approve', 'post', 'void', 'export'], 'profitability': ['view', 'export'], 'employee': ['view', 'create', 'edit', 'view_private', 'export'], 'attendance': ['view', 'create', 'edit', 'checkin', 'approve', 'export'], 'leave': ['view', 'create', 'edit', 'approve', 'export'], 'payroll': ['view', 'create', 'edit', 'approve', 'finalize', 'record_payment', 'export'], 'task': ['view', 'create', 'edit'], 'approval': ['view', 'decide', 'manage'], 'settings': ['view', 'manage'], 'document': ['view', 'upload', 'print', 'share'], 'data': ['import', 'export'], 'report': ['view', 'export']}
    CRM_PERMISSIONS = {'leads': ('lead.view', 'lead.add', 'lead.edit', 'lead.analytics'), 'deals': ('deal.view', 'deal.add', 'deal.edit', 'deal.status.edit', 'deal.analytics'), 'descriptions': ('description.add', 'description.edit', 'description.delete'), 'reminders': ('meeting.schedule',), 'quotations': ('quotation.view', 'quotation.add', 'quotation.edit', 'quotation.delete', 'quotation.copy'), 'orders': ('order.view', 'order.add', 'order.edit', 'order.delete', 'order.payment.manage', 'order.colour.add'), 'drawings': ('drawing.view', 'drawing.upload', 'drawing.approve', 'drawing.delete', 'po.view', 'po.upload', 'pi.view', 'pi.upload', 'general.view', 'general.upload'), 'tenant': ('tenant.manage', 'staff.manage', 'roles.assign', 'audit.view'), 'billing': ('billing.view', 'billing.manage')}
    PREDEFINED = {'Co-owner': None, 'Accountant': {'payment.record', 'order.view', 'invoice.export', 'workspace.view', 'customer.view', 'expense.view', 'invoice.create', 'payment.view', 'expense.create', 'invoice.view', 'report.view'}, 'Sales': {'quotation.create', 'item.create', 'order.create', 'order.post', 'order.view', 'item.view', 'workspace.view', 'customer.view', 'quotation.edit', 'customer.create', 'lead.add', 'quotation.view', 'lead.view', 'quotation.convert', 'lead.edit', 'customer.edit'}, 'Marketing': {'workspace.view', 'customer.view', 'lead.add', 'customer.create', 'lead.view', 'lead.edit'}, 'Factory Manager': {'job.view', 'employee.view', 'order.view', 'employee.create', 'workspace.view', 'dispatch.create', 'job.assign', 'dispatch.view', 'job.create', 'job.progress'}, 'HR': {'employee.create', 'payroll.create', 'workspace.view', 'attendance.view', 'employee.view_private', 'payroll.view', 'employee.view', 'payroll.finalize', 'attendance.create', 'employee.edit'}, 'Viewer': {'report.view', 'workspace.view'}}
    ROLE_DESCRIPTIONS = {'Co-owner': "Manage all company operations and staff; the original owner's identity and access remain protected.", 'Accountant': 'Manage invoices, receipts, payments and expenses; view customer and order information.', 'Sales': 'Manage enquiries, customers, quotations, products and confirmed sales orders.', 'Marketing': 'Capture and follow up enquiries and maintain customer contacts.', 'Factory Manager': 'Split and assign jobs, record production progress, maintain worker records and dispatch completed work.', 'HR': 'Maintain employee records, attendance and payroll, including private salary details.', 'Viewer': 'View the dashboard and reports without changing company records.'}
    from django.utils.text import slugify
    Permission = apps.get_model('core', 'BusinessPermission')
    Role = apps.get_model('core', 'Role')
    Link = apps.get_model('core', 'RolePermission')
    Record = apps.get_model('reference_erp', 'ERPRecord')
    for module, actions in PERMISSIONS.items():
        for action in actions:
            code = f'{module}.{action}'
            Permission.objects.get_or_create(code=code, defaults={'name': code.replace('.', ' ').title(), 'module': module})
    for module, codes in CRM_PERMISSIONS.items():
        for code in codes:
            Permission.objects.get_or_create(code=code, defaults={'name': code.replace('.', ' ').title(), 'module': module})
    for name, codes in PREDEFINED.items():
        role, created = Role.objects.get_or_create(tenant=None, code='global-' + slugify(name), defaults={'name': name, 'description': ROLE_DESCRIPTIONS[name], 'is_system': True})
        if created:
            for p in Permission.objects.all():
                if codes is None or p.code in codes:
                    Link.objects.get_or_create(role=role, permission=p)
    for record in Record.objects.filter(collection='orders').iterator():
        order = record.payload
        jobs = Record.objects.filter(tenant_id=record.tenant_id, collection='jobs', payload__order=record.code)
        for job in jobs:
            data = job.payload
            if 'qty' not in data:
                data.update(qty=order.get('qty', 1), item=order.get('item', ''), worker='', instructions='')
                job.payload = data
                job.save(update_fields=['payload'])
        if not jobs.exists() and order.get('status') in ('Confirmed', 'In production') and not order.get('read_only'):
            codes = Record.objects.filter(tenant_id=record.tenant_id, collection='jobs').values_list('code', flat=True)
            largest = max((int(c.rsplit('-', 1)[-1]) for c in codes if c.rsplit('-', 1)[-1].isdigit()), default=0)
            code = f'JOB-{largest + 1:03d}'
            Record.objects.create(tenant_id=record.tenant_id, collection='jobs', code=code, payload={'id': code, 'order': record.code, 'item': order.get('item', ''), 'qty': order.get('qty', 1), 'stage': 'Planning', 'status': 'Pending', 'progress': 0, 'worker': '', 'assignee': '', 'priority': 'Normal', 'due': order.get('due', ''), 'instructions': ''})


class Migration(migrations.Migration):
    dependencies = [('reference_erp', '0004_companyinvitation_orderattachment')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
