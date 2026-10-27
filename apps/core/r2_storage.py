"""Cloudflare R2 S3-compatible storage adapter for ephemeral generated documents."""
import io
import logging
import uuid
from datetime import timedelta
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.utils import timezone

from apps.reference_erp.models import GeneratedDocument

logger = logging.getLogger(__name__)

DOC_RETENTION_DAYS = 30
PRESIGNED_URL_EXPIRY_SECONDS = 900  # 15 minutes


def get_s3_client():
    endpoint = getattr(settings, "AWS_S3_ENDPOINT_URL", "")
    access_key = getattr(settings, "AWS_ACCESS_KEY_ID", "")
    secret_key = getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
    region = getattr(settings, "AWS_S3_REGION_NAME", "auto")

    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def is_r2_configured():
    return bool(
        getattr(settings, "AWS_ACCESS_KEY_ID", "")
        and getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        and getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
    )


def upload_bytes_to_r2(object_key: str, data: bytes, content_type: str = "application/pdf") -> bool:
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "myraid")
    if is_r2_configured():
        try:
            client = get_s3_client()
            client.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )
            return True
        except Exception as exc:
            logger.warning("R2 upload failed, falling back to local media: %s", exc)

    # Local fallback for environments without R2/S3 access
    root = Path(getattr(settings, "ERP_PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private-media"))
    target = root / object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return True


def generate_presigned_download_url(object_key: str, filename: str = "", expires_in: int = PRESIGNED_URL_EXPIRY_SECONDS) -> str:
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "myraid")
    params = {"Bucket": bucket, "Key": object_key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'

    if is_r2_configured():
        try:
            client = get_s3_client()
            url = client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_in,
            )
            # If a custom public endpoint is configured, swap host
            public_endpoint = getattr(settings, "AWS_S3_PUBLIC_ENDPOINT_URL", "")
            internal_endpoint = getattr(settings, "AWS_S3_ENDPOINT_URL", "")
            if public_endpoint and internal_endpoint and internal_endpoint in url:
                url = url.replace(internal_endpoint, public_endpoint)
            return url
        except Exception as exc:
            logger.warning("Presigned URL generation failed: %s", exc)

    # Fallback endpoint URL
    return f"/api/v1/workspace-ui/documents/download-key/?key={object_key}&name={filename}"


def delete_r2_object(object_key: str) -> bool:
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "myraid")
    if is_r2_configured():
        try:
            client = get_s3_client()
            client.delete_object(Bucket=bucket, Key=object_key)
        except Exception as exc:
            logger.warning("R2 object deletion error for %s: %s", object_key, exc)

    root = Path(getattr(settings, "ERP_PRIVATE_MEDIA_ROOT", settings.BASE_DIR / "private-media"))
    target = root / object_key
    if target.exists():
        target.unlink(missing_ok=True)
    return True


def store_generated_document(tenant, doc_type: str, doc_code: str, data: bytes, filename: str, content_type: str = "application/pdf"):
    """
    Stores an ephemeral generated document in Cloudflare R2 with a 30-day retention period.
    """
    token = uuid.uuid4().hex[:12]
    ext = Path(filename).suffix or ".pdf"
    object_key = f"tenants/{tenant.pk}/generated/{doc_type}/{doc_code}_{token}{ext}"

    upload_bytes_to_r2(object_key, data, content_type=content_type)
    expires_at = timezone.now() + timedelta(days=DOC_RETENTION_DAYS)

    doc, _ = GeneratedDocument.objects.update_or_create(
        tenant=tenant,
        document_type=doc_type,
        document_code=doc_code,
        defaults={
            "filename": filename,
            "object_key": object_key,
            "size": len(data),
            "content_type": content_type,
            "expires_at": expires_at,
            "is_deleted": False,
        },
    )

    url = generate_presigned_download_url(object_key, filename=filename)
    return doc, url


def get_or_regenerate_document(tenant, doc_type: str, doc_code: str, filename: str, generator_callable):
    """
    Retrieves a valid, unexpired document link or regenerates it from permanent DB data if expired.
    """
    now = timezone.now()
    existing = GeneratedDocument.objects.filter(
        tenant=tenant,
        document_type=doc_type,
        document_code=doc_code,
        expires_at__gt=now,
        is_deleted=False,
    ).first()

    if existing:
        url = generate_presigned_download_url(existing.object_key, filename=existing.filename)
        return existing, url, False

    # Document expired or never generated; regenerate from permanent DB records
    pdf_bytes = generator_callable()
    doc, url = store_generated_document(tenant, doc_type, doc_code, pdf_bytes, filename)
    return doc, url, True


def cleanup_expired_r2_documents() -> int:
    """
    Automated cleanup task to prune documents older than 30 days from R2 and update database records.
    """
    now = timezone.now()
    expired_docs = GeneratedDocument.objects.filter(expires_at__lte=now, is_deleted=False)
    count = 0
    for doc in expired_docs[:200]:
        delete_r2_object(doc.object_key)
        doc.is_deleted = True
        doc.save(update_fields=["is_deleted"])
        count += 1
    return count
