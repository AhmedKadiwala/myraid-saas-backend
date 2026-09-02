"""Explicit synthetic local workspace. Never updates the existing CRM tenant."""
import calendar
from datetime import date, timedelta, time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import User, Tenant, TenantMembership, Branch, TenantSettings, Role, RolePermission, BusinessPermission, UserRole, Company
from apps.core.management.commands.bootstrap_saas import PERMISSIONS as CRM_PERMISSIONS
from apps.erp import models as m, services as svc, workforce
from apps.erp.catalog import FEATURES, PERMISSIONS
from apps.erp.money import calculate_line


class Command(BaseCommand):
    help = "Create an isolated ERP demo company with synthetic operational data. DEBUG only."
    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)
        parser.add_argument("--email", default="owner@myraid.example")
        parser.add_argument("--phone", default="9999900001")
        parser.add_argument("--allow-production-demo", action="store_true")

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["allow_production_demo"]:
            raise CommandError("Demo data is only allowed in DEBUG unless --allow-production-demo is passed explicitly.")
        tenant, created = Tenant.objects.get_or_create(slug="myraid-erp-demo",defaults={"name":"Myraid Industries","status":"active"})
        branch,_=Branch.objects.get_or_create(tenant=tenant,code="main",defaults={"name":"Pune Works","address":"Industrial Estate, Pune"})
        TenantSettings.objects.get_or_create(tenant=tenant)
        user,user_created=User.objects.get_or_create(email=opts["email"],defaults={"first_name":"Ahmed","last_name":"Kadiwala","phone":opts["phone"],"department":"admin"})
        if not user_created and user.phone != opts["phone"]:
            user.phone = opts["phone"]
            user.save(update_fields=["phone", "phone_e164"])
        if user_created:user.set_password(opts["password"]);user.save()
        TenantMembership.objects.get_or_create(tenant=tenant,user=user,defaults={"is_tenant_admin":True,"default_branch":branch})
        role,_=Role.objects.get_or_create(tenant=tenant,code="tenant-admin",defaults={"name":"ERP Owner","is_system":True})
        for group,actions in PERMISSIONS.items():
            for action in actions:
                code=f"{group}.{action}";permission,_=BusinessPermission.objects.get_or_create(code=code,defaults={"name":code.replace("."," ").title(),"module":group})
                RolePermission.objects.get_or_create(role=role,permission=permission)
        for module,codes in CRM_PERMISSIONS.items():
            for code in codes:
                permission,_=BusinessPermission.objects.get_or_create(code=code,defaults={"name":code,"module":module})
                RolePermission.objects.get_or_create(role=role,permission=permission)
        UserRole.objects.filter(tenant=tenant,user=user,is_active=True).exclude(role=role).update(is_active=False)
        assignment,_=UserRole.objects.get_or_create(tenant=tenant,user=user,role=role,branch=None,defaults={"assigned_by":user})
        if not assignment.is_active:assignment.is_active=True;assignment.save(update_fields=["is_active","updated_at"])
        for feature in FEATURES:
            if feature!="gst_integrations":m.Entitlement.objects.get_or_create(tenant=tenant,feature=feature,defaults={"enabled":True,"reason":"Explicit local demonstration grant","changed_by":user})
        settings_obj,_=m.ErpSettings.objects.get_or_create(tenant=tenant,defaults={"legal_name":"Myraid Industries","address":"Industrial Estate, Pune","switches":{"demo":True},"expected_expense_categories":["Payroll","Rent / lease","Utilities"]})
        settings_obj.legal_name="Myraid Industries"
        settings_obj.gstin="27AABCM1234F1Z5"
        settings_obj.address="Industrial Estate, Pune, Maharashtra"
        settings_obj.expected_expense_categories=["Payroll","Rent / lease","Utilities","Materials","Contract labour","Transport"]
        settings_obj.switches={**settings_obj.switches,"demo":True}
        settings_obj.save()
        if not created and m.Item.objects.filter(tenant=tenant).exists():
            self.stdout.write(f"ERP demo already exists; permissions refreshed. tenant_id={tenant.pk}")
            return
        base={"tenant":tenant,"branch":branch,"created_by":user}
        warehouse=m.Warehouse.objects.create(**base,name="Main warehouse",code="WH-01",address="Pune Works · Ground floor")
        secondary=m.Warehouse.objects.create(**base,name="Finished goods",code="WH-02",address="Pune Works · Dispatch bay")
        for code in ["A-01","A-02","B-01"]:m.WarehouseBin.objects.create(**base,warehouse=warehouse,code=code,rack=code[0])
        customer_rows=[
            ("Apex Engineering Pvt Ltd","Chakan MIDC, Pune","27AAECA1234F1Z5","Neha Shah","neha.shah@apex.example","9000001001",30,450000),
            ("Northstar Fabrication LLP","Bhosari Industrial Estate, Pune","27AANFN5678L1Z2","Vikram Rao","vikram.rao@northstar.example","9000001002",21,325000),
            ("Vertex Industrial Systems","Peenya Industrial Area, Bengaluru","29AABCV4412K1Z6","Ananya Iyer","ananya.iyer@vertex.example","9000001003",30,600000),
            ("Cobalt Storage Solutions","GIDC Makarpura, Vadodara","24AABCC3344D1Z7","Rahul Mehta","rahul.mehta@cobalt.example","9000001004",15,275000),
            ("Sterling Works India","Rabale MIDC, Navi Mumbai","27AABCS9988M1Z1","Farah Khan","farah.khan@sterling.example","9000001005",45,525000),
            ("Prism Auto Components","Sanand GIDC, Ahmedabad","24AAICP7234R1Z3","Sameer Joshi","sameer.joshi@prismauto.example","9000001006",30,700000),
            ("Evergreen Textiles Ltd","Sachin GIDC, Surat","24AABCE7712Q1Z8","Pooja Desai","pooja.desai@evergreen.example","9000001007",20,380000),
        ]
        customers=[]
        for name,address,gstin,contact,email,phone,terms,limit in customer_rows:
            customer=Company.objects.create(tenant=tenant,name=name,address=address,gst_no=gstin)
            m.CustomerProfile.objects.create(**base,customer=customer,contact_name=contact,email=email,phone=phone,
                shipping_address=address,payment_terms=terms,credit_limit=limit,
                notes="Synthetic but realistic customer profile for ERP demonstration.")
            customers.append(customer)
        suppliers=[m.Supplier.objects.create(**base,name=n,contact_name=c,email=f"supplier{i}@example.test",phone=f"90000020{i+1:02d}",payment_terms=30) for i,(n,c) in enumerate([("Pioneer Steel Co.","Rohan Shah"),("Metro Components","Priya Desai"),("Precision Supply","Vikram Patil")])]
        items=[]
        for i,(name,unit,rate,reorder,opening) in enumerate([("Mild steel sheet · 2 mm","kg",85,100,1250),("Square hollow section · 40 mm","m",220,80,620),("Stainless steel fasteners","pcs",12,100,72),("Industrial primer","litre",280,30,120),("Powder coating · graphite","kg",320,50,36),("Welding electrodes · 3.15 mm","box",780,15,48),("Protective packaging","roll",145,20,12)]):
            item=m.Item.objects.create(**base,name=name,sku=f"MAT-{i+1:03d}",unit=unit,purchase_rate=rate,sale_rate=Decimal(rate)*Decimal("1.25"),reorder_level=reorder,target_stock=reorder*3,category="Raw materials",barcode=f"MYR{i+1:06d}")
            items.append(item);svc.move_stock(tenant=tenant,actor=user,item=item,warehouse=warehouse,quantity=opening,unit_cost=rate,kind="opening",reason="Synthetic demo opening balance")
        service=m.Item.objects.create(**base,name="Industrial fabrication & assembly",sku="SVC-001",item_type="service",unit="lot",sale_rate=28000,category="Services")
        departments=[m.Department.objects.create(**base,name=n) for n in ["Production","Warehouse","Sales","Finance"]]
        shift=m.Shift.objects.create(**base,name="General shift",start_time=time(9),end_time=time(18),weekly_offs=[6])
        now=timezone.localdate();start=now.replace(day=1);month_end=start.replace(day=calendar.monthrange(start.year,start.month)[1])
        employees=[]
        for i,name in enumerate(["Aarav Sharma","Priya Desai","Rohan Patil","Ananya Mehta","Vikram Joshi","Sana Khan","Nikhil Rao","Meera Iyer"]):
            employee=m.Employee.objects.create(**base,code=f"EMP-{i+1:03d}",name=name,designation=["Production supervisor","Store coordinator","Fabricator","Sales executive"][i%4],department=departments[i%4],shift=shift,joining_date=now-timedelta(days=240),monthly_salary=24000+i*2200,email=f"employee{i}@example.test",user=user if i==0 else None)
            employees.append(employee)
            m.SalaryComponent.objects.create(**base,employee=employee,name="House rent allowance",kind="earning",amount=Decimal("3500.00"),prorate=True,effective_from=start)
            m.SalaryComponent.objects.create(**base,employee=employee,name="Professional tax",kind="deduction",amount=Decimal("200.00"),prorate=False,effective_from=start)
            if i in (2, 5):
                m.EmployeeLoan.objects.create(**base,employee=employee,name="Salary advance",principal=Decimal("12000.00"),recovered=Decimal("2000.00"),monthly_recovery=Decimal("1500.00"),date=start)
            day=start
            while day<=month_end:
                m.Attendance.objects.create(**base,employee=employee,date=day,status="weekly_off" if day.weekday()==6 else "absent" if i in (3,6) and day==now else "present")
                day+=timedelta(days=1)
        paid_leave=m.LeaveType.objects.create(**base,name="Annual leave",paid=True,annual_allowance=18)
        m.LeaveType.objects.create(**base,name="Unpaid leave",paid=False,annual_allowance=0)
        m.LeaveRequest.objects.create(**base,employee=employees[2],leave_type=paid_leave,start_date=now+timedelta(days=3),end_date=now+timedelta(days=4),days=2,reason="Family commitment")
        categories={n:m.ExpenseCategory.objects.create(**base,name=n,classification=k) for n,k in [("Rent / lease","opex"),("Utilities","opex"),("Maintenance","opex"),("Contract labour","direct"),("Transport","direct"),("Office & admin","opex")]}
        center=m.CostCenter.objects.create(**base,name="Pune operations",code="PUNE")
        def document(kind,customer=None,supplier=None,business_date=None,amount=28000,status_post=True):
            doc=m.Document.objects.create(**base,kind=kind,number=svc.number(tenant,kind),title="Fabrication & assembly",customer=customer,supplier=supplier,date=business_date or now,due_date=(business_date or now)+timedelta(days=21),warehouse=warehouse)
            calc=calculate_line({"quantity":1,"rate":amount,"tax_rate":18})
            m.DocumentLine.objects.create(**base,document=doc,item=service,description=service.name,unit="lot",**calc)
            svc.total_document(doc)
            if status_post:svc.post_document(doc,user,set(FEATURES))
            return doc
        for offset in range(5,-1,-1):
            absolute=start.year*12+start.month-1-offset
            month=date(absolute//12,absolute%12+1,1)
            for i in range(3):
                inv=document("invoice",customers[(i+offset)%len(customers)],business_date=month+timedelta(days=i*5+2),amount=92000+(5-offset)*13000+i*18000)
                svc.record_payment(tenant,user,{"direction":"receipt","customer":inv.customer_id,"amount":str(inv.gross*Decimal(".7")),"date":str(inv.date),"reference":f"DEMO-{offset}-{i}","allocations":[{"document":str(inv.pk),"amount":str(inv.gross*Decimal(".7"))}]})
            for category,amount in [("Rent / lease",28000),("Utilities",14500+offset*600),("Contract labour",42000),("Office & admin",6500)]:
                exp=m.Expense.objects.create(**base,title=f"{category} · {month:%B}",category=categories[category],amount=amount,date=month+timedelta(days=2),cost_center=center)
                svc.post_expense(exp,user)
        orders=[]
        for i in range(6):
            order=document("sales_order",customers[i%len(customers)],amount=68000+i*19500,business_date=now-timedelta(days=i*2));order.due_date=now+timedelta(days=i*3-2);order.save();orders.append(order)
        for i,name in enumerate(["Warehouse racking · Phase 2","Precision machine enclosures","Conveyor support frames","Modular storage installation","Assembly line workstations"]):
            job=m.Job.objects.create(**base,name=name,number=svc.number(tenant,"job"),source_order=orders[i],customer=customers[i%len(customers)],quantity=50,due_date=now+timedelta(days=i*2-3),priority="high" if i==0 else "normal",status="in_progress",owner=user,instructions="Follow the approved customer drawing. Check dimensions before packing.")
            for stage_i,stage_name in enumerate(["Preparation","Fabrication","Finishing","Packing"]):
                complete=50 if stage_i<i%3+1 else 20 if stage_i==i%3+1 else 0
                m.JobStage.objects.create(**base,job=job,name=stage_name,position=stage_i,planned=50,completed=complete,status="completed" if complete==50 else "in_progress" if complete else "pending")
            svc.move_stock(tenant=tenant,actor=user,item=items[0],warehouse=warehouse,quantity=-25,kind="issue",reason=job.number,job=job)
        for i in range(2):
            po=m.Document.objects.create(**base,kind="purchase_order",number=svc.number(tenant,"purchase_order"),supplier=suppliers[i],warehouse=warehouse,date=now,due_date=now+timedelta(days=4+i),title="Production material replenishment")
            for j in range(2):
                item=items[i+j];calc=calculate_line({"quantity":100,"rate":str(item.purchase_rate),"tax_rate":0})
                m.DocumentLine.objects.create(**base,document=po,item=item,description=item.name,unit=item.unit,**calc)
            svc.total_document(po);svc.post_document(po,user,set(FEATURES))
        for customer in customers[:4]:document("quotation",customer,amount=115000,status_post=False)
        for name,category,amount in [("Monthly factory rent","Rent / lease",28000),("Internet & software","Office & admin",6500)]:
            m.RecurringExpense.objects.create(**base,name=name,category=categories[category],amount=amount,next_due=start,anchor_day=1,cost_center=center)
        for i,title in enumerate(["Confirm Apex delivery window","Review low-stock fasteners","Collect signed delivery challans","Approve next week’s leave","Reconcile August supplier bills"]):
            m.Task.objects.create(**base,title=title,owner=user,due_date=now+timedelta(days=i-1),priority="high" if i==0 else "normal",status="in_progress" if i<2 else "open")
        run=m.PayrollRun.objects.create(**base,name=f"{now:%B %Y} · Monthly payroll",month=start)
        workforce.calculate_payroll(run,user)
        run.status="approved"
        svc.touch(run)
        workforce.finalize_payroll(run,user)
        self.stdout.write(self.style.SUCCESS(f"ERP demo created. tenant_id={tenant.pk}; phone login={user.phone_e164 or user.phone}. Synthetic records only; existing CRM tenant untouched."))
