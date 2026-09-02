"""Explicit local ERP development profile; does not read production credentials."""
import os
os.environ["MYRAID_SKIP_ENV"] = "1"
from .settings import *  # noqa: F403,F401,E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "erp-development.sqlite3"}}  # noqa: F405
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
CORS_ALLOWED_ORIGINS = ["http://localhost:5175", "http://127.0.0.1:5175", "http://localhost:5173"]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
ERP_EMAIL_ENABLED = True
ERP_SMS_ENABLED = True
ERP_DEV_FIXED_OTP = "654321"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
