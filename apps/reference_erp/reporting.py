"""Dated operating statements. GST and cash settlement remain separate dimensions."""
import calendar
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from rest_framework.exceptions import ValidationError
from .engine import money, inclusive_gst

ZERO = Decimal("0.00")


def period_range(params):
    try:
        kind = params.get("period", "month")
        anchor = date.fromisoformat(params.get("date") or datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat())
        if kind == "day": start = end = anchor
        elif kind == "week":
            start = anchor - timedelta(days=anchor.weekday()); end = start + timedelta(days=6)
        elif kind == "month":
            start = anchor.replace(day=1); end = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
        elif kind == "year": start, end = date(anchor.year, 1, 1), date(anchor.year, 12, 31)
        elif kind == "financial_year":
            year = int(params.get("fy_start") or (anchor.year if anchor.month >= 4 else anchor.year-1))
            start, end = date(year, 4, 1), date(year+1, 3, 31)
        elif kind == "custom":
            start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        else: raise ValueError()
        if start > end or (end-start).days > 3660: raise ValueError()
        return start, end, kind
    except (ValueError, TypeError, KeyError):
        raise ValidationError("Choose a valid reporting period and an ordered date range of at most ten years.")


def row_date(record, fallback="date"):
    value = record.get(fallback) or record.get("date")
    try: return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError): return None


def payroll_date(record):
    try:
        value = record.get("month")
        parsed = date.fromisoformat(value + "-01") if value else datetime.strptime(record["period"], "%b %Y").date()
        return parsed.replace(day=calendar.monthrange(parsed.year, parsed.month)[1])
    except (ValueError, TypeError, KeyError): return row_date(record)


