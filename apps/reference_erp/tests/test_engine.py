import json
from copy import deepcopy
from pathlib import Path
import unittest
from apps.reference_erp.engine import apply, inclusive_gst, DomainError, check_access, PLANS

FIXTURE=Path(__file__).resolve().parents[1]/'fixtures'/'demo_workspace.json'

class BusinessRulesTests(unittest.TestCase):
    def setUp(self):
        self.s=json.loads(FIXTURE.read_text())
    def cmd(self,name,fields=None,code='',kind='form',state=None,role='Owner',plan='recommended'):
        return apply(self.s if state is None else state,{'kind':kind,'name':name,'id':code,'input':fields or {}},role,plan)
    def test_gst_inclusive_preserves_gross(self):
        self.assertEqual(inclusive_gst('100000','5'),{'amount':100000.,'gst':5.,'taxable':95238.10,'gst_amount':4761.90})
        self.assertEqual(inclusive_gst('100000','18')['amount'],100000.)
    def test_quote_order_job_dispatch_invoice_payment(self):
        state,res=self.cmd('quote',{'customer':'C-001','item':'SKU-001','qty':'2','amount':'100000','gst':'5'})
        quote=res['id']
        state,res=self.cmd('quote-convert',code=quote,kind='action',state=state)
        so=res['id']; self.assertEqual(next(o['amount'] for o in state['orders'] if o['id']==so),100000)
        job=next(j['id'] for j in state['jobs'] if j['order']==so); state,_=self.cmd('job-update',{'stage':'Packing','status':'Completed','progress':'100','assignee':'Demo'},code=job,state=state)
        self.assertEqual(next(o['status'] for o in state['orders'] if o['id']==so),'Ready to dispatch')
        orig=next(i['stock'] for i in state['items'] if i['id']=='SKU-001')
        state,_=self.cmd('dispatch',{'order':so,'qty':'2','carrier':'Test','tracking':'A','status':'Dispatched'},state=state)
        self.assertEqual(next(i['stock'] for i in state['items'] if i['id']=='SKU-001'),orig)
        state,res=self.cmd('invoice-from-order',code=so,kind='action',state=state)
        inv=res['id'];self.assertEqual(next(i['amount'] for i in state['invoices'] if i['id']==inv),100000.)
        state,_=self.cmd('payment',{'invoice':inv,'amount':'40000','date':'2026-10-10','mode':'NEFT'},state=state)
        state,_=self.cmd('payment',{'invoice':inv,'amount':'60000','date':'2026-10-10','mode':'UPI'},state=state)
        with self.assertRaisesRegex(DomainError,'exceeds'):
            self.cmd('payment',{'invoice':inv,'amount':'0.01','date':'2026-10-10','mode':'UPI'},state=state)
    def test_purchase_partial_receipt(self):
        stock=next(i['stock'] for i in self.s['items'] if i['id']=='SKU-002')
        s,_=self.cmd('receive',{'qty':30},code='PO-001')
        self.assertEqual(next(x['received'] for x in s['purchases'] if x['id']=='PO-001'),30)
        self.assertEqual(next(x['stock'] for x in s['items'] if x['id']=='SKU-002'),stock)
        s,_=self.cmd('receive',{'qty':70},code='PO-001',state=s)
        self.assertEqual(next(x['status'] for x in s['purchases'] if x['id']=='PO-001'),'Received')
        with self.assertRaises(DomainError):self.cmd('receive',{'qty':1},code='PO-001',state=s)
    def test_reject_negative_stock(self):
        with self.assertRaisesRegex(DomainError,'Negative'):
            self.cmd('stock',{'item':'SKU-002','delta':-999,'reason':'Write off'})
    def test_role_plan_permissions(self):
        with self.assertRaises(PermissionError):self.cmd('purchase',{'vendor':'V-001','item':'SKU-001','qty':1,'unitCost':2,'due':'2026-10-10'},role='Sales')
        with self.assertRaises(PermissionError):self.cmd('purchase',{'vendor':'V-001','item':'SKU-001','qty':1,'unitCost':2,'due':'2026-10-10'},plan='basic')
        with self.assertRaises(PermissionError):check_access('Viewer','full','reports','write')
    def test_user_cap(self):
        s=deepcopy(self.s)
        for i in range(5):
            s,_=self.cmd('user',{'name':'Temp','phone':f'98765435{i:02d}','role':'Sales'},state=s,plan='basic')
        with self.assertRaisesRegex(DomainError,'cap'):
            self.cmd('user',{'name':'Temp','phone':'9876543599','role':'Sales'},state=s,plan='basic')
    def test_duplicate_quote_conversion(self):
        with self.assertRaises(DomainError):self.cmd('quote-convert',code='Q-001',kind='action')
    def test_invoice_cannot_duplicate(self):
        with self.assertRaises(DomainError):self.cmd('invoice-from-order',code='SO-003',kind='action')
    def test_owner_only_plan(self):
        with self.assertRaises(PermissionError):self.cmd('set-plan',{'plan':'full'},kind='action',role='Admin')
    def test_mutations_do_not_change_input_snapshot(self):
        original=deepcopy(self.s)
        self.cmd('quote-mark-sent',code='Q-003',kind='action')
        self.assertEqual(original,self.s)
    def test_demo_reset(self):
        modified,_=self.cmd('quote-mark-sent',code='Q-003',kind='action')
        restored,result=self.cmd('reset',kind='action',state=modified)
        self.assertEqual(result['id'],'demo-reset')
        self.assertEqual(next(q for q in restored['quotes'] if q['id']=='Q-003')['status'],'Draft')
        self.assertEqual(restored['plan'],'recommended')
    def test_no_invalid_cross_reference(self):
        with self.assertRaises(DomainError):self.cmd('quote',{'customer':'C-999','item':'SKU-001','qty':1,'amount':1,'gst':18})
    def test_payroll_restricted(self):
        with self.assertRaises(PermissionError):self.cmd('payroll',{'period':'2026-11'},role='Production')

if __name__=='__main__': unittest.main()
