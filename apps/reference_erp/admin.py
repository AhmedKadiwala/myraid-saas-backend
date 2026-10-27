from django.contrib import admin
from .models import CompanyInvitation, OrderAttachment


@admin.register(CompanyInvitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("tenant", "name", "phone", "role", "invited_by", "status", "expires_at", "accepted_at", "delivery_status")
    search_fields = ("name", "phone", "tenant__name", "role__name")
    list_filter = ("status", "tenant")
    exclude = ("token_hash",)
    readonly_fields = ("tenant", "name", "phone", "role", "invited_by", "status", "expires_at", "accepted_at", "communication", "created_at")

    @admin.display(description="WhatsApp delivery")
    def delivery_status(self, obj):
        return obj.communication.status if obj.communication_id else "Not configured"

    def has_add_permission(self, request):
        return False


@admin.register(OrderAttachment)
class OrderAttachmentAdmin(admin.ModelAdmin):
    list_display = ("tenant", "order_code", "name", "size", "uploaded_by", "created_at")
    search_fields = ("order_code", "name", "tenant__name")
    list_filter = ("tenant",)
    readonly_fields = ("tenant", "order_code", "name", "object_key", "size", "uploaded_by", "created_at")

    def has_add_permission(self, request):
        return False
