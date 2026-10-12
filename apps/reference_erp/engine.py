"""Pure Python domain rules; Django wraps these commands in tenant-level DB transactions.

Amounts are represented as INR decimal values rounded to paisa. `amount` includes GST.
This is an operational MVP, not statutory invoice/payroll compliance software.
"""
from copy import deepcopy
import json
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from re import fullmatch

COLLECTIONS = ('customers','leads','items','suppliers','quotes','orders','jobs','purchases',
               'dispatches','invoices','payments','employees','attendance','payroll',
               'expenses','approvals','tasks','users','stockMoves')
PREFIX = {'customers':'C','leads':'L','items':'SKU','suppliers':'V','quotes':'Q',
          'orders':'SO','jobs':'JOB','purchases':'PO','dispatches':'DC','invoices':'INV',
          'payments':'PAY','employees':'EMP','attendance':'AT','payroll':'PR',
          'expenses':'EXP','approvals':'APR','tasks':'TK','users':'U','stockMoves':'ST'}
PLANS = {'basic': {'price':2999, 'cap':10, 'modules':{'dashboard','sales','finance','reports','settings'}},
         'recommended':{'price':9999,'cap':30,'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}},
         'full': {'price':11999,'cap':50,'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}}}
ROLE_MODULES = {'Owner':set(PLANS['full']['modules']), 'Admin':set(PLANS['full']['modules']),
                'Sales':{'dashboard','sales','reports'},
                'Production':{'dashboard','jobs','inventory','dispatch'},
                'Purchase':{'dashboard','purchase','inventory'},
                'Store':{'dashboard','purchase','inventory','dispatch'},
                'Accounts':{'dashboard','finance','sales','reports'},
                'HR':{'dashboard','employees'},'Viewer':{'dashboard','reports'}}
COLLECTION_MODULE = {'customers':'sales','leads':'sales','quotes':'sales','orders':'sales',
                     'jobs':'jobs','suppliers':'purchase','purchases':'purchase',
                     'items':'inventory','stockMoves':'inventory','dispatches':'dispatch',
                     'invoices':'finance','payments':'finance','expenses':'finance',
                     'employees':'employees','attendance':'employees','payroll':'employees',
                     'tasks':'approvals','approvals':'approvals','users':'settings'}
FORM_MODULE = {'lead':'sales','customer':'sales','quote':'sales','order':'sales',
               'job':'jobs','job-update':'jobs','supplier':'purchase','purchase':'purchase',
               'receive':'purchase','item':'inventory','stock':'inventory','dispatch':'dispatch',
               'invoice':'finance','payment':'finance','expense':'finance',
               'employee':'employees','attendance':'employees','payroll':'employees',
               'task':'approvals','user':'settings'}
ACTION_MODULE = {'quote-mark-sent':'sales','quote-convert':'sales','lead-convert':'sales',
                 'invoice-from-order':'finance','dispatch-invoice':'finance',
                 'approval-approve':'approvals','approval-reject':'approvals','task-done':'approvals',
                 'user-toggle':'settings','payroll-finalize':'employees','company-save':'settings',
                 'set-plan':'settings','reset':'settings'}

class DomainError(ValueError): pass


def money(value, *, positive=False):
    try:
        d=Decimal(str(value)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as e:
        raise DomainError('Invalid monetary amount') from e
    if not d.is_finite() or abs(d)>Decimal('999999999999.99') or (positive and d<=0):
        raise DomainError('Amount must be positive and within allowed limits')
    return d

def numeric(value, *, minimum=0, maximum=100000000, integer=True):
    try: n=Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as e: raise DomainError('Invalid quantity') from e
    if not n.is_finite() or n<minimum or n>maximum or (integer and n!=n.to_integral_value()):
        raise DomainError('Quantity outside permitted range')
    return int(n) if integer else float(n)

def inclusive_gst(gross, rate):
    gross=money(gross,positive=True)
    rate=money(rate)
    if rate not in {Decimal(x) for x in ('0','5','12','18','28')}: raise DomainError('Select a supported GST rate: 0, 5, 12, 18 or 28')
    taxable=(gross/(Decimal('1')+rate/100)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
    return {'amount':float(gross),'gst':float(rate),'taxable':float(taxable),'gst_amount':float(gross-taxable)}

def text(value, field, required=False, max_length=200):
    val=str(value if value is not None else '').strip()
    if required and not val: raise DomainError(f'{field} is required')
    if len(val)>max_length: raise DomainError(f'{field} is too long')
    return val

def check_date(value, optional=False):
    v=text(value,'Date',not optional,10)
    if v:
        try: date.fromisoformat(v)
        except ValueError as e: raise DomainError('Use YYYY-MM-DD date format') from e
    return v

def get(state, collection, code):
    found=next((x for x in state[collection] if x['id']==code),None)
    if found is None: raise DomainError(f'{collection}: record {code} not found in this company')
    return found

def has(state, collection, code):
    return bool(next((x for x in state[collection] if x['id']==code),None))

def add(state,collection,attrs):
    largest=max((int(x['id'].rsplit('-',1)[-1]) for x in state[collection] if x.get('id','').rsplit('-',1)[-1].isdigit()),default=0)
    obj={'id':f'{PREFIX[collection]}-{largest+1:03d}', **attrs}
    state[collection].insert(0,obj)
    return obj

def today(offset=0): return (datetime.now(ZoneInfo('Asia/Kolkata')).date()+timedelta(days=offset)).isoformat()

def check_access(role,plan,module,action='view'):
    if plan not in PLANS: raise DomainError('Invalid subscription plan')
    if role not in ROLE_MODULES: raise DomainError('Invalid user role')
    if module not in PLANS[plan]['modules']: raise PermissionError(f'{module} is not included in your subscription')
    if module not in ROLE_MODULES[role]: raise PermissionError(f'{role} role cannot access {module}')
    if action!='view' and role=='Viewer': raise PermissionError('Read-only user cannot change company data')
    if module=='settings' and action!='view' and role not in ('Owner','Admin'):
        raise PermissionError('Only company admins can change settings')
    if module=='approvals' and action=='approve' and role not in ('Owner','Admin','Accounts','Purchase'):
        raise PermissionError('You do not have approval authority')
    if module=='employees' and action in ('payroll','salary') and role not in ('Owner','Admin','HR'):
        raise PermissionError('You do not have salary/payroll permission')

def balance(state,invoice):
    paid=sum((money(p['amount']) for p in state['payments'] if p['invoice']==invoice['id']),Decimal('0'))
    return money(invoice['amount'])-paid

def create_invoice(state,order_id,due=''):
    order=get(state,'orders',order_id)
    if any(x['order']==order_id for x in state['invoices']): raise DomainError('An invoice already exists for this order')
    g=inclusive_gst(order['amount'],order['gst'])
    inv=add(state,'invoices',{'order':order_id,'customer':order['customer'],**g,
                              'date':today(),'due':check_date(due or today(15))})
    order['status']='Completed'
    return inv

def _audit(state,entry):
    state.setdefault('audit',[]).insert(0,{'text':entry[:350],'at':'Just now'})
    state['audit']=state['audit'][:70]

def apply(state,command,role='Owner',plan='recommended'):
    """Mutate only a copy. Call under transaction.atomic + locked tenant in Django."""
    state=deepcopy(state)
    for col in COLLECTIONS:state.setdefault(col,[])
    kind=command.get('kind')
    body=command.get('input',{})
    if not isinstance(body,dict): raise DomainError('Input must be an object')
    name=text(command.get('name',''),'Command',True,40)
    code=text(command.get('id',''),'ID',False,40)
    module=FORM_MODULE.get(name) if kind=='form' else ACTION_MODULE.get(name)
    if not module: raise DomainError('Unsupported command')
    detail = 'approve' if name.startswith('approval-') else 'payroll' if name in ('payroll','payroll-finalize') else 'write'
    check_access(role,plan,module,detail)
    obj=None
    f=body
    if kind=='form':
        if name=='lead':
            obj=add(state,'leads',{'name':text(f.get('name'),'Lead',True),'contact':text(f.get('contact'),'Contact'),
              'requirement':text(f.get('requirement'),'Requirement',False,800),'value':float(money(f.get('value',0))),
              'follow':check_date(f.get('follow',''),True),'stage':text(f.get('stage'),'Stage') or 'New',
              'owner':text(f.get('owner'),'Owner')})
        elif name=='customer':
            label=text(f.get('name'),'Customer name',True)
            if any(c['name'].lower()==label.lower() for c in state['customers']): raise DomainError('Customer already exists')
            obj=add(state,'customers',{'name':label,'contact':text(f.get('contact'),'Contact'),
              'phone':text(f.get('phone'),'Phone',False,15),'gst':text(f.get('gst'),'GSTIN',False,15),
              'city':text(f.get('city'),'City'),'credit':numeric(f.get('credit',0),maximum=365)})
        elif name in ('quote','order'):
            cust=get(state,'customers',f.get('customer'))
            item=get(state,'items',f.get('item'))
            qty=numeric(f.get('qty'),minimum=1)
            tax=inclusive_gst(f.get('amount'),f.get('gst',18))
            if name=='quote':
                obj=add(state,'quotes',{'customer':cust['id'],'item':item['id'],'qty':qty,
                  **tax,'status':'Draft','created':today()})
            else:
                obj=add(state,'orders',{'quote':'','customer':cust['id'],'item':item['id'],
                  'qty':qty,**tax,'status':'Confirmed','due':check_date(f.get('due',''))})
        elif name=='job':
            o=get(state,'orders',f.get('order'))
            if any(x['order']==o['id'] for x in state['jobs']): raise DomainError('Work order already exists')
            obj=add(state,'jobs',{'order':o['id'],'stage':text(f.get('stage'),'Stage') or 'Pending',
                'status':'Pending','progress':0,'assignee':text(f.get('assignee'),'Assignee'),
                'priority':text(f.get('priority'),'Priority') or 'Normal','due':check_date(f.get('due',''))})
            o['status']='In production'
        elif name=='job-update':
            j=get(state,'jobs',code)
            p=numeric(f.get('progress'),maximum=100)
            j.update({'stage':text(f.get('stage'),'Stage',True),'status':text(f.get('status'),'Status',True),
                      'progress':p,'assignee':text(f.get('assignee'),'Assignee')})
            if j['status']=='Completed' or j['progress']==100: j.update(status='Completed',progress=100)
            get(state,'orders',j['order'])['status']='Ready to dispatch' if j['status']=='Completed' else 'In production'
            obj=j
        elif name=='supplier':
            obj=add(state,'suppliers',{'name':text(f.get('name'),'Supplier',True),
              'city':text(f.get('city'),'City'),'phone':text(f.get('phone'),'Phone',False,15)})
        elif name=='purchase':
            supplier=get(state,'suppliers',f.get('vendor'))
            item=get(state,'items',f.get('item'))
            obj=add(state,'purchases',{'vendor':supplier['id'],'item':item['id'],
              'qty':numeric(f.get('qty'),minimum=1),'received':0,
              'unitCost':float(money(f.get('unitCost'),positive=True)),
              'status':'Pending','due':check_date(f.get('due',''))})
        elif name=='receive':
            po=get(state,'purchases',code)
            qty=numeric(f.get('qty'),minimum=1)
            if po['received']+qty>po['qty']: raise DomainError('Receipt exceeds outstanding PO quantity')
            po['received']+=qty
            po['status']='Received' if po['received']==po['qty'] else 'Partial'
            item=get(state,'items',po['item']);item['stock']+=qty
            add(state,'stockMoves',{'item':item['id'],'delta':qty,'reason':f'Goods receipt {po["id"]} '+text(f.get('note'),'Note',False,200),'date':today()})
            obj=po
        elif name=='item':
            sku=text(f.get('sku'),'SKU',True,35)
            if any(i['sku'].lower()==sku.lower() for i in state['items']): raise DomainError('SKU already exists')
            stock=numeric(f.get('stock',0)); min_stock=numeric(f.get('min',0))
            obj=add(state,'items',{'name':text(f.get('name'),'Item',True), 'sku':sku,
              'category':text(f.get('category'),'Category'),'stock':stock,'min':min_stock,
              'unit':text(f.get('unit'),'Unit') or 'Nos', 'rate':float(money(f.get('rate',0))),
              'cost':float(money(f.get('cost',0))),'hsn':text(f.get('hsn'),'HSN',False,15)})
            if stock: add(state,'stockMoves',{'item':obj['id'],'delta':stock,'reason':'Opening balance','date':today()})
        elif name=='stock':
            item=get(state,'items',f.get('item'))
            delta=numeric(f.get('delta'),minimum=-100000000)
            if delta==0: raise DomainError('Stock adjustment cannot be zero')
            reason=text(f.get('reason'),'Adjustment reason',True,300)
            if item['stock']+delta<0: raise DomainError('Negative stock blocked')
            item['stock']+=delta
            obj=add(state,'stockMoves',{'item':item['id'],'delta':delta,'reason':reason,'date':today()})
        elif name=='dispatch':
            order=get(state,'orders',f.get('order'))
            item=get(state,'items',order['item'])
            qty=numeric(f.get('qty'),minimum=1)
            used=sum(d['qty'] for d in state['dispatches'] if d['order']==order['id'])
            if qty+used>order['qty']: raise DomainError('Dispatch exceeds unshipped order quantity')
            if item['stock']<qty: raise DomainError('Insufficient inventory for dispatch')
            item['stock']-=qty
            add(state,'stockMoves',{'item':item['id'],'delta':-qty,'reason':f'Dispatch for {order["id"]}', 'date':today()})
            obj=add(state,'dispatches',{'order':order['id'],'qty':qty,
              'carrier':text(f.get('carrier'),'Carrier'), 'tracking':text(f.get('tracking'),'Tracking'),
              'status':text(f.get('status'),'Status') or 'Dispatched','date':today()})
            order['status']='Dispatched'
        elif name=='invoice': obj=create_invoice(state,f.get('order'),f.get('due',''))
        elif name=='payment':
            inv=get(state,'invoices',f.get('invoice'))
            amount=money(f.get('amount'),positive=True)
            if amount>balance(state,inv): raise DomainError('Payment exceeds invoice outstanding')
            obj=add(state,'payments',{'invoice':inv['id'],'amount':float(amount),
              'date':check_date(f.get('date','')),'mode':text(f.get('mode'),'Payment mode',True),
              'ref':text(f.get('ref'),'Reference',False,100) or '—'})
        elif name=='expense':
            obj=add(state,'expenses',{'name':text(f.get('name'),'Expense',True),
              'category':text(f.get('category'),'Category',True), 'amount':float(money(f.get('amount'),positive=True)),
              'date':check_date(f.get('date','')),'recurring':text(f.get('recurring'),'Recurring')})
        elif name=='employee':
            obj=add(state,'employees',{'name':text(f.get('name'),'Employee',True),
              'department':text(f.get('department'),'Department',True),'role':text(f.get('role'),'Role'),
              'salary':float(money(f.get('salary',0))), 'phone':text(f.get('phone'),'Phone',False,15),
              'status':'Active'})
        elif name=='attendance':
            emp=get(state,'employees',f.get('employee'))
            day=check_date(f.get('date',''))
            status=text(f.get('status'),'Status',True)
            if status not in {'Present','Absent','Leave','Half-day'}: raise DomainError('Invalid attendance status')
            obj=next((x for x in state['attendance'] if x['employee']==emp['id'] and x['date']==day),None)
            if obj: obj['status']=status
            else: obj=add(state,'attendance',{'employee':emp['id'],'date':day,'status':status})
        elif name=='payroll':
            year_month=text(f.get('period'),'Payroll period',True,7)
            if not fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',year_month): raise DomainError('Month must be YYYY-MM')
            period=date.fromisoformat(year_month+'-01').strftime('%b %Y')
            if any(p['period']==period for p in state['payroll']): raise DomainError('Payroll run for this period exists')
            salary=sum(money(e.get('salary',0)) for e in state['employees'] if e['status']=='Active')
            obj=add(state,'payroll',{'period':period,'amount':float(salary),'status':'Draft'})
        elif name=='task':
            obj=add(state,'tasks',{'title':text(f.get('title'),'Task',True),
              'owner':text(f.get('owner'),'Owner'),'due':check_date(f.get('due','')),'status':'Pending'})
        elif name=='user':
            if role not in ('Owner','Admin'): raise PermissionError('Only admins can invite users')
            phone=text(f.get('phone'),'Phone',True,10)
            if not fullmatch('[6-9][0-9]{9}',phone):raise DomainError('Enter a valid Indian mobile number')
            if len([u for u in state['users'] if u['status']=='Active'])>=PLANS[plan]['cap']:raise DomainError('Active user cap reached')
            if any(u['phone']==phone for u in state['users']):raise DomainError('Phone is already invited')
            grant=text(f.get('role'),'Role',True)
            if grant not in ROLE_MODULES: raise DomainError('Invalid role')
            if grant=='Owner' and role!='Owner':raise PermissionError('Only the owner can grant owner access')
            obj=add(state,'users',{'name':text(f.get('name'),'Name',True), 'phone':phone,'role':grant,'status':'Active'})
        else:raise DomainError('Form unavailable')
    elif kind=='action':
        if name=='quote-mark-sent':
            q=get(state,'quotes',code);q['status']='Sent';obj=q
        elif name=='quote-convert':
            q=get(state,'quotes',code)
            if any(o['quote']==code for o in state['orders']): raise DomainError('Quotation already converted')
            obj=add(state,'orders',{'quote':code,'customer':q['customer'],'item':q['item'],
              'qty':q['qty'],**inclusive_gst(q['amount'],q['gst']), 'status':'Confirmed','due':today(10)})
            q['status']='Accepted'
        elif name=='lead-convert':
            lead=get(state,'leads',code)
            obj=next((c for c in state['customers'] if c['name'].lower()==lead['name'].lower()),None)
            if not obj:obj=add(state,'customers',{'name':lead['name'],'contact':lead['contact'],
              'phone':'','gst':'','city':'','credit':30})
            lead['stage']='Won'
        elif name in ('invoice-from-order','dispatch-invoice'):
            order_id=get(state,'dispatches',code)['order'] if name=='dispatch-invoice' else code
            obj=create_invoice(state,order_id)
        elif name in ('approval-approve','approval-reject'):
            obj=get(state,'approvals',code)
            if obj['status']!='Pending':raise DomainError('Approval already processed')
            obj['status']='Approved' if name=='approval-approve' else 'Rejected'
        elif name=='task-done':obj=get(state,'tasks',code);obj['status']='Completed'
        elif name=='payroll-finalize':
            obj=get(state,'payroll',code)
            if obj['status']!='Draft':raise DomainError('Only draft payroll can be finalized')
            obj['status']='Finalized'
        elif name=='user-toggle':
            obj=get(state,'users',code)
            if obj['role']=='Owner' and obj['status']=='Active':raise DomainError('Owner cannot be deactivated')
            if obj['status']=='Inactive' and len([u for u in state['users'] if u['status']=='Active'])>=PLANS[plan]['cap']:
                raise DomainError('Active user cap reached')
            obj['status']='Inactive' if obj['status']=='Active' else 'Active'
        elif name=='set-plan':
            if role!='Owner':raise PermissionError('Only owner may change plan')
            target=text(f.get('plan'),'Plan',True,20)
            if target not in PLANS:raise DomainError('Unknown plan')
            if sum(u['status']=='Active' for u in state['users'])>PLANS[target]['cap']:
                raise DomainError('Too many active users for selected plan')
            state['plan']=target
            obj={'id':target}
        elif name=='company-save':
            company=state.setdefault('company',{})
            for key in ('name','industry','location','gst','branch'):
                if key in f: company[key]=text(f[key],key, key=='name',160)
            obj={'id':'company'}
        elif name=='reset':
            if role!='Owner':raise PermissionError('Only owner may reset demo data')
            fixture=Path(__file__).parent/'fixtures'/'demo_workspace.json'
            state=json.loads(fixture.read_text(encoding='utf-8'))
            state['plan']='recommended'
            obj={'id':'demo-reset'}
        else: raise DomainError('Action unavailable')
    else: raise DomainError('Invalid command type')
    message=f'{name} · {obj.get("id") if obj else code}'
    _audit(state,message)
    return state, {'id':obj.get('id') if obj else code, 'message':message, 'plan':state.get('plan',plan)}
