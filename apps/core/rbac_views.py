from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from .api import APIView
from .models import (
    AuditLog,
    Branch,
    BusinessPermission,
    Role,
    RolePermission,
    TenantMembership,
    UserRole,
)
from .serializers import (
    AuditLogSerializer,
    BranchSerializer,
    BusinessPermissionSerializer,
    RoleSerializer,
    UserRoleSerializer,
)
from .services import (
    audit,
    effective_permissions,
    enforce_tenant_admin,
    resolve_branch,
    resolve_tenant,
)


class PermissionListView(APIView):
    def get(self, request):
        tenant = enforce_tenant_admin(request)
        permissions = BusinessPermission.objects.filter(is_active=True).order_by(
            "module", "code"
        )
        return Response({
            "message": "Permissions fetched successfully",
            "permissions": BusinessPermissionSerializer(permissions, many=True).data,
        })


class RoleListCreateView(APIView):
    def get(self, request):
        tenant = enforce_tenant_admin(request)
        roles = Role.objects.filter(
            Q(tenant=tenant)
            | Q(tenant__isnull=True, approved_for_tenant_assignment=True),
            is_active=True,
        )
        return Response({"roles": RoleSerializer(roles, many=True).data})

    def post(self, request):
        tenant = enforce_tenant_admin(request)
        role = Role.objects.create(
            tenant=tenant,
            name=request.data["name"],
            code=request.data["code"],
            description=request.data.get("description", ""),
        )
        permission_ids = request.data.get("permission_ids", [])
        for permission in BusinessPermission.objects.filter(pk__in=permission_ids):
            RolePermission.objects.create(role=role, permission=permission)
        audit(
            actor=request.user, tenant=tenant, action="role.created", resource=role,
            after=RoleSerializer(role).data, request=request,
        )
        return Response(RoleSerializer(role).data, status=status.HTTP_201_CREATED)


class RoleDetailView(APIView):
    def put(self, request, role_id):
        tenant = enforce_tenant_admin(request)
        role = Role.objects.filter(pk=role_id, tenant=tenant, is_system=False).first()
        if not role:
            return Response({"message": "Role not found or immutable"}, status=404)
        before = RoleSerializer(role).data
        for field in ("name", "description", "is_active"):
            if field in request.data:
                setattr(role, field, request.data[field])
        with transaction.atomic():
            role.save()
            if "permission_ids" in request.data:
                role.permission_links.all().delete()
                RolePermission.objects.bulk_create([
                    RolePermission(role=role, permission=permission)
                    for permission in BusinessPermission.objects.filter(
                        pk__in=request.data["permission_ids"], is_active=True
                    )
                ])
        audit(
            actor=request.user, tenant=tenant, action="role.updated", resource=role,
            before=before, after=RoleSerializer(role).data, request=request,
        )
        return Response(RoleSerializer(role).data)

    def delete(self, request, role_id):
        tenant = enforce_tenant_admin(request)
        role = Role.objects.filter(pk=role_id, tenant=tenant, is_system=False).first()
        if not role:
            return Response({"message": "Role not found or immutable"}, status=404)
        before = RoleSerializer(role).data
        role.is_active = False
        role.save(update_fields=["is_active"])
        audit(
            actor=request.user, tenant=tenant, action="role.disabled", resource=role,
            before=before, request=request,
        )
        return Response(status=204)


class UserRoleListCreateView(APIView):
    def get(self, request):
        tenant = enforce_tenant_admin(request)
        assignments = UserRole.objects.filter(tenant=tenant).select_related("role")
        return Response({"assignments": UserRoleSerializer(assignments, many=True).data})

    def post(self, request):
        tenant = enforce_tenant_admin(request)
        user_id = request.data.get("user")
        if not TenantMembership.objects.filter(
            tenant=tenant, user_id=user_id, is_active=True
        ).exists():
            return Response({"message": "User is not a tenant member"}, status=400)
        role = Role.objects.filter(pk=request.data.get("role"), is_active=True).filter(
            Q(tenant=tenant)
            | Q(tenant__isnull=True, approved_for_tenant_assignment=True)
        ).first()
        if not role:
            return Response({"message": "Role is not assignable in this tenant"}, status=400)
        branch = None
        if request.data.get("branch"):
            branch = Branch.objects.filter(
                pk=request.data["branch"], tenant=tenant, is_active=True
            ).first()
            if not branch:
                return Response({"message": "Invalid branch scope"}, status=400)
        assignment = UserRole(
            tenant=tenant,
            user_id=user_id,
            role=role,
            branch=branch,
            valid_from=request.data.get("valid_from"),
            valid_to=request.data.get("valid_to"),
            assigned_by=request.user,
        )
        assignment.full_clean()
        assignment.save()
        audit(
            actor=request.user, tenant=tenant, action="user_role.assigned",
            resource=assignment, after=UserRoleSerializer(assignment).data,
            request=request,
        )
        return Response(UserRoleSerializer(assignment).data, status=201)


class UserRoleDetailView(APIView):
    def delete(self, request, assignment_id):
        tenant = enforce_tenant_admin(request)
        assignment = UserRole.objects.filter(pk=assignment_id, tenant=tenant).first()
        if not assignment:
            return Response({"message": "Assignment not found"}, status=404)
        before = UserRoleSerializer(assignment).data
        audit(
            actor=request.user, tenant=tenant, action="user_role.revoked",
            resource=assignment, before=before, request=request,
        )
        assignment.delete()
        return Response(status=204)


class EffectivePermissionView(APIView):
    def get(self, request):
        tenant = resolve_tenant(request)
        user_id = request.query_params.get("user", request.user.pk)
        if str(user_id) != str(request.user.pk):
            enforce_tenant_admin(request)
        if not TenantMembership.objects.filter(tenant=tenant, user_id=user_id).exists():
            return Response({"message": "User not found"}, status=404)
        branch = None
        branch_id = request.query_params.get("branch")
        if branch_id:
            branch = Branch.objects.filter(pk=branch_id, tenant=tenant).first()
            if not branch:
                return Response({"message": "Branch not found"}, status=404)
        from .models import User
        user = User.objects.get(pk=user_id)
        return Response({
            "user": user.pk,
            "tenant": tenant.pk,
            "branch": getattr(branch, "pk", None),
            "permissions": sorted(effective_permissions(user, tenant, branch)),
        })


class AuditLogListView(APIView):
    def get(self, request):
        tenant = enforce_tenant_admin(request)
        logs = AuditLog.objects.filter(tenant=tenant).order_by("-created_at")[:500]
        return Response({"audit_logs": AuditLogSerializer(logs, many=True).data})
