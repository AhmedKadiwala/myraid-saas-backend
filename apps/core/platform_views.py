from decimal import Decimal
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User, Tenant, TenantMembership, TenantSubscription, SubscriptionPlan, Invoice


def enforce_platform_staff(request):
    user = request.user
    if not (user and user.is_authenticated and (user.is_superuser or user.platform_admin or user.platform_role in ("owner", "sales", "developer") or user.is_staff)):
        return False
    return True


def enforce_platform_owner(request):
    user = request.user
    if not (user and user.is_authenticated and (user.is_superuser or user.platform_admin or user.platform_role == "owner")):
        return False
    return True


class PlatformOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not enforce_platform_staff(request):
            return Response({"message": "Access denied. Platform staff only."}, status=status.HTTP_403_FORBIDDEN)

        total_tenants = Tenant.objects.count()
        total_users = User.objects.count()
        active_subs = TenantSubscription.objects.filter(status="active").count()
        trial_tenants = Tenant.objects.filter(status="trial").count()

        # Calculate estimated Monthly Recurring Revenue (MRR)
        active_subscriptions = TenantSubscription.objects.filter(status="active").select_related("plan")
        mrr = sum(sub.plan.price_inr if sub.plan else 2999 for sub in active_subscriptions)

        # Tenants & Subscriptions Table Data
        tenants_data = []
        tenants = Tenant.objects.all().select_related("owner", "subscription", "subscription__plan").order_by("-created_at")[:100]

        for t in tenants:
            owner = t.owner
            sub = getattr(t, "subscription", None)
            plan_name = sub.plan.name if (sub and sub.plan) else "Standard All-Inclusive"
            plan_price = sub.plan.price_inr if (sub and sub.plan) else 2999
            sub_status = sub.status if sub else (t.status or "trial")

            members_count = TenantMembership.objects.filter(tenant=t, is_active=True).count()

            tenants_data.append({
                "id": t.id,
                "name": t.name,
                "slug": t.slug,
                "status": t.status,
                "owner_name": owner.get_full_name() or owner.username if owner else "N/A",
                "owner_email": owner.email if owner else "N/A",
                "owner_phone": owner.phone or owner.phone_e164 if owner else "N/A",
                "plan_name": plan_name,
                "plan_price_inr": plan_price,
                "subscription_status": sub_status,
                "trial_ends_at": t.trial_ends_at,
                "members_count": members_count,
                "created_at": t.created_at,
            })

        # Platform Staff Directory ("On My Side")
        staff_data = []
        staff_users = User.objects.filter(
            Q(is_superuser=True) | Q(platform_admin=True) | Q(platform_role__isnull=False) | Q(platform_sales_rep=True)
        ).distinct().order_by("email")

        for u in staff_users:
            role = u.platform_role or ("owner" if (u.is_superuser or u.platform_admin) else ("sales" if u.platform_sales_rep else "developer"))
            staff_data.append({
                "id": u.id,
                "email": u.email,
                "name": u.get_full_name() or u.username,
                "phone": u.phone or "",
                "role": role,
                "role_display": dict(User.PlatformRole.choices).get(role, role.capitalize()),
                "is_superuser": u.is_superuser,
                "last_login": u.last_login,
                "created_at": u.date_joined,
            })

        return Response({
            "metrics": {
                "total_tenants": total_tenants,
                "active_subscriptions": active_subs,
                "trial_tenants": trial_tenants,
                "total_users": total_users,
                "mrr_inr": mrr,
            },
            "tenants": tenants_data,
            "staff": staff_data,
            "current_user_platform_role": request.user.platform_role or ("owner" if request.user.is_superuser else "staff"),
        })


class PlatformStaffManageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not enforce_platform_owner(request):
            return Response({"message": "Only the Platform Owner (Super Admin) can manage platform staff."}, status=status.HTTP_403_FORBIDDEN)

        email = User.objects.normalize_email(str(request.data.get("email", "")).strip()).lower()
        name = str(request.data.get("name", "")).strip()
        password = str(request.data.get("password", "")).strip()
        role = str(request.data.get("role", "sales")).strip().lower()

        if not email or "@" not in email:
            return Response({"message": "Valid email address is required."}, status=status.HTTP_400_BAD_REQUEST)

        if role not in ("owner", "sales", "developer"):
            return Response({"message": "Role must be one of: owner, sales, developer."}, status=status.HTTP_400_BAD_REQUEST)

        first_name, _, last_name = name.partition(" ")
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "username": email.split("@")[0],
                "first_name": first_name or "Staff",
                "last_name": last_name,
                "is_staff": True,
                "is_active": True,
            }
        )

        user.platform_role = role
        user.is_staff = True
        user.department = "admin"

        if role == "owner":
            user.is_superuser = True
            user.platform_admin = True
        elif role == "sales":
            user.platform_sales_rep = True

        if password:
            user.set_password(password)

        if name:
            user.first_name = first_name
            user.last_name = last_name

        user.save()

        return Response({
            "message": f"Platform staff member '{email}' configured as '{role}'.",
            "staff": {
                "id": user.id,
                "email": user.email,
                "name": user.get_full_name(),
                "role": role,
            }
        }, status=status.HTTP_201_CREATED)


class PlatformSubscriptionActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not enforce_platform_staff(request):
            return Response({"message": "Platform staff only."}, status=status.HTTP_403_FORBIDDEN)

        tenant_id = request.data.get("tenant_id")
        action = request.data.get("action", "activate") # "activate", "extend_trial", "cancel"

        try:
            tenant = Tenant.objects.get(pk=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"message": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        sub, _ = TenantSubscription.objects.get_or_create(tenant=tenant)

        if action == "activate":
            sub.status = "active"
            tenant.status = "active"
            sub.save(update_fields=["status"])
            tenant.save(update_fields=["status"])
            msg = f"Subscription for '{tenant.name}' has been activated."
        elif action == "extend_trial":
            days = int(request.data.get("days", 14))
            tenant.trial_ends_at = timezone.now() + timezone.timedelta(days=days)
            tenant.status = "trial"
            sub.status = "trial"
            sub.save(update_fields=["status"])
            tenant.save(update_fields=["trial_ends_at", "status"])
            msg = f"Trial for '{tenant.name}' extended by {days} days."
        elif action == "cancel":
            sub.status = "cancelled"
            tenant.status = "suspended"
            sub.save(update_fields=["status"])
            tenant.save(update_fields=["status"])
            msg = f"Subscription for '{tenant.name}' cancelled."
        else:
            return Response({"message": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": msg, "tenant_id": tenant.id, "status": tenant.status})
