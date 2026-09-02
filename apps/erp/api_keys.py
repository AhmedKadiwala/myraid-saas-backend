import hashlib,hmac,ipaddress,secrets,socket
from datetime import timedelta
from urllib.parse import urlparse
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed,ValidationError
from . import models as m

def valid_webhook_url(url):
    parsed=urlparse(url)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:raise ValidationError("Webhook URLs must use HTTPS without embedded credentials.")
    try:addresses={item[4][0] for item in socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)}
    except OSError:raise ValidationError("Webhook hostname could not be resolved.")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:raise ValidationError("Webhook URLs cannot target private, local or link-local networks.")
    return url

class ERPApiKeyAuthentication(BaseAuthentication):
    def authenticate(self,request):
        raw=request.headers.get("X-ERP-API-Key","")
        if not raw:return None
        if "." not in raw:raise AuthenticationFailed("Invalid API key.")
        prefix=raw.split(".",1)[0]
        credential=m.ApiCredential.objects.select_related("user","tenant").filter(prefix=prefix,revoked_at__isnull=True,expires_at__gt=timezone.now(),archived=False).first()
        if not credential or not hmac.compare_digest(credential.key_hash,hashlib.sha256(raw.encode()).hexdigest()):raise AuthenticationFailed("Invalid or expired API key.")
        if not credential.user.is_active:raise AuthenticationFailed("API key owner is inactive.")
        requested=request.headers.get("X-Tenant-ID")
        if requested and str(requested)!=str(credential.tenant_id):raise AuthenticationFailed("API key is not valid for the requested company.")
        request.requested_tenant_id=str(credential.tenant_id);request.api_credential=credential
        return credential.user,None

def create_key(tenant,user,name,permissions,days):
    from apps.core.services import effective_permissions
    from .security import features
    if "api_access" not in features(tenant):raise ValidationError("The API access module is not enabled.")
    effective=effective_permissions(user,tenant)
    if "*" not in effective and set(permissions)-effective:raise ValidationError("An API key cannot receive permissions its owner does not currently hold.")
    if not 1<=int(days)<=180:raise ValidationError("API key expiry must be between 1 and 180 days.")
    raw=f"mra_{secrets.token_urlsafe(8)}.{secrets.token_urlsafe(32)}";prefix=raw.split(".",1)[0]
    obj=m.ApiCredential.objects.create(tenant=tenant,created_by=user,user=user,name=name,prefix=prefix,key_hash=hashlib.sha256(raw.encode()).hexdigest(),permissions=sorted(set(permissions)),expires_at=timezone.now()+timedelta(days=int(days)))
    return obj,raw
