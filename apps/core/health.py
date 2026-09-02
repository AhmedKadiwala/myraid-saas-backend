from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    database_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database_ok = cursor.fetchone()[0] == 1
    except Exception:
        database_ok = False

    status = 200 if database_ok else 503
    response = JsonResponse({
        "status": "ok" if database_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "checked_at": timezone.now().isoformat(),
    }, status=status)
    response["Cache-Control"] = "no-store"
    return response
