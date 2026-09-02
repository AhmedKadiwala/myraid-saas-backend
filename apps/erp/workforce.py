import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import models as m
from .money import money, decimal, ZERO
from .services import Conflict, fingerprint, record_event, touch, fact, open_period


def leave_days(request):
    if request.end_date < request.start_date:
        raise ValidationError({"end_date": "End date must be after the start date."})
    if request.half_day and request.start_date != request.end_date:
        raise ValidationError({"half_day": "A half-day request must cover one date."})
    holidays = set(m.Holiday.objects.filter(tenant=request.tenant, archived=False, date__range=(request.start_date, request.end_date)).filter(Q(branch__isnull=True) | Q(branch=request.employee.branch)).values_list("date", flat=True))
    offs = request.employee.shift.weekly_offs if request.employee.shift else [6]
    count = 0
    day = request.start_date
    while day <= request.end_date:
        if day.weekday() not in offs and day not in holidays:
            count += 1
        day += timedelta(days=1)
    return Decimal(".5") if request.half_day and count else Decimal(count)


def review_leave(leave, actor, decision):
    if leave.status != "pending":
        raise Conflict("This leave request has already been reviewed.")
    if decision not in ("approved", "rejected"):
        raise ValidationError({"decision": "Choose approve or reject."})
    if leave.employee.user_id == actor.pk:
        raise ValidationError("You cannot approve your own leave request.")
    if decision == "approved":
        employee = m.Employee.objects.select_for_update().get(pk=leave.employee_id)
        if m.LeaveRequest.objects.filter(employee=employee, status="approved", start_date__lte=leave.end_date, end_date__gte=leave.start_date).exclude(pk=leave.pk).exists():
            raise Conflict("An approved leave request already overlaps these dates.")
        if m.Attendance.objects.filter(employee=employee, date__range=(leave.start_date, leave.end_date), locked=True).exists():
            raise Conflict("Payroll has locked these attendance dates. Record a future-period adjustment.")
        used = m.LeaveRequest.objects.filter(employee=employee, leave_type=leave.leave_type, status="approved", start_date__year=leave.start_date.year).aggregate(v=Sum("days"))["v"] or ZERO
        leave.days = leave_days(leave)
        if leave.days <= 0:
            raise ValidationError("The selected dates contain no working days.")
        if leave.leave_type.paid and used + leave.days > leave.leave_type.annual_allowance:
            raise ValidationError("There is not enough paid leave available. Select an unpaid leave type.")
    leave.status = decision
    leave.reviewed_by = actor
    touch(leave)
    record_event(leave, actor, f"leave.{decision}")
    return leave


def payroll_inputs(run):
    start = run.month.replace(day=1)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    employees = m.Employee.objects.filter(tenant=run.tenant, archived=False, joining_date__lte=end).filter(Q(exit_date__isnull=True) | Q(exit_date__gte=start)).order_by("id")
    if run.branch_id:
        employees = employees.filter(branch=run.branch)
    inputs = []
    from .security import features
    attendance_enabled = "attendance_hr" in features(run.tenant)
    for employee in employees:
        attendance = list(m.Attendance.objects.filter(employee=employee, date__range=(start, end)).order_by("date").values("id", "version", "date", "status", "approved_ot_hours", "check_in", "check_out"))
        leaves = list(m.LeaveRequest.objects.filter(employee=employee, status="approved", start_date__lte=end, end_date__gte=start).values("id", "version", "start_date", "end_date", "days", "half_day", "leave_type__paid"))
        components = list(m.SalaryComponent.objects.filter(employee=employee, archived=False, effective_from__lte=end).filter(Q(effective_until__isnull=True) | Q(effective_until__gte=start)).order_by("id").values("id", "name", "kind", "amount", "prorate", "version", "effective_from", "effective_until"))
        loans = list(m.EmployeeLoan.objects.filter(employee=employee, status="active", archived=False).values("id", "principal", "recovered", "monthly_recovery", "version"))
        holidays = list(m.Holiday.objects.filter(tenant=run.tenant, archived=False, date__range=(start, end)).filter(Q(branch__isnull=True) | Q(branch=employee.branch)).values("date", "paid"))
        inputs.append({"employee_id": str(employee.pk), "employee_name": employee.name, "employee_version": employee.version,
            "monthly_salary": employee.monthly_salary, "joining_date": employee.joining_date, "exit_date": employee.exit_date,
            "branch_id": employee.branch_id, "department_id": str(employee.department_id) if employee.department_id else None,
            "weekly_offs": employee.shift.weekly_offs if employee.shift else [6], "attendance": attendance,
            "leaves": leaves, "components": components, "loans": loans, "holidays": holidays, "attendance_enabled": attendance_enabled})
    return start, end, inputs


