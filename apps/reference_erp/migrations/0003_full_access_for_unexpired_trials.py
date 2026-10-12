from django.db import migrations
from django.utils import timezone


TRIAL_FEATURES = ('purchase','inventory','work_orders','production_tracking','dispatch','hrms','attendance_hr','payroll','expense_management','profitability','approvals','tasks','crm_advanced','communications','multi_branch','multi_warehouse','custom_fields','custom_workflows','custom_templates','advanced_analytics','scheduled_reports','tally_integration','api_access')


def enable_full_trials(apps,schema_editor):
    Tenant=apps.get_model('core','Tenant')
    Plan=apps.get_model('core','SubscriptionPlan')
    Entitlement=apps.get_model('erp','Entitlement')
    plan,_=Plan.objects.get_or_create(code='erp-full-trial',defaults={'name':'Full ERP access','price_monthly':0,'user_limit':50,'branch_limit':5,'storage_limit_mb':1024})
    for tenant in Tenant.objects.filter(status='trial',trial_ends_at__gt=timezone.now()):
        tenant.plan=plan;tenant.save(update_fields=['plan'])
        for feature in TRIAL_FEATURES:
            Entitlement.objects.update_or_create(tenant=tenant,feature=feature,defaults={'enabled':True,'expires_at':tenant.trial_ends_at,'reason':'All-module trial; original expiry retained'})


class Migration(migrations.Migration):
    dependencies=[('reference_erp','0002_tenantcharge_tenantpayment_and_more')]
    operations=[migrations.RunPython(enable_full_trials,migrations.RunPython.noop)]
