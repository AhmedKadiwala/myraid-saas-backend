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
               'expenses','approvals','tasks','users','stockMoves','payouts','boms','qcInspections')
PREFIX = {'customers':'C','leads':'L','items':'SKU','suppliers':'V','quotes':'Q',
          'orders':'SO','jobs':'JOB','purchases':'PO','dispatches':'DC','invoices':'INV',
          'payments':'PAY','employees':'EMP','attendance':'AT','payroll':'PR',
          'expenses':'EXP','approvals':'APR','tasks':'TK','users':'U','stockMoves':'ST','payouts':'OUT',
          'boms':'BOM','qcInspections':'QC'}
PLANS = {'basic': {'price':250, 'cap':10, 'per_user':250, 'name':'Basic ERP', 'modules':{'dashboard','sales','finance','reports','settings'}},
         'advanced': {'price':300, 'cap':30, 'per_user':300, 'name':'Advanced ERP', 'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}},
         'premium': {'price':350, 'cap':100, 'per_user':350, 'name':'Premium ERP', 'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}},
         'recommended':{'price':300,'cap':30, 'per_user':300, 'name':'Advanced ERP', 'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}},
         'full': {'price':350,'cap':100, 'per_user':350, 'name':'Premium ERP', 'modules':{'dashboard','sales','jobs','purchase','inventory','dispatch','finance','employees','approvals','reports','settings'}}}
ROLE_MODULES = {'Owner':set(PLANS['full']['modules']), 'Admin':set(PLANS['full']['modules']),
                'Co-owner':set(PLANS['full']['modules']),
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
                     'tasks':'approvals','approvals':'approvals','users':'settings','payouts':'finance',
                     'boms':'jobs','qcInspections':'jobs'}
FORM_MODULE = {'lead':'sales','customer':'sales','quote':'sales','order':'sales',
               'job':'jobs','job-update':'jobs','supplier':'purchase','purchase':'purchase',
               'receive':'purchase','item':'inventory','stock':'inventory','dispatch':'dispatch',
               'invoice':'finance','payment':'finance','expense':'finance',
               'employee':'employees','attendance':'employees','payroll':'employees',
               'task':'approvals','approval':'approvals','user':'settings','payout':'finance',
               'bom':'jobs','qc-inspection':'jobs','job-schedule':'jobs','bom-explode':'jobs'}
FORM_MODULE.update({'job-assign':'jobs', 'job-split':'jobs', 'lead-update':'sales'})
ACTION_MODULE = {'quote-mark-sent':'sales','quote-convert':'sales','lead-convert':'sales',
                 'invoice-from-order':'finance','dispatch-invoice':'finance',
                 'approval-approve':'approvals','approval-reject':'approvals','task-done':'approvals',
                 'user-toggle':'settings','payroll-finalize':'employees','company-save':'settings',
                 'set-plan':'settings','reset':'settings',
                 'job-complete':'jobs','bom-toggle':'jobs'}
ACTION_MODULE['order-confirm'] = 'sales'
ACTION_MODULE.update({'order-cancel':'sales', 'quote-status':'sales'})

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
    if rate < 0 or rate > 100: raise DomainError('GST rate must be between 0 and 100 percent')
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


def phone_number(value):
    phone = text(value, 'Phone number', True, 16)
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 12 and digits.startswith('91'): digits = digits[2:]
    if not fullmatch(r'[6-9][0-9]{9}', digits):
        raise DomainError('Enter a valid Indian mobile number')
    return digits


def bom_explode(state, item_id, qty=1, visited=None):
    """
    Recursively explodes a multi-level BOM for an item.
    Returns structured components, scrap percentages, routing operations, and rolled-up manufacturing costs.
    """
    if visited is None:
        visited = set()
    if item_id in visited:
        raise DomainError(f"Recursive BOM loop detected for item {item_id}")
    visited.add(item_id)
    boms = state.get("boms", [])
    bom = next((b for b in boms if (b.get("item") == item_id or b.get("item_sku") == item_id or b.get("item_id") == item_id) and b.get("is_active", True)), None)
    if not bom:
        item = get(state, "items", item_id)
        unit_cost = float(money(item.get("cost", 0)))
        return {
            "item": item_id,
            "name": item.get("name", item_id),
            "qty": qty,
            "unit_cost": unit_cost,
            "total_cost": float(money(Decimal(str(unit_cost)) * Decimal(str(qty)))),
            "components": [],
            "operations": [],
            "is_raw_material": True,
        }

    exploded_components = []
    total_material_cost = Decimal("0.00")
    for comp in bom.get("components", []):
        c_item_id = comp["item"]
        scrap = Decimal(str(comp.get("scrap_percent", 0)))
        c_qty = Decimal(str(comp["qty"])) * Decimal(str(qty)) * (Decimal("1") + scrap / Decimal("100"))
        sub_explosion = bom_explode(state, c_item_id, float(c_qty), visited.copy())
        exploded_components.append(sub_explosion)
        total_material_cost += Decimal(str(sub_explosion["total_cost"]))

    op_cost = Decimal("0.00")
    exploded_ops = []
    for op in bom.get("operations", []):
        hours = Decimal(str(op.get("hours", 0))) * Decimal(str(qty))
        rate = Decimal(str(op.get("hourly_rate", 0)))
        c = hours * rate
        op_cost += c
        exploded_ops.append({**op, "total_hours": float(hours), "total_cost": float(money(c))})

    total_cost = total_material_cost + op_cost
    return {
        "bom_id": bom.get("id", ""),
        "item": item_id,
        "name": bom.get("name", item_id),
        "qty": qty,
        "material_cost": float(money(total_material_cost)),
        "operation_cost": float(money(op_cost)),
        "total_cost": float(money(total_cost)),
        "unit_cost": float(money(total_cost / Decimal(str(max(1, qty))))),
        "components": exploded_components,
        "operations": exploded_ops,
        "is_raw_material": False,
    }


def initial_job(state, order):
    if not any(j["order"] == order["id"] for j in state["jobs"]):
        boms = state.get("boms", [])
        order_item = order.get("item")
        bom = next((b for b in boms if (b.get("item") == order_item or b.get("item_id") == order_item or b.get("item_sku") == order_item) and b.get("is_active", True)), None)
        stages = ["Planning", "Production", "Quality check", "Packing"]
        cost_per_unit = 0.0
        planned_materials = []
        if bom:
            if bom.get("operations"):
                stages = [op["stage"] for op in bom["operations"]]
            cost_per_unit = float(bom.get("total_cost", 0))
            planned_materials = [
                {"item": c["item"], "qty": float(Decimal(str(c["qty"])) * Decimal(str(order["qty"])))}
                for c in bom.get("components", [])
            ]
        return add(state, "jobs", {
            "order": order["id"],
            "item": order["item"],
            "qty": order["qty"],
            "stage": stages[0] if stages else "Planning",
            "stages": stages,
            "status": "Pending",
            "progress": 0,
            "worker": "",
            "assignee": "",
            "priority": "Normal",
            "due": order["due"],
            "instructions": "",
            "bom": bom["id"] if bom else "",
            "cost_per_unit": cost_per_unit,
            "planned_materials": planned_materials,
        })


def job_quantity(state, job):
    return numeric(job.get('qty', get(state, 'orders', job['order'])['qty']), minimum=1)


def sync_order(state, order):
    if order['status'] in ('Draft', 'Cancelled', 'Completed', 'Dispatched', 'Partially dispatched'):
        return
    jobs = [j for j in state['jobs'] if j['order'] == order['id'] and j['status'] != 'Cancelled']
    if jobs and sum(job_quantity(state, j) for j in jobs) == order['qty'] and all(j['status'] == 'Completed' for j in jobs):
        order['status'] = 'Ready to dispatch'
    elif any(j['status'] != 'Pending' or j.get('progress', 0) for j in jobs):
        order['status'] = 'In production'
    else:
        order['status'] = 'Confirmed'

def get(state, collection, code):
    found=next((x for x in state[collection] if x.get('id')==code or (collection=='items' and x.get('sku')==code)),None)
    if found is None: raise DomainError(f'{collection}: record {code} not found in this company')
    return found

def has(state, collection, code):
    return bool(next((x for x in state[collection] if x.get('id')==code or (collection=='items' and x.get('sku')==code)),None))

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

def create_invoice(state,order_id,due='',gst=None,invoice_date=None):
    order=get(state,'orders',order_id)
    if order['status'] in ('Draft', 'Cancelled'): raise DomainError('Confirm the sales order before creating an invoice')
    if any(x['order']==order_id for x in state['invoices']): raise DomainError('An invoice already exists for this order')
    g=inclusive_gst(order['amount'],order['gst'] if gst in (None,'') else gst)
    item=get(state,'items',order['item'])
    cogs=money(item.get('cost',0))*Decimal(str(order['qty']))
    inv=add(state,'invoices',{'order':order_id,'customer':order['customer'],**g,
                              'date':check_date(invoice_date or today()),'due':check_date(due or today(15)),
                              'cogs':float(cogs),'cost_basis':'item_cost_snapshot', 'item':item['id'],'qty':order['qty']})
    if sum(d['qty'] for d in state['dispatches'] if d['order'] == order_id) == order['qty']:
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
    check_access(role,plan,'sales' if name == 'item' else module,detail)
    obj=None
    f=body
    if kind=='form':
        if name=='lead-update':
            obj = get(state, 'leads', code)
            stage = text(f.get('stage'), 'Stage', True)
            if stage not in ('New', 'Qualified', 'Quotation sent', 'Negotiation', 'Won', 'Lost'): raise DomainError('Choose a valid sales stage')
            if stage == 'Won' and not any(o.get('lead') == code and o['status'] not in ('Draft', 'Cancelled') for o in state['orders']): raise DomainError('Confirm a linked sales order before marking this opportunity won')
            obj.update(stage=stage, follow=check_date(f.get('follow', obj.get('follow')), True))
        elif name=='lead':
            stage = text(f.get('stage'), 'Stage') or 'New'
            if stage not in ('New', 'Qualified', 'Quotation sent', 'Negotiation', 'Lost'):
                raise DomainError('A lead is won only after a linked order is confirmed')
            obj=add(state,'leads',{'name':text(f.get('name'),'Lead',True),'contact':text(f.get('contact'),'Contact',True), 'phone':phone_number(f.get('phone')),
              'requirement':text(f.get('requirement'),'Requirement',False,800),'value':float(money(f.get('value',0))),
              'follow':check_date(f.get('follow',''),True),'stage':stage,
              'owner':text(f.get('owner'),'Owner')})
        elif name=='customer':
            label=text(f.get('name'),'Customer name',True)
            if any(c['name'].lower()==label.lower() for c in state['customers']): raise DomainError('Customer already exists')
            obj=add(state,'customers',{'name':label,'contact':text(f.get('contact'),'Contact'),
              'phone':phone_number(f.get('phone')),'gst':text(f.get('gst'),'GSTIN',False,15),
              'city':text(f.get('city'),'City'),'credit':numeric(f.get('credit',0),maximum=365)})
        elif name in ('quote','order'):
            cust=get(state,'customers',f.get('customer'))
            item=get(state,'items',f.get('item'))
            qty=numeric(f.get('qty'),minimum=1)
            tax=inclusive_gst(f.get('amount'),f.get('gst',18))
            item_ref = f.get('item') or item.get('sku') or item['id']
            if name=='quote':
                obj=add(state,'quotes',{'customer':cust['id'],'item':item_ref,'item_id':item['id'],'item_sku':item.get('sku',''),'qty':qty,
                  **tax,'status':'Draft','created':today(), 'lead':text(f.get('lead'), 'Lead')})
                if obj['lead']: get(state, 'leads', obj['lead'])
            else:
                status = f.get('status', 'Confirmed')
                if status not in ('Draft', 'Confirmed'): raise DomainError('Choose Draft or Confirmed')
                obj=add(state,'orders',{'quote':'','customer':cust['id'],'item':item_ref,'item_id':item['id'],'item_sku':item.get('sku',''),
                  'qty':qty,**tax,'status':status,'due':check_date(f.get('due','')), 'lead':text(f.get('lead'), 'Lead')})
                if obj['lead']: get(state, 'leads', obj['lead'])
                if status == 'Confirmed':
                    initial_job(state, obj)
                    if obj['lead']: get(state, 'leads', obj['lead'])['stage'] = 'Won'
        elif name=='job':
            o=get(state,'orders',f.get('order'))
            if o['status'] not in ('Confirmed', 'In production'): raise DomainError('Choose a confirmed order still in production')
            qty = numeric(f.get('qty'), minimum=1)
            used = sum(job_quantity(state, j) for j in state['jobs'] if j['order'] == o['id'] and j['status'] != 'Cancelled')
            if used + qty > o['qty']: raise DomainError('Job quantities exceed the order. Split an existing pending job instead.')
            obj=add(state,'jobs',{'order':o['id'],'stage':text(f.get('stage'),'Stage') or 'Pending',
                'item':o['item'], 'qty':qty, 'status':'Pending','progress':0,'worker':'','assignee':'',
                'priority':text(f.get('priority'),'Priority') or 'Normal','due':check_date(f.get('due','')), 'instructions':text(f.get('instructions'), 'Instructions', False, 2000)})
            sync_order(state, o)
        elif name=='job-split':
            source = get(state, 'jobs', code)
            if source['status'] != 'Pending' or source.get('progress', 0): raise DomainError('Only a pending job with no progress can be split')
            o = get(state, 'orders', source['order'])
            if o['status'] not in ('Confirmed', 'In production'): raise DomainError('This order cannot be split')
            qty = numeric(f.get('qty'), minimum=1, maximum=job_quantity(state, source) - 1)
            original = job_quantity(state, source)
            source['qty'] = original - qty
            obj = add(state, 'jobs', {k:v for k,v in source.items() if k != 'id'})
            obj.update(qty=qty, worker='', assignee='')
            sync_order(state, o)
        elif name=='job-assign':
            obj = get(state, 'jobs', code)
            if obj['status'] in ('Completed', 'Cancelled'): raise DomainError('A closed job cannot be assigned')
            worker_id = text(f.get('worker'), 'Worker', True)
            worker = get(state, 'employees', worker_id)
            if worker.get('status') != 'Active': raise DomainError('Choose an active worker')
            phone = phone_number(worker.get('phone'))
            obj.update(worker=worker['id'], assignee=worker['name'], worker_phone=phone, instructions=text(f.get('instructions', obj.get('instructions')), 'Instructions', False, 2000), assigned_by=f.get('assigned_by', {}))
        elif name=='bom':
            item = get(state, 'items', f.get('item'))
            name_text = text(f.get('name'), 'BOM name', True)
            base_qty = numeric(f.get('qty', 1), minimum=1)
            raw_comps = f.get('components', [])
            if not isinstance(raw_comps, list): raise DomainError('Components must be a list')
            parsed_comps = []
            material_cost = Decimal('0.00')
            for c in raw_comps:
                c_item = get(state, 'items', c.get('item'))
                c_qty = numeric(c.get('qty', 1), minimum=0.0001, integer=False)
                c_scrap = numeric(c.get('scrap_percent', 0), minimum=0, maximum=100, integer=False)
                c_cost = money(c.get('unit_cost', c_item.get('cost', 0)))
                comp_total = Decimal(str(c_qty)) * c_cost * (Decimal('1') + Decimal(str(c_scrap)) / Decimal('100'))
                material_cost += comp_total
                comp_ref = c.get('item') or c_item.get('sku') or c_item['id']
                parsed_comps.append({
                    'item': comp_ref,
                    'item_id': c_item['id'],
                    'item_sku': c_item.get('sku', ''),
                    'item_name': c_item.get('name', ''),
                    'qty': c_qty,
                    'unit': c.get('unit') or c_item.get('unit') or 'Nos',
                    'scrap_percent': c_scrap,
                    'unit_cost': float(c_cost),
                    'total_cost': float(money(comp_total)),
                })
            raw_ops = f.get('operations', [])
            parsed_ops = []
            op_cost = Decimal('0.00')
            for op in raw_ops:
                op_stage = text(op.get('stage'), 'Operation stage', True)
                workstation = text(op.get('workstation', 'Workstation 1'), 'Workstation')
                hours = numeric(op.get('hours', 1), minimum=0.01, maximum=1000, integer=False)
                rate = money(op.get('hourly_rate', 200))
                c_op = Decimal(str(hours)) * rate
                op_cost += c_op
                parsed_ops.append({
                    'stage': op_stage,
                    'workstation': workstation,
                    'hours': hours,
                    'hourly_rate': float(rate),
                    'cost': float(money(c_op)),
                })
            total_unit_cost = (material_cost + op_cost) / Decimal(str(base_qty))
            item_ref = f.get('item') or item.get('sku') or item['id']
            existing_bom = next((b for b in state.get('boms', []) if b.get('item') in (item['id'], item.get('sku'), item_ref) or b.get('item_id') == item['id']), None)
            bom_attrs = {
                'item': item_ref,
                'item_id': item['id'],
                'item_sku': item.get('sku', ''),
                'item_name': item.get('name', ''),
                'name': name_text,
                'qty': base_qty,
                'components': parsed_comps,
                'operations': parsed_ops,
                'material_cost': float(money(material_cost)),
                'operation_cost': float(money(op_cost)),
                'total_cost': float(money(total_unit_cost)),
                'is_active': True,
            }
            if existing_bom and not code:
                existing_bom.update(bom_attrs)
                obj = existing_bom
            else:
                obj = add(state, 'boms', bom_attrs)
            item['cost'] = float(money(total_unit_cost))
        elif name=='bom-explode':
            target_item = text(f.get('item'), 'Item SKU', True)
            target_qty = numeric(f.get('qty', 1), minimum=1)
            obj = bom_explode(state, target_item, target_qty)
        elif name=='job-schedule':
            j = get(state, 'jobs', code)
            j['start_date'] = check_date(f.get('start_date'), True)
            j['due'] = check_date(f.get('due') or j.get('due'), True)
            j['priority'] = text(f.get('priority', j.get('priority', 'Normal')), 'Priority')
            j['workstation'] = text(f.get('workstation', j.get('workstation', '')), 'Workstation')
            obj = j
        elif name=='qc-inspection':
            target_type = text(f.get('target_type', 'job'), 'Target type', True)
            target_id = text(f.get('target_id'), 'Target ID', True)
            item_rec = get(state, 'items', f.get('item'))
            inspected = numeric(f.get('inspected_qty', 1), minimum=1)
            accepted = numeric(f.get('accepted_qty', inspected), minimum=0, maximum=inspected)
            rejected = inspected - accepted
            qc_status = 'Passed' if rejected == 0 else 'Failed' if accepted == 0 else 'Conditionally Passed'
            item_ref = f.get('item') or item_rec.get('sku') or item_rec['id']
            obj = add(state, 'qcInspections', {
                'target_type': target_type,
                'target_id': target_id,
                'item': item_ref,
                'item_id': item_rec['id'],
                'item_sku': item_rec.get('sku', ''),
                'inspected_qty': inspected,
                'accepted_qty': accepted,
                'rejected_qty': rejected,
                'status': qc_status,
                'parameters': f.get('parameters', ['Dimensions', 'Tolerance', 'Visual finish']),
                'inspector': text(f.get('inspector', 'QC Inspector'), 'Inspector'),
                'remarks': text(f.get('remarks', ''), 'Remarks', False, 500),
                'date': check_date(f.get('date') or today()),
            })
            if rejected > 0 and state.get('inventory_enabled', False):
                item_rec['stock'] = max(0, item_rec['stock'] - rejected)
                add(state, 'stockMoves', {
                    'item': item_ref,
                    'item_id': item_rec['id'],
                    'delta': -rejected,
                    'reason': f'QC Inspection {obj["id"]} rejected scrap from {target_type} {target_id}',
                    'date': today()
                })
        elif name=='job-update':
            j=get(state,'jobs',code)
            if j['status'] in ('Completed', 'Cancelled') or get(state, 'orders', j['order'])['status'] == 'Cancelled':
                raise DomainError('A closed job cannot be changed')
            p=numeric(f.get('progress'),maximum=100)
            if f.get('status') not in ('Pending', 'In Progress', 'On Hold', 'Completed'): raise DomainError('Choose a valid job status')
            prev_status = j['status']
            j.update({'stage':text(f.get('stage'),'Stage',True),'status':text(f.get('status'),'Status',True),
                      'progress':p})
            if j['status']=='Completed' or j['progress']==100:
                j.update(status='Completed',progress=100)
                # When job completes, consume raw materials if configured and add finished goods to stock
                if prev_status != 'Completed' and state.get('inventory_enabled', False):
                    finished_item = get(state, 'items', j['item'])
                    finished_item['stock'] += j['qty']
                    add(state, 'stockMoves', {'item': finished_item['id'], 'delta': j['qty'], 'reason': f'Production completed from {j["id"]}', 'date': today()})
                    # Consume planned raw materials if BOM exists
                    for mat in j.get('planned_materials', []):
                        m_item = get(state, 'items', mat['item'])
                        c_qty = numeric(mat.get('qty', 0), minimum=0, integer=False)
                        if c_qty > 0:
                            m_item['stock'] = max(0, m_item['stock'] - int(c_qty))
                            add(state, 'stockMoves', {'item': m_item['id'], 'delta': -int(c_qty), 'reason': f'Material consumed in work order {j["id"]}', 'date': today()})
            sync_order(state, get(state,'orders',j['order']))
            obj=j
        elif name=='supplier':
            obj=add(state,'suppliers',{'name':text(f.get('name'),'Supplier',True),
              'city':text(f.get('city'),'City'),'phone':phone_number(f.get('phone'))})
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
            if state.get('inventory_enabled', False):
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
            if stock and state.get('inventory_enabled', False): add(state,'stockMoves',{'item':obj['id'],'delta':stock,'reason':'Opening balance','date':today()})
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
            completed = sum(job_quantity(state, j) for j in state['jobs'] if j['order'] == order['id'] and j['status'] == 'Completed')
            if qty + used > completed: raise DomainError('Dispatch exceeds the completed job quantity')
            if state.get('inventory_enabled', False):
                if item['stock']<qty: raise DomainError('Insufficient inventory for dispatch')
                item['stock']-=qty
                add(state,'stockMoves',{'item':item['id'],'delta':-qty,'reason':f'Dispatch for {order["id"]}', 'date':today()})
            obj=add(state,'dispatches',{'order':order['id'],'qty':qty,
              'carrier':text(f.get('carrier'),'Carrier'), 'tracking':text(f.get('tracking'),'Tracking'),
              'status':text(f.get('status'),'Status') or 'Dispatched','date':today()})
            order['status']='Partially dispatched' if used + qty < order['qty'] else 'Completed' if any(i['order'] == order['id'] for i in state['invoices']) else 'Dispatched'
        elif name=='invoice': obj=create_invoice(state,f.get('order'),f.get('due',''),f.get('gst'),f.get('date'))
        elif name=='payment':
            inv=get(state,'invoices',f.get('invoice'))
            amount=money(f.get('amount'),positive=True)
            if amount>balance(state,inv): raise DomainError('Payment exceeds invoice outstanding')
            obj=add(state,'payments',{'invoice':inv['id'],'amount':float(amount),
              'date':check_date(f.get('date','')),'mode':text(f.get('mode'),'Payment mode',True),
              'ref':text(f.get('ref'),'Reference',False,100) or '—'})
        elif name=='expense':
            tax=inclusive_gst(f.get('amount'),f.get('gst',0))
            obj=add(state,'expenses',{'name':text(f.get('name'),'Expense',True),
              'category':text(f.get('category'),'Category',True), **tax,
              'gst_creditable':str(f.get('gst_creditable',False)).lower() in ('true','on','1'),
              'payment_status':text(f.get('payment_status','Paid'),'Payment status') or 'Paid',
              'mode':text(f.get('mode','Bank transfer'),'Payment mode') or 'Bank transfer',
              'date':check_date(f.get('date','')),'recurring':text(f.get('recurring'),'Recurring')})
            if obj['payment_status'] not in ('Paid','Unpaid'):raise DomainError('Choose Paid or Unpaid')
        elif name=='payout':
            amount=money(f.get('amount'),positive=True)
            category=text(f.get('kind'),'Payout type',True)
            if category not in ('salary','other_expense','expense','supplier','asset_purchase','loan_repayment','transfer','tax','salary_advance'):
                raise DomainError('Choose a valid payout type')
            linked_expense=text(f.get('expense',''),'Expense ID')
            linked_payroll=text(f.get('payroll',''),'Payroll ID')
            employee=text(f.get('employee',''),'Employee ID')
            if employee:get(state,'employees',employee)
            if category in ('salary','salary_advance') and not employee:raise DomainError('Select an employee for a salary payment or advance')
            if linked_expense:
                exp=get(state,'expenses',linked_expense)
                paid=money(exp['amount']) if exp.get('payment_status','Paid')=='Paid' else sum((money(p['amount']) for p in state['payouts'] if p.get('expense')==linked_expense),Decimal('0'))
                if amount>money(exp['amount'])-paid:raise DomainError('Payment exceeds expense outstanding')
                if category!='expense':raise DomainError('Linked expenses use the expense payment type')
            if category=='expense' and not linked_expense:raise DomainError('Select the expense being paid')
            if linked_payroll:
                pay=get(state,'payroll',linked_payroll)
                if pay['status']!='Finalized' or category!='salary':raise DomainError('Select a finalized payroll run for salary settlement')
                settled=sum((money(p['amount']) for p in state['payouts'] if p.get('payroll')==linked_payroll),Decimal('0'))
                if amount>money(pay['amount'])-settled:raise DomainError('Payment exceeds payroll outstanding')
            obj=add(state,'payouts',{'kind':category,'payee':text(f.get('payee'),'Payee',True),
                'employee':employee,'expense':linked_expense,'payroll':linked_payroll,'amount':float(amount),
                'date':check_date(f.get('date','')),'mode':text(f.get('mode'),'Payment mode',True),
                'ref':text(f.get('ref'),'Reference'),'note':text(f.get('note'),'Note')})
        elif name=='employee':
            obj=add(state,'employees',{'name':text(f.get('name'),'Employee',True),
              'department':text(f.get('department'),'Department',True),'role':text(f.get('role'),'Role'),
              'salary':float(money(f.get('salary',0))), 'phone':phone_number(f.get('phone')),
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
            obj=add(state,'payroll',{'period':period,'month':year_month,'amount':float(salary),'status':'Draft'})
        elif name=='approval':
            if money(f.get('amount',0))<0:raise DomainError('Proposed amount cannot be negative')
            obj=add(state,'approvals',{'title':text(f.get('title'),'Approval request',True),
              'type':text(f.get('type'),'Request type',True,30),'amount':float(money(f.get('amount',0))),
              'by':text(f.get('by'),'Requested by'),'date':today(),'status':'Pending'})
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
        if name=='order-cancel':
            obj = get(state, 'orders', code)
            jobs = [j for j in state['jobs'] if j['order'] == code]
            if obj['status'] not in ('Draft', 'Confirmed') or any(j.get('progress',0) or j['status'] not in ('Pending', 'Cancelled') for j in jobs) or any(d['order'] == code for c in ('dispatches','invoices') for d in state[c]):
                raise DomainError('Only an order with no production, dispatch or invoice activity can be cancelled')
            obj['status'] = 'Cancelled'
            for job in jobs: job['status'] = 'Cancelled'
        elif name=='quote-status':
            obj = get(state, 'quotes', code)
            status = text(f.get('status'), 'Quotation status', True)
            if obj['status'] not in ('Draft', 'Sent') or status not in ('Rejected', 'Expired'): raise DomainError('Only an unaccepted quotation can be rejected or expired')
            obj['status'] = status
        elif name=='order-confirm':
            obj = get(state, 'orders', code)
            if obj['status'] not in ('Draft', 'Confirmed'): raise DomainError('This order cannot be confirmed')
            obj['status'] = 'Confirmed'
            initial_job(state, obj)
            if obj.get('lead'): get(state, 'leads', obj['lead'])['stage'] = 'Won'
        elif name=='quote-mark-sent':
            q=get(state,'quotes',code)
            if q['status'] != 'Draft': raise DomainError('Only a draft quotation can be sent')
            q['status']='Sent';obj=q
            if q.get('lead'): get(state, 'leads', q['lead'])['stage'] = 'Quotation sent'
        elif name=='quote-convert':
            q=get(state,'quotes',code)
            if q['status'] not in ('Draft', 'Sent'): raise DomainError('Only an open quotation can be confirmed')
            if any(o['quote']==code for o in state['orders']): raise DomainError('Quotation already converted')
            obj=add(state,'orders',{'quote':code,'customer':q['customer'],'item':q['item'],
              'qty':q['qty'],**inclusive_gst(q['amount'],q['gst']), 'status':'Confirmed','due':check_date(f.get('due') or today(10)), 'lead':q.get('lead', '')})
            q['status']='Accepted'
            initial_job(state, obj)
            if obj['lead']: get(state, 'leads', obj['lead'])['stage'] = 'Won'
        elif name=='lead-convert':
            lead=get(state,'leads',code)
            obj=next((c for c in state['customers'] if c['name'].lower()==lead['name'].lower()),None)
            if not obj:obj=add(state,'customers',{'name':lead['name'],'contact':lead['contact'],
              'phone':phone_number(lead.get('phone') or f.get('phone')),'gst':'','city':'','credit':30})
            lead['customer'] = obj['id']
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
            obj['date']=today()
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
    if name == 'bom-explode' and isinstance(obj, dict):
        return state, {**obj, 'id': obj.get('bom_id', ''), 'message': message, 'plan': state.get('plan', plan)}
    return state, {'id':obj.get('id') if obj else code, 'message':message, 'plan':state.get('plan',plan)}
