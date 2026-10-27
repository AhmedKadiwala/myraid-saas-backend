import tempfile
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from apps.core.models import User, Tenant, TenantMembership, SubscriptionPlan, Role, UserRole, BusinessPermission, RolePermission
from apps.erp.models import Entitlement
from apps.reference_erp.models import WorkspaceSettings, ERPRecord, CompanyInvitation

BASE = '/api/v1/workspace-ui/'


class ApprovedWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email='owner@approved.test', phone='9876500001', first_name='Factory Owner')
        self.plan = SubscriptionPlan.objects.create(name='Factory', code='full', user_limit=10)
        self.tenant = Tenant.objects.create(name='Factory A', slug='factory-a', status='active', plan=self.plan, owner=self.owner)
        TenantMembership.objects.create(tenant=self.tenant, user=self.owner, is_tenant_admin=True)
        WorkspaceSettings.objects.create(tenant=self.tenant, initialized=True)
        for feature in ('work_orders', 'dispatch', 'hrms'):
            Entitlement.objects.create(tenant=self.tenant, feature=feature, enabled=True)
        self.api = APIClient()
        self.login(self.owner)
        self.customer = self.command('customer', {'name':'Buyer', 'contact':'Customer Person', 'phone':'9876500002'}).data['result']['id']
        self.item = self.command('item', {'name':'Product', 'sku':'PART-1', 'unit':'Nos', 'rate':100}).data['result']['id']

    def login(self, user):
        self.api.force_authenticate(user)
        self.api.credentials(HTTP_X_TENANT_ID=str(self.tenant.pk))

    def command(self, name, fields=None, code='', kind='form', request_id=None):
        return self.api.post(BASE+'commands/', {'name':name, 'input':fields or {}, 'id':code, 'kind':kind, 'request_id':request_id or str(uuid4())}, format='json')

    def order(self, status='Confirmed'):
        response = self.command('order', {'customer':self.customer, 'item':self.item, 'qty':20, 'amount':2000, 'gst':18, 'due':'2026-12-01', 'status':status})
        self.assertEqual(response.status_code,200,response.data)
        return response.data['result']['id']

    def invitation(self, role='Sales', phone='9876500003'):
        response = self.command('user', {'phone':phone, 'role':role})
        self.assertEqual(response.status_code,200,response.data)
        invitation = CompanyInvitation.objects.get(pk=response.data['result']['id'])
        token = parse_qs(urlparse(invitation.communication.content.split('Accept within 7 days: ')[-1]).query)['invite'][0]
        return invitation, token

    def test_confirmation_is_idempotent_and_splitting_preserves_total(self):
        order = self.order('Draft')
        self.assertFalse(ERPRecord.objects.filter(collection='jobs',payload__order=order).exists())
        key = str(uuid4())
        for _ in range(2):
            r = self.command('order-confirm',code=order,kind='action',request_id=key)
            self.assertEqual(r.status_code,200,r.data)
        job = ERPRecord.objects.get(collection='jobs',payload__order=order)
        self.assertEqual(job.payload['qty'],20)
        self.assertEqual(job.payload['assignee'],'')
        split = self.command('job-split',{'qty':8},job.code)
        self.assertEqual(split.status_code,200,split.data)
        jobs = ERPRecord.objects.filter(collection='jobs',payload__order=order)
        self.assertEqual(sorted(j.payload['qty'] for j in jobs),[8,12])
        self.assertEqual(self.command('job-split',{'qty':12},job.code).status_code,400)
        self.assertEqual(sum(j.payload['qty'] for j in jobs),20)
        one = split.data['result']['id']
        r = self.command('job-update',{'stage':'Packing','status':'Completed','progress':100},one)
        self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(ERPRecord.objects.get(collection='orders',code=order).payload['status'],'In production')
        r = self.command('job-update',{'stage':'Packing','status':'Completed','progress':100},job.code)
        self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(ERPRecord.objects.get(collection='orders',code=order).payload['status'],'Ready to dispatch')

    def test_worker_record_has_no_login_and_assignment_cannot_be_bypassed(self):
        order = self.order()
        job = ERPRecord.objects.get(collection='jobs',payload__order=order)
        users = User.objects.count()
        worker = self.command('employee',{'name':'Worker One','phone':'9876500004','department':'Production','role':'Operator','salary':0})
        self.assertEqual(worker.status_code,200,worker.data)
        self.assertEqual(User.objects.count(),users)
        r = self.command('job-assign',{'worker':worker.data['result']['id']},job.code)
        self.assertEqual(r.status_code,200,r.data)
        staff = User.objects.create_user(email='progress@approved.test',phone='9876500005',first_name='Supervisor')
        TenantMembership.objects.create(tenant=self.tenant,user=staff)
        role = Role.objects.create(tenant=self.tenant,name='Line supervisor',code='line-supervisor')
        RolePermission.objects.create(role=role,permission=BusinessPermission.objects.get(code='job.progress'))
        UserRole.objects.create(tenant=self.tenant,user=staff,role=role)
        self.login(staff)
        workspace = self.api.get(BASE+'workspace/')
        self.assertEqual(workspace.data['user']['role'],'Line supervisor')
        self.assertEqual(self.command('job-assign',{'worker':worker.data['result']['id']},job.code).status_code,403)
        self.assertEqual(self.command('job-update',{'stage':'Cutting','status':'In Progress','progress':20,'assignee':'Someone else'},job.code).status_code,400)
        r = self.command('job-update',{'stage':'Cutting','status':'In Progress','progress':20},job.code)
        self.assertEqual(r.status_code,200,r.data)

    def test_dispatch_works_without_inventory_and_only_completed_quantities_ship(self):
        order = self.order()
        self.assertEqual(self.command('dispatch',{'order':order,'qty':1,'carrier':'Truck'}).status_code,400)
        job = ERPRecord.objects.get(collection='jobs',payload__order=order)
        self.command('job-update',{'stage':'Packing','status':'Completed','progress':100},job.code)
        r = self.command('dispatch',{'order':order,'qty':8,'carrier':'Truck'})
        self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(ERPRecord.objects.get(collection='orders',code=order).payload['status'],'Partially dispatched')
        self.assertFalse(ERPRecord.objects.filter(collection='stockMoves').exists())
        self.assertEqual(ERPRecord.objects.get(collection='items',code=self.item).payload['stock'],0)
        self.assertNotIn('inventory',self.api.get(BASE+'workspace/').data['user']['modules'])

    @override_settings(WHATSAPP_API_URL='',WHATSAPP_ACCESS_TOKEN='')
    def test_pending_invitation_grants_no_access_and_requires_matching_verified_phone(self):
        invitation,token = self.invitation()
        staff = User.objects.get(phone_e164=invitation.phone)
        self.assertFalse(TenantMembership.objects.filter(tenant=self.tenant,user=staff).exists())
        self.assertEqual(invitation.communication.status,'not_configured')
        self.assertIn(self.tenant.name,invitation.communication.content)
        self.assertIn('Factory Owner',invitation.communication.content)
        self.login(staff)
        self.assertEqual(self.api.get(BASE+'workspace/').status_code,403)
        self.assertEqual(self.api.post(BASE+'invitations/accept/',{'token':token,'name':'Sales Person'},format='json').status_code,403)
        self.owner.phone_verified_at=timezone.now();self.owner.save(update_fields=['phone_verified_at'])
        self.login(self.owner)
        self.assertEqual(self.api.post(BASE+'invitations/accept/',{'token':token,'name':'Wrong Person'},format='json').status_code,403)
        staff.phone_verified_at=timezone.now();staff.save(update_fields=['phone_verified_at'])
        self.login(staff)
        accepted = self.api.post(BASE+'invitations/accept/',{'token':token,'name':'Sales Person'},format='json')
        self.assertEqual(accepted.status_code,200,accepted.data)
        self.assertEqual(self.api.get(BASE+'workspace/').data['user']['role'],'Sales')
        self.assertEqual(self.api.post(BASE+'invitations/accept/',{'token':token,'name':'Sales Person'},format='json').status_code,404)

    def test_coowner_cannot_deactivate_or_demote_owner(self):
        inv,token = self.invitation('Co-owner')
        staff = User.objects.get(phone_e164=inv.phone)
        staff.phone_verified_at=timezone.now();staff.save(update_fields=['phone_verified_at'])
        self.login(staff)
        accepted = self.api.post(BASE+'invitations/accept/',{'token':token,'name':'Factory Manager'},format='json')
        self.assertEqual(accepted.status_code,200,accepted.data)
        self.assertEqual(self.api.get(BASE+'workspace/').data['user']['role'],'Co-owner')
        owner_member = TenantMembership.objects.get(tenant=self.tenant,user=self.owner)
        self.assertEqual(self.command('user-toggle',code=f'U-{owner_member.pk:03d}',kind='action').status_code,400)
        sales = Role.objects.get(code='global-sales')
        self.assertEqual(self.api.post(BASE+f'members/{owner_member.pk}/role/',{'role_id':sales.pk},format='json').status_code,400)
        self.assertEqual(self.api.post(f'/api/v1/auth/edit-user/{self.owner.pk}',{'phone':'9876599999'},format='json').status_code,403)

    def test_company_roles_are_isolated_and_permission_ids_are_validated(self):
        p = BusinessPermission.objects.get(code='job.assign')
        role = self.api.post(BASE+'roles/',{'name':'Job Allocator','description':'Assign factory jobs','permissions':[p.pk]},format='json')
        self.assertEqual(role.status_code,201,role.data)
        foreign = Tenant.objects.create(name='Other',slug='other')
        foreign_role = Role.objects.create(tenant=foreign,name='Other role',code='other-role')
        fake_owner = Role.objects.create(tenant=self.tenant,name='Owner',code='pretend-owner')
        self.assertEqual(self.command('user',{'phone':'9876500003','role_id':fake_owner.pk}).status_code,400)
        self.assertEqual(self.command('user',{'phone':'9876500003','role_id':foreign_role.pk}).status_code,400)
        self.assertEqual(self.command('user',{'phone':'9876500003','role_id':'invalid'}).status_code,400)
        self.assertEqual(self.api.post(BASE+'roles/',{'name':'Invalid','permissions':[999999]},format='json').status_code,400)
        self.assertEqual(self.api.post(BASE+'roles/',{'id':Role.objects.get(code='global-sales').pk,'name':'Changed','permissions':[]},format='json').status_code,400)

    def test_pdf_is_private_and_shared_with_job_without_copying(self):
        order = self.order()
        with tempfile.TemporaryDirectory() as directory, override_settings(ERP_PRIVATE_MEDIA_ROOT=directory):
            r = self.api.post(BASE+f'orders/{order}/attachments/',{'file':SimpleUploadedFile('customer-po.pdf',b'%PDF-1.4\n%%EOF',content_type='application/pdf')},format='multipart')
            self.assertEqual(r.status_code,201,r.data)
            workspace = self.api.get(BASE+'workspace/').data['data']
            self.assertEqual(workspace['orders'][0]['attachments'][0]['name'],'customer-po.pdf')
            response = self.api.get(BASE+f'attachments/{r.data["id"]}/download/')
            self.assertEqual(response.status_code,200)
            self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF-'))
            self.assertEqual(self.api.post(BASE+f'orders/{order}/attachments/',{'file':SimpleUploadedFile('fake.pdf',b'not a pdf')},format='multipart').status_code,400)
            other = Tenant.objects.create(name='Other',slug='other',status='active')
            TenantMembership.objects.create(tenant=other,user=self.owner,is_tenant_admin=True)
            self.api.credentials(HTTP_X_TENANT_ID=str(other.pk))
            self.assertEqual(self.api.get(BASE+f'attachments/{r.data["id"]}/download/').status_code,404)

    def test_phone_is_required_for_people_and_customer_creation_does_not_win_sale(self):
        self.assertEqual(self.command('employee',{'name':'No phone','department':'Production'}).status_code,400)
        self.assertEqual(self.command('customer',{'name':'No phone'}).status_code,400)
        lead = self.command('lead',{'name':'Prospect','contact':'Prospect Person','phone':'9876500008','stage':'Qualified','value':100})
        self.assertEqual(lead.status_code,200,lead.data)
        code = lead.data['result']['id']
        self.assertEqual(self.command('lead-convert',code=code,kind='action').status_code,200)
        self.assertEqual(ERPRecord.objects.get(collection='leads',code=code).payload['stage'],'Qualified')
        self.assertEqual(self.command('lead-update',{'stage':'Won'},code).status_code,400)

    def test_cancel_and_closed_quote_cannot_be_invoiced_or_converted(self):
        order = self.order()
        self.assertEqual(self.command('order-cancel',code=order,kind='action').status_code,200)
        self.assertEqual(self.command('invoice-from-order',code=order,kind='action').status_code,400)
        self.assertEqual(ERPRecord.objects.get(collection='jobs',payload__order=order).payload['status'],'Cancelled')
        job = ERPRecord.objects.get(collection='jobs',payload__order=order)
        self.assertEqual(self.command('job-update',{'stage':'Cutting','status':'In Progress','progress':1},job.code).status_code,400)
        quote = self.command('quote',{'customer':self.customer,'item':self.item,'qty':20,'amount':2000,'gst':18})
        code = quote.data['result']['id']
        self.assertEqual(self.command('quote-status',{'status':'Rejected'},code,kind='action').status_code,200)
        self.assertEqual(self.command('quote-convert',code=code,kind='action').status_code,400)
