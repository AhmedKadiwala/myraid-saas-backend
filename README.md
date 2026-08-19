# Myraid SaaS Backend

Django REST Framework backend for the Myraid SaaS CRM. It includes tenant-aware CRM models, Simple JWT authentication, dynamic RBAC, Celery tasks, billing hooks, file upload support and API documentation.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py bootstrap_saas
.\.venv\Scripts\python.exe manage.py runserver
```

The API runs at `http://localhost:8000/api/v1/` by default.

## Checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
```
