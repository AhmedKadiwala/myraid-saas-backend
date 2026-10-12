from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from apps.core.api import APIView
from apps.erp.security import has_permission
from .access import resolve
from .views import get_state, filtered_state
from .reporting import build_statement, statement_csv


class StatementView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant, member = resolve(request)
        full = member.is_tenant_admin or has_permission(request, "financial_statement.view")
        invoices_allowed = full or has_permission(request, "invoice.view")
        payments_allowed = full or has_permission(request, "payment.view")
        if not (invoices_allowed or payments_allowed):
            raise PermissionDenied("Financial record access is required.")
        if request.query_params.get("full") == "1" and not full:
            raise PermissionDenied("A company admin or financial-statement grant is required for the full statement.")
        raw = get_state(tenant)
        report = build_statement(raw if full else filtered_state(request, member, raw), request.query_params, full=full)
        report.update(can_view_invoices=invoices_allowed, can_view_payments=payments_allowed)
        if not full:
            report["summary_scope"] = "Only financial records authorized by your role. The GST filter changes invoice/receipt rows only."
        if not payments_allowed or not invoices_allowed:
            report["opening_receivables"] = report["closing_receivables"] = None
        if not payments_allowed:
            for key in ("cash_received", "bank_received", "cash_paid", "bank_paid"):report["totals"][key] = None
            report["cash_movement"] = report["bank_movement"] = None
        if not invoices_allowed:
            for key in ("gross_invoiced", "net_revenue", "output_gst"):report["totals"][key] = None
            report["invoice_count"] = report["gst_balance"] = None
            report["trend"] = []
            report["transactions"] = [row for row in report["transactions"] if row["source"] != "Invoice"]
            report["shown_transaction_count"] = len(report["transactions"])
        if request.query_params.get("export") == "csv":
            response = HttpResponse(statement_csv(report), content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="financial-statement-{report["from"]}-{report["to"]}.csv"'
            return response
        return Response(report)
