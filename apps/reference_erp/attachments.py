"""Private sales PDFs, shared through the order/job relationship."""
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from django.utils import timezone
from django.db import transaction
from rest_framework.exceptions import ValidationError, NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.core.api import APIView
from .access import resolve, has_permission
from .models import ERPRecord, OrderAttachment, AuditEvent


def order_access(request, order_code, write=False):
    tenant, _ = resolve(request)
    order = ERPRecord.objects.filter(tenant=tenant, collection="orders", code=order_code).first()
    if not order:
        raise NotFound("Sales order not found in this company.")
    allowed = has_permission(request, "order.edit") or has_permission(request, "order.create") if write else has_permission(request, "order.view") or has_permission(request, "job.view")
    if not allowed:
        raise PermissionDenied("You cannot access this order's attachments.")
    if write and order.payload.get("read_only"):
        raise ValidationError("Imported legacy orders cannot be edited here.")
    return tenant


class OrderAttachmentsView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, order_code):
        tenant = order_access(request, order_code, write=True)
        upload = request.FILES.get("file")
        if not upload or upload.size > 10 * 1024 * 1024:
            raise ValidationError("Choose a PDF no larger than 10 MB.")
        content = upload.read()
        if Path(upload.name).suffix.lower() != ".pdf" or not content.startswith(b"%PDF-"):
            raise ValidationError("Choose a genuine PDF file.")
        if any(token in content for token in (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile")):
            raise ValidationError("Active or embedded PDF content is not accepted.")
        key = f"workspace-orders/{tenant.pk}/{uuid.uuid4().hex}.pdf"
        root = Path(getattr(settings, "ERP_PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private-media"))
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        try:
            attachment = OrderAttachment.objects.create(tenant=tenant, order_code=order_code, name=Path(upload.name).name[:200], object_key=key, size=len(content), uploaded_by=request.user)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        AuditEvent.objects.create(tenant=tenant, user=request.user, message=f"PDF attached to {order_code}: {attachment.name}")
        return Response({"id": attachment.pk, "name": attachment.name, "size": attachment.size}, status=201)


class OrderAttachmentDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        tenant, _ = resolve(request)
        attachment = OrderAttachment.objects.filter(tenant=tenant, pk=pk).first()
        if not attachment:
            raise NotFound()
        order_access(request, attachment.order_code)
        root = Path(getattr(settings, "ERP_PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private-media")).resolve()
        target = (root / attachment.object_key).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise NotFound("File unavailable.")
        response = FileResponse(target.open("rb"), as_attachment=True, filename=attachment.name, content_type="application/pdf")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DocumentPDFView(APIView):
    """
    Generates and returns on-demand, time-limited presigned R2 download URLs for business documents.
    Permanent records stay in PostgreSQL; downloadable PDFs stay in R2 for 30 days and can be regenerated on demand.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_type, doc_code):
        tenant, member = resolve(request)
        doc_type = doc_type.lower()
        if doc_type not in ("invoice", "quote", "order", "job", "payroll"):
            raise ValidationError("Unsupported document type for PDF generation.")

        # Permission verification
        perm_map = {
            "invoice": "invoice.view",
            "quote": "quotation.view",
            "order": "order.view",
            "job": "job.view",
            "payroll": "payroll.view",
        }
        if not (member.is_tenant_admin or has_permission(request, perm_map[doc_type])):
            raise PermissionDenied(f"You do not have permission to access {doc_type} documents.")

        col_map = {
            "invoice": "invoices",
            "quote": "quotes",
            "order": "orders",
            "job": "jobs",
            "payroll": "payroll",
        }
        rec = ERPRecord.objects.filter(tenant=tenant, collection=col_map[doc_type], code=doc_code).first()
        if not rec:
            raise NotFound(f"{doc_type.title()} {doc_code} not found in this company.")

        payload = rec.payload
        from apps.core.r2_storage import get_or_regenerate_document
        from apps.erp.documents import make_pdf

        def generator_fn():
            company_name = tenant.name
            title = f"{doc_type.upper()} {doc_code}"
            headers = ["Field", "Value"]
            meta = [
                f"Document: {doc_code}",
                f"Generated for: {company_name}",
                f"Generated on: {timezone.now().strftime('%Y-%m-%d %H:%M')}",
            ]
            rows = []
            for k, v in sorted(payload.items()):
                if isinstance(v, (str, int, float, bool)) and v not in (None, ""):
                    rows.append([k.replace("_", " ").title(), str(v)])
            totals = []
            if "amount" in payload:
                totals.append(f"Final Amount (GST inclusive): INR {float(payload['amount']):,.2f}")
            if "taxable" in payload:
                totals.append(f"Taxable: INR {float(payload['taxable']):,.2f}")
            if "gst_amount" in payload:
                totals.append(f"GST: INR {float(payload['gst_amount']):,.2f}")
            return make_pdf(title, company_name, meta, headers, rows, totals=totals, note="Permanent business data retained in database. Generated copy expires from cloud cache after 30 days.")

        filename = f"{tenant.slug}-{doc_type}-{doc_code}.pdf"
        doc, url, regenerated = get_or_regenerate_document(
            tenant=tenant,
            doc_type=doc_type,
            doc_code=doc_code,
            filename=filename,
            generator_callable=generator_fn,
        )

        AuditEvent.objects.create(
            tenant=tenant,
            user=request.user,
            message=f"{doc_type.title()} {doc_code} PDF requested ({'regenerated' if regenerated else 'cached'})"
        )

        if request.query_params.get("redirect") == "1":
            from django.shortcuts import redirect
            return redirect(url)

        return Response({
            "document_type": doc_type,
            "document_code": doc_code,
            "filename": doc.filename,
            "url": url,
            "expires_at": doc.expires_at.isoformat(),
            "regenerated": regenerated,
            "retention_policy": "Stored in Cloudflare R2 for 30 days. Regenerate anytime from permanent DB records.",
        })


class LocalFallbackDownloadView(APIView):
    """
    Local fallback endpoint for serving private media when running without Cloudflare R2.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant, _ = resolve(request)
        key = str(request.query_params.get("key", "")).strip()
        filename = str(request.query_params.get("name", "document.pdf")).strip()
        if not key or f"tenants/{tenant.pk}/" not in key and f"workspace-orders/{tenant.pk}/" not in key and f"{tenant.pk}/" not in key:
            raise PermissionDenied("You can only download files belonging to your active company.")
        root = Path(getattr(settings, "ERP_PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private-media")).resolve()
        target = (root / key).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise NotFound("File unavailable or expired.")
        response = FileResponse(target.open("rb"), as_attachment=True, filename=filename, content_type="application/pdf")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
