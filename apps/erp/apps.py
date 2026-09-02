from django.apps import AppConfig


class ErpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.erp"
    verbose_name = "Myraid ERP"
    def ready(self):
        from . import signals  # noqa:F401
