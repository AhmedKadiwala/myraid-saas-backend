import uuid
import boto3
from botocore.config import Config
from django.conf import settings
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated


def get_r2_client():
    """
    Initialize reusable S3v4 client pointing to Cloudflare R2.
    Defaults to Cloudflare R2 endpoint with auto region and path addressing.
    """
    account_id = getattr(settings, "CLOUDFLARE_ACCOUNT_ID", "")
    endpoint_url = (
        f"https://{account_id}.r2.cloudflarestorage.com"
        if account_id
        else getattr(settings, "AWS_S3_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    )
    access_key = getattr(
        settings,
        "R2_ACCESS_KEY_ID",
        getattr(settings, "AWS_ACCESS_KEY_ID", "r2_key"),
    )
    secret_key = getattr(
        settings,
        "R2_SECRET_ACCESS_KEY",
        getattr(settings, "AWS_SECRET_ACCESS_KEY", "r2_secret"),
    )

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key or "r2_key",
        aws_secret_access_key=secret_key or "r2_secret",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="auto",
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_r2_presigned_upload(request):
    """
    Generates a direct browser-to-Cloudflare-R2 PUT URL.
    Zero file bytes ever pass through the Oracle Cloud VPS.
    """
    filename = str(request.data.get("filename", "attachment.pdf")).replace("/", "_")
    content_type = request.data.get("content_type", "application/pdf")
    doc_category = request.data.get("category", "work_orders")
    object_key = f"erp/{doc_category}/{uuid.uuid4().hex}_{filename}"

    r2_client = get_r2_client()
    bucket_name = getattr(
        settings,
        "R2_BUCKET_NAME",
        getattr(settings, "AWS_STORAGE_BUCKET_NAME", "myraid"),
    )

    upload_url = r2_client.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket_name,
            "Key": object_key,
            "ContentType": content_type,
        },
        ExpiresIn=600,  # Valid for 10 minutes
    )
    return JsonResponse({"upload_url": upload_url, "object_key": object_key})
