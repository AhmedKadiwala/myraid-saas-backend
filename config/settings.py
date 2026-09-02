import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent


def load_project_env():
    if os.getenv("MYRAID_SKIP_ENV") == "1":
        return
    env_path = next(
        (path for path in (BASE_DIR / ".env", BASE_DIR.parent / ".env") if path.exists()),
        None,
    )
    if env_path is None:
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


load_project_env()


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "unsafe-local-development-key-change-me",
)

DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"


ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv(
        "DJANGO_ALLOWED_HOSTS",
        "myraid-saas-backend.onrender.com,localhost,127.0.0.1",
    ).split(",")
    if h.strip()
]

render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")

if render_hostname and render_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(render_hostname)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_celery_beat",

    "apps.core",
    "apps.erp",
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    # Serve Django/static admin assets in production.
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.TenantContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]


ROOT_URLCONF = "config.urls"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]


WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
    )
}


AUTH_USER_MODEL = "core.User"


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
        "OPTIONS": {
            "min_length": 6,
        },
    },
]


LANGUAGE_CODE = "en-us"

TIME_ZONE = os.getenv(
    "TIME_ZONE",
    "Asia/Kolkata",
)

USE_I18N = True
USE_TZ = True


# -------------------------------------------------------------------
# STATIC FILES
# -------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage."
            "CompressedManifestStaticFilesStorage"
        ),
    },
}


WHITENOISE_AUTOREFRESH = DEBUG


# -------------------------------------------------------------------
# MEDIA
# -------------------------------------------------------------------

MEDIA_URL = "/media/"

MEDIA_ROOT = Path(
    os.getenv(
        "MEDIA_ROOT",
        BASE_DIR.parent / "media",
    )
)


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -------------------------------------------------------------------
# CORS / CSRF
# -------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = [
    value.strip()
    for value in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173",
    ).split(",")
    if value.strip()
]


CORS_ALLOW_CREDENTIALS = True


CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-tenant-id",
    "x-branch-id",
    "idempotency-key",
    "if-match",
)


CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        ",".join(CORS_ALLOWED_ORIGINS),
    ).split(",")
    if value.strip()
]


for local_origin in (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
):
    if local_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(local_origin)


# -------------------------------------------------------------------
# DJANGO REST FRAMEWORK
# -------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.erp.api_keys.ERPApiKeyAuthentication",
        "apps.core.authentication.CookieOrHeaderJWTAuthentication",
    ),

    "DEFAULT_PERMISSION_CLASSES": (
        "apps.core.permissions.TenantRBACPermission",
    ),

    "DEFAULT_SCHEMA_CLASS": (
        "drf_spectacular.openapi.AutoSchema"
    ),

    "DEFAULT_PAGINATION_CLASS": (
        "apps.core.pagination.LegacyPagination"
    ),

    "PAGE_SIZE": 20,

    "EXCEPTION_HANDLER": (
        "apps.core.exceptions."
        "legacy_exception_handler"
    ),
}


# -------------------------------------------------------------------
# JWT
# -------------------------------------------------------------------

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(
            os.getenv(
                "JWT_ACCESS_MINUTES",
                "15",
            )
        )
    ),

    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(
            os.getenv(
                "JWT_REFRESH_DAYS",
                "7",
            )
        )
    ),

    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,

    "SIGNING_KEY": SECRET_KEY,
}


# -------------------------------------------------------------------
# OPENAPI / SWAGGER
# -------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    "TITLE": "Myraid ERP SaaS API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}


# -------------------------------------------------------------------
# REDIS / CACHE / CELERY
# -------------------------------------------------------------------

REDIS_URL = (
    os.getenv("REDIS_URL")
    or "redis://redis:6379/0"
)


CACHE_BACKEND = os.getenv(
    "CACHE_BACKEND",
    "django.core.cache.backends.locmem.LocMemCache",
)


CACHES = {
    "default": {
        "BACKEND": CACHE_BACKEND,
    }
}


if CACHE_BACKEND == (
    "django.core.cache.backends.redis.RedisCache"
):
    CACHES["default"]["LOCATION"] = REDIS_URL


CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL


CELERY_TASK_ALWAYS_EAGER = (
    os.getenv(
        "CELERY_TASK_ALWAYS_EAGER",
        "0",
    )
    == "1"
)


CELERY_BEAT_SCHEDULER = (
    "django_celery_beat.schedulers:"
    "DatabaseScheduler"
)


# -------------------------------------------------------------------
# EMAIL
# -------------------------------------------------------------------

EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
)


EMAIL_HOST = os.getenv(
    "EMAIL_HOST",
    "mailpit",
)


EMAIL_PORT = int(
    os.getenv(
        "EMAIL_PORT",
        "1025",
    )
)


DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    "noreply@myraid.local",
)


# -------------------------------------------------------------------
# ERP SETTINGS
# -------------------------------------------------------------------

ERP_EMAIL_ENABLED = (
    os.getenv(
        "ERP_EMAIL_ENABLED",
        "0",
    )
    == "1"
)


ERP_ALLOW_PASSWORD_LOGIN = (
    os.getenv(
        "ERP_ALLOW_PASSWORD_LOGIN",
        "0",
    )
    == "1"
)


ERP_DEV_FIXED_OTP = (
    os.getenv(
        "ERP_DEV_FIXED_OTP",
        "",
    )
    if DEBUG
    else ""
)


# OTP delivery mode:
# - "mock": no SMS gateway is used. Intended for seeded/mock phone numbers only.
# - "sms": real SMS delivery is used through MSG91.
#
# During the current development/testing phase we default to "mock".
ERP_OTP_MODE = os.getenv(
    "ERP_OTP_MODE",
    "mock",
).strip().lower()

ERP_MOCK_OTP = os.getenv(
    "ERP_MOCK_OTP",
    "123456",
).strip()

# seed_mock_data.py uses 88888xxxxx phone numbers. Mock OTP mode is restricted
# to that range so real/admin phone numbers do not receive a test OTP.
ERP_MOCK_PHONE_PREFIX = os.getenv(
    "ERP_MOCK_PHONE_PREFIX",
    "88888",
).strip()


ERP_SMS_ENABLED = (
    os.getenv(
        "ERP_SMS_ENABLED",
        "0",
    )
    == "1"
)


MSG91_FLOW_URL = os.getenv(
    "MSG91_FLOW_URL",
    "https://api.msg91.com/api/v5/flow/",
)


MSG91_AUTH_KEY = os.getenv(
    "MSG91_AUTH_KEY",
    "",
)


MSG91_OTP_FLOW_ID = os.getenv(
    "MSG91_OTP_FLOW_ID",
    "",
)


MSG91_SENDER_ID = os.getenv(
    "MSG91_SENDER_ID",
    "",
)


MSG91_OTP_VARIABLE = os.getenv(
    "MSG91_OTP_VARIABLE",
    "OTP",
)


WHATSAPP_API_URL = os.getenv(
    "WHATSAPP_API_URL",
    "",
)


WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN",
    "",
)


ERP_ENCRYPTION_KEYS = [
    value.strip()
    for value in os.getenv(
        "ERP_ENCRYPTION_KEYS",
        "",
    ).split(",")
    if value.strip()
]


# -------------------------------------------------------------------
# STORAGE / CLOUDFLARE R2 / S3
# -------------------------------------------------------------------

STORAGE_BACKEND = os.getenv(
    "STORAGE_BACKEND",
    "local",
)


AWS_ACCESS_KEY_ID = os.getenv(
    "AWS_ACCESS_KEY_ID",
    "minioadmin",
)


AWS_SECRET_ACCESS_KEY = os.getenv(
    "AWS_SECRET_ACCESS_KEY",
    "minioadmin",
)


AWS_STORAGE_BUCKET_NAME = os.getenv(
    "AWS_STORAGE_BUCKET_NAME",
    "myraid",
)


AWS_S3_ENDPOINT_URL = os.getenv(
    "AWS_S3_ENDPOINT_URL",
    "http://minio:9000",
)


AWS_S3_PUBLIC_ENDPOINT_URL = os.getenv(
    "AWS_S3_PUBLIC_ENDPOINT_URL",
    AWS_S3_ENDPOINT_URL,
)


# -------------------------------------------------------------------
# RAZORPAY
# -------------------------------------------------------------------

RAZORPAY_KEY_ID = os.getenv(
    "RAZORPAY_KEY_ID",
    "",
)


RAZORPAY_KEY_SECRET = os.getenv(
    "RAZORPAY_KEY_SECRET",
    "",
)


RAZORPAY_WEBHOOK_SECRET = os.getenv(
    "RAZORPAY_WEBHOOK_SECRET",
    "",
)


RAZORPAY_MODE = os.getenv(
    "RAZORPAY_MODE",
    "test",
)


# -------------------------------------------------------------------
# PRODUCTION SECURITY
# -------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)


SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG


SESSION_COOKIE_SAMESITE = os.getenv(
    "SESSION_COOKIE_SAMESITE",
    "Lax",
)


CSRF_COOKIE_SAMESITE = os.getenv(
    "CSRF_COOKIE_SAMESITE",
    SESSION_COOKIE_SAMESITE,
)


SECURE_SSL_REDIRECT = (
    os.getenv(
        "SECURE_SSL_REDIRECT",
        "0" if DEBUG else "1",
    )
    == "1"
)