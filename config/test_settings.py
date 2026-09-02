"""Isolated, deterministic tests. Never connect to the configured customer database."""
import os
os.environ["MYRAID_SKIP_ENV"] = "1"
from .settings import *  # noqa: F403,F401,E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ERP_ALLOW_PASSWORD_LOGIN = True
MEDIA_ROOT = BASE_DIR / "test-media"  # noqa: F405
