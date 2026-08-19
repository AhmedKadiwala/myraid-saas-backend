from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import BusinessPermission, Role, RolePermission, UserRole


def clear_rbac_cache():
    # LocMem supports clear and Redis backends do too. RBAC writes are rare; full
    # invalidation avoids stale privilege grants/revocations across role graphs.
    cache.clear()


@receiver([post_save, post_delete], sender=UserRole)
@receiver([post_save, post_delete], sender=RolePermission)
@receiver([post_save, post_delete], sender=Role)
@receiver([post_save, post_delete], sender=BusinessPermission)
def invalidate_rbac_cache(**kwargs):
    clear_rbac_cache()
