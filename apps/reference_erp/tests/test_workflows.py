from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from apps.core.models import User, Tenant, TenantMembership, Role, UserRole, BusinessPermission, RolePermission
from apps.core.serializers import TenantSerializer, SubscriptionPlanSerializer
from apps.erp.catalog import FEATURES
from apps.erp.models import Entitlement
from apps.erp.onboarding import create_company_workspace
from apps.reference_erp.models import TenantCharge, TenantPayment, ERPRecord
from apps.reference_erp.reporting import build_statement, period_range, statement_csv
from apps.reference_erp.engine import COLLECTIONS, inclusive_gst, apply

BASE='/api/v1/workspace-ui/'


@override_settings(DEBUG=True)
class FullTrialWorkflowTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user(email='owner@workflow.test',phone='9876501101')
        self.tenant,_,_=create_company_workspace(self.user,'Trial Manufacturing',[])
        self.api=APIClient();self.api.force_authenticate(self.user)
        self.api.credentials(HTTP_X_TENANT_ID=str(self.tenant.pk))

    def command(self,name,fields=None,code='',kind='form'):
        response=self.api.post(BASE+'commands/',{'name':name,'input':fields or {},'id':code,'kind':kind,'request_id':str(uuid4())},format='json')
        self.assertEqual(response.status_code,200,response.data)
        return response.data['result']

    def test_all_modules_free_and_complete_operational_workflow(self):
        self.assertEqual(set(Entitlement.objects.filter(tenant=self.tenant,enabled=True).values_list('feature',flat=True)),set(FEATURES)-{'gst_integrations'})
        self.assertGreater(self.tenant.trial_ends_at,timezone.now()+timedelta(days=6,hours=23))
        workspace=self.api.get(BASE+'workspace/');self.assertEqual(workspace.status_code,200,workspace.data)
        self.assertEqual(set(workspace.data['user']['modules']),{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'})
        customer=self.command('customer',{'name':'Buyer','phone':'9876501102'})
        item=self.command('item',{'name':'Product','sku':'SKU-QA','category':'Finished','unit':'pcs','rate':100000,'cost':3000,'stock':10,'min':1,'hsn':'1234'})
        supplier=self.command('supplier',{'name':'Supplier','phone':'9876501103'})
        purchase=self.command('purchase',{'vendor':supplier['id'],'item':item['id'],'qty':2,'unitCost':3000,'due':'2026-10-20'})
        self.command('receive',{'qty':2},purchase['id'])
        quote=self.command('quote',{'customer':customer['id'],'item':item['id'],'qty':2,'amount':200000,'gst':5})
        order=self.command('quote-convert',code=quote['id'],kind='action')
        job=self.command('job',{'order':order['id'],'stage':'Assembly','priority':'High','assignee':'Operator','due':'2026-10-20'})
        self.command('job-update',{'stage':'Packing','status':'Completed','progress':100,'assignee':'Operator'},job['id'])
        dispatch=self.command('dispatch',{'order':order['id'],'qty':2,'carrier':'Local','tracking':'QA','status':'Dispatched'})
        invoice=self.command('invoice',{'order':order['id'],'date':'2026-10-13','due':'2026-10-20','gst':5})
        saved=ERPRecord.objects.get(tenant=self.tenant,collection='invoices',code=invoice['id']).payload
        self.assertEqual(saved['amount'],200000);self.assertEqual(saved['gst'],5)
        self.command('payment',{'invoice':invoice['id'],'amount':100000,'date':'2026-10-13','mode':'Cash'})
        employee=self.command('employee',{'name':'Employee','department':'Production','salary':10000,'phone':'9876501104'})
        self.command('attendance',{'employee':employee['id'],'date':'2026-10-13','status':'Present'})
        payroll=self.command('payroll',{'period':'2026-10'})
        self.command('payroll-finalize',code=payroll['id'],kind='action')
        self.command('payout',{'kind':'salary','employee':employee['id'],'payee':'Employee','payroll':payroll['id'],'amount':10000,'date':'2026-10-13','mode':'UPI'})
        expense=self.command('expense',{'name':'Rent','category':'Office','amount':5000,'date':'2026-10-13','gst':0,'payment_status':'Unpaid'})
        self.command('payout',{'kind':'expense','expense':expense['id'],'payee':'Landlord','amount':5000,'date':'2026-10-13','mode':'Bank transfer'})
        task=self.command('task',{'title':'Follow up','due':'2026-10-20','owner':'Owner'})
        self.command('task-done',code=task['id'],kind='action')
        approval=self.command('approval',{'title':'Equipment purchase','type':'Purchase','amount':50000})
        self.command('approval-approve',code=approval['id'],kind='action')
        self.assertEqual(ERPRecord.objects.get(tenant=self.tenant,collection='approvals',code=approval['id']).payload['status'],'Approved')
        self.command('user',{'name':'Staff','phone':'9876501110','role':'Sales'})
        self.command('company-save',{'name':'Tenant Brand'},kind='action')
        statement=self.api.get(BASE+'statement/?period=month&date=2026-10-13&full=1')
        self.assertEqual(statement.status_code,200,statement.data)
        self.assertEqual(statement.data['closing_receivables'],100000)
        self.assertEqual(statement.data['totals']['salary_expense'],10000)
        self.assertEqual(statement.data['totals']['operating_expenses'],5000)
        self.assertEqual(statement.data['totals']['cash_received'],100000)
        self.assertEqual(statement.data['totals']['bank_paid'],15000)
        self.assertEqual(statement.data['totals']['cogs'],6000)
        self.assertTrue(dispatch['id'])

    def test_staff_cannot_rename_or_read_subscription_even_with_settings_grant(self):
        self.tenant.agreed_monthly_price=Decimal('35000');self.tenant.save()
        member=TenantMembership.objects.get(tenant=self.tenant,user=self.user);member.is_tenant_admin=False;member.save()
        role=Role.objects.create(tenant=self.tenant,code='admin-assistant',name='Admin assistant')
        # Remove the owner's former assignment, then grant only selected settings rights.
        UserRole.objects.filter(tenant=self.tenant,user=self.user).delete()
        UserRole.objects.create(tenant=self.tenant,user=self.user,role=role)
        for code in ['settings.manage','settings.view','workspace.view','invoice.view','payment.view']:
            permission,_=BusinessPermission.objects.get_or_create(code=code,defaults={'name':code,'module':code.split('.')[0]})
            RolePermission.objects.create(role=role,permission=permission)
        workspace=self.api.get(BASE+'workspace/')
        self.assertNotIn('price',workspace.data['user']);self.assertFalse(workspace.data['user']['can_edit_company'])
        self.assertEqual(self.api.get(BASE+'billing/').status_code,403)
        self.assertEqual(self.api.post(BASE+'commands/',{'kind':'action','name':'company-save','input':{'name':'Unauthorized'},'request_id':str(uuid4())},format='json').status_code,403)
        self.assertNotIn('agreed_monthly_price',TenantSerializer(self.tenant).data)
        self.assertNotIn('price_monthly',SubscriptionPlanSerializer(self.tenant.plan).data)
        self.assertEqual(self.api.get(BASE+'statement/?full=1').status_code,403)

    def test_trial_expires_but_admin_can_read_own_billing(self):
        self.tenant.trial_ends_at=timezone.now()-timedelta(seconds=1);self.tenant.save()
        self.assertEqual(self.api.get(BASE+'workspace/').status_code,403)
        self.assertEqual(self.api.get('/api/v1/tenants/me').status_code,403)
        self.assertEqual(self.api.get(BASE+'billing/').status_code,200)
        self.api.credentials(HTTP_X_TENANT_ID='999999')
        self.assertEqual(self.api.get(BASE+'billing/').status_code,403)

    def test_accounts_invoice_reader_does_not_receive_cost_snapshot_or_salary_totals(self):
        customer=self.command('customer',{'name':'Buyer'})
        item=self.command('item',{'name':'Item','sku':'PRIVATE-COST','rate':100,'cost':45})
        order=self.command('order',{'customer':customer['id'],'item':item['id'],'qty':1,'amount':100,'gst':5,'due':'2026-10-20'})
        self.command('invoice',{'order':order['id'],'date':'2026-10-13','due':'2026-10-20'})
        member=TenantMembership.objects.get(tenant=self.tenant,user=self.user);member.is_tenant_admin=False;member.save()
        UserRole.objects.filter(tenant=self.tenant,user=self.user).delete()
        role=Role.objects.create(tenant=self.tenant,code='accounts-reader',name='Accounts reader')
        UserRole.objects.create(tenant=self.tenant,user=self.user,role=role)
        for code in ['invoice.view','report.view']:
            permission=BusinessPermission.objects.get(code=code);RolePermission.objects.create(role=role,permission=permission)
        workspace=self.api.get(BASE+'workspace/')
        self.assertNotIn('cogs',workspace.data['data']['invoices'][0])
        report=self.api.get(BASE+'statement/?period=month&date=2026-10-13')
        self.assertFalse(report.data['full_statement']);self.assertIsNone(report.data['profit'])
        self.assertIsNone(report.data['cash_movement'])

    def test_migration_enables_existing_unexpired_trial_without_extending_it(self):
        from importlib import import_module
        from django.apps import apps
        expiry=self.tenant.trial_ends_at
        Entitlement.objects.filter(tenant=self.tenant).delete()
        expired=Tenant.objects.create(name='Expired trial',slug='expired-trial',status='trial',trial_ends_at=timezone.now()-timedelta(days=1))
        import_module('apps.reference_erp.migrations.0003_full_access_for_unexpired_trials').enable_full_trials(apps,None)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.trial_ends_at,expiry)
        self.assertEqual(set(Entitlement.objects.filter(tenant=self.tenant,enabled=True).values_list('feature',flat=True)),set(FEATURES)-{'gst_integrations'})
        self.assertFalse(Entitlement.objects.filter(tenant=expired).exists())


@override_settings(DEBUG=True)
class PlatformWorkflowTests(TestCase):
    def setUp(self):
        self.owner=User.objects.create_user(email='platform@workflow.test',phone='9876501201',platform_admin=True)
        self.api=APIClient();self.api.force_authenticate(self.owner)

    def create(self,**kwargs):
        body={'company_name':'Company A','admin_name':'Tenant Admin','phone':'9876501202','access_mode':'trial','free_days':1,**kwargs}
        response=self.api.post(BASE+'platform/tenants/',body,format='json')
        self.assertEqual(response.status_code,201,response.data)
        return Tenant.objects.get(pk=response.data['tenant_id'])

    def test_new_paid_tenant_still_gets_at_least_seven_days_without_charge(self):
        tenant=self.create(access_mode='paid',monthly_price='35000',billing_start=timezone.localdate().isoformat())
        self.assertEqual(tenant.status,'trial')
        self.assertGreater(tenant.trial_ends_at,timezone.now()+timedelta(days=6,hours=23))
        self.assertGreaterEqual(tenant.billing_start_date,timezone.localdate()+timedelta(days=7))
        self.assertEqual(self.api.get(BASE+'platform/').data['tenants'][0]['charges'],[])
        self.assertEqual(Entitlement.objects.filter(tenant=tenant,enabled=True).count(),len(FEATURES)-1)
        tenant.billing_start_date=timezone.localdate()
        tenant.trial_ends_at=timezone.now()+timedelta(hours=1)
        tenant.save()
        self.assertEqual(self.api.get(BASE+'platform/').data['tenants'][0]['charges'],[])

    def test_complimentary_access_and_admin_replacement(self):
        tenant=self.create(access_mode='complimentary')
        self.assertIsNone(tenant.trial_ends_at)
        old=TenantMembership.objects.get(tenant=tenant,is_tenant_admin=True)
        response=self.api.patch(BASE+f'platform/tenants/{tenant.pk}/',{'phone':'9876501203','admin_name':'New Admin'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        old.refresh_from_db();self.assertFalse(old.is_active)
        self.assertTrue(TenantMembership.objects.filter(tenant=tenant,user__phone_e164='+919876501203',is_tenant_admin=True,is_active=True).exists())

    def test_manual_payment_overdue_duplicate_and_cross_tenant_safety(self):
        tenant=self.create();today=timezone.localdate();start=today-timedelta(days=2)
        response=self.api.patch(BASE+f'platform/tenants/{tenant.pk}/',{'access_mode':'paid','monthly_price':'20000','billing_start':start.isoformat(),'due_day':start.day},format='json')
        self.assertEqual(response.status_code,200,response.data)
        charge=response.data['charges'][0]
        body={'request_id':str(uuid4()),'amount':'15000','date':today.isoformat(),'mode':'UPI','reference':'TEST-UTR-1','charge_id':charge['id']}
        response=self.api.post(BASE+f'platform/tenants/{tenant.pk}/payments/',body,format='json')
        self.assertEqual(response.status_code,201,response.data);self.assertEqual(response.data['overdue'],5000)
        self.assertEqual(self.api.post(BASE+f'platform/tenants/{tenant.pk}/payments/',body,format='json').status_code,200)
        self.assertEqual(TenantPayment.objects.count(),1)
        duplicate={**body,'request_id':str(uuid4())}
        self.assertEqual(self.api.post(BASE+f'platform/tenants/{tenant.pk}/payments/',duplicate,format='json').status_code,400)
        other=self.create(company_name='Company B',phone='9876501204')
        crossed={**body,'request_id':str(uuid4()),'reference':'TEST-UTR-2'}
        self.assertEqual(self.api.post(BASE+f'platform/tenants/{other.pk}/payments/',crossed,format='json').status_code,404)
        dashboard=self.api.get(BASE+f'platform/?period=day&date={today}')
        self.assertEqual(dashboard.data['collected'],15000)
        self.assertEqual(dashboard.data['integration']['upi'],'Planned')

    def test_only_platform_owner_can_manage_all_companies(self):
        tenant=self.create();admin=TenantMembership.objects.get(tenant=tenant,is_tenant_admin=True).user
        self.api.force_authenticate(admin)
        self.assertEqual(self.api.get(BASE+'platform/').status_code,403)
        self.assertEqual(self.api.post(BASE+'platform/tenants/',{},format='json').status_code,403)
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(BASE+'platform/').status_code,401)

    @override_settings(DEBUG=False,ERP_OTP_MODE='mock',ERP_SMS_ENABLED=False)
    def test_production_global_owner_awaits_msg91_activation(self):
        self.assertEqual(self.api.get(BASE+'platform/').status_code,403)


class FinancialStatementTests(TestCase):
    def state(self):return {key:[] for key in COLLECTIONS}

    def test_dynamic_inclusive_gst_keeps_total_and_rejects_invalid_rates(self):
        for rate in (0,2.5,5,12.25,18,28):
            split=inclusive_gst(100000,rate)
            self.assertEqual(round(split['taxable']+split['gst_amount'],2),100000)
        self.assertEqual(inclusive_gst(100000,5)['taxable'],95238.1)

    def test_fy_boundaries_zero_gst_filter_and_cash_do_not_change_profit(self):
        state=self.state()
        state['invoices']=[{'id':f'I-{i}','amount':amount,'gst':gst,'cogs':100,'date':day} for i,(amount,gst,day) in enumerate([(100000,5,'2027-04-01'),(100000,0,'2028-03-31'),(999,0,'2027-03-31'),(999,0,'2028-04-01')])]
        state['payments']=[{'id':'R-1','invoice':'I-0','amount':50000,'mode':'Cash','date':'2027-04-01'},{'id':'R-2','invoice':'I-1','amount':25000,'mode':'UPI','date':'2028-03-31'}]
        params={'period':'financial_year','fy_start':'2027'}
        report=build_statement(state,params)
        self.assertEqual((report['from'],report['to']),('2027-04-01','2028-03-31'))
        self.assertEqual(report['totals']['gross_invoiced'],200000)
        self.assertEqual(report['profit'],195038.1)
        self.assertEqual(report['totals']['cash_received'],50000)
        filtered=build_statement(state,{**params,'gst_scope':'taxable'})
        self.assertEqual(filtered['totals'],report['totals'])
        self.assertEqual([r['id'] for r in filtered['transactions']],['I-0','R-1'])
        zero=build_statement(state,{**params,'gst_scope':'zero'})
        self.assertEqual([r['id'] for r in zero['transactions']],['I-1','R-2'])

    def test_expenses_salary_settlements_and_capital_outflows_are_not_double_counted(self):
        state=self.state()
        state['expenses']=[{'id':'E-1','name':'Unpaid rent','amount':10500,'gst':5,'gst_creditable':True,'payment_status':'Unpaid','date':'2027-04-01'}]
        state['payroll']=[{'id':'PR-1','amount':20000,'status':'Finalized','month':'2027-04','period':'Apr 2027'}]
        state['payouts']=[{'id':'P-1','kind':'expense','expense':'E-1','amount':10500,'date':'2027-04-20','mode':'Cash'}, {'id':'P-2','kind':'salary','payroll':'PR-1','amount':15000,'date':'2027-04-30','mode':'UPI'}, {'id':'P-3','kind':'asset_purchase','amount':50000,'date':'2027-04-30','mode':'UPI'}]
        report=build_statement(state,{'period':'month','date':'2027-04-01'})
        self.assertEqual(report['totals']['operating_expenses'],10000)
        self.assertEqual(report['totals']['salary_expense'],20000)
        self.assertEqual(report['profit'],-30000)
        self.assertEqual(report['totals']['cash_paid'],10500)
        self.assertEqual(report['totals']['bank_paid'],65000)
        self.assertEqual(report['expense_payable'],0)
        self.assertEqual(report['payroll_payable'],5000)
        self.assertEqual(report['totals']['other_outflows'],50000)

    def test_period_filters_and_csv_export(self):
        self.assertEqual(period_range({'period':'week','date':'2027-04-04'})[:2],(date(2027,3,29),date(2027,4,4)))
        self.assertEqual(period_range({'period':'day','date':'2027-04-01'})[:2],(date(2027,4,1),date(2027,4,1)))
        self.assertEqual(period_range({'period':'year','date':'2027-04-01'})[:2],(date(2027,1,1),date(2027,12,31)))
        state=self.state();state['expenses']=[{'id':'E','name':'=SUM(1,2)','amount':100,'date':'2027-04-01'}]
        report=build_statement(state,{'period':'custom','from':'2027-04-01','to':'2027-04-01'})
        self.assertIn("'=SUM(1,2)",statement_csv(report))