def build_statement(state, params, full=True):
    start, end, kind = period_range(params)
    zero_filter = params.get("gst_scope", "all")
    if zero_filter not in ("all", "taxable", "zero"):
        raise ValidationError("GST scope must be all, taxable or zero.")
    invoices = state.get("invoices", [])
    invoice_map = {r["id"]: r for r in invoices}
    expense_map = {r["id"]: r for r in state.get("expenses", [])}
    payroll_map = {r["id"]: r for r in state.get("payroll", [])}
    items = {r["id"]: r for r in state.get("items", [])}
    orders = {r["id"]: r for r in state.get("orders", [])}
    totals = {k: ZERO for k in ("gross_invoiced", "net_revenue", "output_gst", "input_gst_credit", "cogs", "operating_expenses", "salary_expense", "cash_received", "cash_paid", "bank_received", "bank_paid", "other_outflows")}
    register, warnings, invoice_count = [], [], 0
    trend = {}

    def in_period(day): return bool(day and start <= day <= end)
    def number(value): return float(money(value))
    def append(day, source, record, description, incoming=ZERO, outgoing=ZERO, income=ZERO, expense=ZERO, gst=ZERO, rate=None, mode="", related="", classification=""):
        if not in_period(day): return
        register.append({"date": day.isoformat(), "source": source, "id": record["id"], "description": description,
            "inflow": number(incoming), "outflow": number(outgoing), "income": number(income), "expense": number(expense),
            "gst": number(gst), "gst_rate": float(rate) if rate is not None else None, "mode": mode,
            "reference": record.get("ref", ""), "related": related, "classification": classification})

    def cash_movement(mode, amount, incoming=False):
        key = ("cash" if str(mode).lower() == "cash" else "bank") + ("_received" if incoming else "_paid")
        totals[key] += amount

    for invoice in invoices:
        day = row_date(invoice)
        if not day:
            warnings.append(f"Invoice {invoice['id']} has no valid date."); continue
        tax = inclusive_gst(invoice["amount"], invoice.get("gst", 0))
        net, gst, gross = money(tax["taxable"]), money(tax["gst_amount"]), money(invoice["amount"])
        cogs = ZERO
        if full:
            if "cogs" in invoice:
                cogs = money(invoice["cogs"])
            elif in_period(day):
                order = orders.get(invoice.get("order"), {})
                item = items.get(invoice.get("item") or order.get("item"), {})
                if item:
                    cogs = money(item.get("cost", 0)) * Decimal(str(invoice.get("qty") or order.get("qty") or 0))
                    warnings.append(f"Invoice {invoice['id']}: historical item cost was not stored; current cost is an estimate.")
                else: warnings.append(f"Invoice {invoice['id']}: cost of sales is missing.")
        if in_period(day):
            invoice_count += 1
            totals["gross_invoiced"] += gross; totals["net_revenue"] += net; totals["output_gst"] += gst
            totals["cogs"] += cogs
            bucket = day.strftime("%Y-%m") if (end-start).days > 31 else day.isoformat()
            trend[bucket] = trend.get(bucket, ZERO) + net
        append(day, "Invoice", invoice, "Sales invoice", income=net, expense=cogs, gst=gst, rate=invoice.get("gst",0), classification="Accrual revenue and cost of sales")
    for payment in state.get("payments", []):
        day = row_date(payment); amount = money(payment["amount"]); invoice = invoice_map.get(payment.get("invoice"), {})
        if in_period(day): cash_movement(payment.get("mode"), amount, True)
        append(day, "Receipt", payment, "Customer payment", incoming=amount, rate=invoice.get("gst"), mode=payment.get("mode", ""), related=payment.get("invoice", ""), classification="Receivable settlement; not additional revenue")
    if full:
        linked_expenses = {p.get("expense") for p in state.get("payouts", []) if p.get("expense")}
        for expense in state.get("expenses", []):
            day = row_date(expense); amount = money(expense["amount"])
            tax = inclusive_gst(amount, expense.get("gst",0))
            credit = money(tax["gst_amount"]) if expense.get("gst_creditable", False) else ZERO
            cost = amount-credit
            direct_paid = expense.get("payment_status", "Paid") == "Paid" and expense["id"] not in linked_expenses
            if in_period(day):
                totals["operating_expenses"] += cost; totals["input_gst_credit"] += credit
                if direct_paid: cash_movement(expense.get("mode", "Bank transfer"), amount)
            append(day, "Expense", expense, expense.get("name", "Expense"), outgoing=amount if direct_paid else ZERO,
                   expense=cost, gst=credit, rate=expense.get("gst",0), mode=expense.get("mode", "Bank transfer") if direct_paid else "Unpaid", classification="Operating expense")
        for payroll in state.get("payroll", []):
            if payroll.get("status") != "Finalized": continue
            day = payroll_date(payroll); amount = money(payroll["amount"])
            if in_period(day): totals["salary_expense"] += amount
            append(day, "Payroll", payroll, "Salary accrued for " + payroll.get("period", ""), expense=amount, classification="Salary expense and payroll payable")
        for payout in state.get("payouts", []):
            day = row_date(payout); amount = money(payout["amount"]); category = payout.get("kind", "other_expense")
            cost = amount if category == "other_expense" or (category == "salary" and not payout.get("payroll")) else ZERO
            if in_period(day):
                cash_movement(payout.get("mode"), amount)
                if category == "salary" and cost: totals["salary_expense"] += cost
                elif cost: totals["operating_expenses"] += cost
                elif category not in ("salary", "expense"): totals["other_outflows"] += amount
            append(day, "Payout", payout, payout.get("payee", "") + (" — " + payout.get("note", "") if payout.get("note") else ""),
                   outgoing=amount, expense=cost, mode=payout.get("mode", ""), related=payout.get("payroll") or payout.get("expense") or "", classification=category.replace("_", " ").title())

    def receivables(as_of):
        billed = sum((money(i["amount"]) for i in invoices if row_date(i) and row_date(i)<=as_of), ZERO)
        received = sum((money(p["amount"]) for p in state.get("payments", []) if row_date(p) and row_date(p)<=as_of and p.get("invoice") in invoice_map and row_date(invoice_map[p["invoice"]]) and row_date(invoice_map[p["invoice"]])<=as_of), ZERO)
        return billed-received

    expense_payable, payroll_payable = ZERO, ZERO
    if full:
        for expense in expense_map.values():
            if not row_date(expense) or row_date(expense)>end or expense.get("payment_status", "Paid") == "Paid": continue
            paid = sum((money(p["amount"]) for p in state.get("payouts", []) if p.get("expense")==expense["id"] and row_date(p) and row_date(p)<=end), ZERO)
            expense_payable += money(expense["amount"])-paid
        for payroll in payroll_map.values():
            if payroll.get("status") != "Finalized" or not payroll_date(payroll) or payroll_date(payroll)>end: continue
            paid = sum((money(p["amount"]) for p in state.get("payouts", []) if p.get("payroll")==payroll["id"] and row_date(p) and row_date(p)<=end), ZERO)
            payroll_payable += money(payroll["amount"])-paid
    total_income = totals["net_revenue"]
    total_cost = totals["cogs"] + totals["operating_expenses"] + totals["salary_expense"]
    complete_rows = sorted(register, key=lambda r:(r["date"], r["source"], r["id"]))
    selected_rows = [r for r in complete_rows if zero_filter=="all" or r["source"] not in ("Invoice", "Receipt") or
                     (r["gst_rate"] is not None and ((r["gst_rate"]==0) == (zero_filter=="zero")))]
    return {"period":kind,"from":start.isoformat(),"to":end.isoformat(),"gst_scope":zero_filter,
        "summary_scope":"All company transactions, including 0% GST; the GST filter changes invoice/receipt rows only.",
        "basis":"Accrual profit; customer receipts settle receivables. Cash movement is reported separately. GST is excluded from revenue.",
        "full_statement":full,"invoice_count":invoice_count,"totals":{k:number(v) for k,v in totals.items()},
        "profit":number(total_income-total_cost) if full else None,
        "profit_is_estimate":bool(warnings),
        "gross_profit":number(total_income-totals["cogs"]) if full else None,
        "cash_movement":number(totals["cash_received"]-totals["cash_paid"]),
        "bank_movement":number(totals["bank_received"]-totals["bank_paid"]),
        "opening_receivables":number(receivables(start-timedelta(days=1))),"closing_receivables":number(receivables(end)),
        "expense_payable":number(expense_payable) if full else None,"payroll_payable":number(payroll_payable) if full else None,
        "gst_balance":number(totals["output_gst"]-totals["input_gst_credit"]),
        "transactions":selected_rows,"all_transaction_count":len(complete_rows),"shown_transaction_count":len(selected_rows),
        "trend":[{"label":key,"net_revenue":number(value)} for key,value in sorted(trend.items())],
        "warnings":sorted(set(warnings)),"limitations":["Cash/bank figures are period movements, not bank-statement balances.",
            "This operating statement needs opening balances, depreciation and other journal adjustments for a complete statutory balance sheet."]}


def statement_csv(report):
    stream = io.StringIO()
    columns = ["date","source","id","description","inflow","outflow","income","expense","gst","gst_rate","mode","reference","related","classification"]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for record in report["transactions"]:
        # Spreadsheet formula protection for user-controlled descriptions/references.
        safe = {k: ("'"+v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v) for k,v in record.items()}
        writer.writerow(safe)
    return "\ufeff" + stream.getvalue()
