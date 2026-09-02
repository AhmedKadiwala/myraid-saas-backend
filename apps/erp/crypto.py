import base64
import hashlib
from django.conf import settings
from cryptography.fernet import Fernet, MultiFernet
from rest_framework.exceptions import ValidationError

def keyring():
    keys=settings.ERP_ENCRYPTION_KEYS
    if not keys and settings.DEBUG:keys=[base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()]
    if not keys:raise ValidationError("Server encryption keys are not configured. Secret-bearing integrations are disabled.")
    try:return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception:raise ValidationError("Server encryption key configuration is invalid.")

def encrypt(value):return keyring().encrypt(value.encode()).decode()
def decrypt(value):return keyring().decrypt(value.encode()).decode()