def calculate_payroll(run, actor, manual_inputs=None):
    if run.status not in ("draft", "review"):
        raise Conflict("Reopen review before changing an approved payroll run.")
    start, end, inputs = payroll_inputs(run)
    if not inputs:
        raise ValidationError("No eligible employees were found for this payroll period.")
    manual_inputs = manual_inputs or {}
    run.results.all().delete()  # Only unposted calculation snapshots are replaceable.
    total_gross = total_deductions = total_net = total_cost = ZERO
    from .services import snapshot
    for data in inputs:
        attendance = {a["date"]: a for a in data["attendance"]}
        holidays = {h["date"]: h["paid"] for h in data["holidays"]}
        eligible = paid_days = ot = ZERO
        warnings = []
        day = start
        while day <= end:
            if day < data["joining_date"] or (data["exit_date"] and day > data["exit_date"]):
                day += timedelta(days=1)
                continue
            eligible += 1
            record = attendance.get(day)
            approved_leave = next((v for v in data["leaves"] if v["start_date"] <= day <= v["end_date"]), None)
            if day in holidays:
                paid_days += 1 if holidays[day] else 0
            elif day.weekday() in data["weekly_offs"]:
                paid_days += 1
            elif approved_leave:
                paid_days += (Decimal(".5") if approved_leave["half_day"] else 1) if approved_leave["leave_type__paid"] else 0
                if approved_leave["half_day"] and record and record["status"] in ("present", "half_day"):
                    paid_days += Decimal(".5")
            elif record:
                paid_days += {"present": 1, "half_day": Decimal(".5"), "weekly_off": 1, "holiday": 1}.get(record["status"], 0)
                if record["check_in"] and not record["check_out"]:
                    warnings.append(f"Missing check-out on {day.isoformat()}")
            elif data["attendance_enabled"]:
                warnings.append(f"Missing attendance on {day.isoformat()}")
            if record:
                ot += record["approved_ot_hours"]
            day += timedelta(days=1)
        manual = manual_inputs.get(data["employee_id"])
        if manual:
            if not manual.get("reason"):
                raise ValidationError("Manual payroll input needs a reason.")
            paid_days = decimal(manual.get("payable_days"), "payable_days", 0)
            if paid_days > eligible:
                raise ValidationError("Payable days cannot exceed eligible calendar days.")
            warnings = []
            data["manual_override"] = manual
        elif not data["attendance_enabled"]:
            warnings.append("Attendance module is disabled. Enter reviewed manual payable days.")
        divisor = Decimal(end.day)
        salary = money(data["monthly_salary"] * paid_days / divisor)
        calculated = [{"name": "Basic salary", "kind": "earning", "amount": str(salary)}]
        gross = salary
        deductions = employer = ZERO
        for c in data["components"]:
            if c["effective_from"] > start or (c["effective_until"] and c["effective_until"] < end):
                warnings.append(f"{c['name']} changes during this period; review a manual component adjustment.")
            amount = money(c["amount"] * paid_days / divisor) if c["prorate"] else c["amount"]
            calculated.append({"name": c["name"], "kind": c["kind"], "amount": str(amount)})
            if c["kind"] == "earning": gross += amount
            elif c["kind"] == "deduction": deductions += amount
            else: employer += amount
        for loan in data["loans"]:
            amount = min(loan["monthly_recovery"], loan["principal"] - loan["recovered"])
            if amount > 0:
                deductions += amount
                calculated.append({"name": "Loan recovery", "kind": "loan", "loan_id": str(loan["id"]), "amount": str(amount)})
        if ot and not any("overtime" in c["name"].lower() for c in calculated):
            warnings.append(f"{ot:g} approved overtime hours need an overtime earning component.")
        net = gross - deductions
        if net < 0: warnings.append("Deductions exceed earnings.")
        result = m.PayrollResult.objects.create(tenant=run.tenant, branch_id=data["branch_id"], created_by=actor,
            run=run, employee_id=data["employee_id"], payable_days=paid_days, gross=gross, deductions=deductions, net=net,
            employer_cost=gross + employer, components=calculated, warnings=warnings, input_snapshot=snapshot(data))
        total_gross += result.gross; total_deductions += result.deductions; total_net += result.net; total_cost += result.employer_cost
    run.gross, run.deductions, run.net, run.employer_cost = total_gross, total_deductions, total_net, total_cost
    run.input_hash = fingerprint(inputs)
    run.status = "review"
    touch(run)
    record_event(run, actor, "payroll.calculated")
    return run


def finalize_payroll(run, actor):
    if run.status != "approved":
        raise Conflict("Payroll must be reviewed and approved before finalization.")
    start, end, inputs = payroll_inputs(run)
    if fingerprint(inputs) != run.input_hash:
        raise Conflict("Attendance, employee details or salary inputs changed. Recalculate and review payroll.")
    open_period(run.tenant, run.month)
    if not run.results.exists() or any(r.warnings for r in run.results.all()):
        raise ValidationError("Resolve all payroll input exceptions before finalizing.")
    for result in run.results.select_for_update():
        fact(result, "opex", result.employer_cost, f"Payroll · {run.month:%B %Y}", "Payroll", business_date=run.month)
        for component in result.components:
            if component["kind"] == "loan":
                loan = m.EmployeeLoan.objects.select_for_update().get(tenant=run.tenant, pk=component["loan_id"])
                amount = decimal(component["amount"])
                if loan.recovered + amount > loan.principal:
                    raise Conflict("The loan balance changed. Recalculate payroll.")
                loan.recovered += amount
                if loan.recovered == loan.principal: loan.status = "closed"
                touch(loan)
        m.Attendance.objects.filter(employee=result.employee, date__range=(start, end)).update(locked=True)
    run.status = "finalized"
    run.finalized_at = timezone.now()
    touch(run)
    record_event(run, actor, "payroll.finalized")
    return run
