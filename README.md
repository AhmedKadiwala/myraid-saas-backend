# Myraid SaaS Backend

Django REST Framework backend for Myraid ERP SaaS. It includes company-isolated ERP and Sales records, phone OTP authentication, per-company dynamic RBAC, Celery tasks, billing hooks, private documents and API documentation.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item ..\.env.local.backup ..\.env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py bootstrap_saas
.\.venv\Scripts\python.exe manage.py runserver
```

The API runs at `http://localhost:8000/api/v1/` by default.
Public health check: `http://localhost:8000/api/health/`.

## Checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
```

## Render + Neon

Use `backend` as the Render service root. The included `render.yaml` runs
`bash build.sh`, which installs dependencies, collects static files and applies
migrations before starting Gunicorn.

Set these Render environment variables:

```text
DJANGO_SECRET_KEY=<long random secret>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=<your-render-service>.onrender.com
DATABASE_URL=<your Neon pooled PostgreSQL URL with sslmode=require>
CORS_ALLOWED_ORIGINS=https://<your-vercel-app>.vercel.app
CSRF_TRUSTED_ORIGINS=https://<your-vercel-app>.vercel.app
SESSION_COOKIE_SAMESITE=None
CSRF_COOKIE_SAMESITE=None
SECURE_SSL_REDIRECT=1
```

Redis is optional for first boot. Add `REDIS_URL` and change `CACHE_BACKEND` to
`django.core.cache.backends.redis.RedisCache` when Celery/Beat should run with a
shared production cache.

To keep a free Render service and Neon compute warm, enable the included
`.github/workflows/keepalive.yml` workflow and add this repository secret:

```text
RENDER_HEALTH_URL=https://<your-render-service>.onrender.com/api/health/
```

The health endpoint performs a cheap database `SELECT 1`, so the ping warms both
the Render app process and the Neon database connection.
